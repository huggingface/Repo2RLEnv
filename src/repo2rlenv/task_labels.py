"""CLI for retaining, filtering, and diagnosing generated Harbor tasks."""

from __future__ import annotations

from pathlib import Path

from rich.table import Table
from rich.text import Text

from repo2rlenv.emitter.evaluation import EvaluationLabel, evaluation_time
from repo2rlenv.quality.labels import label_from_quality, read_evaluation, write_labeled_copy
from repo2rlenv.ui import console
from repo2rlenv.ui.errors import report_error


def command(args) -> int:
    try:
        if args.tasks_action == "label":
            if args.quality_result:
                label = label_from_quality(
                    args.task, args.quality_result, provenance=args.provenance
                )
            else:
                if args.status != "unverified" and not (args.reason_code and args.detail):
                    raise ValueError("A repair/blocked label needs --reason-code and --detail")
                label = EvaluationLabel(
                    status=args.status,
                    stage=args.stage,
                    reason_codes=args.reason_code or ["validation_not_run"],
                    detail=args.detail or "Generated task; validation has not been established.",
                    checked_at=evaluation_time() if args.reason_code else None,
                    provenance=args.provenance,
                )
            task = write_labeled_copy(args.task, args.out, label)
            rows = [{"task": str(task), **read_evaluation(task).toml_metadata()}]
        else:
            tasks = (
                [args.path]
                if args.tasks_action == "show"
                else (path.parent for path in sorted(args.path.rglob("task.toml")))
            )
            rows = []
            for task in tasks:
                label = read_evaluation(task)
                if args.tasks_action == "list" and args.status and label.status != args.status:
                    continue
                rows.append({"task": str(task), **label.toml_metadata()})
        if args.json:
            console.json(rows)
        else:
            table = Table("Task", "Status", "Stage", "Provenance", "Reasons")
            for row in rows:
                table.add_row(
                    *[
                        Text(str(value))
                        for value in (
                            row["task"],
                            row["status"],
                            row["stage"],
                            row["provenance"],
                            ", ".join(row["reason_codes"]),
                        )
                    ]
                )
            console.print(table)
            if len(rows) == 1:
                console.print(Text(rows[0]["detail"]))
            console.print(
                "Labels describe saved evidence; acceptance requires checking that evidence."
            )
        return 0
    except (OSError, ValueError) as exc:
        return report_error(exc, json_output=args.json, verbose=getattr(args, "verbose", False))


def add_tasks_parser(subparsers):
    parser = subparsers.add_parser("tasks", help="Retain and inspect Harbor evaluation labels")
    actions = parser.add_subparsers(dest="tasks_action", required=True)
    for name in ("show", "list"):
        action = actions.add_parser(name, help=f"{name.title()} advisory task labels")
        action.add_argument("path", type=Path)
        action.add_argument("--json", action="store_true")
        if name == "list":
            action.add_argument(
                "--status", choices=("unverified", "verified", "needs_repair", "blocked")
            )
        action.set_defaults(func=command)
    label = actions.add_parser("label", help="Publish a labeled copy; preserve the original")
    label.add_argument("task", type=Path)
    label.add_argument("--out", type=Path, required=True, help="New task directory")
    source = label.add_mutually_exclusive_group()
    source.add_argument("--quality-result", type=Path, help="Saved quality-loop result.json")
    source.add_argument(
        "--status", choices=("unverified", "needs_repair", "blocked"), default="unverified"
    )
    label.add_argument(
        "--stage",
        choices=(
            "generation",
            "bootstrap",
            "construction",
            "review",
            "controls",
            "probes",
            "rollout",
            "repair",
            "complete",
            "unknown",
        ),
        default="generation",
    )
    label.add_argument("--reason-code", action="append", help="Repeatable snake_case diagnosis")
    label.add_argument("--detail", help="Human-readable reason and next diagnostic step")
    label.add_argument(
        "--provenance", choices=("unknown", "assisted", "unattended"), default="unknown"
    )
    label.add_argument("--json", action="store_true")
    label.set_defaults(func=command)
