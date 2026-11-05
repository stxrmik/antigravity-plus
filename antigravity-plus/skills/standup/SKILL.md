---
name: standup
description: >-
  Summary of what got done across projects in the last day or week: Antigravity conversations and Claude Code
  sessions, commits, uncommitted work, open handoff notes and unfinished checklists, grouped by project. Use when the
  user asks «что сделано за день / неделю», «итоги», «на чём мы остановились по всем проектам», «стендап», /standup.
---

# /standup — what got done

1. Run (it only reads data, but some of it lives outside the workspace — `BypassSandbox: true`):
   ```bash
   python3 "<skill_dir>/scripts/standup.py" --days 1
   ```
   Use `--days 7` for «за неделю», and `--workspace "<ws>"` when the user asks about one project.
2. Turn it into a short report in the user's language, per project, skipping test and empty conversations:
   - **Сделано** — results, not activity: what works now, what was released or fixed (commits help).
   - **В работе / осталось** — unfinished checklist items, open handoff notes, uncommitted changes.
   - **Риски** — only if visible: failing checks, a deploy that wasn't verified, a restriction in force.
3. End with one line suggesting where to continue, e.g. «Продолжить checkout: открыть новый разговор и написать
   «продолжи»». Don't start anything yourself.
