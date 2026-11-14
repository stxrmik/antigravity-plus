---
trigger: always_on
---
# Antigravity Plus — Git and GitHub

- **Look first.** Before branching, pulling, merging, committing or pushing — and after a git command fails — run the `git` skill's `git_state.py status` (or at least `git status -sb`). «Not a git repository» means: don't run git there.
- **Never wait for input.** Here `GIT_EDITOR=false`, so anything that opens an editor fails, and keyboard prompts hang. `git commit -m "…"` (or `-F -`); `GIT_EDITOR=true git rebase|cherry-pick|revert --continue`; `--no-edit` on `merge` and `revert`; no `-i` / `-p` forms; `GIT_TERMINAL_PROMPT=0` before fetch, pull and push; `git push -q`; `gh pr create --title … --body-file …` (or `--fill`), `gh issue create --title … --body-file …`. `gh auth login` is the user's to run.
- **Small outputs.** `git diff --stat` / `--name-only` first, then `git diff -- <file>`; `git log --oneline -n 20`; `gh … --json <fields> --jq …`. Don't page through one big diff with `head`/`tail`/`sed`.
- **Finish what you start.** Never end your turn with a rebase, merge, cherry-pick or bisect half done, or with `<<<<<<<` / `>>>>>>>` markers in files: finish it, or abort it and say why.
- **History over guesses.** Who changed what and why: `git log -S`, `git blame -L`, `git show`; which commit broke it: `git bisect run`. Conflicts, syncing, undo and GitHub (issues, PRs, review threads, releases): follow the `git` skill.
- **Visible to others = the user's call.** Commits (/commit), pushes and PRs (/pr), and anything on GitHub that others see — comments, reviews, approvals, merges, issues, releases — only on the user's request or under full access.
