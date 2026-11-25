"""Mutants for Go, Rust and JS/TS hidden tests (see `_mutants` for the method).

tree-sitter finds the test function and the exact byte span of the expected
value in one assertion at the top level of the test body (before any branch or
loop); the edit changes that value and keeps its shape (`_mutants.changed_literal`):

* Go: the literal side of `if got != want { t.Error... }`, the expected
  argument of testify `assert.Equal` / `require.Equal` / `EqualValues`, and the
  `want` field of a named case in a table-driven test (that subtest's ID only).
* Rust: the literal argument of `assert_eq!`, the literal side of `==` in `assert!`.
  `#[should_panic]` and `#[ignore]` tests are skipped.
* JS/TS: the literal argument of `expect(x).toBe/toEqual/toStrictEqual(...)`
  (not under `.not`), chai `expect(x).to.equal/eql(...)`, and node:assert
  `strictEqual` / `deepStrictEqual` / `equal` / `deepEqual`. Snapshot tests are skipped.

No literal, no mutant: a flipped comparison is the plain negation of the test,
which a value whose equality always holds (a Rust `PartialEq` or a jest
asymmetric matcher) passes exactly like an honest fix. Decoys respell one
literal of another test with the same value (`_mutants.same_value_literal`).

Mutation-testing tools (cargo-mutants, go-mutesting, StrykerJS) skip test code
by design, so nothing upstream does this. Every edit is checked to parse, then
validated by running it at generation.

Generation only: the task's verifier never imports this module or tree-sitter.
Install with the `mutants` extra; without it, tasks ship with no mutants.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from collections.abc import Callable, Iterator
from functools import cache

from repo2rlenv.pipelines._mutants import (
    MAX_DECOYS,
    MAX_MUTANTS,
    Edit,
    Mutants,
    _reserve_a_decoy,
    apply_edits,
    changed_literal,
    same_value_literal,
)

logger = logging.getLogger(__name__)

_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
_TS_SUFFIXES = (".ts", ".mts", ".cts")
_EXPECT_MATCHERS = {"toBe", "toEqual", "toStrictEqual"}
_CHAI_MATCHERS = {"equal", "eql"}
_NODE_ASSERT = {"strictEqual", "deepStrictEqual", "equal", "deepEqual"}
_TESTIFY = {"Equal", "EqualValues"}
_GO_WANT = {"want", "expected", "expect", "exp", "out", "wantOut"}
_GO_NAME = {"name", "desc", "title", "scenario"}
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_LITERALS = {
    "go": {
        "int_literal": "number",
        "float_literal": "number",
        "interpreted_string_literal": "string",
        "true": "bool",
        "false": "bool",
    },
    "rust": {
        "integer_literal": "number",
        "float_literal": "number",
        "string_literal": "string",
        "boolean_literal": "bool",
    },
    "js": {"number": "number", "string": "string", "true": "bool", "false": "bool"},
}
_STOPS = {
    "go": {
        "return_statement",
        "for_statement",
        "expression_switch_statement",
        "type_switch_statement",
        "select_statement",
        "go_statement",
        "defer_statement",
        "labeled_statement",
        "goto_statement",
        "if_statement",
    },
    "rust": {
        "if_expression",
        "match_expression",
        "for_expression",
        "while_expression",
        "loop_expression",
        "return_expression",
    },
    "js": {
        "return_statement",
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "try_statement",
        "switch_statement",
    },
}


def language_of(path: str) -> str | None:
    if path.endswith("_test.go"):
        return "go"
    if path.endswith(".rs"):
        return "rust"
    if path.endswith(_JS_SUFFIXES):
        return "javascript"
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith(_TS_SUFFIXES):
        return "typescript"
    return None


def _family(language: str) -> str:
    return language if language in ("go", "rust") else "js"


def _parser(language: str):
    """A tree-sitter parser, or None when the `mutants` extra isn't installed."""
    try:
        from tree_sitter import Language, Parser

        if language == "go":
            import tree_sitter_go as grammar

            return Parser(Language(grammar.language()))
        if language == "rust":
            import tree_sitter_rust as grammar

            return Parser(Language(grammar.language()))
        if language == "javascript":
            import tree_sitter_javascript as grammar

            return Parser(Language(grammar.language()))
        import tree_sitter_typescript as grammar

        loader = grammar.language_tsx if language == "tsx" else grammar.language_typescript
        return Parser(Language(loader()))
    except ImportError:
        return None


def _walk(node) -> Iterator:
    yield node
    for child in node.children:
        yield from _walk(child)


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


# --- finding test functions ------------------------------------------------


def _go_tests(root, source: bytes) -> dict[tuple[str, ...], object]:
    """`TestX` -> its body block."""
    out = {}
    for node in root.children:
        if node.type != "function_declaration":
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is not None and body is not None and _text(name, source).startswith("Test"):
            out[(_text(name, source),)] = body
    return out


def _rust_tests(root, source: bytes) -> dict[tuple[str, ...], object]:
    """`fn name` under `#[test]` (not should_panic / ignore) -> its body."""
    out = {}
    for node in _walk(root):
        if node.type != "function_item":
            continue
        attributes = []
        sibling = node.prev_named_sibling
        while sibling is not None and sibling.type == "attribute_item":
            attributes.append(_text(sibling, source))
            sibling = sibling.prev_named_sibling
        if not any(a.replace(" ", "") in ("#[test]", "#[tokio::test]") for a in attributes):
            continue
        if any("should_panic" in a or "ignore" in a for a in attributes):
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is not None and body is not None:
            key = (_text(name, source),)
            out[key] = None if key in out else body  # duplicate names: ambiguous
    return {key: body for key, body in out.items() if body is not None}


def _string_value(node, source: bytes) -> str | None:
    if node.type == "string":
        return _text(node, source)[1:-1]
    if node.type == "template_string" and not any(
        child.type == "template_substitution" for child in node.children
    ):
        return _text(node, source)[1:-1]
    return None


def _js_tests(root, source: bytes) -> dict[tuple[str, ...], object]:
    """(describe titles..., test title) -> the test's callback."""
    out: dict[tuple[str, ...], object] = {}

    def visit(node, chain: tuple[str, ...]) -> None:
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            name = _text(function, source) if function is not None else ""
            named = [child for child in (arguments.named_children if arguments else [])]
            title = _string_value(named[0], source) if named else None
            if name in ("describe", "it", "test") and title is not None and len(named) >= 2:
                callback = named[1]
                if name == "describe":
                    for child in callback.children:
                        visit(child, (*chain, title))
                    return
                key = (*chain, title)
                out[key] = None if key in out else callback
                return
        for child in node.children:
            visit(child, chain)

    visit(root, ())
    return {key: body for key, body in out.items() if body is not None}


# --- the statements that always run ------------------------------------------


def _statements(body, family: str) -> list:
    """Top-level statements of a test body, up to the first branch, loop or return."""
    if family == "js":
        block = body.child_by_field_name("body") if body.type != "statement_block" else body
        if block is None or block.type != "statement_block":
            return []
        children = block.named_children
    elif family == "go":
        lists = [child for child in body.named_children if child.type == "statement_list"]
        children = lists[0].named_children if lists else body.named_children
    else:
        children = body.named_children
    out = []
    for statement in children:
        inner = statement.named_children[0] if statement.named_children else statement
        if statement.type in _STOPS[family] or inner.type in _STOPS[family]:
            if family == "go" and statement.type == "if_statement" and _go_reports(statement):
                out.append(statement)  # an assertion: `if got != want { t.Errorf }`
                continue
            break
        out.append(statement)
    return out


def _go_reports(statement) -> bool:
    consequence = statement.child_by_field_name("consequence")
    if consequence is None or statement.child_by_field_name("alternative") is not None:
        return False
    text = consequence.text.decode("utf-8", "replace") if consequence.text else ""
    return ".Error" in text or ".Fatal" in text


def _literal_kind(node, family: str) -> str | None:
    kind = _LITERALS[family].get(node.type)
    if kind == "string" and family == "js" and node.type != "string":
        return None
    return kind


# --- choosing the edit -----------------------------------------------------

# A target is (start byte, end byte, literal kind, test IDs it covers or None
# for "every ID of the function").
_Target = tuple[int, int, str, list[str] | None]


def _go_targets(body, source: bytes, test: str, ids: list[str]) -> list[_Target]:
    out: list[_Target] = []
    for statement in _statements(body, "go"):
        if statement.type == "if_statement":
            condition = statement.child_by_field_name("condition")
            if condition is not None and condition.type == "binary_expression":
                operator = condition.child_by_field_name("operator")
                if operator is not None and operator.type == "!=":
                    for side in ("right", "left"):
                        node = condition.child_by_field_name(side)
                        kind = _literal_kind(node, "go") if node is not None else None
                        if kind is not None:
                            out.append((node.start_byte, node.end_byte, kind, None))
                            break
        elif statement.type == "expression_statement":
            call = statement.named_children[0] if statement.named_children else None
            function = call.child_by_field_name("function") if call is not None else None
            if call is not None and call.type == "call_expression" and function is not None:
                operand = function.child_by_field_name("operand")
                field = function.child_by_field_name("field")
                arguments = call.child_by_field_name("arguments")
                if (
                    function.type == "selector_expression"
                    and operand is not None
                    and field is not None
                    and _text(operand, source) in ("assert", "require")
                    and _text(field, source) in _TESTIFY
                    and arguments is not None
                    and len(arguments.named_children) >= 3
                ):
                    expected = arguments.named_children[1]
                    kind = _literal_kind(expected, "go")
                    if kind is not None:
                        out.append((expected.start_byte, expected.end_byte, kind, None))
        if statement.type in ("short_var_declaration", "var_declaration"):
            out += _go_table_targets(statement, source, test, ids)
    return out


def _go_table_targets(statement, source: bytes, test: str, ids: list[str]) -> list[_Target]:
    """`want` literals of named cases in a table literal: each covers one subtest."""
    out: list[_Target] = []
    for node in _walk(statement):
        if node.type != "literal_value":
            continue
        fields: dict[str, object] = {}
        for element in node.named_children:
            if element.type != "keyed_element" or len(element.named_children) < 2:
                continue
            key, value = element.named_children[0], element.named_children[-1]
            value = value.named_children[0] if value.named_children else value
            fields[_text(key, source).strip()] = value
        name = next((fields[k] for k in _GO_NAME if k in fields), None)
        want = next((fields[k] for k in _GO_WANT if k in fields), None)
        if name is None or want is None or name.type != "interpreted_string_literal":
            continue
        kind = _literal_kind(want, "go")
        subtest = f"{test}/{_text(name, source)[1:-1].replace(' ', '_')}"
        covered = [name for name in ids if name.split("::", 1)[-1] == subtest]
        if kind is not None and covered:
            out.append((want.start_byte, want.end_byte, kind, covered))
    return out


def _macro_args(tokens) -> list[list]:
    """A macro's token tree split at its top-level commas."""
    args: list[list] = [[]]
    for token in tokens.children[1:-1]:
        if token.type == ",":
            args.append([])
        else:
            args[-1].append(token)
    return [arg for arg in args if arg]


def _single_literal(tokens: list, family: str):
    if len(tokens) == 1 and _literal_kind(tokens[0], family):
        return tokens[0]
    return None


def _rust_targets(body, source: bytes) -> list[_Target]:
    out: list[_Target] = []
    for statement in _statements(body, "rust"):
        macro = statement.named_children[0] if statement.named_children else None
        if macro is None or macro.type != "macro_invocation":
            continue
        name = macro.child_by_field_name("macro")
        tokens = next((c for c in macro.children if c.type == "token_tree"), None)
        if name is None or tokens is None:
            continue
        args = _macro_args(tokens)
        found = None
        if _text(name, source) == "assert_eq" and len(args) >= 2:
            found = _single_literal(args[1], "rust") or _single_literal(args[0], "rust")
        elif _text(name, source) == "assert" and args:
            sides: list[list] = [[]]
            for token in args[0]:
                if token.type == "==":
                    sides.append([])
                else:
                    sides[-1].append(token)
            if len(sides) == 2:
                found = _single_literal(sides[1], "rust") or _single_literal(sides[0], "rust")
        if found is not None:
            out.append((found.start_byte, found.end_byte, _literal_kind(found, "rust"), None))
    return out


def _chain_root(node, source: bytes) -> str:
    """The callee at the bottom of `a(...).b.c(...)`."""
    while node is not None:
        if node.type == "call_expression":
            node = node.child_by_field_name("function")
        elif node.type == "member_expression":
            node = node.child_by_field_name("object")
        else:
            return _text(node, source)
    return ""


def _js_targets(callback, source: bytes) -> list[_Target]:
    text = _text(callback, source)
    if "toMatchSnapshot" in text or "toMatchInlineSnapshot" in text:
        return []
    out: list[_Target] = []
    for statement in _statements(callback, "js"):
        call = statement.named_children[0] if statement.named_children else None
        if call is None or call.type != "call_expression":
            continue
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or function.type != "member_expression" or arguments is None:
            continue
        obj = function.child_by_field_name("object")
        matcher = _text(function.child_by_field_name("property"), source)
        named = arguments.named_children
        expected = None
        if obj is not None and obj.type == "identifier" and _text(obj, source) == "assert":
            if matcher in _NODE_ASSERT and len(named) >= 2:
                expected = named[1]
        elif obj is not None and _chain_root(obj, source) == "expect" and len(named) == 1:
            chain = _text(obj, source)
            negated = ".not" in chain.replace(" ", "")
            chai = matcher in _CHAI_MATCHERS and chain.replace(" ", "").endswith(".to")
            if not negated and (matcher in _EXPECT_MATCHERS or chai):
                expected = named[0]
        kind = _literal_kind(expected, "js") if expected is not None else None
        if kind is not None:
            out.append((expected.start_byte, expected.end_byte, kind, None))
    return out


def _decoy_targets(body, family: str) -> list[tuple[int, int, str]]:
    return [
        (node.start_byte, node.end_byte, _literal_kind(node, family))
        for node in _walk(body)
        if _literal_kind(node, family) is not None
    ]


_FINDERS = {"go": _go_tests, "rust": _rust_tests, "js": _js_tests}


# --- test IDs ----------------------------------------------------------------


def _key_for(test_id: str, path: str, language: str) -> tuple[str, ...] | None:
    """The function key a runner-reported test ID belongs to in `path`, if any."""
    if language == "go":
        return (test_id.split("::", 1)[-1].split("/", 1)[0],)
    if language == "rust":
        name = test_id.split(" - ", 1)[0].rsplit("::", 1)[-1].strip()
        return (name,) if name.isidentifier() else None
    parts = [part.strip() for part in test_id.split(" > ")]
    if len(parts) > 1 and ("/" in parts[0] or language_of(parts[0])):
        if not (path.endswith(parts[0]) or parts[0].endswith(path)):
            return None
        parts = parts[1:]
    return tuple(parts)


def _has_error(parser, text: str) -> bool:
    return parser.parse(text.encode("utf-8")).root_node.has_error


def _go_package(path: str, read: Callable[[str], str | None]) -> str | None:
    """Resolve a test's import path from its nearest go.mod, without running Go."""
    directory = path.rpartition("/")[0]
    root = directory
    while True:
        module = read(f"{root}/go.mod" if root else "go.mod")
        if module is not None:
            match = re.search(r'^\s*module\s+(?:"([^"\s]+)"|([^\s"`]+))', module, re.M)
            if match is None:
                return None
            suffix = directory[len(root) :].strip("/") if root else directory
            return (match[1] or match[2]).rstrip("/") + ("/" + suffix if suffix else "")
        if not root:
            return None
        root = root.rpartition("/")[0]


def make_tree_sitter_mutants(
    targets: list[str],
    read: Callable[[str], str | None],
    *,
    allowed_files: set[str],
    seed: str,
    limit: int = MAX_MUTANTS,
    decoys: list[str] = (),
    decoy_limit: int = MAX_DECOYS,
) -> Mutants | None:
    """Mutants for up to `limit` fail-to-pass Go/Rust/JS/TS `targets`, plus decoys; or None.

    `allowed_files` are the hidden test patch's files; a test name that occurs
    in more than one of them is skipped as ambiguous.
    """
    files = sorted(path for path in allowed_files if language_of(path))
    if not files:
        return None
    sources: dict[str, str] = {}
    tests: dict[str, dict[tuple[str, ...], object]] = {}
    go_packages: dict[str, str | None] = {}
    read_module = cache(read)
    parsers = {}
    for path in files:
        language = language_of(path)
        if language == "go":
            go_packages[path] = _go_package(path, read_module)
        parser = _parser(language)
        if parser is None:
            logger.info(
                "mutants: tree-sitter not installed (repo2rlenv[mutants]); none for %s", path
            )
            return None
        source = read(path)
        if source is None:
            continue
        parsers[path] = parser
        sources[path] = source
        data = source.encode("utf-8")
        tests[path] = _FINDERS[_family(language)](parser.parse(data).root_node, data)

    def group(tests_ids: list[str]) -> dict[tuple[str, tuple[str, ...]], list[str]]:
        out: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for test in tests_ids:
            owners = []
            for path, found in tests.items():
                if (
                    path in go_packages
                    and "::" in test
                    and test.split("::", 1)[0] != go_packages[path]
                ):
                    continue
                key = _key_for(test, path, language_of(path))
                if key is not None and key in found:
                    owners.append((path, key))
            if len(owners) == 1:
                out.setdefault(owners[0], []).append(test)
        return out

    wanted = group(list(targets))
    if not wanted:
        return None
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    words = {}
    for path, text in sources.items():
        family = _family(language_of(path))
        data = text.encode("utf-8")
        strings = [
            _text(node, data)
            for node in _walk(parsers[path].parse(data).root_node)
            if _literal_kind(node, family) == "string"
        ]
        # Words from the file's own strings, so a changed message still reads naturally.
        words[path] = sorted(
            set(_WORD_RE.findall(" ".join(strings))) or set(_WORD_RE.findall(text))
        )
    edits: list[Edit] = []
    edited: set[tuple[str, tuple[str, ...]]] = set()
    origin: dict[str, tuple[str, tuple[str, ...], list[str]]] = {}
    ids_by_function: dict[str, list[str]] = {}

    def add(path: str, function: str, start: int, end: int, text: str, ids, kind: str) -> bool:
        candidate = Edit(path, function, start, end, text, kind)
        mine = [e for e in edits if e.path == path]
        if any(e.start < end and start < e.end for e in mine):
            return False
        if _has_error(parsers[path], apply_edits(sources[path], [*mine, candidate])):
            return False
        edits.append(candidate)
        ids_by_function[function] = sorted(ids)
        return True

    def mutate(path: str, key: tuple[str, ...], ids: list[str]) -> bool:
        language = language_of(path)
        family = _family(language)
        data = sources[path].encode("utf-8")
        body = tests[path][key]
        if family == "go":
            found = _go_targets(body, data, key[0], ids)
        elif family == "rust":
            found = _rust_targets(body, data)
        else:
            found = _js_targets(body, data)
        rng.shuffle(found)
        function = f"{path}::{' > '.join(key)}"
        for start, end, kind, covered in found:
            token = data[start:end].decode("utf-8")
            text = changed_literal(token, kind, rng, words[path])
            if text is None or text == token:
                continue
            if covered is None:  # a parent-level check: not its subtests
                covered = [test for test in ids if "/" not in test.split("::", 1)[-1]] or ids
            name = function if covered == ids else f"{function} > {covered[0]}"
            if add(path, name, start, end, text, covered, "mutant"):
                edited.add((path, key))
                origin[name] = (path, key, ids)
                return True
        return False

    def decoy(path: str, key: tuple[str, ...], ids: list[str]) -> bool:
        family = _family(language_of(path))
        data = sources[path].encode("utf-8")
        found = _decoy_targets(tests[path][key], family)
        rng.shuffle(found)
        for start, end, kind in found:
            token = data[start:end].decode("utf-8")
            text = same_value_literal(token, kind, rng, family)
            if (
                text is not None
                and text != token
                and add(path, f"{path}::{' > '.join(key)}", start, end, text, ids, "decoy")
            ):
                return True
        return False

    order = sorted(wanted)
    rng.shuffle(order)
    for path, key in order:
        if sum(e.kind == "mutant" for e in edits) >= limit:
            break
        mutate(path, key, wanted[(path, key)])
    if not edits:
        return None
    pool = group(list(decoys))
    order = [k for k in sorted(pool) if k not in edited]
    rng.shuffle(order)
    for path, key in order:
        if sum(e.kind == "decoy" for e in edits) >= decoy_limit:
            break
        decoy(path, key, pool[(path, key)])
    _reserve_a_decoy(edits, ids_by_function, origin, decoy)
    return Mutants(dict(sources), edits, ids_by_function)
