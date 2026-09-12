"""Private pytest entry point with a bounded framework-tampering check.

The host still owns reward parsing. This guard detects replacement or in-place
editing of unittest assertions, a demonstrated attack against repository tests.
It does not claim to isolate arbitrary test and solution code in one interpreter.
"""

from __future__ import annotations

import sys
import unittest


def run(arguments: list[str]) -> int:
    import pytest

    # Capture before pytest imports any repository modules. Retain code objects
    # too: an attacker can mutate function.__code__ without replacing a method.
    original = {
        name: (value, getattr(value, "__code__", None))
        for name, value in vars(unittest.TestCase).items()
        if name.startswith("assert") and callable(value)
    }
    case = unittest.TestCase
    code = int(pytest.main(arguments))
    changed = []
    for name, (method, implementation) in original.items():
        current = getattr(case, name, None)
        if current is not method or getattr(current, "__code__", None) is not implementation:
            changed.append(name)
    if unittest.TestCase is not case or changed:
        sys.stderr.write("Verifier framework was modified: " + ", ".join(changed) + "\n")
        return 254
    return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
