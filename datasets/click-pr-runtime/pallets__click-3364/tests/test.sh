#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 8a2b48901a08b3d2ec3a9bbd151948a9765368c6 -- tests/test_defaults.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_defaults.py b/tests/test_defaults.py
index cec051c67..49f0ac6ed 100644
--- a/tests/test_defaults.py
+++ b/tests/test_defaults.py
@@ -357,6 +357,43 @@ def cli(value):
     assert result.output == repr(expected)
 
 
+@pytest.mark.parametrize(
+    ("default_map", "option_kwargs", "cli_args", "expected"),
+    [
+        # String is split for nargs=2 option.
+        ({"point": "3 4"}, {"nargs": 2, "type": int}, [], (3, 4)),
+        # String is split for explicit Tuple type.
+        ({"point": "hello world"}, {"type": (str, str)}, [], ("hello", "world")),
+        # Already-structured tuple passes through unchanged.
+        ({"point": ("a", "b")}, {"nargs": 2}, [], ("a", "b")),
+        # Already-structured list passes through unchanged.
+        ({"point": [5, 6]}, {"nargs": 2, "type": int}, [], (5, 6)),
+        # CLI args override default_map for nargs > 1.
+        (
+            {"point": "3 4"},
+            {"nargs": 2, "type": int},
+            ["--point", "10", "20"],
+            (10, 20),
+        ),
+    ],
+)
+def test_default_map_nargs(runner, default_map, option_kwargs, cli_args, expected):
+    """A string in ``default_map`` for an option with ``nargs > 1`` should be
+    split the same way an environment variable string is split.
+
+    Regression test for https://github.com/pallets/click/issues/2745.
+    """
+
+    @click.command()
+    @click.option("--point", **option_kwargs)
+    def cli(point):
+        click.echo(repr(point))
+
+    result = runner.invoke(cli, cli_args, default_map=default_map)
+    assert result.exit_code == 0
+    assert result.output.strip() == repr(expected)
+
+
 def test_unset_in_default_map(runner):
     """An ``UNSET`` value in ``default_map`` should be treated as if
     the key is absent, and so fallback to the parameter's own default.

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 8a2b48901a08b3d2ec3a9bbd151948a9765368c6 -- tests/test_defaults.py || true
