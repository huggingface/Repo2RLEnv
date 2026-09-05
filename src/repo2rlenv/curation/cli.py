from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from repo2rlenv.curation.models import CampaignConfig
from repo2rlenv.curation.sources import parse_seeds, repo_seeds
from repo2rlenv.ui import console


def register(subparsers) -> None:
    audit = subparsers.add_parser(
        "curation-audit", help="Audit retained curation evidence without paid calls"
    )
    audit.add_argument("--workspace", type=Path, required=True)
    audit.add_argument("--seeds", type=Path, required=True)
    audit.add_argument("--ledger", type=Path, action="append", default=[])
    audit.add_argument("--selection", type=Path, action="append", default=[])
    audit.add_argument("--out", type=Path, required=True)
    audit.set_defaults(func=cmd_audit)
    pilot = subparsers.add_parser(
        "curation-pilot", help="Run a frozen five-PR, $40 controlled pilot"
    )
    pilot.add_argument("--protocol", type=Path, required=True)
    pilot.add_argument("--out", type=Path, required=True)
    pilot.set_defaults(func=cmd_pilot)
    parser = subparsers.add_parser(
        "curate", help="Build and review PR tasks in cloud sandboxes (requires [curation])"
    )
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument(
        "--pr", action="append", help="GitHub PR URL; repeat for multiple candidates"
    )
    sources.add_argument("--seeds", type=Path, help="Markdown/text file containing seed PR URLs")
    sources.add_argument("--repo", help="GitHub owner/name; discover recent merged PRs")
    parser.add_argument("--out", type=Path, default=Path("workspace/curation"))
    parser.add_argument(
        "--compare-runtimes",
        action="store_true",
        help="Compare LangGraph, Pi and OpenCode authors on every input PR",
    )
    parser.add_argument(
        "--retry-rejected",
        action="store_true",
        help="Retry rejected candidates, preserving earlier evidence and spend",
    )
    parser.add_argument(
        "--config", type=Path, help="JSON CampaignConfig; exact config retained for resume"
    )
    parser.add_argument("--target", type=int, help="Desired accepted count (default 30)")
    parser.add_argument(
        "--budget-usd", type=float, help="Total reservations/spend ceiling (default 450, max 500)"
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Resolve seed URLs and config without spending or starting sandboxes",
    )
    parser.set_defaults(func=cmd_curate)


def cmd_audit(args: argparse.Namespace) -> int:
    from repo2rlenv.curation.funnel import audit_funnel, write_report

    report = audit_funnel(
        args.workspace,
        seed_paths=(args.seeds,),
        ledger_paths=tuple(args.ledger),
        selection_paths=tuple(args.selection),
    )
    _, markdown = write_report(report, args.out)
    console.kv({"report": str(markdown.resolve())}, title="Curation audit")
    return 0


def cmd_pilot(args: argparse.Namespace) -> int:
    from repo2rlenv.curation.pilot import run_pilot

    result = asyncio.run(run_pilot(args.protocol, args.out))
    console.kv(
        {
            "status": result["status"],
            "automatic_accepted": result["automatic_accepted"],
            "selected": "pending independent audit",
            "charged_or_reserved_usd": result["charged_or_reserved_usd"],
        },
        title="Controlled pilot",
    )
    return 0


def cmd_curate(args: argparse.Namespace) -> int:
    values = json.loads(args.config.read_text()) if args.config else {}
    if args.target is not None:
        values["target"] = args.target
    if args.budget_usd is not None:
        values["budget_usd"] = args.budget_usd
    config = CampaignConfig.model_validate(values)
    seeds = args.pr or (parse_seeds(args.seeds) if args.seeds else repo_seeds(args.repo))
    if not seeds:
        raise ValueError("No merged PR candidates found")
    if args.plan:
        console.kv(
            {
                "seed_count": len(seeds),
                "target": config.target,
                "budget_usd": config.budget_usd,
                "provider": "modal",
                "models": ", ".join(config.solver_models),
            },
            title="Curation plan",
        )
        return 0
    import importlib.util

    missing = [
        name
        for name in ("harbor", "modal", "langgraph", "dockerfile_parse")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        console.error(
            "Curation dependencies missing: "
            + ", ".join(missing)
            + ". Install with `uv sync --extra curation` or `pip install 'repo2rlenv[curation]'`."
        )
        return 2
    from repo2rlenv.curation.campaign import campaign

    if args.compare_runtimes:
        from repo2rlenv.curation.compare import compare

        result = asyncio.run(compare(seeds, args.out, config, retry_failures=args.retry_rejected))
        console.kv(
            {
                "status": result["status"],
                "cells": len(result["rows"]),
                "report": str(args.out.resolve() / "comparison.md"),
            },
            title="Runtime comparison",
        )
        return 0 if result["status"] == "complete" else 2

    result = asyncio.run(campaign(seeds, args.out, config, retry_rejected=args.retry_rejected))
    console.kv(
        {
            "status": result["status"],
            "accepted": len(result["accepted"]),
            "rejected": len(result["rejected"]),
            "report": str(args.out.resolve() / "manifest.json"),
        },
        title="Curation result",
    )
    return 0 if result["status"] == "target_reached" else 2
