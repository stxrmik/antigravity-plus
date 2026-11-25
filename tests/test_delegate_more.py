"""Claude Code → Antigravity bridge, 0.29 (delegate.py) against a fake language server and agentapi: a task Antigravity
stopped with an error is resumed by itself (wait, the supervisor) up to --retries times and the report quotes the error;
an error a resume won't fix isn't resumed; a command still running in the background is listed with its last output,
log and processes; the images the agent opened (and couldn't); the queue (--max-parallel: queued, listed, canceled,
started by a waiter or by the supervisor, which then ends); watch numbers its events, never tells one twice (two
watches at once) and --follow takes tasks started later; series (run --series, next, next --fresh, series); templates
before the brief; estimate from past tasks (and filling in older ones); run --dry-run; wake; settings; the quota
forecast that agrees with its verdict; the rule about naming a background command it waits for."""
import http.server, json, os, pathlib, subprocess, sys, threading, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
DELEGATE = os.path.join(ROOT, "claude-code", "skills", "antigravity", "scripts", "delegate.py")
HOME = os.path.expanduser("~")
TASKS = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate")
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-1500:]}"))

ws = os.path.realpath(os.path.join(SP, "proj"))
os.makedirs(os.path.join(ws, "shots"))
open(os.path.join(ws, "src.py"), "w", encoding="utf-8").write("a = 1\n")
subprocess.run(["git", "init", "-q"], cwd=ws); subprocess.run(["git", "add", "-A"], cwd=ws)
subprocess.run(["git", "commit", "-qm", "init"], cwd=ws)
uri = lambda p: pathlib.Path(p).as_uri()
iso = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs))
cid = lambda c: c * 8 + "-aaaa-4bbb-8ccc-" + c * 12
E1, E2, E3, E4, BG, IMG, RUN1, QD, RUN2, QD2, W1, W2, SR1, SR2 = (cid(c) for c in "abcdef0123456789"[:14])
# the review's cases: a tell just before the end, a question while it works on, a cancel after an error stop, two runs
# at once, a conversation deleted in Antigravity (never listed)
W3, W4, E5, QA1, QA2, GONE, FIN = (c * 8 + "-dddd-4eee-8fff-" + c * 12 for c in "uvwxyzt")
SENT = os.path.join(SP, "agentapi-calls.jsonl")
EOF_ERR = "API error (attempt 1): stream reading error: unexpected EOF"
QUOTA_ERR = "RESOURCE_EXHAUSTED: You have exceeded your quota for this model"

def step(kind, **kw):
    return dict({"type": "CORTEX_STEP_TYPE_" + kind, "status": "CORTEX_STEP_STATUS_DONE"}, **kw)
def said(text):
    return step("PLANNER_RESPONSE", plannerResponse={"response": text})
def error_steps(text):  # a model call that broke off, as Antigravity records it
    return [step("PLANNER_RESPONSE", status="CORTEX_STEP_STATUS_ERROR", error={"shortError": "stream reading error"}),
            step("ERROR_MESSAGE", errorMessage={"error": {"shortError": text, "errorId": "x-1"}})]
def talk(verb, mid, text):
    kind = "question" if verb == "ask" else "note"
    line = f'python3 "{DELEGATE}" {verb} "{text}"'
    wire = json.dumps({"to_claude_code": kind, "id": mid, "text": text})
    return step("RUN_COMMAND", runCommand={"commandLine": line, "exitCode": 0, "combinedOutput": {
        "full": f"Command exited with code 0.\n\nCommand output:\n{wire}\n"}})
DONE = "STATUS: done\nCHANGED: src.py — a = 2\nCHECKS: pytest -q → 3 passed\nNOTES: none"
SLOW = [sys.executable, os.path.join(SP, "slow_job.py"), "--frames", "900"]
open(SLOW[1], "w", encoding="utf-8").write("import time\ntime.sleep(120)\n")
open(os.path.join(SP, "fake_ls.py"), "w", encoding="utf-8").write(
    "import subprocess, sys, time\n"
    f"child = subprocess.Popen({SLOW!r})\n"
    "time.sleep(120)\n")
LOG = os.path.join(SP, "brain", "task-2.log")
BASE = {
    E1: [step("USER_INPUT"), said(""), step("RUN_COMMAND", runCommand={"commandLine": "pytest -q", "exitCode": 0})]
        + error_steps(EOF_ERR),
    E2: [step("USER_INPUT"), said("")] + error_steps(EOF_ERR),
    E3: [step("USER_INPUT"), said("")] + error_steps(QUOTA_ERR),
    E4: [step("USER_INPUT"), said("")] + error_steps(EOF_ERR),
    BG: [],  # bg_steps(): its command went to the background five minutes before each look
    IMG: [step("USER_INPUT"), said(""),
          step("VIEW_FILE", viewFile={"absolutePathUri": uri(os.path.join(ws, "shots", "a.jpg")), "numBytes": "49290",
                                      "mediaData": {"mimeType": "image/jpeg", "inlineData": "/9j/AAAA"}}),
          step("VIEW_FILE", viewFile={"absolutePathUri": uri(os.path.join(ws, "shots", "a.jpg")), "numBytes": "49290",
                                      "mediaData": {"mimeType": "image/jpeg", "inlineData": "/9j/AAAA"}}),
          step("VIEW_FILE", status="CORTEX_STEP_STATUS_ERROR", error={"shortError": "file does not exist"},
               viewFile={"absolutePathUri": uri(os.path.join(ws, "shots", "missing.png"))}),
          step("VIEW_FILE", viewFile={"absolutePathUri": uri(os.path.join(ws, "src.py")), "content": "a = 1"}),
          said(DONE)],
    RUN1: [step("USER_INPUT"), said("")], QD: [step("USER_INPUT"), said(DONE)],
    RUN2: [step("USER_INPUT"), said("")], QD2: [step("USER_INPUT"), said(DONE)],
    W1: [step("USER_INPUT"), said(""), talk("ask", "aaaa1111", "Which port?"), said("Waiting for the answer.")],
    W2: [step("USER_INPUT"), said(""), talk("ask", "bbbb2222", "Keep the old name?"), said("Waiting for the answer.")],
    SR1: [step("USER_INPUT"), said(DONE)], SR2: [step("USER_INPUT"), said(DONE)],
    W3: [step("USER_INPUT"), said(""), talk("tell", "cccc3333", "The old tests were red before my change."), said(DONE)],
    W4: [step("USER_INPUT"), said(""), talk("ask", "dddd4444", "Keep the alias?"),
         step("RUN_COMMAND", status="CORTEX_STEP_STATUS_RUNNING", runCommand={"commandLine": "pytest -q"})],
    E5: [step("USER_INPUT"), said("")] + error_steps(EOF_ERR),
    QA1: [step("USER_INPUT"), said("")], QA2: [step("USER_INPUT"), said("")], FIN: [step("USER_INPUT"), said(DONE)],
}
UNLISTED = {GONE}  # conversations the fake doesn't list
def bg_steps():
    return [step("USER_INPUT"), said(""),
            step("RUN_COMMAND", status="CORTEX_STEP_STATUS_RUNNING",
                 runCommand={"commandLine": " ".join(SLOW), "combinedOutput": {"full": "frame 100/900"}},
                 taskDetails={"id": BG + "/task-2", "title": "Render frames", "description": " ".join(SLOW),
                              "progress": "frame 100/900\r\nframe 120/900\r\n", "logUri": uri(LOG),
                              "movedToBackgroundAt": iso(-300)}),
            said("Waiting for the render (task-2) to finish.")]
background = {BG}
finished = set()  # RUN1 / RUN2 once their «work» is over

def sends(c):
    try:
        return [json.loads(l) for l in open(SENT, encoding="utf-8")
                if json.loads(l)["argv"][:1] == ["send-message"] and c in json.loads(l)["argv"]]
    except OSError:
        return []
def steps_of(c):
    n = len(sends(c))
    if c in (E1, E4):
        return BASE[c] + ([step("SYSTEM_MESSAGE"), said(DONE)] if n else [])
    if c == E2:
        out = list(BASE[c])
        for _ in range(n):
            out += [step("SYSTEM_MESSAGE")] + error_steps(EOF_ERR)
        return out
    if c == BG:
        return bg_steps()
    if c == SR1:
        return BASE[c] + [s for _ in range(n) for s in (step("SYSTEM_MESSAGE"), said(DONE))]
    if c in (RUN1, RUN2) and c in finished:
        return BASE[c] + [said(DONE)]
    return BASE.get(c, [])
def status_of(c):
    if c in (W4, QA1, QA2):
        return "CASCADE_RUN_STATUS_RUNNING"  # W4 works on after its question; QA1/QA2 work
    return "CASCADE_RUN_STATUS_RUNNING" if c in (RUN1, RUN2) and c not in finished else "CASCADE_RUN_STATUS_IDLE"
def ending(c):
    if c in (E2, E3, E5) or (c in (E1, E4) and not sends(c)):
        return "EXECUTOR_TERMINATION_REASON_ERROR"
    return "EXECUTOR_TERMINATION_REASON_NO_TOOL_CALL"

def answer(method, body):
    if method == "GetCascadeModelConfigData":
        ms = [("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"), ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
              ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)")]
        return {"clientModelConfigs": [{"modelId": m, "label": l, "quotaInfo": {"remainingFraction": QUOTA[0], "resetTime": iso(QUOTA[1])}}
                                       for m, l in ms]}
    if method == "RetrieveUserQuotaSummary":
        return {"response": {"groups": [{"displayName": "Gemini Models", "description": "Models within this group: Gemini 4 Hydrogen, Gemini 4 Neon",
                                         "buckets": [{"window": "weekly", "remainingFraction": QUOTA[0], "resetTime": iso(QUOTA[1])},
                                                     {"window": "5h", "remainingFraction": 1, "resetTime": iso(4 * 3600)}]}]}}
    if method == "ListProjects":
        folder = os.path.join(HOME, ".gemini", "config", "projects")
        return {"projectIds": [f[:-5] for f in os.listdir(folder) if f.endswith(".json")] if os.path.isdir(folder) else []}
    if method == "GetAllCascadeTrajectories":
        return {"trajectorySummaries": {c: {"status": status_of(c), "stepCount": len(steps_of(c)), "summary": "task " + c[:4],
                                            "createdTime": iso(-120), "lastModifiedTime": iso(-60),
                                            "latestExecutorTerminationReason": ending(c)} for c in BASE
                                        if c not in UNLISTED}}
    if method == "WaitForConversationFullyIdle":
        c = body.get("conversationId")
        if c in background or status_of(c) != "CASCADE_RUN_STATUS_IDLE":
            time.sleep(2.5)
        return {}
    if method == "GetCascadeTrajectorySteps":
        return {"steps": steps_of(body.get("cascadeId"))[int(body.get("stepOffset") or 0):]}
    if method == "GetTrajectoryFileDiffs":
        return {}
    if method == "GetCascadeTrajectoryGeneratorMetadata":
        return {"generatorMetadata": [
            {"stepIndices": [1], "chatModel": {"modelDisplayName": "Gemini 4 Hydrogen (Low)", "responseModel": "gemini-4-hydrogen",
                                               "usage": {"inputTokens": "40000", "outputTokens": "200"}}},
            {"stepIndices": [2], "chatModel": {"modelDisplayName": "Gemini 4 Hydrogen (Low)", "responseModel": "gemini-4-hydrogen",
                                               "usage": {"inputTokens": "1000", "cacheReadTokens": "40000", "outputTokens": "300"}}}]}
    if method == "CancelCascadeInvocation":
        return {}
    raise KeyError(method)
QUOTA = [0.9, 6 * 86400]

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, data):
        raw = json.dumps(data).encode()
        try:
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        except OSError:
            pass
    def do_GET(self):
        self.reply(200, {"status": "ok"} if self.path == "/healthz" else {})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            self.reply(200, answer(self.path.rsplit("/", 1)[-1], body))
        except KeyError:
            self.reply(404, {"code": "unimplemented", "message": "no such method"})
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()

fake = os.path.join(SP, "fake_agentapi.py")
open(fake, "w", encoding="utf-8").write(
    "import json, os, sys\n"
    f"with open({SENT!r}, 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
    "if sys.argv[1] == 'new-conversation':\n"
    "    print(json.dumps({'response': {'newConversation': {'conversationId': os.environ['FAKE_CID']}}}))\n"
    "else:\n"
    "    print(json.dumps({'response': {'sendMessage': {'recipientId': sys.argv[2]}}}))\n")
ENV = dict(os.environ, ANTIGRAVITY_LS_ADDRESS=f"127.0.0.1:{server.server_address[1]}", ANTIGRAVITY_CSRF_TOKEN="tok",
           AGP_DELEGATE_AGENTAPI=json.dumps([sys.executable, fake]), AGP_DELEGATE_POLL="0.3", AGP_DELEGATE_IDLE_GRACE="1",
           AGP_DELEGATE_NO_LAUNCH="1", AGP_DELEGATE_WATCH_POLL="0.3", AGP_DELEGATE_IDLE_PROBE="1",
           AGP_DELEGATE_RETRY_DELAY="1", AGP_DELEGATE_NO_SUPERVISOR="1", AGP_DELEGATE_SUPERVISE_IDLE="2")
def run(*args, env=None, stdin=None, timeout=120):
    r = subprocess.run([sys.executable, DELEGATE, *args], cwd=ws, env=dict(ENV, **(env or {})), input=stdin,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, r.stdout + r.stderr
def spawn(*args, env=None):
    return subprocess.Popen([sys.executable, DELEGATE, *args], cwd=ws, env=dict(ENV, **(env or {})),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
def calls():
    try:
        return [json.loads(l) for l in open(SENT, encoding="utf-8")]
    except OSError:
        return []
def record(c):
    with open(os.path.join(TASKS, c + ".json"), encoding="utf-8") as f:
        return json.load(f)
def eventually(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return cond()
def supervisor_free():
    path = os.path.join(TASKS, ".supervisor.lock")
    if not os.path.exists(path):
        return True
    with open(path, "a+", encoding="utf-8") as fh:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            return True
        except OSError:
            return False

# 1. an error Antigravity ended the turn with: wait resumes it, the report quotes what happened
code, out = run("run", "--model", "fast", "--no-wait", "Fix the parser.", env={"FAKE_CID": E1})
prompt = calls()[-1]["argv"][-1]
check("run: the rule asks the agent to name the background command it waits for",
      code == 0 and "say in one line which command you wait for" in prompt and record(E1).get("retries_max") == 2, (out, prompt[:600]))
code, out = run("wait", E1[:8], "--timeout", "40")
resume = [c for c in sends(E1)]
check("wait: an «unexpected EOF» stop is resumed once, with the error quoted", len(resume) == 1
      and resume[0]["argv"][3].startswith("Claude Code says:\nAntigravity stopped your last turn with an error («" + EOF_ERR)
      and "Go on from where you stopped" in resume[0]["argv"][3], resume)
check("wait: … then the report: finished, resumed once in this turn", code == 0 and f"✅ Antigravity finished task {E1}" in out
      and f"Resumed automatically after 1 error in this turn: step 4 «{EOF_ERR}»" in out and "≈$" in out
      and "first call 40k, none from cache" in out, out)
rec = record(E1)
check("the record keeps the resume and the cost of the turn", len(rec.get("resumes") or []) == 1
      and rec["costs"]["0"]["calls"] == 2 and rec["costs"]["0"]["usd"] > 0 and rec["costs"]["0"]["first_call"] == 40000, rec)

code, out = run("run", "--model", "fast", "--no-wait", "Fix the lexer.", env={"FAKE_CID": E2})
code, out = run("wait", E2[:8], "--timeout", "40")
check("an error that keeps coming back: resumed twice (--retries 2), then reported with the error and what to do",
      code == 2 and len(sends(E2)) == 2 and f"❌ Antigravity task {E2} stopped by an error" in out and f"«{EOF_ERR}»" in out
      and "Not resumed automatically: it was resumed 2 times already" in out and f"delegate.py send {E2[:8]}" in out
      and "Antigravity's error above stopped it" in out, out)
code, out = run("run", "--model", "fast", "--no-wait", "--retries", "3", "Fix the cache.", env={"FAKE_CID": E3})
code, out = run("wait", E3[:8], "--timeout", "30")
check("a quota error isn't resumed — the report says why", code == 2 and not sends(E3) and QUOTA_ERR in out
      and "doesn't look like a hiccup a resume fixes" in out and record(E3).get("no_resume_at") == 4, out)
code, out = run("watch", E2[:8], "--timeout", "3")
check("watch after wait's report: nothing told twice — its resumes and its end were in the report", code == 0
      and "#1" not in out and EOF_ERR not in out, out)

# 2. the supervisor resumes a task nobody waits for, then ends on its own
code, out = run("run", "--model", "fast", "--no-wait", "Fix the printer.", env={"FAKE_CID": E4, "AGP_DELEGATE_NO_SUPERVISOR": ""})
check("supervisor: resumes the task by itself — no wait, no watch", eventually(lambda: len(sends(E4)) == 1, 25), calls()[-3:])
check("supervisor: … keeps the record up to date and ends when nothing is left", eventually(lambda: record(E4).get("state") == "done", 15)
      and eventually(supervisor_free, 20), record(E4))
log = open(os.path.join(TASKS, "supervisor.log"), encoding="utf-8").read()
check("supervisor: its log", f"resumed {E4} after an error: {EOF_ERR}" in log and "ended" in log, log)
code, out = run("watch", E4[:8], "--timeout", "3")
check("watch on a task the supervisor resumed and saw end: the resume as a numbered event, then the end",
      code == 0 and f"🔁 #1 Antigravity task {E4[:8]}" in out and "(1 of 2)" in out and EOF_ERR in out
      and f"✅ #2 Antigravity task {E4[:8]} finished" in out, out)

# 3. a command still running in the background: listed with its last output, log and processes
fake_ls = subprocess.Popen([sys.executable, os.path.join(SP, "fake_ls.py")])
time.sleep(1.5)
code, out = run("run", "--model", "fast", "--no-wait", "Render the frames.", env={"FAKE_CID": BG})
code, out = run("result", BG[:8], env={"AGP_DELEGATE_LS_PID": str(fake_ls.pid)})
check("result: the background command, how long it runs, its last output and log", code == 0
      and "Still running in the background" in out and "⏳ task-2 " in out and "for 5 min" in out
      and "last output: frame 120/900" in out and f"log: {LOG}" in out, out)
procs_line = next((ln for ln in out.splitlines() if "processes:" in ln), "")
check("result: … and the processes that run it (found under the language server)", "processes:" in procs_line
      and "python" in procs_line.lower(), out)
check("result: a running background command isn't listed as a finished command", "→ ?" not in out, out)
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # noqa: E402
compat.kill_tree(fake_ls.pid)  # the fake language server and the «render» it started
background.discard(BG)  # … and the render is over: its slot is free for the queue below

# 4. the images it opened — and the one it couldn't
code, out = run("run", "--model", "fast", "--no-wait", "Look at the shots.", env={"FAKE_CID": IMG})
code, out = run("result", IMG[:8])
check("result: the images it opened (each once, with its size and how often) and the one it couldn't",
      "Images it opened (1): shots/a.jpg (48 KB, 2×)" in out and "couldn't open 1: shots/missing.png — file does not exist" in out
      and "src.py (" not in out.split("Images it opened")[1].split("\n")[0], out)

# 5. the queue: --max-parallel 1 while a task runs
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "First job.", env={"FAKE_CID": RUN1})
check("queue: the first task starts at once", code == 0 and f"Started Antigravity task {RUN1}" in out, out)
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Second job.", env={"FAKE_CID": QD})
qid = next((w for w in out.split() if w.startswith("q-")), "").rstrip(":")
check("queue: the second waits — a q- id, how to follow it", code == 0 and qid and "⏸ Queued Antigravity task" in out
      and "1 task run now (--max-parallel 1)" in out and f"delegate.py wait {qid}" in out
      and not any(c["argv"][-1].endswith("Second job.") for c in calls()), out)
code, out = run("list")
check("list: the queue first", code == 0 and out.startswith("Queued") and qid in out.split("Recent")[0], out)
code, out = run("result", qid)
check("result of a queued task: its place in the queue", code == 3 and "waits in the queue (number 1 of 1" in out, out)
code, out = run("cancel", qid)
check("cancel: takes it off the queue", code == 0 and "off the queue" in out and record(qid)["state"] == "canceled", out)
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Second job.", env={"FAKE_CID": QD})
qid = next((w for w in out.split() if w.startswith("q-")), "").rstrip(":")
waiter = spawn("wait", qid, "--timeout", "40", env={"FAKE_CID": QD})
time.sleep(2)
check("queue: still waiting while the slot is taken", waiter.poll() is None and record(qid)["state"] == "queued", record(qid))
finished.add(RUN1)  # the first task is over: its slot frees up
wout, _ = waiter.communicate(timeout=60)
check("queue: the waiter starts it when the slot frees up, then reports it", waiter.returncode == 0
      and f"Started queued task {qid} as {QD}" in wout and f"✅ Antigravity finished task {QD}" in wout
      and record(qid)["state"] == "started" and record(QD).get("queued_as") == qid, wout)
code, out = run("result", qid)
check("result: a started queued id stands for its task", f"Antigravity finished task {QD}" in out, out)
# … and the supervisor does the same when nobody waits
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Third job.", env={"FAKE_CID": RUN2})
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Fourth job.",
                env={"FAKE_CID": QD2, "AGP_DELEGATE_NO_SUPERVISOR": ""})
q2 = next((w for w in out.split() if w.startswith("q-")), "").rstrip(":")
time.sleep(1.5)
check("supervisor: a queued task waits while the slot is taken", record(q2)["state"] == "queued", record(q2))
finished.add(RUN2)
check("supervisor: starts it once the slot frees up", eventually(lambda: record(q2).get("state") == "started", 25), record(q2))
check("supervisor: … and ends when nothing is left", eventually(supervisor_free, 25))

# 6. watch: numbered events, never twice — two watches at once — and --follow takes a task started later
code, out = run("run", "--model", "fast", "--no-wait", "Pick a port.", env={"FAKE_CID": W1, "CLAUDE_CODE_SESSION_ID": "s-1"})
a, b = spawn("watch", W1[:8], "--timeout", "4"), spawn("watch", W1[:8], "--timeout", "4")
out_a, _ = a.communicate(timeout=60)
out_b, _ = b.communicate(timeout=60)
check("two watches at once: the question arrives once, numbered #1", (out_a + out_b).count("Which port?") == 1
      and "❓ #1 Antigravity task" in out_a + out_b, (out_a, out_b))
follow = spawn("watch", "--follow", "--timeout", "12", env={"CLAUDE_CODE_SESSION_ID": "s-1"})
time.sleep(2)
code, out = run("run", "--model", "fast", "--no-wait", "Rename it.", env={"FAKE_CID": W2, "CLAUDE_CODE_SESSION_ID": "s-1"})
fout, _ = follow.communicate(timeout=60)
check("watch --follow: takes a task this session started after the watch began", follow.returncode == 0
      and "Keep the old name?" in fout and "Which port?" not in fout and "❓ #1 " in fout, fout)

# 7. a series: the next piece in the same conversation, the templates once, a fresh conversation on demand
tpl = os.path.join(ws, "port-conventions.md")
open(tpl, "w", encoding="utf-8").write("Port one module per piece; keep the public names and list what changed in PORTING.md.")
code, out = run("run", "--model", "fast", "--no-wait", "--series", "port-7", "--template", tpl, "Piece 1: the parser.",
                env={"FAKE_CID": SR1})
prompt = calls()[-1]["argv"][-1]
check("run --template: the reference before the folder and the task", code == 0
      and prompt.index("Reference for this kind of task") < prompt.index("## port-conventions.md") < prompt.index("Working folder")
      < prompt.index("Piece 1: the parser."), prompt[-700:])
code, out = run("run", "--model", "fast", "--no-wait", "--series", "port-7", "Again.", env={"FAKE_CID": SR2})
check("run --series: a name in use is refused", code == 1 and "exists already" in out, out)
code, out = run("next", "port-7", "Piece 2: the lexer.", "--no-wait")
msg = sends(SR1)[-1]["argv"][3] if sends(SR1) else ""
check("next: the next piece goes into the same conversation", code == 0 and "Sent piece 2 of «port-7»" in out
      and msg.startswith("Follow-up from Claude Code: Piece 2 of the series «port-7»:\nPiece 2: the lexer.")
      and [p["n"] for p in record(SR1)["pieces"]] == [1, 2], (out, msg))
code, out = run("series", "port-7")
check("series: its pieces and how each ended", code == 0 and "#1 " in out and "#2 " in out and "STATUS: done" in out, out)
code, out = run("next", "port-7", "--fresh", "Piece 3: the printer.", "--no-wait", env={"FAKE_CID": SR2})
prompt = calls()[-1]["argv"][-1]
check("next --fresh: a new conversation with the series' template, the last report and the new piece",
      code == 0 and "starts a new conversation (--fresh)" in out and "This continues the series «port-7»: pieces 1–2" in prompt
      and "port-conventions.md" in prompt and "STATUS: done" in prompt and prompt.rstrip().endswith("Piece 3: the printer.")
      and record(SR2).get("series") == "port-7" and record(SR2)["pieces"][0]["n"] == 3, (out, prompt[-900:]))

# 8. estimate (and filling in the cost of a task from before reports saved it), dry run
old = record(IMG)
old.pop("costs", None)
old["state"] = "done"
json.dump(old, open(os.path.join(TASKS, IMG + ".json"), "w", encoding="utf-8"))
code, out = run("estimate", "--model", "fast")
check("estimate: what a task costs here, from your own tasks", code == 0 and "--model fast → Gemini 4 Hydrogen (Low)" in out
      and "per task (median of" in out and "at API prices" in out and "first call reads ≈40k tokens here" in out
      and "Every tier now:" in out and "Quota left:" in out, out)
check("estimate: … after filling in the cost of an older task", (record(IMG).get("costs") or {}).get("0", {}).get("backfilled") is True,
      record(IMG).get("costs"))
before = len(calls())
code, out = run("run", "--dry-run", "--model", "fast", "Port the parser.")
check("run --dry-run: the model, why, the expected cost — and nothing started", code == 0 and "Would start on Gemini 4 Hydrogen (Low)" in out
      and "A task here: ≈" in out and "Nothing was started (--dry-run)" in out and len(calls()) == before, out)

# 8b. what a review found: each event once, an end nobody told yet, a question's slot, cancels that hold, two runs at
# once, a deleted conversation, a long piece, a template that isn't UTF-8
code, out = run("run", "--model", "fast", "--no-wait", "Fix the tests.", env={"FAKE_CID": W3})
code, out = run("watch", W3[:8], "--timeout", "5")
check("watch: a message just before the end comes once, numbered, then the end", code == 0
      and out.count("The old tests were red before my change.") == 1 and "💬 #1 " in out and "✅ #2 " in out, out)
code, out = run("run", "--model", "fast", "--no-wait", "Small fix.", env={"FAKE_CID": FIN, "CLAUDE_CODE_SESSION_ID": "s-2"})
code, out = run("list")
check("list: records the finished task as done (no watch told Claude Code)", record(FIN)["state"] == "done", record(FIN))
code, out = run("watch", "--follow", "--timeout", "4", env={"CLAUDE_CODE_SESSION_ID": "s-2"})
check("watch --follow: the end of a task that was over before the watch began still arrives, once",
      f"✅ #1 Antigravity task {FIN[:8]} finished" in out, out)
code, out = run("watch", "--follow", "--timeout", "3", env={"CLAUDE_CODE_SESSION_ID": "s-2"})
check("… and the next watch doesn't repeat it", FIN[:8] not in out, out)

code, out = run("run", "--model", "fast", "--no-wait", "Rename it.", env={"FAKE_CID": W4})
code, out = run("wait", W4[:8], "--timeout", "4")
check("wait: its question (exit 4) while it works on", code == 4 and "Keep the alias?" in out
      and record(W4)["state"] == "question", out)
code, out = run("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Another job.", env={"FAKE_CID": QD})
q3 = next((w for w in out.split() if w.startswith("q-")), "").rstrip(":")
check("queue: a task that asked a question and works on keeps its slot — the next one waits", "⏸ Queued" in out and q3, out)
run("cancel", q3)
code, out = run("wait", q3, "--timeout", "5")
check("wait on a task taken off the queue: says so, waits for nothing", code == 2 and "taken off the queue" in out, out)
code, out = run("cancel", W4[:8])

code, out = run("run", "--model", "fast", "--no-wait", "Fix the cache again.", env={"FAKE_CID": E5})
code, out = run("cancel", E5[:8])
code, out = run("result", E5[:8])
check("a task canceled after an error stop isn't resumed by a later look", not sends(E5)
      and record(E5)["state"] == "canceled", (out, record(E5)))

j1 = spawn("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Job one.", env={"FAKE_CID": QA1})
j2 = spawn("run", "--model", "fast", "--no-wait", "--max-parallel", "1", "Job two.", env={"FAKE_CID": QA2})
o1, _ = j1.communicate(timeout=120)
o2, _ = j2.communicate(timeout=120)
check("two runs at once with --max-parallel 1: one starts, the other waits in the queue",
      (o1 + o2).count("Started Antigravity task") == 1 and (o1 + o2).count("⏸ Queued") == 1, (o1, o2))
q4 = next((w for w in (o1 + o2).split() if w.startswith("q-")), "").rstrip(":")
q4_path = os.path.join(TASKS, q4 + ".json")
rec = json.load(open(q4_path, encoding="utf-8"))
rec["state"], rec["starting"] = "starting", time.time()  # a dispatcher is starting it this very moment
json.dump(rec, open(q4_path, "w", encoding="utf-8"))
canceling = spawn("cancel", q4)
time.sleep(2.5)
rec = json.load(open(q4_path, encoding="utf-8"))
rec.update(state="started", started_as=QD)  # … and it started as QD
json.dump(rec, open(q4_path, "w", encoding="utf-8"))
cout, _ = canceling.communicate(timeout=60)
check("cancel while a queued task is being started: it waits for the start and stops what it became",
      canceling.returncode == 0 and f"has started as {QD[:8]}" in cout and record(QD)["state"] == "canceled", cout)
for c in (QA1, QA2):
    if os.path.exists(os.path.join(TASKS, c + ".json")):
        run("cancel", c[:8])

code, out = run("run", "--model", "fast", "--no-wait", "Some job.", env={"FAKE_CID": GONE})
rec = record(GONE)
rec["started"] = time.time() - 300
json.dump(rec, open(os.path.join(TASKS, GONE + ".json"), "w", encoding="utf-8"))
code, out = run("result", GONE[:8])
check("result: a conversation Antigravity no longer lists is gone, and the record says so", code == 2 and "is gone" in out
      and record(GONE)["state"] == "gone", out)

long_piece = "Modules to port next: " + "x" * 6000
before_sends = len(sends(SR2))
code, out = run("next", "port-7", long_piece, "--no-wait")
check("next: a piece longer than a reply goes out whole", code == 0 and len(sends(SR2)) == before_sends + 1
      and long_piece in sends(SR2)[-1]["argv"][3], out)
code, out = run("next", "port-7", "y" * 25000, "--no-wait")
check("next: a piece longer than a brief is refused at once — nothing sent", code == 1 and "nothing was sent" in out
      and len(sends(SR2)) == before_sends + 1, out)
bad_tpl = os.path.join(ws, "legacy.md")
open(bad_tpl, "wb").write("Шаблон отчёта".encode("cp1251"))
code, out = run("run", "--model", "fast", "--no-wait", "--template", bad_tpl, "Job.")
check("a template that isn't UTF-8: one clear line, no traceback", code == 1 and "isn't UTF-8 text" in out
      and "Traceback" not in out, out)

# 9. wake, settings, the quota forecast
code, out = run("wake", "--timeout", "5")
check("wake: Antigravity is ready", code == 0 and "is ready: 3 models" in out, out)
code, out = run("settings", "--max-parallel", "2", "--retries", "1")
check("settings: saved and shown", code == 0 and "Saved." in out and "max-parallel 2" in out and "retries 1" in out
      and json.load(open(os.path.join(TASKS, "settings.json"), encoding="utf-8")) == {"max_parallel": 2, "retries": 1}, out)
code, out = run("settings", "--retries", "99")
check("settings: a value out of range is refused", code == 1 and "between 0 and 10" in out, out)
QUOTA[:] = [0.45, 3.5 * 86400]
code, out = run("budget")
check("budget: a week that won't last at this pace isn't just «fine»",
      "spent 16% a day so far → at that pace it runs out ~" in out and "→ fine for now — keep to about 13% a day" in out, out)
QUOTA[:] = [0.2, 6.5 * 86400]
code, out = run("budget")
check("budget: spending far ahead of the week → tight, with the numbers", "a day would last → tight — save it" in out, out)
server.shutdown()
sys.exit(0 if ok else 1)
