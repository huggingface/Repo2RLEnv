"""Standalone answer-file adapter for SCALER's active math-verify reward route."""

from __future__ import annotations

import json
import stat
from pathlib import Path


def main() -> None:
    from math_verify.errors import TimeoutException
    from math_verify.metric import math_metric
    from math_verify.parser import ExprExtractionConfig, LatexExtractionConfig

    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    reward = logs / "reward.txt"
    reward.write_text("-1\n")
    path = Path("/workspace/answer.txt")
    if (
        not path.exists()
        or path.is_symlink()
        or not stat.S_ISREG(path.stat().st_mode)
        or path.stat().st_size > 65536
    ):
        return
    verify = math_metric(
        gold_extraction_target=(LatexExtractionConfig(),),
        pred_extraction_target=(ExprExtractionConfig(), LatexExtractionConfig()),
    )
    try:
        answer = path.read_text()
        reference = json.loads(Path("/tests/reference.json").read_text())
        accuracy, _ = verify([reference], [answer])
    except (Exception, TimeoutException) as exc:
        (logs / "result.json").write_text(json.dumps({"score": -1, "error": type(exc).__name__}))
        return
    score = 1 if accuracy else -1
    (logs / "result.json").write_text(json.dumps({"score": score, "accuracy": accuracy}))
    reward.write_text(str(score) + "\n")


if __name__ == "__main__":
    main()
