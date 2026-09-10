# Recorded deviations

`001-api-receipts.patch` records original LiteLLM responses and usage; it does not
change prompts, sampler, fixtures, test creation, build retries or acceptance.

Smoke configuration: original `legacy` corpus sampler, Python random seed 20260910,
one input and one worker. The model is explicitly substituted with
`anthropic/claude-sonnet-4-6` using the available API key, instead of upstream's
`gemini/gemini-3.1-pro-preview`. This is a workflow reproduction, not an exact
configuration or paper-result reproduction.

Native generation and fixtures execute inside a Modal VM using Apptainer 1.5.3.
Use the released Harbor converter and retain its missing reference solution as a
limitation. A successful original solver trace, if available, is a derived witness.

`002-null-metadata.patch` serializes optional null metadata as an empty TOML
string. The released legacy generator writes `base_image: null`, but its converter
calls `.replace()` on that value and crashes. This changes metadata serialization
only; the instruction, container setup and final tests are copied by the original
converter. The failed unpatched export remains recorded separately.

`003-solver-provider-compat.patch` sets `tool_choice="required"` only when
`REPRO_REQUIRE_TOOL_CALL=1` is explicitly enabled. The original solver already
asks for a bash tool call on every turn. This prevents a text-only final assistant
message from becoming an unsupported assistant prefill on the next Sonnet request.
It also ends a failed sample after an empty provider response, avoiding the
observed loop of invalid retries. A fresh 26-response native sample succeeded with
this adapter. The generator's prompt, sampler and acceptance rules are unchanged.

The smoke task itself omits `pytest-timeout` from its image while its verifier
invokes `pytest --timeout=120`. `fix_verifier_dependency.sh` creates a separate
runtime variant with `pytest-timeout==2.4.0`; instruction and final tests remain
identical. The original 50/51 failure and corrected 51/51 result are both retained.
This is a repair of one observed generated task, not an upstream-equivalent result
or a general automatic dependency fixer.
