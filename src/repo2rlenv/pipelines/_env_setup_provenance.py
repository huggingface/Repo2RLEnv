"""Exit 0 iff the package under test came from /workspace.

argv[2] is the probe rung: "direct_url" (PEP 610 metadata OR import location)
or "path" (import location only). Gate 0 never invokes this with "none".
"""

import importlib, importlib.metadata as md, json, sys

cfg = json.load(open(sys.argv[1]))
probe = sys.argv[2] if len(sys.argv) > 2 else cfg.get("probe", "direct_url")


def from_workspace_dist(dist_name: str) -> bool:
    # PEP 610: installers write <dist-info>/direct_url.json for a local / VCS / URL
    # install and MUST NOT write it for an index (PyPI) install.
    try:
        raw = md.distribution(dist_name).read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        return json.loads(raw).get("url", "").startswith("file:///workspace")
    except Exception:
        return False


def from_workspace_import(import_name: str) -> bool:
    try:
        m = importlib.import_module(import_name)
    except Exception:
        return False
    paths = [m.__file__] if getattr(m, "__file__", None) else list(getattr(m, "__path__", []))
    return any(
        p
        and p.startswith("/workspace/")
        and "/site-packages/" not in p
        and "/dist-packages/" not in p
        for p in paths
    )


if probe == "direct_url":
    # dist_name may be absent when only the import name could be determined at
    # emit time; `or ""` keeps that a clean False rather than a KeyError.
    ok = from_workspace_dist(cfg.get("dist_name") or "") or from_workspace_import(cfg["package"])
else:  # "path" — the weaker rung: import location only, no dist metadata required
    ok = from_workspace_import(cfg["package"])

sys.exit(0 if ok else 1)
