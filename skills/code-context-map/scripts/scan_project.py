#!/usr/bin/env python3
"""Bootstrap (or targeted single-file) scan for the code-context map.

Run this once for a project the incremental hook hasn't fully covered yet —
i.e. existing code Claude needs to *call into* but hasn't edited this
session. `--file <path>` updates just one file (cheap); with no arguments
it rebuilds the whole project's map.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractors import (  # noqa: E402
    LANGUAGE_BY_EXT,
    add_to_reverse_calls,
    add_to_reverse_imports,
    add_to_symbol_index,
    build_imports_resolved,
    extract_file,
    prefetch_typescript,
    extract_python_contracts,
    render_markdown,
    resolve_calls_for_file,
)
from gitignore_safety import ensure_gitignore_entries  # noqa: E402
from toggle_state import GLOBAL_DISABLE_MARKER, is_globally_disabled  # noqa: E402

IGNORE_DIRS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__",
                ".git", ".next", "target", "vendor"}
FILE_WARN_THRESHOLD = 500
SYMBOL_WARN_THRESHOLD = 4000


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def git_tracked_files(root: Path):
    """Tracked + untracked-but-not-ignored files (so a fresh repo with no
    commits yet, or files Claude just created but hasn't `git add`ed, still
    get scanned) — but genuinely gitignored files are still excluded."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        )
        return [root / line for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def walk_files(root: Path):
    tracked = git_tracked_files(root)
    if tracked is not None:
        return [p for p in tracked if p.suffix.lower() in LANGUAGE_BY_EXT and p.exists()]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() in LANGUAGE_BY_EXT:
                files.append(p)
    return files


def load_config_ignores(root: Path):
    cfg_path = root / ".claude" / "context-map-config.json"
    if not cfg_path.exists():
        return []
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("ignore", [])
    except (json.JSONDecodeError, OSError):
        return []


def build_map(root: Path, files):
    ignores = load_config_ignores(root)
    files_data = {}
    skipped = 0
    prefetch_typescript(files)  # one Node process for all TS files instead of one per file
    for f in files:
        if not is_relative_to(f, root):
            continue
        rel = str(f.relative_to(root))
        if ignores and any(fnmatch(rel, pat) for pat in ignores):
            continue
        lang, symbols = extract_file(f)
        if lang is None or symbols is None:
            skipped += 1
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        contracts = extract_python_contracts(f) if lang == "python" else []
        files_data[rel] = {"language": lang, "mtime": mtime, "symbols": [s.to_dict() for s in symbols],
                            "data_contracts": [c.to_dict() for c in contracts]}
    return files_data, skipped


def build_reverse_indexes(root: Path, files_data: dict) -> tuple:
    """Full rebuild of symbol_index/reverse_imports/reverse_calls from
    scratch, given every file's already-extracted symbols. Two passes:
    first every file's definitions go into symbol_index, THEN calls/imports
    are resolved against the now-complete index — so resolution here is
    order-independent (unlike the incremental hook, which only sees
    whatever's already indexed at the moment a given file is touched).
    This is the on-demand path, so paying O(project) cost here is fine —
    the whole point of keeping it out of the per-edit hook."""
    symbol_index: dict = {}
    for rel, entry in files_data.items():
        add_to_symbol_index(symbol_index, rel, entry.get("symbols", []), entry.get("language"))

    reverse_imports: dict = {}
    reverse_calls: dict = {}
    for rel, entry in files_data.items():
        language = entry.get("language")
        if language not in ("python", "typescript"):
            entry.setdefault("imports_resolved", [])
            continue
        path = root / rel
        imports_resolved = build_imports_resolved(entry.get("symbols", []), path, root, language)
        entry["imports_resolved"] = imports_resolved
        add_to_reverse_imports(reverse_imports, rel, imports_resolved)
        resolve_calls_for_file(symbol_index, entry.get("symbols", []), language)
        add_to_reverse_calls(reverse_calls, rel, entry.get("symbols", []))

    return symbol_index, reverse_imports, reverse_calls


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_map(root: Path, files_data: dict) -> dict:
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    ensure_gitignore_entries(root)
    symbol_index, reverse_imports, reverse_calls = build_reverse_indexes(root, files_data)
    map_data = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files_data,
        "symbol_index": symbol_index,
        "reverse_imports": reverse_imports,
        "reverse_calls": reverse_calls,
    }
    atomic_write(claude_dir / "context-map.json", json.dumps(map_data, indent=2))
    atomic_write(claude_dir / "context-map.md", render_markdown(map_data))
    return map_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the code-context map for a project.")
    parser.add_argument("--root", default=None, help="Project root (default: $CLAUDE_PROJECT_DIR or cwd)")
    parser.add_argument("--file", default=None, help="Only scan/update this single file")
    args = parser.parse_args()

    if is_globally_disabled():
        print(f"code-context-map is turned off globally ({GLOBAL_DISABLE_MARKER} exists) — not "
              "scanning. Run `python3 scripts/toggle.py on` to re-enable.")
        return 0

    root = Path(args.root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()

    if (root / ".claude" / "no-context-map").exists():
        print(f"code-context-map is turned off for {root} — not scanning. Run "
              "`python3 scripts/toggle.py on --project` to re-enable it here.")
        return 0

    if args.file:
        target = Path(args.file).resolve()
        claude_dir = root / ".claude"
        map_path = claude_dir / "context-map.json"
        map_data = {"schema_version": 1, "files": {}}
        if map_path.exists():
            try:
                map_data = json.loads(map_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        lang, symbols = extract_file(target)
        if lang is None or symbols is None:
            print(f"Skipped {target} (unsupported extension or unparseable).")
            return 0
        rel = str(target.relative_to(root)) if is_relative_to(target, root) else str(target)
        contracts = extract_python_contracts(target) if lang == "python" else []
        map_data.setdefault("files", {})[rel] = {
            "language": lang, "mtime": target.stat().st_mtime,
            "symbols": [s.to_dict() for s in symbols],
            "data_contracts": [c.to_dict() for c in contracts],
        }
        write_map(root, map_data["files"])
        print(f"Updated {rel}: {len(symbols)} symbols.")
        return 0

    files = walk_files(root)
    files_data, skipped = build_map(root, files)
    write_map(root, files_data)

    total_files = len(files_data)
    total_symbols = sum(len(f["symbols"]) for f in files_data.values())
    print(f"code-context-map: indexed {total_files} files, {total_symbols} symbols "
          f"({skipped} skipped as unsupported/unparseable).")
    if total_files > FILE_WARN_THRESHOLD or total_symbols > SYMBOL_WARN_THRESHOLD:
        print(f"  Warning: map is large ({total_files} files, {total_symbols} symbols). "
              "Consider adding ignore globs in .claude/context-map-config.json, e.g. "
              '{"ignore": ["legacy/**", "generated/**"]}, and re-running this scan.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
