---
name: pr
description: >-
  Prepares a pull request for the current branch: all commits and the full diff against the base, a title and
  description by the repo's template, then, if the user wants, pushes and opens it with gh. Use only when the user
  asked (/pr, «открой PR», «сделай пул-реквест», merge request). Never on your own unless the user granted full access.
---

# /pr — pull request

> **Only on the user's request, or under full access:** the command, the same request in their own words, or full access the user granted (rules: "Full access"). Otherwise, if you got here on your own, don't run it; offer it to the user in one line instead.

## Steps

1. **Gather context** (sandboxed):
   ```bash
   bash <skill_dir>/../commit/scripts/git_context.sh pr [base]
   ```
   Pass `base` only if the user named it. If the base can't be detected, ask.
   - If on the base branch itself → ask whether to create a feature branch first (suggest a name from the changes).
   - If there are uncommitted changes → ask whether to `/commit` them first.
   - If the diff was truncated, read the most important changed files directly.

2. **Understand the change as a whole** — look at **all** commits in the range, not just the last one.
   Identify: the problem/motivation, the approach, user-visible effects, risky areas, migration or
   config steps, and how it was (or should be) tested. Don't guess test results — run the project's
   tests if cheap, otherwise state what should be tested.

3. **Write the PR** (English unless the repo uses another language). If a PR template exists, fill it in; otherwise:

   ```markdown
   ## Summary
   <1–3 sentences: what and why.>

   ## Changes
   - <grouped, meaningful bullets — not a file list>

   ## Testing
   - <commands run and results / manual steps>

   ## Risks & notes
   - <breaking changes, migrations, flags, follow-ups> (omit if none)
   ```
   Title: ≤ 70 chars, imperative, matches commit style.

4. **Save** the title + body to `<artifact_dir>/pr.md` (user-facing artifact) so the user can review/edit it.

5. **Open the PR only if the user asked to create it** (not just to describe it):
   - Pushing and `gh` need network → run those commands with `BypassSandbox: true`.
   - Push if needed: `git push -u origin <branch>` (never force-push).
   - Create: `gh pr create --base <base> --title "<title>" --body-file <artifact_dir>/pr.md` (add `--draft` if the user said draft).
   - If `gh` is missing or not authenticated, give the user the compare URL / instructions instead of trying workarounds.

6. **Report** (in the user's language): PR link (or the saved description), and any open questions.
