"""The reward half of the Repo2RLEnv verifier contract.

Every runtime verifier we emit ends by writing its verdict into
`/logs/verifier/`, never through its exit code (`test.sh` ends with `exit 0` on
purpose). Two files matter:

    reward.txt           the scalar training signal
    reward-details.json  the diagnostic breakdown (F2P/P2P counts, resolved,
                         parse_status; or per-component diff-similarity scores)

Harbor also allows a flat numeric `reward.json`, so we read that first and fall
back to `reward.txt` — the same precedence Harbor itself applies.

The reward is produced *inside* the sandbox by the task's own verifier and only
forwarded from here. When no reward file exists the reward is `None`, never
`0.0`: a fabricated zero is indistinguishable from a genuine failure and would
quietly poison a training run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Reads a file name inside `/logs/verifier/`, returning None when absent.
#: Taking a reader rather than a path keeps this identical for a local
#: directory and for files inside a container.
LogReader = Callable[[str], "str | None"]

REWARD_JSON = "reward.json"
REWARD_TXT = "reward.txt"
REWARD_DETAILS_JSON = "reward-details.json"

#: Key treated as the scalar when `reward.json` carries several metrics.
PRIMARY_METRIC = "reward"


@dataclass(frozen=True)
class RewardReport:
    """What the verifier left behind in `/logs/verifier/`.

    Attributes:
        value: The scalar reward, or None when the verifier wrote no reward file.
        source: Which artifact the scalar came from — reward.json / reward.txt / missing.
        metrics: Every flat numeric metric from `reward.json`.
        details: Parsed `reward-details.json`, when present.
        errors: Problems encountered while reading the artifacts.
    """

    value: float | None = None
    source: str = "missing"
    metrics: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def graded(self) -> bool:
        """Whether the verifier produced a usable reward."""
        return self.value is not None

    def as_info(self) -> dict[str, Any]:
        """A JSON-serializable digest for `Observation.info`."""
        info: dict[str, Any] = {"reward_source": self.source}
        if self.metrics:
            info["reward_metrics"] = self.metrics
        if self.details:
            info["reward_details"] = self.details
        if self.errors:
            info["reward_errors"] = self.errors
        return info


def read_reward(read_text: LogReader) -> RewardReport:
    """Recover the verdict from a verifier log directory."""
    errors: list[str] = []
    details = _load_details(read_text, errors)
    metrics, scalar = _load_metrics(read_text, errors)

    if scalar is not None:
        return RewardReport(scalar, REWARD_JSON, metrics, details, errors)

    scalar = _load_scalar_txt(read_text, errors)
    if scalar is not None:
        return RewardReport(scalar, REWARD_TXT, metrics, details, errors)

    return RewardReport(None, "missing", metrics, details, errors)


def _load_metrics(read_text: LogReader, errors: list[str]) -> tuple[dict[str, float], float | None]:
    raw = _read(read_text, REWARD_JSON, errors)
    if raw is None:
        return {}, None

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        errors.append(f"{REWARD_JSON} is not valid JSON: {exc}")
        return {}, None
    if not isinstance(payload, dict):
        errors.append(f"{REWARD_JSON} must be a JSON object, got {type(payload).__name__}")
        return {}, None

    metrics = {
        str(key): float(val)
        for key, val in payload.items()
        # bool is an int subclass; the schema here is numeric-only.
        if isinstance(val, (int, float)) and not isinstance(val, bool)
    }
    if PRIMARY_METRIC in metrics:
        return metrics, metrics[PRIMARY_METRIC]
    if len(metrics) == 1:
        return metrics, next(iter(metrics.values()))
    if metrics:
        # Several metrics and no agreed primary: fall through to reward.txt
        # rather than picking one arbitrarily.
        errors.append(
            f"{REWARD_JSON} has no {PRIMARY_METRIC!r} key among {sorted(metrics)}; "
            f"falling back to {REWARD_TXT}"
        )
    return metrics, None


def _load_scalar_txt(read_text: LogReader, errors: list[str]) -> float | None:
    raw = _read(read_text, REWARD_TXT, errors)
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        errors.append(f"{REWARD_TXT} is not a number: {raw.strip()[:80]!r}")
        return None


def _load_details(read_text: LogReader, errors: list[str]) -> dict[str, Any]:
    raw = _read(read_text, REWARD_DETAILS_JSON, errors)
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        errors.append(f"{REWARD_DETAILS_JSON} is not valid JSON: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _read(read_text: LogReader, name: str, errors: list[str]) -> str | None:
    try:
        return read_text(name)
    except Exception as exc:  # pragma: no cover - backend-specific failures
        errors.append(f"could not read {name}: {exc}")
        return None
