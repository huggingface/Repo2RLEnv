#!/bin/bash
set -uxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git checkout e4dbb3e8f4878a80a3a6b8380216f5f49eb7e6a7 -- .github/workflows/test-flask.yaml .github/workflows/tests.yaml || true
git apply --verbose --reject - <<'EOF_R2E_TEST_PATCH'
diff --git a/.github/workflows/test-flask.yaml b/.github/workflows/test-flask.yaml
deleted file mode 100644
index a15c21619..000000000
--- a/.github/workflows/test-flask.yaml
+++ /dev/null
@@ -1,25 +0,0 @@
-name: Test Flask Main
-on:
-  pull_request:
-    paths-ignore: ['docs/**', 'README.md']
-  push:
-    branches: [main, stable]
-    paths-ignore: ['docs/**', 'README.md']
-jobs:
-  flask-tests:
-    name: flask-tests
-    runs-on: ubuntu-latest
-    steps:
-      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
-        with:
-          enable-cache: true
-          prune-cache: false
-      - run: git clone https://github.com/pallets/flask
-      - run: uv venv --python 3.14
-        working-directory: ./flask
-      - run: source .venv/bin/activate
-        working-directory: ./flask
-      - run: uv sync --all-extras
-        working-directory: ./flask
-      - run: uv run --with "git+https://github.com/pallets/click.git@main" -- pytest
-        working-directory: ./flask
diff --git a/.github/workflows/tests.yaml b/.github/workflows/tests.yaml
index a4d247724..a5b1da3ea 100644
--- a/.github/workflows/tests.yaml
+++ b/.github/workflows/tests.yaml
@@ -5,6 +5,10 @@ on:
   push:
     branches: [main, stable]
     paths-ignore: ['docs/**', 'README.md']
+permissions: {}
+concurrency:
+  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
+  cancel-in-progress: true
 jobs:
   tests:
     name: ${{ matrix.name || matrix.python }}
@@ -14,48 +18,35 @@ jobs:
       matrix:
         include:
           - {python: '3.14'}
-          - {name: free-threaded-latest, python: '3.14t'}
+          - {python: '3.14t'}
+          - {name: Windows, python: '3.14', os: windows-latest}
+          - {name: Mac, python: '3.14', os: macos-latest}
           - {python: '3.13'}
-          - {name: Windows, python: '3.13', os: windows-latest}
-          - {name: Mac, python: '3.13', os: macos-latest}
           - {python: '3.12'}
           - {python: '3.11'}
           - {python: '3.10'}
           - {name: PyPy, python: 'pypy-3.11', tox: pypy3.11}
     steps:
       - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
-      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
         with:
-          enable-cache: true
-          prune-cache: false
-      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
-        with:
-          python-version: ${{ matrix.python }}
-      - run: uv run --locked tox run -e ${{ matrix.tox || format('py{0}', matrix.python) }}
-  stress:
-    name: stress (${{ matrix.name || matrix.python }})
-    runs-on: ${{ matrix.os || 'ubuntu-latest' }}
-    strategy:
-      fail-fast: false
-      matrix:
-        include:
-          - {python: '3.14'}
-          - {name: free-threaded, python: '3.14t', tox: stress-py3.14t}
-    steps:
-      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
-      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
+          persist-credentials: false
+      - uses: astral-sh/setup-uv@5a095e7a2014a4212f075830d4f7277575a9d098 # v7.3.1
         with:
           enable-cache: true
           prune-cache: false
       - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
         with:
           python-version: ${{ matrix.python }}
-      - run: uv run --locked tox run -e ${{ matrix.tox || format('stress-py{0}', matrix.python) }}
+      - run: uv run --locked --no-default-groups --group dev tox run
+        env:
+          TOX_ENV: ${{ matrix.tox || format('py{0}', matrix.python) }}
   typing:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
-      - uses: astral-sh/setup-uv@e06108dd0aef18192324c70427afc47652e63a82 # v7.5.0
+        with:
+          persist-credentials: false
+      - uses: astral-sh/setup-uv@5a095e7a2014a4212f075830d4f7277575a9d098 # v7.3.1
         with:
           enable-cache: true
           prune-cache: false
@@ -67,4 +58,4 @@ jobs:
         with:
           path: ./.mypy_cache
           key: mypy|${{ hashFiles('pyproject.toml') }}
-      - run: uv run --locked tox run -e typing
+      - run: uv run --locked --no-default-groups --group dev tox run -e typing

EOF_R2E_TEST_PATCH
: 'START_TEST_OUTPUT'
cd /workspace && pytest --collect-only
: 'END_TEST_OUTPUT'
git checkout e4dbb3e8f4878a80a3a6b8380216f5f49eb7e6a7 -- .github/workflows/test-flask.yaml .github/workflows/tests.yaml || true
