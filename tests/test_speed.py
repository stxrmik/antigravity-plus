"""Speed and context: the sandbox memory, the context budget, very large reads and loops of failing commands."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
STORE = os.path.expanduser("~/.gemini/antigravity-plus/sandbox.json")
AG_CONFIG = os.path.expanduser("~/.gemini/config/config.json")
art = os.path.join(SP, "speedtest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
WS = os.path.realpath(os.path.join(SP, "proj")); os.makedirs(os.path.join(WS, "deploy"), exist_ok=True)
tp = os.path.join(art, "transcript_full.jsonl")
NOTE = ("There were sandbox errors that may or may not be related to the failure. If you think the failure is because "
        "of running in the sandbox, you can run the command again with `BypassSandbox` set to true to request explicit "
        "user permission.")
ok = True
cid = ""


def iso(ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def start(name, main=True):
    global cid, rows
    cid = f"agp-speed-{name}"
    rows = []
    for f in (tp, os.path.join(conv.STATE_DIR, cid + ".json")):
        if os.path.exists(f):
            os.remove(f)
    if main:
        add("USER_INPUT", content="<USER_REQUEST>Обнови панель на сервере</USER_REQUEST>")
    else:
        add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>injected by a SessionStart hook</SYSTEM_MESSAGE>")


def add(kind, content="", calls=None, **extra):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso(), **extra}
    if content:
        row["content"] = content
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def cmd(line, code=0, output="ok", bypass=False, note=False):
    args = {"CommandLine": line, "WaitMsBeforeAsync": 5000}
    if bypass:
        args["BypassSandbox"] = True
    add("PLANNER_RESPONSE", calls=[("run_command", args)])
    add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\nCommand exited with code {code}.\n\n"
                           + (NOTE + "\n\n" if note else "") + f"Command output:\n{output}\n")


def read(path, lines, rng=False):
    args = {"Path": path}
    if rng:
        args.update(StartLine=1, EndLine=lines)
    add("PLANNER_RESPONSE", calls=[("read_file", args)])
    body = "".join(f"{i}: line {i} of the file with some code in it\n" for i in range(1, lines + 1))
    add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\nShowing lines 1-{lines} of {lines}\n{body}")


def think(tokens, cached):
    add("PLANNER_RESPONSE", content="…", input_tokens=tokens, cache_read_tokens=cached, output_tokens=200)


def pre():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [s["ephemeralMessage"] for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if any(t in m for t in ("<sandbox", "<error_loop", "<context_budget"))), out.stderr.strip()


def session():
    p = {"conversationId": "agp-speed-new", "workspacePaths": [WS], "lastUserInput": "привет"}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "session_start.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    return d["injectSteps"][0]["systemMessage"]["systemMessage"] if d.get("injectSteps") else "", out.stderr.strip()


def stored():
    try:
        return json.load(open(STORE, encoding="utf-8"))["projects"].get(WS, {})
    except (OSError, ValueError, KeyError):
        return {}


def check(name, got, want_in=(), want_out=(), empty=False):
    global ok
    text, err = got if isinstance(got, tuple) else (got, "")
    good = (not text) if empty else bool(text) and all(w in text for w in want_in) and not any(w in text for w in want_out)
    ok &= good and not err
    line = next((l for l in text.splitlines() if l and not l.startswith("<")), text)[:96] if text else "—"
    print(f"{'✅' if good and not err else '❌'} {name:<60} → {line}" + (f"   stderr: {err}" if err else ""))


def same(name, got, want):
    global ok
    ok &= got == want
    print(f"{'✅' if got == want else '❌'} {name:<60} → {got!r}")


# 0. what a command runs, stable across retries
for line, want in [("./deploy/ssh.sh 'docker exec web python3 -'", "./deploy/ssh.sh"),
                   (f"{WS}/deploy/ssh.sh \"cat /etc/hosts\"", "./deploy/ssh.sh"),
                   ("cd /tmp && docker compose up -d", "docker"),
                   ("FOO=1 BAR=2 ./deploy/update.sh", "./deploy/update.sh"),
                   ("python3 -m pytest -q tests", "python3 -m pytest"),
                   ("/opt/homebrew/bin/python3 tools/x.py --fast", "python3 tools/x.py"),
                   ("npm run build -- --prod", "npm run build"),
                   ("git push origin main", "git push"),
                   ('"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless', "Google Chrome")]:
    same(f"key of `{line[:40]}`", conv.command_key(line, WS), want)

# 1. a command fails inside the sandbox, works with BypassSandbox → remembered for the project
start("learn")
cmd("./deploy/ssh.sh 'docker ps'", 1, "WARNING: open /Users/x/.docker/config.json: operation not permitted", note=True)
check("sandbox failure alone → no hint yet", pre(), empty=True)
cmd("./deploy/ssh.sh 'docker ps'", 0, "CONTAINER ID", bypass=True)
check("worked with BypassSandbox → remembered", pre(), ["<sandbox", "`./deploy/ssh.sh`", "remembered", "BypassSandbox: true"])
same("sandbox.json has it for this project", sorted(stored()), ["./deploy/ssh.sh"])
check("…said once", pre(), empty=True)
check("new conversation: the session context lists it", session(), ["Terminal sandbox", "`./deploy/ssh.sh`"])
os.makedirs(os.path.dirname(AG_CONFIG), exist_ok=True)
json.dump({"userSettings": {"enableTerminalSandbox": False}}, open(AG_CONFIG, "w"))
check("…but not while the sandbox is off", session(), [], ["Terminal sandbox"])
os.remove(AG_CONFIG)

# 2. a remembered command fails inside again (absolute path this time) → a reminder, once
start("known")
cmd(f"{WS}/deploy/ssh.sh 'uptime'", 1, "ssh: operation not permitted", note=True)
check("known command failed inside → reminder", pre(), ["is known to fail inside", "`./deploy/ssh.sh`"])
cmd(f"{WS}/deploy/ssh.sh 'uptime'", 1, "ssh: operation not permitted", note=True)
check("…once per conversation", pre(), empty=True)

# 3. not learned: no denial in the output, or the failure already had BypassSandbox
start("nolearn")
cmd("python3 app.py", 1, "Traceback: ValueError: bad input", note=True)
cmd("python3 app.py", 0, "done", bypass=True)
check("sandbox note but no denial → nothing learned", pre(), empty=True)
same("…store unchanged", sorted(stored()), ["./deploy/ssh.sh"])

# 4. a remembered command that works inside the sandbox twice is forgotten
start("forget")
cmd("./deploy/ssh.sh 'uptime'", 0, "up 3 days")
pre()
same("worked inside once → still remembered", sorted(stored()), ["./deploy/ssh.sh"])
cmd("./deploy/ssh.sh 'uptime'", 0, "up 3 days")
pre()
same("worked inside twice → forgotten", sorted(stored()), [])

# 5. loops of failing commands
start("loop")
cmd("./deploy/ssh.sh 'docker exec web python3 -c \"import x\"'", 2, "bash: -c: unexpected EOF while looking for matching quote")
cmd("./deploy/ssh.sh 'docker exec web python3 -c \"import x\\\"'", 2, "SyntaxError: unterminated string literal")
check("two failures → nothing yet", pre(), empty=True)
cmd("grep -rn missing_name src", 1, "")
check("grep without matches isn't a failure", pre(), empty=True)
cmd("./deploy/ssh.sh 'docker exec web python3 -c x'", 1, "NameError: name 'x' is not defined")
check("third failure in a row → stop and change the approach", pre(), ["<error_loop", "3 commands", "piped to stdin", "debugger"])
cmd("python3 -c 'print(1/0)'", 1, "ZeroDivisionError")
check("fourth → no repeat", pre(), empty=True)
cmd("ls missing", 1, "No such file or directory")
cmd("ls missing2", 1, "No such file or directory")
check("sixth → once more", pre(), ["6 commands"])
cmd("python3 -c 'print(1)'", 0, "1")
for i in range(3):
    cmd(f"false {i}", 1, "error")
check("a success resets it; three new failures → again", pre(), ["3 commands"])
start("loop2")
cmd("pytest -q", 1, "1 failed")
add("PLANNER_RESPONSE", calls=[("edit_file", {"Path": "a.py", "Target": "x", "Replacement": "y"})])
add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\nMade 1 replacement in a.py")
cmd("pytest -q", 1, "1 failed")
cmd("pytest -q", 1, "1 failed")
check("a fix in between is progress → no loop yet", pre(), empty=True)
start("loop3")
for i in range(3):
    cmd("./deploy/ssh.sh 'docker ps'", 1, "operation not permitted", note=True)
check("sandbox failures aren't a loop", pre(), empty=True)

# 6. the context budget
start("ctx")
think(20_000, 100_000)
check("120k context → nothing", pre(), empty=True)
think(10_000, 150_000)
check("160k → keep it lean, /handoff later", pre(), ["<context_budget", "~160k tokens", "rg -n", "/handoff"])
think(10_000, 160_000)
check("…once", pre(), empty=True)
think(10_000, 220_000)
check("230k → close to compaction", pre(), ["~230k", "compacts"])
add("CHECKPOINT", content="# Resuming from a compaction")
think(5_000, 40_000)
check("after compaction → quiet", pre(), empty=True)
think(10_000, 150_000)
check("climbing again → warns again", pre(), ["~160k"])
start("ctxsub", main=False)
think(10_000, 160_000)
check("subagent: no /handoff", pre(), ["~170k"], ["/handoff"])

# 7. very large reads and outputs: once per user message, main agent only
start("big")
read(os.path.join(WS, "app.py"), 900)
check("whole 900-line file → read ranges next time", pre(), ["<context_budget", "the whole app.py (900 lines)", "StartLine"])
read(os.path.join(WS, "db.py"), 800)
check("…once per user message", pre(), empty=True)
add("USER_INPUT", content="<USER_REQUEST>Теперь логи</USER_REQUEST>")
cmd("docker logs web", 0, "log line\n" * 5000)
check("new message, huge command output → once more", pre(), ["the output of `docker logs web`"])
start("small")
read(os.path.join(WS, "app.py"), 120)
read(os.path.join(WS, "app.py"), 700, rng=True)
check("small file, or a range → nothing", pre(), empty=True)
start("bigsub", main=False)
read(os.path.join(WS, "app.py"), 900)
check("subagent reading a big file → nothing", pre(), empty=True)

# 8. speed-hints off → nothing, and the session context doesn't list commands
json.dump({"projects": {WS: {"docker": {"n": 1, "ok": 0}}}}, open(STORE, "w"))
subprocess.run([sys.executable, MEMORY, "config", "speed-hints", "off"], capture_output=True)
start("off")
for i in range(3):
    cmd(f"false {i}", 1, "error")
think(10_000, 160_000)
check("speed-hints off → no hints", pre(), empty=True)
check("speed-hints off → no sandbox list at the start", session(), [], ["Terminal sandbox"])
subprocess.run([sys.executable, MEMORY, "config", "speed-hints", "on"], capture_output=True)

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
