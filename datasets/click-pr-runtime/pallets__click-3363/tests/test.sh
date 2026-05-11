#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout c8da1fcc2cb4523c1fb5bef7f0ca82394dde1efd -- tests/test_options.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_options.py b/tests/test_options.py
index bc80c8a0a..1d90d2959 100644
--- a/tests/test_options.py
+++ b/tests/test_options.py
@@ -1406,6 +1406,11 @@ def test_type_from_flag_value():
     assert param.type is click.INT
     param = click.Option(["-b", "x"], flag_value=8)
     assert param.type is click.INT
+    # Non-basic types auto-detect as UNPROCESSED to avoid stringification.
+    param = click.Option(["-c", "x"], flag_value=EngineType.OSS)
+    assert param.type is click.UNPROCESSED
+    param = click.Option(["-d", "x"], flag_value=frozenset())
+    assert param.type is click.UNPROCESSED
 
 
 @pytest.mark.parametrize(
@@ -2127,13 +2132,13 @@ class Class2:
             [],
             EngineType.OSS,
         ),
-        # Type is not specified and default to string, so the default value is
-        # returned as a string, even if it is a boolean. Also, defaults to the
-        # flag_value instead of the default value to support legacy behavior.
+        # Type is not specified. For string flag_value, STRING type is used and
+        # the default value is converted to string. For non-basic types (like
+        # enums), UNPROCESSED is used and values pass through unchanged.
         ({"flag_value": "1", "default": True}, [], "1"),
         ({"flag_value": "1", "default": 42}, [], "42"),
-        ({"flag_value": EngineType.OSS, "default": True}, [], "EngineType.OSS"),
-        ({"flag_value": EngineType.OSS, "default": 42}, [], "42"),
+        ({"flag_value": EngineType.OSS, "default": True}, [], EngineType.OSS),
+        ({"flag_value": EngineType.OSS, "default": 42}, [], 42),
         # See: the result is the same if we force the type to be str.
         ({"type": str, "flag_value": 1, "default": True}, [], "1"),
         ({"type": str, "flag_value": 1, "default": 42}, [], "42"),
@@ -2199,28 +2204,29 @@ def scan(pro):
             ["--opt2"],
             EngineType.PRO,
         ),
-        # Check that passing exotic flag values like classes is supported, but are
-        # rendered to strings when the type is not specified.
+        # Exotic flag values like classes are passed through unchanged when no
+        # explicit type is given (UNPROCESSED is auto-detected).
+        # https://github.com/pallets/click/issues/2012
         # https://github.com/pallets/click/issues/3121
         (
             {"flag_value": Class1, "default": True},
             {"flag_value": Class2},
             [],
-            "<class 'test_options.Class1'>",
+            Class1,
         ),
         (
             {"flag_value": Class1, "default": True},
             {"flag_value": Class2},
             ["--opt1"],
-            "<class 'test_options.Class1'>",
+            Class1,
         ),
         (
             {"flag_value": Class1, "default": True},
             {"flag_value": Class2},
             ["--opt2"],
-            "<class 'test_options.Class2'>",
+            Class2,
         ),
-        # Even the default is processed as a string.
+        # String and None defaults pass through unchanged.
         ({"flag_value": Class1, "default": "True"}, {"flag_value": Class2}, [], "True"),
         ({"flag_value": Class1, "default": None}, {"flag_value": Class2}, [], None),
         # To get the classes as-is, we need to specify the type as UNPROCESSED.
@@ -2245,18 +2251,18 @@ def scan(pro):
         ),
         # Setting the default to a class, an instance of the class is returned instead
         # of the class itself, because the default is allowed to be callable (and
-        # consummd). And this happens whatever the type is.
+        # consumed). And this happens whatever the type is.
         (
             {"flag_value": Class1, "default": Class1},
             {"flag_value": Class2},
             [],
-            re.compile(r"'<test_options.Class1 object at 0x[0-9A-Fa-f]+>'"),
+            re.compile(r"<test_options.Class1 object at 0x[0-9A-Fa-f]+>"),
         ),
         (
             {"flag_value": Class1, "default": Class2},
             {"flag_value": Class2},
             [],
-            re.compile(r"'<test_options.Class2 object at 0x[0-9A-Fa-f]+>'"),
+            re.compile(r"<test_options.Class2 object at 0x[0-9A-Fa-f]+>"),
         ),
         (
             {"flag_value": Class1, "type": UNPROCESSED, "default": Class1},
@@ -2322,12 +2328,13 @@ def cli(dual_option):
             ["--opt"],
             Class1,
         ),
-        # Without UNPROCESSED, the class is str()-ified by the default STRING type.
-        ({"flag_value": Class1, "default": True}, [], "<class 'test_options.Class1'>"),
+        # Without explicit UNPROCESSED, the class still passes through unchanged
+        # because UNPROCESSED is auto-detected for non-basic flag_value types.
+        ({"flag_value": Class1, "default": True}, [], Class1),
         (
             {"flag_value": Class1, "default": True},
             ["--opt"],
-            "<class 'test_options.Class1'>",
+            Class1,
         ),
         # Explicit default=Class1 (not via default=True alignment): callable IS invoked,
         # because the user explicitly set a callable as the default.
@@ -2473,6 +2480,48 @@ def cli(value):
     assert opt.get_default(ctx, call=True) is expected_get_default
 
 
+def test_flag_value_not_stringified_for_custom_types(runner):
+    """Non-basic flag_value types are passed through unchanged without
+    requiring ``type=click.UNPROCESSED``.
+
+    Regression test for https://github.com/pallets/click/issues/2012
+    """
+
+    @click.command()
+    @click.option("--cls1", "config_cls", flag_value=Class1, default=True)
+    @click.option("--cls2", "config_cls", flag_value=Class2)
+    def cli(config_cls):
+        click.echo(repr(config_cls), nl=False)
+
+    # Default activates --cls1 (default=True resolves to flag_value).
+    result = runner.invoke(cli, [])
+    assert result.exit_code == 0
+    assert result.output == repr(Class1)
+
+    result = runner.invoke(cli, ["--cls1"])
+    assert result.exit_code == 0
+    assert result.output == repr(Class1)
+
+    result = runner.invoke(cli, ["--cls2"])
+    assert result.exit_code == 0
+    assert result.output == repr(Class2)
+
+    # Enum flag_value without explicit type is also preserved.
+    @click.command()
+    @click.option("--oss", "engine", flag_value=EngineType.OSS, default=True)
+    @click.option("--pro", "engine", flag_value=EngineType.PRO)
+    def cli2(engine):
+        click.echo(repr(engine), nl=False)
+
+    result = runner.invoke(cli2, [])
+    assert result.exit_code == 0
+    assert result.output == repr(EngineType.OSS)
+
+    result = runner.invoke(cli2, ["--pro"])
+    assert result.exit_code == 0
+    assert result.output == repr(EngineType.PRO)
+
+
 def test_custom_type_frozenset_flag_value(runner):
     """Check that frozenset is correctly handled as a type, a flag value and a default.
 

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout c8da1fcc2cb4523c1fb5bef7f0ca82394dde1efd -- tests/test_options.py || true
