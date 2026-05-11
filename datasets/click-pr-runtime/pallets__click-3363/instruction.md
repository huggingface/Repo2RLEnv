# Issue

**Title:** Auto-detect `type=UNPROCESSED` when `flag_value` has non-basic types

## Description

This  by addressing the left-over cases that were not fixed in #3030.

With this PR, when `flag_value` is not a basic Python type, like a class or an `Enum` value, we do not force the stringification imposed by `convert_type`. But still use the later for detection of the guessed type.

This way we can preserves programmer-provided Python objects as-is, without requiring having `type=click.UNPROCESSED` sets explicitly, restoring pre-8.0.0 user-friendlyness and user expectations.

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `c8da1fcc2cb4` and running the modified tests.