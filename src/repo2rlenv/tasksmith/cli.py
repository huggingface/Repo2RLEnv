from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import PRIdentity
from repo2rlenv.ui import console


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "tasksmith", help="Construct resumable PR environments remotely (experimental; [curation])"
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Frozen TasksmithConfig JSON with existing ledger limits",
    )
    parser.add_argument(
        "--panel", type=Path, help="JSON URL list or research object containing panel rows"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Persistent run directory; reuse it for recovery"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="Validate local config/panel only; no remote calls or output writes",
    )
    mode.add_argument(
        "--check-providers",
        action="store_true",
        help="Run metered remote CPU fixtures on both Modal and Daytona; no PR generation",
    )
    parser.set_defaults(func=cmd_tasksmith)


def _panel_urls(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    rows = data.get("panel") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("Panel must be a nonempty URL list or an object containing a panel list")
    identities = []
    for row in rows:
        url = row.get("url") if isinstance(row, dict) else row
        if not isinstance(url, str):
            raise ValueError("Every panel row needs a GitHub PR URL")
        identities.append(PRIdentity.from_url(url))
    if len({pr.key for pr in identities}) != len(identities):
        raise ValueError("Panel contains duplicate canonical PR identities")
    return [pr.url for pr in identities]


def cmd_tasksmith(args: argparse.Namespace) -> int:
    config = TasksmithConfig.model_validate_json(args.config.read_text())
    if not args.check_providers:
        if args.panel is None:
            raise ValueError("--panel is required for Tasksmith planning or generation")
        urls = _panel_urls(args.panel)
        if args.plan:
            console.kv(
                {
                    "mode": "static plan; source/profile/task validity not evaluated",
                    "panel_prs": len(urls),
                    "provider": config.provider,
                    "author_runtime": config.author_runtime,
                    "solver_models": ", ".join(config.solver_models),
                    "ledger": str(config.ledger_path),
                    "ledger_limit_usd": config.ledger_limit_usd,
                    "campaign_limit_usd": config.campaign_limit_usd,
                    "candidate_limit_usd": config.candidate_limit_usd,
                    "remote_calls": 0,
                    "out": str(args.out),
                },
                title="Tasksmith plan",
            )
            return 0
    if args.check_providers:
        from repo2rlenv.tasksmith.conformance import check_providers

        result = asyncio.run(check_providers(config, args.out))
        console.kv(
            {
                "profile_checks_passed": result.get("passed", False),
                "scope": result.get("scope", "Remote CPU profile fixtures; no task admission"),
                "out": str(args.out),
            },
            title="Tasksmith provider checks",
        )
        return 0 if result.get("passed") is True else 1

    from repo2rlenv.tasksmith.pipeline import run_panel

    result = asyncio.run(run_panel(config, args.panel, args.out))
    summary = {"out": str(args.out)}
    for name in ("status", "accepted", "selected_prs", "charged_or_reserved_usd"):
        if name in result:
            summary[name] = result[name]
    console.kv(summary, title="Tasksmith run")
    return 1 if result.get("status") in {"failed", "incomplete", "interrupted"} else 0
