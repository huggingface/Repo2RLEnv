# Issue

**Title:** Path normalization documentation clean-up + `shlex.split` extensive tests

## Description

This is a follow up on #3245, which was merged to `stable` a bit too early before some documentation issues has been caught.

So this PR:
- Move all references to issues and PR, as well as developer-centric details, out of their published docstrings and to Python code comments
- Add some more edge-cases to illustrate the behavior of path normalization
- Add explicit test cases to demonstrate and verify the behavior of the underlying `shlex.split` function without `posix=False`

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `1339fd332335` and running the modified tests.