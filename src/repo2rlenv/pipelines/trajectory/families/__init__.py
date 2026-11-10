"""Task families. Each one subclasses `task.Family`; add a new one here to make it available."""

from __future__ import annotations

from .build import Build
from .extend import Extend
from .qa import QA
from .repair import Repair

FAMILIES = {family.name: family for family in (Repair(), Build(), Extend(), QA())}
