"""Experimental benchmark and feedback commands on the existing quality CLI."""

from __future__ import annotations

import json
from pathlib import Path

from rich.table import Table
from rich.text import Text

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.quality.benchmark.models import BenchmarkOptions, load_manifest
from repo2rlenv.quality.loop.models import LoopOptions
from repo2rlenv.ui import console
from repo2rlenv.ui.errors import report_error


def load_env(path):
    if path is not None:
        from dotenv import load_dotenv

        if not path.is_file():
            raise ValueError("--env-file must name an existing credentials file")
        load_dotenv(path, override=False)


def display_findings(path):
    from repo2rlenv.quality.reviewer.profile import load_review

    record, _, _ = load_review(path)
    table = Table("Criterion", "Status", "Score / 4")
    for item in record["review"]["criteria"]:
        table.add_row(item["criterion"], item["status"], str(item["score"]))
    console.print(table)
    for issue in record["findings"]:
        console.print(
            Text(
                f"{issue['issue_id']} · {issue['severity']} · {issue['basis']} · "
                f"{issue['attribution']}: {issue['problem']}"
            )
        )
        for citation in issue["evidence"]:
            console.print(Text(f"  {citation['path']}: {citation['quote']}"))


def display_benchmark(report):
    console.kv(
        {
            "state": report["state"],
            "split": report["split"],
            "attempts": f"{len(report['attempts'])}/{report['scheduled_attempts']}",
            "promotion": report["summary"]["promotion"],
        },
        title="Reviewer benchmark",
    )
    table = Table("Profile", "Labels", "Detected", "Blocked clean labels", "Decidable", "Errors")
    for profile, groups in report["summary"]["profiles"].items():
        for label, data in groups.items():
            if data["attempts"]:
                table.add_row(
                    profile,
                    label,
                    f"{data['detected']}/{data['defective_attempts']}",
                    f"{data['false_blocked']}/{data['clean_attempts']}",
                    f"{data['decidable']}/{data['attempts']}",
                    str(data["execution_errors"]),
                )
    console.print(table)
    if any(groups["provisional"]["attempts"] for groups in report["summary"]["profiles"].values()):
        console.print(
            "Provisional clean labels are unverified; a blocking finding may reveal a real defect."
        )


def cmd_benchmark(args):
    try:
        if args.benchmark_action == "validate":
            manifest = load_manifest(args.manifest)
            data = {
                "valid": True,
                "cases": len(manifest.cases),
                "families": len({c.family for c in manifest.cases}),
                "adjudicated": sum(c.audit.status == "adjudicated" for c in manifest.cases),
            }
        elif args.benchmark_action == "show":
            path = args.path / "report.json" if args.path.is_dir() else args.path
            data = json.loads(path.read_text())
        else:
            from repo2rlenv.quality.benchmark.runner import run_benchmark

            load_env(args.env_file)
            options = BenchmarkOptions.model_validate_json(args.config.read_text())

            def progress(row, done, total):
                if not args.json:
                    console.print(
                        Text(f"[{done}/{total}] {row['profile']} {row['case_id']}: {row['status']}")
                    )

            data = run_benchmark(
                args.manifest,
                options,
                args.out,
                BudgetLedger(args.campaign / "budget.sqlite3"),
                split=args.split,
                resume=args.resume,
                selection_report=args.selection_report,
                on_event=progress,
            )
        if args.json or args.benchmark_action == "validate":
            console.json(data)
        else:
            display_benchmark(data)
        return 1 if any(not row["completed"] for row in data.get("attempts", [])) else 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        return report_error(exc, json_output=args.json)


def cmd_feedback(args):
    from repo2rlenv.quality.feedback import adjudicate_feedback, record_feedback

    try:
        if args.feedback_action == "record":
            path = record_feedback(
                args.review,
                issue_id=args.issue,
                author=args.author,
                decision=args.decision,
                reason=args.reason_file.read_text(),
                directory=args.out,
            )
            data = {"feedback": str(path), "label_changed": False}
        else:
            load_env(args.env_file)
            options = LoopOptions.model_validate_json(args.config.read_text())
            data = adjudicate_feedback(
                args.feedback,
                directory=args.out,
                ledger=BudgetLedger(args.campaign / "budget.sqlite3"),
                options=options,
                resume=args.resume,
            )
        console.json(data)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        return report_error(exc, json_output=args.json)


def add_reviewer_parsers(actions):
    benchmark = actions.add_parser(
        "benchmark", help="Compare reviewers against private audited cases"
    )
    choices = benchmark.add_subparsers(dest="benchmark_action", required=True)
    validate = choices.add_parser(
        "validate", help="Check hashes, labels and family splits without paid calls"
    )
    validate.add_argument("manifest", type=Path)
    show = choices.add_parser("show", help="Read benchmark results without paid calls")
    show.add_argument("path", type=Path)
    run = choices.add_parser("run", help="Compare both reviewer profiles on one frozen split")
    run.add_argument("manifest", type=Path)
    run.add_argument("--split", required=True, choices=["development", "selection", "final"])
    run.add_argument("--config", required=True, type=Path, help="BenchmarkOptions JSON")
    run.add_argument("--campaign", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--env-file", type=Path)
    run.add_argument("--selection-report", type=Path)
    run.add_argument("--resume", action="store_true")
    for parser in (validate, show, run):
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_benchmark)
    feedback = actions.add_parser(
        "feedback", help="Record or adjudicate a finding without changing task labels"
    )
    choices = feedback.add_subparsers(dest="feedback_action", required=True)
    record = choices.add_parser("record", help="Attach attributed feedback to a frozen finding")
    record.add_argument("review", type=Path)
    record.add_argument("--issue", required=True)
    record.add_argument("--author", required=True)
    record.add_argument("--decision", required=True, choices=["confirmed", "disputed", "corrected"])
    record.add_argument("--reason-file", required=True, type=Path)
    record.add_argument("--out", required=True, type=Path)
    adjudicate = choices.add_parser(
        "adjudicate", help="Request a metered, evidence-grounded second opinion"
    )
    adjudicate.add_argument("feedback", type=Path)
    adjudicate.add_argument(
        "--config",
        required=True,
        type=Path,
        help="LoopOptions JSON; review_model is the adjudicator",
    )
    adjudicate.add_argument("--campaign", required=True, type=Path)
    adjudicate.add_argument("--out", required=True, type=Path)
    adjudicate.add_argument("--env-file", type=Path)
    adjudicate.add_argument("--resume", action="store_true")
    for parser in (record, adjudicate):
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_feedback)
