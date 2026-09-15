# SWE-Flow adaptation

Source: [Hambaobao/SWE-Flow](https://github.com/Hambaobao/SWE-Flow),
commit `7da5b046fa1dc184674e4e94a9989be56c39e4e7`, MIT, copyright Lei Zhang.
The original license, docstring/specification prompts and their two-shot example
files are retained alongside this notice.

The owned implementation follows `extensions/python/schedule.py`: group tests
with identical observed core dependencies, sort by dependency count, and introduce
only functions not already developed in prior steps. `helper/code_utils.py`
provides the target-stub / newly introduced dependency-removal workflow. Separate
docstring and test-based specification stages retain their native prompts.

The first profile supports module-level synchronous Python functions. Owned
`sys.setprofile` instrumentation replaces installing SWE-Flow-Trace, following its
`sweflow_trace/python/hooks.py` call-recording approach at trace source commit
`e7251448ae7d29c7de5fdcda2a3bc175e547420e`. Sampling uses an explicit
count and seed, as supported by the upstream trace CLI. A five-second per-test
instrumentation window bounds profiler overhead; incomplete traces are recorded
and excluded from scheduling. Classes, async and nested definitions are outside
this profile, and tracing does not follow additional threads or subprocesses.

Model selection and SDK calls use Repo2RLEnv's metered model interface instead of
FluxLLM servers. Bootstrap uses the existing remote image cache rather than an
upstream image. The full configured test suite is measured in fresh healthy and
skeleton containers; this is stronger execution coverage than using only the
step's scheduled test IDs. AST emission may normalize formatting. Generated
docstrings enter the learner skeleton, while the private reference restores the
original source. No upstream Git history, images or research package installation
is required by emitted Harbor tasks.
