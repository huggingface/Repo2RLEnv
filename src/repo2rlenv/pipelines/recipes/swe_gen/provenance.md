# SWE-gen adaptation

Source: [abundant-ai/SWE-gen](https://github.com/abundant-ai/SWE-gen),
commit `14e185f413f7bff03f8f9fec6fb246681bf61d74`, Apache-2.0.
The upstream license is retained in `UPSTREAM_LICENSE`.

`instruction_prompt.md` retains `COMBINED_SYSTEM_PROMPT` from
`src/swegen/create/task_instruction.py`. Runtime guidance explicitly changes the
repository root from `/app/src` to `/workspace`. Its combined substantiality and
instruction stage, including the optional `force_generate_instruction` switch,
remains visible in the owned recipe.

`src/swegen/create/claude_code_runner.py` provides the healthy-head / reverse-source-
patch / reference-fix workflow. The owned worker implements that workflow with
Repo2RLEnv's cached bootstrap and fresh offline test containers. It retains the
healthy head's tests while reversing the implementation patch.

This is an explicit Python profile adaptation, not a byte-identical execution of
the upstream agent. Repository exploration and environment creation use supplied
source/test paths and Docker build settings, replacing upstream Claude Code tool
use and automatic environment discovery. Only existing Python source files may be
replaced. The healthy/broken test contrast runs before the model's instruction
stage to avoid paying for inputs this runtime cannot execute. Harbor emission uses
the owned private verifier and artifact collection, rather than upstream scripts.
No research repository is imported, cloned or installed at runtime.
