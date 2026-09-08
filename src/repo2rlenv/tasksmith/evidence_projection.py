"""Reversible final-dossier projection; no task code execution or semantic summaries.

All original roles remain readable. Long JSON strings appear once in the required
``shared_texts`` document. ``@tN`` tokens reference that document; line edits refer
only to earlier visible entries. Receipt metadata preserves JSON whitespace and
lexical spelling so reconstruction recovers the exact original UTF-8 texts.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

POLICY = "reversible-dossier-v2"
_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|@[a-z0-9]+|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\]:,]'
)
_MIN_STRING = 256


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _pairs(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Duplicate JSON keys must remain verbatim")
    return dict(pairs)


def _nonfinite(value):
    raise ValueError(f"Nonstandard JSON value {value} stays verbatim")


def _documents(text):
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_nonfinite)
    cursor = 0
    for match in re.finditer(r"(?m)^[\[{]", text):
        start = match.start()
        if start < cursor:
            continue
        try:
            _, length = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        end = start + length
        yield cursor, start, end
        cursor = end
    yield cursor, len(text), len(text)


def _edits(before, after):
    left, right = before.splitlines(True), after.splitlines(True)
    return [
        [i, j, "".join(right[a:b])]
        for tag, i, j, a, b in difflib.SequenceMatcher(
            None, left, right, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]


def _apply(before, edits):
    lines = before.splitlines(True)
    cursor, parts = 0, []
    for start, end, text in edits:
        if (
            type(start) is not int
            or type(end) is not int
            or not cursor <= start <= end <= len(lines)
        ):
            raise ValueError("Invalid reconstruction line edits")
        parts.extend(["".join(lines[cursor:start]), text])
        cursor = end
    return "".join([*parts, "".join(lines[cursor:])])


def project_dossier(texts: dict[str, str]) -> tuple[dict[str, str], dict]:
    """Project captured texts; caller must require complete reads of ALL output roles.

    Never use the receipt as read coverage. Keep raw artifacts and commit both this
    receipt and projected texts under the same source/task identity before review.
    """
    if not texts or "shared_texts" in texts or any(not isinstance(v, str) for v in texts.values()):
        raise ValueError("Expected named text evidence without reserved shared_texts role")
    pool, entries, values, seen = [], {}, {}, {}
    pool_length = 0

    def intern(value, depth=0):
        nonlocal pool_length
        if value in seen:
            return seen[value]
        content, kind, base = value, "literal", None
        documents = None
        if depth < 4 and value.startswith(("{", "[")):
            try:
                parsed = json.loads(value, object_pairs_hook=_pairs, parse_constant=_nonfinite)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)) and any(
                token.group().startswith('"') and len(json.loads(token.group())) >= _MIN_STRING
                for token in _TOKEN.finditer(value)
            ):
                content, documents = encode_text(value, intern_plain=False, depth=depth + 1)
                kind = "nested_json"
        # Bounded comparison: recent entries plus recent similarly sized entries.
        candidates = list(values)[-12:]
        candidates += [k for k, v in values.items() if abs(len(v) - len(value)) < len(value) // 3][
            -12:
        ]
        for candidate in () if documents is not None else dict.fromkeys(candidates):
            if "\n" not in value or "\n" not in values[candidate]:
                continue
            encoded = _json({"base": candidate, "line_edits": _edits(values[candidate], value)})
            if len(encoded) < len(content):
                content, kind, base = encoded, "line_edits", candidate
        name = f"t{len(entries)}"
        # Hashes and lengths remain in the authenticated receipt, not repeated
        # inside every model-visible block heading.
        heading = f"\n{name} {kind}\n"
        start = pool_length + len(heading)
        pool.extend([heading, content, "\n"])
        pool_length = start + len(content) + 1
        entries[name] = {
            "start": start,
            "end": start + len(content),
            "kind": kind,
            "base": base,
            "sha256": _sha(value),
            **({"documents": documents} if documents is not None else {}),
        }
        values[name], seen[value] = value, name
        return name

    def encode_text(original, *, intern_plain=True, depth=0):
        parts, documents, length = [], [], 0
        for cursor, start, end in _documents(original):
            plain = original[cursor:start]
            if intern_plain and len(plain) >= _MIN_STRING:
                reference = intern(plain, depth + 1)
                marker = f"@{reference}"
                documents.append(
                    {
                        "kind": "text_ref",
                        "name": reference,
                        "start": length,
                        "end": length + len(marker),
                    }
                )
                parts.append(marker)
                length += len(marker)
            else:
                parts.append(plain)
                length += len(plain)
            if start == end:
                continue
            raw = original[start:end]
            tokens = list(_TOKEN.finditer(raw))
            gaps, overrides, encoded, offset = [], {}, [], 0
            for index, token in enumerate(tokens):
                gap = raw[offset : token.start()]
                if gap.strip():
                    raise ValueError("Unrecognized JSON lexical content")
                gaps.append(gap)
                lexical = token.group()
                if lexical.startswith('"'):
                    value = json.loads(lexical)
                    if len(value) >= _MIN_STRING:
                        encoded.append("@" + intern(value, depth + 1))
                        if lexical != _json(value):
                            overrides[str(index)] = lexical
                    else:
                        encoded.append(lexical)
                else:
                    encoded.append(lexical)
                offset = token.end()
            gaps.append(raw[offset:])
            compact = "".join(encoded)
            documents.append(
                {
                    "start": length,
                    "end": length + len(compact),
                    "gaps": gaps,
                    "lexical_overrides": overrides,
                }
            )
            parts.append(compact)
            length += len(compact)
        return "".join(parts), documents

    projected, roles = {}, {}
    for name, original in sorted(texts.items()):
        projected[name], documents = encode_text(original)
        roles[name] = {
            "original_sha256": _sha(original),
            "original_characters": len(original),
            "documents": documents,
        }
    projected["shared_texts"] = (
        "Required shared evidence. @tN in JSON expands to this entry as a JSON string; "
        "standalone @tN expands to the verbatim text entry. "
        "nested_json entries likewise expand their @tN JSON-string references recursively. "
        "Line edits [start,end,text] replace zero-based lines of the named earlier entry; "
        "all unchanged text is inherited. Every entry remains required reading.\n" + "".join(pool)
    )
    prefix = len(projected["shared_texts"]) - pool_length
    for entry in entries.values():
        entry["start"] += prefix
        entry["end"] += prefix
    receipt = {
        "policy": POLICY,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "roles": roles,
        "shared": entries,
        "projected_sha256": {k: _sha(v) for k, v in projected.items()},
    }
    # Check every unique byte of source evidence before returning a projection.
    if restore_dossier(projected, receipt) != texts:
        raise ValueError("Projection did not preserve original evidence")
    return projected, receipt


def restore_dossier(projected: dict[str, str], receipt: dict) -> dict[str, str]:
    """Authenticate projection and reconstruct original text, failing on changed evidence."""
    if receipt.get("policy") not in {POLICY, "reversible-dossier-v1"} or set(projected) != set(
        receipt["roles"]
    ) | {"shared_texts"}:
        raise ValueError("Projection policy or role inventory changed")
    if {k: _sha(v) for k, v in projected.items()} != receipt["projected_sha256"]:
        raise ValueError("Projected evidence hash mismatch")
    values = {}
    for name, entry in sorted(receipt["shared"].items(), key=lambda item: item[1]["start"]):
        content = projected["shared_texts"][entry["start"] : entry["end"]]
        if entry["kind"] == "line_edits":
            patch = json.loads(content)
            if patch["base"] != entry["base"] or patch["base"] not in values:
                raise ValueError("Unknown or forward shared reference")
            value = _apply(values[patch["base"]], patch["line_edits"])
        elif entry["kind"] == "nested_json":
            value = _restore_text(content, entry["documents"], values)
        elif entry["kind"] == "literal":
            value = content
        else:
            raise ValueError("Unsupported shared evidence encoding")
        if _sha(value) != entry["sha256"]:
            raise ValueError("Reconstructed shared evidence hash mismatch")
        values[name] = value
    restored = {}
    for name, role in receipt["roles"].items():
        original = _restore_text(projected[name], role["documents"], values)
        if (
            len(original) != role["original_characters"]
            or _sha(original) != role["original_sha256"]
        ):
            raise ValueError("Reconstructed role evidence hash mismatch")
        restored[name] = original
    return restored


def _restore_text(text, documents, values):
    parts, cursor = [], 0
    for document in documents:
        start, end = document["start"], document["end"]
        if not cursor <= start <= end <= len(text):
            raise ValueError("Invalid projection document offsets")
        parts.append(text[cursor:start])
        if document.get("kind") == "text_ref":
            if text[start:end] != "@" + document["name"]:
                raise ValueError("Changed verbatim text reference")
            parts.append(values[document["name"]])
            cursor = end
            continue
        tokens = list(_TOKEN.finditer(text[start:end]))
        if (
            "".join(t.group() for t in tokens) != text[start:end]
            or len(document["gaps"]) != len(tokens) + 1
        ):
            raise ValueError("Changed projection JSON tokens")
        for index, token in enumerate(tokens):
            lexical = token.group()
            if lexical.startswith("@"):
                lexical = _json(values[lexical[1:]])
            gap = document["gaps"][index]
            override = document["lexical_overrides"].get(str(index), lexical)
            if gap.strip() or (override != lexical and json.loads(override) != json.loads(lexical)):
                raise ValueError("Receipt formatting cannot hide semantic content")
            parts.extend([gap, override])
        if document["gaps"][-1].strip():
            raise ValueError("Receipt formatting cannot hide semantic content")
        parts.append(document["gaps"][-1])
        cursor = end
    return "".join([*parts, text[cursor:]])
