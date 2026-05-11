#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout 3299ba1a8a5de34b24a7226a683a837d8a0857e7 -- .github/workflows/test-flask.yaml .github/workflows/tests.yaml || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/.github/workflows/test-flask.yaml b/.github/workflows/test-flask.yaml
index 9fe4785f7..a15c21619 100644
--- a/.github/workflows/test-flask.yaml
+++ b/.github/workflows/test-flask.yaml
@@ -10,7 +10,7 @@ jobs:
     name: flask-tests
     runs-on: ubuntu-latest
     steps:
-      - uses: astral-sh/setup-uv@5a7eac68fb9809dea845d802897dc5c723910fa3 # v7.1.3
+      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
         with:
           enable-cache: true
           prune-cache: false
diff --git a/.github/workflows/tests.yaml b/.github/workflows/tests.yaml
index 9a6d0395d..a4d247724 100644
--- a/.github/workflows/tests.yaml
+++ b/.github/workflows/tests.yaml
@@ -23,12 +23,12 @@ jobs:
           - {python: '3.10'}
           - {name: PyPy, python: 'pypy-3.11', tox: pypy3.11}
     steps:
-      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
-      - uses: astral-sh/setup-uv@5a7eac68fb9809dea845d802897dc5c723910fa3 # v7.1.3
+      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
+      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
         with:
           enable-cache: true
           prune-cache: false
-      - uses: actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c # v6.0.0
+      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
         with:
           python-version: ${{ matrix.python }}
       - run: uv run --locked tox run -e ${{ matrix.tox || format('py{0}', matrix.python) }}
@@ -42,28 +42,28 @@ jobs:
           - {python: '3.14'}
           - {name: free-threaded, python: '3.14t', tox: stress-py3.14t}
     steps:
-      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
-      - uses: astral-sh/setup-uv@5a7eac68fb9809dea845d802897dc5c723910fa3 # v7.1.3
+      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
+      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
         with:
           enable-cache: true
           prune-cache: false
-      - uses: actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c # v6.0.0
+      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
         with:
           python-version: ${{ matrix.python }}
       - run: uv run --locked tox run -e ${{ matrix.tox || format('stress-py{0}', matrix.python) }}
   typing:
     runs-on: ubuntu-latest
     steps:
-      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
-      - uses: astral-sh/setup-uv@5a7eac68fb9809dea845d802897dc5c723910fa3 # v7.1.3
+      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
+      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
         with:
           enable-cache: true
           prune-cache: false
-      - uses: actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c # v6.0.0
+      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
         with:
           python-version-file: pyproject.toml
       - name: cache mypy
-        uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830 # v4.3.0
+        uses: actions/cache@cdf6c1fa76f9f475f3d7449005a359c84ca0f306 # v5.0.3
         with:
           path: ./.mypy_cache
           key: mypy|${{ hashFiles('pyproject.toml') }}

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout 3299ba1a8a5de34b24a7226a683a837d8a0857e7 -- .github/workflows/test-flask.yaml .github/workflows/tests.yaml || true
