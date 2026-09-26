#!/usr/bin/env python3
"""PostToolUse hook for Bash: re-indexes source files a shell command changed.

A Bash call carries no `file_path` — files can be changed by heredocs, `sed -i`,
inline Python, codegen, `git checkout`… — so instead of guessing from the
command, this sweeps the project's tracked source files and re-extracts only
those whose mtime moved past what the map recorded (plus new/deleted files).
When nothing changed, it's one `git ls-files` plus stats and writes nothing.

Only runs inside a git repo: without one, `walk_files` would fall back to a full
`os.walk`, which for a session opened in `~` means scanning the whole home dir.

Must always exit 0, same as update_symbol_map.py.
"""
import json
import os
import sys
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractors import extract_file, extract_python_contracts, prefetch_typescript  # noqa: E402
from scan_project import git_tracked_files, load_config_ignores, walk_files, write_map  # noqa: E402
from toggle_state import is_globally_disabled  # noqa: E402
from update_symbol_map import is_ignored, load_map  # noqa: E402

MTIME_TOLERANCE = 1  # same slack session_status.py uses for its staleness check


def main() -> int:
    if is_globally_disabled():
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()).resolve()
    if (root / ".claude" / "no-context-map").exists():
        return 0
    if git_tracked_files(root) is None:
        return 0

    ignores = load_config_ignores(root)
    current = {}
    for f in walk_files(root):
        if is_ignored(f, root):
            continue
        rel = str(f.relative_to(root))
        if ignores and any(fnmatch(rel, pat) for pat in ignores):
            continue
        current[rel] = f

    map_data = load_map(root / ".claude" / "context-map.json")
    files_data = map_data.get("files", {})

    removed = [rel for rel in files_data if rel not in current]
    changed = []
    for rel, f in current.items():
        entry = files_data.get(rel)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if entry is None or mtime > entry.get("mtime", 0) + MTIME_TOLERANCE:
            changed.append(rel)

    if not changed and not removed:
        return 0

    for rel in removed:
        del files_data[rel]

    prefetch_typescript(current[rel] for rel in changed)  # one Node process for all TS files
    for rel in changed:
        f = current[rel]
        lang, symbols = extract_file(f)
        if lang is None or symbols is None:
            continue  # parse failure — leave any existing entry alone
        contracts = extract_python_contracts(f) if lang == "python" else []
        files_data[rel] = {"language": lang, "mtime": f.stat().st_mtime,
                           "symbols": [s.to_dict() for s in symbols],
                           "data_contracts": [c.to_dict() for c in contracts]}

    write_map(root, files_data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        sys.exit(0)
