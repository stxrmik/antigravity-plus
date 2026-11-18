---
name: schedule-task
description: >-
  Manages quota-aware scheduled Antigravity automations (Antigravity's own `"builtin": "schedule"` sidecars in
  `~/.gemini/config/sidecars/agp-<slug>/sidecar.json`) with a `RetrieveUserQuotaSummary` quota gate (`list`, `gate`,
  `create`, `remove`). `list` and `gate` are read-only; `create` and `remove` run ONLY when the user explicitly asks
  (an automation starts recurring background agent runs that spend plan quota).
---

# /schedule-task — quota-aware scheduled Antigravity automations

> **Read-only vs. mutating commands:**
> - `list` and `gate` are **read-only** and safe to run whenever inspecting scheduled tasks or checking quota thresholds.
> - `create` and `remove` run **only when the user explicitly asks**. Antigravity Plus's guard asks the user to confirm every `schedule_task.py create|remove` and any write under `~/.gemini/config/sidecars`, so creating or deleting an automation is always the user's decision.

Helper: `python3 "<skill_dir>/scripts/schedule_task.py"` (below: `schedule_task.py`).
Because it reads/writes `~/.gemini/config/sidecars/` (outside the workspace) and queries Antigravity's local Language Server, run commands with `BypassSandbox: true` when sandbox blocks home/localhost access.

## Commands

```bash
schedule_task.py list   [--json]
schedule_task.py gate   [--group gemini|claude] [--min-weekly 15] [--min-5h 20] [--json]
schedule_task.py create --slug SLUG --schedule "[CRON_TZ=<zone>] CRON" --prompt "..." [--title "..."] \
                        [--group gemini|claude] [--min-weekly 15] [--min-5h 20] \
                        [--workspace DIR] [--grant "command(npm test)" ...] [--overwrite] [--json]
schedule_task.py remove --slug SLUG
```

## What `create` writes

Antigravity's scheduled-automation format — `~/.gemini/config/sidecars/agp-<slug>/sidecar.json`:

```json
{
  "builtin": "schedule",
  "args": ["CRON_TZ=Europe/Berlin 0 9 * * 1-5", "agentapi", "new-conversation", "--", "<Step 0 gate + prompt>"],
  "restart_policy": "always",
  "display_name": "Nightly Audit",
  "description": "…",
  "agent_permissions": {"workspace_uris": ["file:///…"], "access_grants": ["command(python3 …/schedule_task.py gate …)"]}
}
```

- **Every run is a fresh conversation** with no memory of earlier runs: write a self-contained prompt (sources, actions, output).
- **Ids:** the task is saved as `agp-<slug>` (`^[a-z0-9][a-z0-9-]{1,44}$`). That prefix marks the automations this skill created: `remove` and `--overwrite` touch only `agp-…` tasks; the user's own automations (made with Antigravity's `/automation`) appear in `list` as read-only, and `remove` refuses them with exit `2`.
- **Schedule:** 5 cron fields (`minute hour day-of-month month day-of-week`, characters `0-9*/,-`, `min 0-59`, `hour 0-23`, `dom 1-31`, `mon 1-12`, `dow 0-7`) in the user's local time. Without a prefix, `create` adds `CRON_TZ=<this computer's IANA zone>` when it can tell it ($TZ, `/etc/localtime`); on Windows, or for another zone, pass it yourself: `--schedule "CRON_TZ=Europe/Berlin 0 9 * * *"` (an IANA name — never `PST` or `+02:00`).
- **Quota gate (Step 0):** when `--min-weekly > 0` or `--min-5h > 0` (defaults `15` and `20`), the prompt starts with Step 0 — run `schedule_task.py gate …`, stop on exit `3` (or when the command can't run), continue on `0` — and that exact command is granted (`command(…)`). `--min-weekly 0 --min-5h 0` omits the gate.
- **Permissions:** unattended runs **auto-deny every command that isn't granted**. `--workspace DIR` grants the folder (`workspace_uris`) and names it in the prompt; `--grant` adds what the task needs (`command(<command prefix>)`, `read_file(<abs path>)`, `write_file(<abs path>)`, `mcp(<server>/<tool>)`, `read_url(<domain>)`; wildcards are refused). Best: offer one test run of the prompt in a normal conversation first, then `create … --overwrite` with the grants it really needed.

## After `create`

- **Never enable the automation yourself.** Tell the user it runs only after they enable «display_name» in the **Automations** dashboard: `[Open Automations Dashboard](sidecar://dashboard)`, and that runs auto-deny commands that aren't granted (`create` prints both).
- Say the schedule with its zone («every weekday at 9:00 Europe/Berlin time»).

## How `gate` works

- Queries `RetrieveUserQuotaSummary` from Antigravity's Language Server (`ANTIGRAVITY_LS_ADDRESS` + `ANTIGRAVITY_CSRF_TOKEN`, or process discovery via `compat.processes` / `compat.listening_ports`).
- If the Language Server is unreachable, falls back to `~/.gemini/antigravity-plus/quota.json` when fresh within 15 minutes.
- Evaluates the selected quota group's `weekly` and `5h` buckets:
  - `disabled: true` buckets are ignored.
  - Missing `remainingFraction` means `0.0` (proto3 JSON omits zeros).
- **Exit codes:**
  - `0` (`RUN: ...`) — all active buckets meet thresholds, or quota is unavailable (`RUN: quota unavailable — proceeding`).
  - `3` (`SKIP: ...`) — `weekly < --min-weekly` or `5h < --min-5h` (prints reset time when reported).

## Rules for `create` and `remove`

- **Ask/confirm before `create` or `remove`:** never register, overwrite, or delete scheduled automations on your own. Confirm the schedule (with its zone), prompt, workspace, grants and quota thresholds with the user first.
- **Overwrite & remove safety:** `create` refuses to overwrite an existing task unless `--overwrite` is given, and never overwrites a sidecar that isn't a schedule sidecar (exit `2`). `remove` deletes only an `agp-…` folder whose `sidecar.json` has `"builtin": "schedule"`; anything else is refused with exit `2` — the user disables or deletes their own automations in the Automations dashboard.
