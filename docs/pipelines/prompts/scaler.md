# SCALER: instruction construction

This recipe makes **zero LLM calls**. Its generator and reference are supplied programs executed remotely. The code below shows exactly how the concrete learner instruction is assembled; it is not a system prompt sent to a model. See the [walkthrough](../scaler.md).

## Instruction assembly and instance contract

### families.py

[Source: `src/repo2rlenv/pipelines/recipes/scaler/families.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/recipes/scaler/families.py) · SHA-256 `0b36f4d19d6103cc30d8454743a3ebe8da7f4a2b49f2f6bc96a223416512d93b`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read families.py</summary>

````python
"""Difficulty scaling and native generator result parsing without code execution."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path


def load_families(path: Path) -> tuple[dict, str]:
    payload = path.read_bytes()
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("Family input exceeds 32 MiB")
    data = json.loads(payload)
    if not isinstance(data, dict) or not data:
        raise ValueError("SCALER expects a native mapping of family names to definitions")
    for name, family in data.items():
        if not isinstance(name, str) or not isinstance(family, dict):
            raise ValueError("Invalid family definition")
        for key in (
            "name",
            "logic_description",
            "generate_testcase",
            "params",
            "difficulty_dict",
            "solutions",
        ):
            if not family.get(key):
                raise ValueError(f"Family {name!r} lacks {key}")
        solutions = family["solutions"]
        if len(solutions["solution"]) != len(solutions["language"]) or not any(
            lang in {2, 3} for lang in solutions["language"]
        ):
            raise ValueError("Family requires aligned Python/C++ reference programs")
        answer_contract(family)
    return data, hashlib.sha256(payload).hexdigest()


def answer_contract(family: dict) -> dict:
    """Keep answer semantics explicit; do not guess tolerances from prose."""
    kind = family.get("output_type")
    if kind is None:
        return {}  # Older family fixtures use the original math-verify route.
    if kind not in {"number", "string", "array"}:
        raise ValueError("Unsupported family output_type")
    contract = {"output_type": kind}
    tolerance = family.get("answer_tolerance", {})
    if not isinstance(tolerance, dict) or set(tolerance) - {"absolute", "relative"}:
        raise ValueError("Answer tolerance requires absolute/relative values")
    if tolerance and kind == "string":
        raise ValueError("String outputs cannot have numeric tolerances")
    for key, value in tolerance.items():
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number < 1:
            raise ValueError("Answer tolerance must be finite, nonnegative and below one")
        contract[key + "_tolerance"] = str(number)
    return contract


def scale_parameters(family: dict, difficulty: int) -> dict[str, int]:
    scale = family["difficulty_dict"][str(difficulty)]
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale < 0:
        raise ValueError("Difficulty scale must be finite and nonnegative")
    return {
        key: int(scale * value.get("base", 1.0) + value["min"])
        for key, value in family["params"].items()
        if key != "difficulty"
    }


def parse_testcase(text: str) -> tuple[str, dict]:
    """Retain the native first-comma parser and JSON/Python-literal fallback."""
    value = text.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    parts = value.split(",", 1)
    if len(parts) != 2:
        raise ValueError("Generator must emit a string and concrete-input dictionary")
    stdin, detail = parts[0].strip(), parts[1].strip()
    if len(stdin) >= 2 and stdin[0] in {"'", '"'} and stdin[-1] == stdin[0]:
        try:
            parsed = ast.literal_eval(stdin)
            if isinstance(parsed, str):
                stdin = parsed
        except (SyntaxError, ValueError):
            pass
    try:
        detail = json.loads(detail.replace("'", '"'))
    except json.JSONDecodeError:
        detail = ast.literal_eval(detail)
    if not isinstance(detail, dict):
        raise ValueError("Generator detail must be a dictionary")
    return stdin, detail


def instruction_for(family: dict, detail: dict) -> str:
    text = f"# {family['name']} Problem Description:\n{family['logic_description']}\n\n# Input Instance:\n{detail}\n\n# Instruction\n{family.get('instruction', '')}\n"
    text += "Treat the description as a single fully specified math/algorithm problem about the given concrete input, and ignore any mentions of writing a program, input/output formats, or multiple test cases. Reason step by step to compute the required answer for this instance.\n"
    if family.get("output_type") == "array":
        text += (
            r"Output all required numerical answers in \boxed{[]} as a one-dimensional array."
            + "\n"
        )
    contract = answer_contract(family)
    if family.get("output_type") == "string":
        text += "Preserve the complete output string, including any leading zeros.\n"
    if family.get("output_type") in {"number", "array"}:
        text += "Use decimal numbers (scientific notation is allowed).\n"
    if family.get("answer_tolerance"):
        text += (
            "A numeric answer is accepted when its absolute error is at most "
            f"{contract.get('absolute_tolerance', '0')} or its relative error is at most "
            f"{contract.get('relative_tolerance', '0')}.\n"
        )
    return (
        text + r"Write your final answer to `/workspace/answer.txt`, using \boxed{answer}." + "\n"
    )
````

</details>
