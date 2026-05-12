# `cve_patches`

Map OSV vulnerability records to fixing commits in the target repo,
replay the pre-fix state in a sandbox, and emit a Harbor task whose
oracle is the upstream security patch.

| | |
|---|---|
| Status | **shipped (v0.7)** — Python ecosystem |
| Sandbox required at gen | Yes |
| LLM required at gen | No (the LLM spec is still required for bootstrap; the pipeline itself does no LLM calls) |
| Reward kinds emitted | `test_execution`, `diff_similarity` |
| Inspiration | [PatchSeeker](https://github.com/hungkien05/PatchSeeker), CVE-Bench (NAACL '25) |

## Why this pipeline matters

Lots of *datasets* of CVE-fix pairs exist (PatchSeeker covers 5K CVEs;
PATCHEVAL has 1K). What didn't exist before v0.7: a **reusable pipeline**
that takes a repo + the OSV vuln database and turns the pair into
Repo2RLEnv-shaped tasks. Plenty of paper-only artifacts; no library —
until now.

## Algorithm

```mermaid
flowchart TD
    A[Repo URL] --> B[OSV API:<br/>POST /v1/query]
    B --> C[Filter by severity + must have<br/>github commit in references]
    C --> D{For each vuln}
    D --> E[gh api: fetch parent SHA + commit diff]
    E --> F[Split into patch + test_patch]
    F --> G{Has test_patch?}
    G -- yes --> H[Reuse pr_runtime_validate<br/>F2P/P2P inside bootstrap]
    G -- no --> I[Skip validation;<br/>emit with validation_status=no_test_patch]
    H --> J[Emit Harbor task<br/>(instruction=CVE description; oracle=fix diff)]
    I --> J
```

## Data source: OSV (Open Source Vulnerabilities)

We hit OSV's public `/v1/query` endpoint with `{"package": {"name": <pkg>,
"ecosystem": <eco>}}`. The response includes vulns for the target package
across CVE / GHSA / PYSEC identifiers, with structured `references[]`
that often link directly to fix commits.

Why OSV (vs NVD or GitHub Security Advisories):
- No auth required (free, no API key)
- Pre-resolves CVE → fix-commit URLs in `references[]` (saves us from
  building a PatchSeeker-style LLM mapper)
- Covers PyPI / npm / crates.io / Go / Maven / Debian / Alpine / ...
- Records are cross-linked (a single OSV id often carries CVE + GHSA + PYSEC aliases)

## Filters

1. Severity ≥ `min_severity` (default `low`; CVSS-like ranks)
2. At least one `references[].url` of the form
   `https://github.com/<owner>/<repo>/commit/<sha>` matching the
   target repo (handles fork URLs gracefully — they're rejected)
3. `gh api commits/<sha>` resolves to a parent (skip root commits)
4. Source patch must be non-empty (some "fix" commits are CI-only)
5. `len(source_files) ≤ max_source_files_per_fix` (default 50)

## Validation

Reuses `pipelines/pr_runtime_validate.py` verbatim:
- If `test_patch` is non-empty → two-stage F2P/P2P (same as pr_runtime)
- If `test_patch` is empty → skip validation; `validation_status="no_test_patch"`
  is recorded in metadata. The Harbor verifier still runs the test suite
  after the agent applies their patch — the reward signal is just
  "tests still pass with the fix applied", which is weaker than F2P but
  useful as training data.

## Options

See `CVEPatchesOptions` in `src/repo2rlenv/spec/options.py`.

| Field | Default | Notes |
|---|---|---|
| `osv_ecosystem` | `None` (auto from owner) | `PyPI` / `npm` / `crates.io` / ... |
| `osv_package` | `None` (= repo name lowercased) | package identifier in the ecosystem |
| `min_severity` | `"low"` | `low` / `medium` / `moderate` / `high` / `critical` |
| `limit` | 50 | max emitted tasks |
| `require_fail_to_pass` | **False** | CVE fixes often have no test_patch — accept anyway |
| `min_fail_to_pass` | 0 | tighten if you want F2P-only |
| `max_source_files_per_fix` | 50 | reject sprawling fixes |
| `require_new_test_funcs` | False | security commits often don't add new tests |
| `skip_validation` | False | emit raw without sandbox run (debug) |
| `validation_timeout_sec` | 600 | per-candidate cap |

## `[metadata.repo2env.cve_patches]` schema

```toml
[metadata.repo2env.cve_patches]
cve_id = "CVE-2024-49767"
osv_id = "GHSA-q34m-jh98-gwm2"
aliases = ["CVE-2024-49767"]
cwe_ids = ["CWE-407"]
severity = "HIGH"
published = "2024-10-25T00:00:00Z"
fix_commit = "50cfeebcb0727e18cc52ffbeb125f4a66551179b"
parent_commit = "f8c2a3a..."
fail_to_pass = []
pass_to_pass = []
validation_status = "no_test_patch"  # or "verified" when F2P is non-empty
```

## End-to-end smoke

```bash
repo2rlenv generate \
  --repo pallets/werkzeug \
  --pipeline cve_patches \
  --pipeline-opt limit=1 \
  --pipeline-opt min_severity=high \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/werkzeug-cve

harbor run -a oracle -p ./datasets/werkzeug-cve/<task-id>
# Mean reward 1.000
```

## v0.7 trade-offs (to revisit)

- **No PoC synthesis.** When `test_patch` is empty, the verifier signal
  is weak (just "suite passes with fix applied"). A future v0.8 mode
  can LLM-synthesize a PoC test that exercises the vulnerability —
  with a gate around that (security implications of distributing PoCs).
- **No NVD-direct path.** We rely on OSV's pre-resolved fix URLs. For
  CVEs OSV hasn't curated, we miss them. A PatchSeeker-style
  LLM+embedding fallback is on the roadmap.
- **Single ecosystem auto-guess per owner.** Repos owned by users not
  in our table (Pallets / PSF / Django / etc.) default to PyPI; the
  user can always override with `--pipeline-opt osv_ecosystem=npm`.

## What we adapted from inspiration projects

| What | Where | How we apply it |
|---|---|---|
| OSV `/v1/query` API | osv.dev (Google public service) | Direct HTTPS POST, stdlib only |
| Severity rank ordering | CVSS conventions | `_SEVERITY_RANK` constant |
| CVE-→commit reference pattern | PatchSeeker recipe | `OSVVuln.fix_commits` regex on `references[].url` |
| F2P/P2P validation harness | SWE-bench / pr_runtime | Reused verbatim (no new code) |

No code is copied from inspiration projects. The pipeline is original Python stdlib + reuses pr_runtime's emission helpers.
