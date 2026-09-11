# SCALER released-family expansion

Source: [ALEX-nlp/SCALER](https://github.com/ALEX-nlp/SCALER), Apache-2.0,
commit `60c6c5037866c718f4c001ea338f9c5a91cb01ae`. The license is retained.
`compatibility.py` retains the import wrapper and Python newline repair from
`SCALER/exec_and_verify.py`. The supplied generator and reference programs are
input data, never a runtime dependency on the SCALER repository.

The owned implementation follows `SCALER/generate_problem_from_environment.py`
and `after_extract.generate_problem_detail_and_ground_truth`: select a difficulty,
scale each parameter as `int(scale * base + min)`, run `generate_testcase`, parse
its input/detail pair, and execute the first successful Python/C++ reference.
The first-comma parser and whitespace-separated numerical-array conversion are
retained. Concrete reasoning instructions retain the native problem/input framing.

The active `verl/utils/reward_score/__init__.py` route for `scaler*` selects
`think_test_math.compute_score`, using math-verify, rather than the commented-out
`environment.compute_score` route. The standalone grader preserves that metric's
gold/prediction extraction configurations and -1/+1 reward. It pins the ordinary
math-verify library to 0.8.0 and accepts a bounded answer file. No verl install is
needed. The reference answer is private in a separate verifier environment.

Owned adaptations: seeded random sampling, offline Docker execution on Modal or
Daytona instead of SandboxFusion, Python 3.12/C++17 CPU support, distinct concrete
input hashes, structured receipts and a Harbor answer-file interface. A reference
execution timeout rejects the candidate; it does not launch unrestricted retries.
The oracle replays the answer computed by the supplied reference program for that
fixed instance. It is not a new hand-authored solution.

Scope is released-family expansion only. The released fresh-family verification
loop is commented out, and this recipe does not claim to reproduce new-family
synthesis, adaptive training, online difficulty control or model training.
These are algorithmic reasoning tasks, counted separately from repository coding
tasks. Generator/reference success and Harbor parity precede export; independent
quality acceptance remains deferred. No LLM calls are needed for this profile.
