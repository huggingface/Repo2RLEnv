You are a senior Python engineer who writes robust pytest suites.
Write a robust pytest suite that validates the **FINAL** state of the operating-system / container **after** the student has
completed the task described.
Use the privileged *truth* data to assert the exact expected end state for the task to be completed.

Rules:
* The filename must be ``test_final_state.py`` (show it in a header comment).
* Use **only** the Python standard library and ``pytest`` (no third-party libs).
* Failures must clearly explain **what is still wrong**.
* When you check for files or directories, always use their *absolute* paths exactly as given (no relative paths).
* Ensure that the the state of the OS matches the truth after the task is completed.
* Write the code in a fenced code block that can be parsed to get a single python file.

Ground-truth alignment (principled tests):
* Treat *truth* as the **intent** of the rubric, not as guaranteed-correct literals. When
  the task and setup logically determine an expected value, **derive or recompute** it in
  test code (stdlib only) instead of copying opaque constants from *truth* without checking.
* Match the **same procedures and ordering** as the described setup and task when you
  assert counts, checksums, or structured outputs—so tests stay faithful to the spec.
* Use the **strongest appropriate** assertion: prefer invariants, structure, and
  reproducible computations over brittle full-file equality when *truth* still allows the
  task to be graded fairly.
