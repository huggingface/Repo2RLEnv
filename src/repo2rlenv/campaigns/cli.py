"""Explicit campaign accounting and remote worker lifecycle commands."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.base import WorkerSpec, connect_worker
from repo2rlenv.execution.lifecycle import provision_worker, stop_worker
from repo2rlenv.execution.probes import probe_worker
from repo2rlenv.ui import console


def cmd_campaign(args: argparse.Namespace) -> int:
    ledger = BudgetLedger(
        args.path / "budget.sqlite3",
        limit_usd=args.budget_usd if args.campaign_action == "init" else None,
    )
    if args.campaign_action == "settle":
        evidence = args.evidence.resolve()
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
        ledger.settle(args.operation, args.cost_usd, evidence=f"{evidence}#sha256={digest}")
    data = ledger.status()
    if args.json:
        console.json(data)
    else:
        console.kv(
            {
                key.removesuffix("_usd"): "$" + data[key]
                for key in ("limit_usd", "accounted_usd", "reserved_usd", "remaining_usd")
            },
            title="Campaign budget",
        )
        pending = sum(item["status"] == "uncertain" for item in data["operations"])
        if pending:
            console.warn(
                f"{pending} operations need reconciliation; their reservations remain held."
            )
    return 0


def cmd_workers(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    if args.worker_action == "start":
        ledger = BudgetLedger(args.campaign / "budget.sqlite3")
        spec = WorkerSpec(
            provider=args.provider,
            name=args.name,
            cpus=args.cpus,
            memory_mb=args.memory_mb,
            timeout_sec=args.timeout_sec,
        )
        _, receipt = provision_worker(
            spec, args.campaign / "workers", ledger, reserve_usd=args.reserve_usd
        )
        data = {"receipt": str(receipt), **json.loads(receipt.read_text())}
    else:
        record = json.loads(args.receipt.read_text())
        if args.worker_action == "stop":
            data = stop_worker(args.receipt, BudgetLedger(Path(record["ledger"])))
        else:
            if record["state"] != "running":
                raise ValueError("Probe requires a running worker receipt")
            worker = connect_worker(record["spec"]["provider"], record["worker_id"])
            data = probe_worker(worker, args.out)
    if args.json:
        console.json(data)
    else:
        console.kv(
            {key: value for key, value in data.items() if not isinstance(value, (dict, list))},
            title="Remote worker",
        )
    return 0


def add_campaign_parsers(subparsers) -> None:
    campaign = subparsers.add_parser(
        "campaign", help="Initialize, inspect and reconcile a generation budget"
    )
    actions = campaign.add_subparsers(dest="campaign_action", required=True)
    for action in ("init", "status", "settle"):
        parser = actions.add_parser(action)
        parser.add_argument("path", type=Path)
        parser.add_argument("--json", action="store_true")
        if action == "init":
            parser.add_argument("--budget-usd", required=True)
        elif action == "settle":
            parser.add_argument("--operation", required=True)
            parser.add_argument("--cost-usd", required=True)
            parser.add_argument("--evidence", required=True, type=Path)
        parser.set_defaults(func=cmd_campaign)
    workers = subparsers.add_parser(
        "workers", help="Create, probe and terminate remote generation workers"
    )
    actions = workers.add_subparsers(dest="worker_action", required=True)
    start = actions.add_parser("start")
    start.add_argument("--campaign", required=True, type=Path)
    start.add_argument("--provider", required=True, choices=["modal", "daytona"])
    start.add_argument("--name", required=True)
    start.add_argument("--reserve-usd", required=True)
    start.add_argument("--cpus", type=int, default=2)
    start.add_argument("--memory-mb", type=int, default=4096)
    start.add_argument("--timeout-sec", type=int, default=3600)
    for parser in (start, actions.add_parser("probe"), actions.add_parser("stop")):
        if parser is not start:
            parser.add_argument("receipt", type=Path)
        if parser.prog.endswith(" probe"):
            parser.add_argument("--out", required=True, type=Path)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_workers)
