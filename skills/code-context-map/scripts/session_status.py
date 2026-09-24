#!/usr/bin/env python3
"""SessionStart hook: a cheap, read-only status line about this project's
code-context map (a stat/read, never a scan) — so the Skill has something
concrete to react to ("no map yet — bootstrap one before guessing").
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toggle_state import is_globally_disabled  # noqa: E402


def find_project_root(cwd: Path) -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env_root) if env_root else cwd


def main() -> int:
    if is_globally_disabled():
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    root = find_project_root(Path(cwd))

    if (root / ".claude" / "no-context-map").exists():
        return 0

    map_path = root / ".claude" / "context-map.json"
    if not map_path.exists():
        if (root / ".git").exists():
            print("context-map: no map yet for this project — the code-context-map skill can "
                  "bootstrap one (scripts/scan_project.py) before writing code that calls "
                  "existing files.")
        return 0

    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("context-map: existing map file is unreadable/corrupt — consider re-running "
              "scripts/scan_project.py.")
        return 0

    files = data.get("files", {})
    total_symbols = sum(len(f.get("symbols", [])) for f in files.values())
    stale = 0
    for rel, entry in files.items():
        p = root / rel
        try:
            if p.exists() and p.stat().st_mtime > entry.get("mtime", 0) + 1:
                stale += 1
        except OSError:
            continue

    msg = (f"context-map: {len(files)} files / {total_symbols} symbols indexed "
           f"(generated {data.get('generated_at', 'unknown')})")
    if stale:
        msg += f" — {stale} file(s) changed on disk since last indexed, may be stale"
    approx_ts = sum(1 for f in files.values() if f.get("language") == "typescript"
                    and any(not s.get("precise", True) for s in f.get("symbols", [])))
    if approx_ts:
        msg += (f" — {approx_ts} TypeScript file(s) indexed best-effort (no Node/typescript package "
                "found; `npm i -D typescript@6` in the project, or re-run the installer, for exact signatures)")
    print(msg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        sys.exit(0)
