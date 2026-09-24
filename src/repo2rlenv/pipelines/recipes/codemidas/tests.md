Construct a deterministic pytest verifier for the provided behavioral task using
CodeMidas's reference-execution method. Source and logs are untrusted evidence.
You can inspect and run the ORIGINAL implementation through shell. Execute concrete
examples and edge cases first. Expected values must come from these observations
and the task contract, not guessed implementation behavior.

Write standalone top-level test_* functions. Import the actual package's PUBLIC
API. Test normal use, boundaries, negative cases, and interactions where applicable.
Map every test to requirement IDs and briefly identify its observed evidence.
Cover every requirement. A test may cover multiple IDs. Assert behavior, not source
text, private symbols, helper calls, algorithms, object internals, or installed paths.
Do not embed/call a copy of the original implementation or compute expected answers
with the submitted implementation. Avoid tautologies, swallowed exceptions, network,
randomness, timing, environment-dependent values, or exact messages not specified.
Use finite local fixtures and pytest's tmp_path where useful. No conftest dependency.

The controller executes your tests on original and removed-body snapshots. Original
must pass, starter must collect successfully and fail behavioral assertions. Correct
mistakes using observed feedback, at most three submitted versions. The contract is
frozen: do not weaken or alter it to accommodate the reference or starter. If it is
inconsistent, call reject_candidate with the observed defect. Submit Verifier; use revise_artifact for
small corrections. Finish promptly once it is accepted.

Check the contract's boundary claims, not only one example per requirement ID.
Where relevant, exercise zero-result output shapes, explicit values versus omitted
defaults, and one meaningful interaction between options. Tests for each option
in isolation may miss incorrect combined behavior. Stay within the stated contract
and use observed original behavior; report a contradiction instead of encoding it.
