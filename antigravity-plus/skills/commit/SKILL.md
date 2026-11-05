---
name: commit
description: >-
  Creates well-formed git commits from the current changes: checks for secrets and junk, matches the repo's
  message style, splits unrelated changes, handles pre-commit hooks safely. Use only when the user asked to
  commit: /commit or in their own words («закоммить», «сохрани в git», «сделай коммит»). Never on your own unless the user granted full access; when
  finished work is uncommitted, you may offer it in one line.
---

# /commit — smart commits

**Only on the user's request, or under full access:** /commit, asking to commit in their own words («закоммить», «сохрани в git»), or full access the user granted (rules: "Full access"). That is permission to **commit**; pushing is part of /pr (or of full access when the task needs it). Otherwise, if you got here on your own, don't commit; offer it in one line instead.

## Steps

1. **Gather context** (one call, sandboxed):
   ```bash
   bash <skill_dir>/scripts/git_context.sh commit
   ```
   If it says "not inside a git repository", tell the user and stop.

2. **Safety gate — stop and ask** if any of these show up:
   - `POSSIBLE SECRETS` or risky files (`.env`, keys, credentials) that would be committed;
   - large binaries / build output / `node_modules`-like junk — suggest a `.gitignore` entry instead;
   - nothing to commit → say so and stop.

3. **Decide what goes in.**
   - **Other agents working here** (an `<other_agents>` block before your step, or
     `python3 "<skill_dir>/../../hooks/coord.py" status --workspace "<ws>"` lists someone): commit only your own files,
     `python3 "<skill_dir>/../../hooks/coord.py" mine --conversation <your id> --workspace "<ws>"`. Leave theirs alone.
   - If something is already staged, commit **only the staged changes** unless the user asked for "everything".
   - If nothing is staged, stage the relevant changes by **explicit paths** (`git add <paths>`), never blind `git add -A` when untracked junk exists.
   - If the changes contain clearly unrelated concerns (e.g. a bug fix + an unrelated refactor + a dependency bump), propose a split into logical commits and do it, unless the user said "one commit". `git add -p` is interactive — avoid it; split by files, and if one file mixes concerns, keep it in one commit and mention it.

4. **Write the message** matching the repo's recent style (Conventional Commits if the log uses them, otherwise mirror what you see):
   - Subject: imperative, ≤ 72 chars, no trailing period, describes **why/what**, not "update files".
   - Body (when non-trivial): wrapped at 72, explains motivation and notable details; reference issues if known (`Fixes #123`).
   - English, unless the history is in another language.

5. **Commit** with a heredoc to preserve formatting:
   ```bash
   git commit -F - <<'EOF'
   type(scope): subject

   Body…
   EOF
   ```

6. **If a pre-commit hook fails:** read the output, fix the actual problem (or re-stage files the hook auto-formatted), and run the commit again as a **new** commit. Never use `--no-verify`, never `--amend` unless the user asked and the commit is unpushed.

7. **Report** (in the user's language): commit hash(es) + subjects (`git log --oneline -n <k>`), and what remains uncommitted. Do **not** push; offer `/pr` or `git push` as a next step.
