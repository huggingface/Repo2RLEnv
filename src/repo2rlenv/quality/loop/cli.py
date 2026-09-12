"""CLI entry point using the shared console and campaign ledger."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.table import Table
from rich.text import Text

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.quality.loop.models import LoopOptions, LoopResult
from repo2rlenv.spec.input import LLMSpec
from repo2rlenv.ui import console


def model_spec(value: str) -> LLMSpec:
    provider, separator, model = value.partition("/")
    if not separator or not model or provider not in {"openai", "anthropic"}:
        raise argparse.ArgumentTypeError("Use openai/MODEL or anthropic/MODEL")
    return LLMSpec(provider=provider, model=model, timeout_sec=180)


def display(result: LoopResult):
    console.kv(
        {
            "status": result.status,
            "profile": result.profile,
            "task": result.task_path,
            "repairs": result.repairs,
            "accounted": "$" + result.accounted_usd,
            "reserved": "$" + result.reserved_usd,
        },
        title="Harbor quality review",
    )
    if result.review:
        table = Table("Criterion", "Status", "Score / 4", "Finding")
        for name in ("task", "verifier", "leakage"):
            item = getattr(result.review, name)
            table.add_row(name, item.status, str(item.score), Text(item.explanation))
        console.print(table)
    for reason in result.reasons:
        console.print(Text("• " + reason))


def cmd_quality(args) -> int:
    from repo2rlenv.quality.loop.runner import QualityLoop

    try:
        if args.quality_action == "show":
            result = LoopResult.model_validate_json((args.path / "result.json").read_text())
        else:
            if args.env_file:
                from dotenv import load_dotenv

                if not args.env_file.is_file():
                    raise ValueError("--env-file must name an existing credentials file")
                load_dotenv(args.env_file, override=False)
            options = LoopOptions(**{key: getattr(args, key) for key in LoopOptions.model_fields})
            if (options.repair or options.run_rollout) and not args.runtime_wheel:
                raise ValueError(
                    "--repair and --run-rollout require --runtime-wheel (build with uv build)"
                )
            ledger = BudgetLedger(args.campaign / "budget.sqlite3")

            def on_event(event):
                if not args.json:
                    console.print(Text(f"[{event.stage}] {event.message}"))

            loop = QualityLoop(options, args.out, ledger, on_event=on_event)
            if options.repair or options.run_rollout:
                from repo2rlenv.quality.loop.remote import RemoteTrials

                loop.remote = RemoteTrials(
                    args.out,
                    loop.budget,
                    options,
                    wheel=args.runtime_wheel,
                    provider=args.provider,
                    worker_receipt=args.worker_receipt,
                    worker_reservation_usd=args.worker_reservation_usd,
                )
            result = loop.run(
                args.task,
                baseline=args.baseline,
                oracle=args.oracle,
                rollout=args.rollout,
                probes=args.probes,
                resume=args.resume,
            )
            # Worker cleanup retains its hold until campaign reconciliation.
            result = result.model_copy(update=loop.budget.totals())
            from repo2rlenv.execution.lifecycle import save_record

            save_record(args.out / "result.json", result.model_dump(mode="json"))
        if args.json:
            console.json(result.model_dump(mode="json"))
        else:
            display(result)
        return 0 if result.status in {"usable", "reviewed"} else 1
    except (ValueError, OSError, RuntimeError) as exc:
        if args.json:
            console.json({"error": type(exc).__name__, "message": str(exc)})
        else:
            console.print(Text(f"{type(exc).__name__}: {exc}"))
        return 2


def add_quality_parser(subparsers):
    parser = subparsers.add_parser(
        "quality", help="Review and repair a Harbor task using rollout evidence"
    )
    actions = parser.add_subparsers(dest="quality_action", required=True)
    show = actions.add_parser("show", help="Read a completed quality report without paid calls")
    show.add_argument("path", type=Path)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_quality)
    run = actions.add_parser(
        "run", help="Review existing evidence or run bounded remote validation and repairs"
    )
    run.add_argument("task", type=Path)
    run.add_argument("--campaign", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument(
        "--env-file", type=Path, help="Load credentials without copying them into task artifacts"
    )
    for role in ("baseline", "oracle", "rollout"):
        run.add_argument(
            "--" + role,
            type=Path,
            help="Existing trial receipt, result.json, or single-trial directory",
        )
    run.add_argument(
        "--probes",
        type=Path,
        help="Task-bound JSON manifest of known semantic counterexamples/alternatives",
    )
    defaults = LoopOptions()
    for name in ("review_model", "repair_model", "escalation_model", "solver_model"):
        run.add_argument(
            "--" + name.replace("_", "-"), type=model_spec, default=getattr(defaults, name)
        )
    for name in (
        "max_repairs",
        "max_read_rounds",
        "max_probes",
        "context_chars",
        "model_tokens",
        "max_turns",
        "solver_tokens",
        "trial_timeout_sec",
    ):
        run.add_argument("--" + name.replace("_", "-"), type=int, default=getattr(defaults, name))
    for name in ("model_reservation_usd", "solver_reservation_usd", "max_spend_usd"):
        run.add_argument("--" + name.replace("_", "-"), default=getattr(defaults, name))
    run.add_argument("--success-reward", type=float, default=defaults.success_reward)
    run.add_argument(
        "--repair",
        action="store_true",
        help="Author repairs and validate each new revision remotely",
    )
    run.add_argument(
        "--run-rollout",
        action="store_true",
        help="Run missing controls, probes and a blind solver remotely",
    )
    run.add_argument("--provider", choices=["modal", "daytona"], default="modal")
    run.add_argument("--runtime-wheel", type=Path)
    run.add_argument(
        "--worker-receipt", type=Path, help="Reuse a running worker; caller owns its cleanup"
    )
    run.add_argument("--worker-reservation-usd", default="3.00")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_quality)
