#!/usr/bin/env python3
"""Installer for the code-context-map Claude Code skill.

    python3 install.py                  # install (or upgrade in place)
    python3 install.py --uninstall      # remove the hooks and the skill files
    python3 install.py --no-typescript  # skip the optional `npm install typescript`
    python3 install.py --dry-run        # show what would change, touch nothing

What it does (and nothing else):
  1. Copies skills/code-context-map/ to <claude-dir>/skills/code-context-map/
     (overlay copy: an upgrade keeps your .disabled switch and any node_modules).
  2. Adds two hooks to <claude-dir>/settings.json — PostToolUse (re-index a file
     after Claude edits it) and SessionStart (one-line status). Existing settings
     and hooks are preserved; re-running never duplicates them; a timestamped
     backup is written before any change.
  3. Optionally runs `npm install typescript` inside the skill's scripts/ folder
     so TypeScript is parsed exactly even in projects without their own copy
     (pinned to typescript@6: version 7+ dropped the JS API the parser needs).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_NAME = "code-context-map"
REPO_SKILL_DIR = Path(__file__).resolve().parent / "skills" / SKILL_NAME

# TypeScript 7+ is a native rewrite with no JavaScript compiler API, which the parser
# helper needs — so pin the last JS-based major.
TYPESCRIPT_SPEC = "typescript@6"

# (event, matcher, script, timeout-seconds)
HOOKS = (
    ("PostToolUse", "Edit|Write|MultiEdit", "update_symbol_map.py", 15),
    ("SessionStart", "*", "session_status.py", 5),
)


def display_path(path: Path) -> str:
    """`$HOME/...` when under the home directory, so settings.json stays portable."""
    try:
        return "$HOME/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def hook_command(skill_dir: Path, script: str) -> str:
    return f'python3 "{display_path(skill_dir)}/scripts/{script}"'


def is_ours(command: str, script: str) -> bool:
    return f"{SKILL_NAME}/scripts/{script}" in command


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        sys.exit(f"error: {path} is not valid JSON ({exc}). Fix it (or move it aside) and re-run; "
                 "nothing was changed.")
    if not isinstance(data, dict):
        sys.exit(f"error: {path} must contain a JSON object. Nothing was changed.")
    return data


def write_settings(path: Path, data: dict) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"  backed up settings -> {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_hooks(settings: dict, skill_dir: Path) -> list:
    """Adds any missing hook; returns the list of events it added to."""
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, matcher, script, timeout in HOOKS:
        groups = hooks.setdefault(event, [])
        present = any(is_ours(h.get("command", ""), script)
                      for g in groups for h in g.get("hooks", []))
        if present:
            continue
        groups.append({"matcher": matcher, "hooks": [
            {"type": "command", "command": hook_command(skill_dir, script), "timeout": timeout}]})
        added.append(event)
    return added


def remove_hooks(settings: dict) -> list:
    """Removes only our hook commands; drops groups/events that become empty."""
    removed = []
    hooks = settings.get("hooks", {})
    for event, _matcher, script, _timeout in HOOKS:
        groups = hooks.get(event, [])
        for g in groups:
            kept = [h for h in g.get("hooks", []) if not is_ours(h.get("command", ""), script)]
            if len(kept) != len(g.get("hooks", [])):
                removed.append(event)
                g["hooks"] = kept
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return removed


def install_typescript(scripts_dir: Path) -> None:
    npm = shutil.which("npm")
    if not (shutil.which("node") and npm):
        print("  TypeScript: node/npm not found — skipped. TypeScript files will use the best-effort "
              "parser unless a project has its own node_modules/typescript.")
        return
    if (scripts_dir / "node_modules" / "typescript" / "lib" / "typescript.js").is_file():
        print("  TypeScript: already installed.")
        return
    print(f"  TypeScript: running `npm install {TYPESCRIPT_SPEC}` in the skill's scripts folder ...")
    result = subprocess.run(
        [npm, "install", "--prefix", str(scripts_dir), "--no-save", "--no-package-lock",
         "--no-audit", "--no-fund", "--loglevel=error", TYPESCRIPT_SPEC],
        capture_output=True, text=True)
    if result.returncode == 0:
        print("  TypeScript: installed.")
    else:
        print("  TypeScript: npm install failed — continuing without it (best-effort TS parsing).\n    "
              + (result.stderr.strip().splitlines() or ["unknown error"])[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description="Install the code-context-map Claude Code skill.")
    ap.add_argument("--uninstall", action="store_true", help="remove the hooks and the skill files")
    ap.add_argument("--keep-files", action="store_true", help="with --uninstall: leave the skill folder")
    ap.add_argument("--no-typescript", action="store_true", help="skip `npm install typescript`")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude"),
                    help="Claude Code config dir (default: ~/.claude)")
    args = ap.parse_args()

    claude_dir = Path(args.claude_dir).expanduser().resolve()
    skill_dir = claude_dir / "skills" / SKILL_NAME
    settings_path = claude_dir / "settings.json"
    settings = load_settings(settings_path)

    if args.uninstall:
        removed = remove_hooks(settings)
        print(f"Uninstalling from {claude_dir}")
        print(f"  hooks removed: {', '.join(removed) if removed else 'none found'}")
        if args.dry_run:
            return 0
        if removed:
            write_settings(settings_path, settings)
        if not args.keep_files and skill_dir.exists():
            shutil.rmtree(skill_dir)
            print(f"  removed {skill_dir}")
        print("Done. Start a new Claude Code session for it to take effect.")
        return 0

    if not REPO_SKILL_DIR.is_dir():
        sys.exit(f"error: {REPO_SKILL_DIR} not found — run install.py from a full checkout of the repo.")

    print(f"Installing into {claude_dir}")
    added = add_hooks(settings, skill_dir)
    print(f"  skill files -> {skill_dir}")
    print(f"  hooks added: {', '.join(added) if added else 'none (already configured)'}")
    if args.dry_run:
        return 0

    shutil.copytree(REPO_SKILL_DIR, skill_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".disabled"))
    if added:
        write_settings(settings_path, settings)
    if not args.no_typescript:
        install_typescript(skill_dir / "scripts")
    print("Done. Start a new Claude Code session — the map builds itself as Claude edits files "
          f"(or run: python3 \"{display_path(skill_dir)}/scripts/scan_project.py\" in a project).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
