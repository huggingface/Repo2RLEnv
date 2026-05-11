#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 30a7f7c8e6cf40c1a09f22ac83377c6ae1d201b5 -- tests/test_imports.py || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/tests/test_imports.py b/tests/test_imports.py
index 917b245f29..74b78642bc 100644
--- a/tests/test_imports.py
+++ b/tests/test_imports.py
@@ -27,6 +27,7 @@ def tracking_import(module, locals=None, globals=None, fromlist=None,
 
 ALLOWED_IMPORTS = {
     "__future__",
+    "abc",
     "codecs",
     "collections",
     "collections.abc",
@@ -49,6 +50,7 @@ def tracking_import(module, locals=None, globals=None, fromlist=None,
     "threading",
     "types",
     "typing",
+    "uuid",
     "weakref",
 }
 

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 30a7f7c8e6cf40c1a09f22ac83377c6ae1d201b5 -- tests/test_imports.py || true
