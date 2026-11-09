"""qa: a question whose answer the agent computed from public Hugging Face data.

Claude proposes questions grounded in one tool output. Kept only if the gold answer appears
verbatim in that output, the question doesn't contain it, every input file resolves to the
dataset commit current at the trajectory's date, and in Docker the reference solution scores
1.0 while a wrong or missing answer scores 0.0 (deterministic grader, no LLM judge).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..llm import ask
from ..task import AGENT_TOOLS, Family, Reject, Trial, task_toml
from ..trajectory import Trajectory

GRADER = Path(__file__).resolve().parents[1] / "assets" / "grader.py"
INPUT_DIR = "/home/user/input"

SYSTEM_PROMPT = """You turn recorded agent trajectories into data-analysis RL tasks. Each task stages the
input files in /home/user/input, asks one question, and grades a single value by exact/numeric match.

Propose 1-3 tasks. Each must satisfy ALL of these:
- The gold answer appears verbatim in one tool call's output (cite step_id, call_id, an exact quote).
- Answerable from the staged files alone: no internet, no Hub metadata (downloads, likes, sizes).
- Unambiguous: spell out definitions that matter (split, empty vs NULL, rounding). No facts that
  depend on the original agent's own heuristics (custom regexes, keyword lists).
- The question does not contain or hint at the answer.
- Inputs are public Hugging Face dataset files at the repo/revision/path the agent used; the
  '/api/datasets/<id>/parquet/<config>/<split>/0.parquet' endpoint is revision 'refs/convert/parquet',
  path '<config>/<split>/0000.parquet'.
- reference_solution reads os.environ['INPUT_DIR'] with pandas or duckdb and prints only the answer.
Return no tasks if the trajectory has no usable grounded fact."""

# Pinned: an unpinned image drifts (a newer duckdb broke reference solutions written against 1.x).
DOCKERFILE = f"""FROM python:3.12-slim
{AGENT_TOOLS}
RUN pip install --no-cache-dir pandas==2.2.3 numpy==2.1.3 scipy==1.14.1 pyarrow==18.1.0 duckdb==1.1.3 huggingface_hub==0.30.2
COPY pull_inputs.py inputs.json /opt/
WORKDIR /workdir
"""
PULL_INPUTS = '''"""Stage the pinned input files into /home/user/input (run by the healthcheck, before the agent)."""
import json
import shutil
from pathlib import Path
from huggingface_hub import hf_hub_download

destination = Path("/home/user/input")
destination.mkdir(parents=True, exist_ok=True)
for file in json.loads(Path("/opt/inputs.json").read_text()):
    if not (destination / file["local_name"]).exists():
        local = hf_hub_download(file["repo_id"], file["path_in_repo"], repo_type="dataset", revision=file["commit"])
        shutil.copyfile(local, destination / file["local_name"])
'''
TEST_SH = """#!/usr/bin/env bash
mkdir -p /logs/verifier
[ -s /workdir/answer.txt ] || { echo 0.0 > /logs/verifier/reward.txt; exit 0; }
python3 /tests/grader.py < /workdir/answer.txt > /logs/verifier/reward.txt
"""
SOLVE_SH = f"""#!/usr/bin/env bash
# The reference solution, which reproduced the gold answer on the pinned inputs.
INPUT_DIR={INPUT_DIR} python3 /solution/solve.py | tail -n 1 | tr -d '\\n' > /workdir/answer.txt
"""
INSTRUCTION = """You are a data-analysis agent working in a sandbox. Use your code-execution tool to inspect the files and compute the answer.

Files (in /home/user/input, no subfolders):
{files}

Installed Python packages (use python3): pandas, numpy, scipy, pyarrow, duckdb. There is no duckdb command-line tool.

Question:
{question}

Answer with a single clean value: a bare number (no commas or units), a short label, yes/no, or a comma-separated list. Keep decimal precision.

Write only that value to /workdir/answer.txt (e.g. `echo -n "<value>" > /workdir/answer.txt`), then stop."""


class InputFile(BaseModel):
    repo_id: str
    revision: str
    path_in_repo: str
    local_name: str


class ProposedQuestion(BaseModel):
    question: str
    gold_answer: str
    answer_kind: Literal["number", "label", "list", "yes_no"]
    evidence_step_id: int
    evidence_call_id: str
    evidence_quote: str
    inputs: list[InputFile]
    reference_solution: str


class ProposedQuestions(BaseModel):
    tasks: list[ProposedQuestion]


@dataclass
class QACandidate:
    trajectory: Trajectory
    proposal: ProposedQuestion
    pinned_inputs: list[dict] = field(default_factory=list)  # input + "commit" + "downloaded_path"


def normalize(text: str) -> str:
    return " ".join(text.replace(",", "").lower().split())


def render_for_llm(trajectory: Trajectory, max_output_chars: int = 6000) -> str:
    sections = []
    for step in trajectory.steps:
        if step["source"] == "user":
            sections.append(f"### step {step['step_id']} USER\n{step.get('message', '')}")
        elif step["source"] == "agent":
            outputs = {
                r["source_call_id"]: r["content"]
                for r in (step.get("observation") or {}).get("results", [])
            }
            sections.append(f"### step {step['step_id']} AGENT\n{step.get('message', '')}")
            for call in step.get("tool_calls", []):
                sections.append(
                    f"--- call {call['tool_call_id']} {call['function_name']}\n{json.dumps(call['arguments'])[:3000]}\n"
                    f"--- output\n{outputs.get(call['tool_call_id'], '')[:max_output_chars]}"
                )
    return "\n\n".join(sections)


def cited_output(trajectory: Trajectory, step_id: int, call_id: str) -> str:
    step = next((s for s in trajectory.steps if s["step_id"] == step_id), {})
    results = (step.get("observation") or {}).get("results", [])
    return next((r["content"] for r in results if r["source_call_id"] == call_id), "")


def pin_to_commit_at(input_file: InputFile, when: datetime) -> dict:
    """Resolve an input to the last commit on its revision at `when`, and download it."""
    from huggingface_hub import HfApi, hf_hub_download

    commits = HfApi().list_repo_commits(
        input_file.repo_id, repo_type="dataset", revision=input_file.revision
    )
    commit = ([c for c in commits if c.created_at <= when] or commits)[0].commit_id  # newest first
    downloaded = hf_hub_download(
        input_file.repo_id, input_file.path_in_repo, repo_type="dataset", revision=commit
    )
    return {**input_file.model_dump(), "commit": commit, "downloaded_path": downloaded}


class QA(Family):
    name = "qa"

    def find_candidates(self, trajectory: Trajectory) -> list[QACandidate]:
        proposals = ask(
            SYSTEM_PROMPT,
            f"<trajectory>\n{render_for_llm(trajectory)}\n</trajectory>",
            ProposedQuestions,
        )
        return [
            QACandidate(trajectory, proposal) for proposal in (proposals.tasks if proposals else [])
        ]

    def task_id(self, c: QACandidate) -> str:
        return hashlib.sha256(f"{c.trajectory.source}|{c.proposal.question}".encode()).hexdigest()[
            :12
        ]

    def prepare(self, c: QACandidate) -> None:
        p = c.proposal
        output = cited_output(c.trajectory, p.evidence_step_id, p.evidence_call_id)
        answer_parts = p.gold_answer.split(",") if p.answer_kind == "list" else [p.gold_answer]
        if p.evidence_quote not in output or not all(
            normalize(part) in normalize(p.evidence_quote) for part in answer_parts
        ):
            raise Reject("answer_not_in_cited_output")
        if re.search(
            rf"(?<![\w.]){re.escape(normalize(p.gold_answer))}(?![\w.])", normalize(p.question)
        ):
            raise Reject("answer_in_question")
        first_timestamp = next(
            (s["timestamp"] for s in c.trajectory.steps if s.get("timestamp")),
            "2100-01-01T00:00:00Z",
        )
        when = datetime.fromisoformat(first_timestamp.replace("Z", "+00:00"))
        try:
            c.pinned_inputs = [pin_to_commit_at(input_file, when) for input_file in p.inputs]
        except Exception as error:
            raise Reject("input_not_resolvable", type(error).__name__) from error

    def environment_files(self, c: QACandidate) -> dict[str, str]:
        inputs_json = [
            {key: f[key] for key in ("repo_id", "path_in_repo", "commit", "local_name")}
            for f in c.pinned_inputs
        ]
        return {
            "Dockerfile": DOCKERFILE,
            "pull_inputs.py": PULL_INPUTS,
            "inputs.json": json.dumps(inputs_json, indent=1),
        }

    def image_tag(self, c: QACandidate) -> str:
        return "repo2rlenv-trajectory-qa"  # same image for every qa task; inputs are pulled at start, not baked in

    def verifier_env(self, c: QACandidate) -> dict[str, str]:
        reward_mode = "list" if c.proposal.answer_kind == "list" else "flexible"
        return {
            "EXPECTED_ANSWER": c.proposal.gold_answer,
            "QUESTION": c.proposal.question,
            "REWARD_MODE": reward_mode,
            "ATOL": "0.0",
            "RTOL": "0.0",
        }

    def task_files(self, c: QACandidate) -> dict[str, str]:
        p = c.proposal
        return {
            "instruction.md": INSTRUCTION.format(
                files="\n".join(f"- {f['local_name']}" for f in c.pinned_inputs),
                question=p.question,
            ),
            "tests/test.sh": TEST_SH,
            "tests/grader.py": GRADER.read_text(),
            "solution/solve.py": p.reference_solution,
            "solution/solve.sh": SOLVE_SH,
            "task.toml": task_toml(
                self.name,
                self.task_id(c),
                p.question,
                {
                    "source_trace": c.trajectory.source,
                    "source_model": c.trajectory.model,
                    "gold_answer": p.gold_answer,
                    "answer_kind": p.answer_kind,
                    "evidence_step_id": p.evidence_step_id,
                    "evidence_call_id": p.evidence_call_id,
                    "input_commits": [f["commit"] for f in c.pinned_inputs],
                },
                verifier_env=self.verifier_env(c),
                allow_internet=True,
                healthcheck_command=f'python3 /opt/pull_inputs.py && [ -n "$(ls {INPUT_DIR})" ]',
            ),
        }

    def trials(self, c: QACandidate, scratch_dir: Path) -> tuple[list[Trial], dict]:
        staged_inputs = (
            scratch_dir / "inputs"
        )  # stands in for the healthcheck download (trials run offline)
        staged_inputs.mkdir()
        for f in c.pinned_inputs:
            shutil.copyfile(f["downloaded_path"], staged_inputs / f["local_name"])
        wrong_answer = (
            "-987654321" if c.proposal.answer_kind == "number" else "certainly not the answer"
        )
        return [
            Trial("reference_solution", "mkdir -p /workdir && bash /solution/solve.sh", "1.0"),
            Trial(
                "wrong_answer",
                f"mkdir -p /workdir && echo -n '{wrong_answer}' > /workdir/answer.txt",
                "0.0",
            ),
            Trial("no_answer", "mkdir -p /workdir", "0.0"),
        ], {staged_inputs: f"{INPUT_DIR}:ro"}

    def summary(self, c: QACandidate) -> dict:
        return {"question": c.proposal.question, "gold_answer": c.proposal.gold_answer}
