"""Stage contracts for the FrontierSmith adaptation; no generated code runs here."""

from __future__ import annotations

import ast
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Seed(Artifact):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,60}$")
    title: str
    problem: str = Field(min_length=40, max_length=12000)
    source: str
    license: str


class Design(Artifact):
    title: str
    mutation: Literal["objective", "output_constraints", "input_constraints"]
    instruction: str = Field(min_length=200, max_length=12000)
    objective: str
    feasibility: str
    score_formula: str
    baseline_strategy: str
    why_open_ended: str


class Review(Artifact):
    approved: bool
    issues: list[str]
    rationale: str

    @model_validator(mode="after")
    def consistent(self):
        if self.approved == bool(self.issues):
            raise ValueError("Approval requires no issues; rejection requires concrete issues")
        return self


class Program(Artifact):
    strategy: str
    code: str = Field(min_length=30, max_length=30000)

    @model_validator(mode="after")
    def syntax(self):
        ast.parse(self.code)
        return self


class Infrastructure(Artifact):
    generator: str = Field(min_length=60, max_length=24000)
    scorer: str = Field(min_length=60, max_length=24000)
    feasibility: str = Field(default="", max_length=24000)

    @model_validator(mode="after")
    def syntax(self):
        for source, entry in ((self.generator, "generate"), (self.scorer, "score")):
            tree = ast.parse(source)
            if entry not in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}:
                raise ValueError(f"Infrastructure must define {entry}")
        if self.feasibility:
            tree = ast.parse(self.feasibility)
            if "is_feasible" not in {
                node.name for node in tree.body if isinstance(node, ast.FunctionDef)
            }:
                raise ValueError("Feasibility validator must define is_feasible")
        return self


class Divergence(Artifact):
    distinct_pairs: list[bool]
    rationale: str
