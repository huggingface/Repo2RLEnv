"""Rename detector — unit tests for commit-message regex + diff verification."""

from __future__ import annotations

from repo2rlenv.pipelines._rename_detector import (
    find_rename_in_message,
    verify_rename_in_diff,
)

# ---------------------------------------------------------------------------
# find_rename_in_message
# ---------------------------------------------------------------------------


def test_basic_rename():
    out = find_rename_in_message("Rename foo to bar")
    assert out == ("foo", "bar", "")


def test_rename_lowercase():
    out = find_rename_in_message("rename foo to bar")
    assert out == ("foo", "bar", "")


def test_renamed_past_tense():
    out = find_rename_in_message("Renamed old_helper to new_helper")
    assert out == ("old_helper", "new_helper", "")


def test_rename_with_kind_function():
    out = find_rename_in_message("Rename function do_thing to perform_action")
    assert out == ("do_thing", "perform_action", "function")


def test_rename_with_kind_class():
    out = find_rename_in_message("Rename class Foo to Bar")
    assert out == ("Foo", "Bar", "class")


def test_rename_with_arg_normalized():
    out = find_rename_in_message("rename arg x to value")
    assert out is not None
    assert out[2] == "argument"  # `arg` normalized → `argument`


def test_rename_with_param_normalized():
    out = find_rename_in_message("rename param x to value")
    assert out is not None
    assert out[2] == "parameter"


def test_rename_with_backticks():
    out = find_rename_in_message("rename method `do_thing` to `perform_action`")
    assert out == ("do_thing", "perform_action", "method")


def test_rename_inside_longer_message():
    out = find_rename_in_message(
        "Refactor: drop legacy helpers, rename helper to util. Closes #42."
    )
    assert out == ("helper", "util", "")


def test_no_match_for_unrelated_message():
    assert find_rename_in_message("Fix typo in docs") is None


def test_no_match_when_old_equals_new():
    assert find_rename_in_message("rename foo to foo") is None


def test_no_match_for_just_one_name():
    assert find_rename_in_message("rename foo") is None


# ---------------------------------------------------------------------------
# verify_rename_in_diff — happy paths
# ---------------------------------------------------------------------------


_SIMPLE_RENAME_DIFF = """\
diff --git a/src/lib.py b/src/lib.py
--- a/src/lib.py
+++ b/src/lib.py
@@ -1,3 +1,3 @@
-def old_name(x):
+def new_name(x):
     return x
"""


def test_diff_verify_happy_path():
    out = verify_rename_in_diff(_SIMPLE_RENAME_DIFF, old_name="old_name", new_name="new_name")
    assert out.ok
    assert out.reason == ""


def test_diff_verify_rejects_callsite_only_rename():
    """v0.8 requires a real def/class removal — call-site-only rename isn't a refactor."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-result = old_name(1, 2)\n"
        "+result = new_name(1, 2)\n"
    )
    out = verify_rename_in_diff(diff, old_name="old_name", new_name="new_name")
    assert not out.ok
    assert out.reason == "old_def_not_removed"


def test_diff_verify_allows_backcompat_shim():
    """Real-world renames keep a forwarding `def old(...)` — that's still a valid rename."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,3 +1,7 @@\n"
        "-def old_name(x):\n"
        "-    return x + 1\n"
        "+def new_name(x):\n"
        "+    return x + 1\n"
        "+\n"
        "+def old_name(x):\n"
        '+    """Deprecated: use new_name."""\n'
        "+    return new_name(x)\n"
    )
    out = verify_rename_in_diff(diff, old_name="old_name", new_name="new_name")
    assert out.ok


# ---------------------------------------------------------------------------
# verify_rename_in_diff — rejection paths
# ---------------------------------------------------------------------------


def test_diff_verify_rejects_empty_diff():
    assert verify_rename_in_diff("", old_name="x", new_name="y").reason == "empty_diff"


def test_diff_verify_rejects_when_old_def_not_removed():
    """No `-def old_name(...)` means this isn't a real symbol rename."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-something\n"
        "+def new_name(x): return x\n"
    )
    out = verify_rename_in_diff(diff, old_name="old_name", new_name="new_name")
    assert not out.ok
    assert out.reason == "old_def_not_removed"


def test_diff_verify_rejects_when_new_def_not_added():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-def old_name(x): return x\n"
        "+x = 1\n"
    )
    out = verify_rename_in_diff(diff, old_name="old_name", new_name="new_name")
    assert not out.ok
    assert out.reason == "new_def_not_added"


def test_diff_verify_class_rename():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-class OldName(Base):\n"
        "+class NewName(Base):\n"
        "     pass\n"
    )
    out = verify_rename_in_diff(diff, old_name="OldName", new_name="NewName")
    assert out.ok


def test_diff_verify_ignores_metadata_lines():
    """`--- a/old_name.py` shouldn't match as a removed `old_name` reference."""
    diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "--- a/old_name.py\n"
        "+++ b/new_name.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+y = 1\n"
    )
    # Neither side has a real def/class line; we reject for missing old def.
    out = verify_rename_in_diff(diff, old_name="old_name", new_name="new_name")
    assert not out.ok
