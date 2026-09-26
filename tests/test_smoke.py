"""Smoke tests: python3 -m unittest discover -s tests -v

TypeScript-parser tests are skipped when no `typescript` package can be found
(`npm install typescript` at the repo root enables them).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "code-context-map" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import extractors as ex  # noqa: E402

TS_PKG = ex._find_typescript(str(REPO)) if ex._find_node() else None


def run_installer(claude_dir: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "install.py"), "--claude-dir", str(claude_dir),
                           "--no-typescript", *args], capture_output=True, text=True)


def commands(settings: dict, event: str) -> list:
    return [h["command"] for g in settings.get("hooks", {}).get(event, []) for h in g["hooks"]]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.claude = Path(self._tmp.name)
        self.settings = self.claude / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_merges_is_idempotent_and_uninstalls(self):
        self.settings.write_text(json.dumps({
            "theme": "dark",
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]},
        }))
        self.assertEqual(run_installer(self.claude).returncode, 0)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertIn("echo mine", commands(data, "PostToolUse"))
        self.assertEqual(sum("update_symbol_map.py" in c for c in commands(data, "PostToolUse")), 1)
        self.assertEqual(sum("sync_changed.py" in c for c in commands(data, "PostToolUse")), 1)
        self.assertEqual(sum("session_status.py" in c for c in commands(data, "SessionStart")), 1)
        self.assertTrue((self.claude / "skills" / "code-context-map" / "SKILL.md").is_file())

        run_installer(self.claude)  # second run must not duplicate anything
        again = json.loads(self.settings.read_text())
        self.assertEqual(again, data)

        self.assertEqual(run_installer(self.claude, "--uninstall").returncode, 0)
        after = json.loads(self.settings.read_text())
        self.assertEqual(commands(after, "PostToolUse"), ["echo mine"])
        self.assertNotIn("SessionStart", after.get("hooks", {}))
        self.assertFalse((self.claude / "skills" / "code-context-map").exists())

    def test_invalid_settings_json_is_left_untouched(self):
        self.settings.write_text("{ not json")
        result = run_installer(self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.settings.read_text(), "{ not json")

    def test_fresh_install_creates_settings(self):
        self.assertEqual(run_installer(self.claude).returncode, 0)
        self.assertTrue(commands(json.loads(self.settings.read_text()), "PostToolUse"))


class PythonExtractionTests(unittest.TestCase):
    def test_python_signature_is_exact(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.py"
            f.write_text("def total(items, rate=0.0, *, digits: int = 2) -> float:\n    return 1\n")
            (sym,) = ex.extract_python(f)
            self.assertTrue(sym.precise)
            self.assertEqual([p["name"] for p in sym.params], ["items", "rate", "digits"])
            self.assertEqual(sym.return_type, "float")


class TypeScriptResolutionTests(unittest.TestCase):
    """Pure Python — no Node needed."""

    def test_paths_alias_extends_index_and_esm_js_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "src" / "lib").mkdir(parents=True)
            (root / "src" / "ui").mkdir()
            (root / "tsconfig.base.json").write_text(
                '{ // comment\n "compilerOptions": { "baseUrl": "./src", "paths": { "@lib/*": ["lib/*"], }, }, }')
            (root / "tsconfig.json").write_text('{ "extends": "./tsconfig.base.json" }')
            (root / "src" / "lib" / "fmt.ts").write_text("export const a = 1;")
            (root / "src" / "ui" / "index.tsx").write_text("export const b = 2;")
            app = root / "src" / "app.ts"
            app.write_text("")
            resolve = lambda m: ex.resolve_ts_import_target(m, app, root)
            self.assertEqual(resolve("@lib/fmt")[0], "src/lib/fmt.ts")
            self.assertEqual(resolve("lib/fmt")[0], "src/lib/fmt.ts")          # baseUrl
            self.assertEqual(resolve("./ui")[0], "src/ui/index.tsx")           # directory index
            self.assertEqual(resolve("./lib/fmt.js")[0], "src/lib/fmt.ts")     # ESM-style .js -> .ts
            self.assertFalse(resolve("react")[1])                              # bare package
            self.assertFalse(resolve("./missing")[1])

    def test_calls_only_resolve_within_the_same_language(self):
        index = {"shared": [{"file": "a.ts", "kind": "function", "line": 1, "language": "typescript"},
                            {"file": "b.py", "kind": "function", "line": 1}]}
        for language, expected in (("python", "b.py"), ("typescript", "a.ts")):
            syms = [{"name": "caller", "calls": [{"name": "shared", "line": 3}]}]
            ex.resolve_calls_for_file(index, syms, language)
            call = syms[0]["calls"][0]
            self.assertTrue(call["resolved"], language)
            self.assertEqual(call["target"]["file"], expected)


@unittest.skipUnless(TS_PKG, "no typescript package / node available")
class TypeScriptExtractionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "typescript").symlink_to(TS_PKG, target_is_directory=True)
        ex._find_typescript.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()
        ex._find_typescript.cache_clear()

    def extract(self, name, source):
        f = self.root / name
        f.write_text(source)
        return ex.extract_typescript(f)

    def test_generics_types_overloads_and_secrets(self):
        syms = self.extract("m.ts", (
            "export interface User<T = string> extends Base { id: number; name?: string }\n"
            "export type R<T> = { ok: true; value: T } | { ok: false }\n"
            "export const API_KEY = 'sk-abcdefghijklmnop';\n"
            "export async function get<T extends User>(id: string, o?: { n: number }, ...r: string[]): Promise<R<T>> { return h(id); }\n"
            "export function over(a: string): string;\nexport function over(a: number): number;\n"
            "export function over(a: any): any { return a; }\n"))
        by = {(s.kind, s.name): s for s in syms}
        self.assertTrue(all(s.precise for s in syms))
        get = by[("function", "get")]
        self.assertEqual(get.generics, "<T extends User>")
        self.assertEqual(get.return_type, "Promise<R<T>>")
        self.assertEqual([(p["name"], p.get("optional"), p["kind"]) for p in get.params],
                         [("id", None, "positional"), ("o", True, "positional"), ("r", None, "vararg")])
        self.assertEqual(get.calls[0]["name"], "h")
        self.assertIn(("interface", "User"), by)
        self.assertIn(("type", "R"), by)
        self.assertEqual(len([s for s in syms if s.name == "over"]), 2)  # overloads kept, implementation dropped
        self.assertIn("redacted", by[("variable", "API_KEY")].doc)
        self.assertNotIn("abcdefghijklmnop", json.dumps([s.to_dict() for s in syms]))

    def test_syntax_error_returns_none_so_old_entry_survives(self):
        self.assertIsNone(self.extract("bad.ts", "export const x = ;"))

    def test_tsx_component(self):
        syms = self.extract("v.tsx", "export const B = ({ label }: { label: string }) => <b>{label}</b>;\n")
        self.assertEqual([(s.kind, s.name, s.precise) for s in syms], [("function", "B", True)])


class TypeScriptFallbackTests(unittest.TestCase):
    def test_no_parser_falls_back_to_best_effort_regex(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d).resolve() / "a.ts"
            f.write_text("export function hello(name: string) { return name; }\n")
            saved = ex._find_typescript
            ex._find_typescript = lambda _dir: None
            try:
                syms = ex.extract_typescript(f)
            finally:
                ex._find_typescript = saved
            self.assertEqual([(s.name, s.precise) for s in syms], [("hello", False)])


class BashSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.map_path = self.root / ".claude" / "context-map.json"

    def tearDown(self):
        self._tmp.cleanup()

    def sync(self, root: Path):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        payload = json.dumps({"cwd": str(root), "tool_name": "Bash", "tool_input": {}})
        result = subprocess.run([sys.executable, str(SCRIPTS / "sync_changed.py")], input=payload,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)

    def mapped(self) -> dict:
        files = json.loads(self.map_path.read_text())["files"]
        return {rel: [s["name"] for s in e["symbols"]] for rel, e in files.items()}

    def test_bootstraps_updates_and_drops_deleted_files(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "a.py").write_text("def alpha():\n    return 1\n")
        (self.root / "b.py").write_text("def beta():\n    return 2\n")
        self.sync(self.root)
        self.assertEqual(self.mapped(), {"a.py": ["alpha"], "b.py": ["beta"]})

        written = self.map_path.stat().st_mtime_ns
        self.sync(self.root)  # nothing changed -> map not rewritten
        self.assertEqual(self.map_path.stat().st_mtime_ns, written)

        a = self.root / "a.py"
        a.write_text("def alpha():\n    return 1\n\ndef gamma():\n    return 3\n")
        later = a.stat().st_mtime + 5
        os.utime(a, (later, later))
        (self.root / "b.py").unlink()
        self.sync(self.root)
        self.assertEqual(self.mapped(), {"a.py": ["alpha", "gamma"]})

    def test_skips_directories_that_are_not_git_repos(self):
        (self.root / "a.py").write_text("def alpha():\n    return 1\n")
        self.sync(self.root)
        self.assertFalse(self.map_path.exists())


if __name__ == "__main__":
    unittest.main()
