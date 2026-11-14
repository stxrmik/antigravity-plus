# Antigravity Plus

Claude Code-style engineering discipline plugin for **Google Antigravity 2.x** (21 skills, 7 subagents, 6 hooks, always-on rules, and a live token-usage sidecar). Pure Python 3.8+ standard library (plus bundled Node 24 for the usage sidecar), cross-platform across macOS, Linux, and Windows.

## Commands
- Install / update into `~/.gemini/config/plugins/antigravity-plus`: `./install.sh` (Windows: `.\install.cmd`, or `python3 install.py`)
- Uninstall: `./install.sh --uninstall` (`--purge` also deletes `~/.gemini/antigravity-plus`)
- Test all (offline, isolated temporary `HOME`): `python3 tests/run_tests.py`
- Single test suite: `python3 tests/run_tests.py test_speed` (any `tests/test_<name>.py` without `.py`)
- Plugin health & install check: `python3 antigravity-plus/skills/doctor/scripts/doctor.py` · post-install diff check: `python3 antigravity-plus/skills/doctor/scripts/doctor.py --install-check`
- Token usage CLI: `python3 antigravity-plus/skills/usage/scripts/usage.py --all --days 7`

## Architecture
- `antigravity-plus/plugin.json` & `antigravity-plus/hooks.json` — plugin manifest and hook wiring (`SessionStart`, `PreInvocation`, `PreToolUse`, `PostToolUse`, `Stop`). `install.py` rewrites `python3` in the installed `hooks.json` to `py -3 -X utf8` on Windows.
- `antigravity-plus/hooks/` — runtime hooks and shared state (`~/.gemini/antigravity-plus/state/`):
  - `session_start.py` — git snapshot, project rules (`AGENTS.md` / `CLAUDE.md` bridge), recent conversations, memory index, sandbox memory.
  - `instructions.py` — `PreInvocation` dispatcher: re-injects user constraints, edit-check errors (`check_edit.py`), active peers (`coord.py`), parallel hints (`parallel.py`), sandbox/context/error-loop hints (`speed.py`), memory recall/hints (`memory_hints.py`), fact-grounding (`facts.py`), and objectivity hints (`objective.py`).
  - `guard.py` — `PreToolUse` safety guard: destructive commands, user-stated bans, secret reads/writes, cross-agent stale-file protection, and permission-file protection.
  - `check_edit.py` & `checks.py` — `PostToolUse` syntax and project-linter checks on written/edited files.
  - `stop.py` — `Stop` hook: open `task.md` items, syntax errors, unverified UI/mobile/code changes, announce-only turns, agreed-without-looking replies, fact-checking of claims/URLs/numbers, and end-of-work memory reminders.
  - `conversation.py` & `compat.py` — incremental transcript parser and OS abstractions (file locks, atomic replace, LS process/port discovery, browser/Node lookup).
- `antigravity-plus/skills/<name>/` — `SKILL.md` instructions and helper scripts under `scripts/` (plus `skills/usage/pricing.json` for model rates, context windows, and compaction thresholds).
- `antigravity-plus/agents/*.md` — read-only reviewers/researchers (`code-reviewer`, `security-auditor`, `explorer`, `researcher`) and write-capable specialists (`worker`, `debugger`, `test-writer`).
- `antigravity-plus/rules/*.md` — always-on rules (`AGENTS.md`, `FACTS.md`, `OBJECTIVE.md`).
- `antigravity-plus/sidecars/usage/` — auxiliary pane UI (`sidecar.json`, `server.mjs`, `index.html`) calling `usage.py --json` / `--recent` and Antigravity's `RetrieveUserQuotaSummary` RPC.
- `tests/` — standalone offline process tests (`test_*.py`) executed by `tests/run_tests.py`.

## Conventions
- **Zero external Python dependencies:** keep all Python scripts compatible with Python 3.8+ (`from __future__ import annotations`, stdlib only) — hook PATH on macOS often resolves `/usr/bin/python3` (3.9), and CI runs Python 3.9 and 3.13 across Ubuntu, macOS, and Windows.
- **Cross-platform paths and locks:** use `hooks/compat.py` for file locking and atomic file replacement; pass `-X utf8` / explicit `encoding="utf-8"` for file and pipe I/O.
- **Public repository hygiene:** never include real hostnames, emails, private project names, bundle IDs, device names, or verbatim user chat quotes in code, tests, comments, or docs — use neutral synthetic examples.
- **Rule files:** every file in `antigravity-plus/rules/` requires `trigger: always_on` YAML frontmatter and must remain under Antigravity's 24 KB per-file limit.

## Gotchas
- Editing files in this repository does **not** update the live Antigravity installation until `./install.sh` is run.
- `sidecars/usage/index.html`, `usage.py`, and `pricing.json` are read per request, whereas changes to `sidecars/usage/server.mjs` require restarting the `antigravity-plus/usage` sidecar via Language Server RPC (`ManageSidecar`).
- Hooks must never crash or break a conversation on malformed input; `PreToolUse` returning `{}` means **deny** in Antigravity (use `{"decision": "allow"}` for pass-through).
