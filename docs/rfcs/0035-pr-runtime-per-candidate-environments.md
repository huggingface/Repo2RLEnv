---
title: "RFC 0035: pr_runtime per-candidate environments"
navTitle: "0035 · pr_runtime per-candidate environments"
---

**Status:** draft
**Author:** `rycerzes`
**Created:** 2026-11-27

## Summary

Install dependencies at each candidate's base commit, as a layer on a shared toolchain image, and validate in the
image the task ships. Tracked in #253.

## Motivation

`pr_runtime` installs dependencies once at the tip. Validation and the task Dockerfile (`pr_runtime.py:326`) only
reset git, though `BOOTSTRAP.md:310` says dependencies are reinstalled.

On hugo (v0.9.6 with local patches):
- one environment at the tip: 2 tasks from 185 PRs;
- bootstrap re-anchored per two-week window: 19 tasks;
- the same, plus `go mod download` at each base: 54 tasks.

pydantic gave 0 of 360 PRs, because `pydantic-core` built at the tip doesn't import at older bases.

Grouping candidates by dependency files saves little. hugo's 55 bases have 27 distinct fingerprints. pydantic's 113
bases have 43 fingerprints and 67 distinct `pydantic-core` trees. Toolchains rarely change: 2 Go versions and 1
Python.

## Design

Opt-in: `--pipeline-opt environment=candidate`. The default stays `head`.

1. **Bootstrap** saves `install_cmds` (`go mod download all`, `uv sync --frozen`, `cargo fetch --locked`, `npm ci`)
   next to `rebuild_cmds` and `test_cmds`. A recipe is accepted only if it replays in a fresh container and reports
   at least one test.
2. **Toolchain image:** the bootstrap image without the source. It keeps the package stores and build directories as
   warm caches. A candidate whose toolchain is newer than the image's (for Go, the `go.mod` version) gets a toolchain
   image where an installer exists. Otherwise it is dropped as `toolchain_unsupported`.
3. **Candidate image:**
   - a dependency layer, cached by fingerprint, holding the base's dependency files plus `install_cmds`;
   - a source layer: checkout, the editable install and `rebuild_cmds`.

   Unpinned Python resolution is date-capped to the base. Then:
   - a provenance check confirms the project comes from `/workspace`;
   - download caches are removed;
   - offline settings are applied (`GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `PIP_NO_INDEX`, cargo/npm
     offline);
   - the history scrub and #278 run.

   The task ships `FROM` this image.
4. **Validation:** each stage runs in a fresh container (#279) from the candidate image, under the verifier's
   network setting.
5. **Gate:** gold passes F2P and P2P, base fails F2P, P2P passes at base. Test counts are recorded only as
   diagnostics.
6. **Environment failures:** `env_install_failed` and `env_gold_unrunnable` (gold can't build or collect outside
   the test patch). These escalate to one re-bootstrap at the base, capped by `max_rebootstraps`. F2P inference for
   test-patch files is unchanged.

## Verification

Reward, verifier, config guard and mutants are unchanged. The gold patch scores 1.0 in the shipped image.

## Anti-contamination

Lockfiles, the date cap and the provenance check keep later code out of dependencies. Network settings stay as in
#257. huggingface_hub 2.5.0 added sandbox network policies (no internet, or an allowlist, set at
creation), but Harbor's `hf_sandbox` doesn't declare the capability yet.

## LLM use

At bootstrap, plus at most `max_rebootstraps` re-bootstraps per run, each capped by `max_llm_spend_usd`.

## Cost

pydantic on arm64:
- a cold sync with the Rust core build: 39 s;
- the next candidate with a warm `target/`: 10 s;
- environment: 81 MB per candidate.

x86 timings are measured in the rollout.

## Alternatives considered

- **Epochs by fingerprint:** about one per two candidates, and no help for native code. Kept only as a cache key.
- **Time windows:** hugo reached 19 tasks, not 54.
- **Test-percentage reuse:** rejected on #253.
- **Install at solve time:** needs network, and validation would differ from the shipped image.

## Rollout plan

1. Replay check on six bootstrapped repos. Stop if recipes don't replay.
2. Compare with `head` on hugo, networkx, pydantic and fiber: yield by PR age, time, storage.
3. Oracle and no-op runs on every task. Then make `candidate` the default.

## Open questions

- Can the agent reliably separate `install_cmds` from one-off setup?
- How should differing Python or Node versions be installed?

## References

#253, #279, #257, #278. RepoLaunch (2603.05026), SWE-Next (2603.20691), SWE-rebench V2 (2602.23866).

## Implementation

*Placeholder while `draft`.*
