"""Prepare and publish explicit, immutable selections of Harbor tasks."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.campaigns.release import ReleasePlan, publish_release, stage_release, verify_release
from repo2rlenv.ui import console


def cmd_release(args):
    if args.release_action == "stage":
        plan = ReleasePlan.model_validate_json(args.plan.read_text())
        result = stage_release(plan, args.out)
        data = {
            "repo_id": result["repo_id"],
            "tasks": result["task_count"],
            "directory": str(args.out),
        }
    elif args.release_action == "verify":
        result = verify_release(args.directory)
        data = {"repo_id": result["repo_id"], "files": len(result["files"]), "verified": True}
    else:
        from huggingface_hub import HfApi

        from repo2rlenv.auth import resolve_hf_token
        from repo2rlenv.spec.input import AuthSpec

        token = resolve_hf_token(AuthSpec())
        if not token:
            raise ValueError("Publishing requires HF_TOKEN or a configured Hub login")
        data = publish_release(
            args.directory,
            api=HfApi(token=token),
            receipt=args.receipt,
            collection_slug=args.collection,
        )
    if args.json:
        console.json(data)
    else:
        console.kv(data, title="Harbor dataset release")
    return 0


def add_release_parser(subparsers):
    parser = subparsers.add_parser(
        "release", help="Stage, verify and publish immutable Harbor datasets"
    )
    actions = parser.add_subparsers(dest="release_action", required=True)
    stage = actions.add_parser(
        "stage", help="Check an explicit task selection and build a release archive"
    )
    stage.add_argument("plan", type=Path)
    stage.add_argument("--out", type=Path, required=True)
    verify = actions.add_parser(
        "verify", help="Check staged bytes and modes without executing tasks"
    )
    verify.add_argument("directory", type=Path)
    publish = actions.add_parser(
        "publish", help="Upload a verified release to the Hub and optional collection"
    )
    publish.add_argument("directory", type=Path)
    publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--collection")
    for command in (stage, verify, publish):
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=cmd_release)
