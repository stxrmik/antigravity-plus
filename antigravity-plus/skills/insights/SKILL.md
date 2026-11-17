---
name: insights
description: >-
  Read-only analytics of what Antigravity Plus hooks did and how agents reacted: PreInvocation hint counts by hook,
  Stop hook blocks and whether agents acted or re-ended with the same block, guard.log decisions and prompt outcomes,
  edit-check errors, and top false-positive candidates to review. Use when the user asks (/insights, «статистика
  хуков», «что делали хуки», «ложные срабатывания»), or when tuning the plugin's hooks.
---

# /insights — hook activity and false-positive signals

Read-only report from [insights.py](scripts/insights.py) across recent transcripts (`EPHEMERAL_MESSAGE`, Stop blocks, denied/timed-out tool steps), `~/.gemini/antigravity-plus/guard.log`, and `~/.gemini/antigravity-plus/state/*.checks.json`. Secrets and home paths are masked and `«…»` quotes are dropped.

## Steps

1. **Pick options from the request:**
   - Period → `--days N` (default `7`).
   - Single project → `--workspace "<workspace-root>"`.
   - Structured output → `--json`.

2. **Run it** (read-only; reads `~/.gemini/`, so run with `BypassSandbox: true` if the sandbox blocks access):
   ```bash
   python3 "<skill_dir>/scripts/insights.py" --days 7
   ```

3. **Present the summary in the user's language:**
   - Highlight the key counts: scanned conversations, top PreInvocation hints, Stop hook blocks (how often agents `acted` vs `re-ended` or got the `same block again`), guard decisions and prompt outcomes (`asks ran`, `timed out`, `refused`), edit-check counts, and the highest-scoring review candidates.
   - Never print raw user messages or unmasked credentials; do not alter reported counts.
   - If the user asked to tune the plugin, investigate the top review candidates (score 5 repeated identical Stop blocks, score 3 repeated guard denials or repeated block kinds, score 2 user-approved asks) before proposing rule changes.
