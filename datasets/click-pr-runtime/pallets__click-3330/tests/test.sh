#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 1339fd3323357119a9c7a6326c788f80295954ce -- tests/test_termui.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_termui.py b/tests/test_termui.py
index 0883906f4..be0c2df69 100644
--- a/tests/test_termui.py
+++ b/tests/test_termui.py
@@ -1,4 +1,5 @@
 import platform
+import shlex
 import tempfile
 import time
 from unittest.mock import patch
@@ -426,21 +427,21 @@ def test_edit(runner):
             '"C:\\Program Files\\Sublime Text 3\\sublime_text.exe"',
             ["f.txt"],
             ["C:\\Program Files\\Sublime Text 3\\sublime_text.exe", "f.txt"],
-            id="quoted windows path with spaces (issue 1026)",
+            id="quoted windows path with spaces",
         ),
         # PR #1477: pager/editor command with flags, like ``less -FRSX``.
         pytest.param(
             "less -FRSX",
             ["f.txt"],
             ["less", "-FRSX", "f.txt"],
-            id="command with flags (pr 1477)",
+            id="command with flags",
         ),
         # Issue #1026: quoted command with ``--wait`` flag.
         pytest.param(
             '"my command" --option value arg',
             ["f.txt"],
             ["my command", "--option", "value", "arg", "f.txt"],
-            id="quoted command with args (issue 1026)",
+            id="quoted command with args",
         ),
         # PR #1477: unquoted unix path.
         pytest.param(
@@ -454,7 +455,49 @@ def test_edit(runner):
             "/Applications/Sublime\\ Text.app/Contents/SharedSupport/bin/subl",
             ["f.txt"],
             ["/Applications/Sublime Text.app/Contents/SharedSupport/bin/subl", "f.txt"],
-            id="escaped space in unix path (issue 1026)",
+            id="escaped space in unix path",
+        ),
+        pytest.param(
+            "  vim  ",
+            ["f.txt"],
+            ["vim", "f.txt"],
+            id="leading and trailing whitespace",
+        ),
+        pytest.param(
+            "vim\tf.txt",
+            [],
+            ["vim", "f.txt"],
+            id="tab-separated tokens",
+        ),
+        pytest.param(
+            "'/Applications/My Editor.app/Contents/MacOS/editor'",
+            ["f.txt"],
+            ["/Applications/My Editor.app/Contents/MacOS/editor", "f.txt"],
+            id="single-quoted path with spaces",
+        ),
+        pytest.param(
+            '"my editor" --wait --new-window',
+            ["file 1.txt", "file 2.txt"],
+            ["my editor", "--wait", "--new-window", "file 1.txt", "file 2.txt"],
+            id="quoted editor with multiple flags and filenames with spaces",
+        ),
+        pytest.param(
+            "vim -u NONE -N",
+            ["f.txt"],
+            ["vim", "-u", "NONE", "-N", "f.txt"],
+            id="multiple short flags",
+        ),
+        pytest.param(
+            "editor",
+            ['file"name.txt'],
+            ["editor", 'file"name.txt'],
+            id="filename with double quote",
+        ),
+        pytest.param(
+            "editor",
+            ["file'name.txt"],
+            ["editor", "file'name.txt"],
+            id="filename with single quote",
         ),
     ],
 )
@@ -520,6 +563,68 @@ def test_editor_nonexistent_exception():
             Editor(editor="nonexistent").edit_files(["f.txt"])
 
 
+@pytest.mark.parametrize(
+    ("pager_env", "expected_parts"),
+    [
+        # Simple commands.
+        pytest.param("cat", ["cat"], id="simple command"),
+        pytest.param("less", ["less"], id="less"),
+        pytest.param("less -FRSX", ["less", "-FRSX"], id="command with flags"),
+        # Whitespace handling.
+        pytest.param("", [], id="empty string"),
+        pytest.param("   ", [], id="whitespace only"),
+        pytest.param("  less  ", ["less"], id="leading and trailing spaces"),
+        pytest.param("less\t-R", ["less", "-R"], id="tab as separator"),
+        # Quoted Windows paths: quotes are stripped in POSIX mode (the
+        # default), preserving backslashes inside quoted tokens (issue #1026).
+        pytest.param(
+            '"C:\\Program Files\\Git\\usr\\bin\\less.exe"',
+            ["C:\\Program Files\\Git\\usr\\bin\\less.exe"],
+            id="quoted windows path with spaces",
+        ),
+        pytest.param(
+            '"C:\\Program Files\\Git\\usr\\bin\\less.exe" -R',
+            ["C:\\Program Files\\Git\\usr\\bin\\less.exe", "-R"],
+            id="quoted windows path with flag",
+        ),
+        # Single-quoted path.
+        pytest.param(
+            "'/usr/local/bin/my pager'",
+            ["/usr/local/bin/my pager"],
+            id="single-quoted path with spaces",
+        ),
+        # Unix paths.
+        pytest.param("/usr/bin/less", ["/usr/bin/less"], id="unix absolute path"),
+        pytest.param(
+            "/usr/bin/my\\ pager",
+            ["/usr/bin/my pager"],
+            id="escaped space in unix path",
+        ),
+        # PR #1477: POSIX mode (the default) eats unquoted backslashes.
+        # On Windows, users must quote paths that contain backslashes.
+        pytest.param(
+            "C:\\path\\to\\exe /test other\\path",
+            ["C:pathtoexe", "/test", "otherpath"],
+            id="unquoted backslashes eaten in POSIX mode",
+        ),
+    ],
+)
+def test_pager_shlex_split(pager_env, expected_parts):
+    """Verify shlex.split produces the expected argv for PAGER values.
+
+    Tests the splitting logic used by :func:`click._termui_impl.pager` to
+    turn the ``PAGER`` environment variable into an ``argv`` list. See
+    issue #1026, PR #1477, PR #1543, PR #2775.
+    """
+    assert shlex.split(pager_env) == expected_parts
+
+
+def test_editor_unclosed_quote():
+    """An unclosed quote in the editor command raises ValueError."""
+    with pytest.raises(ValueError, match="No closing quotation"):
+        Editor(editor='"unclosed').edit_files(["f.txt"])
+
+
 @pytest.mark.parametrize(
     ("prompt_required", "required", "args", "expect"),
     [

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 1339fd3323357119a9c7a6326c788f80295954ce -- tests/test_termui.py || true
