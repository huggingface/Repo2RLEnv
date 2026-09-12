"""Bounded evidence packs, explicit omissions and verifiable model citations."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

from repo2rlenv.quality.loop.artifacts import digest
from repo2rlenv.quality.loop.models import ReadRequest, Review, TrialRecord


class EvidenceContext:
    def __init__(self, task: Path, trials: list[TrialRecord], *, limit: int):
        self.task, self.limit = task, limit
        self.documents: dict[str, str] = {}
        self.inventory: list[dict] = []
        self.omitted: list[str] = []
        self._paths: dict[str, Path] = {}
        self._texts: dict[str, str] = {}
        self._initial_limit = int(limit * (0.35 if trials else 0.7))
        for path in sorted(task.rglob("*")):
            if path.is_file():
                key = path.relative_to(task).as_posix()
                self._paths[key] = path
                self.inventory.append(
                    {"path": key, "bytes": path.stat().st_size, "sha256": digest(path)}
                )
        priority = ["instruction.md", "task.toml", "environment/Dockerfile", "solution/solve.sh"]
        priority += [
            key
            for key in self._paths
            if key.startswith(("tests/", "solution/")) and key.count("/") == 1
        ]
        for key in priority:
            if key in self._paths:
                self._include(key, self._paths[key], maximum=12000, tail=key.endswith(".json"))
        instruction = (task / "instruction.md").read_text()
        symbols = set(re.findall(r"\bdef\s+([A-Za-z_]\w*)", instruction))
        if not symbols:
            symbols.update(re.findall(r"`([A-Za-z_]\w*)\(", instruction))
        for key, path in self._paths.items():
            if (
                key not in self.documents
                and path.suffix == ".py"
                and path.stat().st_size < 2_000_000
            ):
                self._include_symbols(key, path, symbols)
        # Reserve an execution share before reading large repository files. Collect
        # every trial first: a baseline's long test inventory must not crowd out a
        # later counterexample's actual assertion failure.
        candidates = []
        for index, trial in enumerate(trials):
            path = Path(trial.result)
            if digest(path) != trial.result_sha256:
                raise ValueError("Trial result changed since ingestion")
            prefix = f"evidence/{index}-{trial.role}/"
            summary = trial.model_dump(mode="json")
            if trial.probe:
                script_key = prefix + "probe-script.sh"
                script = summary["probe"].pop("script")
                summary["probe"]["script_path"] = script_key
                self._texts[script_key] = script
                self.inventory.append(
                    {
                        "path": script_key,
                        "bytes": len(script.encode()),
                        "sha256": hashlib.sha256(script.encode()).hexdigest(),
                    }
                )
                candidates.append((2, index, script_key, 8000))
            self.documents[prefix + "result.json"] = json.dumps(summary, indent=2)
            root = path.parent
            # Failure logs precede trajectories, verbose test inventories and
            # captured source. All remain addressable through bounded read requests.
            selected = [
                (0, "verifier/stdout.txt", 6000),
                (0, "verifier/stderr.txt", 3000),
                (0, "exception.txt", 3000),
                (1, "verifier/test-stdout.txt", 4000),
                (1, "verifier/test-stderr.txt", 3000),
                (1, "agent/exit-code.txt", 100),
                (3, "agent/oracle.txt", 4000),
                (3, "agent/trajectory.json", 12000),
                (4, "verifier/result.json", 3000),
            ]
            selected += (
                [
                    (5, item.relative_to(root).as_posix(), 3000)
                    for item in sorted((root / "artifacts").rglob("*"))
                ]
                if (root / "artifacts").is_dir()
                else []
            )
            for priority, relative, maximum in selected:
                candidate = root / relative
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Trial evidence cannot contain symlinks")
                if candidate.is_file():
                    key = prefix + relative
                    self._paths[key] = candidate
                    self.inventory.append(
                        {
                            "path": key,
                            "bytes": candidate.stat().st_size,
                            "sha256": digest(candidate),
                        }
                    )
                    # Expected baseline failures are less useful for diagnosis than
                    # oracle/probe/solver failures on the same revision.
                    candidates.append(
                        (priority + (6 if trial.role == "baseline" else 0), index, key, maximum)
                    )
        self._initial_limit = int(limit * 0.7)
        for _, _, key, maximum in sorted(candidates):
            if key in self._texts:
                self._include_text(key, self._texts[key], maximum=maximum, tail=True)
            else:
                self._include(key, self._paths[key], maximum=maximum, tail=True)

    def _include(self, key: str, path: Path, *, maximum: int, tail: bool = False):
        if path.stat().st_size > 2_000_000:
            self.omitted.append(key + ": larger than 2 MB")
            return
        try:
            text = path.read_text()
        except UnicodeError:
            self.omitted.append(key + ": binary")
            return
        self._include_text(key, text, maximum=maximum, tail=tail)

    def _include_text(self, key: str, text: str, *, maximum: int, tail: bool):
        # Leave room for model-requested source excerpts instead of filling the
        # initial prompt with unrelated repository documentation and test names.
        remaining = self._initial_limit - sum(len(value) for value in self.documents.values())
        maximum = min(maximum, remaining)
        if maximum < 100:
            self.omitted.append(key + ": context budget")
            return
        if len(text) > maximum:
            self.omitted.append(key + ": partial text; request line ranges if needed")
            text = (
                (text[: maximum // 2] + "\n[... omitted ...]\n" + text[-maximum // 2 :])
                if tail
                else text[:maximum]
            )
        self.documents[key] = text

    def _include_symbols(self, key: str, path: Path, symbols: set[str]):
        if not symbols:
            return
        try:
            text = path.read_text()
            tree = ast.parse(text)
        except (UnicodeError, SyntaxError):
            return
        lines = text.splitlines(keepends=True)
        chunks = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
                symbol.lower() in node.name.lower() for symbol in symbols if len(symbol) >= 3
            ):
                start = max(1, node.lineno - 1)
                end = min(node.end_lineno or node.lineno, start + 399)
                chunks.append(f"[Lines {start}-{end}]\n" + "".join(lines[start - 1 : end]))
                if sum(map(len, chunks)) >= 12000:
                    break
        if chunks:
            # Repairs need the module's real imports and aliases as well as the
            # selected function/class. Keep the header bounded and explicit.
            header_end = min(60, len(lines))
            header = "".join(lines[:header_end])[:4000]
            chunks.insert(
                0, f"[Module header, lines 1-{header_end}, at most 4000 chars]\n" + header
            )
        excerpt = "\n".join(chunks)[:16000]
        if excerpt and sum(map(len, self.documents.values())) + len(excerpt) <= self._initial_limit:
            self.documents[key] = excerpt
            self.omitted.append(
                key + ": symbol excerpts only; request ranges/search for other code"
            )

    def read_more(self, requests: list[ReadRequest]):
        for request in requests:
            if request.path not in self._paths and request.path not in self._texts:
                raise ValueError(f"Requested file is not in evidence inventory: {request.path}")
            if request.path in self._texts:
                text = self._texts[request.path]
            else:
                path = self._paths[request.path]
                if path.stat().st_size > 2_000_000:
                    raise ValueError("Requested file exceeds bounded text reader")
                text = path.read_text()
            lines = text.splitlines(keepends=True)
            if request.query is not None:
                if not request.query.strip() or len(request.query) > 200:
                    raise ValueError("Search query must have 1-200 characters")
                matches = [index for index, line in enumerate(lines) if request.query in line][:8]
                text = (
                    "\n".join(
                        f"[Lines {max(1, index - 4)}-{min(len(lines), index + 26)}]\n"
                        + "".join(lines[max(0, index - 5) : index + 26])
                        for index in matches
                    )
                    or "[No literal matches found]"
                )
            else:
                text = "".join(lines[request.start_line - 1 : request.end_line])
            if not text:
                raise ValueError("Requested range is empty")
            suffix = (
                f"search={request.query}"
                if request.query is not None
                else f"L{request.start_line}-L{request.end_line}"
            )
            key = f"{request.path}:{suffix}"
            self.documents[key] = text
        if sum(map(len, self.documents.values())) > self.limit:
            raise ValueError("Additional reads exceeded context budget")

    def payload(self, **extra) -> str:
        result = json.dumps(
            {
                "documents": self.documents,
                "inventory": self.inventory,
                "omitted": self.omitted,
                **extra,
            },
            ensure_ascii=False,
        )
        # Inventory and decision metadata have their own small allowance.
        if len(result) > self.limit + 70000:
            raise ValueError("Evidence pack exceeds context and inventory limits")
        return result

    def validate_review(self, review: Review):
        citations = [
            citation
            for name in ("task", "verifier", "leakage")
            for citation in getattr(review, name).evidence
        ]
        citations += [
            citation for item in [*review.issues, *review.probes] for citation in item.evidence
        ]
        for citation in citations:
            document = self.documents.get(citation.path, "")
            if not document or " ".join(citation.quote.split()) not in " ".join(document.split()):
                raise ValueError(
                    f"Review citation is not grounded in supplied text: {citation.path}; "
                    f"invalid quote={citation.quote[:250]!r}. Copy a short contiguous excerpt; no ellipses."
                )
