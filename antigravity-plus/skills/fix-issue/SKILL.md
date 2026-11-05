---
name: fix-issue
description: >-
  Takes a GitHub issue from description to a verified fix: reads it with gh, reproduces the problem, fixes the
  root cause with a regression test on its own branch, runs the checks and prepares the commit and PR. Use when
  the user points to an issue and asks to fix it (/fix-issue 42, «почини issue #123», an issue link with
  «почини»). Not on your own unless the user granted full access.
---

# /fix-issue — from GitHub issue to a verified fix

> **Only on the user's request, or under full access:** the command, the same request in their own words, or full access the user granted (rules: "Full access"). Otherwise, if you got here on your own, don't run it; offer it to the user in one line instead.

Invoking this skill is permission to create a local branch and edit code. It is **not** permission to
push, open a PR or comment on the issue — offer those at the end.

## Steps
1. **Read the issue** (network → `BypassSandbox: true`):
   `gh issue view <n> --comments` (add `-R owner/repo` if the remote is ambiguous). If `gh` is missing
   or not authenticated, ask the user to paste the issue text. Summarize the expected vs actual
   behavior and acceptance criteria in 2–4 lines.
2. **Prepare a branch.** If there are uncommitted changes, ask before touching anything. If on the
   default branch: `git switch -c fix/<n>-<short-slug>`.
3. **Locate** the relevant code (delegate broad searches to the `explorer` subagent and keep reading what
   you already know meanwhile). Read the code paths and the existing tests around them.
4. **Reproduce** — ideally as a failing test in the project's framework; otherwise a minimal script or
   command. Record the exact command and the failing output. If you can't reproduce, report what you
   tried and ask for details instead of guessing a fix.
5. **Fix the root cause** with the smallest correct change (hard cases → `debugger` subagent).
6. **Verify:** the reproduction now passes, the related suite passes, lint/type-check are clean.
7. **Wrap up (in the user's language):** root cause, the fix (`file:line` links), the regression test, verification
   commands + results. Offer: `/commit` (message referencing `Fixes #<n>`), then `/pr`.
