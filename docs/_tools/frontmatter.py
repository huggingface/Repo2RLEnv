"""Front matter for generated pages: the site reads each page's title from it."""

from __future__ import annotations

import json


def with_title(text: str, *, nav_title: str | None = None) -> str:
    """Move a leading ``# Heading`` into YAML front matter.

    The docs site renders ``title`` itself, so pages carry it as front matter
    rather than as a first-level heading; ``nav_title`` is a shorter sidebar
    label. JSON strings are valid YAML scalars.
    """
    heading, _, body = text.partition("\n")
    if not heading.startswith("# "):
        raise ValueError("generated page must start with a '# ' heading")
    fields = {"title": heading[2:].strip()}
    if nav_title:
        fields["navTitle"] = nav_title
    matter = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fields.items())
    return f"---\n{matter}---\n\n{body.lstrip()}"
