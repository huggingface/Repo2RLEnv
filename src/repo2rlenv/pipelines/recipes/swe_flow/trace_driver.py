"""Standalone pytest call tracer, copied into a remote repository container."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import pytest


class CallTrace:
    def __init__(self, settings: dict):
        self.roots = [Path("/workspace") / root for root in settings["source_paths"]]
        self.settings = settings
        self.current = None
        self.records = {}
        self.identities = {}

    def identity(self, code):
        code_id = id(code)
        if code_id in self.identities:
            return self.identities[code_id]
        path = Path(code.co_filename)
        if not path.is_absolute() or not any(
            path == root or root in path.parents for root in self.roots
        ):
            self.identities[code_id] = None
            return None
        if code.co_name.startswith("<"):
            return None
        result = f"{path.relative_to('/workspace')}:{code.co_firstlineno}:{code.co_name}"
        self.identities[code_id] = result
        return result

    def profile(self, frame, event, arg):
        if event != "call" or self.current is None:
            return
        self.calls += 1
        if self.calls % 10000 == 0 and time.monotonic() >= self.deadline:
            self.records[self.current]["truncated"] = True
            sys.setprofile(None)
            return
        callee = self.identity(frame.f_code)
        if callee is None:
            return
        record = self.records[self.current]
        record["core_nodes"].add(callee)
        caller = self.identity(frame.f_back.f_code) if frame.f_back else None
        if caller:
            record["edges"].add((caller, callee))
        elif frame.f_back and "/tests/" in frame.f_back.f_code.co_filename:
            record["target_nodes"].add(callee)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = item.nodeid
        self.records[self.current] = {
            "core_nodes": set(),
            "target_nodes": set(),
            "edges": set(),
            "truncated": False,
        }
        self.calls = 0
        self.deadline = time.monotonic() + 5
        previous = sys.getprofile()
        sys.setprofile(self.profile)
        try:
            yield
        finally:
            sys.setprofile(previous)
            self.current = None

    def pytest_collection_modifyitems(self, session, config, items):
        selected = list(items)
        random.Random(self.settings["seed"]).shuffle(selected)
        selected = set(selected[: self.settings["max_tests"]])
        config.hook.pytest_deselected(items=[item for item in items if item not in selected])
        items[:] = [item for item in items if item in selected]


def main() -> None:
    tracer = CallTrace(json.loads(sys.argv[1]))
    code = pytest.main(sys.argv[2:], plugins=[tracer])
    Path("/tmp/traces.json").write_text(
        json.dumps(
            [
                {
                    "test_id": key,
                    **{
                        name: sorted(value) if isinstance(value, set) else value
                        for name, value in record.items()
                    },
                }
                for key, record in tracer.records.items()
            ],
            sort_keys=True,
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
