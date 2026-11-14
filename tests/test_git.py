"""Git and GitHub module: git_state.py on real repositories (ahead/behind a bare remote, a rebase stopped by a
conflict, detached HEAD, credentials kept out of remote URLs, not a repository), the git/gh commands that would hang
the agent in Antigravity's terminal (hooks/gitcheck.py) and the guard refusing them, conflict markers caught as an
edit error, and a rebase or merge left half done — sent back by the Stop hook, flagged at session start."""
import json, os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
STATE = os.path.join(PLUGIN, "skills", "git", "scripts", "git_state.py")
sys.path.insert(0, HOOKS)
import gitcheck, checks, conversation as conv
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-700:]}"))
def sh(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, GIT_EDITOR="true"))
def state(*args, cwd=None):
    r = subprocess.run([sys.executable, STATE, *args], cwd=cwd or SP, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    return r.returncode, r.stdout + r.stderr
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

# a bare «remote», a clone with work on it
remote, repo = os.path.join(SP, "remote.git"), os.path.join(SP, "repo")
sh(["init", "-q", "--bare", "-b", "main", remote], SP)
sh(["clone", "-q", remote, repo], SP)
write(os.path.join(repo, "app.py"), "a = 1\nb = 2\nc = 3\n")
sh(["add", "."], repo); sh(["commit", "-qm", "base"], repo); sh(["push", "-q", "-u", "origin", "main"], repo)
code, out = state("status", "--path", repo)
check("status: clean, in sync with its upstream", code == 0 and "Branch: main" in out and "tracks origin/main: in sync" in out
      and "Changes: none" in out and "Remotes: origin → " in out, out)
write(os.path.join(repo, "app.py"), "a = 1\nb = 2\nc = 30\n")
sh(["commit", "-qam", "local change"], repo)
other = os.path.join(SP, "other")
sh(["clone", "-q", remote, other], SP)
write(os.path.join(other, "app.py"), "a = 10\nb = 2\nc = 3\n")
sh(["commit", "-qam", "their change"], other); sh(["push", "-q"], other)
code, out = state("status", "--path", repo, "--fetch")
check("status --fetch: ahead and behind → how to bring it together", "1 ahead, 1 behind" in out and "Diverged from origin/main" in out
      and "git rebase origin/main" in out, out)
write(os.path.join(repo, "new.py"), "x = 1\n"); write(os.path.join(repo, "app.py"), "a = 1\nb = 2\nc = 31\n")
sh(["stash", "push", "-q", "-m", "wip"], repo)
write(os.path.join(repo, "notes.txt"), "n\n")
code, out = state("status", "--path", repo)
check("status: changes by kind and the stash (a stash leaves untracked files)", "2 untracked" in out and "notes.txt" in out and "Stash: 1 entry" in out
      and "new.py" in out, out)
os.remove(os.path.join(repo, "notes.txt"))
sh(["stash", "drop", "-q"], repo)
for f in ("new.py",):
    if os.path.exists(os.path.join(repo, f)): os.remove(os.path.join(repo, f))
sh(["checkout", "-q", "--", "."], repo)

# a rebase stopped by a conflict
sh(["fetch", "-q"], repo)
write(os.path.join(repo, "app.py"), "a = 2\nb = 2\nc = 31\n")
sh(["commit", "-qam", "a=2 locally"], repo)
r = sh(["rebase", "origin/main"], repo)
code, out = state("status", "--path", repo)
check("status: a rebase stopped by a conflict, with the commands that finish or undo it",
      "In progress: a rebase onto origin/main" in out and "conflicts in 1 file(s)" in out and "conflicted: app.py" in out
      and "GIT_EDITOR=true git rebase --continue" in out and "git rebase --abort" in out
      and "normal while a rebase" in out and "git switch -c" not in out, out)
code, out = state("conflicts", "--path", repo)
check("conflicts: the file, its block and which half is which in a rebase",
      "app.py: both changed it; 1 block(s): lines" in out and "In a rebase, «ours» (HEAD, the upper half) is the branch you rebase onto" in out
      and "git show :1:<file>" in out, out)
left = gitcheck.unfinished(repo)
check("gitcheck.unfinished: the rebase and its conflicted file", left and left["kind"] == "rebase" and left["conflicts"] == ["app.py"]
      and "GIT_EDITOR=true git rebase --continue" in gitcheck.describe(left), left)

# the Stop hook: the agent started that rebase in this turn and wants to stop → sent back; a question to the user → fine
cid, art = "agp-gittest-0001", os.path.join(SP, "art"); os.makedirs(art)
tp = os.path.join(art, "transcript_full.jsonl")
def turn(commands, answer):
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-14T10:00:00Z", "content": "<USER_REQUEST>Подтяни main в мою ветку</USER_REQUEST>"}]
    for i, c in enumerate(commands, 1):
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "run_command", "args": {"CommandLine": c}}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": answer})
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
def hook(name, **extra):
    p = dict({"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [repo],
              "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}, **extra)
    r = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(p), capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout or "{}")
turn(["git fetch -q", "git rebase origin/main"], "Готово, ветка обновлена.")
d = hook("stop.py")
check("Stop: a rebase it left half done → sent back with the exact commands", d.get("decision") == "continue"
      and "rebase is still in progress" in d.get("reason", "") and "GIT_EDITOR=true git rebase --continue" in d.get("reason", "")
      and "git rebase --abort" in d.get("reason", ""), d)
turn(["git fetch -q", "git rebase origin/main"], "В app.py конфликт: оставить a = 2 или a = 10?")
check("Stop: it asks the user how to resolve → that's fine", hook("stop.py") == {}, hook("stop.py"))
turn(["git status"], "Посмотрел статус.")
check("Stop: a rebase it didn't start in this turn → not its business", hook("stop.py") == {}, hook("stop.py"))
msg = json.dumps(hook("session_start.py", lastUserInput="Привет"), ensure_ascii=False)
check("SessionStart: unfinished git work from before is flagged, not to be touched without the user",
      "Unfinished git work from before this conversation" in msg and "ask the user before finishing or undoing it" in msg, msg[-500:])
sh(["rebase", "--abort"], repo)
check("after the abort nothing is left", gitcheck.unfinished(repo) is None, gitcheck.unfinished(repo))

# conflict markers in a changed file: an edit error, but not in docs
write(os.path.join(repo, "conf.toml"), "a = 1\nb = 2\n")
sh(["add", "conf.toml"], repo); sh(["commit", "-qm", "conf"], repo)
write(os.path.join(repo, "conf.toml"), "a = 1\n<<<<<<< HEAD\nb = 2\n=======\nb = 3\n>>>>>>> feature\n")
probs = checks.check_file(os.path.join(repo, "conf.toml"))
check("a file with conflict markers is an edit error (blocks the turn like a syntax error)", probs and probs[0]["kind"] == "syntax"
      and "conflict markers" in probs[0]["msg"] and probs[0]["line"] == 2, probs)
write(os.path.join(repo, "GUIDE.md"), "Example:\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n")
check("…but not in docs that show an example", checks.check_file(os.path.join(repo, "GUIDE.md")) is None)
check("unfinished(): markers left in a changed file", (gitcheck.unfinished(repo) or {}).get("markers") == [("conf.toml", 2)],
      gitcheck.unfinished(repo))
sh(["checkout", "-q", "--", "conf.toml"], repo); os.remove(os.path.join(repo, "GUIDE.md"))

# credentials never printed; not a repository
sh(["remote", "set-url", "origin", "https://someone:s3cret-token@example.com/team/app.git"], repo)
code, out = state("status", "--path", repo)
check("a token in a remote URL is not printed", "s3cret-token" not in out and "origin → https://example.com/team/app.git" in out, out)
plain = os.path.join(SP, "plain"); os.makedirs(plain)
code, out = state("status", "--path", plain)
check("not a git repository → exit 2 and what to do", code == 2 and "Not a git repository" in out and "/git-init" in out, out)

# commands that would hang (or fail on GIT_EDITOR=false) in Antigravity's terminal
cases = {
    "git add -p": True, "git add -A": False, "git -C repo add --patch x": True, "git checkout -p HEAD -- a": True,
    "git checkout -b feat": False, "git stash push -p": True, "git stash pop": False, "git clean -fdi": True,
    "git clean -fd": False, "git add -i": True, "git rebase --continue": True,
    "GIT_EDITOR=true git rebase --continue": False, "git -c core.editor=true rebase --continue": True,
    "$env:GIT_EDITOR='true'; git rebase --continue": False, "git am --continue": False, "git merge --continue": True,
    "git rebase -i HEAD~3": True, "GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash main": False,
    "git rebase -Xignore-all-space main": False, "git rebase main": False, "gh auth login": True, "gh auth status": False,
    "gh pr create": True, "gh pr create --fill": False, "gh pr create --title T --body-file b.md": False,
    "gh pr create -t T": True, "gh pr create --web": True, "gh pr view --web": False,
    "gh issue create --title T --body B": False, "gh issue comment 5": True, "gh issue comment 5 --body hi": False,
    "gh pr review 3 --approve": False, "gh pr review 3 --comment": True, "gh pr review 3 --comment -b ok": False,
    "gh pr merge 3": True, "gh pr merge 3 --squash": False, "gh release create v1": True,
    "gh release create v1 --generate-notes": False, "gh repo create": True, "gh repo create demo --private": False,
    "gh repo fork o/r": True, "gh repo fork o/r --clone=false": False,
    "git commit -F - <<'EOF'\nfix: add -p handling\nEOF": False, "cd x && git status": False,
}
wrong = [(c, w) for c, w in cases.items() if bool(gitcheck.interactive(c)) != w]
check(f"{len(cases)} git/gh commands: the hanging ones caught, the rest left alone", not wrong, wrong)
def guard(cmd):
    p = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}, "workspacePaths": [repo]}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p), capture_output=True, text=True,
                       encoding="utf-8", cwd=PLUGIN)
    return json.loads(r.stdout or "{}")
d = guard("git rebase --continue")
check("guard: `git rebase --continue` refused with the form that works", d.get("decision") == "deny"
      and "GIT_EDITOR=true git rebase --continue" in d.get("reason", ""), d)
check("guard: `gh pr merge 7` gets the missing flag explained before anyone is asked", guard("gh pr merge 7").get("decision") == "deny")
check("guard: `git status` passes", guard("git status").get("decision") == "allow", guard("git status"))
settings = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus", "settings.json")
with open(settings, "w", encoding="utf-8") as f:
    json.dump({"gitChecks": False}, f)
check("guard: git-checks off → not refused", guard("git add -p").get("decision") != "deny", guard("git add -p"))
os.remove(settings)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
