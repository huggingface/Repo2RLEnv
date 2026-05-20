"""AST function extractor for equivalence_tests — unit tests.

Each test feeds a small Python source string through extract_from_module
and checks that the right candidates survive.
"""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.pipelines._function_extractor import (
    _ast_has_side_effect,
    _is_forbidden_call,
    extract_from_module,
    walk_repo,
)


def _extract(source: str, *, min_loc: int = 1, max_loc: int = 200):
    return extract_from_module(
        Path("/dev/null"),
        source,
        relative_path="src/foo.py",
        min_loc=min_loc,
        max_loc=max_loc,
    )


# ---------------------------------------------------------------------------
# Basic acceptance
# ---------------------------------------------------------------------------


def test_extracts_simple_function():
    source = 'def add(x, y):\n    """Sum two ints."""\n    return x + y\n'
    out = _extract(source)
    assert len(out) == 1
    c = out[0]
    assert c.name == "add"
    assert c.arg_names == ("x", "y")
    assert c.docstring == "Sum two ints."
    assert "return x + y" in c.source


def test_skips_zero_arg_function():
    """Functions with no args can't be exercised by an equivalence test."""
    source = "def get():\n    return 42\n"
    assert _extract(source) == []


def test_skips_no_explicit_return():
    """Functions without `return <expr>` give no output to compare."""
    source = 'def shout(msg):\n    """Print loudly."""\n    print(msg.upper())\n'
    out = _extract(source)
    assert out == []


def test_skips_bare_return():
    """`return` without a value isn't useful for differential testing."""
    source = "def maybe(x):\n    if x:\n        return\n"
    assert _extract(source) == []


def test_skips_async_functions():
    source = "async def fetch(url):\n    return await get(url)\n"
    assert _extract(source) == []


# ---------------------------------------------------------------------------
# LOC range filter
# ---------------------------------------------------------------------------


def test_below_min_loc_filtered():
    source = "def f(x):\n    return x\n"  # body = 1 line
    assert _extract(source, min_loc=5, max_loc=100) == []


def test_above_max_loc_filtered():
    body = "\n".join(f"    x = x + {i}" for i in range(20))
    source = f"def f(x):\n{body}\n    return x\n"
    assert _extract(source, min_loc=1, max_loc=5) == []


def test_within_loc_range_kept():
    body = "\n".join(f"    x = x + {i}" for i in range(5))
    source = f"def f(x):\n{body}\n    return x\n"
    out = _extract(source, min_loc=5, max_loc=10)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Name filters
# ---------------------------------------------------------------------------


def test_skips_test_prefix():
    source = "def test_add(x, y):\n    return x + y\n"
    assert _extract(source) == []


def test_skips_underscore_prefix():
    source = "def _helper(x):\n    return x + 1\n"
    assert _extract(source) == []


def test_skips_main():
    source = "def main(argv):\n    return argv[0]\n"
    assert _extract(source) == []


def test_skips_dunder():
    source = "def __getitem__(self, idx):\n    return idx\n"
    # __getitem__ is on a class technically; even at module level it starts with _
    assert _extract(source) == []


# ---------------------------------------------------------------------------
# Side-effect heuristics
# ---------------------------------------------------------------------------


def test_skips_open():
    source = (
        "def read_text(path):\n"
        '    """Read a file."""\n'
        "    with open(path) as f:\n"
        "        return f.read()\n"
    )
    assert _extract(source) == []


def test_skips_subprocess():
    source = "def run_cmd(cmd):\n    import subprocess\n    return subprocess.run(cmd).returncode\n"
    assert _extract(source) == []


def test_skips_print():
    source = "def logging_helper(x):\n    print(x)\n    return x\n"
    assert _extract(source) == []


def test_pure_function_accepted():
    source = (
        "def total(items):\n"
        '    """Sum the items."""\n'
        "    n = 0\n"
        "    for x in items:\n"
        "        n = n + x\n"
        "    return n\n"
    )
    out = _extract(source)
    assert len(out) == 1
    assert out[0].name == "total"


# ---------------------------------------------------------------------------
# Multiple functions in one module
# ---------------------------------------------------------------------------


def test_picks_only_module_level():
    source = "def outer(x):\n    def inner(y):\n        return y * 2\n    return inner(x)\n"
    out = _extract(source)
    # Only `outer`, not `inner`
    assert [c.name for c in out] == ["outer"]


def test_picks_multiple_top_level():
    source = "def double(x):\n    return x * 2\n\ndef triple(x):\n    return x * 3\n"
    out = _extract(source)
    assert sorted(c.name for c in out) == ["double", "triple"]


def test_skips_class_methods_for_v0_7():
    """v0.7 scope: module-level only. Class methods need different handling."""
    source = "class Counter:\n    def inc(self, n):\n        return self.value + n\n"
    assert _extract(source) == []


# ---------------------------------------------------------------------------
# Source slicing
# ---------------------------------------------------------------------------


def test_source_includes_decorators():
    source = "@staticmethod\n@cache\ndef add(x, y):\n    return x + y\n"
    out = _extract(source)
    assert len(out) == 1
    assert "@staticmethod" in out[0].source
    assert "@cache" in out[0].source


def test_handles_syntax_error_gracefully():
    """Un-parseable source yields no candidates instead of raising."""
    source = "def f( :\n"  # invalid
    assert _extract(source) == []


# ---------------------------------------------------------------------------
# walk_repo
# ---------------------------------------------------------------------------


def test_walk_repo_respects_globs(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("def hello(x):\n    return x\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_hello(x):\n    return x\n")

    found = list(
        walk_repo(
            tmp_path,
            file_glob="**/*.py",
            exclude_glob=["tests/**"],
            min_loc=1,
            max_loc=50,
        )
    )
    paths = {c.relative_path for c in found}
    assert paths == {"src/a.py"}


def test_walk_repo_handles_unreadable_file(tmp_path: Path):
    """An I/O error on one file shouldn't blow up the whole walk."""
    (tmp_path / "good.py").write_text("def x(a):\n    return a + 1\n")
    # Create a binary file with invalid utf-8 — read_text uses errors="replace"
    (tmp_path / "binary.py").write_bytes(b"\x00\x01\x02\x03 not python")
    found = list(
        walk_repo(
            tmp_path,
            file_glob="*.py",
            exclude_glob=[],
            min_loc=1,
            max_loc=50,
        )
    )
    assert {c.name for c in found} == {"x"}


# ---------------------------------------------------------------------------
# AST side-effect detection (v0.8.3 Arc 7)
# ---------------------------------------------------------------------------


def _parse_fn(src: str):
    """Parse src and return the first function def found."""
    import ast as _ast

    tree = _ast.parse(src)
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef | _ast.AsyncFunctionDef):
            return node
    raise AssertionError(f"no function in {src!r}")


def test_ast_se_global_statement():
    fn = _parse_fn("def f(x):\n    global counter\n    return x + counter\n")
    se, kind = _ast_has_side_effect(fn)
    assert se and kind == "global"


def test_ast_se_yield_is_side_effect():
    fn = _parse_fn("def g(xs):\n    for x in xs:\n        yield x * 2\n")
    se, kind = _ast_has_side_effect(fn)
    assert se and kind == "yield"


def test_ast_se_yield_from():
    fn = _parse_fn("def g(xs):\n    yield from xs\n")
    se, kind = _ast_has_side_effect(fn)
    assert se and kind == "yield"


def test_ast_se_open_call():
    fn = _parse_fn("def f(p):\n    return open(p).read()\n")
    se, kind = _ast_has_side_effect(fn)
    assert se and kind == "forbidden_call"


def test_ast_se_dotted_os_call():
    fn = _parse_fn("def f(p):\n    return os.path.exists(p)\n")
    se, kind = _ast_has_side_effect(fn)
    assert se and kind == "forbidden_call"


def test_ast_se_pure_function_passes():
    fn = _parse_fn("def f(a, b):\n    return a * b + 1\n")
    se, _ = _ast_has_side_effect(fn)
    assert not se


def test_ast_se_does_not_descend_into_nested_fn():
    """A nested helper having side effects shouldn't disqualify the outer fn."""
    src = (
        "def outer(xs):\n"
        "    def _helper(x):\n"
        "        print(x)  # nested side effect\n"
        "        return x\n"
        "    return [_helper(x) for x in xs]\n"
    )
    fn = _parse_fn(src)
    se, _ = _ast_has_side_effect(fn)
    # The outer fn itself doesn't call print/etc., so it passes
    assert not se


def test_is_forbidden_call_reopen_no_false_positive():
    """`reopen(...)` should NOT match the `open` forbidden-call entry."""
    import ast as _ast

    tree = _ast.parse("def f(): reopen()")
    call = next(n for n in _ast.walk(tree) if isinstance(n, _ast.Call))
    assert _is_forbidden_call(call) is False


def test_pipeline_filters_global_statement():
    """End-to-end: a function with `global` is filtered out by the extractor."""
    src = "def f(x):\n    global g\n    return x + g\n"
    found = _extract(src, min_loc=1, max_loc=10)
    assert found == []


def test_pipeline_filters_generator():
    """End-to-end: generator functions are filtered out."""
    src = "def gen(xs):\n    for x in xs:\n        yield x * 2\n"
    found = _extract(src, min_loc=1, max_loc=10)
    assert found == []
