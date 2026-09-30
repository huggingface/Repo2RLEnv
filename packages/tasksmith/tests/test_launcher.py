"""The launcher must preserve arguments and the underlying CLI's exit status."""

import sys
import unittest
from unittest.mock import patch

from tasksmith import main


class LauncherTests(unittest.TestCase):
    def test_explicit_arguments_and_failure_status(self):
        args = ["show", "a path with spaces", "--json"]
        with patch("repo2rlenv.cli.main", return_value=1) as cli:
            self.assertEqual(main(args), 1)
        cli.assert_called_once_with(["tasksmith", *args])
        self.assertEqual(args, ["show", "a path with spaces", "--json"])

    def test_process_arguments(self):
        with (
            patch.object(sys, "argv", ["tasksmith", "run", "panel.json"]),
            patch("repo2rlenv.cli.main", return_value=0) as cli,
        ):
            self.assertEqual(main(), 0)
        cli.assert_called_once_with(["tasksmith", "run", "panel.json"])

    def test_help_exit_propagates(self):
        with patch("repo2rlenv.cli.main", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
