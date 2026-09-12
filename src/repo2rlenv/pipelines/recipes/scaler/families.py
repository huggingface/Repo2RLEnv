"""Difficulty scaling and native generator result parsing without code execution."""

from __future__ import annotations

import ast
import hashlib
import json
import math
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
    return data, hashlib.sha256(payload).hexdigest()


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
    return (
        text + r"Write your final answer to `/workspace/answer.txt`, using \boxed{answer}." + "\n"
    )
