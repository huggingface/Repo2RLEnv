# Issue

**Title:** Fix speculative empty string check

## Description

Fix #3298 by checking against a speculative empty string.

That not unreasonable given the hard-coded nature of the empty string.

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `04ef3a6f473d` and running the modified tests.