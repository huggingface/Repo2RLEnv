"""Private assertions. Submitted Python is never imported into this interpreter."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, "/tests")
from isolated_call import WorkerFailure, call
from submission_guard import validate_submission


def tensor(value):
    return {"$tensor": value}


def main():
    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "reward.txt").write_text("0\n")
    validate_submission(
        Path("/workspace"), {"submitted_files": ["src/accelerate/utils/operations.py"]}
    )
    # Challenge values are private until each call. No expected data crosses the boundary.
    token = secrets.token_hex(12)
    number = secrets.randbelow(10000)
    a = [[number, 2], [3, 4]]
    b = [[5, 6], [7, 8]]
    pairs = []
    for scalar in (token, True, number, None, 1.5):
        pairs.append(({"batches": [scalar]}, {"value": scalar}))
    pairs += [
        ({"batches": ["a", "b"]}, {"error": "TypeError"}),
        ({"batches": [tensor(a), tensor(b)]}, {"value": tensor(a + b)}),
        ({"batches": [tensor(a)]}, {"value": tensor(a)}),
        (
            {"batches": [tensor(a), tensor(b)], "dim": 1},
            {"value": tensor([a[0] + b[0], a[1] + b[1]])},
        ),
        ({"batches": [{"t": tensor(a), "s": token}]}, {"value": {"t": tensor(a), "s": token}}),
        (
            {"batches": [{"t": tensor(a), "s": "a"}, {"t": tensor(b), "s": "b"}]},
            {"error": "TypeError"},
        ),
        ({"batches": [[tensor(a)], [tensor(b)]]}, {"value": [tensor(a + b)]}),
        ({"batches": [{"$tuple": [tensor(a), None]}]}, {"value": {"$tuple": [tensor(a), None]}}),
        (
            {"mode": "loader", "length": 10, "metadata": token},
            {
                "value": [
                    {"tensor": tensor(list(range(i, min(i + 4, 10)))), "metadata": token}
                    for i in (0, 4, 8)
                ]
            },
        ),
    ]
    rows = []
    for index, (request, expected) in enumerate(pairs):
        try:
            value = call(
                ["/usr/local/bin/python", "-I", "/opt/value_worker.py"], request, timeout=15
            )
            # Serialized exact data compares booleans/types too; never trusts a 'passed' key.
            passed = json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)
            rows.append({"case": index, "passed": passed})
        except WorkerFailure as exc:
            rows.append({"case": index, "passed": False, "error": str(exc)})
            break
    passed = len(rows) == len(pairs) and all(row["passed"] for row in rows)
    (logs / "result.json").write_text(json.dumps({"passed": passed, "checks": rows}))
    (logs / "reward.txt").write_text("1\n" if passed else "0\n")


if __name__ == "__main__":
    main()
