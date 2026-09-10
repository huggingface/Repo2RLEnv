# Running upstream reproductions

## Unified experimental runner

The preferred interface now lists implemented workflows and executes one native
stage at a time. The examples below use a new worker name; inspect existing
receipts before reusing a worker or creating an attempt.

```bash
.venv/bin/python reproductions/run.py list
.venv/bin/python reproductions/run.py plan swe-gen
.venv/bin/python reproductions/runtime/modal_worker.py create --name reproduction-worker-03 --hours 3 --cloud-reserve 10
.venv/bin/python reproductions/run.py run runtime bootstrap --worker reproduction-worker-03
.venv/bin/python reproductions/run.py run swe-gen bootstrap --worker reproduction-worker-03
.venv/bin/python reproductions/run.py run swe-gen generate --worker reproduction-worker-03
.venv/bin/python reproductions/run.py run swe-gen validate --worker reproduction-worker-03
```

The runner uploads reviewed recipe files, binds only declared credentials,
reserves paid-stage allowance and records logs under
`runs/runner/WORKER/EXPERIMENT/STAGE-ATTEMPT.*`. A repeated attempt ID is refused
before remote execution or spending. To retry after inspecting a failure, pass
an explicit new `--attempt` value. Native directories may still need inspection;
the runner never assumes a failed command had no side effects.

Dependencies must have a completed receipt on the same worker with the current
shell recipe hash. Receipt manifests preserve Python helpers and other source
hashes for review; dependency reuse currently checks the shell recipe, not every
helper or remote package. Re-bootstrap after changing a dependency's helpers.
Stage completion means its process exited successfully, not that tasks passed.

Only CLI-Gym and harden-v0 examples need `runtime/restore-pilot` plus access to the
private first-pilot artifacts. Other bootstraps do not require that dataset.
SCALER's `expand` stage reproduces released-family expansion, and the
SWE-rebench V2 component replays a published sample rather than synthesizing a task.
See [STATUS.md](STATUS.md) and individual `RESULTS.md` files for measured outcomes.

## Original direct recipe commands

Use Python 3.12+, the local controller requirements, a configured Modal account,
and `.env` containing the required OpenAI/Anthropic keys. Keep the controller at
the repository root. No Docker daemon or Apptainer installation is needed locally.

```bash
uv pip install -r reproductions/runtime/requirements.txt
.venv/bin/python reproductions/runtime/modal_worker.py create --name reproduction-worker-02 --hours 3 --cloud-reserve 10
.venv/bin/python reproductions/runtime/sync_recipes.py --name reproduction-worker-02 runtime seta-seed2synth seta-evol endless-terminals tmax swe-smith
```

For each remote script below, use this command shape. Set an adequate bounded
timeout for that stage and include only the named credential. Reserve a new
operation in `runtime/budget.py` before paid inference. Existing operation IDs
cannot be reused accidentally.

```bash
.venv/bin/python reproductions/runtime/modal_worker.py exec \
  --name reproduction-worker-02 \
  --script reproductions/seta-seed2synth/bootstrap.sh --timeout 900
```

| Stage | Remote recipe | Credential / result |
| --- | --- | --- |
| Docker smoke | `runtime/docker_smoke.sh` | No model key; build/run evidence |
| Native terminal runtime | `runtime/apptainer_bootstrap.sh`, then `runtime/apptainer_smoke.sh` | No model key; required before Endless/TMax |
| SETA setup | `seta-seed2synth/bootstrap.sh` | Frozen public Unix.SE sample; published seed split was gated |
| SETA generation | `seta-seed2synth/run_smoke.sh` | `--credential ANTHROPIC_API_KEY`; original Opus author |
| SETA export/contrast | `seta-seed2synth/validate_smoke.sh` | Fresh Harbor NOP and oracle |
| SETA offline blind audit | `seta-seed2synth/run_offline_v2.sh` | `--credential ANTHROPIC_API_KEY`; separate audit image |
| Evol generation | `seta-evol/run_smoke.sh` | Same SETA installation; original Sonnet author |
| Evol export/contrast | `seta-evol/validate_smoke.sh` | Child lineage plus Harbor execution |
| Endless setup | `endless-terminals/bootstrap.sh` | Original source and endpoint adapter |
| Endless generation | `endless-terminals/run_smoke.sh`, then `run_batch.sh` | `--credential OPENAI_API_KEY`; original GPT-4o CLI defaults |
| Endless conversion | `endless-terminals/convert_batch.sh` | Original converter plus Harbor layout; OpenAI key |
| Endless native/Harbor probes | `endless-terminals/probe_batch.sh` | Original solver, remote SIF build and compatibility patch; OpenAI key |
| TMax setup/generation | `tmax/bootstrap.sh`, then `run_smoke.sh` | Anthropic key for generation; recorded Sonnet substitution |
| TMax conversion | `tmax/convert_compat.sh` | Original converter with nullable-metadata serialization fix |
| TMax native probes | `tmax/probe_smoke.sh` | Anthropic key; preserve missing-oracle/failed-solver results |
| SWE-smith setup | `swe-smith/bootstrap.sh` | Supported addict profile and native image |
| SWE-smith mutations/validation | `swe-smith/run_native.sh` | Native procedural generation, collection and validator; no model key |
| SWE-smith issues/export | `swe-smith/run_issues.sh` | OpenAI key; original issue writer, local Harbor adapter |
| SWE-smith contrast | `swe-smith/validate_harbor.sh` | All five exports, offline task containers |
| SWE-smith blind audit | `swe-smith/run_blind_audit.sh` | Anthropic key; one separate tmux-enabled audit image |

These recipes intentionally use fixed experiment directories. Bootstraps and
patch application expect a fresh experiment; do not rerun completed steps without
inspecting native state. Newly sampled UUIDs differ between runs. The TMax
`probe_extended.sh`, `recover_trace.sh`, `fix_verifier_dependency.sh` and
`run_solver_compat.sh` preserve the **recorded smoke case**, including its concrete
task ID and missing pytest plugin. They are evidence-backed case repairs, not
automatic repair rules for arbitrary new tasks. Generic generation/export and
`probe_smoke.sh` discover each new bundle by directory.

`seta-seed2synth/run_repeat.sh` uses two more frozen source IDs.
`seta-evol/run_repeat.sh` validates both new parents before starting their children.
In this campaign the timing parent failed, so `resume_validated_repeat.sh` records
that exclusion and evolves only the passing parent. `seta-seed2synth/fix_reference_precision.sh`
keeps a separate repaired reference and performs three fresh validation repetitions.
Endless `run_batch_02.sh`, `convert_batch_02.sh` and `probe_batch_02.sh` run the next
five candidates. These follow-up recipes expect the initial patches already applied.

`endless-terminals/audit_shortcuts.sh` runs deliberately invalid solutions against
unchanged verifiers. Its reward-1 outcomes are **quality failures**, not valid
oracles. `REPRO_ATTACK_TASK` restricts the case and `REPRO_ATTACK_RUN` names a fresh
output directory when diagnosing a failed probe.

## Preserve results and stop the worker

Run `runtime/collect_artifacts.py DESTINATION.tar.gz` inside the worker, then use
`modal_worker.py download` for the archive and its manifest. The collector excludes
Docker/SIF images, records file hashes and explicitly lists skipped files. Raw
logs, native bundles, failed attempts and corrected variants remain separate.

```bash
.venv/bin/python reproductions/runtime/modal_worker.py stop --name reproduction-worker-02
.venv/bin/python reproductions/runtime/budget.py status
```

Settle cloud allowance from elapsed allocation and pricing, or an invoice when
available. Model usage estimates and cloud reservations are not billed totals.
The new campaign limit is $500; the earlier Tasksmith campaign is separate.
The checked-in [artifact receipt](ARTIFACTS.md) identifies the immutable snapshot
and provides download instructions. Generated datasets remain outside Git.

## What counts as a result

Keep generation, native acceptance, Harbor export, execution contrast, conversion
parity and independent quality separate. A generated bundle can pass the released
initial tests yet already contain its final answer. A model failure can expose a
missing verifier dependency rather than an impossible coding task. Both cases
occurred in this campaign; see the per-experiment `RESULTS.md` files.
