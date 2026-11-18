---
name: pr-comments
description: >-
  Fetches GitHub pull request review threads and top-level reviews cleanly via GraphQL (`gh api graphql`) and replies
  to or resolves threads on request. Use when the user asks to read, inspect, fix, reply to, or resolve PR review
  comments (/pr-comments, «комментарии в PR», «ревью треды», «замечания к PR», «ответь на ревью», «закрой треды»), and
  on your own (`list` only, read-only) when inspecting PR feedback on a branch. Never reply to or resolve threads
  without the user's explicit request or full access.
---

# /pr-comments — GitHub PR review threads and replies

Helper: `python3 "<skill_dir>/scripts/pr_comments.py"` (below: `pr_comments.py`).
All subcommands talk to GitHub via `gh` over the network → run them with `BypassSandbox: true`.

## Read-only vs outward GitHub actions

- **`list` is read-only:** you may run `pr_comments.py list` on your own whenever inspecting PR feedback, checking what reviewers asked for, or verifying whether unresolved threads remain.
- **`reply` and `resolve` are outward GitHub actions** (visible to collaborators on GitHub): run them **ONLY** when the user explicitly asks («ответь в PR», «закрой тред», «resolve the threads», …) or under **full access** (rules: "Full access"). Otherwise, prepare the local fixes/proposed replies and ask before posting or resolving anything.

## Commands

```bash
pr_comments.py list    [--workspace DIR] [--pr NUMBER|URL] [--all] [--json]
pr_comments.py reply   --thread PRRT_... (--body TEXT | --body-file PATH) [--hostname HOST] [--workspace DIR]
pr_comments.py resolve --thread PRRT_... [--unresolve] [--hostname HOST] [--workspace DIR]
```

## Workflow

1. **Fetch unresolved threads** (defaults to the current branch's PR; pass `--pr <N>` for another PR, `--all` to include resolved threads, `--json` for structured output):
   ```bash
   python3 "<skill_dir>/scripts/pr_comments.py" list --workspace "<ws>"
   ```
   Each thread prints its index, GraphQL thread ID (`PRRT_...`), `file:line`, `[OUTDATED]`/`[RESOLVED]` badges, the last 8 lines of `diffHunk`, and all comments (long threads are fetched page by page; a page that can't be fetched shows `[TRUNCATED] N of M comments shown`), followed by non-empty top-level PR reviews.
   On **GitHub Enterprise** the PR's host is queried (`gh api --hostname`, taken from the PR URL) and `list` prints `Host: <host>` — pass the same `--hostname <host>` to `reply` and `resolve`.
2. **Handle feedback locally:** read the referenced file/lines, address valid review comments in code, and run the project's tests/checks.
3. **Reply or resolve (only when requested or under full access):**
   - For multi-line replies or replies containing special shell characters, write the text to a temp file and pass `--body-file <path>` (delete the temp file afterward), or pass `--body "..."` for short replies:
     ```bash
     python3 "<skill_dir>/scripts/pr_comments.py" reply --thread PRRT_... --body "Fixed in <commit>." --workspace "<ws>"
     ```
   - Resolve a thread once addressed (pass `--unresolve` to reopen):
     ```bash
     python3 "<skill_dir>/scripts/pr_comments.py" resolve --thread PRRT_... --workspace "<ws>"
     ```

## Rules

- Never post replies (`reply`) or change thread state (`resolve` / `--unresolve`) unless the user asked or granted full access.
- If `gh` is missing or not authenticated, tell the user to install `gh` and run `gh auth login` instead of inventing workarounds.
- Thread `diffHunk` and `file:line` may be `[OUTDATED]` if later commits shifted lines — always read the current local file before editing.
