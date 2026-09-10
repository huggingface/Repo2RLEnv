"""Controller contracts: these tests never start a sandbox or make API calls."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import run as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / "reproductions").resolve()
        (self.root / "runs").mkdir(parents=True)
        (self.root / "runs/worker.json").write_text(json.dumps({"state": "running"}))
        self.root_patch = patch.object(runner, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.make_workflow(
            "example",
            {
                "generate": {
                    "script": "example/generate.sh",
                    "description": "test stage",
                    "timeout_seconds": 30,
                    "reserve_usd": 2,
                }
            },
        )
        self.remote = patch.object(runner, "invoke")
        self.invoke = self.remote.start()
        self.addCleanup(self.remote.stop)
        self.money = patch.object(runner, "reserve")
        self.reserve = self.money.start()
        self.addCleanup(self.money.stop)

    def make_workflow(self, name, stages):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        for spec in stages.values():
            (self.root / spec["script"]).write_text("#!/bin/bash\ntrue\n")
        (folder / "workflow.json").write_text(json.dumps({"id": name, "stages": stages}))

    def test_parallel_duplicate_claim_starts_only_once(self):
        def attempt(_):
            try:
                runner.execute("example", "generate", "worker", "one")
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(sum(results), 1)
        self.reserve.assert_called_once()
        self.assertEqual(self.invoke.call_count, 2)

    def test_failed_launch_remains_recorded_and_is_not_retried(self):
        self.invoke.side_effect = RuntimeError("remote outcome uncertain")
        with self.assertRaises(RuntimeError):
            runner.execute("example", "generate", "worker", "one")
        with self.assertRaises(ValueError):
            runner.execute("example", "generate", "worker", "one")
        self.assertEqual(self.invoke.call_count, 1)
        self.reserve.assert_called_once()
        record = json.loads(runner.journal_path("worker", "example", "generate", "one").read_text())
        self.assertEqual(record["status"], "failed_or_interrupted")
        self.assertIn("example/generate.sh", record["recipe_files"])

    def test_completed_setup_can_be_reused_by_a_different_attempt(self):
        stages = runner.workflow("example")["stages"]
        stages["generate"]["requires"] = ["runtime/bootstrap"]
        self.make_workflow("example", stages)
        self.make_workflow(
            "runtime",
            {
                "bootstrap": {
                    "script": "runtime/bootstrap.sh",
                    "description": "test setup",
                    "timeout_seconds": 30,
                }
            },
        )
        with self.assertRaises(ValueError):
            runner.execute("example", "generate", "worker", "new")
        self.reserve.assert_not_called()
        self.invoke.assert_not_called()
        setup = runner.journal_path("worker", "runtime", "bootstrap", "old")
        setup.parent.mkdir(parents=True)
        setup.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "worker": "worker",
                    "attempt": "old",
                    "finished_at": "2026-09-10T12:00:00Z",
                    "script_sha256": hashlib.sha256(
                        (self.root / "runtime/bootstrap.sh").read_bytes()
                    ).hexdigest(),
                }
            )
        )
        record = runner.execute("example", "generate", "worker", "new")
        self.assertEqual(record["dependencies"]["runtime/bootstrap"]["attempt"], "old")
        (self.root / "runtime/bootstrap.sh").write_text("changed recipe\n")
        with self.assertRaises(ValueError):
            runner.completed_dependency("worker", "runtime/bootstrap")

    def test_path_escape_and_symlink_recipe_rejected_before_launch(self):
        outside = self.root.parent / "outside.sh"
        outside.write_text("private\n")
        path = self.root / "example/workflow.json"
        flow = json.loads(path.read_text())
        flow["stages"]["generate"]["script"] = "../outside.sh"
        path.write_text(json.dumps(flow))
        with self.assertRaises(ValueError):
            runner.execute("example", "generate", "worker", "one")
        flow["stages"]["generate"]["script"] = "example/link.sh"
        (self.root / "example/link.sh").symlink_to(outside)
        path.write_text(json.dumps(flow))
        with self.assertRaises(ValueError):
            runner.execute("example", "generate", "worker", "one")
        self.invoke.assert_not_called()
        self.reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
