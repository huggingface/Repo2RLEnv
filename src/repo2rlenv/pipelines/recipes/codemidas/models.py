"""Artifacts bind behavioral requirements, implementation boundaries and tests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Symbol(Artifact):
    path: str
    qualified_name: str


class Requirement(Artifact):
    id: str = Field(pattern=r"^R[1-9][0-9]*$")
    behavior: str = Field(min_length=15, max_length=2000)


class Feature(Artifact):
    title: str = Field(min_length=10, max_length=140)
    instruction: str = Field(min_length=200, max_length=12000)
    symbols: list[Symbol] = Field(min_length=1, max_length=12)
    requirements: list[Requirement] = Field(min_length=3, max_length=20)
    rationale: str = Field(min_length=30, max_length=3000)

    def task_instruction(self) -> str:
        """The verifier's requirement map must never be a private task contract."""
        return (
            self.instruction.rstrip()
            + "\n\nRequired behavior:\n\n"
            + "\n".join("- " + requirement.behavior for requirement in self.requirements)
        )

    @model_validator(mode="after")
    def unique(self):
        ids = [requirement.id for requirement in self.requirements]
        symbols = [(symbol.path, symbol.qualified_name) for symbol in self.symbols]
        if len(ids) != len(set(ids)) or len(symbols) != len(set(symbols)):
            raise ValueError("Requirement IDs and selected symbols must be unique")
        return self


class AssertionMap(Artifact):
    test: str = Field(pattern=r"^test_[A-Za-z0-9_]+$")
    requirements: list[str] = Field(min_length=1)
    observation: str = Field(min_length=15, max_length=1500)


class Verifier(Artifact):
    test_code: str = Field(min_length=200, max_length=32000)
    assertions: list[AssertionMap] = Field(min_length=3, max_length=40)


class Review(Artifact):
    approved: bool
    issues: list[str]
    explanation: str = Field(min_length=30, max_length=5000)
    repair_target: Literal["tests", "contract"] = "tests"

    @model_validator(mode="after")
    def consistent(self):
        if self.approved and self.issues:
            raise ValueError("An approved review must have no unresolved issues")
        return self
