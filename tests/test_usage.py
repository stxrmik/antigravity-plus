"""/usage & pricing.json: Gemini 4 / 3.x / 2.5 models, Antigravity's own windows (512k for Gemini 4, 256k for Gemini
3.x Flash, 128k for Flash-Lite, 840k for Claude 5.5 — it compacts when the next call won't fit, so no separate
compaction threshold), tiers and price steps."""
import datetime as dt, json, os, subprocess, sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
USAGE_DIR = os.path.join(PLUGIN, "skills", "usage", "scripts")
PRICING = os.path.join(PLUGIN, "skills", "usage", "pricing.json")
sys.path.insert(0, USAGE_DIR)
import usage  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {detail}"))


p = usage.Pricing(PRICING)

# 1. Model lookup: new Gemini 4 & 3.x lineup, and flash-lite isn't swallowed by generic flash
expected = [
    ("Gemini 4 Carbon (High)", "gemini-4-carbon", 2.0, 10.0, 0.10, 512_000, None),
    ("Gemini 4 Argon (Medium)", "gemini-4-argon", 2.0, 10.0, 0.10, 512_000, None),
    ("Gemini 4 Hydrogen (XHigh)", "gemini-4-hydrogen", 1.50, 7.50, 0.075, 512_000, None),
    ("gemini-4-hydrogen-tiered-high", "gemini-4-hydrogen", 1.50, 7.50, 0.075, 512_000, None),
    # Gemini 4 Neon (2026-11-18): $3 / $15 / $0.15, Antigravity's default model
    ("Gemini 4 Neon (High)", "gemini-4-neon", 3.0, 15.0, 0.15, 512_000, None),
    ("gemini-4-neon-xhigh", "gemini-4-neon", 3.0, 15.0, 0.15, 512_000, None),
    ("Gemini 3.8 Flash (High)", "gemini-3.8-flash", 0.75, 3.75, 0.075, 256_000, None),
    ("gemini-3.8-flash-tiered", "gemini-3.8-flash", 0.75, 3.75, 0.075, 256_000, None),
    ("Gemini 3.6 Flash (Low)", "gemini-3.6-flash", 0.75, 3.75, 0.075, 256_000, None),
    ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 128_000, None),
    ("Gemini 3.5 Flash Lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 128_000, None),
    ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite", 0.25, 1.50, 0.025, 128_000, None),
    ("Gemini 3.1 Pro (High)", "gemini-3-pro", 2.0, 12.0, 0.20, 256_000, None),
    ("gemini-3-flash-preview", "gemini-3-flash", 0.50, 3.0, 0.05, 256_000, None),
    ("gemini-2.5-pro", "gemini-2.5-pro", 1.25, 10.0, 0.125, 128_000, None),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite", 0.10, 0.40, 0.01, 128_000, None),
    ("gemini-2.5-flash", "gemini-2.5-flash", 0.30, 2.50, 0.03, 128_000, None),
    ("Claude Opus 5.5 (High)", "claude-opus-5.5", 4.0, 20.0, 0.20, 840_000, None),
    # Orchestrator: priced as its planner, the Hydrogen that carries out its plan as Hydrogen (see 5.)
    ("Orchestrator (Carbon)", "gemini-4-carbon", 2.0, 10.0, 0.10, 512_000, None),
    ("Orchestrator (Argon)", "gemini-4-argon", 2.0, 10.0, 0.10, 512_000, None),
    ("Orchestrator (Neon)", "gemini-4-neon", 3.0, 15.0, 0.15, 512_000, None),
    ("gemini-pro-orchestrator-neon", "gemini-4-neon", 3.0, 15.0, 0.15, 512_000, None),
    ("gemini-pro-orchestrator", "gemini-4-carbon", 2.0, 10.0, 0.10, 512_000, None),
    ("gemini-pro-orchestrator-argon", "gemini-4-argon", 2.0, 10.0, 0.10, 512_000, None),
    ("Gemini 4 Hydrogen (Orchestrator)", "gemini-4-hydrogen", 1.50, 7.50, 0.075, 512_000, None),
    ("gemini-flash-worker-agy", "gemini-4-hydrogen", 1.50, 7.50, 0.075, 512_000, None),
]
ts_nov = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)
for raw_name, want_id, want_in, want_out, want_cr, want_win, want_comp in expected:
    m = p.lookup(raw_name)
    r = p.rates(m, ts_nov) if m else {}
    check(f"lookup {raw_name} → {want_id}",
          m and m["id"] == want_id and r.get("input") == want_in and r.get("output") == want_out
          and r.get("cache_read") == want_cr and p.context_window(raw_name) == want_win
          and p.compaction_threshold(raw_name) == want_comp,
          f"got={m and m['id']} rates={r}")

# 2. Subagent model aliases match Antigravity 2.x tiers
check("subagent aliases: flash_lite / flash / pro",
      p.alias("flash_lite") == "gemini-3.5-flash-lite"
      and p.alias("flash") == "gemini-4-hydrogen"  # since 2026-11: subagents with Model: flash run Hydrogen (High)
      and p.alias("pro") == "gemini-4-argon",
      p.cfg.get("subagent_model_aliases"))

# 3. Dated price changes & auto-routing (Dec 1, 2026 and Jan 1, 2027)
ts_dec = dt.datetime(2026, 12, 15, 12, 0, tzinfo=dt.timezone.utc)
ts_jan = dt.datetime(2027, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
c_dec = usage.Call(1, ts_dec, "Gemini 4 Carbon (High)", 1_000_000, 1_000_000, 1_000_000, [], 1)
p.price_call(c_dec)
check("Gemini 4 Carbon price step from 2026-12-01 ($4 / $20 / $0.20)",
      abs(c_dec.cost - (4.0 + 0.20 + 20.0)) < 1e-6, c_dec.cost)

f38_jan = usage.Call(2, ts_jan, "Gemini 3.8 Flash (Medium)", 1_000_000, 1_000_000, 1_000_000, [], 1)
p.price_call(f38_jan)
check("Gemini 3.8 Flash price step from 2027-01-01 ($1.50 / $7.50 / $0.15)",
      abs(f38_jan.cost - (1.50 + 0.15 + 7.50)) < 1e-6, f38_jan.cost)

# Gemini 2.5 Pro >200k prompt: tiered before Dec 1, flat 3.8-flash rates on/after Dec 1 (long_context cleared)
p25_nov = usage.Call(3, ts_nov, "gemini-2.5-pro", 250_000, 0, 100_000, [], 1)
p25_dec = usage.Call(4, ts_dec, "gemini-2.5-pro", 250_000, 0, 100_000, [], 1)
p.price_call(p25_nov)
p.price_call(p25_dec)
check("Gemini 2.5 Pro >200k uses long_context in Nov 2026 and flat 3.8-flash rate in Dec 2026",
      abs(p25_nov.cost - (0.25 * 2.5 + 0.1 * 15.0)) < 1e-6
      and abs(p25_dec.cost - (0.25 * 0.75 + 0.1 * 3.75)) < 1e-6,
      (p25_nov.cost, p25_dec.cost))

# Service tiers: Contributor on Hydrogen ($0.75 / $3.75 / $0.0375), Batch on 3.1 Pro >200k ($2.0 / $9.0 / $0.40)
p_contrib = usage.Pricing(PRICING, service_tier="contributor")
hyd = usage.Call(5, ts_nov, "Gemini 4 Hydrogen (High)", 1_000_000, 1_000_000, 1_000_000, [], 1)
p_contrib.price_call(hyd)
check("Gemini 4 Hydrogen contributor tier", abs(hyd.cost - (0.75 + 0.0375 + 3.75)) < 1e-6, hyd.cost)

p_batch = usage.Pricing(PRICING, service_tier="batch")
pro31 = usage.Call(6, ts_nov, "Gemini 3.1 Pro (High)", 250_000, 0, 100_000, [], 1)
p_batch.price_call(pro31)
check("Gemini 3.1 Pro batch long_context (>200k)", abs(pro31.cost - (0.25 * 2.0 + 0.1 * 9.0)) < 1e-6, pro31.cost)

# 4. End-to-end CLI output (--json, markdown, --html) shows Gemini 4's 512k window, with no separate compaction mark
cid = "11111111-2222-3333-4444-555555555555"
logs = os.path.join(SP, "brain", cid, ".system_generated", "logs")
os.makedirs(logs, exist_ok=True)
tf = os.path.join(logs, "transcript_full.jsonl")
steps = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-14T10:00:00Z",
     "content": "<USER_REQUEST>Проверь лимиты</USER_REQUEST>\nModel Selection` from None to Gemini 4 Carbon (High)."},
    {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-14T10:00:10Z",
     "input_tokens": 20_000, "cache_read_tokens": 210_000, "output_tokens": 1_500, "content": "Готово."},
]
with open(tf, "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n")

js = subprocess.run([sys.executable, os.path.join(USAGE_DIR, "usage.py"), "--transcript", tf, "--json"],
                    capture_output=True, text=True, encoding="utf-8")
data = json.loads(js.stdout)
ctx = data.get("context") or {}
check("usage.py --json reports window=512000 and no compaction threshold for Gemini 4",
      ctx.get("window") == 512_000 and "compaction" not in ctx and ctx.get("current") == 230_000,
      ctx)

html_out = os.path.join(SP, "usage.html")
md = subprocess.run([sys.executable, os.path.join(USAGE_DIR, "usage.py"), "--transcript", tf, "--html", html_out],
                    capture_output=True, text=True, encoding="utf-8")
html_body = open(html_out, encoding="utf-8").read() if os.path.isfile(html_out) else ""
check("usage.py markdown & HTML show 230k of the 512k window and no compaction mark",
      "230k / 512k" in md.stdout and "сжатие" not in md.stdout.split("**Контекст:**")[1].split("\n")[0]
      and "230k / 512k" in html_body and "сжатие ~" not in html_body,
      md.stdout[:300])

# 5. Orchestrator: the planner (Carbon) plans in plan mode and hands over with exit_plan_mode; Antigravity says the turn
#    «runs on the execution model» and Hydrogen carries the plan out until it hands back with enter_plan_mode. Without
#    the language server (its metadata names each call's model — test_delegate.py) these markers tell them apart.
for key in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
    os.environ.pop(key, None)
ocid = "22222222-3333-4444-5555-666666666666"
ologs = os.path.join(SP, "brain", ocid, ".system_generated", "logs")
os.makedirs(ologs, exist_ok=True)


def call_step(i, tools, out=100):
    return {"step_index": i, "type": "PLANNER_RESPONSE", "created_at": "2026-11-17T10:00:%02dZ" % i, "input_tokens": 1000,
            "cache_read_tokens": 0, "output_tokens": out, "tool_calls": [{"name": t, "args": {}} for t in tools]}


orch = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-17T10:00:00Z",
     "content": "<USER_REQUEST>Add a CLI</USER_REQUEST>\nModel Selection` from None to Orchestrator (Carbon)."},
    {"step_index": 1, "type": "SYSTEM_MESSAGE", "source": "SYSTEM_SDK", "created_at": "2026-11-17T10:00:01Z",
     "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\nThe following system "
                "message was injected by a SessionStart hook:\n<memory>A note: the hand-over says «Plan mode is off: this "
                "turn runs on the execution model».</memory>\n</SYSTEM_MESSAGE>"},  # quoted, not a hand-over
    call_step(2, ["read_file"]), call_step(3, ["exit_plan_mode"], out=5000),
    {"step_index": 4, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-17T10:00:04Z",
     "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as "
                "important information to pay attention to.\n\n<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on "
                "the execution model. Carry out the plan below now.\n<plan>…</plan>\n</SYSTEM_MESSAGE>"},
    call_step(5, ["write_file"]), call_step(6, ["run_command"]), call_step(7, ["enter_plan_mode"]),
    call_step(8, ["read_file"]),
    {"step_index": 9, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-17T10:00:09Z",
     "content": "<USER_REQUEST>And a --version flag</USER_REQUEST>"},
    call_step(10, []),
    {"step_index": 11, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-17T10:00:11Z",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
    call_step(12, []),
    # seen live: a follow-up from Claude Code (agentapi send-message) is taken by Hydrogen, not handed back
    {"step_index": 13, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-17T10:00:13Z",
     "content": "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-17T10:00:13Z sender=system priority=MESSAGE_PRIORITY_HIGH "
                "content=Follow-up from Claude Code: Add a --version flag.\n</SYSTEM_MESSAGE>"},
    call_step(14, ["edit_file"]),
]
with open(os.path.join(ologs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s, ensure_ascii=False) for s in orch) + "\n")
osess = usage.parse_session(ocid, ologs, p, [SP])
plan_m, work_m = "Orchestrator (Carbon)", "Gemini 4 Hydrogen (Orchestrator)"
check("Orchestrator: planner's calls (a quoted hand-over doesn't count), then Hydrogen's from the hand-over to "
      "enter_plan_mode, the planner on a new user message, Hydrogen again through a follow-up from Claude Code",
      [(c.step, c.model) for c in osess.calls] == [(2, plan_m), (3, plan_m), (5, work_m), (6, work_m), (7, work_m),
                                                   (8, plan_m), (10, plan_m), (12, work_m), (14, work_m)]
      and osess.models == [plan_m, work_m] and len(osess.turns) == 3, [(c.step, c.model) for c in osess.calls])
want = 3 * (0.002 + 0.001) + (0.002 + 0.05) + 5 * (0.0015 + 0.00075)  # Carbon $2/$10, Hydrogen $1.5/$7.5 per 1M
check("Orchestrator: each call at its own model's price", abs(sum(c.cost for c in osess.calls) - want) < 1e-9,
      sum(c.cost for c in osess.calls))
omd = usage.render_markdown(osess, p)
check("Orchestrator: the report splits the calls by model",
      f"**По моделям:** {plan_m} — 4 вызова" in omd and f"{work_m} — 5 вызовов" in omd, omd[:600])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
