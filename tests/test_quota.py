"""Plan limits: the main agent hears when the quota of its model's group gets short (hooks/quota.py)."""
import http.server, json, os, socket, subprocess, sys, threading, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
import quota
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
CACHE = os.path.expanduser("~/.gemini/antigravity-plus/quota.json")
art = os.path.join(SP, "quotatest"); os.makedirs(art, exist_ok=True)
WS = os.path.realpath(os.path.join(SP, "proj")); os.makedirs(WS, exist_ok=True)
ok = True

iso = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs))
# what the fake plan has left per group: weekly fraction (0 = used up), seconds until the week resets, 5 h fraction
PLAN = {"gemini": [0.9, 6 * 86400, 1.0], "claude": [0.8, 6 * 86400, 1.0]}
calls = []


def answer():
    def buckets(key):
        week, secs, five = PLAN[key][:3]
        h5_secs = PLAN[key][3] if len(PLAN[key]) > 3 else 3 * 3600
        weekly: dict = {"window": "weekly", "resetTime": iso(secs)}
        if week:  # proto3 JSON leaves a 0 out: no remainingFraction means used up
            weekly["remainingFraction"] = week
        h5: dict = {"window": "5h", "resetTime": iso(h5_secs)}
        if five:
            h5["remainingFraction"] = five
        if not week:
            h5["disabled"] = True
        return [weekly, h5]
    return {"response": {"groups": [
        {"displayName": "Gemini Models", "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen, "
                                                        "Orchestrator (Carbon), Gemini 3.8 Flash", "buckets": buckets("gemini")},
        {"displayName": "Claude and GPT models", "description": "Models within this group: Claude Sonnet 5.5, Claude Opus 5.5",
         "buckets": buckets("claude")}]}}


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
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-codeium-csrf-token") != "tok":
            return self.reply(403, {"code": "permission_denied", "message": "bad csrf token"})
        method = self.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "RetrieveUserQuotaSummary":
            return self.reply(200, answer())
        self.reply(404, {"code": "unimplemented"})


class Slow(Handler):
    def do_POST(self):
        time.sleep(6)  # far longer than the hook waits
        self.reply(200, answer())


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"127.0.0.1:{srv.server_address[1]}"


LS, SLOW = serve(Handler), serve(Slow)
s = socket.socket(); s.bind(("127.0.0.1", 0)); CLOSED = f"127.0.0.1:{s.getsockname()[1]}"; s.close()
ENV = {k: v for k, v in os.environ.items() if k != "AGP_QUOTA_CHECK"}
ENV.update(ANTIGRAVITY_LS_ADDRESS=LS, ANTIGRAVITY_CSRF_TOKEN="tok")


def check(name, cond, got=None):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name:<64} → {str(got)[:110]!r}")


class Conv:
    def __init__(self, name, kind="main"):
        self.cid, self.tp, self.rows = f"agp-quota-{name}", os.path.join(art, f"{name}.jsonl"), []
        for f in (self.tp, os.path.join(conv.STATE_DIR, self.cid + ".json")):
            if os.path.exists(f):
                os.remove(f)
        if kind == "subagent":
            self.add("SYSTEM_MESSAGE", "<SYSTEM_MESSAGE>injected by a SessionStart hook</SYSTEM_MESSAGE>")
        else:
            self.say(conv.DELEGATED_MARK + " Fix the failing test" if kind == "delegated" else "Сделай отчёт по логам")

    def add(self, kind, content=""):
        row = {"step_index": len(self.rows), "type": kind, "status": "DONE", "created_at": iso(0)}
        if content:
            row["content"] = content
        self.rows.append(row)
        with open(self.tp, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in self.rows) + "\n")

    def say(self, text):
        self.add("USER_INPUT", f"<USER_REQUEST>{text}</USER_REQUEST>")
        self.add("PLANNER_RESPONSE", "…")

    def pre_steps(self, model="Gemini 4 Carbon (High)", env=None):
        p = {"conversationId": self.cid, "transcriptPath": self.tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
        if model:
            p["modelName"] = model
        started = time.time()
        out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=dict(ENV, **(env or {})))
        self.took = time.time() - started
        self.err = out.stderr.strip()
        return json.loads(out.stdout or "{}").get("injectSteps", [])

    def pre(self, model="Gemini 4 Carbon (High)", env=None):
        msgs = [(st.get("systemMessage") or {}).get("systemMessage") or st.get("ephemeralMessage") or ""
                for st in self.pre_steps(model=model, env=env)]
        return "\n".join(m for m in msgs if "<quota" in m)

    def state(self):
        return json.load(open(os.path.join(conv.STATE_DIR, self.cid + ".json"), encoding="utf-8"))

    def age(self, secs):  # pretend the last quota reading of this conversation was `secs` ago
        st = self.state()
        st["quota"]["checked"] -= secs
        json.dump(st, open(os.path.join(conv.STATE_DIR, self.cid + ".json"), "w", encoding="utf-8"))


def expire_cache():
    d = json.load(open(CACHE, encoding="utf-8"))
    d["at"] -= quota.CACHE_TTL + 1
    json.dump(d, open(CACHE, "w", encoding="utf-8"))


def drop_cache():
    if os.path.exists(CACHE):
        os.remove(CACHE)


# 1. units: which group a model draws on, and how a group stands
groups = quota.parse(answer())
for model, want in [("Gemini 4 Carbon (High)", "Gemini Models"), ("Orchestrator (Carbon)", "Gemini Models"),
                    ("Gemini 4 Hydrogen (Orchestrator)", "Gemini Models"), ("Claude Sonnet 5.5 (Thinking)", "Claude and GPT models"),
                    ("claude-sonnet-5-5-high", "Claude and GPT models"), ("gemini-4-carbon-high", "Gemini Models"),
                    ("GPT-OSS 120B (Medium)", "Claude and GPT models"), ("Some Other Model", None)]:
    g = quota.group_for(model, groups)
    check(f"group of {model}", (g or {}).get("name") == want, (g or {}).get("name"))
now = time.time()
for name, plan, want in [("fine", [0.9, 6 * 86400, 1.0], "ok"), ("weekly under 15 %", [0.12, 3 * 86400, 1.0], "tight"),
                         ("5 h under 10 %", [0.9, 6 * 86400, 0.05], "tight"),
                         ("spent far ahead of the week", [0.35, 6 * 86400, 1.0], "tight"),
                         ("on pace", [0.45, 3 * 86400, 1.0], "ok"), ("week used up", [0, 86400, 1.0], "exhausted"),
                         ("5 h used up", [0.5, 2 * 86400, 0], "exhausted")]:
    PLAN["gemini"] = plan
    st = quota.state(quota.parse(answer())[0], now)
    check(f"state: {name}", st["status"] == want, f"{st['status']}: {st['text']}")
PLAN["gemini"] = [0.9, 6 * 86400, 1.0]

# 2. tight: the numbers, the reset and what to do — once
PLAN["gemini"] = [0.1, 3 * 86400, 0.6]
c = Conv("tight")
msg = c.pre()
check("tight weekly → a <quota> hint", msg.startswith('<quota source="Antigravity Plus">') and not c.err, msg)
check("… with the numbers and the reset time", all(w in msg for w in ("Gemini Models", "week 10% left", "resets in 2 days",
                                                                      "5 h 60% left")), msg)
check("… and the actions", all(w in msg for w in ("Model: flash", "Gemini 4 Hydrogen", "Orchestrator (Neon)",
                                                  "quarter of Carbon", "/handoff", "rg -n", "«Claude and GPT models»"))
      and "Orchestrator (Carbon)" not in msg, msg)  # it is barely cheaper than Carbon (High): measured 2026-11-19
raw_cache = open(CACHE, encoding="utf-8").read()
check("the reading is cached in quota.json without the CSRF token",
      json.loads(raw_cache).get("groups") and '"ls"' not in raw_cache and '"tok"' not in raw_cache, raw_cache)
n = len(calls)
c.say("А теперь по ошибкам")
check("still tight, new message → told once per level", c.pre() == "" and not c.err, c.err)
check("… and the 5-minute cache answered it (no RPC)", len(calls) == n, len(calls) - n)

# 3. exhausted after tight: told again
PLAN["gemini"] = [0, 20 * 3600, 1.0]
expire_cache()
c.say("Продолжай")
msg = c.pre()
check("tight → exhausted is told again", "used up" in msg and "resets in 20 h" in msg and "/handoff" in msg, msg)
check("… with a reading from the server", len(calls) == n + 1, len(calls) - n)
c.say("Ещё")
expire_cache()
check("exhausted once per conversation", c.pre() == "", c.err)
PLAN["gemini"] = [0.1, 6 * 86400 + 20 * 3600, 1.0]
expire_cache()
c.say("Новая неделя")
msg = c.pre()
check("a new week's window, tight again → told again", "week 10% left" in msg, msg)
PLAN["gemini"] = [0.8, 4 * 86400, 0.05, 3 * 3600]
expire_cache()
h5_conv = Conv("h5-roll")
msg_h5 = h5_conv.pre()
check("5h tight while weekly is ok → told", "5 h 5.0% left" in msg_h5, msg_h5)
h5_conv.say("Дальше")
check("same 5h window → silent", h5_conv.pre() == "", h5_conv.err)
PLAN["gemini"] = [0.8, 4 * 86400, 0.05, 8 * 3600]
expire_cache()
h5_conv.say("Прошло 5 часов")
msg_h5_2 = h5_conv.pre()
check("new 5h window rolls over independently of weekly reset → told again", "5 h 5.0% left" in msg_h5_2, msg_h5_2)
PLAN["gemini"] = [0, 20 * 3600, 1.0]

# 4. throttle: no new message → at most every RECHECK seconds, even with the cache expired
expire_cache()
n = len(calls)
check("same message, cache expired, < 10 min → silent, no RPC", c.pre() == "" and len(calls) == n, len(calls) - n)
c.age(quota.RECHECK + 5)
c.pre()
check("same message after 10 min → reads again", len(calls) == n + 1, len(calls) - n)

# 5. fine: silent
drop_cache()
PLAN["gemini"], PLAN["claude"] = [0.9, 6 * 86400, 1.0], [0.8, 6 * 86400, 1.0]
c = Conv("fine")
check("plenty left → silent", c.pre() == "" and not c.err, c.err)
PLAN["claude"] = [0.05, 2 * 86400, 1.0]
drop_cache()
c = Conv("claude")
msg = c.pre("Claude Sonnet 5.5 (Thinking)")
check("Claude tight → points to the Gemini group's quota", "«Claude and GPT models»" in msg and "«Gemini Models»" in msg
      and "separate quota" in msg, msg)
drop_cache()
check("no modelName in the payload → silent", Conv("nomodel").pre(model="") == "", "")

# 6. subagents and delegated tasks: silent even when tight
PLAN["gemini"] = [0.05, 2 * 86400, 1.0]
drop_cache()
n = len(calls)
check("subagent → silent", Conv("sub", "subagent").pre() == "", "")
check("delegated from Claude Code → silent", Conv("deleg", "delegated").pre() == "", "")
check("… and neither asked the server", len(calls) == n, len(calls) - n)

# 7. setting off / AGP_QUOTA_CHECK=off
r = subprocess.run([sys.executable, MEMORY, "config", "quota-hints", "off"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
check("memory.py config quota-hints off", r.returncode == 0 and '"quotaHints": false' in r.stdout, r.stdout + r.stderr)
check("setting off → silent", Conv("off").pre() == "", "")
subprocess.run([sys.executable, MEMORY, "config", "quota-hints", "on"], capture_output=True, text=True,
               encoding="utf-8", errors="replace")
check("AGP_QUOTA_CHECK=off → silent", Conv("envoff").pre(env={"AGP_QUOTA_CHECK": "off"}) == "", "")
custom_cache = os.path.join(art, "custom-quota.json")
with open(custom_cache, "w", encoding="utf-8") as fh:
    json.dump({"at": time.time(), "groups": quota.parse(answer())}, fh)
check("AGP_QUOTA_CACHE overrides quota.json path", "week 5.0% left" in Conv("customcache").pre(env={"AGP_QUOTA_CACHE": custom_cache}), custom_cache)
check("setting on again → told", Conv("on").pre() != "", "")

# 8. the language server down or hanging: silent and fast; the failure is shared for CACHE_TTL
def cli(*args, addr=LS):
    started = time.time()
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "quota.py"), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=dict(ENV, ANTIGRAVITY_LS_ADDRESS=addr))
    return r, time.time() - started


for name, addr in [("refused", CLOSED), ("hanging", SLOW)]:
    drop_cache()
    c = Conv(f"down-{name}")
    msg = c.pre(env={"ANTIGRAVITY_LS_ADDRESS": addr})
    check(f"LS {name} → silent, no error", msg == "" and not c.err, c.err)
    check("… the failure is cached", json.load(open(CACHE, encoding="utf-8")).get("failed") is True, "")
    r, took = cli("--fresh", addr=addr)
    check(f"… a reading gives up fast ({took:.1f} s)", r.returncode == 1 and took < quota.TIMEOUT + 1.5, took)
r, took = cli(addr=SLOW)
check(f"a cached failure isn't retried at once ({took:.1f} s)", r.returncode == 1 and took < 1.2, took)
for bad in ("{not json", "[]", json.dumps({"at": "x", "groups": "y"})):
    open(CACHE, "w", encoding="utf-8").write(bad)
    c = Conv("badcache")
    check(f"unreadable cache {bad[:12]!r} → read again, told", c.pre() != "" and not c.err, c.err)

# 9. CLI
drop_cache()
r = subprocess.run([sys.executable, os.path.join(HOOKS, "quota.py")], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=ENV)
check("quota.py prints each group's status", r.returncode == 0 and "Gemini Models: tight" in r.stdout
      and "Claude and GPT models:" in r.stdout, r.stdout + r.stderr)

# 10. UI user_markdown badges + zero-token <= 5% critical banner + quota.message backward compatibility
PLAN["gemini"] = [0.12, 3 * 86400, 0.7]
drop_cache()
ui_c = Conv("ui-crit")
steps_15 = ui_c.pre_steps()
q_step = next((s["systemMessage"] for s in steps_15 if "<quota" in (s.get("systemMessage") or {}).get("systemMessage", "")), {})
md_15 = (q_step.get("metadata") or {}).get("user_markdown", "")
check("15% tight → user_markdown shows group, %, reset time and sidecar link",
      all(w in md_15 for w in ("Gemini Models", "12%", "сброс", "[Расход токенов](sidecar://antigravity-plus/usage/)")), md_15)

PLAN["gemini"] = [0.04, 3 * 86400, 0.7]
drop_cache()
ui_c.say("Продолжай до 4%")
steps_05 = ui_c.pre_steps()
zero_steps = [s["systemMessage"] for s in steps_05 if (s.get("systemMessage") or {}).get("systemMessage") == ""
              and (s.get("systemMessage") or {}).get("metadata", {}).get("user_markdown")]
check("<= 5% after 15% hint → zero-token UI-only banner once",
      len(zero_steps) == 1 and "4.0%" in zero_steps[0]["metadata"]["user_markdown"]
      and "sidecar://antigravity-plus/usage/" in zero_steps[0]["metadata"]["user_markdown"]
      and bool(ui_c.state().get("quota_ui_crit")), steps_05)

PLAN["gemini"] = [0.02, 3 * 86400, 0.7]
drop_cache()
ui_c.say("А на 2%?")
steps_02 = ui_c.pre_steps()
check("…still <= 5% same window → silent (no repeat UI banner)",
      not any("Квота" in ((s.get("systemMessage") or {}).get("metadata") or {}).get("user_markdown", "") for s in steps_02), steps_02)

# uiNotices: false disables metadata.user_markdown & zero-token banner
settings_path = os.path.expanduser("~/.gemini/antigravity-plus/settings.json")
with open(settings_path, "w", encoding="utf-8") as fh:
    json.dump({"uiNotices": False}, fh)
PLAN["gemini"] = [0.12, 3 * 86400, 0.7]
drop_cache()
off_c = Conv("ui-off")
steps_off = off_c.pre_steps()
check("uiNotices=false → model <quota> sent without metadata.user_markdown",
      any("<quota" in (s.get("systemMessage") or {}).get("systemMessage", "") for s in steps_off)
      and not any("metadata" in (s.get("systemMessage") or {}) for s in steps_off), steps_off)
PLAN["gemini"] = [0.04, 3 * 86400, 0.7]
drop_cache()
off_c.say("До 4%")
check("uiNotices=false → no <= 5% zero-token banner",
      not any("Квота" in ((s.get("systemMessage") or {}).get("metadata") or {}).get("user_markdown", "") for s in off_c.pre_steps()), "")
with open(settings_path, "w", encoding="utf-8") as fh:
    json.dump({"uiNotices": True}, fh)

# backward-compatible quota.message() still returns Optional[str]
compat_st = {"users": [{"text": "hi", "step": 0, "ts": iso(0)}]}
old_env = os.environ.copy()
os.environ.update(ENV)
drop_cache()
compat_msg = quota.message({"conversationId": "compat-q", "modelName": "Gemini 4 Carbon (High)"}, compat_st)
os.environ.clear(); os.environ.update(old_env)
check("quota.message() stays backward-compatible returning str", isinstance(compat_msg, str) and "<quota" in compat_msg, compat_msg)

sys.exit(0 if ok else 1)
