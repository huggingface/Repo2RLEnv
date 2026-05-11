#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout d777956105fde08e01dd895dde2b86ccdf558d59 -- tests/test_defaults.py tests/test_options.py tests/test_termui.py tests/test_types.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_defaults.py b/tests/test_defaults.py
index 2226bda5e7..cec051c67a 100644
--- a/tests/test_defaults.py
+++ b/tests/test_defaults.py
@@ -84,7 +84,7 @@ def cli(arg):
 
 
 def test_multiple_flag_default(runner):
-    """Default default for flags when multiple=True should be empty tuple."""
+    """Default for flags when multiple=True should be empty tuple."""
 
     @click.command
     # flag due to secondary token
@@ -274,7 +274,7 @@ def test_lookup_default_override_respected(runner):
     ``None``.
 
     Previous attempts in https://github.com/pallets/click/pr/3199 were entirely
-    bypassing the user's overridded method.
+    bypassing the user's overridden method.
     """
 
     class CustomContext(click.Context):
diff --git a/tests/test_options.py b/tests/test_options.py
index f6364b975b..bc80c8a0a7 100644
--- a/tests/test_options.py
+++ b/tests/test_options.py
@@ -520,7 +520,7 @@ def cli(shout):
     "value",
     (
         # Extra spaces inside the value.
-        "tr ue",
+        "tr ue",  # codespell:ignore ue
         "fa lse",
         # Numbers.
         "10",
@@ -999,13 +999,13 @@ def get_default(self, ctx, call=True):
             return "I am a default"
 
     @click.command()
-    @click.argument("testarg", cls=CustomArgument, default="you wont see me")
+    @click.argument("testarg", cls=CustomArgument, default="you won't see me")
     def cmd(testarg):
         click.echo(testarg)
 
     result = runner.invoke(cmd)
     assert "I am a default" in result.output
-    assert "you wont see me" not in result.output
+    assert "you won't see me" not in result.output
 
 
 def test_option_custom_class(runner):
@@ -1015,13 +1015,13 @@ def get_help_record(self, ctx):
             return ("--help", "I am a help text")
 
     @click.command()
-    @click.option("--testoption", cls=CustomOption, help="you wont see me")
+    @click.option("--testoption", cls=CustomOption, help="you won't see me")
     def cmd(testoption):
         click.echo(testoption)
 
     result = runner.invoke(cmd, ["--help"])
     assert "I am a help text" in result.output
-    assert "you wont see me" not in result.output
+    assert "you won't see me" not in result.output
 
 
 @pytest.mark.parametrize(
@@ -1068,8 +1068,8 @@ def get_help_record(self, ctx):
             """a dumb override of a help text for testing"""
             return ("--help", "I am a help text")
 
-    # Assign to a variable to re-use the decorator.
-    testoption = click.option("--testoption", cls=CustomOption, help="you wont see me")
+    # Assign to a variable to reuse the decorator.
+    testoption = click.option("--testoption", cls=CustomOption, help="you won't see me")
 
     @click.command()
     @testoption
@@ -1085,7 +1085,7 @@ def cmd2(testoption):
     for cmd in (cmd1, cmd2):
         result = runner.invoke(cmd, ["--help"])
         assert "I am a help text" in result.output
-        assert "you wont see me" not in result.output
+        assert "you won't see me" not in result.output
 
 
 @pytest.mark.parametrize("custom_class", (True, False))
diff --git a/tests/test_termui.py b/tests/test_termui.py
index d12e403ac2..7aa2600849 100644
--- a/tests/test_termui.py
+++ b/tests/test_termui.py
@@ -396,7 +396,7 @@ def test_edit(runner):
         result = click.edit(filename=named_tempfile.name, editor="sed -i~ 's/$/Test/'")
         assert result is None
 
-        # We need ot reopen the file as it becomes unreadable after the edit.
+        # We need to reopen the file as it becomes unreadable after the edit.
         with open(named_tempfile.name) as reopened_file:
             # POSIX says that when sed writes a pattern space to output then it
             # is immediately followed by a newline and so the expected result
diff --git a/tests/test_types.py b/tests/test_types.py
index 75434f1042..e633be4a2b 100644
--- a/tests/test_types.py
+++ b/tests/test_types.py
@@ -228,7 +228,7 @@ def test_file_surrogates(type, tmp_path):
 
     # - common case: �': No such file or directory
     # - special case: Illegal byte sequence
-    # The spacial case is seen with rootless Podman. The root cause is most
+    # The special case is seen with rootless Podman. The root cause is most
     # likely that the path is handled by a user-space program (FUSE).
     match = r"(�': No such file or directory|Illegal byte sequence)"
     with pytest.raises(click.BadParameter, match=match):

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout d777956105fde08e01dd895dde2b86ccdf558d59 -- tests/test_defaults.py tests/test_options.py tests/test_termui.py tests/test_types.py || true
