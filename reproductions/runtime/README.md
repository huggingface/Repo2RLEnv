# Remote execution and evidence utilities

These helpers run original upstream implementations; they do not author tasks.
Run controllers from the repository root using `.venv/bin/python`.

```bash
python reproductions/runtime/modal_worker.py create --name reproduction-worker-01 --hours 3 --cloud-reserve 10
python reproductions/runtime/modal_worker.py exec --name reproduction-worker-01 --script reproductions/runtime/docker_smoke.sh --timeout 300
python reproductions/runtime/sync_recipes.py --name reproduction-worker-01 runtime seta-seed2synth
python reproductions/runtime/modal_worker.py exec --name reproduction-worker-01 --script reproductions/seta-seed2synth/bootstrap.sh --timeout 900
python reproductions/runtime/budget.py reserve --key seta-synthesis-smoke-01 --usd 20 --note 'Two bounded author sessions'
python reproductions/runtime/modal_worker.py exec --name reproduction-worker-01 --script reproductions/seta-seed2synth/run_smoke.sh --credential ANTHROPIC_API_KEY --timeout 2800
python reproductions/runtime/modal_worker.py stop --name reproduction-worker-01
```

Names/operation IDs in this example are used by the first campaign: use new ones for
new attempts. Do not rerun an existing generation command blindly. Native pipelines
have different resume conventions; inspect their output and the run receipt first.
Bootstrap scripts assume a fresh experiment directory. A failed installation can
be resumed at its failed command after examining logs; completed steps need not rerun.

The worker has 4 CPUs, 16 GiB RAM and a provider-enforced lifetime. Builds occur on
Modal; Docker and Apptainer commands execute inside the VM. The local controller
only submits commands and transfers files. Secrets are injected into the specific
remote process requesting them, not into the worker image or task containers.

The ledger is a locked, atomic accounting file under ignored `reproductions/runs/`.
Its new campaign allowance is $500. Reservations are not billed charges. Settle them
only with an execution receipt, and distinguish SDK-reported model cost, rate-table
estimates and provider invoices. A reservation alone does not enforce API spending:
SDK limits, small native input batches, output limits and worker timeouts bound the
actual execution. Retain uncertain allowance after interruptions.

`harbor_artifacts.py export` copies only native Harbor contract files, excluding
author logs and draft notes. It preserves file bytes and rejects unreviewed symlinks.
`harbor_artifacts.py audit` runs NOP and oracle through Harbor in fresh Docker
containers. It reports missing references and infrastructure exceptions explicitly.
A 0→1 reward contrast alone does not establish verifier quality or conversion parity.

Use `modal_worker.py download --remote PATH --local PATH` for logs/manifests or a
remote-created archive. Do not download Docker or SIF images to the local machine.
All raw logs and artifact folders remain ignored. Filesystem access is supported by
the installed Modal 1.5.5 SDK.

Validated references:

- [Modal VM sandboxes and Docker example](https://modal.com/docs/guide/vm-sandboxes)
- [Modal filesystem API](https://modal.com/docs/guide/sandbox-files)
- [Apptainer installation](https://apptainer.org/docs/admin/main/installation.html)
- [Claude SDK options and cost limits](https://code.claude.com/docs/en/agent-sdk/python)

Daytona credentials are available as a fallback. This first implementation exercises
Modal's VM worker; it does not claim that an equivalent Daytona recipe has been tested.

Contract tests: `python -m unittest discover -s reproductions/runtime/tests -v`.
