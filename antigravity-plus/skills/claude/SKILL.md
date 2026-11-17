---
name: claude
description: >-
  Hand a self-contained sub-task from Antigravity to Claude Code (`claude -p`) in the same folder, monitor it in the
  background, inspect its git diff, and verify its changes. Use ONLY when the user explicitly asks to delegate to
  Claude Code (/claude, «поручи Claude», «передай в Claude Code») or granted full access — it spends the user's
  Claude plan / quota.
---

# /claude — delegate a sub-task to Claude Code

> **Only on the user's request, or under full access:** delegating runs `claude -p` and spends the user's Claude plan or API budget. Never run a task on your own without the user's request or full access (rules: "Full access").

The helper [claude_task.py](scripts/claude_task.py) launches `claude -p` detached in the workspace, tracks changes via a safe git tree snapshot (`write-tree` in a temp index without touching your working index), and lets you inspect the report, diff, and follow up in the same Claude session.

## 1. Check limits first

```bash
python3 "<skill_dir>/scripts/claude_task.py" budget
```
Pass `--claude-5h <pct>` / `--claude-week <pct>` if the user gave them.
- If Claude Code isn't installed or isn't logged in (`NOT logged in`), tell the user in one line and do the work yourself — logging in is theirs to do.
- If Claude's own limit is `critical`, don't delegate; do the work here.

## 2. Start a task (`run`)

Write a concise, self-contained brief (Claude hasn't seen this conversation). `claude_task.py` wraps it with non-interactive rules (stay in the workspace, no git commits/pushes/resets, run checks, end with `STATUS / CHANGED / CHECKS / NOTES`):

```bash
python3 "<skill_dir>/scripts/claude_task.py" run --model sonnet --title "Short task title" --workspace "<workspace-root>" <<'BRIEF'
Goal: <what to build or fix, in 1–2 sentences>
Context: <paths to read first, interfaces, conventions, constraints — never include secrets>
Scope: <files/folders Claude owns; what else is changing and must not be touched>
Done: <exact test/build/lint commands that must pass>
BRIEF
```

- **Models & flags:** `--model sonnet` (default), `opus`, `haiku`, or a full model id; optional `--effort low|medium|high|xhigh|max`, `--allow 'Bash(npm install *)'` (repeatable permission rule), `--permission-mode acceptEdits|plan|auto|dontAsk|manual`, `--max-budget-usd <amount>`.
- **Permissions:** defaults to `acceptEdits`, an allowlist of read/test/build commands, a denylist of destructive/commit/push commands, and `--permission-prompts none` (anything else is refused at once instead of hanging, and listed in the report). Never pass `--dangerously-skip-permissions` unless the user explicitly asked for it.
- If Antigravity's terminal sandbox blocks `claude` from reaching its auth/network, run `run` / `wait` / `send` with `BypassSandbox: true`.

## 3. Wait in the background (`wait`)

`run` prints the task id (first 8 chars are enough) and returns immediately. Start `wait` as a **background `run_command`** — never poll in a loop:

```bash
python3 "<skill_dir>/scripts/claude_task.py" wait <id>
```

Keep working on your own non-overlapping part meanwhile; when nothing else is left, end your turn with one line saying you're waiting for the background task (its completion wakes you up).
- Exit codes: `0` done · `1` failed, canceled, or auth error · `3` still running after `--timeout` (default `1800` s — run `wait <id>` again).

## 4. Verify the work (`result`, `diff`, tests)

Claude's report is a claim, not proof:
1. Check its report (with `--full` if truncated):
   ```bash
   python3 "<skill_dir>/scripts/claude_task.py" result <id> [--full]
   ```
2. Inspect every file change since the task started (works even when the workspace isn't clean):
   ```bash
   python3 "<skill_dir>/scripts/claude_task.py" diff <id> [--stat] [PATH ...]
   ```
3. Open the changed files and run the project's tests/checks yourself before building on them or reporting done.

## 5. Follow-up, cancel, list

- **Follow-up in the same Claude session** (`--resume`, keeps context; then `wait <id>` in the background):
  ```bash
  python3 "<skill_dir>/scripts/claude_task.py" send <id> "Fix the failing edge case in …" [--allow 'Bash(…)']
  ```
- **Stop a running task:** `python3 "<skill_dir>/scripts/claude_task.py" cancel <id>`
- **Recent tasks:** `python3 "<skill_dir>/scripts/claude_task.py" list`
