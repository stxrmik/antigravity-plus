r"""/claude (skills/claude/scripts/claude_task.py) against a fake `claude` executable on PATH:
budget, run (loggedIn false vs ok, git dirty vs clean, non-git), wait (0 / 3 / 1, auth failure, crash),
result, diff (--stat, path filter), send (--resume), cancel, and list."""
import json
import os
import subprocess
import sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
SCRIPT = os.path.join(PLUGIN, "skills", "claude", "scripts", "claude_task.py")
WIN = os.name == "nt"

ok = True


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(detail)[-900]}"))


# 1. Build a cross-platform fake `claude` CLI on PATH
FAKE_BIN = os.path.join(SP, "fake_bin")
FAKE_CLI = os.path.join(SP, "fake_claude.py")
CALL_LOG = os.path.join(SP, "claude_calls.jsonl")
os.makedirs(FAKE_BIN, exist_ok=True)

FAKE_SRC = r'''import json, os, sys, time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
logged_in = os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1"
args = sys.argv[1:]

if "--help" in args:
    print("  -p, --print  --output-format <fmt>  --verbose  --permission-prompts <t>  --allowedTools  --disallowedTools  --session-id  --resume")
    sys.exit(0)

if args[:3] == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": logged_in, "email": "private-user@example.invalid"}))
    sys.exit(0 if logged_in else 1)

prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "prompt": prompt,
                        "has_csrf": "ANTIGRAVITY_CSRF_TOKEN" in os.environ,
                        "has_ls": "ANTIGRAVITY_LS_ADDRESS" in os.environ}) + "\n")

sid = "00000000-0000-4000-8000-000000000000"
for flag in ("--session-id", "--resume"):
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            sid = args[idx + 1]

if mode == "slow":
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "15")))
    sys.exit(0)

if mode == "crash":
    sys.stderr.write("fatal: synthetic crash\n")
    sys.exit(1)

if mode == "auth_fail":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-sonnet-4-5"}), flush=True)
    print(json.dumps({"type": "assistant", "error": "authentication_failed", "message": {"content": []}}), flush=True)
    print(json.dumps({"type": "result", "is_error": True, "result": "Failed to authenticate. Please /login."}), flush=True)
    sys.exit(1)

print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-sonnet-4-5"}), flush=True)

if "--resume" in args:
    target = os.path.join(os.getcwd(), "app.py")
    with open(target, "a", encoding="utf-8") as f:
        f.write("def v2():\n    return 2\n")
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu2", "name": "Edit", "input": {"file_path": target}},
        {"type": "tool_use", "id": "tu3", "name": "Bash", "input": {"command": "python3 -m unittest -v"}},
    ]}}), flush=True)
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu2", "is_error": False},
        {"type": "tool_result", "tool_use_id": "tu3", "is_error": False},
    ]}}), flush=True)
    print(json.dumps({
        "type": "result", "is_error": False, "num_turns": 1, "duration_ms": 900, "total_cost_usd": 0.008,
        "usage": {"input_tokens": 50, "cache_creation_input_tokens": 0, "output_tokens": 20, "cache_read_input_tokens": 100},
        "permission_denials": [],
        "result": "STATUS: done\nCHANGED: app.py — added v2\nCHECKS: python3 -m unittest -v -> 2 passed\nNOTES: none",
    }), flush=True)
    sys.exit(0)

# Initial run: edit app.py and create note.txt
app = os.path.join(os.getcwd(), "app.py")
note = os.path.join(os.getcwd(), "note.txt")
with open(app, "w", encoding="utf-8") as f:
    f.write("def v1():\n    return 42\n")
with open(note, "w", encoding="utf-8") as f:
    f.write("created by fake claude\n")

print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "tu1", "name": "Write", "input": {"file_path": app}},
    {"type": "tool_use", "id": "tu1b", "name": "Bash", "input": {"command": "pytest -q"}},
]}}), flush=True)
print(json.dumps({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tu1", "is_error": False},
    {"type": "tool_result", "tool_use_id": "tu1b", "is_error": False},
]}}), flush=True)
print(json.dumps({
    "type": "result", "is_error": False, "num_turns": 2, "duration_ms": 1800, "total_cost_usd": 0.0125,
    "usage": {"input_tokens": 1700, "cache_creation_input_tokens": 300, "output_tokens": 150, "cache_read_input_tokens": 4000},
    "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}}],
    "result": "STATUS: done\nCHANGED: app.py — returns 42\nCHECKS: pytest -q -> 1 passed\nNOTES: none\n" + ("detail " * 700),
}), flush=True)
'''

with open(FAKE_CLI, "w", encoding="utf-8") as f:
    f.write(FAKE_SRC)

if WIN:
    shim = os.path.join(FAKE_BIN, "claude.cmd")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\n"{sys.executable}" "{FAKE_CLI}" %*\r\n')
else:
    shim = os.path.join(FAKE_BIN, "claude")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({FAKE_CLI!r}, run_name='__main__')\n")
    os.chmod(shim, 0o755)

ws = os.path.realpath(os.path.join(SP, "repo"))
os.makedirs(ws)
with open(os.path.join(ws, "app.py"), "w", encoding="utf-8") as f:
    f.write("def v1():\n    return 1\n")
with open(os.path.join(ws, "dirty.txt"), "w", encoding="utf-8") as f:
    f.write("initial\n")
subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True)
# Dirty file BEFORE task starts — must NOT show up in task's diff
with open(os.path.join(ws, "dirty.txt"), "w", encoding="utf-8") as f:
    f.write("already dirty before task\n")

ENV = dict(os.environ, PATH=FAKE_BIN + os.pathsep + os.environ.get("PATH", ""),
           AGP_CLAUDE_POLL="0.1", FAKE_CLAUDE_LOG=CALL_LOG, FAKE_CLAUDE_MODE="ok", FAKE_CLAUDE_LOGGED_IN="1",
           ANTIGRAVITY_CSRF_TOKEN="synth-csrf-secret", ANTIGRAVITY_LS_ADDRESS="127.0.0.1:42424")


def run_cli(*args, cwd=ws, env=None, stdin=None):
    r = subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd, env=dict(ENV, **(env or {})), input=stdin,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, r.stdout + r.stderr


def calls():
    try:
        with open(CALL_LOG, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except OSError:
        return []


# 2. budget (logged in, tight Claude limit, logged out without leaking email)
code, out = run_cli("budget", "--claude-5h", "80", "--claude-week", "40")
check("budget: reports logged in, tight Claude plan, and advice",
      code == 0 and "Claude Code CLI: logged in" in out and "5 h 80% used" in out
      and "getting tight" in out and "private-user@example.invalid" not in out, out)

code, out = run_cli("budget", env={"FAKE_CLAUDE_LOGGED_IN": "0"})
check("budget: logged out warns and never prints account email",
      code == 0 and "NOT logged in" in out and "private-user@example.invalid" not in out, out)

# 3. run fails immediately when logged out
code, out = run_cli("run", "Do something", env={"FAKE_CLAUDE_LOGGED_IN": "0"})
check("run fails cleanly when Claude isn't logged in", code == 1 and "isn't logged in" in out, out)

# 4. run + wait + result + diff in git repo with pre-existing dirty file
code, out = run_cli("run", "--model", "sonnet", "--effort", "high", "--title", "Update v1",
                    "--allow", "Bash(npm test *)", "--", "-",
                    stdin="Change v1 in app.py to return 42 and add note.txt")
check("run returns immediately with task id and pre-dirty warning",
      code == 0 and "Started Claude Code task " in out and "1 file(s) already had uncommitted changes" in out, out)
tid = out.split("Started Claude Code task ", 1)[1].split(" ", 1)[0].strip()
short = tid[:8]

code, out = run_cli("wait", short)
check("wait exits 0 with STATUS: done, model, turns, cost, tokens, changed files, bash cmds, and permission denials",
      code == 0 and "done (STATUS: done)" in out and "claude-sonnet-4-5" in out and "2 turns" in out
      and "$0.0125" in out and "in 2k · out 150 · cache read 4k" in out
      and "M app.py" in out and "A note.txt" in out and "dirty.txt" not in out
      and "pytest -q" in out and "Refused (nobody could approve) — 1" in out
      and "Bash: curl https://example.com" in out and "--full" in out, out)

clog = calls()
check("claude CLI received --session-id, --permission-prompts none, --allowedTools, --disallowedTools, effort, and brief",
      len(clog) == 1 and "--session-id" in clog[0]["argv"] and tid in clog[0]["argv"]
      and "--permission-prompts" in clog[0]["argv"] and "none" in clog[0]["argv"]
      and "--effort" in clog[0]["argv"] and "high" in clog[0]["argv"]
      and "Bash(npm test *)" in clog[0]["argv"]
      and "Edit" not in clog[0]["argv"] and "Bash(cat *)" not in clog[0]["argv"]
      and not clog[0]["has_csrf"] and not clog[0]["has_ls"]
      and "[Delegated from Antigravity]" in clog[0]["prompt"]
      and "Change v1 in app.py to return 42" in clog[0]["prompt"], clog)

code, out_full = run_cli("result", short, "--full")
check("result --full exits 0 and shows full report text",
      code == 0 and "… (cut;" not in out_full and out_full.count("detail") >= 650, out_full[-300:])

code, out_stat = run_cli("diff", short, "--stat")
check("diff --stat shows only files changed since task started (excludes pre-dirty dirty.txt)",
      code == 0 and "app.py" in out_stat and "note.txt" in out_stat and "dirty.txt" not in out_stat, out_stat)

code, out_diff = run_cli("diff", short, "app.py")
check("diff app.py shows unified diff (+return 42 / -return 1) and excludes note.txt",
      code == 0 and "-    return 1" in out_diff and "+    return 42" in out_diff and "note.txt" not in out_diff, out_diff)

# 5. send (--resume)
code, out = run_cli("send", short, "Also add v2 returning 2")
check("send exits 0 and starts follow-up run 2", code == 0 and "run 2, same session" in out, out)
code, out = run_cli("wait", short)
check("wait after send exits 0 and shows cumulative changes + follow-up report",
      code == 0 and "added v2" in out and "python3 -m unittest -v" in out, out)
clog = calls()
check("send passed --resume with the same session id",
      len(clog) == 2 and "--resume" in clog[1]["argv"] and tid in clog[1]["argv"]
      and "Also add v2 returning 2" in clog[1]["prompt"], clog[1])
code, out_diff2 = run_cli("diff", short, "app.py")
check("diff after send shows cumulative change from initial snapshot",
      code == 0 and "-    return 1" in out_diff2 and "+    return 42" in out_diff2 and "+def v2():" in out_diff2, out_diff2)

# 6. slow task: wait --timeout -> exit 3, then cancel -> wait exits 1 as canceled
code, out = run_cli("run", "Slow task", env={"FAKE_CLAUDE_MODE": "slow", "FAKE_CLAUDE_SLEEP": "30"})
check("slow task started", code == 0, out)
slow_id = out.split("Started Claude Code task ", 1)[1].split(" ", 1)[0].strip()[:8]
code, out = run_cli("wait", slow_id, "--timeout", "0.25", env={"FAKE_CLAUDE_MODE": "slow"})
check("wait --timeout returns exit code 3 while still running",
      code == 3 and "is still running after" in out, (code, out))

code, out = run_cli("cancel", slow_id)
check("cancel exits 0 and stops running task", code == 0 and "Stopped Claude Code task" in out, out)
code, out = run_cli("wait", slow_id)
check("wait after cancel exits 1 and stays Canceled", code == 1 and "— canceled" in out and "Canceled." in out, (code, out))

# 7. stream-json auth failure -> exit 1 with login message
code, out = run_cli("run", "Auth fail task", env={"FAKE_CLAUDE_MODE": "auth_fail"})
check("auth_fail task started", code == 0, out)
auth_id = out.split("Started Claude Code task ", 1)[1].split(" ", 1)[0].strip()[:8]
code, out = run_cli("wait", auth_id)
check("wait on stream-json authentication_failed exits 1 with login guidance",
      code == 1 and "Claude Code isn't logged in" in out, (code, out))

# 8. crash without result -> exit 1 with stderr output
code, out = run_cli("run", "Crash task", env={"FAKE_CLAUDE_MODE": "crash"})
check("crash task started", code == 0, out)
crash_id = out.split("Started Claude Code task ", 1)[1].split(" ", 1)[0].strip()[:8]
code, out = run_cli("wait", crash_id)
check("wait on crash without result exits 1 and shows stderr tail",
      code == 1 and "synthetic crash" in out and "stopped without a result" in out, (code, out))

# 9. list shows recent tasks and their states
code, out = run_cli("list")
check("list shows all recent tasks with done / canceled / failed states",
      code == 0 and short in out and slow_id in out and auth_id in out and crash_id in out
      and "done" in out and "canceled" in out and "failed" in out, out)

# 10. Non-git folder fallback (edited_paths from tool calls)
nongit = os.path.realpath(os.path.join(SP, "plain_dir"))
os.makedirs(nongit)
code, out = run_cli("run", "Edit in plain dir", cwd=nongit)
check("run in non-git directory notes no git repository",
      code == 0 and "Not a git repository" in out, out)
ng_id = out.split("Started Claude Code task ", 1)[1].split(" ", 1)[0].strip()[:8]
code, out = run_cli("wait", ng_id, cwd=nongit)
check("wait in non-git directory lists files from Claude's Write/Edit tool calls",
      code == 0 and "from Claude's own edits" in out and "app.py" in out, out)
code, out = run_cli("diff", ng_id, cwd=nongit)
check("diff in non-git directory explains no git diff and names edited file",
      code == 0 and "Not a git repository" in out and "app.py" in out, out)

# 11. Security guards: kill_tree(0), --dontAsk fallback, guard --dangerously-skip-permissions, py -3 script_of, SECRET_FILE
sys.path.insert(0, os.path.dirname(SCRIPT))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import checks as _checks  # noqa: E402
import claude_task as _ct  # noqa: E402
import guard as _guard  # noqa: E402

_ct.kill_tree(0)
check("kill_tree(0) is a safe no-op", True)
old_shim = os.path.join(SP, "fake_old_claude.py")
with open(old_shim, "w", encoding="utf-8") as f:
    f.write("import sys\nprint('--permission-mode')\n")
old_exe = os.path.join(SP, "old_claude.cmd" if WIN else "old_claude")
with open(old_exe, "w", encoding="utf-8") as f:
    if WIN:
        f.write(f'@echo off\r\n"{sys.executable}" "{old_shim}" %*\r\n')
    else:
        f.write(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({old_shim!r}, run_name='__main__')\n")
if not WIN:
    os.chmod(old_exe, 0o755)
old_flags = _ct.flags_of({"mode": "acceptEdits"}, old_exe)
check("flags_of falls back to dontAsk when --permission-prompts is missing",
      "--permission-mode" in old_flags and "dontAsk" in old_flags and "--permission-prompts" not in old_flags, old_flags)
bypass_hit = _guard.analyze_command('python3 claude_task.py run --dangerously-skip-permissions "x"', system_only=True)
check("guard stops claude_task --dangerously-skip-permissions even in Bypass Mode",
      bypass_hit is not None and bypass_hit[0] == "claude-permissions", bypass_hit)
py_push = _guard.restriction([{"n": 1, "text": "ничего не пушь"}], "py -3 -X utf8 release.py publish --yes")
check("guard catches py -3 -X utf8 release.py under push ban",
      py_push is not None and py_push[0] == "user-said-no-push", py_push)
check("SECRET_FILE blocks lsp daemon token files and quota.json",
      bool(_checks.SECRET_FILE.search("/Users/u/.gemini/antigravity-plus/lsp/abc123.json"))
      and bool(_checks.SECRET_FILE.search("/Users/u/.gemini/antigravity-plus/quota.json")))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
