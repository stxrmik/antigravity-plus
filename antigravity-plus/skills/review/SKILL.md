---
name: review
description: >-
  Independent code review of changes (uncommitted work, a branch, a commit range, a GitHub PR or given files) by
  the code-reviewer subagent, plus security-auditor for sensitive code; findings are verified and ranked by
  severity, with fixes. Use when the user asks for a review («посмотри код», «проверь изменения», /review) or
  before /commit and /pr, and on your own after you finish a substantial change (several files or tricky logic),
  before reporting it done.
---

# /review — independent code review

## 1. Determine scope
From the user's words (default = uncommitted changes):

| User says | Target |
|---|---|
| nothing / "my changes" | `uncommitted` |
| "staged" | `staged` |
| "branch", "before PR" | `branch` |
| a SHA / ref / `a..b` | that ref or range |
| PR number / URL | `gh pr diff <n>` and `gh pr view <n>` (network → `BypassSandbox: true`) |
| file paths | review those files in full (not a diff) |
| you run it on your own, after your own change | the files you changed in this turn: the diff (`uncommitted`) in a git repo, the files in full otherwise |

Gather the diff (sandboxed):
```bash
bash <skill_dir>/../commit/scripts/git_context.sh review <target>
```
If the diff is empty, say so and stop. Read untracked/new files in full.

## 2. Build context for reviewers
Collect: the changed-file list, the diff (or the path to re-run the script), the project's
`AGENTS.md` conventions, and the intent of the change (from the user, commit messages, or PR text).

## 3. Fan out (parallel subagents)
Launch in parallel, each with the full context above and the instruction to return findings in the
format below:
- **`code-reviewer`** — correctness, edge cases, error handling, concurrency, API/contract breaks, performance, readability, tests.
- **`security-auditor`** — when the user asked for a review: only if the diff touches auth, input handling, SQL/shell/HTML construction, file paths, crypto, secrets, permissions, network or deserialization.
- For large diffs (> ~800 changed lines or many unrelated areas), split by area and launch one `code-reviewer` per area.

Invoke them by `TypeName` (`code-reviewer`, `security-auditor`; some builds list them namespaced, e.g.
`antigravity-plus:code-reviewer` — use whatever is listed among your available subagents). If they're
unavailable, use `research` and paste the corresponding
agent instructions from `<plugin_dir>/agents/<name>.md` into the prompt.

Required finding format:
```
[SEVERITY] file:line — title
Problem: what is wrong and the concrete scenario where it breaks.
Fix: specific change (code if short).
Confidence: high | medium
```
Severities: `CRITICAL` (bug/security issue that will hit users or data), `HIGH` (likely bug or significant risk),
`MEDIUM` (edge case, maintainability problem with real cost), `LOW` (minor; style only if it violates project conventions).

## 4. Verify before reporting
For every CRITICAL/HIGH finding, open the code yourself and confirm it's real (check callers, existing
guards, tests). Drop false positives and duplicates. Drop pure style nits unless AGENTS.md mandates them.

## 5. Report (in the user's language, concise)
- One-line verdict: ✅ можно мержить / ⚠️ есть замечания / ⛔ блокеры.
- Findings grouped by severity, each with a clickable `file:line` link, problem, and fix.
- "Что сделано хорошо" — at most 2 bullets, only if genuinely notable.
- Offer to apply fixes. **Do not modify code during the review** unless the user asks.

## When you run it on your own
After a substantial change of your own (several files or tricky logic), before you report the work done:
- Say it in one line first, e.g. «Запускаю /review: изменено 5 файлов».
- Run `code-reviewer`; add `security-auditor` only for security-sensitive code (see step 3).
- Fix the confirmed CRITICAL/HIGH findings that your change introduced right away: that's finishing your own task, not new scope. Then re-run the project's checks.
- In your final report, add one line on what the review found and what you fixed. List anything you didn't fix (pre-existing issues, judgment calls) for the user.
