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

`002-converter-receipts.patch` records the converter's unmodified API responses.
The per-task conversion API avoids the batch command's hardcoded published
o3/pass@16 dataset filter; the new tasks have no such preexisting sample file.
The converter uses GPT-4o rather than its GPT-5.1 default.

`003-build-evidence.patch` preserves definitions, initial tests and build logs for
accepted and rejected candidates. The released helper discarded the failure logs.
The first failed candidate attempted `chown user:user` without creating that user.

`004-modal-userns.patch` adds `--fakeroot --userns` to instance startup, matching
the released generator's initial-test execution flags. Modal's VM kernel lacks
kernel squashfs support; the original privileged instance mount failed. The
native prompts, commands, initial/final tests and reward interpretation stay intact.

The probe builds the unchanged definition into a persistent SIF first. The
generator only tests a temporary SIF and omits the persistent image. Its fallback
runtime builder rewrites the definition to an author-specific `/data/...` path;
the probe avoids that fallback. The observed failed rewrite is retained as evidence.
