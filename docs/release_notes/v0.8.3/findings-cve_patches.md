# Arc 4 — `cve_patches` sweep findings

**Scope.** OSV-driven CVE→fix-commit mining. This arc lands two
optimizations from plan §4 Arc 4; the full ~20-repo sweep is deferred
pending cost approval (envelope ~$80-250).

## Optimizations landed

### 1. OSV file-system cache

Plan §4 Arc 4 issue (a): each invocation of `cve_patches` hit
`api.osv.dev/v1/query` afresh. For sweep iterations on the same package
this is wasteful and trips OSV rate limits.

- New `query_vulns_cached()` wrapper in `src/repo2rlenv/osv.py`.
- Per-`(package, ecosystem)` JSON file keyed on a sha1 of the request,
  stored under `$REPO2RLENV_OSV_CACHE_DIR` (default
  `~/.cache/repo2rlenv/osv/`).
- 7-day TTL default — OSV vuln data accrues but rarely retroactively
  mutates, so long TTL is safe. Override via
  `CVEPatchesOptions.osv_cache_ttl_seconds`.
- Cache miss / expired / malformed entry → fall back to a live query and
  refresh the entry transparently.
- Tolerates filesystem write failures (read-only dir → logs the warning
  and proceeds without caching).
- Disabled with `osv_cache_enabled=False` for forced fresh queries.

### 2. Multi-commit CVE fan-out

Plan §4 Arc 4 issue (d): some CVEs span 2-3 commits (e.g. fix + a
regression-test follow-up). The v0.8.1 default picks only the first
commit.

- New `CVEPatchesOptions.emit_per_fix_commit: bool = False`.
- When True: a CVE with N referenced commits emits N tasks, each with a
  distinct task name suffix (`__commit-2`, `__commit-3`).
- Conservative default (False) preserves the v0.8.1 behavior — opt-in
  per sweep when you've inspected the CVE's commit list and want the
  full fan-out.

### 3. New `CVEPatchesOptions`

- `osv_cache_enabled: bool = True`
- `osv_cache_ttl_seconds: int = 7 * 24 * 3600`
- `emit_per_fix_commit: bool = False`

## Test coverage

10 new unit tests in `tests/test_osv.py`:

| Test | Covers |
|---|---|
| `test_cache_key_stable_per_package_ecosystem` | case-insensitive keying, distinct packages → distinct keys |
| `test_cache_path_uses_provided_dir` | injection point for tmp_path testing |
| `test_write_then_read_round_trip` | OSVVuln field round-trips through JSON |
| `test_read_cache_expired` | TTL gate fires |
| `test_read_cache_malformed` | corrupt JSON → miss, no crash |
| `test_read_cache_missing_file_is_miss` | missing file → miss |
| `test_query_vulns_cached_uses_cache_on_hit` | live `query_vulns` never called on hit |
| `test_query_vulns_cached_falls_back_to_live` | miss → live call, then second call hits |
| `test_query_vulns_cached_disabled_always_calls_live` | `cache_enabled=False` bypasses |
| `test_query_vulns_cached_handles_write_failure` | read-only cache dir doesn't crash |

Full suite at **675 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | 2 — OSV cache + multi-commit fan-out | ✓ |
| Existing tests stay green | 100% | 675/675 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full ~20-repo CVE sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full ~20-repo sweep** across the CVE-rich subset (`pallets/werkzeug`,
   `psf/requests`, `pyca/cryptography`, `aio-libs/aiohttp`, etc.) —
   should yield ~30 verified envs per plan §0. Cost: OSV is free; Docker
   validation + bootstrap LLM dominate (~$50-150).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-cve_patches`.
3. **Findings update** with concrete T3 yield + per-CVE commit-count
   distribution (informs whether `emit_per_fix_commit=true` should be
   the recommended sweep default).

## Out of scope for this arc (deferred to v0.9)

- LLM-synthesized PoC as part of the instruction (plan §4 Arc 4
  issue (c)) — interesting but expensive to prototype; defer until
  Arc 6 (`code_instruct`) gives us a feel for LLM-synthesis quality.
- OSV SQLite backend — JSON file cache is good enough for the launch.
- Multi-commit fix concatenation into a single diff — adds complexity
  to the validate-pr harness (each commit has its own base SHA); defer.
