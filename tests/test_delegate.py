"""Claude Code → Antigravity bridge (claude-code/skills/antigravity/scripts/delegate.py) against a fake language
server and a fake agentapi: tiers and the fallback when a quota is used up, the choice by the limits left (a quota
spent faster than the week lasts, one about to reset unused, everything used up), Claude Code's own limits (given,
from the statusLine, unknown), measured speed, a new Antigravity project for an unknown folder, the report (changed
files, commands, cost, the agent's words), an approval the task waits for, a timeout, a follow-up, diff, cancel,
list, errors — talking while it works (the agent's ask / tell, in a command's output or in the message of a command
that went to the background; wait → exit 4 on a question; watch for the Monitor tool; reply; send to a running
task) — a task that ended its turn while a build it started still runs in the background (result, list, watch and
wait don't call it finished; the record follows), Gemini 4 Neon and Orchestrator (Neon) in the tiers, quota groups
and reports — how the plugin's hooks treat a delegated conversation (no nudges while it waits for Claude Code's
answer, the guard lets the talk commands through and nothing else), and the usage panel naming its model and task."""
import http.server, json, os, pathlib, re, shutil, subprocess, sys, threading, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
DELEGATE = os.path.join(ROOT, "claude-code", "skills", "antigravity", "scripts", "delegate.py")
HOOKS = os.path.join(PLUGIN, "hooks")
USAGE = os.path.join(PLUGIN, "skills", "usage", "scripts", "usage.py")
HOME = os.path.expanduser("~")
INSTALLED = os.path.join(HOME, ".claude", "skills", "antigravity", "scripts", "delegate.py")  # where the guard trusts it
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-900:]}"))

ws = os.path.realpath(os.path.join(SP, "proj"))
os.makedirs(ws)
for name, text in {"src.py": "a = 1\nb = 2\n", "AGENTS.md": "# rules\n"}.items():
    open(os.path.join(ws, name), "w", encoding="utf-8").write(text)
subprocess.run(["git", "init", "-q"], cwd=ws); subprocess.run(["git", "add", "-A"], cwd=ws)
subprocess.run(["git", "commit", "-qm", "init"], cwd=ws)
uri = lambda p: pathlib.Path(p).as_uri()
EMPTY = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
C1, C2, C3, C4, C5, C6, C7, C8, C9 = (c * 8 + "-aaaa-4bbb-8ccc-" + c * 12 for c in "abcdef789")
CB, CN, CX = (c * 8 + "-aaaa-4bbb-8ccc-" + c * 12 for c in "123")  # a background build, Orchestrator (Neon), Neon
SENT = os.path.join(SP, "agentapi-calls.jsonl")

def step(kind, **kw):
    return dict({"type": "CORTEX_STEP_TYPE_" + kind, "status": "CORTEX_STEP_STATUS_DONE"}, **kw)
def talk_line(verb, text):
    return f'python3 "{INSTALLED}" {verb} "{text}"'
def wire(kind, mid, text):
    return json.dumps({"to_claude_code": kind, "id": mid, "text": text}, ensure_ascii=False)
def talk_step(verb, mid, text):  # the agent's ask / tell, its output in the command's own step
    kind = "question" if verb == "ask" else "note"
    return step("RUN_COMMAND", runCommand={"commandLine": talk_line(verb, text), "exitCode": 0, "combinedOutput": {
        "full": f"Command exited with code 0.\n\nCommand output:\n{wire(kind, mid, text)}\nSent to Claude Code.\n"}})
Q1, N1, Q2 = "Keep parse_date as an alias of the new name?", "Tests already failed before my change: test_y", \
    "Which port should the dev server use?"
BASE = {
    C1: [step("USER_INPUT"), step("SYSTEM_MESSAGE"),
         step("PLANNER_RESPONSE", plannerResponse={"response": ""}),
         step("RUN_COMMAND", runCommand={"commandLine": "python3 -m unittest -v", "exitCode": 0, "combinedOutput": {
             "full": "Command exited with code 0.\n\nCommand output:\ntest_b ... ok\n\nRan 3 tests in 0.01s\n\nOK\n"}}),
         step("PLANNER_RESPONSE", plannerResponse={"modifiedResponse": "STATUS: done\nCHANGED: [`src.py`](" + uri(
             os.path.join(ws, "src.py")) + "#L2) — b = 3\nCHECKS: python3 -m unittest -v → Ran 3 tests, OK\nNOTES: none"})],
    C2: [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}),
         dict(step("GENERIC"), status="CORTEX_STEP_STATUS_WAITING", requestedInteraction={"permission": {
             "resource": {"action": "write_file", "target": "/x/.git/hooks/pre-commit"}, "reason": "🛡️ guard says"}})],
    C3: [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""})],
    # it told Claude Code something, asked a question and ended its turn to wait for the answer
    C4: [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}), talk_step("tell", "aaaa1111", N1),
         talk_step("ask", "bbbb2222", Q1), step("PLANNER_RESPONSE", plannerResponse={"response": "Waiting for Claude Code's answer."})],
    # it asked from a command that went to the background (the output comes in Antigravity's message) and works on
    C5: [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}),
         step("RUN_COMMAND", runCommand={"commandLine": talk_line("ask", Q2), "combinedOutput": {"full": "running in the background"}}),
         step("SYSTEM_MESSAGE", systemMessage={"agentMessage": {"sender": C5 + "/task-2", "content":
              f'Task id "{C5}/task-2" finished with result:\nCommand exited with code 0.\n\nCommand output:\n{wire("question", "cccc3333", Q2)}\n',
              "sourceMetadata": {"tool": {"toolCall": {"name": "run_command", "argumentsJson": json.dumps({"CommandLine": talk_line("ask", Q2)})}}}}}),
         step("RUN_COMMAND", status="CORTEX_STEP_STATUS_RUNNING", runCommand={"commandLine": "npm run build"})],
}
FOLLOW = [step("SYSTEM_MESSAGE"), step("RUN_COMMAND", runCommand={"commandLine": "pytest -q", "exitCode": 0, "combinedOutput": {
    "full": "Command exited with code 0.\n\nCommand output:\n4 passed in 0.2s\n"}}),
    step("PLANNER_RESPONSE", plannerResponse={"response": "STATUS: done\nCHANGED: src.py — c = 4\nCHECKS: pytest -q → 4 passed\nNOTES: none"})]
ANSWERED = [step("SYSTEM_MESSAGE"), step("PLANNER_RESPONSE", plannerResponse={"response":
    "STATUS: done\nCHANGED: src.py — renamed, alias kept\nCHECKS: pytest -q → 5 passed\nNOTES: kept the alias as Claude Code said"})]
# seen live: tell and ask from one response — while the tell ran, the ask's step was there without its command line
N7, Q7 = "Found 3 places that use calc.", "Which new name should calc get?"
FILLED_AFTER = 1.0  # seconds after the first look
first_look = {}
def c7_steps():
    filled = time.time() - first_look.setdefault(C7, time.time()) >= FILLED_AFTER
    return [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}), talk_step("tell", "dddd4444", N7),
            talk_step("ask", "eeee5555", Q7) if filled else step("RUN_COMMAND", status="CORTEX_STEP_STATUS_PENDING", runCommand={})] \
        + ([step("PLANNER_RESPONSE", plannerResponse={"response": "Waiting for Claude Code's answer."})] if filled else [])
BASE[C7] = []   # listed among the conversations; its steps come from c7_steps()
BASE[C8] = BASE[C4][:]  # like C4: a record that once stepped over the question (fixed by a last look over the turn)
# Orchestrator (Carbon): Carbon plans, then Gemini 4 Hydrogen carries the plan out (its metadata: answer())
BASE[C9] = [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}), step("SYSTEM_MESSAGE"),
            step("PLANNER_RESPONSE", plannerResponse={"response": ""}),
            step("PLANNER_RESPONSE", plannerResponse={"response": "STATUS: done\nCHANGED: src.py — d = 5\nCHECKS: none\nNOTES: none"})]
BASE[CN] = BASE[C9][:-1] + [step("PLANNER_RESPONSE", plannerResponse={"response": "STATUS: done\nCHANGED: src.py — e = 6\nCHECKS: none\nNOTES: none"})]
# seen live: a long build went to the background and the agent ended its turn to wait for it — the summary says IDLE,
# but WaitForConversationFullyIdle blocks while the build runs; when it is over, the agent goes on and reports
BUILD, WAITING = "xcodebuild -scheme Demo build", "Waiting for the background build (task-219) to finish."
BASE[CB] = [step("USER_INPUT"), step("PLANNER_RESPONSE", plannerResponse={"response": ""}),
            step("RUN_COMMAND", runCommand={"commandLine": BUILD, "combinedOutput": {"full": "Running in the background as task-219."}}),
            step("PLANNER_RESPONSE", plannerResponse={"response": WAITING})]
BUILT = [step("SYSTEM_MESSAGE", systemMessage={"agentMessage": {"sender": CB + "/task-219", "content":
             f'Task id "{CB}/task-219" finished with result:\nCommand exited with code 0.\n\nCommand output:\n** BUILD SUCCEEDED **\n'}}),
         step("PLANNER_RESPONSE", plannerResponse={"response": f"STATUS: done\nCHANGED: nothing\nCHECKS: {BUILD} → BUILD SUCCEEDED\nNOTES: none"})]
background = {CB}  # conversations whose background work still runs
cancelled = set()

def messaged(cid):
    try:
        return any(json.loads(l)["argv"][:1] == ["send-message"] and cid in json.loads(l)["argv"]
                   for l in open(SENT, encoding="utf-8"))
    except OSError:
        return False
def steps_of(cid):
    if cid == C7:
        return c7_steps()
    if cid == CB:
        return BASE[CB] + ([] if CB in background else BUILT)
    return BASE.get(cid, []) + (FOLLOW if cid == C1 and messaged(C1) else ANSWERED if cid == C4 and messaged(C4) else [])
def status_of(cid):
    if cid == C7:
        return "CASCADE_RUN_STATUS_IDLE" if len(c7_steps()) == 5 else "CASCADE_RUN_STATUS_RUNNING"
    return "CASCADE_RUN_STATUS_IDLE" if cid in (C1, C4, C8, C9, CB, CN) or cid in cancelled else "CASCADE_RUN_STATUS_RUNNING"

iso = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs))
# what the fake plan has left: weekly fraction (None = used up) and seconds until the week resets, per group
QUOTA = {"gemini": [0.9, 6 * 86400], "claude": [None, 22 * 3600]}
MODEL_LEFT = {}  # model id → its own fraction left instead of its group's (None = used up)
# in Antigravity's order (2.32, 2026-11-19): Gemini 4 Neon first — Neon (High) is the default — and Orchestrator (Neon)
# before Orchestrator (Carbon)
MODELS = [("gemini-4-neon-high", "Gemini 4 Neon (High)", "M532"), ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)", "M535"),
          ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)", "M533"),
          ("gemini-4-carbon-medium", "Gemini 4 Carbon (Medium)", "M437"), ("gemini-4-carbon-high", "Gemini 4 Carbon (High)", "M427"),
          ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)", "M527"), ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)", "M526"),
          ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)", "M525"), ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)", "M319"),
          ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)", "M404"), ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)", "M405"),
          ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)", "M537"), ("gemini-pro-orchestrator", "Orchestrator (Carbon)", "M667")]

def answer(method, body):
    if method == "GetCascadeModelConfigData":
        def cfg(mid, label, enum):
            left, secs = QUOTA["claude" if mid.startswith("claude") else "gemini"]
            left = MODEL_LEFT.get(mid, left)
            q = {"resetTime": iso(secs)}
            if left:  # proto3 JSON leaves a 0 out: no remainingFraction means used up
                q["remainingFraction"] = left
            return {"modelId": mid, "label": label, "modelOrAlias": {"model": enum}, "quotaInfo": q}
        return {"clientModelConfigs": [cfg(*m) for m in MODELS], "defaultOverrideModelConfig": {"modelOrAlias": {"model": "M532"}}}
    if method == "RetrieveUserQuotaSummary":
        def bucket(key):
            left, secs = QUOTA[key]
            weekly = {"window": "weekly", "resetTime": iso(secs)}
            if left:
                weekly["remainingFraction"] = left
            five = {"window": "5h", "remainingFraction": 1, "resetTime": iso(4 * 3600)}
            if not left:
                five["disabled"] = True
            return [weekly, five]
        return {"response": {"groups": [
            {"displayName": "Gemini Models", "description": "Models within this group: Gemini 4 Neon, Gemini 4 Carbon, "
                                                            "Gemini 4 Argon, Gemini 4 Hydrogen, Orchestrator (Neon), "
                                                            "Orchestrator (Carbon), Orchestrator (Argon), Gemini 3.8 Flash",
             "buckets": bucket("gemini")},
            {"displayName": "Claude and GPT models", "description": "Models within this group: Claude Sonnet 5.5, Claude Opus 5.5",
             "buckets": bucket("claude")}]}}
    if method == "ListProjects":
        folder = os.path.join(HOME, ".gemini", "config", "projects")
        return {"projectIds": [f[:-5] for f in os.listdir(folder) if f.endswith(".json")] if os.path.isdir(folder) else []}
    if method == "GetAllCascadeTrajectories":
        return {"trajectorySummaries": {c: {"status": status_of(c), "stepCount": len(steps_of(c)), "summary": "Claude Code: task " + c[-1],
                                            "createdTime": "2026-11-14T10:00:00Z", "lastModifiedTime": "2026-11-14T10:02:05Z",
                                            "latestExecutorTerminationReason": "EXECUTOR_TERMINATION_REASON_NO_TOOL_CALL"} for c in BASE}}
    if method == "WaitForConversationFullyIdle":
        if body.get("conversationId") in background:
            time.sleep(3)  # idle, but its background work goes on: longer than any client waits (IDLE_PROBE, POLL)
        elif status_of(body.get("conversationId")) != "CASCADE_RUN_STATUS_IDLE":
            time.sleep(2)  # longer than the client waits
        return {}
    if method == "GetCascadeTrajectorySteps":
        return {"steps": steps_of(body.get("cascadeId"))[int(body.get("stepOffset") or 0):]}
    if method == "GetTrajectoryFileDiffs":
        if body.get("conversationId") != C1:
            return {}
        return {"diffs": [{"uri": uri(os.path.join(ws, "src.py")), "originalContent": "a = 1\nb = 2\n", "modifiedContent": "a = 1\nb = 3\n",
                           "originalContentHash": "abc"},
                          {"uri": uri(os.path.join(ws, "test_new.py")), "modifiedContent": "def test_x():\n    assert True\n",
                           "originalContentHash": EMPTY},
                          {"uri": uri(os.path.join(SP, "brain", "task.md")), "modifiedContent": "- [x] a\n", "isArtifactFile": True}]}
    if method == "GetCascadeTrajectoryGeneratorMetadata":
        if body.get("cascadeId") == C6:  # a conversation some other tool started through agentapi
            return {"generatorMetadata": [{"stepIndices": [1], "chatModel": {"modelDisplayName": "Gemini 4 Argon (High)",
                                                                              "responseModel": "gemini-4-argon"}}]}
        # Orchestrator, as Antigravity 2.32 records it: the planner, then Hydrogen (assumed the same for Orchestrator
        # (Neon) — not seen live yet)
        planner = {C9: ("Orchestrator (Carbon)", "gemini-pro-orchestrator"),
                   CN: ("Orchestrator (Neon)", "gemini-pro-orchestrator-neon")}.get(body.get("cascadeId"))
        if planner:
            plan = {"modelDisplayName": planner[0], "responseModel": planner[1], "timeToFirstToken": "8s", "streamingDuration": "2s"}
            work = {"modelDisplayName": "Gemini 4 Hydrogen (Orchestrator)", "responseModel": "gemini-flash-worker-agy",
                    "timeToFirstToken": "2s", "streamingDuration": "1s"}
            return {"generatorMetadata": [
                {"stepIndices": [1], "chatModel": dict(plan, usage={"inputTokens": "3000", "outputTokens": "5000"})},
                {"stepIndices": [3], "chatModel": dict(work, usage={"inputTokens": "500", "cacheReadTokens": "3000", "outputTokens": "200"})},
                {"stepIndices": [4], "chatModel": dict(work, usage={"inputTokens": "400", "cacheReadTokens": "3500", "outputTokens": "300"})}]}
        timing = {"modelDisplayName": "Gemini 4 Hydrogen (High)", "timeToFirstToken": "1.5s", "streamingDuration": "0.5s"}
        return {"generatorMetadata": [{"stepIndices": [2], "chatModel": dict(timing, responseModel="gemini-4-hydrogen", usage={
            "inputTokens": "1000", "cacheReadTokens": "20000", "outputTokens": "300"})},
            {"stepIndices": [4], "chatModel": dict(timing, responseModel="gemini-4-hydrogen", usage={"inputTokens": "500", "outputTokens": "100"})}]}
    if method == "CancelCascadeInvocation":
        cid = body.get("cascadeId")
        if status_of(cid) == "CASCADE_RUN_STATUS_IDLE":
            raise LookupError("executor is not currently running")
        cancelled.add(cid)
        return {}
    raise KeyError(method)

calls = []
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, data):
        raw = json.dumps(data).encode()
        try:
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        except OSError:
            pass  # the client stopped waiting
    def do_GET(self):
        self.reply(200, {"status": "ok"} if self.path == "/healthz" else {})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("x-codeium-csrf-token") != "tok":
            return self.reply(403, {"code": "permission_denied", "message": "bad csrf token"})
        method = self.path.rsplit("/", 1)[-1]
        calls.append(method)
        try:
            self.reply(200, answer(method, body))
        except LookupError as e:
            self.reply(500, {"code": "unknown", "message": str(e)})
        except KeyError:
            self.reply(404, {"code": "unimplemented", "message": "no such method"})
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()

fake = os.path.join(SP, "fake_agentapi.py")
open(fake, "w", encoding="utf-8").write(
    "import json, os, sys\n"
    f"with open({SENT!r}, 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps({'argv': sys.argv[1:], 'project': os.environ.get('ANTIGRAVITY_PROJECT_ID'),\n"
    "                        'ls': os.environ.get('ANTIGRAVITY_LS_ADDRESS')}) + '\\n')\n"
    "if os.environ.get('FAKE_TOUCH'):\n"
    "    open(os.environ['FAKE_TOUCH'], 'w').write('made by a command\\n')\n"
    "if sys.argv[1] == 'new-conversation':\n"
    "    print(json.dumps({'response': {'newConversation': {'conversationId': os.environ['FAKE_CID']}}}))\n"
    "else:\n"
    "    print(json.dumps({'response': {'sendMessage': {'recipientId': sys.argv[2]}}}))\n")
ENV = dict(os.environ, ANTIGRAVITY_LS_ADDRESS=f"127.0.0.1:{server.server_address[1]}", ANTIGRAVITY_CSRF_TOKEN="tok",
           AGP_DELEGATE_AGENTAPI=json.dumps([sys.executable, fake]), AGP_DELEGATE_POLL="0.3",
           AGP_DELEGATE_IDLE_GRACE="1", AGP_DELEGATE_NO_LAUNCH="1", AGP_DELEGATE_WATCH_POLL="0.3",
           AGP_DELEGATE_IDLE_PROBE="1")
def run(*args, cwd=ws, env=None, stdin=None):
    r = subprocess.run([sys.executable, DELEGATE, *args], cwd=cwd, env=dict(ENV, **(env or {})), input=stdin,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, r.stdout + r.stderr
def agentapi_calls():
    try:
        return [json.loads(l) for l in open(SENT, encoding="utf-8")]
    except OSError:
        return []
def record_of(cid):
    return json.load(open(os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", cid + ".json"), encoding="utf-8"))
def line_of(out, start):  # the first line of `out` that starts (after its indent) with `start`
    return next((ln for ln in out.splitlines() if ln.lstrip().startswith(start)), "")
def list_state(out, cid):  # a task's state in `delegate.py list`
    words = next((ln.split() for ln in out.splitlines() if ln.strip().startswith(cid[:8])), [])
    return words[1] if len(words) > 1 else ""
def price_in(line):  # «$2/$10 per 1M» in a line of `delegate.py models`, or ""
    m = re.search(r"\$[\d.]+/\$[\d.]+ per 1M", line)
    return m.group(0) if m else ""

# models: quota and tiers — fast on Gemini 4 Hydrogen (Low), standard on Hydrogen (Medium), strong on Neon (Medium) (the
# 2026-11-20 benchmark); the Claude tier falls back to Gemini while its quota is used up; speeds measured from the
# conversations' own timings
code, out = run("models")
check("models: quota per group and the tiers — Hydrogen (Low), Hydrogen (Medium), Neon (Medium)",
      code == 0 and "«Gemini Models»: week 90% left" in out
      and "fast      gemini-4-hydrogen-low" in out and "standard  gemini-4-hydrogen-medium" in out
      and line_of(out, "strong ").split()[1:6] == ["gemini-4-neon-medium", "Gemini", "4", "Neon", "(Medium)"]
      and "first choice of fast" in out and "first choice of strong" in out and "~0.005% of the weekly Gemini quota" in out, out)
check("models: ultra after strong — Gemini 4 Carbon at its recommended High effort",
      line_of(out, "ultra ").split()[1:6] == ["gemini-4-carbon-high", "Gemini", "4", "Carbon", "(High)"]
      and "first choice of ultra" in out and out.index("strong    ") < out.index("ultra     ") < out.index("claude    ")
      and "recommended High effort" in out, out)
check("models: the models in no tier are listed as Experimental, each with a word on when it fits",
      "Experimental — in no tier" in out and "gemini-4-neon-xhigh" in out.split("Experimental — in no tier")[1].split("All models")[0]
      and "the deepest Neon" in out and "gemini-4-hydrogen-medium " not in out.split("Experimental — in no tier")[1].split("All models")[0], out)
check("models: the Claude tier falls back to a Gemini model with quota left", "claude    gemini-4-neon-medium" in out
      and "Claude Sonnet 5.5 (High): quota used up — resets in 22 h → next with quota left" in out
      and "«Claude and GPT models»: used up — resets in 22 h → unavailable" in out, out)
check("models: speed measured from recent conversations",
      f"≈2.0 s per call ({2 * len([c for c in BASE if c not in (C9, CN)])} calls)" in line_of(out, "gemini-4-hydrogen-high "), out)
orch_line = line_of(out, "gemini-pro-orchestrator ")
check("models: Orchestrator's price is its planner's, its speed counts the Hydrogen calls that carry out the plan",
      "Orchestrator (Carbon)" in orch_line and "≈3.0 s per call (3 calls)" in orch_line
      and price_in(orch_line) == price_in(line_of(out, "gemini-4-carbon-high ")) != "", orch_line)
neon_line = line_of(out, "gemini-pro-orchestrator-neon ")
check("models: … the same for Orchestrator (Neon)", "Orchestrator (Neon)" in neon_line and "≈3.0 s per call (3 calls)" in neon_line
      and price_in(neon_line) == price_in(line_of(out, "gemini-4-neon-high ")) != "", neon_line)
check("models: Gemini 4 Neon (High) is Antigravity's default", "Antigravity's default" in line_of(out, "gemini-4-neon-high ")
      and "Antigravity's default" not in line_of(out, "gemini-4-carbon-medium "), out)
code, out = run("models", "--json")
check("models --json: the experimental models with their notes", list(json.loads(out)["experimental"]) == ["gemini-4-neon-xhigh"], out)
check("models --json", code == 0 and json.loads(out)["tiers"]["claude"] == "gemini-4-neon-medium"
      and json.loads(out)["states"]["Gemini Models"]["status"] == "ok" and json.loads(out)["default"] == "gemini-4-neon-high", out)
# strong by the 2026-11-20 benchmark: Neon (Medium), then Hydrogen (High), Neon (High), Orchestrator (Neon); Carbon and
# Orchestrator (Carbon) are in no tier
MODEL_LEFT["gemini-4-neon-medium"] = None
code, out = run("budget")
check("strong: Neon (Medium) used up → Gemini 4 Hydrogen (High)",
      line_of(out, "strong ").split()[1:6] == ["gemini-4-hydrogen-high", "Gemini", "4", "Hydrogen", "(High)"]
      and "Gemini 4 Neon (Medium): quota used up — resets in" in out and "→ next with quota left" in out, out)
MODEL_LEFT["gemini-4-hydrogen-high"] = None
code, out = run("models", "--json")
check("strong: then Gemini 4 Neon (High)", json.loads(out)["tiers"]["strong"] == "gemini-4-neon-high", out)
MODEL_LEFT["gemini-4-neon-high"] = None
code, out = run("models", "--json")
check("strong: then Orchestrator (Neon) — Carbon and Orchestrator (Carbon) belong to ultra",
      json.loads(out)["tiers"]["strong"] == "gemini-pro-orchestrator-neon", out)
MODEL_LEFT.clear()
MODEL_LEFT["gemini-4-carbon-high"] = None
code, out = run("models", "--json")
check("ultra: Carbon (High) used up → Orchestrator (Carbon)", json.loads(out)["tiers"]["ultra"] == "gemini-pro-orchestrator", out)
MODEL_LEFT["gemini-pro-orchestrator"] = None
code, out = run("models", "--json")
check("ultra: then Carbon (Medium)", json.loads(out)["tiers"]["ultra"] == "gemini-4-carbon-medium", out)
MODEL_LEFT.clear()

# run: a folder Antigravity doesn't know gets a project; the brief goes in with the run rules; the report
code, out = run("run", "--model", "standard", "--title", "Fix b", stdin="Goal: set b to 3.\nDone: tests pass.\n",
                env={"FAKE_CID": C1, "FAKE_TOUCH": os.path.join(ws, "generated.txt")})
projects = os.path.join(HOME, ".gemini", "config", "projects")
made = [json.load(open(os.path.join(projects, f), encoding="utf-8")) for f in os.listdir(projects)] if os.path.isdir(projects) else []
check("run: exit 0 and a start line with the model, the new project and why this model", code == 0 and
      f"Started Antigravity task {C1} — Gemini 4 Hydrogen (Medium), project «proj» (created for this folder)" in out
      and "Why this model: first choice of standard" in out, out)
check("run: the new Antigravity project points at the folder", len(made) == 1 and made[0]["name"] == "proj"
      and made[0]["projectResources"]["resources"][0]["folderUri"] == uri(ws), made)
call = (agentapi_calls() or [{}])[-1]
argv = call.get("argv") or []
check("run: agentapi new-conversation with the model id, the title and the project",
      argv[:3] == ["new-conversation", "--model=gemini-4-hydrogen-medium", "--title=Claude Code: Fix b"]
      and made and call.get("project") == made[0]["id"] and call.get("ls") == ENV["ANTIGRAVITY_LS_ADDRESS"], call)
prompt = argv[3] if len(argv) > 3 else ""
check("run: the prompt starts with the delegation mark and carries the rules, the folder and the brief",
      prompt.startswith("[Delegated from Claude Code]") and "No commits, pushes, deploys" in prompt
      and f"Working folder: {ws}" in prompt and prompt.rstrip().endswith("Task:\nGoal: set b to 3.\nDone: tests pass."), prompt)
check("report: finished, model and cost", "✅ Antigravity finished task " + C1 + " in 2 min" in out
      and "Model: Gemini 4 Hydrogen (Medium) · 2 model calls · 22k tokens read (20k from cache) · 400 written" in out
      and "weekly «Gemini Models» limit 90.0% → 90.0% left" in out, out)
check("report: changed files with +/−, no plan artifacts", "  M src.py  +1 −1" in out and "  A test_new.py  +2 −0" in out
      and "task.md" not in out, out)
check("report: files its commands changed (git)", "since it started (by its commands — or by you): generated.txt" in out, out)
check("report: commands with exit codes and outcome", "✓ python3 -m unittest -v → 0 · Ran 3 tests in 0.01s · OK" in out, out)
check("report: the agent's words, links into files cut to their names", "CHANGED: `src.py` — b = 3" in out
      and "file://" not in out and "check before you rely on it" in out, out)
record = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", C1 + ".json")
check("the task is recorded", os.path.isfile(record) and json.load(open(record, encoding="utf-8"))["state"] == "done")

# Orchestrator (Carbon): its quota group is found by the whole label, and the report says which model made the calls
code, out = run("run", "--model", "orchestrator", "--title", "Add d", stdin="Goal: d = 5.\n", env={"FAKE_CID": C9})
check("run --model orchestrator: Orchestrator (Carbon), its id to agentapi",
      code == 0 and f"Started Antigravity task {C9} — Orchestrator (Carbon)" in out
      and agentapi_calls()[-1]["argv"][1] == "--model=gemini-pro-orchestrator", out)
check("report: the calls split between the planner and Hydrogen, and the weekly Gemini limit",
      "Model: Orchestrator (Carbon) · 3 model calls (1 on Orchestrator (Carbon), 2 on Gemini 4 Hydrogen (Orchestrator))" in out
      and "weekly «Gemini Models» limit 90.0% → 90.0% left" in out, out)
# Orchestrator (Neon) just the same — and a task that finished before anyone waited: list, result and watch say done
code, out = run("run", "--model", "orchestrator neon", "--no-wait", "--title", "Add e", "Goal: e = 6.", env={"FAKE_CID": CN})
check("run --model \"orchestrator neon\": Orchestrator (Neon), its id to agentapi",
      code == 0 and f"Started Antigravity task {CN} — Orchestrator (Neon)" in out
      and agentapi_calls()[-1]["argv"][1] == "--model=gemini-pro-orchestrator-neon", out)
code, out = run("list")
check("list: a task that finished meanwhile is done (idle, nothing left in the background), and its record follows",
      code == 0 and list_state(out, CN) == "done" and record_of(CN)["state"] == "done", out)
code, out = run("result", CN[:8])
check("result: Orchestrator (Neon)'s calls split between the planner and Hydrogen, and the weekly Gemini limit",
      code == 0 and f"✅ Antigravity finished task {CN}" in out
      and "Model: Orchestrator (Neon) · 3 model calls (1 on Orchestrator (Neon), 2 on Gemini 4 Hydrogen (Orchestrator))" in out
      and "weekly «Gemini Models» limit 90.0% → 90.0% left" in out and "CHANGED: src.py — e = 6" in out, out)
code, out = run("watch", CN[:8], "--timeout", "5")
check("watch: … finished", code == 0 and "finished (STATUS: done)" in out, out)
code, out = run("run", "--model", "gemini-4-neon-xhigh", "--no-wait", "Tidy the docs.", env={"FAKE_CID": CX})
check("run --model gemini-4-neon-xhigh: that exact model", code == 0
      and f"Started Antigravity task {CX} — Gemini 4 Neon (XHigh)" in out and "Why this model: your choice" in out
      and agentapi_calls()[-1]["argv"][1] == "--model=gemini-4-neon-xhigh", out)

# an exhausted model gives way within its tier; an approval in Antigravity stops the wait with exit 2
code, out = run("run", "--model", "claude-sonnet-5-5-high", "Write the docs.", env={"FAKE_CID": C2})
check("run: an exhausted model gives way to the next of its tier", "Claude Sonnet 5.5 (High): quota used up" in out
      and "→ took Gemini 4 Neon (Medium) instead" in out and "(created for this folder)" not in out, out)
check("run: an approval it waits for → exit 2 with what and why", code == 2 and "is waiting for the user's approval" in out
      and "Waiting for: write_file /x/.git/hooks/pre-commit" in out and "Reason: 🛡️ guard says" in out
      and "Don't approve the agent's actions yourself" in out, out)
check("the same project is reused", len(os.listdir(projects)) == 1)
code, out = run("run", "--model", "fast", "--timeout", "1", "Long one.", env={"FAKE_CID": C3})
check("run: still running after --timeout → exit 3", code == 3 and "is still running after" in out
      and f"delegate.py wait {C3[:8]}" in out, out)
code, out = run("run", "--model", "fast", "--no-wait", "Quick.", env={"FAKE_CID": C3})
check("run --no-wait: returns at once with the id", code == 0 and f"delegate.py wait {C3[:8]}" in out, out)

# follow-up: same conversation, only the new turn's commands; diff; result; list; cancel
code, out = run("send", C1[:8], "Also set c to 4.")
call = agentapi_calls()[-1]
check("send: agentapi send-message to the conversation", call["argv"][:3] == ["send-message", "--title=Claude Code", C1]
      and call["argv"][3].startswith("Follow-up from Claude Code: Also set c to 4."), call)
check("send: the report covers the follow-up turn only", code == 0 and "CHECKS: pytest -q → 4 passed" in out
      and "✓ pytest -q → 0 · 4 passed in 0.2s" in out and "unittest" not in out, out)
code, out = run("diff", C1[:8], "--stat")
check("diff --stat", code == 0 and out.splitlines()[:2] == ["M src.py  +1 −1", "A test_new.py  +2 −0"], out)
code, out = run("diff", C1[:8], "./src.py")
check("diff <path>: a unified diff of that file", "--- a/src.py" in out and "-b = 2" in out and "+b = 3" in out
      and "test_new" not in out, out)
code, out = run("diff", C1[:8], "other.py")
check("diff: no match", "No file changes by this task match those paths." in out, out)
code, out = run("result", C1[:8], "--full")
check("result: the report again", code == 0 and "✅ Antigravity finished task" in out, out)
code, out = run("list")
check("list: the tasks, newest first", code == 0 and all(c[:8] in out for c in (C1, C2, C3)), out)
code, out = run("cancel", C3[:8])
check("cancel a running task", code == 0 and f"Stopped Antigravity task {C3}" in out, out)
code, out = run("cancel", C1[:8])
check("cancel an idle one", code == 0 and "wasn't running" in out, out)
code, out = run("send", C3[:8], "Also cover the empty input.", "--no-wait")  # idle until it picks the follow-up up
check("send to a stopped task: a follow-up", code == 0 and "Sent the follow-up" in out, out)
code, out = run("list")
check("list: idle, but it hasn't picked up Claude Code's message yet → running, not done", list_state(out, C3) == "running", out)
code, out = run("result", C3[:8])
check("result: … still running (exit 3)", code == 3 and f"Antigravity task {C3} is still running" in out
      and "finished" not in out, out)

# talking while it works — the agent's side: ask / tell only print a JSON line for Claude Code's side
code, out = run("ask", "Keep the old name?", cwd=SP)
line = next((l for l in out.splitlines() if l.startswith('{"to_claude_code"')), "{}")
check("ask: a JSON line with the question and an id, then what to do meanwhile", code == 0
      and json.loads(line).get("to_claude_code") == "question" and json.loads(line).get("text") == "Keep the old name?"
      and len(json.loads(line).get("id", "")) == 10 and "the answer wakes you up" in out and "Don't poll" in out, out)
code, out = run("tell", "The", "tests", "were", "red", "before", "me", cwd=SP)
check("tell: words without quotes become one message", code == 0 and '"text": "The tests were red before me"' in out
      and '"to_claude_code": "note"' in out, out)
code, out = run("ask", cwd=SP, stdin="")  # an empty stdin of its own: an inherited open pipe would keep it reading
check("ask: nothing to send → exit 1 with the form", code == 1 and 'ask "' in out and "to_claude_code" not in out, out)
code, out = run("ask", "Anyone?", cwd=SP, env={"ANTIGRAVITY_CONVERSATION_ID": "77777777-7777-4777-8777-777777777777"})
check("ask from a conversation Claude Code didn't start: nothing sent, decide yourself",
      code == 0 and "to_claude_code" not in out and "Decide yourself" in out, out)
code, out = run("tell", "Note.", cwd=SP, env={"ANTIGRAVITY_CONVERSATION_ID": C1})
check("tell from a delegated conversation goes out", code == 0 and '"to_claude_code": "note"' in out, out)
prompt = next(c["argv"][3] for c in agentapi_calls() if c["argv"][0] == "new-conversation")
check("the brief gives the agent both talk commands, quoted, and says Claude Code answers",
      f'"{os.path.abspath(DELEGATE)}" ask "<your question>"' in prompt and f'"{os.path.abspath(DELEGATE)}" tell "' in prompt
      and "Claude Code does, and it answers" in prompt and "Nobody watches" not in prompt, prompt[:1500])

# … and Claude Code's: a question stops the wait with exit 4; reply answers it and wakes the agent; the report has the talk
code, out = run("run", "--model", "fast", "Rename parse_date to parse_day.", env={"FAKE_CID": C4})
check("run: the agent asks → exit 4 with its question, what it told you, and how to answer", code == 4
      and "asks you (Claude Code) — it waits for your answer" in out and Q1 in out and N1 in out
      and f"delegate.py reply {C4[:8]}" in out and "its words, not the user's" in out, out)
code, out = run("list")
check("list: a task waiting for an answer", any(C4[:8] in l and "question" in l for l in out.splitlines()), out)
code, out = run("wait", C4[:8], "--timeout", "3")
check("wait: an open question returns at once", code == 4 and Q1 in out, out)
code, out = run("reply", C4[:8], "Yes,", "keep the alias for one release.")
call = agentapi_calls()[-1]
check("reply: answers the open question, wakes the idle agent", code == 0 and "Answered its question" in out
      and "wakes it up" in out and call["argv"][:3] == ["send-message", "--title=Claude Code", C4]
      and call["argv"][3].startswith(f"Claude Code answers your question «{Q1}»:\nYes, keep the alias for one release.")
      and "Go on with the task" in call["argv"][3], (out, call))
code, out = run("wait", C4[:8], "--timeout", "10")
check("wait after the answer: its report, with the talk, without the talk commands", code == 0
      and "✅ Antigravity finished task" in out and "Talk with you while it worked:" in out
      and f"it told you: {N1}" in out and f"it asked: {Q1}" in out and "you answered: Yes, keep the alias" in out
      and "NOTES: kept the alias" in out and "delegate.py\" ask" not in out and "Commands it ran" not in out, out)
code, out = run("watch", C4[:8], "--timeout", "5")
check("watch after wait: nothing told twice, only its end", code == 0 and N1 not in out and Q1 not in out
      and "finished (STATUS: done)" in out and f"delegate.py result {C4[:8]}" in out, out)

# a question from a command that went to the background, while the agent keeps working: watch (for Monitor)
SESSION = {"CLAUDE_CODE_SESSION_ID": "sess-test-1"}
code, out = run("run", "--model", "fast", "--no-wait", "Start the dev server.", env=dict(SESSION, FAKE_CID=C5))
check("run --no-wait: says how to follow it (watch with Monitor, or wait)", code == 0
      and f"delegate.py watch {C5[:8]}" in out and "Monitor" in out, out)
code, out = run("watch", "--timeout", "2", env=SESSION)
check("watch (no id: this session's running tasks): the question once, then how to go on", code == 3
      and out.count(Q2) == 1 and f"asks you" in out and f"delegate.py reply {C5[:8]}" in out
      and f"Still running: {C5[:8]}" in out and C4[:8] not in out, out)
code, out = run("watch", C5[:8], "--timeout", "1")
check("watch again: no repeat", code == 3 and Q2 not in out and "Still running" in out, out)
code, out = run("watch", "--timeout", "1", env={"CLAUDE_CODE_SESSION_ID": "another-session"})
check("watch in another session: not its tasks", code == 0 and "No running Antigravity tasks" in out, out)
code, out = run("wait", C5[:8], "--timeout", "3")
check("wait: the open question even though the watch showed it", code == 4 and Q2 in out, out)
code, out = run("reply", C5[:8], "Use 5173.")
call = agentapi_calls()[-1]
check("reply to a running agent: the answer, no wake-up note", code == 0 and "between its steps" in out
      and call["argv"][3] == f"Claude Code answers your question «{Q2}»:\nUse 5173.", call)
code, out = run("wait", C5[:8], "--timeout", "1")
check("wait after the answer: no question any more, still running → exit 3", code == 3
      and "waits for your answer" not in out and "you answered: Use 5173." in out, out)
code, out = run("send", C5[:8], "Also log the port.", "--no-wait")
call = agentapi_calls()[-1]
check("send to a running task: a message, not a new turn", code == 0 and "still working" in out
      and call["argv"][3] == "Claude Code says:\nAlso log the port.", (out, call))
rec5 = json.load(open(os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", C5 + ".json"), encoding="utf-8"))
check("the record keeps the talk and the session", rec5.get("claude_session") == "sess-test-1"
      and [m["kind"] for m in rec5.get("chat", [])] == ["question", "answer", "message"] and rec5.get("turn") == 0, rec5)

# seen live: the ask's step without its command line yet while the tell ran — the watch must not step over it
code, out = run("run", "--model", "fast", "--no-wait", "Rename calc.", env={"FAKE_CID": C7})
code, out = run("watch", C7[:8], "--timeout", "4")
check("watch: a command step not filled in yet is looked at again — the question isn't lost", code == 3
      and N7 in out and Q7 in out and "finished" not in out, out)
code, out = run("run", "--model", "fast", "--no-wait", "Rename parse_date.", env={"FAKE_CID": C8})
rec_path = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", C8 + ".json")
rec8 = json.load(open(rec_path, encoding="utf-8"))
rec8["heard"] = 99  # as a record from before the fix: its look already went past the question
json.dump(rec8, open(rec_path, "w", encoding="utf-8"))
code, out = run("result", C8[:8])
check("result: one last look over the whole turn finds a question the cursor went past", code == 4 and Q1 in out, out)
code, out = run("watch", C8[:8], "--timeout", "2")
check("watch: … and doesn't call such a task finished", "finished" not in out and code == 3, out)

# seen live: it ended its turn to wait for a long build it sent to the background — the summary says IDLE, but the
# build still runs (WaitForConversationFullyIdle blocks), and the agent goes on when it is over: not finished yet
SESSION2 = {"CLAUDE_CODE_SESSION_ID": "sess-test-2"}
code, out = run("run", "--model", "fast", "--no-wait", "Build the app.", env=dict(SESSION2, FAKE_CID=CB))
code, out = run("result", CB[:8])
check("result: idle while its background build runs → it ended its turn, wait for the rest", code == 0
      and f"Antigravity task {CB} ended its turn in 2 min; something it started still runs there" in out
      and "Antigravity finished task" not in out and WAITING in out
      and f"wait for the rest → delegate.py wait {CB[:8]}" in out, out)
check("result: … and its record says background", record_of(CB).get("state") == "background", record_of(CB))
code, out = run("list")
check("list: background, not done", code == 0 and list_state(out, CB) == "background", out)
code, out = run("list", env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9"})
check("list without the language server: the records answer", code == 0
      and list_state(out, CB) == "background", out)
code, out = run("watch", "--timeout", "2", env=SESSION2)
check("watch (this session's tasks, a background one among them): keeps watching while the build runs", code == 3
      and "finished" not in out and f"Still running: {CB[:8]}" in out, out)
code, out = run("wait", CB[:8], "--timeout", "20")
check("wait: «background» after its grace time, as before", code == 0
      and f"Antigravity task {CB} ended its turn" in out and record_of(CB).get("state") == "background", out)
background.discard(CB)  # the build is over: the agent got its result, went on and reported
code, out = run("list")
check("list: done once nothing it started runs any more, and the record follows", list_state(out, CB) == "done"
      and record_of(CB).get("state") == "done", out)
code, out = run("result", CB[:8])
check("result: finished, with the report it wrote after the build", code == 0
      and f"✅ Antigravity finished task {CB} in 2 min" in out and "BUILD SUCCEEDED" in out and WAITING not in out, out)
code, out = run("watch", CB[:8], "--timeout", "5")
check("watch: finished", code == 0 and "finished (STATUS: done)" in out, out)

# errors
code, out = run("run", "--model", "gpt-9", "x", env={"FAKE_CID": C1})
check("unknown model → exit 1 with the choices", code == 1 and "unknown model 'gpt-9'" in out and "gemini-4-carbon-high" in out, out)
code, out = run("run", "--model", "fast", "--cwd", HOME, "x", env={"FAKE_CID": C1})
check("the home folder is never made a project", code == 1 and "home folder" in out, out)
code, out = run("run", "--model", "fast", stdin="  ", env={"FAKE_CID": C1})
check("an empty brief is refused", code == 1 and "empty" in out, out)
code, out = run("models", env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9"})
check("Antigravity not reachable → exit 1, one line, no traceback", code == 1 and "can't reach Antigravity" in out
      and "Traceback" not in out, out)
code, out = run("wait", "nothing-like-this")
check("an unknown task id", code == 1 and "no delegated task" in out, out)

# budget: Claude Code's own limits — unknown, given, from the statusLine — and what to do about them
code, out = run("budget")
check("budget: own limits unknown → how to get them", code == 0 and "Claude Code, your own plan: unknown" in out
      and "get_usage" in out and "«Gemini Models»: week 90% left" in out and "on pace → fine" in out
      and "Antigravity's quota is fine: hand over self-contained work, light tasks included" in out, out)
code, out = run("budget", "--claude-5h", "81", "--claude-week", f"40@{iso(5 * 86400 + 1800)}")
check("budget: own 5-hour limit at 81% → hand over more", code == 0 and "5 h 81% used" in out and "week 40% used (resets in 5 days" in out
      and "→ getting tight" in out and "lean on delegation more than usual" in out, out)
code, out = run("budget", "--claude-week", f"60@{iso(5 * 86400)}")
check("budget: own week spent ahead of its pace → getting tight", "ahead of the week's pace (29% of it has passed)" in out
      and "→ getting tight" in out, out)
code, out = run("budget", "--claude-5h", "95%")
check("budget: own limit almost used up", "→ almost used up" in out and "hand over everything you can describe" in out, out)
sl = json.dumps({"model": {"display_name": "Opus 5.5"}, "rate_limits": {
    "five_hour": {"used_percentage": 22.4, "resets_at": time.time() + 4 * 3600 + 600},
    "seven_day": {"used_percentage": 44, "resets_at": time.time() + 3 * 86400}}})
code, out = run("statusline", stdin=sl)
check("statusline: prints the limits and remembers them", code == 0 and out.strip() == "Opus 5.5 · Claude used: 5h 22% · week 44%"
      and os.path.isfile(os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", "claude-limits.json")), out)
code, out = run("statusline", stdin="not json")
check("statusline: survives garbage", code == 0 and "Traceback" not in out, out)
code, out = run("budget")
check("budget: own limits from the statusLine record", "5 h 22% used (resets in 4 h" in out and "(from the statusLine)" in out
      and "→ fine" in out, out)

# the limits decide within a tier: Gemini spent faster than the week lasts, Claude's quota resets in 20 h unused
QUOTA.update(gemini=[0.3, 5 * 86400 + 1800], claude=[0.6, 20 * 3600 + 600])
code, out = run("budget")
check("budget: a quota spent ahead of pace is tight, one about to reset unused is to spend",
      "«Gemini Models»: week 30% left (resets in 5 days" in out and "spending faster than the week lasts → tight — save it" in out
      and "«Claude and GPT models»: week 60% left (resets in 20 h)" in out and "→ quota to spare — use it" in out, out)
check("budget: fast stays on Hydrogen — every quota it draws on is tight",
      "fast      gemini-4-hydrogen-low" in out and "Every quota this tier draws on is tight" in out, out)
check("budget: standard and strong take the quota that would reset unused",
      "standard  claude-sonnet-5-5-medium" in out and "instead of Gemini 4 Hydrogen (Medium): «Claude and GPT models»" in out
      and "strong    claude-sonnet-5-5-high" in out and "instead of Gemini 4 Neon (Medium)" in out, out)
check("budget: ultra keeps Carbon although the Claude quota would reset unused — it names the model the task needs",
      "ultra     gemini-4-carbon-high" in out, out)
check("budget: advice per group", "«Claude and GPT models» has quota to spare" in out
      and "«Gemini Models» is tight" in out, out)
code, out = run("run", "--model", "strong", "Port the parser.", env={"FAKE_CID": C1})
check("run: strong goes to the Claude quota that would reset unused", agentapi_calls()[-1]["argv"][1] == "--model=claude-sonnet-5-5-high"
      and "Why this model: instead of Gemini 4 Neon (Medium)" in out, out)
code, out = run("run", "--model", "gemini-4-carbon-high", "Port the parser.", env={"FAKE_CID": C1})
check("run: an exact model is kept, with a word about its tight quota", agentapi_calls()[-1]["argv"][1] == "--model=gemini-4-carbon-high"
      and "Why this model: your choice; «Gemini Models»" in out and "a lower tier would save it" in out, out)
QUOTA.update(gemini=[None, 3 * 86400], claude=[None, 20 * 3600])
code, out = run("run", "--model", "fast", "Anything.", env={"FAKE_CID": C1})
check("run: nothing has quota left → exit 1, no task started", code == 1 and "no model with quota left for 'fast'" in out
      and "Started" not in out, out)
code, out = run("budget")
check("budget: everything used up → do it yourself", "Antigravity has no quota left" in out and "do the work yourself" in out
      and "fast      nothing has quota left now" in out, out)
QUOTA.update(gemini=[0.9, 6 * 86400], claude=[None, 22 * 3600])

# the plugin's hooks: a delegated conversation gets no setup, review, CI or memory nudges
sys.path.insert(0, HOOKS)
import conversation as conv
sys.path.insert(0, os.path.dirname(DELEGATE))
import delegate
check("the mark is the same on both sides", delegate.MARK == conv.DELEGATED_MARK)

# Gemini 4 Neon: its quota group by family, Orchestrator (Neon)'s by its whole label; the picks when Antigravity has
# renamed its models; a few words name the right model; a price change applies from its day on
LIVE = delegate.LS(ENV["ANTIGRAVITY_LS_ADDRESS"], "tok")
groups = delegate.quota(LIVE)
gname = lambda label: (delegate.group_of(label, groups) or {}).get("name")
check("group_of: Gemini 4 Neon and Orchestrator (Neon) draw on «Gemini Models», nothing near them does by mistake",
      gname("Gemini 4 Neon (High)") == gname("Gemini 4 Neon (XHigh)") == gname("Orchestrator (Neon)") == "Gemini Models"
      and gname("Orchestrator (Carbon)") == "Gemini Models" and gname("Claude Sonnet 5.5 (High)") == "Claude and GPT models"
      and gname("Orchestrator (Xenon)") is None and gname("Gemini 4 Neonite (High)") is None
      and gname("Orchestrator") is None, groups)
renamed = [{"id": f"m-{i}", "label": label, "left": 0.5, "reset": "", "enum": "", "tag": "", "note": ""} for i, label in
           enumerate(["Gemini 4 Neon (XHigh)", "Gemini 4 Neon (High)", "Gemini 4 Carbon (High)", "Orchestrator (Neon)",
                      "Gemini 4 Hydrogen (High)", "Orchestrator (Carbon)", "Gemini 4 Neon (Medium)",
                      "Gemini 4 Hydrogen (Medium)", "Gemini 4 Hydrogen (Low)"])]
got = [m["label"] for m in delegate.generic("strong", renamed, "")]
check("generic (ids renamed): strong takes Neon (Medium), Hydrogen (High), Neon (High), Orchestrator (Neon), then the "
      "other (High)", got == ["Gemini 4 Neon (Medium)", "Gemini 4 Hydrogen (High)", "Gemini 4 Neon (High)",
                              "Orchestrator (Neon)", "Gemini 4 Carbon (High)"], got)
fresh = [{"id": "gemini-9-test", "label": "Gemini 9 Test (High)", "note": "A model added later"},
         {"id": "gemini-4-hydrogen-low", "label": "Gemini 4 Hydrogen (Low)", "note": ""}]
check("experimental: a model Antigravity adds later shows up with Antigravity's own description; tier models don't",
      [(m["id"], n) for m, n in delegate.experimental(fresh)] == [("gemini-9-test", "A model added later")])
got = [m["label"] for m in delegate.generic("ultra", renamed, "")][:3]
check("generic (ids renamed): ultra takes Carbon (High), Orchestrator (Carbon), then Neon (High)",
      got == ["Gemini 4 Carbon (High)", "Orchestrator (Carbon)", "Gemini 4 Neon (High)"], got)
got = [m["label"] for m in delegate.generic("standard", renamed, "")][:3]
check("generic (ids renamed): standard takes Hydrogen (Medium), Hydrogen (High), Neon (Medium)",
      got == ["Gemini 4 Hydrogen (Medium)", "Gemini 4 Hydrogen (High)", "Gemini 4 Neon (Medium)"], got)
ms, default = delegate.models(LIVE)
pick_id = lambda words, order=ms: (delegate.fuzzy(words, order) or {}).get("id")
check("a few words: «orchestrator» is Orchestrator (Carbon) though Antigravity lists Orchestrator (Neon) first; "
      "«neon high» isn't XHigh in either order", pick_id("orchestrator") == "gemini-pro-orchestrator"
      and pick_id("orchestrator neon") == "gemini-pro-orchestrator-neon" and pick_id("neon high") == "gemini-4-neon-high"
      and pick_id("neon high", ms[::-1]) == "gemini-4-neon-high" and pick_id("neon xhigh", ms[::-1]) == "gemini-4-neon-xhigh"
      and pick_id("carbon high") == "gemini-4-carbon-high" and pick_id("xenon") is None and default == "gemini-4-neon-high")
table = [{"match": "carbon", "input": 2, "output": 10, "price_changes": [{"from": "2027-01-01", "input": 5, "output": 25},
                                                                          {"from": "2026-12-01", "input": 4, "output": 20}]}]
got = [delegate.price("Gemini 4 Carbon (High)", table, day) for day in ("2026-11-30", "2026-12-01", "2027-02-01")]
check("price: the price changes dated on or before the day apply, in date order",
      got == ["$2/$10 per 1M", "$4/$20 per 1M", "$5/$25 per 1M"] and delegate.price("Gemini 4 Neon (High)", table) == "", got)
brief = delegate.compose("Goal: x", ws, ws)
check("delegated(): the bridge's brief", conv.delegated({"users": [{"text": brief}]}))
check("delegated(): a person who wrote into it took it over",
      not conv.delegated({"users": [{"text": brief}, {"text": "сделай ещё вот что"}]}))
check("delegated(): from lastUserInput before the transcript has it",
      conv.delegated(None, {"lastUserInput": "<USER_REQUEST>\n" + brief + "\n</USER_REQUEST>"}))
check("delegated(): an ordinary conversation", not conv.delegated({"users": [{"text": "Сделай фичу"}]}))

cid, art = "agp-delegtest-0001", os.path.join(SP, "dl_art")
tp = os.path.join(art, "transcript_full.jsonl")
def transcript(folder, first, steps, answer_text="Готово.", files=None, repo=False):
    for d in (folder, art):
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    for name, text in (files or {}).items():
        open(os.path.join(folder, name), "w", encoding="utf-8").write(text)
    if repo:
        subprocess.run(["git", "init", "-q"], cwd=folder)
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-14T10:00:00Z",
             "content": f"<USER_REQUEST>\n{first}\n</USER_REQUEST>"}]
    for i, (name, args) in enumerate(steps, 1):
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": name, "args": args}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": answer_text})
    open(tp, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def hook(name, folder, **extra):
    p = dict({"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [folder],
              "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}, **extra)
    r = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(p), capture_output=True,
                       text=True, encoding="utf-8")
    return json.loads(r.stdout or "{}")
reason = lambda d: d.get("reason") if d.get("decision") == "continue" else None
hw = os.path.join(SP, "hook_ws")
edits = lambda n: [("write_file", {"Path": os.path.join(hw, f"m{i}.py"), "Content": "x = 1\n"}) for i in range(n)]
test_run = ("run_command", {"CommandLine": "pytest -q"})
transcript(hw, "Сделай фичу", edits(1) + [test_run])
got = reason(hook("stop.py", hw))
check("Stop, ordinary conversation without git: sets up the project", bool(got) and "create the git repository" in got, got)
transcript(hw, brief, edits(1) + [test_run])
check("Stop, delegated: no project setup", reason(hook("stop.py", hw)) is None, reason(hook("stop.py", hw)))
transcript(hw, "Сделай фичу", edits(3) + [test_run], files={"AGENTS.md": "# r\n"}, repo=True)
got = reason(hook("stop.py", hw))
check("Stop, ordinary conversation, 3 code files: /review", bool(got) and "/review" in got, got)
transcript(hw, brief, edits(3) + [test_run], files={"AGENTS.md": "# r\n"}, repo=True)
check("Stop, delegated, 3 code files: no /review (Claude Code checks the result)", reason(hook("stop.py", hw)) is None)
transcript(hw, brief, edits(1))
got = reason(hook("stop.py", hw))
check("Stop, delegated, unchecked change: still sent back to check it", bool(got) and "ran no check" in got, got)
transcript(hw, "Сделай фичу", [], files={"main.py": "print(1)\n"})
msg = json.dumps(hook("session_start.py", hw, lastUserInput="Сделай фичу"), ensure_ascii=False)
check("SessionStart, ordinary conversation without git: setup hint", "No git repository yet" in msg, msg[-400:])
transcript(hw, brief, [], files={"main.py": "print(1)\n"})
msg = json.dumps(hook("session_start.py", hw, lastUserInput=brief), ensure_ascii=False)
check("SessionStart, delegated: no setup hint, the rest is there", "No git repository yet" not in msg
      and "antigravity_plus_session_context" in msg, msg[-400:])

# it asked Claude Code and ended its turn to wait: no nudges until the answer has come
ask_call = ("run_command", {"CommandLine": talk_line("ask", "Which name?")})
transcript(hw, brief, edits(1) + [ask_call], answer_text="Жду ответа Claude Code.")
check("Stop, delegated, waiting for Claude Code's answer: left alone", reason(hook("stop.py", hw)) is None,
      reason(hook("stop.py", hw)))
said = "sender=system priority=MESSAGE_PRIORITY_HIGH content=" + delegate.ANSWER.format(question="Which name?", message="parse_day")
with open(tp, "a", encoding="utf-8") as f:
    f.write(json.dumps({"step_index": 10, "type": "SYSTEM_MESSAGE", "content": "[Message] timestamp=2026-11-15T10:00:00Z " + said}) + "\n")
    f.write(json.dumps({"step_index": 11, "type": "PLANNER_RESPONSE", "content": "Готово."}) + "\n")
got = reason(hook("stop.py", hw))
check("Stop, delegated, after the answer: the usual checks again", bool(got) and "ran no check" in got, got)
for text in (delegate.ANSWER.format(question="q", message="a"), delegate.MESSAGE.format(message="m"),
             delegate.FOLLOW_UP.format(message="f")):
    check(f"the hooks know Claude Code's message «{text[:28]}…»",
          conv.CLAUDE_SAYS.search(("[Message] sender=system priority=MESSAGE_PRIORITY_HIGH content=" + text).encode()))
check("…but not a background task's output that quotes it", not conv.CLAUDE_SAYS.search(
    b'[Message] sender=x/task-3 priority=MESSAGE_PRIORITY_HIGH content=Task finished: Claude Code says: hi'))

# the guard lets the talk commands through without asking — only the installed script, only as one plain command
import guard
os.makedirs(os.path.dirname(INSTALLED), exist_ok=True)
shutil.copy(DELEGATE, INSTALLED)
for cmd, want in [(talk_line("ask", "Keep the name?"), True), (talk_line("tell", "a; b && c | d > e"), True),
                  (f'py -3 "{INSTALLED}" tell "x"', True), (f'python -X utf8 "{INSTALLED}" ask "x"', True),
                  (f'python3 ~/.claude/skills/antigravity/scripts/delegate.py ask "x"', True),
                  (talk_line("ask", "x") + "; rm -rf ~", False), (talk_line("ask", "x") + "\nrm -rf ~", False),
                  (talk_line("ask", "x") + " # ; rm -rf ~", False), (talk_line("ask", "$(rm -rf ~)"), False),
                  (talk_line("ask", "`id`"), False), (talk_line("ask", "x") + " > ~/.zshrc", False),
                  (talk_line("ask", "x") + " | sh", False), (talk_line("ask", "x") + " &", False),
                  (f'python3 "{os.path.join(ws, "delegate.py")}" ask "x"', False), (talk_line("run", "x"), False),
                  (f'bash -c \'python3 "{INSTALLED}" ask x\'', False), (f'FOO=1 python3 "{INSTALLED}" ask "x"', False)]:
    check(f"guard: {'lets through' if want else 'leaves to the usual policy'}: {cmd.replace(HOME, '~')[-60:]!r}",
          guard.talks_to_claude(cmd) is want)
r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], capture_output=True, text=True, encoding="utf-8",
                   input=json.dumps({"conversationId": cid, "transcriptPath": tp, "workspacePaths": [hw],
                                     "toolCall": {"name": "run_command", "args": {"CommandLine": talk_line("ask", "Which name?")}}}))
check("guard (hook): auto_approve for the talk command", json.loads(r.stdout or "{}").get("decision") == "auto_approve", r.stdout)

# the usage panel: the real model of a conversation started through agentapi, and the task's title
def brain(conv_id, rows):
    logs = os.path.join(HOME, ".gemini", "antigravity", "brain", conv_id, ".system_generated", "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
def usage_json(conv_id, env=None):
    r = subprocess.run([sys.executable, USAGE, "--conversation", conv_id, "--json"], capture_output=True, text=True,
                       encoding="utf-8", env=dict({k: v for k, v in os.environ.items() if not k.startswith("ANTIGRAVITY_")},
                                                  **(env or {})))
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": r.stdout + r.stderr}
call_row = lambda i, out=300: {"step_index": i, "type": "PLANNER_RESPONSE", "created_at": "2026-11-15T10:00:%02dZ" % i,
                               "input_tokens": 5000, "cache_read_tokens": 20000, "output_tokens": out, "content": ""}
rec1 = json.load(open(record, encoding="utf-8"))
brain(C1, [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-15T10:00:00Z",
            "content": "<USER_REQUEST>\n" + delegate.compose("Goal: port the parser.", ws, ws) + "\n</USER_REQUEST>"},
           call_row(1), {"step_index": 2, "type": "SYSTEM_MESSAGE", "created_at": "2026-11-15T10:00:03Z", "content":
                         "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-15T10:00:03Z sender=system priority=MESSAGE_PRIORITY_HIGH "
                         "content=" + delegate.FOLLOW_UP.format(message="Also set c to 4.") + "\n</SYSTEM_MESSAGE>"},
           call_row(3)])
d = usage_json(C1)
PRICES = json.load(open(os.path.join(PLUGIN, "skills", "usage", "pricing.json"), encoding="utf-8"))
window = lambda mid: next((m.get("context_window") for m in PRICES["models"] if m["id"] == mid), None)
check("usage: a delegated task gets the model it ran on (from the bridge's record), not the default",
      d.get("models") == [rec1["label"]] and d.get("model_guessed_calls") == 0 and rec1["label"] == "Gemini 4 Carbon (High)"
      and (d.get("context") or {}).get("model") == rec1["label"]
      and (d.get("context") or {}).get("window") == window("gemini-4-carbon") != window(PRICES["default_model"]), d)
check("usage: its title is the task, not the run rules; a follow-up is a turn of its own",
      d.get("title") == rec1["title"] and [t["text"] for t in d.get("turns", [])] == [rec1["title"], "Claude Code: Also set c to 4."], d)
brain(C6, [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-15T10:00:00Z",
            "content": "<USER_REQUEST>\nSummarize the logs.\n</USER_REQUEST>"}, call_row(1)])
d = usage_json(C6)
check("usage: no record and no way to ask Antigravity → priced as the default model, and it says so",
      d.get("model_guessed_calls") == 1, d)
d = usage_json(C6, {"ANTIGRAVITY_LS_ADDRESS": ENV["ANTIGRAVITY_LS_ADDRESS"], "ANTIGRAVITY_CSRF_TOKEN": "tok"})
check("usage (side panel, which can ask Antigravity): the model from Antigravity's own metadata",
      d.get("models") == ["Gemini 4 Argon (High)"] and d.get("model_guessed_calls") == 0, d)
d = usage_json(C6)
check("usage: … remembered for the next time", d.get("models") == ["Gemini 4 Argon (High)"], d)
# Orchestrator: Antigravity's metadata names each call's model — the panel prices Hydrogen's calls as Hydrogen
brain(C9, [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-15T10:00:00Z",
            "content": "<USER_REQUEST>\n" + delegate.compose("Goal: d = 5.", ws, ws) + "\n</USER_REQUEST>"},
           call_row(1, 5000), {"step_index": 2, "type": "SYSTEM_MESSAGE", "created_at": "2026-11-15T10:00:02Z",
                               "content": "<SYSTEM_MESSAGE>\n(the plan, in other words)\n</SYSTEM_MESSAGE>"},
           call_row(3), call_row(4)])
d = usage_json(C9)
check("usage: Orchestrator with nothing to tell its calls apart → all at the planner's price, none unpriced",
      d.get("models") == ["Orchestrator (Carbon)"] and not d.get("unpriced_models") and d.get("model_guessed_calls") == 0, d)
d = usage_json(C9, {"ANTIGRAVITY_LS_ADDRESS": ENV["ANTIGRAVITY_LS_ADDRESS"], "ANTIGRAVITY_CSRF_TOKEN": "tok"})
check("usage (side panel): Orchestrator's calls by the model Antigravity names for each",
      [(c.get("step"), c.get("model")) for c in d.get("calls") or []]
      == [(1, "Orchestrator (Carbon)"), (3, "Gemini 4 Hydrogen (Orchestrator)"), (4, "Gemini 4 Hydrogen (Orchestrator)")]
      and d.get("models") == ["Orchestrator (Carbon)", "Gemini 4 Hydrogen (Orchestrator)"] and not d.get("unpriced_models"), d)
r = subprocess.run([sys.executable, USAGE, "--recent", "5"], capture_output=True, text=True, encoding="utf-8")
recent = json.loads(r.stdout or "{}")
check("usage --recent: the panel's list and per-model totals carry the real models",
      {c["id"]: c["title"] for c in recent.get("conversations", [])}.get(C1) == rec1["title"]
      and {m["model"] for m in recent.get("by_model", [])} >= {rec1["label"], "Gemini 4 Argon (High)"}
      and not any(m.get("guessed") for m in recent.get("by_model", [])), recent)

server.shutdown()
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
