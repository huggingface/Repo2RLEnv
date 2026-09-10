from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget import locked, reserve, settle, total
from harbor_artifacts import export_native


class BudgetTests(unittest.TestCase):
    def test_concurrent_reservations_cannot_overspend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            with locked(path) as data:
                data["limit_usd"] = "10"
            def attempt(index):
                try:
                    reserve(str(index), "3", "test", path)
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                accepted = list(pool.map(attempt, range(8)))
            self.assertEqual(sum(accepted), 3)
            self.assertEqual(total(json.loads(path.read_text())), 9)

    def test_duplicate_and_uncertain_reservation_stay_accounted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            reserve("attempt", "8", "test", path)
            with self.assertRaises(ValueError):
                reserve("attempt", "8", "test", path)
            self.assertEqual(total(json.loads(path.read_text())), 8)
            settle("attempt", "3.12", "result.json", "model_reported", path)
            self.assertEqual(str(total(json.loads(path.read_text()))), "3.12")


class ExportTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "native"
        (source / "environment").mkdir(parents=True)
        (source / "tests").mkdir()
        (source / "instruction.md").write_text("Fixture instruction")
        (source / "task.toml").write_text('version = "1.0"\n')
        (source / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        (source / "tests" / "test.sh").write_text("#!/bin/bash\nexit 1\n")
        (source / "idea_agent_log.txt").write_text("private author evidence")
        return source

    def test_export_preserves_verifier_excludes_author_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            destination = root / "export"
            report = export_native(source, destination)
            self.assertFalse((destination / "idea_agent_log.txt").exists())
            self.assertEqual((source / "tests" / "test.sh").read_bytes(),
                             (destination / "tests" / "test.sh").read_bytes())
            self.assertFalse(report["oracle_present"])
            with self.assertRaises(FileExistsError):
                export_native(source, destination)

    def test_host_symlink_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            (source / "environment" / "outside").symlink_to(root / "private")
            with self.assertRaises(ValueError):
                export_native(source, root / "export")
            self.assertFalse((root / "export").exists())


if __name__ == "__main__":
    unittest.main()
