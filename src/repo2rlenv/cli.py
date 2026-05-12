"""Repo2RLEnv CLI — argparse-based, Rich-driven UI.

Subcommands:
  generate    Run a synthesis pipeline against a repo
  validate    Validate a generated dataset directory
  reward      Score a predicted diff against a task's oracle (smoke test)
  bootstrap   Build a working Docker image via an LLM agent loop
  init        Write a sample config file
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
from repo2rlenv.ui import console, install_logging

logger = logging.getLogger("repo2rlenv")


def _load_dotenv_if_present() -> None:
    """Load .env so OPENAI_API_KEY / ANTHROPIC_API_KEY / HF_TOKEN are available."""
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
    from repo2rlenv.ui.views.generation import generation_view_or_plain

    overrides: dict[str, Any] = {}
    if args.repo:
        overrides["repo"] = {"url": args.repo, "ref": args.ref, "access": args.access}
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
        if getattr(args, "llm_fallback", None):
            if "/" not in args.llm_fallback:
                raise SystemExit(
                    f"--llm-fallback expects provider/model, got {args.llm_fallback!r}"
                )
            fb_provider, fb_model = args.llm_fallback.split("/", 1)
            overrides["llm"]["fallback"] = {"provider": fb_provider, "model": fb_model}
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
        console.error(
            f"pipeline {gen_input.pipeline.name.value!r} not implemented in v{__version__}; "
            f"available: {sorted(PIPELINES)}"
        )
        return 2

    options = parse_options(gen_input.pipeline.name.value, gen_input.pipeline.options)

    # Pre-flight: does this pipeline support this repo's primary language?
    # Cheap GitHub API call; runs BEFORE bootstrap so we fail fast on a
    # Go/Rust/Node repo + Python-only pipeline mismatch (~2s vs ~5 min).
    if getattr(pipeline_cls, "supported_languages", None) is not None:
        from repo2rlenv.auth import resolve_github_token
        from repo2rlenv.bootstrap.language import language_from_github_name
        from repo2rlenv.github import get_primary_language
        from repo2rlenv.pipelines.base import (
            LanguageMismatchError,
            check_language_compatibility,
        )

        owner, name = gen_input.repo.owner_name
        gh_token = resolve_github_token(gen_input.repo, gen_input.auth)
        gh_lang_name = get_primary_language(owner, name, token=gh_token)
        detected = language_from_github_name(gh_lang_name)
        try:
            check_language_compatibility(pipeline_cls, detected, force=args.force_language)
        except LanguageMismatchError as exc:
            console.error(str(exc))
            return 2

    # Auto-trigger bootstrap for sandbox-required pipelines (requires_bootstrap=True).
    # Cache hit ⇒ instant; cache miss ⇒ full LLM-agent run with the live UI.
    bootstrap_result = None
    if getattr(pipeline_cls, "requires_bootstrap", False):
        from repo2rlenv.bootstrap import LanguageHint, ensure_bootstrap
        from repo2rlenv.bootstrap.runner import BootstrapError
        from repo2rlenv.ui.views.bootstrap import bootstrap_view_or_plain

        # Mutate spec with CLI overrides (language / base-image / budget / force / --bootstrap-opt)
        bspec = gen_input.bootstrap.model_copy(deep=True)
        if args.language:
            try:
                bspec.languages_hint = [LanguageHint(args.language).value]
            except ValueError as exc:
                raise SystemExit(f"unknown --language: {args.language!r}") from exc
        if args.base_image:
            bspec.base_image = args.base_image
        # --max-spend-usd=0 ⇒ no cap; map to None
        if args.max_spend_usd is not None:
            bspec.max_llm_spend_usd = args.max_spend_usd if args.max_spend_usd > 0 else None
        # Generic --bootstrap-opt key=value for any other BootstrapSpec field
        # (cache_dir / max_iterations / max_seconds / image_registry / platform / ...)
        for k, v in _parse_pipeline_opts(getattr(args, "bootstrap_opt", None)).items():
            if not hasattr(bspec, k):
                raise SystemExit(f"--bootstrap-opt: unknown BootstrapSpec field {k!r}")
            # Pydantic will coerce types as needed (str→Path, str→int, etc.)
            try:
                bspec = bspec.model_copy(update={k: v})
            except Exception as exc:
                raise SystemExit(f"--bootstrap-opt {k}={v!r}: {exc}") from exc
        with bootstrap_view_or_plain(
            repo=gen_input.repo.url,
            ref=gen_input.repo.ref,
            model=gen_input.llm.qualified_name,
            max_iterations=bspec.max_iterations,
            language=(bspec.languages_hint[0] if bspec.languages_hint else "unknown"),
            base_image=bspec.base_image or "(auto-detect)",
            force_plain=args.no_ui,
        ) as bs_view:
            try:
                bootstrap_result = ensure_bootstrap(
                    gen_input.repo,
                    bspec,
                    gen_input.llm,
                    gen_input.auth,
                    force=args.force_bootstrap,
                    on_turn=bs_view.on_turn if bs_view else None,
                    on_thinking=bs_view.on_thinking if bs_view else None,
                    on_executing=bs_view.on_executing if bs_view else None,
                    on_phase=bs_view.on_phase if bs_view else None,
                )
            except BootstrapError as exc:
                if bs_view is not None:
                    bs_view.set_outcome(success=False, reason=str(exc))
                else:
                    console.error(f"bootstrap error: {exc}")
                return 1
            if bs_view is not None:
                bs_view.set_outcome(
                    success=True,
                    image_digest=bootstrap_result.image_digest,
                    image_tag=bootstrap_result.image_tag,
                    rebuild_cmds=bootstrap_result.rebuild_cmds,
                    test_cmds=bootstrap_result.test_cmds,
                )

    pipeline = pipeline_cls(gen_input, options, bootstrap=bootstrap_result)

    dest = gen_input.output.destination
    push_to_hub_after = dest.startswith("hf://")
    if push_to_hub_after:
        repo_id = dest.removeprefix("hf://")
        out_dir = Path(f"./.r2e_cache/{repo_id.replace('/', '__')}").resolve()
    else:
        out_dir = Path(dest).expanduser().resolve()

    # Pipeline-specific limit hint for the progress bar
    limit_hint = int(gen_input.pipeline.options.get("limit", 50)) or 50

    with generation_view_or_plain(
        repo=gen_input.repo.url,
        pipeline=gen_input.pipeline.name.value,
        model=gen_input.llm.qualified_name,
        limit=limit_hint,
        out=str(out_dir),
        force_plain=args.no_ui,
    ) as view:
        on_candidate = view.on_candidate if view is not None else None
        # Pipelines that don't yet emit on_candidate just won't update the bar
        if hasattr(pipeline, "set_progress_callback") and on_candidate is not None:
            pipeline.set_progress_callback(on_candidate)
        elif on_candidate is None:
            console.info(f"generating with pipeline={gen_input.pipeline.name.value}")
            console.dim(f"  repo: {gen_input.repo.url} (access={gen_input.repo.access})")
            console.dim(f"  out:  {out_dir}")

        result = pipeline.run(out_dir)

        if view is not None:
            view.set_outcome(
                emitted=result.emitted,
                skipped=result.skipped,
                skip_reasons=result.skip_reasons,
            )

    if view is None:
        console.kv(
            {
                "candidates": result.candidates,
                "emitted": result.emitted,
                "skipped": f"{result.skipped} ({result.skip_reasons})",
                "out_dir": str(result.out_dir),
            },
            title="Generation result",
        )

    if push_to_hub_after and result.emitted > 0:
        from repo2rlenv.hub import push_to_hub

        with console.section(f"Pushing to {dest}"):
            push_result = push_to_hub(
                local_dataset_dir=out_dir,
                repo_id=repo_id,
                auth=gen_input.auth,
                private=(gen_input.output.visibility == "private"),
                pipeline=gen_input.pipeline.name.value,
                repo_source=f"{gen_input.repo.owner_name[0]}/{gen_input.repo.owner_name[1]}",
            )
            console.kv(
                {
                    "repo_id": push_result.repo_id,
                    "commit": push_result.commit_sha,
                    "task_count": push_result.task_count,
                    "registry_url": push_result.registry_url,
                },
                title="HF Hub push",
            )

    return 0 if result.emitted > 0 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    import tomllib

    dataset_dir = Path(args.path).expanduser().resolve()
    task_files = sorted(dataset_dir.rglob("task.toml"))
    if not task_files:
        console.error(f"no task.toml files found under {dataset_dir}")
        return 1

    with console.section(f"Validating {dataset_dir}"):
        failures = 0
        for tf in task_files:
            try:
                data = tomllib.loads(tf.read_text())
            except Exception as exc:
                console.error(f"{tf.relative_to(dataset_dir)}: cannot parse TOML: {exc}")
                failures += 1
                continue

            missing = [k for k in ("version", "task") if k not in data]
            if missing:
                console.error(f"{tf.relative_to(dataset_dir)}: missing top-level {missing}")
                failures += 1
                continue

            t = data["task"]
            if "name" not in t:
                console.error(f"{tf.relative_to(dataset_dir)}: [task] missing name")
                failures += 1
                continue

            r2e = data.get("metadata", {}).get("repo2env")
            if r2e is None:
                console.warn(f"{t['name']}: missing [metadata.repo2env] — non-r2e task")
            else:
                console.success(t["name"])

    if failures == 0:
        console.success(f"all {len(task_files)} tasks valid")
    else:
        console.error(f"{failures}/{len(task_files)} tasks failed")
    return 0 if failures == 0 else 1


def cmd_reward(args: argparse.Namespace) -> int:
    from repo2rlenv.reward import calculate_diff_similarity_reward

    task_dir = Path(args.task).expanduser().resolve()
    oracle_path = task_dir / "solution" / "patch.diff"
    pred_path = Path(args.prediction).expanduser().resolve()
    if not oracle_path.exists():
        console.error(f"oracle not found: {oracle_path}")
        return 2
    if not pred_path.exists():
        console.error(f"prediction not found: {pred_path}")
        return 2

    reward, meta = calculate_diff_similarity_reward(oracle_path.read_text(), pred_path.read_text())
    meta_dict = asdict(meta)
    console.kv(
        {
            "task": task_dir.name,
            "reward": f"{reward:.4f}",
            "matched_lines": f"{meta_dict['matched_lines']}/{meta_dict['oracle_lines']}",
            "pred_lines": meta_dict["pred_lines"],
            "parse_error": meta_dict["parse_error"] or "(none)",
        },
        title="diff_similarity reward",
    )
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Build a working Docker image for (repo, ref) via an LLM agent loop."""
    from repo2rlenv.bootstrap import LanguageHint, ensure_bootstrap
    from repo2rlenv.bootstrap.language import base_image_for
    from repo2rlenv.bootstrap.runner import BootstrapError
    from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec
    from repo2rlenv.ui.views.bootstrap import bootstrap_view_or_plain

    if not args.llm or "/" not in args.llm:
        raise SystemExit("--llm is required as provider/model (e.g. anthropic/claude-sonnet-4-6)")
    provider, model = args.llm.split("/", 1)
    llm = LLMSpec(provider=provider, model=model)

    repo = RepoSpec(url=args.repo, ref=args.ref, access=args.access)
    # --max-spend-usd=0 means "no cap"; map to None so the agent loop skips the check
    spend_cap = args.max_spend_usd if args.max_spend_usd and args.max_spend_usd > 0 else None
    spec = BootstrapSpec(
        max_iterations=args.max_iterations,
        max_seconds=args.max_seconds,
        cache_dir=Path(args.cache_dir),
        image_registry=args.image_registry,
        platform=args.platform,
        base_image=args.base_image,
        max_llm_spend_usd=spend_cap,
    )
    if args.language:
        try:
            spec.languages_hint = [LanguageHint(args.language).value]
        except ValueError as exc:
            raise SystemExit(f"unknown language: {args.language!r}") from exc

    guessed_lang = spec.languages_hint[0] if spec.languages_hint else "unknown"
    try:
        guessed_enum = LanguageHint(guessed_lang)
    except ValueError:
        guessed_enum = LanguageHint.UNKNOWN
    guessed_base = spec.base_image or base_image_for(guessed_enum)

    with bootstrap_view_or_plain(
        repo=args.repo,
        ref=args.ref,
        model=args.llm,
        max_iterations=args.max_iterations,
        language=guessed_lang,
        base_image=guessed_base,
        force_plain=args.no_ui,
    ) as view:
        on_turn = view.on_turn if view is not None else None
        on_thinking = view.on_thinking if view is not None else None
        on_executing = view.on_executing if view is not None else None
        on_phase = view.on_phase if view is not None else None
        try:
            result = ensure_bootstrap(
                repo,
                spec,
                llm,
                AuthSpec(),
                force=args.force,
                on_turn=on_turn,
                on_thinking=on_thinking,
                on_executing=on_executing,
                on_phase=on_phase,
            )
        except BootstrapError as exc:
            # When the user didn't pin the language or base, the failure is
            # most likely because auto-detection picked the wrong one. Surface
            # actionable hints instead of just dumping the agent's last error.
            reason = str(exc)
            if not args.language and not args.base_image:
                reason += (
                    "\n\nhint: language was auto-detected. If this repo is polyglot or has "
                    "unusual markers, retry with --language <python|node|go|rust|java|c_cpp> "
                    "or --base-image <image:tag> (e.g. --base-image ubuntu:24.04 for a "
                    "generic Linux base)."
                )
            if view is not None:
                view.set_outcome(success=False, reason=reason)
            else:
                console.error(f"bootstrap error: {reason}")
            return 1
        if view is not None:
            view.set_outcome(
                success=True,
                image_digest=result.image_digest,
                image_tag=result.image_tag,
                rebuild_cmds=result.rebuild_cmds,
                test_cmds=result.test_cmds,
            )

    if view is None:
        console.kv(
            {
                "repo": f"{result.repo}@{result.ref[:12]}",
                "language": result.language.value,
                "image_digest": result.image_digest,
                "image_tag": result.image_tag,
                "iterations": result.iterations,
                "build_time": f"{result.build_time_sec:.1f}s",
                "smoke_passed": result.smoke_passed,
                "pushed": result.pushed_to_registry,
            },
            title="Bootstrap result",
        )
    return 0


_SAMPLE_CONFIG = """spec_version: "0.1.0"

repo:
  url: "huggingface/trl"
  access: "auto"

pipeline:
  name: "pr_diff"
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
"""


def cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.out or "repo2rlenv.config.yaml").expanduser().resolve()
    if out.exists() and not args.force:
        console.error(f"refusing to overwrite {out} (use --force)")
        return 1
    out.write_text(_SAMPLE_CONFIG, encoding="utf-8")
    console.success(f"wrote sample config to {out}")
    console.dim("  edit it, then run: repo2rlenv generate --config <path>")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(
        prog="repo2rlenv",
        description="Turn any repository into an RL environment for training and evaluation.",
    )
    parser.add_argument("--version", action="version", version=f"repo2rlenv {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--no-ui", action="store_true", help="disable Rich live displays globally (plain logs)"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # generate
    g = sub.add_parser("generate", help="Run a synthesis pipeline against a repo")
    g.add_argument("--config", help="path to YAML/TOML config file")
    g.add_argument("--repo", help="repo URL or owner/name (overrides config)")
    g.add_argument("--ref", default="HEAD", help="branch/tag/commit (default: HEAD)")
    g.add_argument("--access", choices=["public", "private", "auto"], default="auto")
    g.add_argument("--pipeline", help="pipeline name")
    g.add_argument(
        "--pipeline-opt",
        action="append",
        default=[],
        help="pipeline-specific kwarg, repeatable (key=value)",
    )
    g.add_argument("--llm", help="LLM as provider/model (e.g. anthropic/claude-sonnet-4-6)")
    g.add_argument(
        "--llm-fallback",
        help=(
            "fallback LLM as provider/model — used automatically when the primary "
            "returns 5xx / rate-limit / network errors"
        ),
    )
    g.add_argument("--out", help="output directory")
    g.add_argument("--org", help="task.org for Harbor")
    g.add_argument("--dataset-name", help="dataset name")
    g.add_argument("--visibility", choices=["public", "private"], default="public")
    # Bootstrap-related (only used for pipelines with requires_bootstrap=True)
    g.add_argument(
        "--max-spend-usd",
        type=float,
        default=5.0,
        help="LLM budget cap across bootstrap + pipeline (default 5.0; 0 = unlimited)",
    )
    g.add_argument(
        "--language", help="bootstrap: override auto-detect (python|node|go|rust|java|c_cpp)"
    )
    g.add_argument(
        "--base-image", help="bootstrap: override base image (e.g. ubuntu:24.04, python:3.11-slim)"
    )
    g.add_argument(
        "--force-bootstrap",
        action="store_true",
        help="ignore bootstrap cache, rebuild from scratch",
    )
    g.add_argument(
        "--bootstrap-opt",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "override any BootstrapSpec field (e.g. cache_dir=./envs-matrix/sonnet-4-6, "
            "max_iterations=30, max_seconds=2400). Repeatable."
        ),
    )
    g.add_argument(
        "--force-language",
        action="store_true",
        help=(
            "skip the pipeline-language compatibility check "
            "(e.g. run a Python-only pipeline against a Go repo anyway)"
        ),
    )
    g.set_defaults(func=cmd_generate)

    # validate
    v = sub.add_parser("validate", help="Validate task.toml files in a dataset")
    v.add_argument("path", help="dataset or task directory")
    v.set_defaults(func=cmd_validate)

    # reward
    r = sub.add_parser("reward", help="Score a predicted diff against a task's oracle")
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
    bs.add_argument(
        "--base-image",
        help="override per-language default base (e.g. ubuntu:24.04, python:3.11-slim)",
    )
    bs.add_argument(
        "--max-spend-usd",
        type=float,
        default=5.0,
        help="abort if cumulative LLM cost exceeds this (default 5.0; 0 = unlimited)",
    )
    bs.add_argument("--force", action="store_true", help="ignore cache, rebuild from scratch")
    bs.set_defaults(func=cmd_bootstrap)

    # init
    i = sub.add_parser("init", help="Write a sample config file")
    i.add_argument("--out", help="output path (default: repo2rlenv.config.yaml)")
    i.add_argument("--force", action="store_true", help="overwrite if exists")
    i.set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    install_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
