---
trigger: always_on
---
# Antigravity Plus — Code navigation through language servers

Antigravity has no code-intelligence tool of its own. The `/lsp` skill (`lsp.py`) asks the project's language server — pyright, sourcekit-lsp, kotlin-lsp, typescript-language-server, gopls, rust-analyzer, clangd — and the session context names the servers here with the exact command.

- **A symbol in code → ask the server, not rg.** Where it is defined: `def`; every use with the function it is in: `refs`; who calls it: `calls` (`--depth 2` for their callers, `--out` for what it calls); its type: `hover`; a big file's outline before reading it: `symbols`; a symbol by name: `find`. rg finds text: it can't tell a call from a comment, a string or another symbol with the same name.
- **Via MCP (`antigravity-plus_tools`):** when available, prefer the typed tools `lsp_def`, `lsp_refs`, `lsp_calls`, `lsp_hover`, `lsp_symbols`, `lsp_find`, `lsp_check` — they talk to the same warm daemon without shell quoting.
- **Before you change a signature, rename, move or delete:** `refs`/`calls` first, update every place listed, then `check` the files and run the tests. Renames across files: `rename … --to <new>` (preview), then `--apply`.
- **Notes under an answer are part of it.** «Not linked by the server, but … also in» lists files where the name appears in code the server didn't link — check them before calling the list complete. Swift and Kotlin answers are as fresh as the last build.
- **rg stays right** for strings, config keys, routes, comments, file names, and languages with no server.
- **A missing server is the user's to install:** tell them the command `lsp.py servers` prints; meanwhile use rg.
