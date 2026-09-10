# Recorded deviations

`001-api-endpoint-and-receipts.patch` makes the original OpenAI SDK client use the
configured API key and endpoint (default `https://api.openai.com/v1`) instead of a
hardcoded local vLLM server. It records complete original responses and usage.

The smoke command uses `gpt-4o`, the original CLI's default model, via the OpenAI API.
This differs from the README's Qwen3-32B/vLLM configuration. All original prompts,
temperature defaults, generation stages and acceptance checks are retained. We seed
Python's original random sampler with 20260910 and reduce batch/concurrency to one.
The initial smoke keeps the original 1024-token generation setting.

Apptainer 1.5.3 runs inside a Modal VM with subordinate root IDs configured. The
upstream installation script's PPA/sudo setup is replaced by pinned release packages;
the same Apptainer build and execution operations are exercised remotely.

The released converter creates a Dockerfile and runs initial tests. The released
reward helper expects an existing Harbor layout. A packaging adapter must supply
that layout while copying instruction and test contents without rewriting them.
Its conversion result is recorded separately from native generation acceptance.
