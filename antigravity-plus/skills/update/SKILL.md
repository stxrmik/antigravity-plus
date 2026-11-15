---
name: update
description: >-
  Updates Antigravity Plus to its newest release in one step, rolls an update back, and says which version is
  installed and whether a newer one is out. Use when the user asks to update the plugin («обнови Antigravity Plus»,
  «обнови плагин», /update), agrees to the new-release notice from the session context, asks to go back to the
  previous version, or asks about the plugin's version. Helper: scripts/update.py (status, check, run, rollback).
---

# /update — Antigravity Plus updates

Helper: `python3 "<skill_dir>/scripts/update.py"`. It works outside the workspace and goes to the network
(GitHub, or the source clone's remote), so run it with `BypassSandbox: true`.

**Only on the user's words.** The new-release notice in your session context is for telling the user (once, in one
line, at the end of a reply), not for running the update yourself.

| The user wants | Run |
|---|---|
| to update | `update.py run` |
| the installed version / is there a newer one | `update.py check` (goes to the network) — `status` shows what is known without it |
| the previous version back (something broke after an update) | `update.py rollback` — running it again goes forward |
| no daily check, or updates installed by themselves | `/settings`: `plugin update-check off` / `plugin auto-update on` |

After `run`, tell the user in a few lines, in their language: from which version to which, what's new (the list it
prints), what it says to do (new conversations get the update; restart Antigravity only when it says a new command
appeared), and any problem the check after installing found.

If it stops («Not updated: …» — uncommitted changes in the source folder, another branch, commits of its own,
GitHub not answering), tell the user why and what it suggests. Don't commit, stash, switch branches or reset the
user's repository to get past it unless they ask.
