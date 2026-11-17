---
name: context
description: >-
  Read-only breakdown of what fills the current conversation's context window: fixed overhead (system prompt, tool
  definitions, rules, skills list), SessionStart context, compaction summary, user messages, model text and thinking,
  tool arguments and outputs per tool, agent/task messages, live hook hints, top 10 biggest items, and tips to save
  space. Use when the user asks (/context, «что занимает контекст», «почему контекст переполнен»), and on your own
  when speed/context hints warn (~150k / ~220k tokens) to find what is bloating the context.
---

# /context — what fills the context window

> **On your own:** read-only — run it when `<context_budget>` warns (around ~150k or ~220k tokens) so you act on what actually fills the context instead of guessing.

The script [context.py](scripts/context.py) reads the conversation's transcript since the last compaction (`CHECKPOINT`), counts only live hook hints (ephemeral messages last one step), calibrates estimates against the last `PLANNER_RESPONSE` token count (`input_tokens + cache_read_tokens`), and breaks everything down by category.

## Steps

1. **Pick the target:**
   - Default → the current conversation (`--conversation <conversation-id>` from your system context, also the `<id>` segment of your artifact directory path).
   - Another project's latest session → `--workspace "<workspace-root>"` (or an explicit `--transcript <path>`).

2. **Run it** (read-only; sandboxed first):
   ```bash
   python3 "<skill_dir>/scripts/context.py" --conversation <conversation-id>
   ```
   Optional flags: `--workspace "<ws>"`, `--model <id>`, `--json`.
   If the sandbox blocks reading `~/.gemini/`, re-run the **same** command with `BypassSandbox: true`.

3. **Present or act on the result:**
   - When the user asked `/context`: paste the category table, top 10 items, and tips (or summarize them faithfully in the user's language without altering numbers).
   - When running on your own: target the biggest drivers shown in the report — switch whole-file reads to `read_file` with `StartLine`/`EndLine`, trim long command output (`| tail`, `| grep`), delegate broad searches to `explorer`, or suggest `/handoff` if near the compaction threshold.
