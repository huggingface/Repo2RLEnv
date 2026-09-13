"""Publish a labeled copy without changing the task used by a saved trial."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.labels import label_from_quality, read_evaluation, write_labeled_copy
from repo2rlenv.quality.loop.artifacts import digest
from repo2rlenv.quality.loop.models import LoopResult


def publish_label(directory: Path) -> dict:
    """Retain the quality result even if publication fails, and report that failure.

    The result digest selects an immutable export. Repeating publication checks
    its current task and evidence rather than trusting the previous receipt.
    Original Harbor checksums and LoopResult.task_path are never rewritten.
    """
    result_path = directory / "result.json"
    receipt = {"quality_result": str(result_path.resolve()), "state": "failed"}
    try:
        result = LoopResult.model_validate_json(result_path.read_text())
        source = Path(result.task_path)
        result_hash = digest(result_path)
        label = label_from_quality(source, result_path)
        destination = directory / "labeled" / result_hash / source.name
        if destination.exists():
            saved = read_evaluation(destination)
            ignored = {"checked_at", "source_task_path", "source_task_toml_sha256"}
            if (
                saved.model_dump(exclude=ignored) != label.model_dump(exclude=ignored)
                or saved.source_task_path != str(source.absolute())
                or saved.source_task_toml_sha256 != digest(source / "task.toml")
            ):
                raise ValueError("Existing labeled copy disagrees with its original evidence")
        else:
            write_labeled_copy(source, destination, label)
        receipt.update(
            state="completed",
            task_path=str(destination.resolve()),
            original_task_path=str(source.resolve()),
            quality_result_sha256=result_hash,
            bundle_hash=result.bundle_hash,
            status=label.status,
        )
    except (OSError, ValueError, KeyError, IndexError) as exc:
        receipt.update(error_type=type(exc).__name__, error=str(exc))
    save_record(directory / "labeled-task.json", receipt)
    return receipt
