---
title: "Learner Python runtime"
---

Repository tasks with `use_system_site_packages=true` install their
dependencies into `/opt/tasksmith-venv`. The separate verifier already calls that
interpreter explicitly. The learner is the tricky part: Harbor's Terminus-2
learner starts a tmux pane with `bash --login`, and the login startup files can
replace the image's `PATH`.

The shared repository emitter adds one last layer to the learner image that
restores the venv after the user's login startup. It saves an existing
`.bash_profile` as `.repo2rlenv-original-bash-profile` and sources it. Failing
that, it sources the existing `.bash_login` or `.profile`, in Bash's order of
precedence. An early `return` in the original file returns to the wrapper, which
then sets `PATH` and `VIRTUAL_ENV`.

For native commands that run `bash -c`, the learner image also sets `BASH_ENV` to
`/etc/repo2rlenv-venv.sh`. That file only restores the runtime variables; it
doesn't source user login or interactive scripts. Bash reads it for every
noninteractive shell, including the one Harbor starts through `su learner`. The
task instruction names the interpreter explicitly as well.

Dependency and source layers, the verifier image and non-venv profiles are
unchanged. Harbor and previously generated bundles aren't modified. New task
revisions still need fresh validation, because cached image layers aren't
validation evidence.

## Remote smoke before using a new runtime

Run this in a metered cloud environment, within the campaign's remaining
allowance. It makes no model calls. Use an image with a small dependency, such as
`safetensors`, installed only in the task venv. Keep the image identity and a
small receipt of what you observed.

1. Through the owned Harbor environment, run the command below as
   `user="learner"`, both directly and with
   `command="bash --login -c " + shlex.quote(command)`.
2. Start a real Harbor `TmuxSession` as `learner`, with recording off, and send
   the same command with `send_keys([command, "Enter"], block=True,
   max_timeout_sec=30)`. That exercises the same login pane Terminus-2 uses. Put
   bounds on setup and cleanup too, then stop the session and release its
   allocation.
3. Capture `PATH`, `BASH_ENV`, the interpreter identity and the prefix before
   importing the dependency. Check both command names, even if one fails. When
   you're diagnosing a mismatch, compare a direct SDK exec, root `bash -c` and the
   learner shell, with the runtime hook disabled for those diagnostic controls.
   With the startup layer in place, all three ordinary learner paths must select
   the same venv:

```sh
runtime_status=0
for runtime_command in python python3; do
  "$runtime_command" -c 'import json, os, pwd, sys; print(json.dumps({"executable": sys.executable, "prefix": sys.prefix}), flush=True); import safetensors; assert pwd.getpwuid(os.geteuid()).pw_name == "learner"; assert sys.prefix == "/opt/tasksmith-venv", sys.executable; print(safetensors.__version__)' || runtime_status=1
done
exit "$runtime_status"
```

For the CPU system-interpreter profile, use a dependency from that exact
profile (for example `pytest`) and assert the declared system prefix, which is
normally `/usr/local` for `python:3.12-slim-bookworm`. Repeat the venv smoke on
the cached PyTorch CUDA image without initializing CUDA or downloading models. If
the results differ, read the actual image's `/etc/profile` and the learner's
startup files; don't guess their contents from the image tag.

The local regression tests run only the generated startup installer and
temporary shell fixtures. They show a failing lookup of the base interpreter, a
successful venv lookup, preserved startup precedence and content, and
early-return behavior. They also exercise nested noninteractive shells after a
`PATH` reset, and check that user login files don't run there. They don't
replace the cloud smoke.
