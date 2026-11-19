"""Git and GitHub module: git_state.py on real repositories (ahead/behind a bare remote, a rebase stopped by a
conflict, detached HEAD, credentials kept out of remote URLs, not a repository), the git/gh commands that would hang
the agent in Antigravity's terminal (hooks/gitcheck.py) and the guard refusing them, conflict markers caught as an
edit error, and a rebase or merge left half done — sent back by the Stop hook, flagged at session start."""
import json, os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
STATE = os.path.join(PLUGIN, "skills", "git", "scripts", "git_state.py")
sys.path.insert(0, HOOKS)
import gitcheck, checks, compat, conversation as conv
import guard as guard_mod
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
# continued lines are one command: sh joins them at a backslash before the newline, PowerShell at a backtick
C = "`" if compat.WINDOWS else "\\"
LONG_PR = f'gh pr create {C}\n  --base main {C}\n  --head feature-x {C}\n  --title "Release 1.2" {C}\n  --body-file "/tmp/pr.md"'
cases.update({LONG_PR: False, f"gh pr create {C}\n  --base main {C}\n  --head feature-x": True,
              f"gh pr create {C}\r\n  --fill": False, f"gh pr create --title 'a {C}\nb'": True,  # in '…' it joins nothing
              f"git status # see {C}\ngit add -p": True,  # a comment ends at the newline all the same
              f"git rebase {C}\n  --continue": True, f"git add {C}\n  -p": True,
              # a separator inside quotes splits nothing; a quote in a comment opens nothing
              'gh pr create --title "Fix A; refactor B" --body-file pr.md': False,
              'gh pr create --title "a && b | c" --body x': False, 'git commit -m "wip; git add -p later"': False,
              "git status # don't\ngit add -p # that's it": True, 'echo "open; git add -p': True})
wrong = [(c, w) for c, w in cases.items() if bool(gitcheck.interactive(c)) != w]
check(f"{len(cases)} git/gh commands: the hanging ones caught, the rest left alone", not wrong, wrong)
def guard(cmd, extra=None):
    p = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}, "workspacePaths": [repo], **(extra or {})}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=PLUGIN)
    return json.loads(r.stdout or "{}")
ed_pfx = "$env:GIT_EDITOR='true'; " if compat.WINDOWS else "GIT_EDITOR=true "
pr_pfx = "$env:GIT_TERMINAL_PROMPT='0'; " if compat.WINDOWS else "GIT_TERMINAL_PROMPT=0 "
d = guard("git rebase --continue")
check("guard: `git rebase --continue` auto-fixed via overwrite", d.get("decision") == "allow"
      and d.get("overwrite", {}).get("CommandLine") == ed_pfx + "git rebase --continue", d)
dm = guard("git merge feat")
check("guard: `git merge feat` auto-fixed with --no-edit", dm.get("decision") == "allow"
      and dm.get("overwrite", {}).get("CommandLine") == "git merge --no-edit feat", dm)
df = guard("git fetch origin")
check("guard: `git fetch origin` auto-fixed with GIT_TERMINAL_PROMPT=0", df.get("decision") == "allow"
      and df.get("overwrite", {}).get("CommandLine") == pr_pfx + "git fetch origin", df)
dchain = guard("cd x && git fetch origin && git merge foo")
want_chain = (  # PowerShell: `$env:X=…;` can't sit between && links without changing them — only --no-edit goes in
    "cd x && git fetch origin && git merge --no-edit foo"
    if compat.WINDOWS else "cd x && GIT_TERMINAL_PROMPT=0 git fetch origin && git merge --no-edit foo"
)
check("guard: multi-segment chain rewrites both fetch and merge", dchain.get("decision") == "allow"
      and dchain.get("overwrite", {}).get("CommandLine") == want_chain, dchain)
dlong = guard(LONG_PR)
check("guard: `gh pr create` with its title and body on continued lines passes", dlong == {"decision": "allow"}, dlong)
dcont = guard(f"git fetch {C}\n  origin")
check("guard: a continued `git fetch` is auto-fixed as the one line it is", dcont.get("decision") == "allow"
      and dcont.get("overwrite", {}).get("CommandLine") == pr_pfx + "git fetch   origin", dcont)
check("rewrite: a continuation inside '…' is text, not a line to join (left alone)",
      gitcheck.rewrite(f"git commit -m 'a {C}\nb' && git push") is None and gitcheck.rewrite(f"git push # x {C}\n origin") is None)
# PowerShell (Windows): a single & is the call operator, a backtick escapes quotes, and an env assignment is a statement
# of its own — so rewrite only where `;` (or nothing) surrounds git, never wrap it in `& { … }`, else leave it alone
was_windows = compat.WINDOWS
compat.WINDOWS = True
try:
    ps_cases = {
        "& git push": None,
        '& "C:\\Program Files\\Git\\cmd\\git.exe" push origin main': None,
        '"C:\\Git\\cmd\\git.exe" push': None,
        "& git merge feature": None,
        "git push &": None,
        "git pull && npm test": None,
        "git pull || git rebase --abort": None,
        "git push 2>&1 | Select-String error": None,
        "cd C:\\repo && git rebase --continue": None,
        'git merge feature -m "a`"b"': None,
        'git commit -m "x`"; git merge y"': None,
        "git push": "$env:GIT_TERMINAL_PROMPT='0'; git push",
        "cd C:\\repo; git pull": "cd C:\\repo; $env:GIT_TERMINAL_PROMPT='0'; git pull",
        "git fetch --all; git status": "$env:GIT_TERMINAL_PROMPT='0'; git fetch --all; git status",
        "git merge feature; git push": "git merge --no-edit feature; $env:GIT_TERMINAL_PROMPT='0'; git push",
        "git rebase --continue": "$env:GIT_EDITOR='true'; git rebase --continue",
        "git status && git merge feat": "git status && git merge --no-edit feat",
        "git fetch `\n  origin": "$env:GIT_TERMINAL_PROMPT='0'; git fetch   origin",  # a backtick continues the line
        'git commit -m "a`\nb"; git push': None,
    }
    ps_got = {c: (gitcheck.rewrite(c) or (None, None))[1] for c in ps_cases}
    ps_stall = {"gh pr create `\n  --title T `\n  --body-file b.md": None,
                "gh pr create \\\n  --title T \\\n  --body-file b.md": "gh-prompt"}  # a backslash continues nothing there
    ps_stall_got = {c: (gitcheck.interactive(c) or (None,))[0] for c in ps_stall}
finally:
    compat.WINDOWS = was_windows
ps_wrong = {c: g for c, g in ps_got.items() if g != ps_cases[c]}
check(f"rewrite (PowerShell): {len(ps_cases)} command lines — no `& {{ … }}` wrap, call operator and backticks left alone",
      not ps_wrong, ps_wrong)
check("interactive (PowerShell): lines continued with a backtick are one command", ps_stall_got == ps_stall, ps_stall_got)
drev_m = guard("git revert -m 1 abc123")
check("guard: `git revert -m 1` still auto-fixes with --no-edit", drev_m.get("decision") == "allow"
      and drev_m.get("overwrite", {}).get("CommandLine") == "git revert --no-edit -m 1 abc123", drev_m)
check("guard: `git merge --no-edit feat` needs no overwrite", "overwrite" not in guard("git merge --no-edit feat"))
check("guard: `git merge -m msg feat` needs no overwrite", "overwrite" not in guard("git merge -m msg feat"))
check("guard: `git merge --abort` needs no overwrite", "overwrite" not in guard("git merge --abort"))
check("guard: `git merge --squash feat` needs no overwrite", "overwrite" not in guard("git merge --squash feat"))
check("guard: `git commit -F - <<'EOF'...` not rewritten", gitcheck.rewrite("git commit -F - <<'EOF'\nmerge foo\nEOF") is None)
dforce = guard("git push --force origin main")
check("guard: `git push --force` asks without overwrite", dforce.get("decision") == "force_ask" and "overwrite" not in dforce, dforce)
turn(["git status"], "Ок.")
dban_push = guard("git push origin main", {"conversationId": cid, "transcriptPath": tp, "lastUserInput": "ничего не пушь"})
check("guard: banned `git push` asks without overwrite", dban_push.get("decision") == "force_ask" and "overwrite" not in dban_push, dban_push)
check("guard: `git add -p` still denied", guard("git add -p").get("decision") == "deny")
check("guard: `gh pr merge 7` gets the missing flag explained before anyone is asked", guard("gh pr merge 7").get("decision") == "deny")
check("guard: `git status` passes without overwrite", guard("git status") == {"decision": "allow"}, guard("git status"))
settings = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus", "settings.json")
with open(settings, "w", encoding="utf-8") as f:
    json.dump({"gitChecks": False}, f)
check("guard: git-checks off → not refused and no overwrite",
      guard("git add -p").get("decision") != "deny" and "overwrite" not in guard("git rebase --continue"),
      guard("git rebase --continue"))
os.remove(settings)

# sidecar control-plane & scheduled-sidecar guards
check("guard: curl to sidecar /api/toggle asks (agent-settings)",
      guard('curl -s -X POST "http://localhost:8123/api/toggle"').get("decision") == "force_ask")
check("guard: curl to sidecar /api/lsp/stop asks (agent-settings)",
      guard("curl -s -X POST http://127.0.0.1:9000/api/lsp/stop").get("decision") == "force_ask")
check("guard: a project's own localhost /api/settings is not the panel",
      guard("curl -s http://localhost:3000/api/settings").get("decision") == "allow")
check("guard: commit message mentioning sidecar URL is allowed",
      guard("git commit -m 'fix localhost:8080/api/settings'").get("decision") == "allow")
sidecar_p = os.path.expanduser("~/.gemini/jetski/sidecars/job.json")
r_sc = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")],
                      input=json.dumps({"toolCall": {"name": "write_file", "args": {"Path": sidecar_p, "Content": "{}"}},
                                        "workspacePaths": [repo]}),
                      capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PLUGIN)
check("guard: write_file into ~/.gemini/jetski/sidecars asks (scheduled-sidecar)",
      json.loads(r_sc.stdout or "{}").get("decision") == "force_ask", r_sc.stdout)
check("guard: rm into ~/.gemini/jetski/sidecars asks",
      guard("rm -f ~/.gemini/jetski/sidecars/job.json").get("decision") == "force_ask")
check("guard: scheduled-sidecar is in SYSTEM_RULES", "scheduled-sidecar" in guard_mod.SYSTEM_RULES)
check("guard: write into ~/.gemini/config/sidecars asks (Antigravity loads sidecars from there)",
      guard_mod.analyze_path(os.path.expanduser("~/.gemini/config/sidecars/agp-nightly/sidecar.json"))[0] == "scheduled-sidecar")
check("guard: tee into ~/.gemini/config/sidecars asks",
      guard("tee ~/.gemini/config/sidecars/job/sidecar.json < x.json").get("decision") == "force_ask")
check("guard: schedule_task.py create asks, quoted path too, even in Bypass Mode",
      guard('python3 "/opt/agp x/skills/schedule-task/scripts/schedule_task.py" create --slug n --schedule "0 3 * * *"').get("decision") == "force_ask"
      and guard_mod.analyze_command("python3 schedule_task.py remove agp-n", system_only=True)[0] == "scheduled-sidecar")
check("guard: schedule_task.py list / gate pass",
      guard("python3 skills/schedule-task/scripts/schedule_task.py list").get("decision") == "allow"
      and guard_mod.analyze_command("python3 schedule_task.py gate --min 20") is None)
check("guard: a commit message naming schedule_task.py create passes",
      guard_mod.analyze_command('git commit -m "schedule_task.py create: fix args"') is None)
check("guard: heredoc header trailing sudo rm caught",
      guard("cat <<EOF && sudo rm -rf /\nok\nEOF").get("decision") == "force_ask")
check("guard: heredoc header trailing git add -p denied",
      guard("cat <<EOF && git add -p\nok\nEOF").get("decision") == "deny")
check("guard: env -i rm -rf / caught", guard("env -i rm -rf /").get("decision") == "force_ask")
check("guard: write_file .. traversal into agent-settings asks",
      json.loads(subprocess.run(
          [sys.executable, os.path.join(HOOKS, "guard.py")],
          input=json.dumps({"toolCall": {"name": "write_file", "args": {"Path": os.path.expanduser("~/.gemini/x/../antigravity-plus/settings.json"), "Content": "{}"}}, "workspacePaths": [repo]}),
          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PLUGIN,
      ).stdout or "{}").get("decision") == "force_ask")
check("guard: command substitution inside echo caught", guard("echo $(sudo rm -rf /)").get("decision") == "force_ask")
check("guard: alternate loopback sidecar API caught", guard("curl -s http://0.0.0.0:8123/api//toggle").get("decision") == "force_ask")

# AST-based read-only `ssh ... python3` inspection under push ban
py_safe = [
    "import json, os\nprint(json.dumps(os.listdir('/etc')))",
    "import sqlite3\nc = sqlite3.connect('app.db')\nprint(c.execute('SELECT count(*) FROM users').fetchall())",
    "from pathlib import Path\np = Path('/etc/hosts')\nwith p.open('r') as f:\n    print(len(f.read()))",
    "with open('/etc/hostname', 'rb') as f:\n    print(f.read(32))",
    "from urllib.request import Request, urlopen\nreq = Request('http://127.0.0.1:8080/health', method='GET')\nprint(len(set([1, 2])))",
    "class Row:\n    name = 'ok'\nprint(getattr(Row(), 'name', None))",
]
py_unsafe = [
    "",
    "x = '" + ("a" * 20001) + "'",
    "def broken(:",
    "exec('print(1)')",
    "eval('1+1')",
    "import subprocess\nsubprocess.run(['ls'])",
    "import shutil\nshutil.rmtree('/tmp/x')",
    "import os\nos.remove('/tmp/x')",
    "from os import unlink\nunlink('/tmp/x')",
    "from pathlib import Path\nPath('/tmp/x').write_text('hi')",
    "open('/tmp/x', 'w').write('hi')",
    "m = 'r'\nopen('/tmp/x', m)",
    "from pathlib import Path\nPath('/tmp/x').open('a')",
    "import sqlite3\nc = sqlite3.connect('a.db')\nc.execute('DELETE FROM users')\nc.commit()",
    "tbl = 'users'\nsql = f'UPDATE {tbl} SET role = 1'",
    "import redis\nr = redis.Redis()\nr.set('k', 'v')",
    "from urllib.request import Request\nRequest('http://x', data=b'1')",
    "from urllib.request import Request\nRequest('http://x', method='POST')",
    "from urllib.request import urlopen\nurlopen('http://x', b'1')",
    "import os\ngetattr(os, 'system')('id')",
    "fn = open\nfn('/tmp/x', 'w')",
    "setattr(object, 'x', 1)",
    "import os\nvars(os)['system']('id')",
    "from io import open as o\no('/tmp/x', 'w')",
    "import sqlite3\nc = sqlite3.connect('a.db')\nc.execute(f\"{'DROP'} TABLE users\")",
    "import os\nos.open('/tmp/x', os.O_WRONLY)",
    "import socket\nf = socket.socket().makefile('w')\nprint('hi', file=f)",
]
bad_safe = [s[:50] for s in py_safe if not guard_mod.readonly_python_code(s)]
bad_unsafe = [s[:50] for s in py_unsafe if guard_mod.readonly_python_code(s)]
check("readonly_python_code accepts safe inspection scripts", not bad_safe, str(bad_safe))
check("readonly_python_code rejects mutations, dangerous imports, writes, SQL/HTTP mutations", not bad_unsafe, str(bad_unsafe))

turn(["git status"], "Ок.")
ssh_ro_heredoc = "ssh web1 'python3 -' <<'EOF'\nimport json, os\nprint(json.dumps(os.listdir('/app')))\nEOF"
ssh_ro_cd = "ssh web1 'cd /app && python3.12 -' <<'EOF'\nimport sqlite3\nc=sqlite3.connect('db.sqlite')\nprint(c.execute('SELECT 1').fetchone())\nEOF"
ssh_ro_inline = r'''ssh web1 "python3 -c 'import os; print(len(os.listdir(\"/app\")))'"'''
ssh_ro_inline2 = "ssh web1 'python3 -c \"import json, os; print(json.dumps(os.listdir()))\"'"
ssh_rw_heredoc = "ssh web1 'python3 -' <<'EOF'\nimport os\nos.remove('/tmp/x')\nEOF"
ssh_rw_shell = "ssh web1 'rm -rf /tmp/x && python3 -' <<'EOF'\nprint(1)\nEOF"
ssh_rw_inline = r'''ssh web1 "python3 -c 'import subprocess; subprocess.run([\"ls\"])'"'''
ssh_rw_sql = "ssh web1 'python3 -' <<'EOF'\nimport sqlite3\nc=sqlite3.connect('db.sqlite')\nc.execute('DROP TABLE users')\nEOF"
ban_extra = {"conversationId": cid, "transcriptPath": tp, "lastUserInput": "ничего не пушь"}
check("push ban: read-only ssh python3 heredoc allowed", guard(ssh_ro_heredoc, ban_extra).get("decision") == "allow", str(guard(ssh_ro_heredoc, ban_extra)))
check("push ban: cd + read-only ssh python3.12 heredoc allowed", guard(ssh_ro_cd, ban_extra).get("decision") == "allow", str(guard(ssh_ro_cd, ban_extra)))
# `\"` inside "…" is a POSIX shell escape; PowerShell has none (guard reads a backslash there as a path separator)
if os.name != "nt":
    check("push ban: read-only ssh python3 -c (double-quoted) allowed", guard(ssh_ro_inline, ban_extra).get("decision") == "allow", str(guard(ssh_ro_inline, ban_extra)))
check("push ban: read-only ssh python3 -c (single-quoted) allowed", guard(ssh_ro_inline2, ban_extra).get("decision") == "allow", str(guard(ssh_ro_inline2, ban_extra)))
check("push ban: ssh python3 writing file triggers user-said-no-push", guard(ssh_rw_heredoc, ban_extra).get("decision") == "force_ask")
check("push ban: ssh rm + python3 triggers user-said-no-push", guard(ssh_rw_shell, ban_extra).get("decision") == "force_ask")
check("push ban: ssh python3 -c subprocess triggers user-said-no-push", guard(ssh_rw_inline, ban_extra).get("decision") == "force_ask")
check("push ban: ssh python3 DROP TABLE triggers user-said-no-push", guard(ssh_rw_sql, ban_extra).get("decision") == "force_ask")
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
