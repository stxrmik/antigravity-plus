---
name: settings
description: >-
  Shows what is set where (Antigravity's permissions, this project in Antigravity, MCP servers, Antigravity Plus
  settings, project rules and hooks, git) and changes the settings the user asks for: connects or removes MCP
  servers, toggles the plugin's behavior. Use when the user asks to change a setting or connect a tool («подключи
  MCP …», «выключи напоминания», «какие у меня настройки», /settings), and on your own (show only) before you
  tell the user something about their setup. Helper: scripts/settings.py (show, mcp, plugin).
---

# /settings — what is set where

Helper: `python3 "<skill_dir>/scripts/settings.py"`. It reads and writes `~/.gemini/`, which is outside the
workspace, so run it with `BypassSandbox: true`.

1. **Look first:** `settings.py show --workspace "<ws>"`. Never guess where a setting lives.
2. **Change what the user asked for** in their own words. Say what you changed in one line.

| The user wants | Do |
|---|---|
| to connect an MCP server (a local program) | `settings.py mcp add <name> --command <cmd> [--env KEY] -- <args…>`, e.g. `mcp add github --command npx --env GITHUB_TOKEN -- -y @modelcontextprotocol/server-github` |
| to connect a remote MCP server | `settings.py mcp add <name> --url https://…/mcp` (Streamable HTTP; for an old `/sse` server, wrap it with `npx mcp-remote <url>` as a command) |
| to remove a server / see the list | `settings.py mcp remove <name>` / `settings.py mcp list` |
| plugin behavior: reminders, auto-review, auto-memory, secret protection, setting up projects on its own | `settings.py plugin <key> on\|off` (keys are listed by `show`) |
| a rule for this repository | `/remember` (goes to `AGENTS.md`) |
| something to run automatically (format, tests before finishing) | `/hooks` |
| Antigravity's permission preset, terminal sandbox, auto-run, access to files outside the project, model | the user does this in Antigravity Settings: tell them exactly where. These decide what you may do, so you never change them yourself |

- **Secrets:** pass keys as `--env KEY` without the value. The helper takes the value from the environment or the
  project's `.env`, so it never shows up in the conversation. If the key isn't there, ask the user to add it to
  `.env`.
- **MCP servers** take effect right away. Their tools show up in new conversations.
- **Confirmation:** connecting an MCP server and turning off secret protection stop for the user's confirmation
  (Antigravity Plus guard). That's expected. Never route around it by editing the files directly.
- **Project config files** (`package.json`, `tsconfig.json`, `.gitignore`, Docker, CI…) aren't this skill's job:
  edit them as part of the task.
