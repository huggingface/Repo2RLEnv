#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 04ef3a6f473deb2499721a8d11f92a7d2c0912f2 -- tests/test_options.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_options.py b/tests/test_options.py
index e335f3c1a..85f622459 100644
--- a/tests/test_options.py
+++ b/tests/test_options.py
@@ -1259,12 +1259,36 @@ def test_show_default_string(runner):
     assert "[default: (unlimited)]" in message
 
 
-def test_show_default_with_empty_string(runner):
-    """When show_default is True and default is set to an empty string."""
-    opt = click.Option(["--limit"], default="", show_default=True)
+class _StrictEq:
+    """Object whose ``__eq__`` raises on string comparison (like semver.Version)."""
+
+    def __eq__(self, other):
+        if isinstance(other, str):
+            raise ValueError("cannot compare to string")
+        return NotImplemented
+
+    def __str__(self):
+        return "strict"
+
+
+@pytest.mark.parametrize(
+    ("default", "expected"),
+    [
+        ("", '[default: ""]'),
+        (_StrictEq(), "[default: strict]"),
+    ],
+    ids=["empty-string", "non-string-comparable-object"],
+)
+def test_show_default_with_empty_string(runner, default, expected):
+    """The empty-string check in help rendering must not break on objects
+    whose ``__eq__`` raises for string operands.
+
+    Regression test for https://github.com/pallets/click/issues/3298.
+    """
+    opt = click.Option(["--limit"], default=default, show_default=True)
     ctx = click.Context(click.Command("cli"))
     message = opt.get_help_record(ctx)[1]
-    assert '[default: ""]' in message
+    assert expected in message
 
 
 def test_do_not_show_no_default(runner):

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 04ef3a6f473deb2499721a8d11f92a7d2c0912f2 -- tests/test_options.py || true
