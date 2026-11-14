"""Claude Code → Antigravity bridge (claude-code/skills/antigravity/scripts/delegate.py) against a fake language
server and a fake agentapi: tiers and the fallback when a quota is used up, the choice by the limits left (a quota
spent faster than the week lasts, one about to reset unused, everything used up), Claude Code's own limits (given,
from the statusLine, unknown), measured speed, a new Antigravity project for an unknown folder, the report (changed
files, commands, cost, the agent's words), an approval the task waits for, a timeout, a follow-up, diff, cancel,
list, errors — and how the plugin's hooks treat a delegated conversation."""
import http.server, json, os, pathlib, shutil, subprocess, sys, threading, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
DELEGATE = os.path.join(ROOT, "claude-code", "skills", "antigravity", "scripts", "delegate.py")
HOOKS = os.path.join(PLUGIN, "hooks")
HOME = os.path.expanduser("~")
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
C1, C2, C3 = (c * 8 + "-aaaa-4bbb-8ccc-" + c * 12 for c in "abc")
SENT = os.path.join(SP, "agentapi-calls.jsonl")

def step(kind, **kw):
    return dict({"type": "CORTEX_STEP_TYPE_" + kind, "status": "CORTEX_STEP_STATUS_DONE"}, **kw)
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
}
FOLLOW = [step("SYSTEM_MESSAGE"), step("RUN_COMMAND", runCommand={"commandLine": "pytest -q", "exitCode": 0, "combinedOutput": {
    "full": "Command exited with code 0.\n\nCommand output:\n4 passed in 0.2s\n"}}),
    step("PLANNER_RESPONSE", plannerResponse={"response": "STATUS: done\nCHANGED: src.py — c = 4\nCHECKS: pytest -q → 4 passed\nNOTES: none"})]
cancelled = set()

def followed_up():
    try:
        return any(json.loads(l)["argv"][:1] == ["send-message"] for l in open(SENT, encoding="utf-8"))
    except OSError:
        return False
def steps_of(cid):
    return BASE.get(cid, []) + (FOLLOW if cid == C1 and followed_up() else [])
def status_of(cid):
    return "CASCADE_RUN_STATUS_IDLE" if cid == C1 or cid in cancelled else "CASCADE_RUN_STATUS_RUNNING"

iso = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs))
# what the fake plan has left: weekly fraction (None = used up) and seconds until the week resets, per group
QUOTA = {"gemini": [0.9, 6 * 86400], "claude": [None, 22 * 3600]}
MODELS = [("gemini-4-carbon-medium", "Gemini 4 Carbon (Medium)", "M437"), ("gemini-4-carbon-high", "Gemini 4 Carbon (High)", "M427"),
          ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)", "M527"), ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)", "M526"),
          ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)", "M525"), ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)", "M319"),
          ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)", "M404"), ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)", "M405")]

def answer(method, body):
    if method == "GetCascadeModelConfigData":
        def cfg(mid, label, enum):
            left, secs = QUOTA["claude" if mid.startswith("claude") else "gemini"]
            q = {"resetTime": iso(secs)}
            if left:  # proto3 JSON leaves a 0 out: no remainingFraction means used up
                q["remainingFraction"] = left
            return {"modelId": mid, "label": label, "modelOrAlias": {"model": enum}, "quotaInfo": q}
        return {"clientModelConfigs": [cfg(*m) for m in MODELS], "defaultOverrideModelConfig": {"modelOrAlias": {"model": "M437"}}}
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
            {"displayName": "Gemini Models", "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen, Gemini 3.8 Flash",
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
        if status_of(body.get("conversationId")) != "CASCADE_RUN_STATUS_IDLE":
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
           AGP_DELEGATE_IDLE_GRACE="1", AGP_DELEGATE_NO_LAUNCH="1")
def run(*args, cwd=ws, env=None, stdin=None):
    r = subprocess.run([sys.executable, DELEGATE, *args], cwd=cwd, env=dict(ENV, **(env or {})), input=stdin,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, r.stdout + r.stderr
def agentapi_calls():
    try:
        return [json.loads(l) for l in open(SENT, encoding="utf-8")]
    except OSError:
        return []

# models: quota and tiers — fast and standard on Gemini 4 Hydrogen; the Claude tier falls back to Gemini while its
# quota is used up; speeds measured from the conversations' own timings
code, out = run("models")
check("models: quota per group and the tiers", code == 0 and "«Gemini Models»: week 90% left" in out
      and "fast      gemini-4-hydrogen-low" in out and "standard  gemini-4-hydrogen-high" in out
      and "strong    gemini-4-carbon-high" in out and "first choice of fast" in out, out)
check("models: the Claude tier falls back to a Gemini model with quota left", "claude    gemini-4-carbon-high" in out
      and "Claude Sonnet 5.5 (High): quota used up — resets in 22 h → next with quota left" in out
      and "«Claude and GPT models»: used up — resets in 22 h → unavailable" in out, out)
check("models: speed measured from recent conversations", "Gemini 4 Hydrogen (High) · ≈2.0 s per call (6 calls)" in out, out)
code, out = run("models", "--json")
check("models --json", code == 0 and json.loads(out)["tiers"]["claude"] == "gemini-4-carbon-high"
      and json.loads(out)["states"]["Gemini Models"]["status"] == "ok", out)

# run: a folder Antigravity doesn't know gets a project; the brief goes in with the run rules; the report
code, out = run("run", "--model", "standard", "--title", "Fix b", stdin="Goal: set b to 3.\nDone: tests pass.\n",
                env={"FAKE_CID": C1, "FAKE_TOUCH": os.path.join(ws, "generated.txt")})
projects = os.path.join(HOME, ".gemini", "config", "projects")
made = [json.load(open(os.path.join(projects, f), encoding="utf-8")) for f in os.listdir(projects)] if os.path.isdir(projects) else []
check("run: exit 0 and a start line with the model, the new project and why this model", code == 0 and
      f"Started Antigravity task {C1} — Gemini 4 Hydrogen (High), project «proj» (created for this folder)" in out
      and "Why this model: first choice of standard" in out, out)
check("run: the new Antigravity project points at the folder", len(made) == 1 and made[0]["name"] == "proj"
      and made[0]["projectResources"]["resources"][0]["folderUri"] == uri(ws), made)
call = (agentapi_calls() or [{}])[-1]
argv = call.get("argv") or []
check("run: agentapi new-conversation with the model id, the title and the project",
      argv[:3] == ["new-conversation", "--model=gemini-4-hydrogen-high", "--title=Claude Code: Fix b"]
      and made and call.get("project") == made[0]["id"] and call.get("ls") == ENV["ANTIGRAVITY_LS_ADDRESS"], call)
prompt = argv[3] if len(argv) > 3 else ""
check("run: the prompt starts with the delegation mark and carries the rules, the folder and the brief",
      prompt.startswith("[Delegated from Claude Code]") and "No commits, pushes, deploys" in prompt
      and f"Working folder: {ws}" in prompt and prompt.rstrip().endswith("Task:\nGoal: set b to 3.\nDone: tests pass."), prompt)
check("report: finished, model and cost", "✅ Antigravity finished task " + C1 + " in 2 min" in out
      and "Model: Gemini 4 Hydrogen (High) · 2 model calls · 22k tokens read (20k from cache) · 400 written" in out
      and "weekly «Gemini Models» limit 90.0% → 90.0% left" in out, out)
check("report: changed files with +/−, no plan artifacts", "  M src.py  +1 −1" in out and "  A test_new.py  +2 −0" in out
      and "task.md" not in out, out)
check("report: files its commands changed (git)", "since it started (by its commands — or by you): generated.txt" in out, out)
check("report: commands with exit codes and outcome", "✓ python3 -m unittest -v → 0 · Ran 3 tests in 0.01s · OK" in out, out)
check("report: the agent's words, links into files cut to their names", "CHANGED: `src.py` — b = 3" in out
      and "file://" not in out and "check before you rely on it" in out, out)
record = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate", C1 + ".json")
check("the task is recorded", os.path.isfile(record) and json.load(open(record, encoding="utf-8"))["state"] == "done")

# an exhausted model gives way within its tier; an approval in Antigravity stops the wait with exit 2
code, out = run("run", "--model", "claude-sonnet-5-5-high", "Write the docs.", env={"FAKE_CID": C2})
check("run: an exhausted model gives way to the next of its tier", "Claude Sonnet 5.5 (High): quota used up" in out
      and "→ took Gemini 4 Carbon (High) instead" in out and "(created for this folder)" not in out, out)
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
check("send: agentapi send-message to the conversation", call["argv"][:2] == ["send-message", C1]
      and call["argv"][2].startswith("Follow-up from Claude Code: Also set c to 4."), call)
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
      "standard  claude-sonnet-5-5-medium" in out and "instead of Gemini 4 Hydrogen (High): «Claude and GPT models»" in out
      and "strong    claude-sonnet-5-5-high" in out and "instead of Gemini 4 Carbon (High)" in out, out)
check("budget: advice per group", "«Claude and GPT models» has quota to spare" in out
      and "«Gemini Models» is tight" in out, out)
code, out = run("run", "--model", "strong", "Port the parser.", env={"FAKE_CID": C1})
check("run: strong goes to the Claude quota that would reset unused", agentapi_calls()[-1]["argv"][1] == "--model=claude-sonnet-5-5-high"
      and "Why this model: instead of Gemini 4 Carbon (High)" in out, out)
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

server.shutdown()
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
