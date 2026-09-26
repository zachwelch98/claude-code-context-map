# claude-code-context-map

A [Claude Code](https://claude.com/claude-code) skill that stops Claude from guessing function names,
parameter names/order, or variable names when it writes code that calls into files you already have.

It keeps a **map of your project's real signatures** (`.claude/context-map.md` / `.json`) and tells Claude
to check it before writing a call. The map is built by plain parsers — **no model, no network access at runtime, nothing
sent anywhere** — and updated automatically every time Claude edits a file.

```
## src/models.ts
- **export interface User<T = string> extends Base** — line 8 — A user record.
- `User.greet(other: User, loud?: boolean) -> string` — line 12
- `export async fetchUser<T extends User>(id: string, ...rest: string[]) -> Promise<Result<T>>` — line 23 — Fetch a user.
- `export API_KEY` = <redacted:str> — line 18
```

## Install

Requires Python 3 and Claude Code. Node is optional (see TypeScript below).

```bash
git clone https://github.com/zachwelch98/claude-code-context-map.git
cd claude-code-context-map
python3 install.py
```

Then start a new Claude Code session. That's it — the map builds itself as Claude edits files. To index a
project you're about to work in right away:

```bash
python3 ~/.claude/skills/code-context-map/scripts/scan_project.py     # run inside the project
```

The installer is idempotent and only does three things: copies the skill to `~/.claude/skills/code-context-map/`,
adds three hooks to `~/.claude/settings.json` (existing settings/hooks are kept; a timestamped backup is written
first), and — if `node` and `npm` are present — installs `typescript@6` into the skill folder.
Use `--dry-run` to preview, `--no-typescript` to skip the npm step, and `--uninstall` to remove everything.

## What gets mapped

| Language | Extraction |
|---|---|
| Python | **Exact** (`ast`): signatures, defaults, annotations, classes, constants, call sites, imports, env vars / CLI flags / API routes |
| TypeScript / TSX | **Exact** (real TypeScript parser): generics, typed/optional/rest params, return types, `interface` / `type` / `enum`, overloads, modifiers, imports resolved through relative paths and `tsconfig` `paths` / `baseUrl` |
| JavaScript, Go, Rust, Ruby, Java, PHP | Best-effort (regex), marked `~` in the map |

Also built: an **import graph** ("who depends on this file") and a heuristic **reverse call index**, both
scoped per language so a Python `shared()` is never confused with a TypeScript `shared()`.

Secrets are never written to the map: secret-looking names and values (API keys, tokens, passwords,
connection strings, …) are recorded as `<redacted>`, and object/array/call initializers are summarized, not copied.

### TypeScript notes

- The parser is TypeScript's own compiler API, run once per scan (or per edited file) through Node, and it
  is syntax-only — no type-checking, no build.
- It uses the project's `node_modules/typescript` if present, otherwise the copy the installer put in the skill.
- **TypeScript 7+ is not supported** (the native rewrite has no JavaScript API); the installer pins `typescript@6`.
  Projects on 7 fall back to the skill's copy.
- With no usable parser, TS files degrade to the regex extractor (`~` markers) and Claude Code's session start
  says so. A `.ts` file with a syntax error is skipped, keeping its previous entry.

## Turning it off / on

```bash
python3 ~/.claude/skills/code-context-map/scripts/toggle.py status
python3 ~/.claude/skills/code-context-map/scripts/toggle.py off              # everywhere
python3 ~/.claude/skills/code-context-map/scripts/toggle.py off --project    # this project only
```

Ignore paths per project with `.claude/context-map-config.json`: `{"ignore": ["**/generated/**"]}`.
The `.claude/context-map.*` files are added to your project's `.gitignore` automatically.

## Platform support

Developed and tested on macOS with Python 3.14; CI runs Linux and macOS. The hook commands use
`python3` and `$HOME`, so Windows is untested — WSL should work.

## Development

```bash
npm install --no-save --no-package-lock typescript@6   # enables the TypeScript tests
python3 -m unittest discover -s tests -v
```

## License

MIT
