---
name: code-context-map
description: >-
  Maintains and consults a project-specific map of existing function/method signatures,
  class names, and variable definitions so you never guess or hallucinate a wrong function
  name, wrong parameter name, wrong parameter count/order, or wrong variable name when
  writing or editing code that calls into existing project files. Use this whenever you're
  about to write code that references, calls, imports, or extends something already defined
  elsewhere in the current project — multi-file features, refactors, "use the existing X
  function," "add a call to Y," bug fixes that touch code you didn't just write yourself.
  Check the map before typing a function call rather than after it breaks. Not needed for a
  brand-new, self-contained, single-file script with no existing codebase to reference. Also use
  this skill whenever the user asks to turn this off/on, pause/resume it, or disable/enable the
  context map — globally or "just for this project."
---

# Code Context Map

A `PostToolUse` hook already keeps `.claude/context-map.json` / `.claude/context-map.md`
current in the background for this project — every time you edit or write a file, a plain
parser script (no model involved) re-indexes that file's functions, classes, and variables.
Your job is to actually *use* that map before guessing.

## Before writing a call to existing code

1. Open `.claude/context-map.md` in the project root and find the function/class/variable
   you're about to reference. It's grouped by file, one line per symbol, e.g.:
   `- \`calculate_total(items, tax_rate=0.0)\` — line 14`
2. Match your call's argument names/order/defaults to what the map shows — don't rely on
   memory or a plausible-sounding guess, even for something you think you remember writing
   earlier in this session.
3. Need exact precision (e.g. confirming keyword-only args or a type annotation right before
   finalizing a cross-file change)? Grep `.claude/context-map.json` instead — it's the
   machine-precise source the Markdown is rendered from.

## If the map doesn't cover what you need

The hook only indexes files *you* edit through Claude Code — it can't see existing code you
merely need to call into but haven't touched yet. When you're about to reference something
from a file that isn't in the map (or there's no map at all yet):

```
python3 "$HOME/.claude/skills/code-context-map/scripts/scan_project.py"          # whole project
python3 "$HOME/.claude/skills/code-context-map/scripts/scan_project.py" --file <path>  # one file
```

Run the single-file form when you just need one more file covered (fast); run the full-project
form once per project when starting real feature work in an existing, unfamiliar codebase.
Session start prints a one-line status (file/symbol counts) — use it to judge whether a
bootstrap is worth doing before you start writing calls into unfamiliar code.

## Trusting the map

- **Python entries are exact** — extracted with Python's own `ast` module, not regex, so
  parameter names/order/defaults are guaranteed correct if present.
- **TypeScript (`.ts`/`.tsx`/`.mts`/`.cts`) is exact** when Node and a `typescript` package are
  available (the project's own `node_modules/typescript`, or one installed in this skill's
  `scripts/`; TypeScript 7+ is not supported — it has no JS compiler API). It's parsed with the real TypeScript parser (`scripts/extract_ts.mjs`), so the map
  shows generics, typed/optional/rest parameters, return types, `interface`/`type`/`enum`,
  overloads, and modifiers (`export`, `static`, `async`, …). If no parser is found it quietly
  falls back to the best-effort regex extractor (entries marked `~`, and session start says so).
  A `.ts` file with a syntax error is skipped, leaving its previous entry in place. Imports
  resolve through relative paths and `tsconfig.json` `paths`/`baseUrl`, and appear in the import graph.
- **Other languages (JS, Go, Rust, Ruby, Java, PHP) are best-effort**, marked with a `~` in
  the Markdown. They're extracted with pattern matching, not a real parser, so they can miss
  decorators, generics, destructured parameters, or dynamically defined methods. Treat these
  as "probably right" — for anything load-bearing, open the real source file and confirm
  rather than trusting the map alone.
- If a file's on-disk modification time is newer than what's recorded in `context-map.json`
  (e.g. it was edited outside Claude Code, or you're on a different branch than last session),
  its entries may be stale — re-run `scan_project.py --file <path>` on it before trusting it.
- When the map genuinely has no entry for something you need, don't invent a plausible name —
  read the real file, or ask the user.

## Turning it on/off

If the user asks to disable, pause, turn off, re-enable, or check the status of this skill
(globally, or "just for this project"), run the toggle script rather than editing marker files
or `settings.json` by hand:

```
python3 "$HOME/.claude/skills/code-context-map/scripts/toggle.py" status
python3 "$HOME/.claude/skills/code-context-map/scripts/toggle.py" off              # everywhere
python3 "$HOME/.claude/skills/code-context-map/scripts/toggle.py" on               # everywhere
python3 "$HOME/.claude/skills/code-context-map/scripts/toggle.py" off --project    # this project only
python3 "$HOME/.claude/skills/code-context-map/scripts/toggle.py" on  --project    # this project only
```

Global off always wins — a project left individually "on" still stays paused while the global
switch is off. Once off (either scope), every entry point — the edit hook, the session-start
status line, and `scan_project.py` — no-ops immediately and silently; nothing gets scanned,
written, or shown until it's turned back on. Report back plainly what you changed and at what
scope, since this affects every future session in that project (or everywhere, for the global
switch), not just the current one.

## Cross-component sections (who imports what, who calls what)

`context-map.md` also has an "Import graph" and a "Reverse call index" section (Python-only
for calls). Read these as a different, weaker kind of claim than the signature entries above —
`precise` (does this entry literally reflect the source) and `resolved` (are we certain which
definition a name refers to) are separate questions:

- **`Import graph`** — file-level, exact. An import statement unambiguously names its target,
  so these entries carry the same trust as a Python signature entry.
- **`Reverse call index`** — heuristic, **name-matched only, not verified call resolution**.
  A call site is linked to a definition purely because the names match — there's no real
  understanding of scope, types, or which imported symbol a name actually refers to. Two
  unrelated things can share a name by coincidence (this already happens in practice — a
  Python class and a totally unrelated JS class named the same thing render as
  `` `Name` — ambiguous: N unrelated definitions ``, listing every candidate rather than
  guessing one).
  - A resolved entry (single match) is still just "the only same-named thing in the project" —
    strong signal, not a guarantee.
  - An **ambiguous** entry means don't pick one yourself: open both/all candidate files and use
    the surrounding code (what's imported, what type the variable actually is) to figure out
    which one is really meant, the same way you'd resolve it by eye.
  - Names with **no entry at all** are the common case (a call to a stdlib/builtin/external
    method, or simply nothing calls that symbol yet) — that's not a gap to fill in, it's the
    honest "no claim" default.
  - Never write `context-map.json`/`.md` yourself to "fix" an ambiguous entry, even if you've
    figured out the right answer — resolving it belongs in your live reasoning for *this* call
    site, not cached as a project-wide fact (a wrong cached guess would look identical to a
    verified one to the next reader).

## Data contracts (env vars / CLI flags / API routes)

`context-map.md` also has a "Data contracts" section, Python-only, `ast`-derived (exact, same
trust level as the signature entries) covering things that cross a runtime boundary rather than
a plain function/variable definition:

- **Environment variables** — `os.getenv(...)`, `os.environ.get(...)`, `os.environ[...]`.
- **CLI flags** — `argparse`'s `add_argument(...)` and `click`'s `@click.option(...)`.
- **API routes** — Flask's `@app.route(...)` and FastAPI's `@app.get/post/put/...(...)`.

Names and shapes only, **never values**: any default/help value that looks secret-shaped (by
name — `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, etc. — or by value pattern — vendor key prefixes,
JWTs, credentials embedded in a URL) is redacted to `<redacted:...>` before it's ever written to
the map, the same protection module-level constants get. This also means the map is a safe
starting point for auditing what a project reads from its environment or exposes as endpoints,
without it becoming a second place secrets could leak from.

## Secrets never get cached here

Module-level constants (`API_KEY = "..."`) and data-contract defaults are checked against the
same name/value secret heuristics before their literal value is ever written to
`context-map.json`/`.md`. If you notice a real secret's value did leak into an existing map
(e.g. from before this protection existed, or a pattern the heuristic missed), tell the user —
don't just quietly fix the entry, since the same value may already be sitting in git history or
somewhere else it was pasted from this file.
