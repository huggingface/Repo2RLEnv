# Issue

**Title:** Split string values from `default_map` for multi-value parameters

## Description

The issue reported in #2745 is still present on `stable` branch, in which data from `default_map` is naively processed when `nargs > 1` by `type_cast_value` and unpack each characters from a string.

The fix consist in evaluating these data as if they are environment variables, where we are in a strict string-domain.

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `8a2b48901a08` and running the modified tests.