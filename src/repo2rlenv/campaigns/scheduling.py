"""Task-count allocation for parallel campaign batches, separate from spend limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActiveBatch:
    recipe: str
    source: str
    target: int
    exported_ids: frozenset[str]


def available_task_slots(
    recipe: str,
    source: str,
    inventory: dict[str, str],
    active: list[ActiveBatch],
    *,
    target: int = 100,
    source_target: int = 25,
) -> int:
    """Reserve unfinished batches against one inventory snapshot before dispatch.

    A run receipt can advance after the inventory was read. Only exports visible
    in that inventory reduce its outstanding allocation; otherwise a racing
    export could free a slot before being counted toward the collection target.
    The caller must serialize allocation and dispatch across campaign workers.
    """
    if target < 1 or source_target < 1 or any(batch.target < 1 for batch in active):
        raise ValueError("Task allocation targets must be positive")
    relevant = [batch for batch in active if batch.recipe == recipe]
    outstanding = sum(
        max(0, batch.target - len(batch.exported_ids.intersection(inventory))) for batch in relevant
    )
    source_outstanding = sum(
        max(0, batch.target - len(batch.exported_ids.intersection(inventory)))
        for batch in relevant
        if batch.source == source
    )
    return max(
        0,
        min(
            target - len(inventory) - outstanding,
            source_target
            - sum(value == source for value in inventory.values())
            - source_outstanding,
        ),
    )
