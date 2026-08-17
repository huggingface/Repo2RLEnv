"""Gate 0's shipped files — the provenance-probe artifacts for `env_setup`.

With egress open — and it must be open, `pip`/`apt`/`cargo` are the task — an
agent can run `pip install click`, install the *released* package into
site-packages, and watch the repo's own tests pass green against it. Reward
1.0, and the source tree in `/workspace` was never made installable. Gate 0
(RFC 0008 §7b) is the only thing standing in front of that shortcut, and the
four files this module exposes are gate 0.

This module ships **no logic of its own** — it is a thin `read_text()`
accessor layer over four real, lintable, directly-testable files living
alongside it in this package (the `pr_runtime` / `_pr_runtime_verifier.py`
precedent: `_verifier_source()` just reads the verifier and
`_runtime_aux_files()` writes it out). Task 7 (the `tests/` bundle
assembler) is the only caller; it decides the emitted filenames, writes
`provenance.json` itself, and owns `test.sh` / gate 0's shell body.

  * `_env_setup_provenance_read.py`  -> emitted as `tests/provenance_read.py`
  * `_env_setup_provenance.py`       -> emitted as `tests/provenance.py`
  * `_env_setup_provenance.js`       -> emitted as `tests/provenance.js`
  * `_env_setup_provenance_run.sh`   -> emitted as `tests/provenance_run.sh`

The leading-underscore names keep these out of reach of a stray `import
provenance` inside the package itself; they are not importable modules, they
are shipped programs that happen to be stored as real `.py`/`.js`/`.sh`
files so ruff can lint them and pytest can run them as subprocesses.

Wheel inclusion (verified, not assumed): the build backend is `uv_build`.
`uv build --wheel` was run against a throwaway `.js`/`.sh` pair dropped into
this same package directory, and `python -m zipfile -l` on the resulting
wheel showed both landing at their in-package path unmodified — `uv_build`
does not restrict a package directory's wheel contents to `*.py`. So all
four probe artifacts ship as real files; no Python string-constant fallback
was needed. See `task-6-report.md` for the exact listing.

`provenance.json`'s key set, read by these four files (nothing else reads
this file — one read of it in gate 0, delegated to `provenance_read.py`,
never re-read by `provenance_run.sh`): `probe`, `base_commit`, `language`,
`package`, `dist_name` (`dist_name` may be absent; the other four are
required, non-blank, single-line strings).

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The provenance-probe design (PEP 610 `direct_url.json` as the non-forgeable
signal, per-language probe posture) this module ships is informed by:

  Repo2Run (ByteDance, arXiv:2502.13681)
  https://github.com/bytedance/Repo2Run    (Apache-2.0)

  SetupBench (Microsoft, arXiv:2507.09063)
  https://github.com/microsoft/SetupBench    (MIT)

  EnvBench (JetBrains Research, ICLR '25 DL4Code, arXiv:2503.14443)
  https://github.com/JetBrains-Research/EnvBench    (MIT)

  PEP 610 — Recording the Direct URL Origin of Installed Distributions
  https://peps.python.org/pep-0610/

This module is an INDEPENDENT IMPLEMENTATION — no code is copied from any of
the three prior-art repos. It reuses only the general shape (verify the
installed artifact actually derives from the workspace source tree, not a
released substitute) and reimplements it from scratch against Python stdlib
(`importlib.metadata`, `json`) plus a pure-Node probe for the JS case. None
of the upstream licenses apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.pipelines._env_setup_lang import TEST_ROOT_PATHSPECS
from repo2rlenv.pipelines._eval_script import _path_prelude_for_language, env_prelude_from_test_cmds
from repo2rlenv.pipelines.pr_runtime import _verifier_source

_PACKAGE_DIR = Path(__file__).parent


def provenance_read_py_source() -> str:
    """Source of `tests/provenance_read.py` — the one read of `provenance.json`.

    Emits `probe`, `base_commit`, `language` (one line each) or exits non-zero
    on anything unreadable, so gate 0's `||` fires closed.
    """
    return (_PACKAGE_DIR / "_env_setup_provenance_read.py").read_text(encoding="utf-8")


def provenance_py_source() -> str:
    """Source of `tests/provenance.py` — the Python provenance probe.

    Reads the rung from `argv[2]`: `direct_url` = PEP 610 dist metadata OR
    import location under `/workspace`; `path` = import location only.
    """
    return (_PACKAGE_DIR / "_env_setup_provenance.py").read_text(encoding="utf-8")


def provenance_js_source() -> str:
    """Source of `tests/provenance.js` — the Node provenance probe.

    `require.resolve` -> `realpathSync`; passes iff under `/workspace` and
    not under any `node_modules` path segment (the `node_modules` exclusion
    is the whole probe — `npm i <released-pkg>` lands under
    `/workspace/node_modules`).
    """
    return (_PACKAGE_DIR / "_env_setup_provenance.js").read_text(encoding="utf-8")


def provenance_run_sh_source() -> str:
    """Source of `tests/provenance_run.sh` — the per-language probe dispatcher.

    Takes both the language and the probe rung as arguments from its caller
    (gate 0), and contains no read of `provenance.json` of its own — one read
    per file, not two reads of one.
    """
    return (_PACKAGE_DIR / "_env_setup_provenance_run.sh").read_text(encoding="utf-8")


def provenance_probe_files() -> dict[str, str]:
    """All four gate-0 probe artifacts, keyed by their emitted `tests/`-relative
    filename. A convenience aggregate over the four accessors above for
    whichever bundle assembler (Task 7) wants to merge them into a larger
    `tests/` file-write dict in one call.
    """
    return {
        "provenance_read.py": provenance_read_py_source(),
        "provenance.py": provenance_py_source(),
        "provenance.js": provenance_js_source(),
        "provenance_run.sh": provenance_run_sh_source(),
    }


def build_provenance_json(
    *,
    probe: str,
    base_commit: str,
    language: str,
    package: str | None,
    dist_name: str | None,
) -> str:
    """The single file gate 0 reads (via `provenance_read.py`).

    `package` / `dist_name` fall back to "" rather than null: the Node probe
    hands `cfg.package` straight to `require.resolve`, where a null is a
    TypeError that reads as a failed probe on a correct solve.
    """
    return json.dumps(
        {
            "probe": probe,
            "base_commit": base_commit,
            "language": language,
            "package": package or "",
            "dist_name": dist_name or "",
        },
        indent=2,
    )


def build_test_roots_json() -> str:
    """The pathspec list gate ½ passes to `git clean -fdq --`.

    Emitted unfiltered: `git clean` accepts pathspecs that match nothing and
    exits 0, and tree-filtering the list is what let an agent-added
    `conftest.py` survive in a repo that had none at base_commit.
    """
    return json.dumps(list(TEST_ROOT_PATHSPECS), indent=2)


def build_env_setup_test_sh(*, language: str | None, test_cmds: list[str], runner: str) -> str:
    """The emitted `tests/test.sh`: gate 0 -> gate 1/2 -> gate 1.

    Mirrors `pr_runtime.build_eval_script`'s head, plus a `write_reward`
    helper (seven call sites want one) and a sourced environment prelude.
    Keeps pr_runtime's PATH prelude: env_setup needs it MORE, because here the
    toolchain is installed by the agent under evaluation rather than baked
    into the image, and Harbor runs `bash test.sh` non-interactively — without
    it `go test` / `cargo test` / `node` exit 127 and the reward is a false 0.
    """
    path_prelude = _path_prelude_for_language(language)
    cmds_str = " && ".join(test_cmds)
    quoted_cmds = cmds_str.replace("'", "'\\''")
    quoted_runner = runner.replace("'", "'\\''")

    head = (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        f"{path_prelude}"  # may be empty
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        "mkdir -p /logs/verifier\n"
        "\n"
        "write_reward() {\n"
        "  printf '%.6f\\n' \"$1\" > /logs/verifier/reward.txt\n"
        '  printf \'{"reward":%s,"resolved":false,"parse_status":"%s"}\' '
        '"$1" "${2:-verifier_crashed}" \\\n'
        "    > /logs/verifier/reward-details.json\n"
        "}\n"
        "\n"
        "# Both gates run under the same environment the test commands run\n"
        "# under. Sourced, never interpolated: real bootstrap test_cmds carry\n"
        "# quoted values, so a single ' would terminate an interpolated string.\n"
        "# `set +u` because older venv activate scripts touch unbound PS1.\n"
        'set +u; . "$SCRIPT_DIR/env_prelude.sh"; set -u\n'
    )

    gate0 = (
        "\n"
        "# --- gate 0: provenance -------------------------------------------\n"
        "# One read of provenance.json, and it fails closed. An earlier draft\n"
        "# read it with no `||`, so a missing python3 produced an empty\n"
        "# substitution, fell to `*)`, and the gate silently PASSED.\n"
        'PROV="$(python3 "$SCRIPT_DIR/provenance_read.py" "$SCRIPT_DIR/provenance.json" '
        '2>/dev/null)" || {\n'
        "  write_reward 0.0 provenance_unreadable; exit 0; }\n"
        '{ read -r R2E_PROBE; read -r R2E_BASE_COMMIT; read -r R2E_LANG; } <<< "$PROV"\n'
        '[ -n "$R2E_PROBE" ] && [ -n "$R2E_BASE_COMMIT" ] && [ -n "$R2E_LANG" ] \\\n'
        "  || { write_reward 0.0 provenance_unreadable; exit 0; }\n"
        "\n"
        'case "$R2E_PROBE" in\n'
        "  none) : ;;\n"
        '  *)    bash "$SCRIPT_DIR/provenance_run.sh" "$R2E_LANG" "$R2E_PROBE" \\\n'
        "          || { write_reward 0.0 package_not_from_source; exit 0; } ;;\n"
        "esac\n"
    )

    gate_half = (
        "\n"
        "# --- gate 1/2: restore the graded tests ----------------------------\n"
        "# `-- .` always matches. A computed pathspec list fails the WHOLE\n"
        "# operation on one non-matching entry, and a bare `--` exits 0 while\n"
        "# detaching HEAD onto that commit.\n"
        'git -C /workspace checkout "$R2E_BASE_COMMIT" -- . '
        "|| { write_reward 0.0 test_restore_failed; exit 0; }\n"
        "\n"
        "# A restore of tracked files cannot delete a file the agent ADDED.\n"
        "mapfile -t R2E_ROOTS < <(python3 -c "
        "'import json,sys;[print(p) for p in json.load(open(sys.argv[1]))]' \\\n"
        '                           "$SCRIPT_DIR/test_roots.json")\n'
        'if [ "${#R2E_ROOTS[@]}" -gt 0 ]; then\n'
        '  git -C /workspace clean -fdq -- "${R2E_ROOTS[@]}" '
        "|| { write_reward 0.0 test_restore_failed; exit 0; }\n"
        "fi\n"
    )

    gate1 = (
        "\n"
        "# --- gate 1: the graded reward -------------------------------------\n"
        "set +x                                          # keep xtrace out of the parsed log\n"
        f"( {cmds_str} ) > /logs/verifier/test_output.log 2>&1\n"
        "TEST_EXIT_CODE=$?\n"
        "set -x\n"
        "cat /logs/verifier/test_output.log\n"
        "\n"
        'python3 "$SCRIPT_DIR/verifier.py" \\\n'
        "    --log /logs/verifier/test_output.log \\\n"
        '    --f2p "$SCRIPT_DIR/f2p.json" --p2p "$SCRIPT_DIR/p2p.json" \\\n'
        f"    --runner '{quoted_runner}' \\\n"
        f"    --test-cmds '{quoted_cmds}' \\\n"
        '    --exit-code "$TEST_EXIT_CODE" \\\n'
        "    --out-dir /logs/verifier \\\n"
        '  || { [ "$TEST_EXIT_CODE" -eq 0 ] && write_reward 1.0 verifier_crashed \\\n'
        "                                   || write_reward 0.0 verifier_crashed; }\n"
        "exit 0\n"
    )

    return head + gate0 + gate_half + gate1


def build_env_setup_aux_files(
    *,
    language: str,
    test_cmds: list[str],
    runner: str,
    probe: str,
    base_commit: str,
    package: str | None,
    dist_name: str | None,
    f2p: list[str],
    p2p: list[str],
) -> dict[str, str]:
    """Everything under the emitted task's `tests/`, keyed for `aux_files`.

    Harbor mounts tests/ at /tests, which is what `SCRIPT_DIR` resolves to.
    Only the probe for `language` ships: `provenance_run.sh`'s `*)` branch
    exits 0 for the languages whose probe kind is `none`.
    """
    files: dict[str, str] = {
        "tests/test.sh": build_env_setup_test_sh(
            language=language, test_cmds=test_cmds, runner=runner
        ),
        "tests/verifier.py": _verifier_source(),
        "tests/env_prelude.sh": env_prelude_from_test_cmds(test_cmds) + "\n",
        "tests/f2p.json": json.dumps(f2p, indent=2),
        "tests/p2p.json": json.dumps(p2p or [], indent=2),
        "tests/provenance.json": build_provenance_json(
            probe=probe,
            base_commit=base_commit,
            language=language,
            package=package,
            dist_name=dist_name,
        ),
        "tests/test_roots.json": build_test_roots_json(),
    }
    probes = provenance_probe_files()
    files["tests/provenance_read.py"] = probes["provenance_read.py"]
    files["tests/provenance_run.sh"] = probes["provenance_run.sh"]
    if language == LanguageHint.PYTHON:
        files["tests/provenance.py"] = probes["provenance.py"]
    elif language == LanguageHint.NODE:
        files["tests/provenance.js"] = probes["provenance.js"]
    return files
