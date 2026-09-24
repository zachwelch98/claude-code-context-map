"""Keeps the generated context-map files out of version control.

Scoped narrowly to just the two files this skill generates — never all of
`.claude/`, since other content there (skills, settings) may be
intentionally tracked. Applied unconditionally, even before `.git` exists:
a stray `.gitignore` is inert until `git init` happens, and doing this
unconditionally is what actually protects a project that hasn't been
initialized as a repo yet.
"""
from pathlib import Path

GITIGNORE_ENTRIES = [".claude/context-map.json", ".claude/context-map.md"]


def ensure_gitignore_entries(project_root: Path) -> None:
    gitignore_path = project_root / ".gitignore"
    try:
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines() \
            if gitignore_path.exists() else []
        existing = set(existing_lines)
        missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
        if not missing:
            return
        block = missing
        if existing_lines and existing_lines[-1].strip() != "":
            block = [""] + block
        with gitignore_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
    except OSError:
        pass  # never block the map-writing hook over a gitignore write failure
