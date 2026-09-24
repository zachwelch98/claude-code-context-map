#!/usr/bin/env python3
"""Turn code-context-map on/off — globally (every project) or for just the
current project.

Usage:
  python3 toggle.py status              # show global + this project's state
  python3 toggle.py on                  # turn ON globally
  python3 toggle.py off                 # turn OFF globally (pauses every project)
  python3 toggle.py on  --project       # turn ON for just this project
  python3 toggle.py off --project       # turn OFF for just this project

"Off" is a plain marker file the hooks check first, before doing any work —
same mechanism whether it's this script or you touching/removing the file
by hand. Global off always wins: a project explicitly turned on stays
paused while the global switch is off, same as it would if you'd disabled
every project individually.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toggle_state import GLOBAL_DISABLE_MARKER  # noqa: E402


def find_project_root(start: Path) -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return Path(env_root)
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists() \
                or (candidate / "package.json").exists():
            return candidate
    return start


def project_marker(root: Path) -> Path:
    return root / ".claude" / "no-context-map"


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def show_status(root: Path) -> None:
    global_state = "OFF" if GLOBAL_DISABLE_MARKER.exists() else "ON"
    print(f"Global:            {global_state}")
    marker = project_marker(root)
    project_state = "OFF" if marker.exists() else "ON"
    print(f"This project ({root}):\n  {project_state}")
    if global_state == "OFF" and project_state == "ON":
        print("(Global OFF wins — this project stays paused until global is turned back ON.)")


def set_global(enabled: bool) -> None:
    if enabled:
        _remove_if_exists(GLOBAL_DISABLE_MARKER)
        print("code-context-map: ON globally.")
    else:
        _touch(GLOBAL_DISABLE_MARKER)
        print("code-context-map: OFF globally — every project is paused until you run "
              "`toggle.py on` again.")


def set_project(root: Path, enabled: bool) -> None:
    marker = project_marker(root)
    if enabled:
        _remove_if_exists(marker)
        print(f"code-context-map: ON for {root}.")
    else:
        _touch(marker)
        print(f"code-context-map: OFF for {root} (other projects unaffected).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Toggle code-context-map on/off.")
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--project", action="store_true",
                         help="Apply to the current project only, not globally.")
    parser.add_argument("--root", default=None,
                         help="Project root override (default: $CLAUDE_PROJECT_DIR or cwd).")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else find_project_root(Path(os.getcwd()))

    if args.action == "status":
        show_status(root)
    elif args.project:
        set_project(root, enabled=(args.action == "on"))
    else:
        set_global(enabled=(args.action == "on"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
