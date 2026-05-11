# Issue

**Title:** Show default string in prompt

## Description

This PR build-up on #2837 (original contributor deleted its fork) and #3165 (revival based on AI slop) to use the value of `show_default` in prompt if the latter is a string. Which aligns the display with the help screen.

Also adds a lot of unit tests for edge-cases for default prompt displaying.

This .

## Task

Modify the repository so that the issue described above is resolved. The task's test suite verifies your patch by applying it on top of the base commit `8c95c73bd5ef` and running the modified tests.