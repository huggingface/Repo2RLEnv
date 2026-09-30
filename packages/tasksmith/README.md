# Tasksmith

Turn a merged pull request into a coding task with a private verifier, reference
solution and a runnable [Harbor](https://harborframework.com/) environment.

This package provides the `tasksmith` command for
[Repo2RLEnv's Tasksmith pipeline](https://github.com/huggingface/Repo2RLEnv).
It installs the existing implementation, Daytona support and Harbor, and forwards
commands to `repo2rlenv tasksmith`. All generation, review and repair code stays
in Repo2RLEnv; this package does not maintain a second implementation.

## Install

Requires Python 3.12 or newer:

```bash
pip install tasksmith
tasksmith --help
tasksmith run --help
python -m tasksmith --help
```

To use Modal, including supported GPU tasks, install `tasksmith[modal]`.

## Generate tasks

Tasksmith currently accepts merged public GitHub PRs with added or modified
Python source. It explores the repository, builds the environment, designs the
task, and runs bounded review and repair. Remote generation requires provider
and model credentials and incurs usage charges. Installation and `--help` do
not launch generation or paid jobs.

Follow the [codebase evaluation tutorial](https://huggingface.github.io/Repo2RLEnv/tutorials/evaluate-your-codebase/)
for PR selection, authentication, runtime setup, campaign budgets and an explicit
Daytona configuration. Use `tasksmith run ...` wherever it shows
`repo2rlenv tasksmith run ...`; all arguments and result formats are the same.
The `repo2rlenv` command is also installed for campaign and quality operations.
The CLI's help retains the underlying `repo2rlenv tasksmith` command name.

No service credentials are bundled. Pi/OpenCode runtime installation needs Node.js
22.19+ and npm; the reference documentation covers the remaining setup.

- [Tasksmith reference](https://huggingface.github.io/Repo2RLEnv/pipelines/tasksmith/)
- [Review and repair](https://huggingface.github.io/Repo2RLEnv/pipelines/quality_loop/)
- [Source and issues](https://github.com/huggingface/Repo2RLEnv)

## Package maintenance

The launcher has its own version (`0.0.1` initially) and supports the Repo2RLEnv
0.9 release series from 0.9.3. Releasing it does not release or rename Repo2RLEnv.
From the repository root:

```bash
uv build packages/tasksmith --out-dir packages/tasksmith/dist
uv venv workspace/tasksmith-package-test
uv pip install --python workspace/tasksmith-package-test/bin/python packages/tasksmith/dist/tasksmith-0.0.1-py3-none-any.whl
workspace/tasksmith-package-test/bin/python -m unittest discover -s packages/tasksmith/tests
workspace/tasksmith-package-test/bin/tasksmith --help
```

Review both built artifacts before publishing with an authorized PyPI token.
Only this package's explicitly named wheel and sdist should be uploaded; never
upload the root project's `dist/` as part of this package's release.
