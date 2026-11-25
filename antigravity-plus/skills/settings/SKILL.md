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
| agents' commands slow down the user's own work (renders, builds; «агенты тормозят мой рендер») | `settings.py plugin agent-priority below_normal` (or `idle`; back: `normal`): agent commands run at a lower OS priority, also in Bypass Mode. Already running commands are lowered within seconds, new ones right away |
| at most N heavy programs of agents at once (ffmpeg, blender, HandBrakeCLI, xcodebuild, gradle…) | `settings.py plugin heavy-max 1` (`0`: no limit); which programs: `settings.py plugin heavy-programs ffmpeg,blender,xcodebuild` (`default`: the built-in list). A heavy command over the limit waits up to 20 s, then the guard refuses it with what runs; also in Bypass Mode |
| a rule for this repository | `/remember` (goes to `AGENTS.md`) |
| something to run automatically (format, tests before finishing) | `/hooks` |
| Antigravity's permission preset, terminal sandbox, auto-run, access to files outside the project, model | the user does this in Antigravity Settings: tell them exactly where. These decide what you may do, so you never change them yourself |
| no permission prompts (Bypass Mode) | the user types «/bypass» themselves (off: «/bypass off»); it stays on for every conversation until then. Only their own message switches it — never you, by a command or by editing settings |

- **Secrets:** pass keys as `--env KEY` without the value. The helper takes the value from the environment or the
  project's `.env`, so it never shows up in the conversation. If the key isn't there, ask the user to add it to
  `.env`.
- **MCP servers** take effect right away. Their tools show up in new conversations.
- **Confirmation:** connecting an MCP server and turning off secret protection stop for the user's confirmation
  (Antigravity Plus guard). That's expected. Never route around it by editing the files directly.
- **Resource limits** (`agent-priority`, `heavy-max`, `heavy-programs`) are the user's choice: change them only when
  the user asks. When the guard refuses a heavy command over the limit, do other work or wait for your background
  command (`command_status`), then run it again; never loosen the limit to get past it. `procs.py heavy` (in the
  plugin's `hooks/`) lists what counts now, `procs.py tree` the agent processes and their priority.
- **Project config files** (`package.json`, `tsconfig.json`, `.gitignore`, Docker, CI…) aren't this skill's job:
  edit them as part of the task.
