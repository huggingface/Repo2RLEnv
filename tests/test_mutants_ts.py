"""tree-sitter mutants for Go, Rust and JS/TS hidden tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_go")

from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines._mutants_ts import language_of, make_tree_sitter_mutants

GO = """package calc

import (
\t"testing"
)

func TestAdd(t *testing.T) {
\tcases := []struct {
\t\tname string
\t\tin   int
\t\twant int
\t}{{name: "small", in: 3, want: 3}, {name: "big one", in: 40, want: 40}}
\tfor _, c := range cases {
\t\tt.Run(c.name, func(t *testing.T) {
\t\t\tif got := Add(c.in, 0); got != c.want {
\t\t\t\tt.Errorf("got %d", got)
\t\t\t}
\t\t})
\t}
}

func TestSplit(t *testing.T) {
\tif got := len(Split("a,b")); got != 2 {
\t\tt.Fatal("split")
\t}
}

func TestBool(t *testing.T) {
\tif !Truthy(-1) {
\t\tt.Fatal("truthy")
\t}
}

func TestQuiet(t *testing.T) {
\tAdd(11, 11)
}
"""

GO_SOURCE = """package calc

import "strings"

func Add(a, b int) int { return a + b }

func Split(s string) []string { return strings.Split(s, ",") }

func Truthy(n int) bool { return n != 0 }
"""

RUST = """#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds() {
        assert_eq!(add(1, 2), 3);
    }

    #[test]
    fn compares() {
        assert!(add(1, 1) == 2, "math");
    }

    #[test]
    fn negated() {
        assert_ne!(add(1, 1), 3);
    }

    #[test]
    #[should_panic]
    fn panics() {
        assert_eq!(boom(), 1);
    }

    #[test]
    fn p2p() {
        assert_eq!(add(10, 12), 22);
    }
}
"""

JS = """const assert = require("node:assert");

describe("math", () => {
  it("adds", () => {
    expect(add(1, 2)).toBe(3);
  });
  it("differs", () => {
    expect(add(1, 1)).not.toEqual(3);
  });
  test(`node assert`, function () {
    assert.strictEqual(add(2, 2), 4);
  });
  it("snapshots", () => {
    expect(render()).toMatchSnapshot();
  });
  it("p2p", () => {
    expect(add(10, 15)).toBe(25);
  });
});
"""


def _make(path: str, source: str, targets: list[str], decoys: list[str] = ()):
    return make_tree_sitter_mutants(
        targets,
        lambda p: source if p == path else None,
        allowed_files={path},
        seed="s",
        decoys=list(decoys),
    )


def test_languages():
    assert language_of("pkg/a_test.go") == "go"
    assert language_of("pkg/a.go") is None
    assert language_of("tests/it.rs") == "rust"
    assert language_of("src/a.test.tsx") == "tsx"
    assert language_of("src/a.spec.mjs") == "javascript"


def test_go_changes_expected_values_never_the_comparison():
    targets = ["TestAdd/small", "TestAdd/big_one", "TestSplit", "TestBool", "TestQuiet"]
    mutants = _make("calc/calc_test.go", GO, targets, decoys=targets)
    assert mutants is not None
    # A named table case covers its own subtest; TestBool has no expected value
    # (a flipped `!` would be a negation); TestQuiet checks nothing.
    assert "TestSplit" in mutants.ids
    assert set(mutants.ids) <= {"TestAdd/small", "TestAdd/big_one", "TestSplit"}
    assert "TestBool" not in mutants.ids and "TestAdd" not in mutants.ids
    text = mutants.files()["calc/calc_test.go"]
    assert "got != 2 {" not in text and "got != " in text
    assert "if !Truthy(-1) {" in text and "==" not in text


@pytest.mark.skipif(shutil.which("go") is None, reason="needs go")
def test_go_mutants_fail_and_originals_pass(tmp_path: Path):
    targets = ["TestAdd/small", "TestAdd/big_one", "TestSplit"]
    mutants = _make("calc_test.go", GO, targets, decoys=[*targets, "TestQuiet"])
    (tmp_path / "go.mod").write_text("module calc\n\ngo 1.21\n")
    (tmp_path / "calc.go").write_text(GO_SOURCE)

    def run(test_source: str) -> dict[str, str]:
        (tmp_path / "calc_test.go").write_text(test_source)
        result = subprocess.run(
            ["go", "test", "-v", "-count=1", "./..."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                "PATH": f"{Path(shutil.which('go')).parent}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "GOPROXY": "off",
                "GOTOOLCHAIN": "local",
                "GOCACHE": str(tmp_path / ".cache"),
            },
        )
        return verifier.parse_go_test(result.stdout)

    original = run(GO)
    assert all(original[test] == "PASSED" for test in targets), original
    mutated = run(mutants.files()["calc_test.go"])
    assert all(mutated[test] == "FAILED" for test in mutants.ids), mutated
    assert all(mutated[test] == "PASSED" for test in mutants.decoy_ids), mutated


def test_rust_changes_the_literal_argument_and_skips_should_panic():
    targets = ["tests::adds", "tests::compares", "tests::negated", "tests::panics"]
    mutants = _make("src/lib.rs", RUST, targets, decoys=[*targets, "tests::p2p"])
    # assert_ne! is a negative check: no expected value to change.
    assert set(mutants.ids) == {"tests::adds", "tests::compares"}
    text = mutants.files()["src/lib.rs"]
    assert "assert_eq!(add(1, 2), 3);" not in text and "assert_eq!(add(1, 2), " in text
    assert 'assert!(add(1, 1) == 2, "math")' not in text and "assert!(add(1, 1) == " in text
    assert "assert_ne!" in text and text.count("assert_ne!") == 1
    assert "assert_eq!(boom(), 1);" in text
    assert "tests::p2p" in mutants.decoy_ids and not set(mutants.decoy_ids) & set(mutants.ids)


def test_js_changes_matcher_literals_skipping_not_and_snapshots():
    targets = [
        "src/math.test.js > math > adds",
        "src/math.test.js > math > differs",
        "src/math.test.js > math > node assert",
        "src/math.test.js > math > snapshots",
        "other.test.js > math > adds",
    ]
    mutants = _make(
        "src/math.test.js", JS, targets, decoys=[*targets, "src/math.test.js > math > p2p"]
    )
    assert set(mutants.ids) == {
        "src/math.test.js > math > adds",
        "src/math.test.js > math > node assert",
    }
    text = mutants.files()["src/math.test.js"]
    assert "expect(add(1, 2)).toBe(3);" not in text and "expect(add(1, 2)).toBe(" in text
    assert ".not.toEqual(" in text  # a negative check: at most a decoy
    assert "assert.strictEqual(add(2, 2), 4);" not in text
    assert "notStrictEqual" not in text and ".not.toBe" not in text
    assert "toMatchSnapshot()" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_js_mutants_still_parse(tmp_path: Path):
    mutants = _make("a.test.js", JS, ["math > adds", "math > node assert"], decoys=["math > p2p"])
    (tmp_path / "a.test.js").write_text(mutants.files()["a.test.js"])
    subprocess.run(["node", "--check", str(tmp_path / "a.test.js")], check=True)


def test_ambiguous_and_unknown_names_are_skipped():
    assert _make("a_test.go", GO, ["TestMissing"]) is None
    twice = make_tree_sitter_mutants(
        ["TestSplit"],
        lambda p: GO,
        allowed_files={"a/x_test.go", "b/y_test.go"},
        seed="s",
    )
    assert twice is None


def test_a_negated_bool_check_gets_no_mutant():
    source = (
        'package cast_test\n\nimport "testing"\n\n'
        'func TestTrue(t *testing.T) {\n\tif !cast.ToBool(-1) {\n\t\tt.Fatal("x")\n\t}\n}\n'
    )
    assert _make("x_test.go", source, ["TestTrue"]) is None
    literal = source.replace("if !cast.ToBool(-1) {", "if got := cast.ToBool(-1); got != true {")
    mutants = _make("x_test.go", literal, ["TestTrue"])
    assert "got != false {" in mutants.files()["x_test.go"]
