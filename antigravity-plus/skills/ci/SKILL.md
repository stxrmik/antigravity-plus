---
name: ci
description: >-
  Tracks CI runs and checks (GitHub Actions via gh, GitLab CI via glab): waits for workflows after a push or PR
  without polling loops, shows current run/check status, extracts clean failure logs without blowing up context, and
  guides fixing red pipelines. Use when the user asks about CI, GitHub Actions, pipelines or checks («проверь CI»,
  «дождись экшенов», «почему упал пайплайн», «почини CI», /ci), and on your own right after `git push` or `/pr` in a
  repository with CI workflows, or before `/deploy` to verify `HEAD` isn't red.
---

# /ci — track CI runs, inspect failures, keep pipelines green

Helper: `python3 "<skill_dir>/scripts/ci.py"` (below: `ci.py`). Run `status`, `watch`, `logs`, and `rerun` with
`BypassSandbox: true` (they call `gh` / `glab` over the network; `detect` is local).

## Commands

```bash
ci.py detect --workspace "<ws>"                  # CI workflows in the repo, remote provider, CLI & auth status
ci.py status --workspace "<ws>"                  # runs & jobs for HEAD (or --sha SHA, --branch BR, --pr N)
ci.py watch  --workspace "<ws>"                  # wait for all runs of HEAD to finish; prints failure logs on red
ci.py logs   [RUN_ID] --workspace "<ws>"         # cleaned error logs of failed steps (--job ID/NAME, --lines 160, --full)
ci.py rerun  [RUN_ID] --failed --workspace "<ws>" # re-run failed jobs (only on user request or confirmed infra flake)
```

## How to track CI after `git push` or `/pr`

1. **Start `watch` right after pushing:**
   ```bash
   python3 "<skill_dir>/scripts/ci.py" watch --workspace "<ws>"
   ```
   Pass `--pr <number>` when tracking a pull request's checks, or `--sha <commit>` for a specific commit.
   `watch` waits for newly-triggered runs of the exact pushed SHA to register (never mistaking the previous commit's
   run for the new one) and tracks **every** workflow triggered for that commit until all of them finish.
2. **Let long runs finish in the background — never poll.**
   If CI takes more than a few seconds, Antigravity automatically moves `ci.py watch` into a background task and wakes
   you up when it finishes. Do other independent local work meanwhile; if nothing else is left, end your turn with one
   line («Запушено в `origin/main`, отслеживаю CI в фоне — сообщу результат») and wait for the wakeup message. Never
   run `sleep` or `gh run list` in a loop.
3. **When `watch` finishes:**
   - **Exit `0` (green):** report the result concisely (workflow names, job count, link).
   - **Exit `1` (red / cancelled):** `watch` already prints the failed jobs and cleaned error logs. Read the failure:
     - If more context is needed for a specific matrix leg: `ci.py logs <run_id> --job "<name or id>" --lines 200`.
     - Reproduce the failure locally if possible (check OS/runtime matrix differences: Windows paths, Python/Node
       version, missing env/fixtures), fix the root cause, and run local tests.
     - **Committing & pushing the fix:** follow Git safety — under **full access** or when the user asked to get CI
       green («пушь и добейся зелёного CI», «почини CI и запушь»), commit, push, and run `ci.py watch` again; otherwise
       report the root cause + local fix and ask whether to commit and push.

## Checking CI before `/deploy` or when asked

- Run `ci.py status --workspace "<ws>"` (`--branch <branch>` or `--pr <n>` if asked about another ref).
- If `HEAD` has a failing CI run, stop before deploying and tell the user which job failed (or fix it first).
- If no CLI is installed or authenticated (`detect` tells you whether `gh auth login` or `glab auth login` is needed),
  tell the user the exact command instead of guessing CI status.

## Rules

- Never claim «CI зелёный / прошёл» without a `ci.py` / `gh` / `glab` check backing the current commit.
- Never blindly `rerun --failed` to paper over a real test failure: inspect `ci.py logs` first, and use `rerun` only
  when the user asks or the log clearly shows an external runner/network outage (e.g. GitHub Actions runner download
  5xx).
