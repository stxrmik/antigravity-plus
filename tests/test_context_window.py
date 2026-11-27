"""The context windows Antigravity really gives its models (hooks/context_window.py) and everything that follows them.

Antigravity sets maxContextTokens on its servers and changes it without a release (Gemini 4: 256k → 512k on
2026-11-16), so the plugin reads it from each recent call's generator metadata instead of trusting the numbers it
shipped with: the context hints (hooks/speed.py), /usage and the side panel (usage.py), /context. A fake language
server answers GetCascadeTrajectoryGeneratorMetadata the way Antigravity 2.35.1 does — `generatorMetadataOffset`
skips calls, an offset past the last call gives {} — and reports a 600k window that no shipped table knows.
"""
import json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
USAGE = os.path.join(PLUGIN, "skills", "usage", "scripts", "usage.py")
CONTEXT = os.path.join(PLUGIN, "skills", "context", "scripts", "context.py")
for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "AGP_WINDOW_CHECK", "AGP_WINDOW_CACHE", "AGP_USAGE_NO_LS",
            "AGY_CLI_AUTO_COMPACT_OVERRIDE"):
    os.environ.pop(var, None)
sys.path.insert(0, HOOKS)
import client  # noqa: E402
import compat  # noqa: E402
import context_window as cw  # noqa: E402
import speed  # noqa: E402

CACHE = cw.cache_path()
ok = True


def check(name, cond, got=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:600]}"))


def iso(ago=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def reset():
    if os.path.exists(CACHE):
        os.remove(CACHE)


# ─────────────────────────────── 1. names and times ───────────────────────────────
names = {
    "Gemini 4 Neon (High)": "gemini-4-neon", "gemini-4-neon-high": "gemini-4-neon", "gemini-4-neon": "gemini-4-neon",
    "gemini-4-neon-xhigh": "gemini-4-neon", "gemini-4-hydrogen-tiered-high": "gemini-4-hydrogen",
    "Gemini 4 Hydrogen (Orchestrator)": "gemini-4-hydrogen", "gemini-flash-worker-agy": "gemini-flash-worker-agy",
    "Orchestrator (Neon)": "orchestrator-neon", "gemini-pro-orchestrator-neon": "orchestrator-neon",
    "gemini-pro-orchestrator": "orchestrator-carbon", "Orchestrator (Carbon)": "orchestrator-carbon",
    "Claude Sonnet 5.5 (High)": "claude-sonnet-5-5", "claude-sonnet-5-5-high": "claude-sonnet-5-5",
    "Claude Opus 4.6 (Thinking)": "claude-opus-4-6", "claude-opus-4-6-thinking": "claude-opus-4-6",
    "gemini-3.5-flash-lite": "gemini-3-5-flash-lite", "Gemini 3.5 Flash Lite": "gemini-3-5-flash-lite",
    "gemini-3.8-flash-tiered": "gemini-3-8-flash", "Gemini 3.8 Flash (Medium)": "gemini-3-8-flash",
    "GPT-OSS 120B (Medium)": "gpt-oss-120b", "google/gemini-4-carbon-high": "gemini-4-carbon", "": "", "  ": "",
}
wrong = {n: (cw.key(n), want) for n, want in names.items() if cw.key(n) != want}
check("key(): display names, ids, effort and tier suffixes, Orchestrators → one name per model", not wrong, wrong)
check("epoch(): Z, fractions and offsets; None for anything else",
      cw.epoch("2026-11-25T10:32:30Z") == cw.epoch("2026-11-25T13:32:30+03:00") == cw.epoch("2026-11-25T10:32:30.25Z")
      == 1795602750.0 and cw.epoch("") is None and cw.epoch("вчера") is None and cw.epoch("2026-13-45T99:00:00Z") is None)


# ─────────────────────────────── 2. readings and the file ───────────────────────────────
def entry(step, window, display="Gemini 4 Neon (High)", rid="gemini-4-neon"):
    cm = {"modelDisplayName": display, "responseModel": rid, "model": "MODEL_PLACEHOLDER_M532"}
    if window is not None:
        cm["chatStartMetadata"] = {"contextWindowMetadata": {"maxContextTokens": window, "estimatedTokensUsed": 1000}}
    return {"stepIndices": [step, step + 1], "chatModel": cm}


now = time.time()
got = cw.readings([entry(3, 600000), entry(5, "524288"), entry(7, True), entry(9, None), entry(11, 99),
                   entry(13, 512000, display="", rid="MODEL_PLACEHOLDER_M1"), {"chatModel": "odd"}, "odd"],
                  {3: now - 60, 5: now - 30}.get)
check("readings(): ints and int64 strings; no bools, missing, absurd or nameless windows; the call's time by its step",
      got == [(["Gemini 4 Neon (High)", "gemini-4-neon"], 600000, now - 60),
              (["Gemini 4 Neon (High)", "gemini-4-neon"], 524288, now - 30)], got)

reset()
check("nothing recorded → lookup() None, window() None", cw.lookup("gemini-4-neon-high") is None and cw.window("x") is None)
check("a call older than FRESH isn't recorded (its window may have changed since)",
      cw.record([(["Gemini 4 Neon (High)"], 256000, now - cw.FRESH - 60)], now) == 0 and not os.path.exists(CACHE))
check("a recent call is recorded under its model's key, whatever name it came by",
      cw.record([(["Gemini 4 Neon (High)", "gemini-4-neon"], 600000, now - 120)], now) == 1
      and cw.lookup("gemini-4-neon-low") == (600000, round(now - 120, 3)) and cw.window("Gemini 4 Neon (XHigh)") == 600000)
check("an older call never overwrites a newer one (reading an old conversation)",
      cw.record([(["gemini-4-neon"], 512000, now - 3600)], now) == 0 and cw.window("gemini-4-neon") == 600000)
check("the same call again writes nothing", cw.record([(["gemini-4-neon"], 600000, now - 120)], now) == 0)
check("a newer call wins, also with a smaller window",
      cw.record([(["gemini-4-neon"], 512000, now - 10)], now) == 1 and cw.window("gemini-4-neon-high") == 512000)
check("a call dated in the future is a bad clock: not recorded", cw.record([(["gemini-4-carbon"], 900000, now + 3600)], now) == 0)
with open(CACHE, "w", encoding="utf-8") as f:
    f.write("{broken")
check("an unreadable file counts as empty, and the next reading rewrites it",
      cw.load() == {} and cw.record([(["gemini-4-carbon"], 600000, now - 5)], now) == 1 and cw.window("gemini-4-carbon-high") == 600000)
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump({"v": 1, "models": {"a": {"window": "600000", "at": now}, "b": {"window": 600000, "at": True},
                                  "c": {"window": 600000, "at": now}, "d": [], "e": {"window": 10**12, "at": now}}}, f)
check("odd entries in the file are ignored", sorted(cw.load()) == ["c"], cw.load())
reset()
cw.record([([f"model-{i}"], 100000 + i, now - 1000 + i) for i in range(cw.KEEP + 20)], now)
kept = cw.load()
check(f"the file keeps the newest {cw.KEEP} models", len(kept) == cw.KEEP and "model-0" not in kept and f"model-{cw.KEEP + 19}" in kept,
      len(kept))
reset()
r = subprocess.run([sys.executable, os.path.join(HOOKS, "context_window.py")], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
check("CLI with nothing recorded says so", r.returncode == 0 and "No context window recorded yet" in r.stdout, r.stdout + r.stderr)
cw.record([(["Gemini 4 Neon (High)", "gemini-4-neon"], 600000, now - 120)], now)
r = subprocess.run([sys.executable, os.path.join(HOOKS, "context_window.py")], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
rj = subprocess.run([sys.executable, os.path.join(HOOKS, "context_window.py"), "--json"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
check("CLI lists the windows (and --json the file's entries)",
      "gemini-4-neon" in r.stdout and "600k" in r.stdout and "Gemini 4 Neon (High)" in r.stdout
      and json.loads(rj.stdout or "{}").get("gemini-4-neon", {}).get("window") == 600000, r.stdout + r.stderr)


# ─────────────────────────────── 3. the fake language server ───────────────────────────────
TOKEN = "tok-window"
CID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CALLS = {CID: [entry(1, 600000, "Gemini 4 Carbon (High)", "gemini-4-carbon"),
               entry(3, 600000, "Gemini 4 Carbon (High)", "gemini-4-carbon"),
               entry(5, 600000, "Gemini 4 Carbon (High)", "gemini-4-carbon")]}
asked = []


class LS(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {"status": "ok"}) if self.path == "/healthz" else self.reply(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("x-codeium-csrf-token") != TOKEN:
            return self.reply(403, {"code": "permission_denied", "message": "bad csrf token"})
        if not self.path.endswith("/GetCascadeTrajectoryGeneratorMetadata"):
            return self.reply(200, {})
        offset = int(body.get("generatorMetadataOffset") or 0)
        asked.append((body.get("cascadeId"), offset))
        found = CALLS.get(body.get("cascadeId"), [])[offset:]
        self.reply(200, {"generatorMetadata": found} if found else {})  # past the last call: {} (as Antigravity)


server = HTTPServer(("127.0.0.1", 0), LS)
threading.Thread(target=server.serve_forever, daemon=True).start()
ADDR = f"127.0.0.1:{server.server_address[1]}"
EP = (ADDR, TOKEN)

check("no environment, no daemon file → no language server (processes are never scanned)", cw.endpoint() is None)
os.makedirs(compat.daemon_dir(), exist_ok=True)
hub = os.path.join(compat.daemon_dir(), f"hub_{server.server_address[1]}.json")
with open(hub, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "httpPort": server.server_address[1], "httpsPort": 1, "lsVersion": "2.35.1",
               "csrfToken": TOKEN}, f)
check("a live daemon file → found (hooks get no ANTIGRAVITY_LS_ADDRESS)", cw.endpoint() == EP, cw.endpoint())
os.remove(hub)
os.environ.update(ANTIGRAVITY_LS_ADDRESS=ADDR, ANTIGRAVITY_CSRF_TOKEN=TOKEN)
check("the agent terminal's / sidecar's environment comes first", cw.endpoint() == EP)

del asked[:]
g = cw.latest(CID, 3)
check("latest(): asks only for the last calls (offset = calls − 2) and returns the latest",
      (g or {}).get("stepIndices") == [5, 6] and asked == [(CID, 1)], (g, asked))
del asked[:]
g = cw.latest(CID, 60)
check("latest() with an overcount: nothing past the end → asks again from further back, then from the start",
      (g or {}).get("stepIndices") == [5, 6] and asked == [(CID, 58), (CID, 20), (CID, 0)], asked)
del asked[:]
check("latest(): a conversation with no calls → None", cw.latest("ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee", 2) is None
      and asked == [("ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee", 0)], asked)
del asked[:]
check("latest(): not a conversation id → None, nothing asked", cw.latest("agp-speed-x", 3) is None and not asked, asked)
os.environ["AGP_WINDOW_CHECK"] = "off"
del asked[:]
check("AGP_WINDOW_CHECK=off → None, nothing asked", cw.latest(CID, 3) is None and not asked)
os.environ.pop("AGP_WINDOW_CHECK")
check("unreachable server → None", cw.metadata(("127.0.0.1:9", TOKEN), CID) is None and cw.latest(CID, 3, ("127.0.0.1:9", TOKEN)) is None)
check("wrong token → None", cw.metadata((ADDR, "nope"), CID) is None)

del asked[:]
check("latest(hook=True): a long conversation is never read from its start",
      cw.latest(CID, 60, hook=True) is None and asked == [(CID, 58), (CID, 20)], asked)
cw.HOOK_BYTES, old_limit = 200, cw.HOOK_BYTES
check("latest(hook=True): an answer over HOOK_BYTES is given up, outside a hook it is read",
      cw.latest(CID, 3, hook=True) is None and (cw.latest(CID, 3) or {}).get("stepIndices") == [5, 6])
cw.HOOK_BYTES = old_limit
reset()
got = cw.refresh(CID, 3, time.time() - 30)
check("refresh(): the latest call's reading, recorded", got is not None and got[1] == 600000 and "Gemini 4 Carbon (High)" in got[0]
      and cw.window("gemini-4-carbon-high") == 600000, got)
reset()
del asked[:]
check("refresh() of a call older than FRESH: nothing asked, nothing recorded",
      cw.refresh(CID, 3, time.time() - cw.FRESH - 5) is None and not asked and not os.path.exists(CACHE))

# client_window() and best() (hooks/client.py)
check("client_window(): bundled snapshot for Neon, Orchestrator (Neon), Claude 5.5 (840k), unknown → None",
      cw.client_window("Gemini 4 Neon (High)") == 512_000
      and cw.client_window("gemini-4-neon") == 512_000
      and cw.client_window("orchestrator-neon") == 512_000
      and cw.client_window("claude-sonnet-5-5") == 840_000
      and cw.client_window("unknown-model-x") is None,
      (cw.client_window("Gemini 4 Neon (High)"), cw.client_window("gemini-4-neon"),
       cw.client_window("orchestrator-neon"), cw.client_window("claude-sonnet-5-5")))

reset()
cw.record([(["gemini-4-neon"], 600_000, time.time() - 60)])
check("best(): a fresh reading wins over the client's window", cw.best("gemini-4-neon") == 600_000)

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump({"v": 1, "models": {"gemini-4-neon": {"window": 256_000, "at": time.time() - cw.FRESH - 60}}}, f)
check("best(): a reading older than FRESH loses to the client's window",
      cw.best("gemini-4-neon") == 512_000, cw.best("gemini-4-neon"))
reset()

_old_cc = os.environ.get("AGP_CLIENT_CACHE")
_old_bundled = client.BUNDLED
_custom_cc = os.path.join(SP, "cw-client-cache.json")
try:
    os.environ["AGP_CLIENT_CACHE"] = os.path.join(SP, "cw-missing-client.json")
    client.BUNDLED = os.path.join(SP, "cw-missing-bundled.json")
    client._memo.clear()
    check("best(): no client snapshot and no reading → None", cw.best("gemini-4-neon") is None, cw.best("gemini-4-neon"))

    with open(_custom_cc, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "at": int(time.time()), "models": {
            "gemini-4-neon-high": {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)",
                                   "family": "gemini-4-neon", "effort": "high", "provider": "google", "window": 1_000_000},
        }}, f)
    os.environ["AGP_CLIENT_CACHE"] = _custom_cc
    client._memo.clear()
    check("client_window(): follows a hand-written cache snapshot (1_000_000)",
          cw.client_window("gemini-4-neon-high") == 1_000_000, cw.client_window("gemini-4-neon-high"))
finally:
    if _old_cc is None:
        os.environ.pop("AGP_CLIENT_CACHE", None)
    else:
        os.environ["AGP_CLIENT_CACHE"] = _old_cc
    client.BUNDLED = _old_bundled
    if os.path.exists(_custom_cc):
        os.remove(_custom_cc)
    client._memo.clear()


# ─────────────────────────────── 4. the context hints (hooks/speed.py) ───────────────────────────────
check("ctx_limits() without a reading: the numbers known at release",
      speed.ctx_limits("gemini-4-carbon-high") == (200_000, 440_000, " of the model's 512k window"))
check("ctx_limits() with a bigger reported window: compaction warning moves with it, the first hint stays",
      speed.ctx_limits("gemini-4-carbon-high", 600_000) == (200_000, 515_625, " of the model's 600k window"),
      speed.ctx_limits("gemini-4-carbon-high", 600_000))
check("ctx_limits() with a smaller one: both thresholds move down",
      speed.ctx_limits("gemini-4-neon-high", 256_000) == (100_000, 220_000, " of the model's 256k window"),
      speed.ctx_limits("gemini-4-neon-high", 256_000))
check("ctx_limits() of a model the table doesn't know, with a reported window",
      speed.ctx_limits("some-new-model", 300_000) == (180_000, 257_812, " of the model's 300k window")
      and speed.ctx_limits("some-new-model") == (speed.CTX_WARN, speed.CTX_HIGH, ""), speed.ctx_limits("some-new-model", 300_000))
check("ctx_limits() ignores a nonsense window", speed.ctx_limits("gemini-4-neon-high", True) == speed.ctx_limits("gemini-4-neon-high")
      and speed.ctx_limits("gemini-4-neon-high", 10) == speed.ctx_limits("gemini-4-neon-high"))
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "80%"
check("the Antigravity CLI's AGY_CLI_AUTO_COMPACT_OVERRIDE counts from the reported window",
      "480k compaction limit" in speed.ctx_limits("gemini-4-neon-high", 600_000)[2], speed.ctx_limits("gemini-4-neon-high", 600_000))
os.environ.pop("AGY_CLI_AUTO_COMPACT_OVERRIDE")

reset()
del asked[:]
payload = {"conversationId": CID, "modelName": "gemini-4-carbon-high"}
calls, sp = {"n_calls": 3, "ctx_at": iso(20)}, {}
check("real_window(): no call made yet → nothing asked, the client's window (hooks/client.py snapshot)",
      speed.real_window(payload, {}, {}, "gemini-4-carbon-high") == 512_000 and not asked)
check("real_window(): calls counted from the middle (begun under an older version) → nothing asked",
      speed.real_window(payload, dict(calls, n_partial=True), {}, "gemini-4-carbon-high") == 512_000 and not asked)
w = speed.real_window(payload, calls, sp, "gemini-4-carbon-high")
check("real_window(): no reading → asks its own latest call after the first one, records it",
      w == 600_000 and asked == [(CID, 1)] and sp.get("win_ask") == {"k": "gemini-4-carbon", "n": 3}
      and cw.window("gemini-4-carbon") == 600_000, (w, asked, sp))
del asked[:]
check("real_window(): a fresh reading is used as it is", speed.real_window(payload, calls, {}, "gemini-4-carbon-high")
      == 600_000 and not asked)
check("real_window(): another conversation of the same model uses it too (one reading per model)",
      speed.real_window({"conversationId": "ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee"}, calls, {}, "Gemini 4 Carbon (Medium)")
      == 600_000 and not asked)
reset()
ORCH = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CALLS[ORCH] = [entry(1, 600000, "Orchestrator (Neon)", "gemini-pro-orchestrator-neon"),
               entry(3, 600000, "Gemini 4 Hydrogen (Orchestrator)", "gemini-flash-worker-agy")]
sp = {}
w = speed.real_window({"conversationId": ORCH}, {"n_calls": 2, "ctx_at": iso(20)}, sp, "gemini-pro-orchestrator-neon")
check("real_window(): Orchestrator whose latest call is its executor's → that reading is the session's own",
      w == 600_000 and sp.get("win_conv") == 600_000 and cw.window("gemini-pro-orchestrator-neon") is None, (w, sp))
del asked[:]
check("… and stays the session's for the next calls, nothing asked",
      speed.real_window({"conversationId": ORCH}, {"n_calls": 3, "ctx_at": iso(10)}, sp, "gemini-pro-orchestrator-neon") == 600_000
      and not asked)
reset()
SWITCH = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CALLS[SWITCH] = [entry(1, 512000, "Gemini 4 Neon (High)", "gemini-4-neon")]
sp = {}
check("real_window(): the user switched to Claude — Neon's latest call is no reading of Claude's window (the client's 840k)",
      speed.real_window({"conversationId": SWITCH}, {"n_calls": 1, "ctx_at": iso(20)}, sp, "claude-sonnet-5-5-high") == 840_000
      and "win_conv" not in sp and cw.window("gemini-4-neon") == 512000, sp)
reset()
with open(CACHE, "w", encoding="utf-8") as f:  # a reading older than STALE (written by hand: record() keeps only fresh ones)
    json.dump({"v": 1, "models": {"gemini-4-carbon": {"window": 512_000, "at": time.time() - cw.STALE - 100}}}, f)
os.environ["ANTIGRAVITY_LS_ADDRESS"] = "127.0.0.1:9"
sp = {}
check("real_window(): a stale reading and no answer → the stale reading still beats the table",
      speed.real_window(payload, calls, sp, "gemini-4-carbon-high") == 512_000 and sp.get("win_ask", {}).get("n") == 3)
os.environ["ANTIGRAVITY_LS_ADDRESS"] = ADDR
del asked[:]
check("real_window(): … and isn't asked again for the next calls of this conversation",
      speed.real_window(payload, dict(calls, n_calls=10), sp, "gemini-4-carbon-high") == 512_000 and not asked)
check(f"real_window(): … until {speed.ASK_AGAIN} calls later (an overcount of a short conversation: read from its start)",
      speed.real_window(payload, dict(calls, n_calls=3 + speed.ASK_AGAIN), sp, "gemini-4-carbon-high") == 600_000
      and asked == [(CID, 1 + speed.ASK_AGAIN), (CID, 0)], asked)
reset()
del asked[:]
check("real_window(): the latest call is days old (an old conversation resumed) → not asked, the client's window",
      speed.real_window(payload, {"n_calls": 3, "ctx_at": iso(3 * 86400)}, {}, "gemini-4-carbon-high") == 512_000
      and not asked)
check("real_window(): never raises on odd state",
      speed.real_window({}, {"n_calls": "x", "ctx_at": 5}, {"win_ask": "odd"}, "gemini-4-carbon-high") is None)

cli_tp = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "brain", CID, ".system_generated", "logs", "transcript_full.jsonl")
desk_tp = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "brain", CID, ".system_generated", "logs", "transcript_full.jsonl")
cli_payload = {"conversationId": CID, "modelName": "gemini-4-carbon-high", "transcriptPath": cli_tp}
desk_payload = {"conversationId": CID, "modelName": "gemini-4-carbon-high", "transcriptPath": desk_tp}
check("cli_session(): True for antigravity-cli transcriptPath, False for Desktop",
      speed.cli_session(cli_payload) is True and speed.cli_session(desk_payload) is False,
      (speed.cli_session(cli_payload), speed.cli_session(desk_payload)))
reset()
del asked[:]
check("real_window() in CLI session (transcriptPath): asks nothing, returns client window (512000)",
      speed.real_window(cli_payload, calls, {}, "gemini-4-carbon-high") == 512_000 and not asked,
      (speed.real_window(cli_payload, calls, {}, "gemini-4-carbon-high"), asked))
os.environ["AGP_RUNTIME"] = "cli"
del asked[:]
w_cli_env = speed.real_window({"conversationId": CID, "modelName": "gemini-4-carbon-high"}, calls, {}, "gemini-4-carbon-high")
os.environ.pop("AGP_RUNTIME", None)
check("real_window() in CLI session (AGP_RUNTIME=cli): asks nothing, returns client window (512000)",
      w_cli_env == 512_000 and not asked, (w_cli_env, asked))
cw.record([(["gemini-4-carbon"], 600_000, time.time() - 60)])
del asked[:]
check("real_window() in CLI session with fresh reading: asks nothing, returns recorded reading (600000)",
      speed.real_window(cli_payload, calls, {}, "gemini-4-carbon-high") == 600_000 and not asked,
      (speed.real_window(cli_payload, calls, {}, "gemini-4-carbon-high"), asked))
reset()

# how the hook counts calls (hooks/conversation.py)
import conversation as conv  # noqa: E402
line = json.dumps({"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                   "created_at": "2026-11-25T10:32:30Z", "model": "gemini-4-neon-high", "input_tokens": 1000,
                   "cache_read_tokens": 40000, "output_tokens": 5, "content": "\"created_at\": \"1999-01-01T00:00:00Z\""}).encode()
st_new, st_old = {"calls": {}}, {"calls": {"ctx": 300_000}}
conv.track_steps(st_new, b"PLANNER_RESPONSE", line)
conv.track_steps(st_new, b"PLANNER_RESPONSE", line)
conv.track_steps(st_old, b"PLANNER_RESPONSE", line)
check("track_steps(): counts the calls and keeps the latest one's created_at (not one quoted in its text)",
      st_new["calls"].get("n_calls") == 2 and st_new["calls"].get("ctx_at") == "2026-11-25T10:32:30Z"
      and not st_new["calls"].get("n_partial") and st_new["calls"].get("ctx") == 41_000, st_new)
check("track_steps(): a conversation begun under an older version is marked as counted from the middle",
      st_old["calls"].get("n_partial") is True and st_old["calls"].get("n_calls") == 1, st_old)


# at most PROBES daemon files are probed (on Windows a stale file's process can't be told dead)
saved_env = {k_: os.environ.pop(k_, None) for k_ in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")}
import socket  # noqa: E402
dead_ports = []
for _ in range(cw.PROBES):
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); dead_ports.append(sk.getsockname()[1]); sk.close()
hubs = []
for i, port in enumerate([server.server_address[1]] + dead_ports):  # the live one is the oldest
    path = os.path.join(compat.daemon_dir(), f"hub_{port}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "httpPort": port, "httpsPort": 1, "lsVersion": "2.35.1", "csrfToken": TOKEN}, f)
    os.utime(path, (time.time() - 100 + i, time.time() - 100 + i))
    hubs.append(path)
t0 = time.time()
check(f"endpoint() probes only the {cw.PROBES} newest daemon files", cw.endpoint() is None and time.time() - t0 < cw.PROBES * cw.PROBE + 1)
os.remove(hubs[-1])
check("… so a live server among them is found", cw.endpoint() == EP, cw.endpoint())
for path in hubs[:-1]:
    os.remove(path)
os.environ.update({k_: v_ for k_, v_ in saved_env.items() if v_})

# a cache path without a folder (AGP_WINDOW_CACHE=name.json) and several writers at once
bare = os.path.join(SP, "bare")
os.makedirs(bare, exist_ok=True)
here = os.getcwd()
os.chdir(bare)
os.environ["AGP_WINDOW_CACHE"] = "windows.json"
cw.record([(["gemini-4-neon"], 600_000, time.time() - 5)])
check("AGP_WINDOW_CACHE without a folder: written next to the process", os.path.isfile(os.path.join(bare, "windows.json")))
os.chdir(here)
os.environ.pop("AGP_WINDOW_CACHE")
reset()
writer = ("import sys, time; sys.path.insert(0, sys.argv[1]); import context_window as cw\n"
          "for j in range(15):\n"
          "    cw.record([([f'model-{sys.argv[2]}-{j}'], 100000, time.time() - 1)])\n")
procs = [subprocess.Popen([sys.executable, "-c", writer, HOOKS, str(i)]) for i in range(6)]
for pr in procs:
    pr.wait()
check("six processes recording at once lose nothing (the file is merged under a lock)", len(cw.load()) == 90, len(cw.load()))
reset()


# end to end: the PreInvocation hook reads the transcript, asks the fake server and words the hint by 600k
art = os.path.join(SP, "art")
os.makedirs(art, exist_ok=True)
tp = os.path.join(art, "transcript_full.jsonl")


rows_by = {}


def transcript(cid, *tokens):
    """Append model calls carrying `tokens` of cache reads to conversation `cid`'s transcript (earlier rows keep their
    bytes: the hook reads the transcript incrementally)."""
    rows = rows_by.setdefault(cid, [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE",
                                     "created_at": iso(60), "content": "<USER_REQUEST>Перенеси модуль отчётов</USER_REQUEST>"}])
    for t in tokens:
        rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": iso(5),
                     "model": "gemini-4-carbon-high", "input_tokens": 5_000, "cache_read_tokens": t, "output_tokens": 300,
                     "content": "…"})
    with open(os.path.join(art, cid + ".jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def pre(cid, env_extra=None):
    p = {"conversationId": cid, "transcriptPath": os.path.join(art, cid + ".jsonl"), "artifactDirectoryPath": art,
         "workspacePaths": [SP], "modelName": "gemini-4-carbon-high"}
    env = dict(os.environ, **(env_extra or {}))
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p), env=env,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    msgs = [(s.get("systemMessage") or {}).get("systemMessage") or "" for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "<context_budget" in m), out.stderr.strip()


reset()
del asked[:]
transcript(CID, 150_000, 300_000, 450_000)
text, err = pre(CID)
check("hook at 455k with a 600k window reported: the lean hint only, not «close to compaction»",
      "~455k" in text and "compacts" not in text and not err and asked == [(CID, 1)]
      and cw.window("gemini-4-carbon") == 600_000, (text, err, asked))
transcript(CID, 515_000)
text, err = pre(CID)
check("hook at 520k: close to compaction, of the model's 600k window",
      "~520k" in text and "600k window" in text and "compacts" in text and not err, (text, err))
reset()
OTHER = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
transcript(OTHER, 150_000, 300_000, 450_000)
text, err = pre(OTHER, {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9"})
check("hook with no server to ask: the window known at release (512k) — as before",
      "~455k" in text and "512k window" in text and "compacts" in text and not err, (text, err))


# ─────────────────────────────── 5. /usage, the side panel and /context ───────────────────────────────
brain = os.path.join(SP, "brain", CID, ".system_generated", "logs")
os.makedirs(brain, exist_ok=True)
rows = [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": iso(120),
         "content": "<USER_REQUEST>Проверь лимиты</USER_REQUEST>\nModel Selection` from None to Gemini 4 Carbon (High)."}]
for i, t in ((1, 100_000), (3, 200_000), (5, 230_000)):
    rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "created_at": iso(60 - i), "input_tokens": 10_000,
                 "cache_read_tokens": t - 10_000, "output_tokens": 500, "content": "…"})
with open(os.path.join(brain, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def usage(*args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    r = subprocess.run([sys.executable, USAGE, "--root", SP, "--conversation", CID] + list(args), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout, r.stderr


reset()
del asked[:]
out, err = usage("--json")
ctx = (json.loads(out or "{}").get("context") or {})
check("usage --json (the side panel): the window Antigravity gave the latest call, asked for that call only",
      ctx.get("window") == 600_000 and ctx.get("window_source") == "antigravity" and ctx.get("current") == 230_000
      and asked == [(CID, 1)], (ctx, asked, err))
check("… and recorded for the hints and lists", cw.window("gemini-4-carbon") == 600_000)
out, err = usage(env_extra={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9"})
check("usage markdown without the server: the recorded window", "230k / 600k" in out, out[:400] + err)
r = subprocess.run([sys.executable, USAGE, "--root", SP, "--recent", "5"], capture_output=True, text=True, encoding="utf-8",
                   env=dict(os.environ, ANTIGRAVITY_LS_ADDRESS="127.0.0.1:9"))
items = json.loads(r.stdout or "{}").get("conversations") or []
first = next((c for c in items if c.get("id") == CID), {})
check("usage --recent (the panel's list): the recorded window, never asked per conversation",
      (first.get("context") or {}).get("window") == 600_000 and (first.get("context") or {}).get("window_source") == "recorded",
      r.stdout[:600] + r.stderr)
cw.record([(["Gemini 4 Carbon (High)"], 640_000, time.time() - 1)])
r = subprocess.run([sys.executable, USAGE, "--root", SP, "--recent", "5"], capture_output=True, text=True, encoding="utf-8",
                   env=dict(os.environ, ANTIGRAVITY_LS_ADDRESS="127.0.0.1:9"))
first = next((c for c in json.loads(r.stdout or "{}").get("conversations") or [] if c.get("id") == CID), {})
check("usage --recent: its cache is computed again when a recorded window changes",
      (first.get("context") or {}).get("window") == 640_000, r.stdout[:600] + r.stderr)
out, err = usage("--json", "--model", "gemini-3.8-flash")
ctx = (json.loads(out or "{}").get("context") or {})
check("usage --model: that model's window (the client's snapshot, before pricing.json), not the conversation's",
      ctx.get("window") == 256_000 and ctx.get("window_source") == "client", ctx)
reset()
out, err = usage("--json", env_extra={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9"})
ctx = (json.loads(out or "{}").get("context") or {})
check("usage with no server and nothing recorded: the client's 512k (its snapshot), before pricing.json",
      ctx.get("window") == 512_000 and ctx.get("window_source") == "client", ctx)
r = subprocess.run([sys.executable, CONTEXT, "--root", SP, "--conversation", CID, "--json"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
cj = json.loads(r.stdout or "{}")
check("/context: the window Antigravity gave the latest call", cj.get("window") == 600_000, r.stdout[:400] + r.stderr)
server.shutdown()
sys.exit(0 if ok else 1)
