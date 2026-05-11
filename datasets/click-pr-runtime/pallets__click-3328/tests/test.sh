#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 8c95c73bd5ef89eac638f85f1904a104ba4b1a32 -- tests/test_options.py tests/test_termui.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_options.py b/tests/test_options.py
index c2bf1e961..f6364b975 100644
--- a/tests/test_options.py
+++ b/tests/test_options.py
@@ -1259,6 +1259,19 @@ def test_show_default_string(runner):
     assert "[default: (unlimited)]" in message
 
 
+def test_string_show_default_shows_custom_string_in_prompt(runner):
+    @click.command()
+    @click.option(
+        "--arg1", show_default="custom", prompt=True, default="my-default-value"
+    )
+    def cmd(arg1):
+        pass
+
+    result = runner.invoke(cmd, input="my-input", standalone_mode=False)
+    assert "(custom)" in result.output
+    assert "my-default-value" not in result.output
+
+
 class _StrictEq:
     """Object whose ``__eq__`` raises on string comparison (like semver.Version)."""
 
diff --git a/tests/test_termui.py b/tests/test_termui.py
index 026e8fce3..d12e403ac 100644
--- a/tests/test_termui.py
+++ b/tests/test_termui.py
@@ -9,6 +9,7 @@
 import click._termui_impl
 from click._compat import WIN
 from click._termui_impl import Editor
+from click._utils import UNSET
 from click.exceptions import BadParameter
 from click.exceptions import MissingParameter
 
@@ -717,6 +718,58 @@ def cmd(arg1):
     assert "my-default-value" not in result.output
 
 
+@pytest.mark.parametrize(
+    ("show_default", "default", "user_input", "in_prompt", "not_in_prompt"),
+    [
+        # Regular string replaces the actual default in the prompt.
+        ("custom", "actual", "\n", "(custom)", "actual"),
+        # String with spaces.
+        ("custom label", "actual", "\n", "(custom label)", "actual"),
+        # Unicode characters.
+        ("∞", "0", "\n", "(∞)", None),
+        # Numeric default: custom string hides the number.
+        ("unlimited", 42, "\n", "(unlimited)", "42"),
+        # Explicit default=None: custom string still appears, must provide input.
+        ("computed at runtime", None, "value\n", "(computed at runtime)", None),
+        # No default kwarg at all (internal UNSET sentinel): same as None.
+        ("computed at runtime", UNSET, "value\n", "(computed at runtime)", None),
+        # Empty string is falsy: suppresses any default display.
+        ("", "actual", "\n", None, "actual"),
+    ],
+    ids=[
+        "simple-string",
+        "string-with-spaces",
+        "unicode",
+        "numeric-default",
+        "default-is-none",
+        "default-is-unset",
+        "empty-string-is-falsy",
+    ],
+)
+def test_string_show_default_in_prompt(
+    runner, show_default, default, user_input, in_prompt, not_in_prompt
+):
+    """When show_default is a string, the prompt should display that
+    string in parentheses instead of the actual default value,
+    matching the help text behavior. See pallets/click#2836."""
+
+    option_kwargs = {"show_default": show_default, "prompt": True}
+    if default is not UNSET:
+        option_kwargs["default"] = default
+
+    @click.command()
+    @click.option("--arg1", **option_kwargs)
+    def cmd(arg1):
+        click.echo(arg1)
+
+    result = runner.invoke(cmd, input=user_input, standalone_mode=False)
+    prompt_line = result.output.split("\n")[0]
+    if in_prompt is not None:
+        assert in_prompt in prompt_line
+    if not_in_prompt is not None:
+        assert not_in_prompt not in prompt_line
+
+
 REPEAT = object()
 """Sentinel value to indicate that the prompt is expected to be repeated.
 

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 8c95c73bd5ef89eac638f85f1904a104ba4b1a32 -- tests/test_options.py tests/test_termui.py || true
