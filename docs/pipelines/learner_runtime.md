# Learner Python runtime

Repository tasks with `use_system_site_packages=true` install their dependencies in
`/opt/tasksmith-venv`. The separate verifier already invokes that interpreter
explicitly. Harbor 0.20.0's Terminus-2 learner starts a tmux pane with
`bash --login`, whose startup files can replace the image's `PATH`.

The shared repository emitter adds a final learner-image layer that restores the
venv after the user's login startup. It preserves an existing `.bash_profile` in
`.repo2rlenv-original-bash-profile` and sources it; otherwise it sources the existing
`.bash_login` or `.profile` in Bash's precedence order. An early `return` in the
original file returns to the wrapper, which then sets `PATH` and `VIRTUAL_ENV`.
The task instruction also names the interpreter explicitly.

This leaves dependency/source layers, the verifier image and non-venv profiles
unchanged. It does not modify Harbor or previously generated bundles. New task
revisions still need fresh validation; cached image layers are not validation
evidence.

## Remote smoke before using a new runtime

Run this only inside a metered cloud environment after its allocation is approved.
There are no model calls. Use an image with a small dependency, such as
`safetensors`, installed only in the task venv. Retain the image identity and a
small receipt of the observations.

1. Through the owned Harbor environment, run the command below with `user="learner"`
   directly and with `command="bash --login -c " + shlex.quote(command)`.
2. Start a real Harbor `TmuxSession` as `learner`, with recording disabled, and send
   the same command using `send_keys(command + "\n", block=True,
   max_timeout_sec=30)`. This exercises the same login pane as Terminus-2. Bound
   setup and cleanup as well; stop the session and release its allocation.
3. Before the final startup layer, the login case must expose the mismatch. After
   the layer, all three paths must select the same venv for both command names:

```sh
for runtime_command in python python3; do
  "$runtime_command" -c 'import json, os, pwd, sys, safetensors; assert pwd.getpwuid(os.geteuid()).pw_name == "learner"; assert sys.prefix == "/opt/tasksmith-venv", sys.executable; print(json.dumps({"executable": sys.executable, "prefix": sys.prefix, "safetensors": safetensors.__version__}))' || exit 1
done
```

For the CPU system-interpreter profile, use a dependency from that exact profile
(for example `pytest`) and assert the declared system prefix, normally
`/usr/local` for `python:3.12-slim-bookworm`. Repeat the venv smoke on the cached
PyTorch CUDA image without initializing CUDA or downloading models. Read the
actual image's `/etc/profile` and learner startup files if results differ; do not
infer their contents from the image tag.

The local regression tests execute only the generated startup installer and
temporary shell fixtures. They demonstrate a failing base-interpreter lookup,
successful venv lookup, preserved startup precedence/content and early-return
behavior. They do not replace the cloud smoke.
