# SWE-rebench V2 released-component pilot

Ran the original PR-description and metadata annotation templates on the released
`unidata__netcdf-c-1925` sample, using GPT-4o with a 4096-token response cap. Both
native API calls completed. The original golden evaluator also passed on the
published image: all 12 expected fail-to-pass tests pass and none of the 177
pass-to-pass tests regress.

This reproduces released annotation and evaluation components. It does not
reproduce an unavailable production miner, generate a new task, or establish an
offline Harbor task. Harbor packaging of this replay sample remains pending.

Source pin: `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`.
Image digest: `swerebenchv2/unidata-netcdf-c@sha256:a8957bcd0e3c40e401c1c4e70bf7ea3c04ded3ac64ef056bbc7302f7289542b6`.
The image HEAD equals the sample's base commit
`ad6bff35c39a0600fb8f2e176be4269e768e4e22`.

Commands: `python reproductions/run.py plan swe-rebench-v2`.
Evidence: `swe-rebench-v2/native-output/annotated.json`,
`swe-rebench-v2/native-output/golden-evaluation.json` and
`evidence/swe-rebench-v2/annotation-api.jsonl` in the phase-two archive.
