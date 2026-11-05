---
name: doctor
description: >-
  Health check of the Antigravity Plus setup: what Antigravity loaded (skills, subagents, hooks, rules), hook
  self-tests, tools (git, gh, ripgrep, linters), memory and edit-check status. Use when the user asks (/doctor,
  «плагин не работает»), and on your own when a plugin command, hook, panel or subagent misbehaves (errors in hook
  output, a skill or subagent not found).
---

# /doctor — health check

> **On your own:** run it when a plugin command, hook, panel or subagent misbehaves. Act on the ❌ lines you can fix yourself (e.g. a missing tool the project needs), and tell the user the rest in two lines.

## Steps
1. Run the read-only check with `BypassSandbox: true` — it only reads files and asks the local
   Antigravity process what it loaded, and the sandbox blocks that local connection:
   ```bash
   python3 "<skill_dir>/scripts/doctor.py" --workspace "<workspace-root>"
   ```
   If the user declines running outside the sandbox, run it sandboxed: everything except the
   "Что видит Antigravity" section still works.
2. Paste the report as is.
3. Below it, give at most 3 short next steps in the user's language, only for the ⚠️/❌ lines, e.g.:
   - plugin not visible → restart Antigravity; plugin disabled → enable it on its page
     (`[antigravity-plus](customization://antigravity-plus)`);
   - skills "ещё не подхвачены" → start a new conversation (or restart Antigravity);
   - no `AGENTS.md` → `/init`; `gh` missing / not logged in → `brew install gh`, `gh auth login`;
   - ripgrep missing → `brew install ripgrep`.
   If everything is ✅, say so in one line.
