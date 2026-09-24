"""Shared global on/off state for code-context-map.

A single marker file, checked by every entry point (both hooks,
`scan_project.py`, and `toggle.py` itself), so "off" means off everywhere
consistently — no risk of one script honoring it and another not.

Per-project disabling is separate and already existed before this: a
`.claude/no-context-map` file in a given project's root, checked by the
hooks alongside this global one. This module only covers the global switch.
"""
from pathlib import Path

GLOBAL_DISABLE_MARKER = Path(__file__).resolve().parent.parent / ".disabled"


def is_globally_disabled() -> bool:
    return GLOBAL_DISABLE_MARKER.exists()
