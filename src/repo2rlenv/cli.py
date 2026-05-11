"""Repo2RLEnv CLI — argparse-based, light dependencies.

Subcommands:
  generate   Run a synthesis pipeline against a repo
  validate   Validate a generated dataset directory
  reward     Score a predicted diff against a task's oracle (smoke test)
  init       Write a sample config file
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from repo2rlenv import __version__

logger = logging.getLogger("repo2rlenv")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _load_dotenv_if_present() -> None:
    """Load .env from cwd or project root so OPENAI_API_KEY etc. are available."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _parse_pipeline_opts(items: list[str] | None) -> dict[str, Any]:
    """Parse repeated --pipeline-opt key=value into a dict (with type coercion)."""
    out: dict[str, Any] = {}
    if not items:
        return out
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--pipeline-opt expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        # Try to coerce ints, floats, bools, JSON
        if v.lower() in {"true", "false"}:
            out[k] = v.lower() == "true"
            continue
        try:
            out[k] = json.loads(v)
            continue
        except json.JSONDecodeError:
            pass
        out[k] = v
    return out


def cmd_generate(args: argparse.Namespace) -> int:
    from repo2rlenv.config import load_generation_input
    from repo2rlenv.pipelines import PIPELINES
    from repo2rlenv.spec.options import parse_options

    overrides: dict[str, Any] = {}
    if args.repo:
        overrides["repo"] = {
            "url": args.repo,
            "ref": args.ref,
            "access": args.access,
        }
    if args.pipeline:
        overrides["pipeline"] = {
            "name": args.pipeline,
            "options": _parse_pipeline_opts(args.pipeline_opt),
        }
    if args.llm:
        if "/" not in args.llm:
            raise SystemExit(f"--llm expects provider/model, got {args.llm!r}")
        provider, model = args.llm.split("/", 1)
        overrides["llm"] = {"provider": provider, "model": model}
    if args.out:
        overrides["output"] = {
            "destination": args.out,
            "org": args.org or "default",
            "dataset_name": args.dataset_name or Path(args.out).name,
            "visibility": args.visibility,
        }

    config_path = Path(args.config) if args.config else None
    gen_input = load_generation_input(config_path, overrides)

    pipeline_cls = PIPELINES.get(gen_input.pipeline.name.value)
    if pipeline_cls is None:
        raise SystemExit(
            f"pipeline {gen_input.pipeline.name.value!r} not implemented in v{__version__}; "
            f"available: {sorted(PIPELINES)}"
        )

    options = parse_options(gen_input.pipeline.name.value, gen_input.pipeline.options)
    pipeline = pipeline_cls(gen_input, options)

    dest = gen_input.output.destination
    push_to_hub_after = dest.startswith("hf://")
    if push_to_hub_after:
        repo_id = dest.removeprefix("hf://")
        out_dir = Path(f"./.r2e_cache/{repo_id.replace('/', '__')}").resolve()
    else:
        out_dir = Path(dest).expanduser().resolve()

    print(f"==> generating with pipeline={gen_input.pipeline.name.value}")
    print(f"    repo: {gen_input.repo.url} (access={gen_input.repo.access})")
    print(f"    out:  {out_dir}")

    result = pipeline.run(out_dir)
    print(f"\n==> done")
    print(f"    candidates: {result.candidates}")
    print(f"    emitted:    {result.emitted}")
    print(f"    skipped:    {result.skipped}  (reasons: {result.skip_reasons})")
    print(f"    out_dir:    {result.out_dir}")

    if push_to_hub_after and result.emitted > 0:
        from repo2rlenv.hub import push_to_hub

        print(f"\n==> pushing to {dest}")
        push_result = push_to_hub(
            local_dataset_dir=out_dir,
            repo_id=repo_id,
            auth=gen_input.auth,
            private=(gen_input.output.visibility == "private"),
            pipeline=gen_input.pipeline.name.value,
            repo_source=f"{gen_input.repo.owner_name[0]}/{gen_input.repo.owner_name[1]}",
        )
        print(f"    repo_id:      {push_result.repo_id}")
        print(f"    commit:       {push_result.commit_sha}")
        print(f"    task_count:   {push_result.task_count}")
        print(f"    registry_url: {push_result.registry_url}")

    return 0 if result.emitted > 0 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    import tomllib

    dataset_dir = Path(args.path).expanduser().resolve()
    tasks_dir = dataset_dir if (dataset_dir / "task.toml").exists() else dataset_dir
    # Walk for task.toml files
    task_files = sorted(tasks_dir.rglob("task.toml"))
    if not task_files:
        print(f"no task.toml files found under {dataset_dir}")
        return 1

    failures = 0
    for tf in task_files:
        try:
            data = tomllib.loads(tf.read_text())
        except Exception as exc:
            print(f"[FAIL] {tf}: cannot parse TOML: {exc}")
            failures += 1
            continue

        # Minimal Harbor checks
        for key in ("version", "task"):
            if key not in data:
                print(f"[FAIL] {tf}: missing top-level [{key}]")
                failures += 1
                break
        else:
            t = data["task"]
            if "name" not in t:
                print(f"[FAIL] {tf}: [task] missing name")
                failures += 1
                continue
            r2e = data.get("metadata", {}).get("repo2env")
            if r2e is None:
                print(f"[WARN] {tf}: missing [metadata.repo2env] — non-r2e task")
            print(f"[OK]   {t['name']}")

    print(f"\n{len(task_files) - failures}/{len(task_files)} tasks valid")
    return 0 if failures == 0 else 1


def cmd_reward(args: argparse.Namespace) -> int:
    from repo2rlenv.reward import calculate_diff_similarity_reward

    task_dir = Path(args.task).expanduser().resolve()
    oracle_path = task_dir / "solution" / "patch.diff"
    pred_path = Path(args.prediction).expanduser().resolve()
    if not oracle_path.exists():
        print(f"oracle not found: {oracle_path}")
        return 2
    if not pred_path.exists():
        print(f"prediction not found: {pred_path}")
        return 2

    reward, meta = calculate_diff_similarity_reward(
        oracle_path.read_text(), pred_path.read_text()
    )
    print(f"reward = {reward:.4f}")
    print(f"metadata = {asdict(meta)}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Build a working Docker image for (repo, ref) via an LLM agent loop."""
    from repo2rlenv.bootstrap import ensure_bootstrap, LanguageHint
    from repo2rlenv.bootstrap.language import base_image_for, detect_language
    from repo2rlenv.bootstrap.runner import BootstrapError
    from repo2rlenv.bootstrap.ui import bootstrap_ui
    from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec

    if not args.llm or "/" not in args.llm:
        raise SystemExit("--llm is required as provider/model (e.g. anthropic/claude-sonnet-4-6)")
    provider, model = args.llm.split("/", 1)
    llm = LLMSpec(provider=provider, model=model)

    repo = RepoSpec(
        url=args.repo,
        ref=args.ref,
        access=args.access,
    )
    spec = BootstrapSpec(
        max_iterations=args.max_iterations,
        max_seconds=args.max_seconds,
        cache_dir=Path(args.cache_dir),
        image_registry=args.image_registry,
        platform=args.platform,
    )
    if args.language:
        try:
            spec.languages_hint = [LanguageHint(args.language).value]
        except ValueError:
            raise SystemExit(f"unknown language: {args.language!r}")

    # Best-guess language up-front so the UI header is populated. Real
    # detection happens inside ensure_bootstrap after the clone.
    guessed_lang = spec.languages_hint[0] if spec.languages_hint else "unknown"
    guessed_base = spec.base_image or base_image_for(LanguageHint(guessed_lang) if guessed_lang in LanguageHint.__members__.values() else LanguageHint.UNKNOWN)

    with bootstrap_ui(
        repo=args.repo,
        ref=args.ref,
        model=args.llm,
        max_iterations=args.max_iterations,
        language=guessed_lang,
        base_image=guessed_base,
        force_plain=args.no_ui,
    ) as ui:
        on_turn = ui.on_turn if ui is not None else None
        try:
            result = ensure_bootstrap(
                repo, spec, llm, AuthSpec(), force=args.force, on_turn=on_turn,
            )
        except BootstrapError as exc:
            if ui is not None:
                ui.set_outcome(success=False, reason=str(exc))
            else:
                print(f"bootstrap error: {exc}")
            return 1
        if ui is not None:
            ui.set_outcome(
                success=True,
                image_digest=result.image_digest,
                image_tag=result.image_tag,
                rebuild_cmds=result.rebuild_cmds,
                test_cmds=result.test_cmds,
            )

    # In non-Rich mode, fall back to plain-text summary
    if args.no_ui or not sys.stdout.isatty():
        print(f"==> bootstrap ok")
        print(f"    repo:          {result.repo}@{result.ref[:12]}")
        print(f"    language:      {result.language.value}")
        print(f"    image_digest:  {result.image_digest}")
        print(f"    image_tag:     {result.image_tag}")
        print(f"    iterations:    {result.iterations}")
        print(f"    build_time:    {result.build_time_sec:.1f}s")
        print(f"    smoke_passed:  {result.smoke_passed}")
        print(f"    pushed:        {result.pushed_to_registry}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.out or "repo2rlenv.config.yaml").expanduser().resolve()
    if out.exists() and not args.force:
        print(f"refusing to overwrite {out} (use --force)")
        return 1
    out.write_text(
        """spec_version: "0.1.0"

repo:
  url: "huggingface/trl"
  access: "auto"

pipeline:
  name: "pr_mining_lite"
  options:
    limit: 10
    skip_drafts: true
    max_files_per_pr: 5

llm:
  provider: "anthropic"
  model: "claude-sonnet-4-6"

output:
  destination: "./datasets/trl-r2e"
  org: "myorg"
  dataset_name: "trl-r2e"
  visibility: "public"

qa:
  enabled: true
  layers: ["diff_parse"]

sandbox:
  provider: "none"
""",
        encoding="utf-8",
    )
    print(f"wrote sample config to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(
        prog="repo2rlenv",
        description="Turn any repository into an RL environment for training and evaluation.",
    )
    parser.add_argument(
        "--version", action="version", version=f"repo2rlenv {__version__}"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # generate
    g = sub.add_parser("generate", help="Run a synthesis pipeline against a repo")
    g.add_argument("--config", help="path to YAML/TOML config file")
    g.add_argument("--repo", help="repo URL or owner/name (overrides config)")
    g.add_argument("--ref", default="HEAD", help="branch/tag/commit (default: HEAD)")
    g.add_argument("--access", choices=["public", "private", "auto"], default="auto")
    g.add_argument("--pipeline", help="pipeline name")
    g.add_argument(
        "--pipeline-opt", action="append", default=[],
        help="pipeline-specific kwarg, repeatable (key=value)",
    )
    g.add_argument("--llm", help="LLM as provider/model (e.g. anthropic/claude-sonnet-4-6)")
    g.add_argument("--out", help="output directory")
    g.add_argument("--org", help="task.org for Harbor")
    g.add_argument("--dataset-name", help="dataset name")
    g.add_argument("--visibility", choices=["public", "private"], default="public")
    g.set_defaults(func=cmd_generate)

    # validate
    v = sub.add_parser("validate", help="Validate task.toml files in a dataset")
    v.add_argument("path", help="dataset or task directory")
    v.set_defaults(func=cmd_validate)

    # reward
    r = sub.add_parser(
        "reward", help="Score a predicted diff against a task's oracle"
    )
    r.add_argument("--task", required=True, help="task directory containing solution/patch.diff")
    r.add_argument("--prediction", required=True, help="path to predicted diff file")
    r.set_defaults(func=cmd_reward)

    # bootstrap
    bs = sub.add_parser("bootstrap", help="Build a working Docker image for a repo (v0.2)")
    bs.add_argument("--repo", required=True, help="GitHub repo (owner/name or URL)")
    bs.add_argument("--ref", default="HEAD", help="branch/tag/commit (default: HEAD)")
    bs.add_argument("--access", choices=["public", "private", "auto"], default="auto")
    bs.add_argument("--llm", required=True, help="provider/model, e.g. anthropic/claude-sonnet-4-6")
    bs.add_argument("--max-iterations", type=int, default=25)
    bs.add_argument("--max-seconds", type=int, default=1800)
    bs.add_argument("--cache-dir", default="./envs")
    bs.add_argument("--image-registry", help="e.g. ghcr.io/myorg/r2e — pushes after build")
    bs.add_argument("--platform", default="linux/amd64", choices=["linux/amd64", "linux/arm64"])
    bs.add_argument("--language", help="override auto-detection: python|node|go|rust|java|c_cpp")
    bs.add_argument("--force", action="store_true", help="ignore cache, rebuild from scratch")
    bs.add_argument("--no-ui", action="store_true", help="disable Rich live UI (plain logs)")
    bs.set_defaults(func=cmd_bootstrap)

    # init
    i = sub.add_parser("init", help="Write a sample config file")
    i.add_argument("--out", help="output path (default: repo2rlenv.config.yaml)")
    i.add_argument("--force", action="store_true", help="overwrite if exists")
    i.set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
