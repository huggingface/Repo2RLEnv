# Issue

**Title:** `ParamType` and other typing improvements

## Description

PR to track and study the unmerged changes from @AndreasBackx that were left in the stale `typing/paramtype`. See the discussion at https://github.com/pallets/click/discussions/3329 for context.

I was able to salvage and rebase on top of `stable` the majority of the initial changes like:
- making `ParamType` a generic ABC and introducing `ParamTypeInfoDict`
- adding `ParamTypeInfoDict`
- updating `FuncParamType` to a generic
- narrowing of `convert()` return types
- `CompositeParamType` generic with abstract `arity`

Stuff that were made obsolete from PRs merged upstream since the last commit in 2024:
- `BoolParamType`: rewritten in #2956
- `Choice` normalization: already part of #2796
- `File` docstring: already cleaned up in #2586

Other stuff that I skipped as too strict:
- `_compat.py` `t.Any` to `t.AnyStr`
- `File` generic with `t.AnyStr`
- `_is_file_like` with `t.AnyStr`

I also extracted from the original `typing/paramtype` the refactor of `convert_type` which is not strictly related to typing improvements and live in its own PR for later evaluation at: https://github.com/pallets/click/pull/3372

Related to https://github.com/pallets/click/pull/2805

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `30a7f7c8e6cf` and running the modified tests.