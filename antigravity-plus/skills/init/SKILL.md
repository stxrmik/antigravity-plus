---
name: init
description: >-
  Analyzes the repository and creates or improves AGENTS.md, the project rules file Antigravity loads
  automatically (Claude Code's /init and CLAUDE.md): commands, architecture, conventions, gotchas. Run it
  yourself, without asking, when you start real work (changing files) in a project that has no AGENTS.md, GEMINI.md
  or CLAUDE.md — in a new project, after the first working version — and when the user asks (/init,
  «инициализируй проект», «создай AGENTS.md»).
---

# /init — create project memory (AGENTS.md)

## When you run it on your own
You don't need to ask: AGENTS.md is a local file in the project, and the user can edit or delete it.
- **When:** you're about to change files in a project with no `AGENTS.md`, `GEMINI.md` or `CLAUDE.md`. Run it
  before the work, since it also finds the commands you'll check your work with. In a new or empty project, run it
  after the first working version, when there's something to describe.
- **Skip it** when the user only asked a question, when the user said not to, or when you're a subagent.
- **Don't stop to announce it**: a message on its own ends your turn. Mention it in the same message as your
  first tool call («Запускаю /init: в проекте ещё нет AGENTS.md»), or just in your final report. Keep the scan
  short (steps 1, 2, 4 and 5), then go on with the user's task in the same turn. Report it in one line
  («Создал AGENTS.md: команды, архитектура, соглашения») instead of step 6's full report.
- **If the project has `GEMINI.md`**, don't create `AGENTS.md` next to it: it already has rules.

Goal: a **concise, high-signal** `AGENTS.md` at the repo root that lets a future agent be productive
immediately. Every line must be specific to this repo. Generic advice ("write clean code", "add tests")
is noise — leave it out.

## Steps

1. **Scan.** Run the helper from the repo root (sandboxed, read-only):
   ```bash
   python3 <skill_dir>/scripts/repo_scan.py .
   ```
   It prints layout, languages, manifests & scripts, CI commands, existing instruction files
   (`AGENTS.md`, `GEMINI.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules/`, `.github/copilot-instructions.md`…),
   README head and recent commit style.

2. **Dig deeper where the scan is thin.** Read the key entry points, a representative module, one or
   two tests, and CI config to learn: how to run a **single** test, real architecture boundaries, code
   generation steps, env/setup requirements, non-obvious conventions. For large repos, delegate this
   exploration to the `explorer` subagent (fast, read-only; fall back to the built-in `research`)
   and ask for a structured summary.

3. **Merge, don't clobber.**
   - If `AGENTS.md` exists: improve it in place — keep correct content, fix stale commands, fill gaps. Show the diff summary.
   - If `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*` or `copilot-instructions.md` exist: import their still-valid, important parts.
   - If `GEMINI.md` exists and no `AGENTS.md`: ask whether to update `GEMINI.md` instead of creating a second file.

4. **Write `AGENTS.md`** using this skeleton (drop empty sections; target ≤ 150 lines):

   ```markdown
   # <Project name>

   <1–3 sentences: what this is, main stack.>

   ## Commands
   - Install: `…`
   - Dev server: `…`
   - Build: `…`
   - Test all: `…` · single file: `…` · single test: `…`
   - Lint / format / typecheck: `…`

   ## Architecture
   <The big picture that requires reading several files to understand: main modules and how they
   interact, data flow, where state lives, key abstractions. Point to directories, not every file.>

   ## Conventions
   <Only project-specific rules actually observed in the code: naming, error handling, imports,
   file placement, test style, commit message style.>

   ## Gotchas
   <Non-obvious traps: required env vars, generated files not to edit, flaky tests, ordering
   requirements, platform quirks.>
   ```

5. **Verify** every command you wrote: confirm it exists in the manifest/Makefile/CI. Run cheap,
   safe ones (e.g. `--help`, lint, a quick test) when feasible; don't run deploys or migrations.
   Mark anything unverified with `(unverified)`.

6. **Report** in the user's language: what was created/changed, notable findings, and what you couldn't determine.
   Mention:
   - teams also using Claude Code can share one file via `ln -s AGENTS.md CLAUDE.md`;
   - `/remember <fact>` adds new facts later;
   - subdirectory-specific rules can go into nested `AGENTS.md` files (they load when working in that directory).

## Rules
- Never invent commands, scripts or paths.
- No secrets, tokens, internal URLs with credentials, or personal data in AGENTS.md.
- Don't list every file or every dependency — the agent can discover those itself.
