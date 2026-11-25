"""Speed and context: the sandbox memory, the context budget, very large reads and loops of failing commands."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv, speed  # noqa: E402
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


def pre(model=""):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
    if model:
        p["modelName"] = model
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    msgs = [(s.get("systemMessage") or {}).get("systemMessage") or s.get("ephemeralMessage", "")
            for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
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
if os.name == "nt":  # PowerShell / cmd: a backslash separates path parts; drive letter case and slashes don't matter
    platform_cases = [(f"{WS}\\deploy\\ssh.sh -Host web", "./deploy/ssh.sh"),
                      (WS.lower().replace("\\", "/") + "/deploy/ssh.sh", "./deploy/ssh.sh"),
                      (".\\deploy\\ssh.sh 'uptime'", "./deploy/ssh.sh"),
                      ('"C:\\Program Files\\nodejs\\node.exe" tools\\x.js --fast', "node tools/x.js"),
                      ("C:\\Python313\\python.exe -m pytest -q", "python -m pytest"),
                      ("git.exe push origin main", "git push")]
else:  # a POSIX shell: a backslash escapes the next character
    platform_cases = [("/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --headless", "Google Chrome"),
                      (f"{WS}/deploy/../deploy/ssh.sh x", "./deploy/ssh.sh")]
for line, want in platform_cases:
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

# 4b. a piped or chained command is learned by its program, as in 0.24 (only the guard's BypassSandbox overwrite leaves
#     chains alone); an inline `python3 -c` script names no program and is not learned
start("pipe")
cmd("./deploy/logs.sh 'web' 2>&1 | tail -20", 1, "open /Users/x/.ssh/known_hosts: operation not permitted", note=True)
cmd("./deploy/logs.sh 'web' 2>&1 | tail -20", 0, "log lines", bypass=True)
check("piped command that needed BypassSandbox → remembered by its program", pre(),
      ["<sandbox", "`./deploy/logs.sh`", "remembered"])
start("chain")
cmd("cd deploy && docker compose up -d && docker ps", 1, "permission denied while trying to connect to the Docker daemon",
    note=True)
cmd("cd deploy && docker compose up -d && docker ps", 0, "web  Up", bypass=True)
pre()
same("chained docker command → remembered as `docker`", sorted(stored()), ["./deploy/logs.sh", "docker"])
start("inline")
cmd("python3 -c 'import socket; socket.create_connection((\"x\", 1))'", 1, "PermissionError: operation not permitted",
    note=True)
cmd("python3 -c 'import socket; socket.create_connection((\"x\", 1))'", 0, "ok", bypass=True)
pre()
same("…an inline python3 -c script is not remembered", sorted(stored()), ["./deploy/logs.sh", "docker"])
json.dump({"projects": {}}, open(STORE, "w"))

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
start("ctxgemini")
think(15_000, 220_000)
check("Gemini 4 at 235k (512k window since 2026-11-16) → only the lean hint", pre("gemini-4-carbon-high"),
      ["~235k", "rg -n"], ["compacts"])
think(15_000, 440_000)
check("Gemini 4 at 455k → near compaction, of its 512k window", pre("gemini-4-carbon-high"),
      ["~455k", "512k window", "compacts", "/compact"], ["505k", "550k"])
start("ctxgemini3")
think(15_000, 220_000)
check("older Gemini at 235k → near compaction, of its 256k window", pre("gemini-3.1-pro-high"),
      ["~235k", "256k window", "compacts"], ["300k"])
start("ctxorch")
think(15_000, 440_000)
check("Orchestrator (Carbon plans, Hydrogen carries out) → the same Gemini 4 window", pre("Orchestrator (Carbon)"),
      ["~455k", "512k window"])
start("ctxneon")
think(15_000, 440_000)
check("Gemini 4 Neon and its Orchestrator → the same Gemini 4 window", pre("gemini-4-neon-high"),
      ["~455k", "512k window"])
start("ctxexec")
think(15_000, 440_000)
check("Orchestrator's executor (gemini-flash-worker-agy) → the Gemini 4 window", pre("gemini-flash-worker-agy"),
      ["~455k", "512k window"])
start("ctxflashlite", main=False)
think(10_000, 440_000)
check("Flash-Lite subagent (3.5) → warns near its 512k window", pre("gemini-3.5-flash-lite"), ["~450k", "512k window"], ["128k"])
start("ctxflashlite25", main=False)
think(10_000, 105_000)
check("Gemini 2.5 Flash-Lite → warns near its 128k window", pre("gemini-2.5-flash-lite"), ["~115k", "128k window"], ["512k"])
start("ctxgptoss", main=False)
think(10_000, 62_000)
check("GPT-OSS 120B → warns near its 80k window", pre("gpt-oss-120b-medium"), ["~72k", "80k window"])
start("ctxoverride")
think(10_000, 350_000)
old_ov = os.environ.get("AGY_CLI_AUTO_COMPACT_OVERRIDE")
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "400K"
check("AGY_CLI_AUTO_COMPACT_OVERRIDE=400K warns near 400k limit", pre("gemini-4-neon-high"), ["~360k", "400k compaction limit"])
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "500"
ov500 = speed.ctx_limits("gemini-4-neon-high")
same("AGY_CLI_AUTO_COMPACT_OVERRIDE=500 formats '500' (not '0k')",
     ("500" in ov500[2] and "0k" not in ov500[2], ov500[:2]), (True, (300, 440)))
if old_ov is None:
    os.environ.pop("AGY_CLI_AUTO_COMPACT_OVERRIDE", None)
else:
    os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = old_ov
for m25 in ("gemini-2.5-flash", "gemini-2.5-pro"):
    same(f"ctx_limits('{m25}') → 128k window", speed.ctx_limits(m25), (80_000, 110_000, " of the model's 128k window"))
start("ctxclaude55")
think(20_000, 210_000)
check("Claude Opus 5.5 at 230k → lean hint, not near-compaction yet", pre("claude-opus-5-5-high"), ["~230k", "rg -n"], ["compacts"])
think(30_000, 710_000)
check("Claude Opus 5.5 at 740k → near compaction, of its 840k window", pre("claude-opus-5-5-high"), ["~740k", "840k window"])
start("ctxswitch")
think(15_000, 440_000)
check("Gemini 4 at 455k → level-2 compaction warning", pre("gemini-4-carbon-high"), ["~455k", "512k window"])
think(10_000, 450_000)
check("switched to Claude Opus 5.5 at 460k → quiet until Claude's high threshold", pre("claude-opus-5-5-high"), empty=True)
think(30_000, 710_000)
check("later at 740k on Claude Opus 5.5 → warns again near its 840k window", pre("claude-opus-5-5-high"), ["~740k", "840k window"])

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

# 8. guard.py BypassSandbox overwrite for learned commands
def guard_cmd(line, bypass_sbx=None, extra=None):
    args = {"CommandLine": line}
    if bypass_sbx is not None:
        args["BypassSandbox"] = bypass_sbx
    p = {"conversationId": cid, "transcriptPath": tp, "workspacePaths": [WS],
         "toolCall": {"name": "run_command", "args": args}, **(extra or {})}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return json.loads(r.stdout or "{}")

start("ow")
json.dump({"projects": {WS: {"./deploy/ssh.sh": {"n": 1, "ok": 0}, "git fetch": {"n": 1, "ok": 0}}}}, open(STORE, "w"))
d1 = guard_cmd("./deploy/ssh.sh 'uptime'")
same("learned command → guard overwrites BypassSandbox: True",
     (d1.get("decision"), d1.get("overwrite")), ("allow", {"BypassSandbox": True}))
d2 = guard_cmd(f"{WS}/deploy/ssh.sh 'uptime'")
same("absolute path to learned script → also overwrites BypassSandbox",
     (d2.get("decision"), d2.get("overwrite")), ("allow", {"BypassSandbox": True}))
d3 = guard_cmd("./deploy/ssh.sh 'uptime'", bypass_sbx=True)
same("already BypassSandbox: true → no overwrite", ("overwrite" in d3, d3.get("decision")), (False, "allow"))
d4 = guard_cmd("git fetch origin")
want_pfx = "$env:GIT_TERMINAL_PROMPT='0'; " if os.name == "nt" else "GIT_TERMINAL_PROMPT=0 "
same("learned git fetch → combines CommandLine + BypassSandbox overwrite",
     d4.get("overwrite"), {"CommandLine": want_pfx + "git fetch origin", "BypassSandbox": True})
d5 = guard_cmd("./deploy/ssh.sh 'systemctl restart app'", extra={"lastUserInput": "ничего не пушь"})
same("push-banned learned command → force_ask WITHOUT overwrite",
     (d5.get("decision"), "overwrite" in d5), ("force_ask", False))
os.makedirs(os.path.dirname(AG_CONFIG), exist_ok=True)
json.dump({"userSettings": {"enableTerminalSandbox": False}}, open(AG_CONFIG, "w"))
same("sandbox disabled in config → no BypassSandbox overwrite", "overwrite" in guard_cmd("./deploy/ssh.sh 'uptime'"), False)
os.remove(AG_CONFIG)

json.dump({"projects": {WS: {"git fetch": {"n": 1, "ok": 0}, "python3 -c": {"n": 1, "ok": 0}}}}, open(STORE, "w"))
same("compound chained command gets NO BypassSandbox overwrite",
     "BypassSandbox" in (guard_cmd("git fetch origin && rm -rf /tmp/x").get("overwrite") or {}), False)
same("inline python3 -c gets NO BypassSandbox overwrite",
     "BypassSandbox" in (guard_cmd("python3 -c 'print(1)'").get("overwrite") or {}), False)

# 9. speed-hints off → nothing, no overwrite, and the session context doesn't list commands
json.dump({"projects": {WS: {"docker": {"n": 1, "ok": 0}}}}, open(STORE, "w"))
subprocess.run([sys.executable, MEMORY, "config", "speed-hints", "off"], capture_output=True)
start("off")
for i in range(3):
    cmd(f"false {i}", 1, "error")
think(10_000, 160_000)
check("speed-hints off → no hints", pre(), empty=True)
check("speed-hints off → no sandbox list at the start", session(), [], ["Terminal sandbox"])
same("speed-hints off → no BypassSandbox overwrite", "overwrite" in guard_cmd("docker ps"), False)
subprocess.run([sys.executable, MEMORY, "config", "speed-hints", "on"], capture_output=True)

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
