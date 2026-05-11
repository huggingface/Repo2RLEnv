#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 4a352253c9ff013e36d11e4a6820d36d00ff2cd4 -- docs/testing.md src/click/testing.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/docs/testing.md b/docs/testing.md
index c7390adbe..73f88f547 100644
--- a/docs/testing.md
+++ b/docs/testing.md
@@ -4,7 +4,8 @@
 .. currentmodule:: click.testing
 ```
 
-Click provides the {ref}`click.testing <testing>` module to help you invoke command line applications and check their behavior.
+Click provides the {ref}`click.testing <testing>` module to help you invoke
+command line applications and check their behavior.
 
 These tools should only be used for testing since they change
 the entire interpreter state for simplicity. They are not thread-safe!
@@ -20,7 +21,9 @@ The examples use [pytest](https://docs.pytest.org/en/stable/) style tests.
 
 The key pieces are:
   - {class}`CliRunner` - used to invoke commands as command line scripts.
-  - {class}`Result` - returned from {meth}`CliRunner.invoke`. Captures output data, exit code, optional exception, and captures the output as bytes and binary data.
+  - {class}`Result` - returned from {meth}`CliRunner.invoke`. Captures output
+    data, exit code, optional exception, and captures the output as bytes and
+    binary data.
 
 ```{code-block} python
 :caption: hello.py
@@ -48,7 +51,8 @@ def test_hello_world():
 
 ## Subcommands
 
-A subcommand name must be specified in the `args` parameter {meth}`CliRunner.invoke`:
+A subcommand name must be specified in the `args` parameter
+{meth}`CliRunner.invoke`:
 
 ```{code-block} python
 :caption: sync.py
@@ -81,7 +85,8 @@ def test_sync():
 
 ## Context Settings
 
-Additional keyword arguments passed to {meth}`CliRunner.invoke` will be used to construct the initial {class}`Context object <click.Context>`.
+Additional keyword arguments passed to {meth}`CliRunner.invoke` will be used to
+construct the initial {class}`Context object <click.Context>`.
 For example, setting a fixed terminal width equal to 60:
 
 ```{code-block} python
@@ -114,7 +119,8 @@ def test_sync():
 
 ## File System Isolation
 
-The {meth}`CliRunner.isolated_filesystem` context manager sets the current working directory to a new, empty folder.
+The {meth}`CliRunner.isolated_filesystem` context manager sets the current
+working directory to a new, empty folder.
 
 ```{code-block} python
 :caption: cat.py
@@ -167,7 +173,8 @@ def test_cat_with_path_specified():
 
 ## Input Streams
 
-The test wrapper can provide input data for the input stream (stdin). This is very useful for testing prompts.
+The test wrapper can provide input data for the input stream (stdin). This is
+very useful for testing prompts.
 
 ```{code-block} python
 :caption: prompt.py
diff --git a/src/click/testing.py b/src/click/testing.py
index 0e2e53a0d..04e7f1d92 100644
--- a/src/click/testing.py
+++ b/src/click/testing.py
@@ -479,9 +479,6 @@ def _patched_pdb_init(
             arguments are honored and not overridden. Debuggers that
             do not subclass ``pdb.Pdb`` (pudb, debugpy) are not
             covered.
-
-            See: https://github.com/pallets/click/issues/654 and
-            https://github.com/pallets/click/issues/824
             """
             if stdin is None:
                 stdin = sys.__stdin__

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 4a352253c9ff013e36d11e4a6820d36d00ff2cd4 -- docs/testing.md src/click/testing.py || true
