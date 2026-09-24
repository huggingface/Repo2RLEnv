"""CodeMidas-specific evidence inspection and paper-style audit commands."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.ui import console
from repo2rlenv.ui.errors import report_error


def command(args):
    try:
        if args.codemidas_action == "source":
            from repo2rlenv.pipelines.recipes.codemidas.stack import provenance, read_manifest

            result = provenance(read_manifest(args.manifest), args.materialization)
        else:
            from repo2rlenv.pipelines.recipes.codemidas.audit import audit_task

            result = audit_task(
                task=args.task,
                controls=args.controls,
                directory=args.out,
                campaign=args.campaign,
                worker_receipt=args.worker_receipt,
                wheel=args.runtime_wheel,
                screen_attempts=args.screen_attempts,
                attempt_concurrency=args.attempt_concurrency,
                max_cost=args.max_cost,
                resume=args.resume,
            )
        console.json(result)
        return 0
    except Exception as exc:
        return report_error(exc, json_output=True, verbose=getattr(args, "verbose", False))


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "codemidas", help="Inspect sources and audit CodeMidas Harbor tasks"
    )
    actions = parser.add_subparsers(dest="codemidas_action", required=True)
    source = actions.add_parser(
        "source", help="Validate a pinned Stack v3 repository manifest without executing it"
    )
    source.add_argument("manifest", type=Path)
    source.add_argument("--materialization", choices=["inline", "hydrated"], default="inline")
    source.set_defaults(func=command)
    audit = actions.add_parser(
        "audit", help="Run adversarial/solver audits and separate curriculum screening"
    )
    audit.add_argument("task", type=Path)
    for flag in ("controls", "out", "campaign", "worker-receipt", "runtime-wheel"):
        audit.add_argument("--" + flag, type=Path, required=True)
    audit.add_argument("--screen-attempts", type=int, default=4)
    audit.add_argument(
        "--attempt-concurrency",
        type=int,
        choices=range(1, 5),
        default=2,
        help="Independent solver attempts in parallel; each has its own environment and receipt",
    )
    audit.add_argument(
        "--max-cost",
        default="6",
        help="Trial allowance in USD; independent review has a separate $1.25 maximum",
    )
    audit.add_argument("--resume", action="store_true")
    audit.set_defaults(func=command)
