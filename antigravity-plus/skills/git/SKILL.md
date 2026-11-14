---
name: git
description: >-
  Git and GitHub playbook with a one-look state check: branches and syncing with the remote, a rejected push,
  merge and rebase conflicts, searching history (who changed what and why, log -S, blame, bisect), undoing changes
  safely, stashes and worktrees, and GitHub through gh — issues, pull requests, review threads and comments,
  releases, the API. Run it yourself, without asking, for any git or GitHub work beyond a quick read, when a git
  command failed or a rebase/merge is in progress, and when the user mentions git, a branch, a conflict, a PR,
  an issue or GitHub («подтяни изменения», «разреши конфликт», «кто сменил», «что в PR», /git). Helper:
  scripts/git_state.py status|conflicts|review. Commits, pushes and PRs themselves: /commit and /pr.
---

# /git — Git and GitHub

Run `python3 "<skill_dir>/scripts/git_state.py" status` first (`--path "<repo>"` when you aren't in it; `--fetch`
to compare with the remote; `--github` for gh's login and this branch's PR). It prints the branch, ahead/behind,
an operation in progress, conflicts, changes, stash, last commits, remotes, and the exact next commands. Exit 2 =
not a git repository: say so; don't run git there (/git-init only when the project should have one).

Who decides: reading anything, fetching, branching locally, resolving conflicts, stashing and local undo of your
own work are yours to do. Commits → /commit, push and PRs → /pr (only on the user's request or under full access).
Anything others see on GitHub — comments, reviews, approvals, merges, issues, releases, labels — only when the user
asks for it. Destructive commands (`reset --hard`, `clean -f`, force-push, `branch -D`, `stash drop`) stop at the
guard for the user: say in one line why you need them, or pick a safe way below.

## Never wait for input

Nobody types into this terminal: a keyboard prompt (`-p`, gh's questions, a password) hangs forever. Antigravity sets `GIT_EDITOR=false`, so a git command that wants an editor fails at once — and `-c core.editor=…` can't change that (the variable wins); `GIT_EDITOR=true git …` does. Pagers are off (`PAGER=cat`).

| Instead of | Run |
|---|---|
| `git commit` (opens an editor) | `git commit -m "<subject>" -m "<body>"`, or a long message: `git commit -F - <<'EOF'` … `EOF` |
| `git rebase/cherry-pick/revert --continue`, `git am --continue` | `GIT_EDITOR=true git rebase --continue` (same for the others) |
| `git merge <ref>`, `git revert <sha>` | add `--no-edit`; a finished merge after conflicts: `git commit --no-edit` |
| `git rebase -i` (squash, reorder, edit) | squash the last N: `git reset --soft HEAD~N && git commit -m "…"`; fixups: `git commit --fixup <sha>` then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>` |
| `git add -p`, `checkout -p`, `reset -p`, `stash -p` | name the files; part of a file: edit a copy, or stage with `git apply --cached <patch>` |
| `git tag -a v1.2` | `git tag -a v1.2 -m "v1.2"` |
| fetch/pull/push that may ask for a password | prefix `GIT_TERMINAL_PROMPT=0` (PowerShell: `$env:GIT_TERMINAL_PROMPT=0;`): no credentials → it fails at once; tell the user to sign in (`gh auth login`, an SSH key) |
| `git push` (progress floods the output) | `git push -q` |
| `gh pr create` (asks questions) | `gh pr create --title "…" --body-file <file> --base <base>` or `--fill` |
| `gh issue create` | `gh issue create --title "…" --body-file <file>` |
| `gh auth login` | the user runs it (it opens a browser); `gh auth status` checks |

Output volume: `git diff --stat` or `--name-only` first, then `git diff -- <file>` for the files that matter;
`git log --oneline -n 20`; `git show --stat <sha>` before `git show <sha> -- <file>`. Never page through one huge diff
with `head`/`tail`/`sed` ranges. gh: ask for exactly the fields you need, `--json number,title,state --jq '…'`.

## Branches and syncing

- New work: `git switch -c <type>/<short-name>` from an up-to-date base (`git fetch -q origin`, then
  `git switch -c feat/x origin/<base>`). Don't put work on the default branch when the repo works through PRs
  (`gh pr list --state merged -L 3` shows if it does).
- Bring a branch up to date: your own branch that nobody else pushes to → `git fetch -q origin && git rebase
  origin/<base>`; a shared branch or one already reviewed → `git merge --no-edit origin/<base>`. Local changes in
  the way: `git stash push -m "<why>"` first, `git stash pop` after.
- Pull: `git pull --ff-only -q`; if it refuses, the branches diverged — rebase or merge as above.
- Push rejected («fetch first», «non-fast-forward»): someone pushed before you. `git fetch -q`, rebase (or merge)
  onto the remote branch, run the tests, push again. Never `--force` to fix it; on your own branch after a rebase
  you rewrote, `git push --force-with-lease` is the safe form — it still goes through the guard to the user.
- «no upstream branch»: `git push -q -u origin HEAD`. Detached HEAD with work on it: `git switch -c <name>`.
- Switch branches with uncommitted changes: commit them, or `git stash push -m "<why>"`; `git switch` refuses
  rather than losing work — never `-f`.
- Several tasks at once (another agent, a long build): `git worktree add ../<repo>-<name> -b <branch>` gives a
  second folder on its own branch; remove it with `git worktree remove ../<repo>-<name>` when merged.

## Conflicts

1. `git_state.py conflicts` lists the files, their blocks and line numbers, and which half is which (in a rebase
   the upper half is the branch you rebase onto, the lower half is your commit).
2. For each file, learn why each side changed (`git log --oneline -3 <ours> -- <file>`, the same for theirs;
   `git show :1:<file>` base, `:2:` ours, `:3:` theirs) and write the version that keeps both intentions. Delete
   every `<<<<<<<`, `=======` and `>>>>>>>` line.
3. A whole side wins: `git checkout --ours <file>` / `--theirs <file>`. Lock files: take one side, then regenerate
   (`npm install`, `poetry lock --no-update`, `pod install`…). Binary files: choose a side explicitly.
4. Run the tests, `git add <file>`, then continue (`GIT_EDITOR=true git rebase --continue`, or
   `git commit --no-edit` for a merge). Not sure how to resolve: `git rebase --abort` / `git merge --abort` and
   ask the user — never end your turn with an operation half done or markers left in files.

## History: who, when, why

- What changed a line: `git blame -L <from>,<to> -- <file>`, then `git show <sha>` (`--stat` first).
- When text appeared or vanished: `git log -S '<text>' --oneline -- <path>`; a regex: `git log -G '<re>'`.
- A file's story: `git log --oneline --follow -- <file>`; a function: `git log -L :<name>:<file>`.
- What a branch adds: `git log --oneline <base>..HEAD`, `git diff --stat <base>...HEAD`.
- Which commit broke it: `git bisect start <bad> <good>`, then `git bisect run <command that exits 0 when good>`,
  read the first bad commit, `git bisect reset`. Never leave a bisect running.
- Lost a commit or a branch: `git reflog -n 30`, then `git switch -c rescue <sha>`.

## Undo safely

| Situation | Command |
|---|---|
| unstage a file | `git restore --staged <file>` |
| throw away your own uncommitted edits to one file | `git restore <file>` (other people's or the user's edits: ask first) |
| the last commit, not pushed: redo it | `git reset --soft HEAD~1` (changes stay staged) |
| fix the last unpushed commit | `git commit --amend --no-edit` (or `-m "…"`) — never a pushed or someone else's commit |
| a pushed commit is wrong | `git revert --no-edit <sha>` (a new commit that undoes it) |
| park changes | `git stash push -m "<why>"` · `git stash list` · `git stash pop` |
| start over from the remote | ask the user: it needs `git reset --hard origin/<branch>` (the guard stops it) |

## GitHub with gh

- Check: `gh auth status`; repo: `gh repo view --json nameWithOwner,defaultBranchRef,visibility`.
- Issues: `gh issue list --state open -L 20 --json number,title,labels,assignees`; `gh issue view <n> --comments`.
  Create, comment, label or close only on the user's request (`--title`, `--body-file`).
- Pull requests: `gh pr list -L 20`; `gh pr view <n> --json title,body,state,reviewDecision,headRefName,baseRefName`;
  `gh pr diff <n> --name-only`, then `gh pr diff <n>` for the files you need; `gh pr checkout <n>` to work on it;
  checks and failed logs → /ci. New PR → /pr. Merge (`gh pr merge`), approve or request changes
  (`gh pr review`) only on the user's request — the guard asks before a merge.
- Review feedback: `python3 "<skill_dir>/scripts/git_state.py" review [<n>]` lists the unresolved threads
  (path:line, who, what) and the latest comments. Fix them in code, push (/pr rules), then report which threads
  you addressed. Reply or resolve threads on GitHub only when the user asks:
  `gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`.
- Releases: `gh release list -L 5`; `gh release view <tag>`. Create one only on request:
  `gh release create v1.2.0 --title "v1.2.0" --notes-file <file> --target <branch>`.
- Anything else: `gh api repos/{owner}/{repo}/<endpoint> --jq '<expr>'`, `gh search code '<text> repo:<owner>/<repo>'`.
- A fork: `gh repo fork --clone=false`, push the branch to it, `gh pr create --repo <upstream> --head <you>:<branch>`.

## Report

Say what you did in git terms the user can check: the branch, commits made or rebased, what was pushed where, the
PR link and its CI (/ci), conflicts and how each was resolved, anything left in a stash. If the remote or GitHub
wasn't reachable, say so instead of guessing its state.
