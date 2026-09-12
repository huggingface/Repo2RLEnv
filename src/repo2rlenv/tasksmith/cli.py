"""Tasksmith CLI: explicit pilot inputs, bounded spend, durable progress."""

from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.tasksmith.models import Options, Panel
from repo2rlenv.ui import console


def command(args):
    if args.action == "bootstrap":
        return _bootstrap(args)
    if args.action == "install-runtime":
        from repo2rlenv.tasksmith.runtime import install

        console.print(str(install()))
        return 0
    if args.action == "show":
        report = json.loads((args.output / "report.json").read_text())
    else:
        from dotenv import load_dotenv

        from repo2rlenv.tasksmith.runner import Tasksmith

        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError("Credentials file does not exist")
            load_dotenv(args.env_file, override=False)
        options = (
            Options.model_validate_json(args.options.read_text())
            if args.options
            else Options(provider=args.provider or "modal", author_runtime=args.author or "pi")
        )
        if args.options and (
            (args.provider and args.provider != options.provider)
            or (args.author and args.author != options.author_runtime)
        ):
            raise ValueError("Explicit provider/author flags conflict with the options file")
        if getattr(args, "bootstrap_report", None):
            from repo2rlenv.tasksmith.bootstrap_inputs import apply_cpu_bootstrap

            options = apply_cpu_bootstrap(options, args.bootstrap_report)
        panel = Panel.model_validate_json(args.panel.read_text())
        runner = Tasksmith(
            args.output,
            args.campaign,
            options,
            args.runtime_wheel,
            on_event=None
            if args.json
            else lambda stage, message: console.print(f"[{stage}] {message}", markup=False),
        )
        report = runner.run(
            panel,
            limit=args.stop_after,
            generation_run=args.generation_run,
            reuse_evidence=args.reuse_evidence,
        )
    if args.json:
        console.json(report)
    else:
        from rich.table import Table

        table = Table(title=f"Tasksmith: {report['panel']}")
        table.add_column("PR")
        table.add_column("State")
        table.add_column("Task")
        for row in report["candidates"]:
            table.add_row(
                row["source"] if isinstance(row["source"], str) else row["source"]["url"],
                row["status"],
                row.get("quality", {}).get(
                    "task_path", row.get("constructed", {}).get("local", "")
                ),
            )
        console.print(table)
        console.print(
            f"{report['generated']}/{report['inputs']} generated; {report['usable']}/{report['inputs']} usable. Accounted ${report['accounted_usd']}; reserved ${report['reserved_usd']}."
        )
    return 0 if report["usable"] == report["inputs"] else 1


def _bootstrap(args):
    from dotenv import load_dotenv
    from rich.table import Table

    from repo2rlenv.tasksmith.bootstrap_matrix import RepositoryBootstrap
    from repo2rlenv.tasksmith.matrix_runner import run_cpu_matrix, run_gpu_repository

    specs = [RepositoryBootstrap.model_validate(row) for row in json.loads(args.matrix.read_text())]
    if not specs or len({spec.name for spec in specs}) != len(specs):
        raise ValueError("Bootstrap matrix must contain distinct repositories")
    if args.env_file:
        if not args.env_file.is_file():
            raise ValueError("Credentials file does not exist")
        load_dotenv(args.env_file, override=False)
    on_event = (
        (lambda _: None) if args.json else lambda message: console.print(message, markup=False)
    )
    reports = []
    if args.resource in {"cpu", "both"}:
        reports.append(
            run_cpu_matrix(
                args.matrix,
                args.output / "cpu",
                args.campaign,
                args.runtime_wheel,
                on_event=on_event,
            )
        )
    if args.resource in {"gpu", "both"}:
        for spec in specs:
            report = run_gpu_repository(
                spec, args.output / "gpu" / spec.name, args.campaign, on_event=on_event
            )
            reports.append(report)
            if report["status"] != "ready":
                # Preserve partial results. Resolve this attempt before another allocation,
                # including an ambiguous provider create or a shared image-build failure.
                break
    if args.json:
        console.json({"bootstrap": reports})
    else:
        table = Table(title="Tasksmith repository readiness")
        for column in ("Repository", "Resource", "Status", "Checked behavior"):
            table.add_column(column)
        for report in reports:
            for row in report.get("repositories", [report]):
                table.add_row(
                    row["source"]["name"],
                    report["resource"],
                    row["status"],
                    "; ".join(row.get("smoke", {}).get("checks", []))
                    or row.get("error", "See retained logs"),
                )
        console.print(table)
        console.print(
            f"Receipts, logs and compute estimates: {args.output.resolve()}", markup=False
        )
    return int(
        any(
            report.get("status") not in {"ready", "completed"}
            or any(row["status"] != "ready" for row in report.get("repositories", []))
            for report in reports
        )
    )


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "tasksmith", help="Build faithful Harbor tasks from PRs with a remote coding agent"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    bootstrap = actions.add_parser(
        "bootstrap", help="Build and smoke-test pinned HF repositories on remote CPU/GPU workers"
    )
    bootstrap.add_argument("matrix", type=Path)
    bootstrap.add_argument("--resource", choices=("cpu", "gpu", "both"), default="both")
    for name in ("campaign", "output", "runtime-wheel"):
        bootstrap.add_argument("--" + name, required=True, type=Path)
    bootstrap.add_argument("--env-file", type=Path)
    bootstrap.add_argument("--json", action="store_true")
    bootstrap.set_defaults(func=command)
    install = actions.add_parser(
        "install-runtime", help="Install pinned Pi/OpenCode libraries in the user cache"
    )
    install.set_defaults(func=command)
    show = actions.add_parser("show", help="Read a saved pilot report without paid calls")
    show.add_argument("output", type=Path)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=command)
    run = actions.add_parser("run", help="Run or resume a fixed PR panel")
    run.add_argument("panel", type=Path)
    for name in ("campaign", "output", "runtime-wheel"):
        run.add_argument("--" + name, required=True, type=Path)
    run.add_argument(
        "--generation-run",
        type=Path,
        help="Reuse verified generation artifacts from an identical panel; run the current quality policy without regenerating source",
    )
    run.add_argument(
        "--reuse-evidence",
        action="store_true",
        help="Reuse matching baseline, reference and solver evidence from --generation-run; rerun semantic probes",
    )
    run.add_argument("--env-file", type=Path)
    run.add_argument(
        "--bootstrap-report",
        type=Path,
        help="Reuse the snapshot and dependency hints from a completed CPU bootstrap report",
    )
    run.add_argument(
        "--options",
        type=Path,
        help="Explicit Options JSON, including quality model and spending limits",
    )
    run.add_argument("--provider", choices=("modal", "daytona"))
    run.add_argument("--author", choices=("pi", "opencode"))
    run.add_argument(
        "--stop-after",
        type=int,
        help="Process the first N frozen inputs; the reported denominator remains the full panel",
    )
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=command)
