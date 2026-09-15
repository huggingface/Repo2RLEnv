"""SWE-smith-inspired procedural defects, validated against healthy repository tests.

Acknowledgment: SWE-bench/SWE-smith (MIT), revision
9b74ac08118a85c39c356802f7961893af73e07f, swesmith/bug_gen/procedural/python/.
This owned implementation enumerates single-site changes deterministically;
upstream samples probabilistic edits over entities. See provenance.md.
"""

from __future__ import annotations
