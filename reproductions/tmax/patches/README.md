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
