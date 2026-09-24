"""
Symbol extraction for the code-context-map skill.

Python is parsed exactly via the `ast` module (real signatures, no false
positives). Every other supported language is extracted with best-effort
regex/brace-tracking — good enough to point Claude at the right name and
rough shape, but not guaranteed exact (flagged via `precise=False`, and
rendered with a `~` marker in the generated Markdown).

Nothing in this file calls out to a model. It only reads files that already
exist on disk and reports facts about what's literally written in them, so
there is nothing here that can "hallucinate."
"""
from __future__ import annotations

import ast
import functools
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tokenize
from dataclasses import asdict, dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# Symbol model
# --------------------------------------------------------------------------


@dataclass
class Symbol:
    kind: str  # function | method | class | struct | interface | enum | impl | variable | import
    name: str
    line: int
    params: Optional[List[dict]] = None
    return_type: Optional[str] = None
    class_context: Optional[str] = None
    doc: Optional[str] = None
    precise: bool = True
    # Gate 2 (cross-component): raw call sites found in this function/method's
    # body — [{"name", "raw", "line"}], enriched with "resolved"/"target"/
    # "candidates" later, once a project-wide symbol_index exists. None for
    # non-callable symbols.
    calls: Optional[List[dict]] = None
    # Gate 1 (cross-component): only set for kind="import" — the raw dotted
    # module path and relative-import level, kept separately from the
    # human-readable `doc` string so a later resolution step (which needs
    # project-root context this per-file function doesn't have) can turn it
    # into a project-relative target file.
    module: Optional[str] = None
    level: Optional[int] = None
    # TypeScript-only extras. Omitted from to_dict() when unset, so the JSON
    # for every other language is byte-identical to what it was before these existed.
    generics: Optional[str] = None      # "<T extends Foo, U = Bar>"
    heritage: Optional[str] = None      # "extends Base<T> implements IRepo"
    modifiers: Optional[List[str]] = None  # export/default/declare/abstract/private/protected/static/async/optional

    _OPTIONAL_KEYS = ("generics", "heritage", "modifiers")

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in self._OPTIONAL_KEYS:
            if d.get(key) is None:
                d.pop(key, None)
        return d


LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".php": "php",
}


def detect_language(path: Path) -> Optional[str]:
    return LANGUAGE_BY_EXT.get(path.suffix.lower())


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# --------------------------------------------------------------------------
# Python — exact, via ast
# --------------------------------------------------------------------------


def _unparse(node) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _first_doc_line(doc: Optional[str], limit: int = 100) -> Optional[str]:
    if not doc:
        return None
    first = doc.strip().splitlines()[0].strip()
    return first[:limit] + "…" if len(first) > limit else first


def _py_params(args: ast.arguments) -> List[dict]:
    params = []
    n_pos = len(args.posonlyargs) + len(args.args)
    defaults = args.defaults
    pos_defaults_start = n_pos - len(defaults)
    idx = 0
    for a in args.posonlyargs:
        d = _unparse(defaults[idx - pos_defaults_start]) if idx >= pos_defaults_start else None
        params.append({"name": a.arg, "default": d, "kind": "posonly",
                        "annotation": _unparse(a.annotation)})
        idx += 1
    for a in args.args:
        d = _unparse(defaults[idx - pos_defaults_start]) if idx >= pos_defaults_start else None
        params.append({"name": a.arg, "default": d, "kind": "positional",
                        "annotation": _unparse(a.annotation)})
        idx += 1
    if args.vararg:
        params.append({"name": args.vararg.arg, "default": None, "kind": "vararg",
                        "annotation": _unparse(args.vararg.annotation)})
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        params.append({"name": a.arg, "default": _unparse(d), "kind": "keyword_only",
                        "annotation": _unparse(a.annotation)})
    if args.kwarg:
        params.append({"name": args.kwarg.arg, "default": None, "kind": "kwarg",
                        "annotation": _unparse(args.kwarg.annotation)})
    return params


def _call_target_name(func_node) -> Optional[str]:
    """The 'leaf' name a call expression targets — `foo()` -> "foo",
    `self.builder.add_item()` -> "add_item". Only Name/Attribute call
    targets are handled (a call on a subscript, another call's result,
    etc. is too dynamic to name-match reliably, so those are skipped
    rather than guessed at)."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


def _collect_calls(node) -> List[dict]:
    """Best-effort call sites within a function's body (Gate 2). Still pure
    ast — no cross-file knowledge here, just *what name does this call
    target*; a later step (which has the whole project's symbol_index)
    resolves each name against real definitions. Walks the whole subtree
    including any nested function bodies, so a nested closure's calls get
    attributed to its enclosing named function — a known, accepted
    simplification rather than tracking closures as their own scope."""
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_target_name(child.func)
            if not name:
                continue
            raw = _unparse(child)
            calls.append({"name": name, "raw": (raw[:120] if raw else name), "line": child.lineno})
    return calls


def _function_symbol(node, class_context: Optional[str]) -> Symbol:
    return Symbol(
        kind="method" if class_context else "function",
        name=node.name,
        line=node.lineno,
        params=_py_params(node.args),
        return_type=_unparse(node.returns),
        class_context=class_context,
        doc=_first_doc_line(ast.get_docstring(node)),
        precise=True,
        calls=_collect_calls(node) or None,
    )


_SECRET_NAME_RE = re.compile(
    r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|DSN|"
    r"CONN(?:ECTION)?_?STRING|ACCESS[_-]?KEY|AUTH)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = [
    re.compile(p) for p in (
        r"^sk-",
        r"^pk_live_",
        r"^AKIA[0-9A-Z]{16}",
        r"^gh[pousr]_",
        r"^xox[baprs]-",
        r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        r"://[^/\s:]+:[^/\s@]+@",
    )
]


def _looks_like_secret(name: str, value) -> bool:
    """Name-based check first (biased toward over-matching — a false
    positive just means slightly less detail in the map; a false negative
    leaks a secret). Falls back to a value-shape check against the raw
    runtime string (not the ast.unparse'd, quote-wrapped text) so an
    oddly-named variable holding a recognizable secret shape still gets
    caught."""
    if _SECRET_NAME_RE.search(name):
        return True
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return any(p.search(value.value) for p in _SECRET_VALUE_PATTERNS)
    return False


def _value_type_hint(name: str, value, limit: int = 80) -> Optional[str]:
    """Prefer showing the actual literal value (e.g. `= 50`, `= 0.08`,
    `= "prod"`) since for tunable constants the value is usually the whole
    point — a bare type tag like `= int` tells you nothing useful. Falls
    back to a coarse type tag only when the literal is too long/complex to
    show inline (keeps the map from bloating on large dict/list literals).
    Secret-looking names/values are redacted before ever reaching the map —
    see `_looks_like_secret`."""
    if value is None:
        return None
    if _looks_like_secret(name, value):
        tag = type(value.value).__name__ if isinstance(value, ast.Constant) else None
        return f"= <redacted:{tag}>" if tag else "= <redacted>"
    literal = _unparse(value)
    if literal is not None and len(literal) <= limit:
        return f"= {literal}"
    if isinstance(value, ast.List):
        return "= list"
    if isinstance(value, ast.Dict):
        return "= dict"
    if isinstance(value, ast.Set):
        return "= set"
    if isinstance(value, ast.Tuple):
        return "= tuple"
    if isinstance(value, ast.Call):
        fname = _unparse(value.func)
        return f"= call:{fname}" if fname else "= call"
    return "= expr"


def _trailing_comments(source: str) -> dict:
    """Maps line number -> trailing inline comment text, for lines where a
    `#` comment follows real code on the same line (not a standalone
    comment line). `ast` strips comments entirely, so this is the only way
    to recover them — useful because a constant's inline comment (e.g.
    `GRID_SIZE = 50  # cells per side`) is often the best explanation of
    what it's for."""
    comments: dict = {}
    last_code_line = None
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, tok_str, start, end, _ in tokens:
            if tok_type == tokenize.COMMENT:
                if last_code_line == start[0]:
                    comments[start[0]] = tok_str.lstrip("#").strip()
            elif tok_type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                    tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER):
                last_code_line = end[0]
    except (tokenize.TokenizeError, IndentationError, SyntaxError, ValueError):
        return {}
    return comments


def _combine_hint_comment(hint: Optional[str], comment: Optional[str]) -> Optional[str]:
    if hint and comment:
        return f"{hint}  # {comment}"
    if comment:
        return f"# {comment}"
    return hint


def _variable_symbols(node, trailing: Optional[dict] = None) -> List[Symbol]:
    trailing = trailing or {}
    comment = trailing.get(node.lineno)
    out = []
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        doc = _combine_hint_comment(_value_type_hint(node.target.id, node.value), comment)
        out.append(Symbol(kind="variable", name=node.target.id, line=node.lineno,
                           return_type=_unparse(node.annotation), doc=doc))
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                doc = _combine_hint_comment(_value_type_hint(t.id, node.value), comment)
                out.append(Symbol(kind="variable", name=t.id, line=node.lineno, doc=doc))
    return out


def extract_python(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None
    trailing = _trailing_comments(text)

    symbols: List[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_function_symbol(node, None))
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol(kind="class", name=node.name, line=node.lineno,
                                   doc=_first_doc_line(ast.get_docstring(node))))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_function_symbol(child, node.name))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            symbols.extend(_variable_symbols(node, trailing))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(Symbol(kind="import", name=alias.asname or alias.name,
                                       line=node.lineno, doc=f"import {alias.name}",
                                       module=alias.name, level=0))
        elif isinstance(node, ast.ImportFrom):
            mod_display = node.module or ("." * node.level)
            for alias in node.names:
                symbols.append(Symbol(kind="import", name=alias.asname or alias.name,
                                       line=node.lineno, doc=f"from {mod_display} import {alias.name}",
                                       module=node.module, level=node.level))
    return symbols


# --------------------------------------------------------------------------
# Python — data contracts (env vars / CLI flags / API routes)
#
# Runtime/data-contract surfaces that cross a boundary the plain
# symbol/call-graph extraction above doesn't capture. A separate whole-tree
# walk, since these constructs can appear inside functions or decorators,
# not just at module top level. Names/shapes only — any default/help value
# is redacted the same way module-level constants are (`_looks_like_secret`
# via `_value_type_hint`), so this can never reintroduce the value-leak the
# redaction above closes.
# --------------------------------------------------------------------------

_FASTAPI_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


def _arg_str(node) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword_value(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _contract_default_doc(name: str, default_node) -> Optional[str]:
    if default_node is None:
        return None
    hint = _value_type_hint(name, default_node)
    return f"default={hint[2:]}" if hint else None


def _env_var_symbol(node) -> Optional["Symbol"]:
    """Matches os.getenv("X", default), os.environ.get("X", default), and
    os.environ["X"] — string literal keys only, never a dynamically
    computed one (no guessing)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr, obj = node.func.attr, _unparse(node.func.value)
        if attr == "getenv" and obj == "os":
            name = _arg_str(node.args[0]) if node.args else None
        elif attr == "get" and obj == "os.environ":
            name = _arg_str(node.args[0]) if node.args else None
        else:
            return None
        if not name:
            return None
        default_node = node.args[1] if len(node.args) > 1 else _keyword_value(node, "default")
        return Symbol(kind="env_var", name=name, line=node.lineno,
                       doc=_contract_default_doc(name, default_node))
    if isinstance(node, ast.Subscript) and _unparse(node.value) == "os.environ":
        name = _arg_str(node.slice)
        if name:
            return Symbol(kind="env_var", name=name, line=node.lineno)
    return None


def _cli_flag_symbol(call: ast.Call) -> Optional["Symbol"]:
    """argparse's parser.add_argument("--foo", "-f", help=...) — any object,
    since the parser variable's name isn't standardized. Deliberately never
    surfaces `default=` (v1 scope: avoids a second secret-check path for
    arbitrary keyword defaults)."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"):
        return None
    flags = [s for s in (_arg_str(a) for a in call.args) if s]
    if not flags:
        return None
    return Symbol(kind="cli_flag", name=", ".join(flags), line=call.lineno,
                   doc=_arg_str(_keyword_value(call, "help")))


def _click_option_symbol(call: ast.Call) -> Optional["Symbol"]:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "option"
            and _unparse(call.func.value) == "click"):
        return None
    flags = [s for s in (_arg_str(a) for a in call.args) if s]
    if not flags:
        return None
    return Symbol(kind="cli_flag", name=", ".join(flags), line=call.lineno,
                   doc=_arg_str(_keyword_value(call, "help")))


def _route_symbols(call: ast.Call) -> List["Symbol"]:
    """Flask's @app.route("/path", methods=[...]) and FastAPI's
    @app.get/post/put/delete/patch/options/head("/path") — decorator
    position only, to avoid misreading calls like dict.get(...) elsewhere
    in a function body as a route."""
    if not isinstance(call.func, ast.Attribute):
        return []
    attr = call.func.attr
    path = _arg_str(call.args[0]) if call.args else None
    if not path:
        return []
    if attr == "route":
        methods_node = _keyword_value(call, "methods")
        if methods_node is None:
            methods = ["GET"]
        elif isinstance(methods_node, ast.List):
            methods = [s.value for s in methods_node.elts
                       if isinstance(s, ast.Constant) and isinstance(s.value, str)]
            if not methods:
                return []
        else:
            return []  # dynamic methods list — don't guess
    elif attr in _FASTAPI_METHODS:
        methods = [attr.upper()]
    else:
        return []
    return [Symbol(kind="api_route", name=f"{m} {path}", line=call.lineno) for m in methods]


def extract_python_contracts(path: Path) -> List[Symbol]:
    text = _read_text(path)
    if text is None:
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []

    out: List[Symbol] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sym = _env_var_symbol(node)
            if sym:
                out.append(sym)
                continue
            sym = _cli_flag_symbol(node)
            if sym:
                out.append(sym)
        elif isinstance(node, ast.Subscript):
            sym = _env_var_symbol(node)
            if sym:
                out.append(sym)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                sym = _click_option_symbol(dec)
                if sym:
                    out.append(sym)
                    continue
                out.extend(_route_symbols(dec))
    return out


# --------------------------------------------------------------------------
# Generic best-effort helpers (non-Python languages)
# --------------------------------------------------------------------------


def _split_top_level(s: str, sep: str) -> List[str]:
    depth = 0
    current: List[str] = []
    parts: List[str] = []
    for ch in s:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_param_names(raw: str) -> List[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    params = []
    for part in _split_top_level(raw, ","):
        part = part.strip()
        if not part:
            continue
        default = None
        if "=" in part:
            part, default = part.split("=", 1)
            part, default = part.strip(), default.strip()
        name = part.split(":")[0].strip()
        is_vararg = name.startswith("...") or name.startswith("*")
        name = name.lstrip(".*").strip()
        name = re.sub(r"^[\{\[]|[\}\]]$", "", name).strip()
        if name:
            params.append({"name": name, "default": default,
                            "kind": "vararg" if is_vararg else "positional"})
    return params


def _extract_scoped(text: str, scope_re, member_re, toplevel_re, var_re=None,
                     scope_kind: str = "class") -> List[Symbol]:
    """Brace-depth scanner: attributes functions to the nearest enclosing
    class/impl/interface block (a scope opened by `scope_re`), or treats
    them as top-level otherwise. Good enough for standard formatting;
    doesn't attempt a real parse."""
    symbols: List[Symbol] = []
    depth = 0
    stack: List[tuple] = []  # (open_depth, name)
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        sm = scope_re.match(stripped)
        if sm:
            name = sm.group(1)
            symbols.append(Symbol(kind=scope_kind, name=name, line=i, precise=False))
            stack.append((depth, name))
        else:
            in_scope = bool(stack) and stack[-1][0] + 1 == depth
            pattern = member_re if in_scope else toplevel_re
            fm = pattern.match(stripped) if pattern else None
            if fm:
                params = _parse_param_names(fm.group(2)) if fm.re.groups >= 2 else None
                symbols.append(Symbol(
                    kind="method" if in_scope else "function",
                    name=fm.group(1), line=i, params=params,
                    class_context=stack[-1][1] if in_scope else None,
                    precise=False,
                ))
            elif var_re and not stack:
                vm = var_re.match(stripped)
                if vm:
                    symbols.append(Symbol(kind="variable", name=vm.group(1), line=i, precise=False))
        depth += line.count("{") - line.count("}")
        while stack and depth <= stack[-1][0]:
            stack.pop()
    return symbols


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------

_JS_CLASS_RE = re.compile(r"(?:export\s+)?(?:default\s+)?class\s+(\w+)")
_JS_TOPFUNC_RE = re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(([^)]*)\)")
_JS_METHOD_RE = re.compile(
    r"(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+|get\s+|set\s+)*"
    r"(\w+)\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\].,\s]+)?\s*\{"
)
_JS_ARROW_RE = re.compile(
    r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[\w<>\[\].,\s]+)?\s*=\s*"
    r"(?:async\s*)?\(([^)]*)\)\s*(?::\s*[\w<>\[\].,\s]+)?\s*=>"
)
_JS_VAR_RE = re.compile(r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=")
_JS_IMPORT_RE = re.compile(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]")
_JS_REQUIRE_RE = re.compile(r"(\w+)\s*=\s*require\(['\"]([^'\"]+)['\"]\)")


def extract_javascript(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    symbols = _extract_scoped(text, _JS_CLASS_RE, _JS_METHOD_RE, _JS_TOPFUNC_RE, None, "class")
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        m = _JS_ARROW_RE.match(stripped)
        if m:
            symbols.append(Symbol(kind="function", name=m.group(1), line=i,
                                   params=_parse_param_names(m.group(2)), precise=False))
            continue
        m = _JS_VAR_RE.match(stripped)
        if m:
            symbols.append(Symbol(kind="variable", name=m.group(1), line=i, precise=False))
            continue
        m = _JS_IMPORT_RE.search(stripped)
        if m:
            symbols.append(Symbol(kind="import", name=m.group(1), line=i, doc=stripped[:100], precise=False))
            continue
        m = _JS_REQUIRE_RE.search(stripped)
        if m:
            symbols.append(Symbol(kind="import", name=m.group(2), line=i,
                                   doc=f"{m.group(1)} = require(...)", precise=False))
    return symbols


# --------------------------------------------------------------------------
# TypeScript
#
# Exact extraction: the real TypeScript parser, run in Node via extract_ts.mjs
# (syntax-only — no type-checking, no tsconfig). If Node or a `typescript`
# package can't be found, or the helper crashes, this quietly degrades to the
# JavaScript regex extractor above (precise=False, `~` in the Markdown). A file
# that doesn't *parse* is a hard failure (None), same as Python's ast.parse, so
# a half-typed edit never overwrites a good map entry.
# --------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_TS_HELPER = _SCRIPTS_DIR / "extract_ts.mjs"
_TS_SKILL_LOCAL = _SCRIPTS_DIR / "node_modules" / "typescript"
_NODE_FALLBACKS = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node",
                   os.path.expanduser("~/.volta/bin/node"))
_TS_HOOK_TIMEOUT = 8      # the PostToolUse hook is killed at 15s
_TS_BATCH_TIMEOUT = 120

# resolved path -> the helper's per-file result; filled by prefetch_typescript()
# so a full scan spawns Node once per typescript install instead of once per file.
_ts_prefetched: Dict[str, dict] = {}


@functools.lru_cache(maxsize=None)
def _find_node() -> Optional[str]:
    """`node` isn't always on PATH inside a hook (GUI-launched editors, nvm)."""
    found = shutil.which("node")
    if found:
        return found
    candidates = list(_NODE_FALLBACKS) + sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    for c in reversed(candidates):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


@functools.lru_cache(maxsize=None)
def _find_typescript(directory: str) -> Optional[str]:
    """Nearest `node_modules/typescript` walking up from `directory`, else one
    installed next to this skill's scripts, else None."""
    d = Path(directory)
    if (d / "node_modules" / "typescript" / "lib" / "typescript.js").is_file():
        return str(d / "node_modules" / "typescript")
    if d.parent != d:
        return _find_typescript(str(d.parent))
    if (_TS_SKILL_LOCAL / "lib" / "typescript.js").is_file():
        return str(_TS_SKILL_LOCAL)
    return None


def _run_ts_helper(ts_pkg: str, files: List[str], timeout: int) -> Optional[dict]:
    node = _find_node()
    if not node or not _TS_HELPER.is_file():
        return None
    try:
        proc = subprocess.run(
            [node, str(_TS_HELPER)], input=json.dumps({"typescript": ts_pkg, "files": files}),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def prefetch_typescript(paths: Iterable[Path]) -> None:
    """Batch-parse many TS files with one Node process per typescript install.
    Purely an optimization: extract_typescript() does the same work on demand
    for anything not prefetched."""
    groups: Dict[str, List[str]] = {}
    for p in paths:
        if detect_language(p) != "typescript":
            continue
        key = str(p.resolve())
        ts_pkg = _find_typescript(str(Path(key).parent))
        if ts_pkg:
            groups.setdefault(ts_pkg, []).append(key)
    for ts_pkg, files in groups.items():
        result = _run_ts_helper(ts_pkg, files, _TS_BATCH_TIMEOUT)
        if result:
            _ts_prefetched.update(result)


def _secret_text(text: str) -> bool:
    return any(p.search(text.strip("'\"`")) for p in _SECRET_VALUE_PATTERNS)


def _ts_value_hint(name: str, init: Optional[dict], limit: int = 80) -> Optional[str]:
    """TS counterpart of _value_type_hint: show short primitive literals,
    redact anything secret-looking, and never echo object/array/call text."""
    if not init:
        return None
    kind = init.get("kind")
    if _SECRET_NAME_RE.search(name) or (kind == "string" and _secret_text(init.get("value") or "")):
        return "= <redacted:str>" if kind == "string" else "= <redacted>"
    if kind in ("string", "literal"):
        text = init.get("text") or ""
        return f"= {text}" if text and len(text) <= limit else "= expr"
    if kind in ("array", "object"):
        return f"= {kind}"
    if kind == "call":
        return f"= call:{init['callee']}" if init.get("callee") else "= call"
    return "= expr"


def _ts_param(p: dict) -> dict:
    default = p.get("default")
    if default is not None and (_SECRET_NAME_RE.search(p.get("name", "")) or _secret_text(default)):
        return {**p, "default": "<redacted>"}
    return p


_SYMBOL_FIELDS = {f.name for f in dataclass_fields(Symbol)}


def _ts_symbol(d: dict) -> Symbol:
    d = dict(d)
    init = d.pop("init", None)
    if d.get("params"):
        d["params"] = [_ts_param(p) for p in d["params"]]
    if d.get("kind") == "variable" and not d.get("class_context"):
        hint = _ts_value_hint(d["name"], init)
        comment = d.get("doc")
        d["doc"] = f"{hint}  // {comment}" if hint and comment else (hint or (f"// {comment}" if comment else None))
    return Symbol(**{k: v for k, v in d.items() if k in _SYMBOL_FIELDS})


def extract_typescript(path: Path) -> Optional[List[Symbol]]:
    key = str(path.resolve())
    result = _ts_prefetched.pop(key, None)
    if result is None:
        ts_pkg = _find_typescript(str(Path(key).parent))
        if ts_pkg:
            out = _run_ts_helper(ts_pkg, [key], _TS_HOOK_TIMEOUT)
            result = out.get(key) if out else None
    if result is None:
        return extract_javascript(path)  # no usable TypeScript parser: best-effort regex
    if not result.get("ok"):
        return None  # syntax error — leave any existing entry alone
    return [_ts_symbol(d) for d in result["symbols"]]


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------

_JAVA_CLASS_RE = re.compile(
    r"(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum)\s+(\w+)"
)
_JAVA_METHOD_RE = re.compile(
    r"(?:public|private|protected|static|final|synchronized|abstract|native|\s)*"
    r"[\w<>\[\],\s]+?\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w,\s.]+)?\s*\{"
)


def extract_java(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    return _extract_scoped(text, _JAVA_CLASS_RE, _JAVA_METHOD_RE, _JAVA_METHOD_RE, None, "class")


# --------------------------------------------------------------------------
# PHP
# --------------------------------------------------------------------------

_PHP_CLASS_RE = re.compile(r"(?:abstract\s+|final\s+)?class\s+(\w+)")
_PHP_METHOD_RE = re.compile(r"(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)\s*\(([^)]*)\)")


def extract_php(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    return _extract_scoped(text, _PHP_CLASS_RE, _PHP_METHOD_RE, _PHP_METHOD_RE, None, "class")


# --------------------------------------------------------------------------
# Rust
# --------------------------------------------------------------------------

_RUST_IMPL_RE = re.compile(r"impl(?:<[^>]*>)?\s+(?:[\w:]+(?:<[^>]*>)?\s+for\s+)?([\w:]+)")
_RUST_FN_RE = re.compile(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)")
_RUST_TYPE_RE = re.compile(r"(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)")
_RUST_CONST_RE = re.compile(r"(?:pub\s+)?(?:const|static)\s+(\w+)")


def extract_rust(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    symbols = _extract_scoped(text, _RUST_IMPL_RE, _RUST_FN_RE, _RUST_FN_RE, _RUST_CONST_RE, "impl")
    for i, line in enumerate(text.splitlines(), start=1):
        m = _RUST_TYPE_RE.match(line.strip())
        if m:
            symbols.append(Symbol(kind="struct", name=m.group(1), line=i, precise=False))
    return symbols


# --------------------------------------------------------------------------
# Go (no class concept — receiver-based methods captured directly)
# --------------------------------------------------------------------------

_GO_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s+)?(\w+)\s*\(([^)]*)\)")
_GO_STRUCT_RE = re.compile(r"^type\s+(\w+)\s+struct\b")
_GO_INTERFACE_RE = re.compile(r"^type\s+(\w+)\s+interface\b")
_GO_VAR_RE = re.compile(r"^(?:var|const)\s+(\w+)")


def extract_go(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    symbols: List[Symbol] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        m = _GO_FUNC_RE.match(stripped)
        if m:
            receiver, name, params = m.group(1), m.group(2), m.group(3)
            symbols.append(Symbol(kind="method" if receiver else "function", name=name, line=i,
                                   params=_parse_param_names(params), class_context=receiver, precise=False))
            continue
        m = _GO_STRUCT_RE.match(stripped)
        if m:
            symbols.append(Symbol(kind="struct", name=m.group(1), line=i, precise=False))
            continue
        m = _GO_INTERFACE_RE.match(stripped)
        if m:
            symbols.append(Symbol(kind="interface", name=m.group(1), line=i, precise=False))
            continue
        m = _GO_VAR_RE.match(stripped)
        if m:
            symbols.append(Symbol(kind="variable", name=m.group(1), line=i, precise=False))
    return symbols


# --------------------------------------------------------------------------
# Ruby (indentation-based, since Ruby uses `end` rather than braces)
# --------------------------------------------------------------------------

_RUBY_CLASS_RE = re.compile(r"^(\s*)(?:class|module)\s+(\w+)")
_RUBY_DEF_RE = re.compile(r"^(\s*)def\s+(?:self\.)?(\w+[?!]?)\s*(?:\(([^)]*)\))?")


def extract_ruby(path: Path) -> Optional[List[Symbol]]:
    text = _read_text(path)
    if text is None:
        return None
    symbols: List[Symbol] = []
    stack: List[tuple] = []  # (indent, name)
    for i, line in enumerate(text.splitlines(), start=1):
        cm = _RUBY_CLASS_RE.match(line)
        if cm:
            indent, name = len(cm.group(1)), cm.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            symbols.append(Symbol(kind="class", name=name, line=i, precise=False))
            stack.append((indent, name))
            continue
        dm = _RUBY_DEF_RE.match(line)
        if dm:
            indent = len(dm.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            in_scope = bool(stack)
            symbols.append(Symbol(
                kind="method" if in_scope else "function",
                name=dm.group(2), line=i,
                params=_parse_param_names(dm.group(3) or ""),
                class_context=stack[-1][1] if in_scope else None,
                precise=False,
            ))
    return symbols


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

_EXTRACTORS = {
    "python": extract_python,
    "javascript": extract_javascript,
    "typescript": extract_typescript,
    "go": extract_go,
    "rust": extract_rust,
    "ruby": extract_ruby,
    "java": extract_java,
    "php": extract_php,
}


def extract_file(path: Path):
    """Returns (language, symbols) — symbols is None if the extension is
    unsupported or the file couldn't be parsed (caller should leave any
    existing map entry untouched in that case, never overwrite good data
    with a transient failure)."""
    lang = detect_language(path)
    if lang is None:
        return None, None
    try:
        symbols = _EXTRACTORS[lang](path)
    except Exception:
        return lang, None
    return lang, symbols


# --------------------------------------------------------------------------
# Cross-component indexing (Gates 1 + 2)
#
# Everything below operates on plain dicts (the JSON shape a file's symbols
# are stored/loaded as), not Symbol objects, since callers work directly
# with the loaded/about-to-be-saved map. All of it stays deterministic:
# resolution is either "this import's dotted path maps to a file that
# exists on disk" (Gate 1) or "this call's name matches exactly one/zero/
# many project definitions" (Gate 2) — never a guess. An ambiguous or
# unresolved match is reported as such, never picked arbitrarily.
#
# Each add/remove helper here only touches the specific symbol_index /
# reverse_imports / reverse_calls keys the ONE file in question
# contributes to — never a scan of every key in the project — so the
# PostToolUse hook's per-edit cost stays proportional to that file's own
# size, not the whole project's.
# --------------------------------------------------------------------------

# Symbol kinds that can be a call's target (i.e. "definitions" for the
# purposes of the call graph). Variables and imports are deliberately
# excluded — matching a call name against a variable is a different, much
# noisier kind of guess, and imports are just re-exports of names already
# indexed at their real definition.
SYMBOL_INDEX_KINDS = {"function", "method", "class", "struct", "interface", "enum", "impl", "type"}


def add_to_symbol_index(symbol_index: dict, rel_path: str, symbols: list,
                        language: Optional[str] = None) -> None:
    """`language` is recorded on non-Python entries only (an absent key means
    Python), so a Python-only project's index is unchanged."""
    for sym in symbols:
        # "overload" (TypeScript 2nd+ signature of one function) is the same definition, not a rival one.
        if sym.get("kind") in SYMBOL_INDEX_KINDS and "overload" not in (sym.get("modifiers") or ()):
            entry = {
                "file": rel_path, "kind": sym["kind"], "line": sym["line"],
                "class_context": sym.get("class_context"),
            }
            if language and language != "python":
                entry["language"] = language
            symbol_index.setdefault(sym["name"], []).append(entry)


def remove_from_symbol_index(symbol_index: dict, rel_path: str, old_symbols: list) -> None:
    for sym in old_symbols or []:
        if sym.get("kind") not in SYMBOL_INDEX_KINDS:
            continue
        name = sym.get("name")
        entries = symbol_index.get(name)
        if not entries:
            continue
        entries[:] = [e for e in entries
                      if not (e.get("file") == rel_path and e.get("line") == sym.get("line"))]
        if not entries:
            del symbol_index[name]


def resolve_import_target(module, level, importing_file: Path, project_root: Path):
    """Maps an import's dotted module path to a project-relative file, via
    pure path arithmetic + an existence check — never a guess. Returns
    (target_file_or_None, resolved_bool, reason_or_None)."""
    if level and level > 0:
        base = importing_file.parent
        for _ in range(max(level - 1, 0)):
            base = base.parent
        candidate_dir = (base / Path(module.replace(".", "/"))) if module else base
    else:
        if not module:
            return None, False, "no module path"
        candidate_dir = project_root / Path(module.replace(".", "/"))

    for candidate in (candidate_dir.with_suffix(".py"), candidate_dir / "__init__.py"):
        try:
            if candidate.is_file():
                rel = candidate.resolve().relative_to(project_root.resolve())
                return str(rel), True, None
        except (OSError, ValueError):
            continue
    return None, False, "external or unresolvable module"


_TS_CODE_EXTS = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx")
_TS_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_TS_INDEX_FILES = ("index.ts", "index.tsx", "index.d.ts", "index.js", "index.jsx")
_JSONC_RE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _strip_jsonc(text: str) -> str:
    """tsconfig is JSON-with-comments-and-trailing-commas."""
    text = _JSONC_RE.sub(lambda m: m.group(1) or "", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


@functools.lru_cache(maxsize=128)
def _read_tsconfig(config: str, depth: int = 0) -> dict:
    """{"baseUrl": Path|None, "paths": dict|None, "paths_dir": Path|None} for one
    tsconfig, following relative `extends` (a child overrides its parent's keys)."""
    result: dict = {"baseUrl": None, "paths": None, "paths_dir": None}
    path = Path(config)
    try:
        data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    ext = data.get("extends")
    if isinstance(ext, str) and ext.startswith(".") and depth < 5:
        parent = path.parent / ext
        if not parent.suffix:
            parent = parent.with_suffix(".json")
        if parent.is_file():
            result = dict(_read_tsconfig(str(parent.resolve()), depth + 1))
    opts = data.get("compilerOptions")
    if isinstance(opts, dict):
        if isinstance(opts.get("baseUrl"), str):
            result["baseUrl"] = (path.parent / opts["baseUrl"]).resolve()
        if isinstance(opts.get("paths"), dict):
            result["paths"] = opts["paths"]
            result["paths_dir"] = path.parent.resolve()
    return result


@functools.lru_cache(maxsize=256)
def _tsconfig_options_for(directory: str, root: str) -> dict:
    """Nearest tsconfig.json/jsconfig.json at or above `directory` (not above
    `root`). A solution-style tsconfig (Vite: no paths, only `references`) falls
    through to the first referenced config that defines baseUrl/paths."""
    d, stop = Path(directory), Path(root)
    while True:
        for name in ("tsconfig.json", "jsconfig.json"):
            cfg = d / name
            if cfg.is_file():
                opts = _read_tsconfig(str(cfg.resolve()))
                if opts["baseUrl"] or opts["paths"]:
                    return opts
                try:
                    refs = json.loads(_strip_jsonc(cfg.read_text(encoding="utf-8"))).get("references") or []
                except (OSError, ValueError):
                    refs = []
                for ref in refs:
                    ref_path = ref.get("path") if isinstance(ref, dict) else None
                    if not isinstance(ref_path, str):
                        continue
                    target = d / ref_path
                    target = target / "tsconfig.json" if target.is_dir() else target
                    if target.is_file():
                        ref_opts = _read_tsconfig(str(target.resolve()))
                        if ref_opts["baseUrl"] or ref_opts["paths"]:
                            return ref_opts
                return opts
        if d == stop or d.parent == d:
            return {"baseUrl": None, "paths": None, "paths_dir": None}
        d = d.parent


def _ts_try_file(base: Path) -> Optional[Path]:
    """TypeScript-style module lookup for one candidate path: exact file,
    ESM-style `./x.js` -> `x.ts`, appended extension, then directory index."""
    if base.suffix in _TS_CODE_EXTS and base.is_file():
        return base
    for ts_ext in _TS_JS_TO_TS.get(base.suffix, ()):
        cand = base.with_suffix(ts_ext)
        if cand.is_file():
            return cand
    for ext in _TS_CODE_EXTS:
        cand = Path(str(base) + ext)
        if cand.is_file():
            return cand
    if base.is_dir():
        for name in _TS_INDEX_FILES:
            if (base / name).is_file():
                return base / name
    return None


def _ts_alias_candidates(module: str, opts: dict):
    """Yields candidate base paths for a bare specifier from tsconfig `paths`
    (longest matching prefix first, as tsc does) and then `baseUrl`."""
    base_url, paths = opts.get("baseUrl"), opts.get("paths")
    if paths:
        anchor = base_url or opts.get("paths_dir")
        matches = []
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            if "*" in pattern:
                prefix, suffix = pattern.split("*", 1)
                if module.startswith(prefix) and module.endswith(suffix) and len(module) >= len(prefix) + len(suffix):
                    matches.append((len(prefix), module[len(prefix):len(module) - len(suffix)], targets))
            elif pattern == module:
                matches.append((len(pattern), "", targets))
        for _, captured, targets in sorted(matches, key=lambda m: -m[0]):
            for target in targets:
                if isinstance(target, str) and anchor:
                    yield Path(anchor) / target.replace("*", captured)
    if base_url:
        yield Path(base_url) / module


def resolve_ts_import_target(module, importing_file: Path, project_root: Path):
    """TypeScript counterpart of resolve_import_target: relative specifiers,
    tsconfig `paths`/`baseUrl` aliases, extension + index lookup — path
    arithmetic plus an existence check, never a guess. Bare package names
    (react, lodash) stay unresolved. Returns (target_or_None, resolved, reason)."""
    if not module:
        return None, False, "no module path"
    if module.startswith("."):
        candidates = [importing_file.parent / module]
    else:
        opts = _tsconfig_options_for(str(importing_file.parent.resolve()), str(project_root.resolve()))
        candidates = list(_ts_alias_candidates(module, opts))
    for cand in candidates:
        found = _ts_try_file(cand)
        if found is None:
            continue
        try:
            return str(found.resolve().relative_to(project_root.resolve())), True, None
        except (OSError, ValueError):
            return None, False, "resolves outside the project"
    return None, False, "external or unresolvable module"


def build_imports_resolved(symbols: list, importing_file: Path, project_root: Path,
                           language: Optional[str] = None) -> list:
    out = []
    for sym in symbols:
        if sym.get("kind") != "import":
            continue
        module, level = sym.get("module"), sym.get("level") or 0
        if language == "typescript":
            target, resolved, reason = resolve_ts_import_target(module, importing_file, project_root)
        else:
            target, resolved, reason = resolve_import_target(module, level, importing_file, project_root)
        entry = {"raw": sym.get("doc"), "module": module, "name": sym.get("name"),
                  "target_file": target, "resolved": resolved, "line": sym.get("line")}
        if reason:
            entry["reason"] = reason
        out.append(entry)
    return out


def add_to_reverse_imports(reverse_imports: dict, rel_path: str, imports_resolved: list) -> None:
    for imp in imports_resolved or []:
        target = imp.get("target_file")
        if target and imp.get("resolved"):
            lst = reverse_imports.setdefault(target, [])
            if rel_path not in lst:
                lst.append(rel_path)


def remove_from_reverse_imports(reverse_imports: dict, rel_path: str, old_imports_resolved: list) -> None:
    for imp in old_imports_resolved or []:
        target = imp.get("target_file")
        if target in reverse_imports:
            reverse_imports[target] = [f for f in reverse_imports[target] if f != rel_path]
            if not reverse_imports[target]:
                del reverse_imports[target]


def resolve_calls_for_file(symbol_index: dict, symbols: list, language: Optional[str] = None) -> None:
    """Mutates each symbol's `calls` entries in place with resolved/target/
    candidates, using the current symbol_index. Exactly one project-wide
    match -> resolved. Zero matches (stdlib/builtin/external calls are the
    common case) -> resolved: false, no claim, not an error. More than one
    match -> resolved: false with every candidate listed — never guessed.
    Only same-language definitions are candidates (an absent `language` key
    means Python), so a Python call can't match a same-named TS function."""
    lang = language or "python"
    for sym in symbols:
        for call in (sym.get("calls") or []):
            matches = [m for m in symbol_index.get(call.get("name"), [])
                       if (m.get("language") or "python") == lang]
            if len(matches) == 1:
                call["resolved"] = True
                call["target"] = matches[0]
                call.pop("candidates", None)
            elif len(matches) > 1:
                call["resolved"] = False
                call["candidates"] = matches
                call.pop("target", None)
            else:
                call["resolved"] = False
                call.pop("target", None)
                call.pop("candidates", None)


def add_to_reverse_calls(reverse_calls: dict, rel_path: str, symbols: list) -> None:
    for sym in symbols:
        for call in (sym.get("calls") or []):
            if not call.get("resolved"):
                continue
            reverse_calls.setdefault(call["name"], []).append({
                "caller_file": rel_path, "caller_symbol": sym["name"],
                "line": call["line"], "resolved": True,
            })


def remove_from_reverse_calls(reverse_calls: dict, rel_path: str, old_symbols: list) -> None:
    for sym in old_symbols or []:
        for call in (sym.get("calls") or []):
            name = call.get("name")
            entries = reverse_calls.get(name)
            if not entries:
                continue
            entries[:] = [c for c in entries
                          if not (c.get("caller_file") == rel_path and c.get("line") == call.get("line"))]
            if not entries:
                del reverse_calls[name]


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _format_param_list(params: Optional[List[dict]], with_types: bool = False) -> str:
    if not params:
        return ""
    parts = []
    for p in params:
        s = p["name"]
        if with_types:  # TypeScript: names alone hide what the argument must be
            if p.get("kind") == "vararg":
                s = "..." + s
            if p.get("optional"):
                s += "?"
            if p.get("annotation"):
                s += f": {p['annotation']}"
            if p.get("default") is not None:
                s += f" = {p['default']}"
        elif p.get("default") is not None:
            s += f"={p['default']}"
        parts.append(s)
    return ", ".join(parts)


def format_symbol_line(sym: dict, language: Optional[str] = None) -> str:
    kind = sym["kind"]
    ts = language == "typescript"
    marker = "" if sym.get("precise", True) else "~"
    mods = sym.get("modifiers") or []
    if kind in ("function", "method"):
        params = _format_param_list(sym.get("params"), with_types=ts)
        ret = f" -> {sym['return_type']}" if sym.get("return_type") else ""
        prefix = f"{sym['class_context']}." if sym.get("class_context") and (kind == "method" or ts) else ""
        lead = f"{' '.join(mods)} " if mods else ""
        line = f"- {marker}`{lead}{prefix}{sym['name']}{sym.get('generics') or ''}({params}){ret}` — line {sym['line']}"
        if sym.get("doc"):
            line += f" — {sym['doc']}"
    elif kind in ("class", "struct", "interface", "enum", "impl", "type"):
        lead = f"{' '.join(mods)} " if mods else ""
        head = f"{kind} {sym['name']}{sym.get('generics') or ''}"
        if sym.get("heritage"):
            head += f" {sym['heritage']}"
        line = f"- {marker}**{lead}{head}** — line {sym['line']}"
        if ts and sym.get("doc"):
            line += f" — {'= ' if kind == 'type' else ''}{sym['doc']}"
    elif kind == "import":
        doc = f" ({sym['doc']})" if sym.get("doc") else ""
        line = f"- {marker}import `{sym['name']}`{doc} — line {sym['line']}"
    else:  # variable
        rt = f": {sym['return_type']}" if sym.get("return_type") else ""
        doc = f" {sym['doc']}" if sym.get("doc") else ""
        name = sym["name"]
        lead = ""
        if ts:
            if sym.get("class_context"):  # interface property
                name = f"{sym['class_context']}.{name}"
            if "optional" in mods:
                name += "?"
            lead = " ".join(m for m in mods if m != "optional")
            lead = f"{lead} " if lead else ""
        line = f"- {marker}`{lead}{name}`{rt}{doc} — line {sym['line']}"
    return line


def render_markdown(map_data: dict) -> str:
    files = map_data.get("files", {})
    lines = [
        "# Code Context Map",
        "",
        f"_Generated {map_data.get('generated_at', 'unknown')} — {len(files)} files indexed. "
        "`~` marks best-effort (non-Python) extraction; unmarked entries are exact._",
        "",
    ]
    for path in sorted(files.keys()):
        entry = files[path]
        syms = entry.get("symbols", [])
        if not syms:
            continue
        lines.append(f"## {path}")
        for sym in sorted(syms, key=lambda s: s["line"]):
            lines.append(format_symbol_line(sym, entry.get("language")))
        lines.append("")
    lines.extend(render_data_contracts(map_data))
    lines.extend(_render_reverse_sections(map_data))
    return "\n".join(lines)


def render_data_contracts(map_data: dict) -> List[str]:
    files = map_data.get("files", {})
    env_vars, cli_flags, api_routes = [], [], []
    for path in sorted(files.keys()):
        for c in files[path].get("data_contracts", []):
            bucket = {"env_var": env_vars, "cli_flag": cli_flags, "api_route": api_routes}.get(c["kind"])
            if bucket is not None:
                bucket.append((path, c))
    if not (env_vars or cli_flags or api_routes):
        return []

    lines = ["## Data contracts (env vars / CLI flags / API routes — names & shapes only, never values)", ""]
    if env_vars:
        lines.append("### Environment variables")
        for path, c in env_vars:
            doc = f" ({c['doc']})" if c.get("doc") else ""
            lines.append(f"- `{c['name']}` — {path}:{c['line']}{doc}")
        lines.append("")
    if cli_flags:
        lines.append("### CLI flags")
        for path, c in cli_flags:
            doc = f" — {c['doc']}" if c.get("doc") else ""
            lines.append(f"- `{c['name']}` — {path}:{c['line']}{doc}")
        lines.append("")
    if api_routes:
        lines.append("### API routes")
        for path, c in api_routes:
            lines.append(f"- `{c['name']}` — {path}:{c['line']}")
        lines.append("")
    return lines


def _render_reverse_sections(map_data: dict) -> List[str]:
    """Renders the compact, aggregate cross-component views. Deliberately
    excludes full forward call-site detail (proportional to call
    expressions, typically several multiples of symbol count) — that stays
    JSON-only, grep-only, same as exact-precision lookups already are. Only
    the reverse views (roughly proportional to symbol count) go into the
    Markdown Claude actually reads, so this can't quietly bloat it."""
    lines: List[str] = []
    symbol_index = map_data.get("symbol_index", {})
    reverse_imports = map_data.get("reverse_imports", {})
    reverse_calls = map_data.get("reverse_calls", {})

    if reverse_imports:
        lines.append("## Import graph (who depends on this file)")
        for target in sorted(reverse_imports):
            sources = ", ".join(sorted(set(reverse_imports[target])))
            lines.append(f"- `{target}` ← imported by: {sources}")
        lines.append("")

    call_lines: List[str] = []
    for name in sorted(set(symbol_index) | set(reverse_calls)):
        defs = symbol_index.get(name, [])
        by_language: dict = {}
        for d in defs:
            by_language.setdefault(d.get("language") or "python", []).append(d)
        if any(len(group) > 1 for group in by_language.values()):
            where = ", ".join(f"{d['file']}:{d['line']} [{d['kind']}]" for d in defs)
            call_lines.append(f"- `{name}` — ambiguous: {len(defs)} unrelated definitions ({where})")
        elif reverse_calls.get(name):
            where = "; ".join("%s:%s" % (d["file"], d["line"]) for d in defs)
            loc = f" ({where})" if defs else ""
            callers = "; ".join(f"{c['caller_file']}:{c['line']} ({c['caller_symbol']})"
                                 for c in reverse_calls[name])
            call_lines.append(f"- `{name}`{loc} ← {callers}")
    if call_lines:
        lines.append("## Reverse call index (heuristic — name-match only, not verified)")
        lines.extend(call_lines)
        lines.append("")

    return lines
