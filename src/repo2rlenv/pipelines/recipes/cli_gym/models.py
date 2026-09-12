"""Typed environment-inversion goals and reproducible filesystem changes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InversionGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=5, max_length=120)
    category: str
    selected_tests: list[str] = Field(min_length=1, max_length=50)
    description: str = Field(min_length=40, max_length=12000)
    expected_result: str
    recovery_strategy: str


class Inversion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destruction_shell: str = Field(min_length=20, max_length=12000)
    recovery_shell: str = Field(min_length=20, max_length=12000)
    explanation: str = Field(min_length=40, max_length=6000)

    @model_validator(mode="after")
    def bash_scripts(self):
        if any(
            not script.startswith("#!/bin/bash\n")
            for script in (self.destruction_shell, self.recovery_shell)
        ):
            raise ValueError("Inversion and recovery must start with a bash shebang")
        return self


class RepairInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=100, max_length=10000)
