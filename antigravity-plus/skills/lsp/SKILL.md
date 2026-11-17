---
name: lsp
description: >-
  Code navigation through the project's language servers (LSP: pyright, sourcekit-lsp, kotlin-lsp,
  typescript-language-server, gopls, rust-analyzer, clangd…): where a symbol is defined, every place it is used with
  the function each one is in, who calls a function (several levels up) and what it calls, its type and signature,
  a file's outline, a symbol by name, implementations, type errors, and a rename across files. Exact where rg
  guesses: no matches in comments, strings or same-named symbols. Run it yourself, without asking, whenever you need
  a symbol's definition, callers or uses — before changing a signature, renaming, moving or deleting code — its
  type, or what a big file contains before reading it; and when the user asks «где определено», «кто вызывает»,
  «где используется», «найди все места», «переименуй везде», «какой тип», /lsp. Helper: scripts/lsp.py.
---

# /lsp — code navigation through language servers

Helper: `python3 "<skill_dir>/scripts/lsp.py"` (below: `lsp.py`). The session context lists the languages of this
project that have a server and gives the exact command. Run it from the project folder.

## Commands

| You need | Run |
|---|---|
| where it is defined | `lsp.py def <file>:<line> <name>` |
| every use, each with the function it is in | `lsp.py refs <file>:<line> <name>` (`--path <folder>` narrows, `--limit 0` shows all) |
| who calls it, levels up | `lsp.py calls <file>:<line> <name> --depth 2` |
| what it calls | `lsp.py calls <file>:<line> <name> --out` |
| type, signature, docs | `lsp.py hover <file>:<line> <name>` |
| implementations of a protocol / interface member | `lsp.py impl <file>:<line> <name>` (`type` — the type's definition) |
| what a file contains (before reading a big one) | `lsp.py symbols <file>` — classes, functions, their line ranges |
| a symbol by name | `lsp.py find <name>` (`--kind` function, class or method; `--path <folder>`) |
| type errors | `lsp.py check <file>…` (`--errors` — errors only) |
| rename everywhere | `lsp.py rename <file>:<line> <name> --to <new>` — a preview; then the same with `--apply` |

The position: `<file>:<line> <name>` — the line you saw the name on; a line or two off is fine (it says which line
it used). Also `<file>:<line>:<col>`, `<file> <name>` (its declaration in that file), `<folder> <name>` (declared
under that folder) and a bare `<name>` or `Class.method` (looked up in the server's index; several matches → exit 3
with the candidates as `<file>:<line>`: pick one and run again). `--lang <language>` when no file says it and the
project has several languages.

## How to use it well
- **Before changing a function's signature, renaming, moving or deleting it:** `refs` (or `calls`) first, change
  every place it lists, then `check` the files you touched and run the tests.
- **Rename:** `rename … --to <new>` shows every edit first; `--apply` writes them all at once. Then re-read the files
  before editing them further (they changed on disk), and run `check` and the tests. Files the server would rename or
  move are listed, not touched.
- **Read the notes under the answer.** «Not linked by the server, but … also in: …» names files where the name
  appears in code the server didn't connect to this symbol: another symbol with the same name — or code it can't
  resolve (imports through `sys.path`, reflection, generated code, a stale index). Check those with rg before you
  call a list of callers complete.
- **rg stays the tool for text:** strings, config keys, routes, comments, SQL, file names, and languages with no server.
- After your edit, a server that is already running reports the type errors your change added in `<edit_check>`.

## Servers
- The first command for a project starts its server in the background (`(starting … )`): pyright takes a few
  seconds, sourcekit-lsp about ten, kotlin-lsp up to a few minutes on a cold Gradle import. A long first command goes
  on in the background and its result arrives as a message: work on something else meanwhile, don't poll. Later
  commands answer in well under a second. A server stops by itself after 10 minutes without commands.
- Swift and Kotlin answers come from an index that is only as fresh as the last build: for code you just added,
  build first or double-check with rg. An Xcode app (not a Swift package) needs xcode-build-server and a
  `buildServer.json` for the app's own code — `lsp.py servers` says so.
- `lsp.py servers` — the languages here, which server each uses and its root, what's missing and how to install it.
  Installing a server is the user's call: say the command; don't install it yourself. `lsp.py status` — what's
  running and its memory; `lsp.py stop` (`--all`) frees it.
- If the terminal sandbox blocks it («operation not permitted»), run it with `BypassSandbox: true`. Errors and the
  servers' own output: `~/.gemini/antigravity-plus/lsp/<id>.log`.
