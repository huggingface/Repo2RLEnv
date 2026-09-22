"""Vitest runs must survive generation-time and standalone parsing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.log_parsers.jest_parser import parse_jest
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier


@pytest.fixture(params=[parse_jest, verifier.parse_jest], ids=["canonical", "standalone"])
def parser(request):
    return request.param


# Real `npx vitest run --reporter=verbose` output (vitest 5.0.1, colored because
# TERM is unset) for a fix to `div`: before it, with the gold patch, and with an
# agent patch that also fixes the unrelated `rounds half up` test.
_BASE = (
    "\n"
    "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m \x1b[36mv5.0.1 \x1b[39m\x1b[90m/repo\x1b[39m\n"
    "\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22madds\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[31m×\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrejects division by zero\x1b[32m 3\x1b[2mms\x1b[22m\x1b[39m\n"
    "\x1b[31m   → expected [Function] to throw an error\x1b[39m\n"
    " \x1b[31m×\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    "\x1b[31m   → expected 2 to be 3 // Object.is equality\x1b[39m\n"
    "\n"
    "\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\x1b[1m\x1b[41m Failed Tests 2 \x1b[49m\x1b[22m\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\n"
    "\n"
    "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrejects division by zero\n"
    "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected [Function] to throw an error\x1b[39m\n"
    "\n"
    "\x1b[32m- Expected:\x1b[39m\n"
    "null\n"
    "\n"
    "\x1b[31m+ Received:\x1b[39m\n"
    "undefined\n"
    "\n"
    "\x1b[36m \x1b[2m❯\x1b[22m math.test.ts:\x1b[2m5:66\x1b[22m\x1b[39m\n"
    '    \x1b[90m  3|\x1b[39m describe("math", () => {\n'
    '    \x1b[90m  4|\x1b[39m   it("adds", () => { expect(add(1, 2)).toBe(3); });\n'
    '    \x1b[90m  5|\x1b[39m   it("rejects division by zero", () => { expect(() => div(1, 0)).toThr…\n'
    "    \x1b[90m   |\x1b[39m                                                                  \x1b[31m^\x1b[39m\n"
    '    \x1b[90m  6|\x1b[39m   it("rounds half up", () => { expect(round(2.5)).toBe(3); });\n'
    "    \x1b[90m  7|\x1b[39m });\n"
    "\n"
    "\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/2]⎯\x1b[22m\x1b[39m\n"
    "\n"
    "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\n"
    "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected 2 to be 3 // Object.is equality\x1b[39m\n"
    "\n"
    "\x1b[32m- Expected\x1b[39m\n"
    "\x1b[31m+ Received\x1b[39m\n"
    "\n"
    "\x1b[32m- 3\x1b[39m\n"
    "\x1b[31m+ 2\x1b[39m\n"
    "\n"
    "\x1b[36m \x1b[2m❯\x1b[22m math.test.ts:\x1b[2m6:51\x1b[22m\x1b[39m\n"
    '    \x1b[90m  4|\x1b[39m   it("adds", () => { expect(add(1, 2)).toBe(3); });\n'
    '    \x1b[90m  5|\x1b[39m   it("rejects division by zero", () => { expect(() => div(1, 0)).toThr…\n'
    '    \x1b[90m  6|\x1b[39m   it("rounds half up", () => { expect(round(2.5)).toBe(3); });\n'
    "    \x1b[90m   |\x1b[39m                                                   \x1b[31m^\x1b[39m\n"
    "    \x1b[90m  7|\x1b[39m });\n"
    "    \x1b[90m  8|\x1b[39m\n"
    "\n"
    "\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[2/2]⎯\x1b[22m\x1b[39m\n"
    "\n"
    "\n"
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m2 failed\x1b[39m\x1b[22m\x1b[2m | \x1b[22m\x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m\x1b[90m (3)\x1b[39m\n"
    "\x1b[2m   Start at \x1b[22m 01:51:08\n"
    "\x1b[2m   Duration \x1b[22m 119ms\x1b[2m (transform 59%, import 18%, tests 15%, worker 7%)\x1b[22m\n"
    "\n"
)

_GOLD = (
    "\n"
    "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m \x1b[36mv5.0.1 \x1b[39m\x1b[90m/repo\x1b[39m\n"
    "\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22madds\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrejects division by zero\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[31m×\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\x1b[32m 3\x1b[2mms\x1b[22m\x1b[39m\n"
    "\x1b[31m   → expected 2 to be 3 // Object.is equality\x1b[39m\n"
    "\n"
    "\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\x1b[1m\x1b[41m Failed Tests 1 \x1b[49m\x1b[22m\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\n"
    "\n"
    "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\n"
    "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected 2 to be 3 // Object.is equality\x1b[39m\n"
    "\n"
    "\x1b[32m- Expected\x1b[39m\n"
    "\x1b[31m+ Received\x1b[39m\n"
    "\n"
    "\x1b[32m- 3\x1b[39m\n"
    "\x1b[31m+ 2\x1b[39m\n"
    "\n"
    "\x1b[36m \x1b[2m❯\x1b[22m math.test.ts:\x1b[2m6:51\x1b[22m\x1b[39m\n"
    '    \x1b[90m  4|\x1b[39m   it("adds", () => { expect(add(1, 2)).toBe(3); });\n'
    '    \x1b[90m  5|\x1b[39m   it("rejects division by zero", () => { expect(() => div(1, 0)).toThr…\n'
    '    \x1b[90m  6|\x1b[39m   it("rounds half up", () => { expect(round(2.5)).toBe(3); });\n'
    "    \x1b[90m   |\x1b[39m                                                   \x1b[31m^\x1b[39m\n"
    "    \x1b[90m  7|\x1b[39m });\n"
    "    \x1b[90m  8|\x1b[39m\n"
    "\n"
    "\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯\x1b[22m\x1b[39m\n"
    "\n"
    "\n"
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[2m | \x1b[22m\x1b[1m\x1b[32m2 passed\x1b[39m\x1b[22m\x1b[90m (3)\x1b[39m\n"
    "\x1b[2m   Start at \x1b[22m 01:51:10\n"
    "\x1b[2m   Duration \x1b[22m 95ms\x1b[2m (transform 50%, import 22%, tests 20%, worker 8%)\x1b[22m\n"
    "\n"
)

_AGENT = (
    "\n"
    "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m \x1b[36mv5.0.1 \x1b[39m\x1b[90m/repo\x1b[39m\n"
    "\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22madds\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrejects division by zero\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    "\n"
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m3 passed\x1b[39m\x1b[22m\x1b[90m (3)\x1b[39m\n"
    "\x1b[2m   Start at \x1b[22m 01:51:11\n"
    "\x1b[2m   Duration \x1b[22m 87ms\x1b[2m (transform 56%, import 25%, tests 9%, worker 9%)\x1b[22m\n"
    "\n"
)

# The same gold run through vitest's default reporter.
_GOLD_DEFAULT = (
    "\n"
    "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m \x1b[36mv5.0.1 \x1b[39m\x1b[90m/repo\x1b[39m\n"
    "\n"
    " \x1b[31m❯\x1b[39m math.test.ts \x1b[2m(\x1b[22m\x1b[2m3 tests\x1b[22m\x1b[2m | \x1b[22m\x1b[31m1 failed\x1b[39m\x1b[2m)\x1b[22m\x1b[32m 5\x1b[2mms\x1b[22m\x1b[39m\n"
    "   \x1b[31m❯\x1b[39m math \x1b[2m(3)\x1b[22m\n"
    "     \x1b[32m✓\x1b[39m adds\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    "     \x1b[32m✓\x1b[39m rejects division by zero\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    "\x1b[31m     \x1b[31m×\x1b[31m rounds half up\x1b[39m\x1b[32m 3\x1b[2mms\x1b[22m\x1b[39m\n"
    "\n"
    "\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\x1b[1m\x1b[41m Failed Tests 1 \x1b[49m\x1b[22m\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\n"
    "\n"
    "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m math.test.ts\x1b[2m > \x1b[22mmath\x1b[2m > \x1b[22mrounds half up\n"
    "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected 2 to be 3 // Object.is equality\x1b[39m\n"
    "\n"
    "\x1b[32m- Expected\x1b[39m\n"
    "\x1b[31m+ Received\x1b[39m\n"
    "\n"
    "\x1b[32m- 3\x1b[39m\n"
    "\x1b[31m+ 2\x1b[39m\n"
    "\n"
    "\x1b[36m \x1b[2m❯\x1b[22m math.test.ts:\x1b[2m6:51\x1b[22m\x1b[39m\n"
    '    \x1b[90m  4|\x1b[39m   it("adds", () => { expect(add(1, 2)).toBe(3); });\n'
    '    \x1b[90m  5|\x1b[39m   it("rejects division by zero", () => { expect(() => div(1, 0)).toThr…\n'
    '    \x1b[90m  6|\x1b[39m   it("rounds half up", () => { expect(round(2.5)).toBe(3); });\n'
    "    \x1b[90m   |\x1b[39m                                                   \x1b[31m^\x1b[39m\n"
    "    \x1b[90m  7|\x1b[39m });\n"
    "    \x1b[90m  8|\x1b[39m\n"
    "\n"
    "\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯\x1b[22m\x1b[39m\n"
    "\n"
    "\n"
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[2m | \x1b[22m\x1b[1m\x1b[32m2 passed\x1b[39m\x1b[22m\x1b[90m (3)\x1b[39m\n"
    "\x1b[2m   Start at \x1b[22m 01:51:10\n"
    "\x1b[2m   Duration \x1b[22m 95ms\x1b[2m (transform 50%, import 22%, tests 20%, worker 8%)\x1b[22m\n"
    "\n"
)

# Test lines of a vitest 5.0.1 run with two projects, with and without color.
_PROJECTS_COLOR = (
    "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m \x1b[36mv5.0.1 \x1b[39m\x1b[90m/repo\x1b[39m\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/other.test.ts\x1b[2m > \x1b[22mstandalone passes\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[43m node \x1b[49m\x1b[39m vt/other.test.ts\x1b[2m > \x1b[22mstandalone passes\x1b[32m 1\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22madd()\x1b[2m > \x1b[22madds two numbers\x1b[32m 2\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22madd()\x1b[2m > \x1b[22mhandles negatives (edge case)\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[31m×\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22madd()\x1b[2m > \x1b[22mbreaks on purpose\x1b[32m 4\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[2m\x1b[90m↓\x1b[39m\x1b[22m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22madd()\x1b[2m > \x1b[22mskipped test\n"
    " \x1b[2m\x1b[90m□\x1b[39m\x1b[22m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22madd()\x1b[2m > \x1b[22mtodo test\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mMath\x1b[2m > \x1b[22mis slow\x1b[33m 352\x1b[2mms\x1b[22m\x1b[39m\n"
    " \x1b[32m✓\x1b[39m \x1b[30m\x1b[42m unit \x1b[49m\x1b[39m vt/math.test.ts\x1b[2m > \x1b[22mTop level\x1b[2m > \x1b[22mreturns 200 (OK)\x1b[32m 0\x1b[2mms\x1b[22m\x1b[39m\n"
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m\x1b[2m | \x1b[22m\x1b[1m\x1b[32m2 passed\x1b[39m\x1b[22m\x1b[90m (3)\x1b[39m\n"
)

_PROJECTS_PLAIN = (
    " RUN  v5.0.1 /repo\n"
    " ✓ |unit| vt/other.test.ts > standalone passes 1ms\n"
    " ✓ |node| vt/other.test.ts > standalone passes 1ms\n"
    " ✓ |unit| vt/math.test.ts > Math > add() > adds two numbers 1ms\n"
    " ✓ |unit| vt/math.test.ts > Math > add() > handles negatives (edge case) 0ms\n"
    " × |unit| vt/math.test.ts > Math > add() > breaks on purpose 3ms\n"
    " ↓ |unit| vt/math.test.ts > Math > add() > skipped test\n"
    " □ |unit| vt/math.test.ts > Math > add() > todo test\n"
    " ✓ |unit| vt/math.test.ts > Math > is slow 352ms\n"
    " ✓ |unit| vt/math.test.ts > Top level > returns 200 (OK) 0ms\n"
    " Test Files  1 failed | 2 passed (3)\n"
)

# Test lines of vitest 3.2.7, which marks todo tests with ↓.
_V3_PLAIN = (
    " RUN  v3.2.7 /repo\n"
    " ✓ other.test.ts > standalone passes 1ms\n"
    " ✓ math.test.ts > Math > add() > adds two numbers 1ms\n"
    " ✓ math.test.ts > Math > add() > handles negatives (edge case) 0ms\n"
    " × math.test.ts > Math > add() > breaks on purpose 3ms\n"
    " ↓ math.test.ts > Math > add() > skipped test\n"
    " ↓ math.test.ts > Math > add() > todo test\n"
    " ✓ math.test.ts > Math > is slow 351ms\n"
    " ✓ math.test.ts > Top level > returns 200 (OK) 0ms\n"
    " Test Files  1 failed | 1 passed (2)\n"
)

# Real `npx jest --verbose` output (jest 30.5.0) with FORCE_COLOR=1.
_JEST_COLOR = (
    "\x1b[0m\x1b[7m\x1b[1m\x1b[31m FAIL \x1b[39m\x1b[22m\x1b[27m\x1b[0m \x1b[2mjt/\x1b[22m\x1b[1mmath.test.js\x1b[22m\n"
    "  Math\n"
    "    add()\n"
    "      \x1b[32m✓\x1b[39m \x1b[2madds two numbers (1 ms)\x1b[22m\n"
    "      \x1b[32m✓\x1b[39m \x1b[2mhandles negatives (edge case) (1 ms)\x1b[22m\n"
    "      \x1b[31m✕\x1b[39m \x1b[2mbreaks on purpose (1 ms)\x1b[22m\n"
    "      \x1b[33m○\x1b[39m \x1b[2mskipped skipped test\x1b[22m\n"
    "      \x1b[35m✎\x1b[39m \x1b[2mtodo todo test\x1b[22m\n"
    "  Top level\n"
    "    \x1b[32m✓\x1b[39m \x1b[2mreturns 200 (OK)\x1b[22m\n"
    "\n"
    "\x1b[1m\x1b[31m  \x1b[1m● \x1b[22m\x1b[1mMath › add() › breaks on purpose\x1b[39m\x1b[22m\n"
    "\n"
    "    \x1b[2mexpect(\x1b[22m\x1b[31mreceived\x1b[39m\x1b[2m).\x1b[22mtoBe\x1b[2m(\x1b[22m\x1b[32mexpected\x1b[39m\x1b[2m) // Object.is equality\x1b[22m\n"
    "\n"
    "    Expected: \x1b[32m3\x1b[39m\n"
    "    Received: \x1b[31m2\x1b[39m\n"
    "\n"
    '\x1b[2m    \x1b[0m \x1b[90m 3 |\x1b[39m     it(\x1b[32m"adds two numbers"\x1b[39m\x1b[33m,\x1b[39m () \x1b[33m=>\x1b[39m { expect(\x1b[35m1\x1b[39m \x1b[33m+\x1b[39m \x1b[35m1\x1b[39m)\x1b[33m.\x1b[39mtoBe(\x1b[35m2\x1b[39m)\x1b[33m;\x1b[39m })\x1b[33m;\x1b[39m\x1b[22m\n'
    '\x1b[2m     \x1b[90m 4 |\x1b[39m     it(\x1b[32m"handles negatives (edge case)"\x1b[39m\x1b[33m,\x1b[39m () \x1b[33m=>\x1b[39m { expect(\x1b[33m-\x1b[39m\x1b[35m1\x1b[39m \x1b[33m+\x1b[39m \x1b[33m-\x1b[39m\x1b[35m1\x1b[39m)\x1b[33m.\x1b[39mtoBe(\x1b[33m-\x1b[39m\x1b[35m2\x1b[39m)\x1b[33m;\x1b[39m })\x1b[33m;\x1b[39m\x1b[22m\n'
    '\x1b[2m    \x1b[31m\x1b[1m>\x1b[22m\x1b[2m\x1b[39m\x1b[90m 5 |\x1b[39m     it(\x1b[32m"breaks on purpose"\x1b[39m\x1b[33m,\x1b[39m () \x1b[33m=>\x1b[39m { expect(\x1b[35m1\x1b[39m \x1b[33m+\x1b[39m \x1b[35m1\x1b[39m)\x1b[33m.\x1b[39mtoBe(\x1b[35m3\x1b[39m)\x1b[33m;\x1b[39m })\x1b[33m;\x1b[39m\x1b[22m\n'
    "\x1b[2m     \x1b[90m   |\x1b[39m                                                   \x1b[31m\x1b[1m^\x1b[22m\x1b[2m\x1b[39m\x1b[22m\n"
    '\x1b[2m     \x1b[90m 6 |\x1b[39m     it\x1b[33m.\x1b[39mskip(\x1b[32m"skipped test"\x1b[39m\x1b[33m,\x1b[39m () \x1b[33m=>\x1b[39m {})\x1b[33m;\x1b[39m\x1b[22m\n'
    '\x1b[2m     \x1b[90m 7 |\x1b[39m     it\x1b[33m.\x1b[39mtodo(\x1b[32m"todo test"\x1b[39m)\x1b[33m;\x1b[39m\x1b[22m\n'
    "\x1b[2m     \x1b[90m 8 |\x1b[39m   })\x1b[33m;\x1b[39m\x1b[0m\x1b[22m\n"
    "\n"
    "\x1b[2m      \x1b[2mat Object.toBe (\x1b[22m\x1b[2m\x1b[0m\x1b[36mmath.test.js\x1b[39m\x1b[0m\x1b[2m:5:51)\x1b[22m\x1b[2m\x1b[22m\n"
    "\n"
    "\x1b[0m\x1b[7m\x1b[1m\x1b[32m PASS \x1b[39m\x1b[22m\x1b[27m\x1b[0m \x1b[2mjt/\x1b[22m\x1b[1mother.test.js\x1b[22m\n"
    "  \x1b[32m✓\x1b[39m \x1b[2mstandalone passes\x1b[22m\n"
    "\n"
    "\x1b[1mTest Suites: \x1b[22m\x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m, \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m, 2 total\n"
    "\x1b[1mTests:       \x1b[22m\x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m, \x1b[1m\x1b[33m1 skipped\x1b[39m\x1b[22m, \x1b[1m\x1b[35m1 todo\x1b[39m\x1b[22m, \x1b[1m\x1b[32m4 passed\x1b[39m\x1b[22m, 7 total\n"
    "\x1b[1mSnapshots:   \x1b[22m0 total\n"
    "\x1b[1mTime:\x1b[22m        0.214 s, estimated 1 s\n"
    "\x1b[2mRan all test suites\x1b[22m\x1b[2m.\x1b[22m\n"
)


def test_colored_verbose_runs_keep_every_test(parser):
    assert parser(_BASE) == {
        "math.test.ts > math > adds": "PASSED",
        "math.test.ts > math > rejects division by zero": "FAILED",
        "math.test.ts > math > rounds half up": "FAILED",
    }
    assert parser(_AGENT) == {
        "math.test.ts > math > adds": "PASSED",
        "math.test.ts > math > rejects division by zero": "PASSED",
        "math.test.ts > math > rounds half up": "PASSED",
    }


def test_project_names_are_part_of_the_identity_with_or_without_color(parser):
    expected = {
        "|unit| vt/other.test.ts > standalone passes": "PASSED",
        "|node| vt/other.test.ts > standalone passes": "PASSED",
        "|unit| vt/math.test.ts > Math > add() > adds two numbers": "PASSED",
        "|unit| vt/math.test.ts > Math > add() > handles negatives (edge case)": "PASSED",
        "|unit| vt/math.test.ts > Math > add() > breaks on purpose": "FAILED",
        "|unit| vt/math.test.ts > Math > add() > skipped test": "SKIPPED",
        "|unit| vt/math.test.ts > Math > add() > todo test": "SKIPPED",
        "|unit| vt/math.test.ts > Math > is slow": "PASSED",
        "|unit| vt/math.test.ts > Top level > returns 200 (OK)": "PASSED",
    }
    assert parser(_PROJECTS_COLOR) == parser(_PROJECTS_PLAIN) == expected


def test_vitest_3_verbose_format(parser):
    status = parser(_V3_PLAIN)
    assert status["math.test.ts > Math > add() > breaks on purpose"] == "FAILED"
    assert status["math.test.ts > Math > add() > todo test"] == "SKIPPED"
    assert status["math.test.ts > Top level > returns 200 (OK)"] == "PASSED"
    assert len(status) == 8


@pytest.mark.parametrize(
    "suffix",
    [
        " 12ms",
        " 0ms (retry x2)",
        " 12ms (repeat x3)",
        " 12ms (retry x1) (repeat x2) 30 MB heap used",
    ],
)
def test_run_suffixes_are_not_part_of_the_name(parser, suffix):
    log = f" RUN  v5.0.1 /repo\n ✓ src/a.test.ts > waits 5ms{suffix}\n"
    assert parser(log) == {"src/a.test.ts > waits 5ms": "PASSED"}


def test_skipped_tests_keep_their_title_and_drop_the_note(parser):
    log = " RUN  v5.0.1 /repo\n ↓ src/a.test.ts > waits 5ms\n □ src/a.test.ts > later [not ready]\n"
    assert parser(log) == {
        "src/a.test.ts > waits 5ms": "SKIPPED",
        "src/a.test.ts > later": "SKIPPED",
    }


def test_default_reporter_lines_are_not_recorded(parser):
    # Once a patch fixes the rest of a file, the default reporter collapses it
    # to `✓ math.test.ts (3 tests)`, so its per-test lines can't be graded.
    assert parser(_GOLD_DEFAULT) == {}


def test_colored_jest_output_matches_plain_jest(parser):
    assert parser(_JEST_COLOR) == {
        "jt/other.test.js > standalone passes": "PASSED",
        "jt/math.test.js > Math > add() > adds two numbers": "PASSED",
        "jt/math.test.js > Math > add() > handles negatives (edge case)": "PASSED",
        "jt/math.test.js > Math > add() > breaks on purpose": "FAILED",
        "jt/math.test.js > Math > add() > skipped skipped test": "SKIPPED",
        "jt/math.test.js > Top level > returns 200 (OK)": "PASSED",
    }


def test_long_lines_do_not_stall_parsing():
    # Isolate the timeout so a backtracking regression cannot hang the suite.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from repo2rlenv.log_parsers.jest_parser import parse_jest\n"
            "from repo2rlenv.pipelines._pr_runtime_verifier import parse_jest as standalone\n"
            "noise = [' ✓ a > b' + ' [' * 100000, ' ✓ a > b' + ' 1' * 100000,\n"
            "         ' ✓ a > ' + ' (retry x1)' * 20000 + 'x', ' ✓ ' + 'a > ' * 50000 + 'x',\n"
            "         ' ✓ a > ' + ' 1 MB heap used' * 20000 + 'x', '\\x1b[' * 100000]\n"
            "log = ' RUN  v5.0.1 /repo\\n' + '\\n'.join(noise) + '\\n ✓ a.test.ts > b 1ms\\n'\n"
            "for parser in (parse_jest, standalone):\n"
            "    assert parser(log)['a.test.ts > b'] == 'PASSED'\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_vitest_fix_grades_through_the_standalone_verifier(tmp_path: Path):
    pre, gold = parse_jest(_BASE), parse_jest(_GOLD)
    f2p = [
        name for name, status in pre.items() if status == "FAILED" and gold.get(name) == "PASSED"
    ]
    p2p = [
        name for name, status in pre.items() if status == "PASSED" and gold.get(name) == "PASSED"
    ]
    assert f2p == ["math.test.ts > math > rejects division by zero"]
    assert p2p == ["math.test.ts > math > adds"]

    # Run the verifier in isolation, with no installed repo2rlenv imports.
    standalone = tmp_path / "verifier.py"
    standalone.write_text(Path(verifier.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "out.log").write_text(_AGENT, encoding="utf-8")
    (tmp_path / "f2p.json").write_text(json.dumps(f2p), encoding="utf-8")
    (tmp_path / "p2p.json").write_text(json.dumps(p2p), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(standalone),
            "--log",
            "out.log",
            "--f2p",
            "f2p.json",
            "--p2p",
            "p2p.json",
            "--test-cmds",
            "npx vitest run --reporter=verbose",
            "--exit-code",
            "0",
            "--out-dir",
            "rewards",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    details = json.loads((tmp_path / "rewards/reward-details.json").read_text())
    assert details["runner"] == "jest"
    assert details["reward"] == 1.0
    assert details["resolved"] is True
