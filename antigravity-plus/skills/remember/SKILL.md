---
name: remember
description: >-
  Saves a lasting fact so future conversations know it: repo rules for everyone into AGENTS.md, personal
  preferences and project context into memory (shared with Claude Code). Use when the user says «запомни», «всегда
  / никогда делай X», /remember, or corrects you in a way that should last. On your own, without asking: save
  personal preferences and corrections to memory, and write a lasting rule for the repository that the user stated
  («тесты лежат в…», «используй pnpm») to AGENTS.md, then say so in one line.
---

# /remember — save it for next time

## Steps

1. **Extract the fact** from the user's message as one short, specific statement. Keep the user's own words when they matter.
   - Good: `Use pnpm, never npm — the lockfile is pnpm-lock.yaml.`
   - Bad: `The user prefers a certain package manager.`
   If the request is vague, ask one clarifying question.

2. **Choose where it goes:**

   | The fact is… | Target |
   |---|---|
   | a rule anyone working on this repo needs: commands, conventions, architecture, gotchas | `AGENTS.md` at the repo root |
   | specific to one folder (e.g. only `frontend/`) | `<folder>/AGENTS.md` — Antigravity loads it when working in that folder |
   | about a kind of file or a topic, not one folder (Swift files, migrations, tests, the API layer) | `.agents/rules/<topic>.md` with `trigger: model_decision` (format below) |
   | about the user or how they want the agent to work ("отвечай короче", "не предлагай Docker") — "для всех проектов" | **personal memory**, type `user` or `feedback` |
   | context for this project that isn't in the code: decisions, deadlines, servers, who's who, links | **project memory**, type `project` or `reference` |

   The memory folders are listed in the session context ("Memory" section). If they aren't there, get them with `python3 "<skill_dir>/../memory/scripts/memory.py" paths --workspace "<ws>"` (`BypassSandbox: true`).
   If the user explicitly asks for a global rules file, use `~/.gemini/config/AGENTS.md` (Antigravity loads it in every project).

3. **Write it.**
   - **AGENTS.md:** read the file first. Put the fact in the right section (`## Commands`, `## Conventions`, `## Gotchas`, `## Architecture`; create `## Notes` if nothing fits). If an existing line contradicts the new fact, **replace** that line instead of adding a duplicate. Keep the file's style. If `AGENTS.md` is missing, create it with a `# <Project>` heading and suggest `/init` later.
     If the repo has `CLAUDE.md` but no `AGENTS.md` (a Claude Code project), ask once: write into `CLAUDE.md` (shared with Claude Code users), or create `AGENTS.md`? Suggest `mv CLAUDE.md AGENTS.md && ln -s AGENTS.md CLAUDE.md`, so both tools read one file.
   - **Topic rule** (`.agents/rules/<topic>.md`): the description is what the agent sees in every conversation and
     decides by, so say exactly when it applies:
     ```markdown
     ---
     trigger: model_decision
     description: Use when editing Swift files (*.swift) in this project
     ---
     <the rule>
     ```
     Don't use `trigger: glob`: this Antigravity build ignores it.
   - **Memory:** if a memory already covers the topic, update that file. Otherwise create `<folder>/<kebab-name>.md` in the format from the "Memory" rules:
     ```markdown
     ---
     name: <kebab-name>
     description: <one line>
     metadata:
       type: user | feedback | project | reference
     ---
     <fact>
     **Why:** …            (feedback / project)
     **How to apply:** …   (feedback / project)
     ```
     Don't edit `MEMORY.md`: the index line is added automatically.

4. **Confirm** in one line (in the user's language): what was saved and where, with a link to the file.

## Rules
- Never store secrets, credentials or personal data of other people.
- One fact per bullet (AGENTS.md) or per file (memory). Keep it short.
- Don't rewrite unrelated parts of the file.
