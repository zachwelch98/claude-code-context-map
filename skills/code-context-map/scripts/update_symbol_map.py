#!/usr/bin/env python3
"""PostToolUse hook: incrementally keeps a project's code-context map current
after Edit/Write/MultiEdit tool calls.

This is deterministic parsing only — no model call happens here, so nothing
in this script can hallucinate. It reads whatever file was just touched,
extracts its real symbols, and replaces that file's entry in the map
(never appends, so renamed/deleted symbols don't linger as stale entries).

Must always exit 0: a bug or parse failure here must never block the user's
edit, and must never corrupt the existing map (writes are atomic).
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractors import (  # noqa: E402
    add_to_reverse_calls,
    add_to_reverse_imports,
    add_to_symbol_index,
    build_imports_resolved,
    extract_file,
    extract_python_contracts,
    remove_from_reverse_calls,
    remove_from_reverse_imports,
    remove_from_symbol_index,
    render_markdown,
    resolve_calls_for_file,
)
from gitignore_safety import ensure_gitignore_entries  # noqa: E402
from toggle_state import is_globally_disabled  # noqa: E402

IGNORE_DIRS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__",
                ".git", ".next", "target", "vendor"}
FILE_WARN_THRESHOLD = 500
SYMBOL_WARN_THRESHOLD = 4000


def find_project_root(start: Path) -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return Path(env_root)
    cur = start if start.is_dir() else start.parent
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists() \
                or (candidate / "package.json").exists():
            return candidate
    return start.parent if start.is_file() else start


def is_ignored(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in IGNORE_DIRS for part in rel_parts)


def load_map(map_path: Path) -> dict:
    if not map_path.exists():
        return {"schema_version": 1, "generated_at": None, "files": {}}
    try:
        return json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema_version": 1, "generated_at": None, "files": {}}


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def main() -> int:
    if is_globally_disabled():
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    file_path_str = tool_input.get("file_path")
    if not file_path_str:
        return 0

    file_path = Path(file_path_str)
    if not file_path.is_absolute():
        file_path = Path(payload.get("cwd") or os.getcwd()) / file_path
    if not file_path.exists():
        return 0

    project_root = find_project_root(file_path)
    if (project_root / ".claude" / "no-context-map").exists():
        return 0
    if is_ignored(file_path, project_root):
        return 0

    lang, symbols = extract_file(file_path)
    if lang is None or symbols is None:
        return 0  # unsupported extension, or a parse failure — leave any existing entry alone

    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    ensure_gitignore_entries(project_root)
    map_path = claude_dir / "context-map.json"
    md_path = claude_dir / "context-map.md"

    map_data = load_map(map_path)
    rel_path = relative_path(file_path, project_root)

    symbol_index = map_data.setdefault("symbol_index", {})
    reverse_imports = map_data.setdefault("reverse_imports", {})
    reverse_calls = map_data.setdefault("reverse_calls", {})

    # Delta update: strip this file's OLD contributions to the cross-file
    # indexes before adding its new ones — using the old entry already in
    # hand (about to be overwritten below) rather than scanning every other
    # file. Keeps this hook's cost proportional to the one file just
    # edited, never to project size.
    old_entry = map_data.get("files", {}).get(rel_path)
    if old_entry:
        remove_from_symbol_index(symbol_index, rel_path, old_entry.get("symbols"))
        remove_from_reverse_imports(reverse_imports, rel_path, old_entry.get("imports_resolved"))
        remove_from_reverse_calls(reverse_calls, rel_path, old_entry.get("symbols"))

    new_symbols = [s.to_dict() for s in symbols]
    add_to_symbol_index(symbol_index, rel_path, new_symbols, lang)

    imports_resolved = []
    if lang in ("python", "typescript"):
        imports_resolved = build_imports_resolved(new_symbols, file_path, project_root, lang)
        add_to_reverse_imports(reverse_imports, rel_path, imports_resolved)
        # Resolve this file's own calls against the index (now including
        # this file's own just-added definitions, so self-references
        # resolve too). Other files' calls into names this edit just
        # added/removed aren't retroactively re-resolved here — that's
        # the accepted incremental-hook tradeoff; `scan_project.py` does a
        # full, order-independent rebuild when run.
        resolve_calls_for_file(symbol_index, new_symbols, lang)
        add_to_reverse_calls(reverse_calls, rel_path, new_symbols)

    contracts = extract_python_contracts(file_path) if lang == "python" else []

    map_data.setdefault("files", {})[rel_path] = {
        "language": lang,
        "mtime": file_path.stat().st_mtime,
        "symbols": new_symbols,
        "imports_resolved": imports_resolved,
        "data_contracts": [c.to_dict() for c in contracts],
    }
    map_data["schema_version"] = 1
    map_data["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    total_files = len(map_data["files"])
    total_symbols = sum(len(f.get("symbols", [])) for f in map_data["files"].values())
    if total_files > FILE_WARN_THRESHOLD or total_symbols > SYMBOL_WARN_THRESHOLD:
        sys.stderr.write(
            f"code-context-map: map is getting large ({total_files} files, {total_symbols} "
            "symbols). Consider ignore globs in .claude/context-map-config.json.\n"
        )

    atomic_write(map_path, json.dumps(map_data, indent=2))
    atomic_write(md_path, render_markdown(map_data))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        sys.exit(0)
