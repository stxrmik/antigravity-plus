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
    ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 512_000, None),
    ("Gemini 3.5 Flash Lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 512_000, None),
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
      and p.alias("pro") == "gemini-4-hydrogen",   # verified 2026-11-24: Model: pro runs Hydrogen (XHigh)
      p.cfg.get("subagent_model_aliases"))

# 2b. GPT-OSS 120B keeps its 80k window while remaining unpriced, and AGY_CLI_AUTO_COMPACT_OVERRIDE works
check("GPT-OSS 120B has 80k window and is marked unpriced",
      p.context_window("GPT-OSS 120B (Medium)") == 80_000 and not p.is_priced("GPT-OSS 120B (Medium)"),
      (p.context_window("GPT-OSS 120B (Medium)"), p.is_priced("GPT-OSS 120B (Medium)")))
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "80%"
check("AGY_CLI_AUTO_COMPACT_OVERRIDE=80% computes 409600 for 512k Gemini 4",
      p.compaction_threshold("gemini-4-neon-high") == 409_600, p.compaction_threshold("gemini-4-neon-high"))
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "400K"
check("AGY_CLI_AUTO_COMPACT_OVERRIDE=400K computes 400000",
      p.compaction_threshold("gemini-4-neon-high") == 400_000, p.compaction_threshold("gemini-4-neon-high"))
os.environ.pop("AGY_CLI_AUTO_COMPACT_OVERRIDE", None)

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

# 6. Configurable Orchestrator (plan_execute:true annotation + Model Selection = Claude Opus 5.5 (High), executor = Claude Sonnet 5.5 (High))
ccid = "33333333-4444-5555-6666-777777777777"
clogs = os.path.join(SP, "brain", ccid, ".system_generated", "logs")
cann = os.path.join(SP, "annotations")
os.makedirs(clogs, exist_ok=True)
os.makedirs(cann, exist_ok=True)
with open(os.path.join(cann, f"{ccid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"Configurable" plan_execute:true')
corch = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-24T10:00:00Z",
     "content": "<USER_REQUEST>Refactor</USER_REQUEST>\nModel Selection` from None to Claude Opus 5.5 (High)."},
    call_step(1, ["read_file"]),
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-24T10:00:02Z",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on Claude Sonnet 5.5 (High). Carry out the plan.\n</SYSTEM_MESSAGE>"},
    call_step(3, ["edit_file"]),
    {"step_index": 4, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-24T10:00:04Z",
     "content": "<SYSTEM_MESSAGE>\n[plan review] Plan mode is on: this turn runs on Claude Opus 5.5 (High).\n</SYSTEM_MESSAGE>"},
    call_step(5, ["read_file"]),
]
with open(os.path.join(clogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s, ensure_ascii=False) for s in corch) + "\n")
csess = usage.parse_session(ccid, clogs, p, [SP])
check("configurable Orchestrator splits planner (Opus 5.5), executor (Sonnet 5.5), and [plan review] handback",
      [(c.step, c.model) for c in csess.calls] == [
          (1, "Claude Opus 5.5 (High)"),
          (3, "Claude Sonnet 5.5 (High)"),
          (5, "Claude Opus 5.5 (High)"),
      ], [(c.step, c.model) for c in csess.calls])

# 7. 2.35.1 updates: hyphenated Claude 5.5 ids, nanosecond timestamps, batch invoke_subagent, and execution model (Claude Sonnet 5.5)
for raw_c55, want_c55 in [("claude-sonnet-5-5-high", "claude-sonnet-5.5"), ("claude-opus-5-5-low", "claude-opus-5.5")]:
    mc = p.lookup(raw_c55)
    check(f"Pricing.lookup('{raw_c55}') resolves & is_priced",
          bool(mc) and mc["id"] == want_c55 and p.is_priced(raw_c55) is True, (mc, p.is_priced(raw_c55)))

ts_ns = usage.parse_ts("2026-11-25T01:02:03.123456789Z")
check("usage.parse_ts parses nanosecond ISO-8601 timestamp",
      ts_ns is not None and (ts_ns.year, ts_ns.month, ts_ns.day, ts_ns.hour, ts_ns.minute, ts_ns.second, ts_ns.microsecond)
      == (2026, 11, 25, 1, 2, 3, 123456) and ts_ns.tzinfo == dt.timezone.utc, ts_ns)

# Batch invoke_subagent with Subagents array + multi-UUID result
bcid = "44444444-5555-6666-7777-888888888888"
sub1_id = "aaaaaaaa-1111-2222-3333-444444444441"
sub2_id = "bbbbbbbb-1111-2222-3333-444444444442"
for sid, smod, scost_in in ((sub1_id, "flash", 500), (sub2_id, "pro", 800)):
    slogs = os.path.join(SP, "brain", sid, ".system_generated", "logs")
    os.makedirs(slogs, exist_ok=True)
    with open(os.path.join(slogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 0, "type": "PLANNER_RESPONSE", "created_at": "2026-11-25T01:02:04Z",
                            "input_tokens": scost_in, "cache_read_tokens": 0, "output_tokens": 50}) + "\n")
blogs = os.path.join(SP, "brain", bcid, ".system_generated", "logs")
os.makedirs(blogs, exist_ok=True)
batch_steps = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:02:00Z",
     "content": "<USER_REQUEST>Run parallel checks</USER_REQUEST>\nModel Selection` from None to Gemini 4 Neon (High)."},
    {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-25T01:02:03.123456789Z",
     "input_tokens": 1000, "cache_read_tokens": 0, "output_tokens": 100,
     "tool_calls": [{"name": "invoke_subagent", "args": {"Subagents": [
         {"Role": "Codebase Explorer", "TypeName": "explorer", "Model": "flash", "Prompt": "Find A"},
         {"Role": "API Researcher", "TypeName": "researcher", "Model": "pro", "Prompt": "Check B"},
     ]}}]},
    {"step_index": 2, "type": "GENERIC", "created_at": "2026-11-25T01:02:05Z",
     "content": f"Started 2 subagents:\n- {sub1_id}\n- {sub2_id}\n"},
]
with open(os.path.join(blogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s, ensure_ascii=False) for s in batch_steps) + "\n")
bsess = usage.parse_session(bcid, blogs, p, [SP])
check("batch invoke_subagent (Subagents array + multi-UUID result) parses all subagents",
      [(sa.conv_id, sa.role, sa.type_name, sa.model_arg, bool(sa.session)) for sa in bsess.subagents] == [
          (sub1_id, "Codebase Explorer", "explorer", "flash", True),
          (sub2_id, "API Researcher", "researcher", "pro", True),
      ], bsess.subagents)

# "Plan mode is off: this turn runs on the execution model (Claude Sonnet 5.5)." prices executor calls as Claude Sonnet 5.5
ecid = "55555555-6666-7777-8888-999999999999"
elogs = os.path.join(SP, "brain", ecid, ".system_generated", "logs")
os.makedirs(elogs, exist_ok=True)
with open(os.path.join(cann, f"{ecid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"ExecParen" plan_execute:true')
eorch = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:00:00Z",
     "content": "<USER_REQUEST>Build</USER_REQUEST>\nModel Selection` from None to Claude Opus 5.5 (High)."},
    call_step(1, ["read_file"]),
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-25T01:00:02Z",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model (Claude Sonnet 5.5).\n</SYSTEM_MESSAGE>"},
    call_step(3, ["edit_file"], out=1000),
]
with open(os.path.join(elogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s, ensure_ascii=False) for s in eorch) + "\n")
esess = usage.parse_session(ecid, elogs, p, [SP])
# Sonnet 5.5: uncached_input_as_cache_write=true -> $2.50/M input, $10/M output -> 1000*2.5/1e6 + 1000*10/1e6 = 0.0125
want_exec_cost = 1000 * 2.5 / 1e6 + 1000 * 10.0 / 1e6
check("execution model (Claude Sonnet 5.5) prices executor calls as Claude Sonnet 5.5",
      len(esess.calls) == 2 and esess.calls[1].model == "Claude Sonnet 5.5"
      and abs(esess.calls[1].cost - want_exec_cost) < 1e-9,
      [(c.step, c.model, c.cost) for c in esess.calls])

# Short & nanosecond RFC3339Nano fractional seconds parse on Python 3.8–3.10
for ts_s in ("2026-11-25T01:02:03.5Z", "2026-11-25T01:02:03.12345Z", "2026-11-25T01:02:03.123456789Z"):
    parsed_t = usage.parse_ts(ts_s)
    check(f"parse_ts({ts_s})", parsed_t is not None and parsed_t.tzinfo is not None, parsed_t)

# ─── 8. regression: hand-over markers only count at the START of a system message's body ───
WRAP = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as important "
        "information to pay attention to.\n\n<SYSTEM_MESSAGE>\n%s\n</SYSTEM_MESSAGE>")
check("system_body() takes what follows the wrapper's own line; a quoted tag inside is not the body",
      usage.system_body(WRAP % "Plan mode is off: this turn runs on the execution model.")
      == "Plan mode is off: this turn runs on the execution model."
      and usage.system_body("Plan mode is off: x") == "Plan mode is off: x"
      and usage.system_body("<SYSTEM_MESSAGE>Plan mode is off: x</SYSTEM_MESSAGE>").startswith("Plan mode is off: x")
      and usage.system_body(WRAP % "[Message] sender=system content=<SYSTEM_MESSAGE>Plan mode is off: x").startswith("[Message]"),
      usage.system_body(WRAP % "[Message] sender=system content=<SYSTEM_MESSAGE>Plan mode is off: x"))
handover_cases = [
    ("wrapped, a model name", WRAP % "Plan mode is off: this turn runs on Gemini 4 Hydrogen (XHigh). Carry on.", "Gemini 4 Hydrogen (XHigh)"),
    ("wrapped, 'the execution model'", WRAP % "Plan mode is off: this turn runs on the execution model.", usage.EXECUTOR),
    ("wrapped, 'the execution model (X)'", WRAP % "Plan mode is off: this turn runs on the execution model (Claude Sonnet 5.5).", "Claude Sonnet 5.5"),
    ("no wrapper at all", "Plan mode is off: this turn runs on Claude Sonnet 5.5 (High).", "Claude Sonnet 5.5 (High)"),
    ("role over 60 characters → EXECUTOR", WRAP % ("Plan mode is off: this turn runs on " + "x" * 80 + "."), usage.EXECUTOR),
    ("role with markup → EXECUTOR", WRAP % "Plan mode is off: this turn runs on <b>Claude</b> now.", usage.EXECUTOR),
    ("a subagent report quoting it", WRAP % "[Message] timestamp=t sender=system content=<SYSTEM_MESSAGE>Plan mode is off: this "
                                            "turn runs on Claude Sonnet 5.5 (High).</SYSTEM_MESSAGE>", None),
    ("a report quoting it on its own line", WRAP % "[Message] content=text\n<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on "
                                                  "Claude Sonnet 5.5 (High).\n</SYSTEM_MESSAGE>", None),
    ("a hook context quoting it", "The following is a <SYSTEM_MESSAGE> x.\n\n<SYSTEM_MESSAGE>\nSessionStart: a note says "
                                  "«Plan mode is off: this turn runs on the execution model».\n</SYSTEM_MESSAGE>", None),
]
for label, text, want in handover_cases:
    got = usage.handover_executor(text)
    check(f"handover_executor: {label}", got == want, got)
check("is_plan_review: '[plan review]' opens the body; '[plan review due]' and a quote don't",
      usage.is_plan_review(WRAP % "[plan review] Plan mode is on: this turn runs on the planning model.")
      and not usage.is_plan_review(WRAP % "[plan review due] Plan mode is on.")
      and not usage.is_plan_review(WRAP % "[Message] content=<SYSTEM_MESSAGE>[plan review] x</SYSTEM_MESSAGE>"))

qcid = "66666666-7777-8888-9999-000000000000"
qlogs = os.path.join(SP, "brain", qcid, ".system_generated", "logs")
os.makedirs(qlogs, exist_ok=True)
qsteps = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:00:00Z",
     "content": "<USER_REQUEST>Review</USER_REQUEST>\nModel Selection` from None to Gemini 4 Hydrogen (High)."},
    call_step(1, ["invoke_subagent"], out=1000),
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-25T01:00:02Z",
     "content": WRAP % ("[Message] timestamp=t sender=agent content=<SYSTEM_MESSAGE>Plan mode is off: this turn runs on "
                        "Claude Sonnet 5.5 (High).</SYSTEM_MESSAGE> and [plan review] too")},
    call_step(3, ["read_file"], out=1000),
]
with open(os.path.join(qlogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s_, ensure_ascii=False) for s_ in qsteps) + "\n")
qsess = usage.parse_session(qcid, qlogs, p, [SP])
check("an ordinary conversation whose subagent report quotes a hand-over stays on its own model (not plan-execute)",
      not usage._transcript_has_plan_execute(qsteps)
      and [(c.step, c.model) for c in qsess.calls] == [(1, "Gemini 4 Hydrogen (High)"), (3, "Gemini 4 Hydrogen (High)")],
      [(c.step, c.model) for c in qsess.calls])
check("…while a real hand-over and a real [plan review] still switch a conversation to plan-execute",
      usage._transcript_has_plan_execute([{"type": "SYSTEM_MESSAGE", "source": "SYSTEM",
                                           "content": WRAP % "Plan mode is off: this turn runs on the execution model."}])
      and usage._transcript_has_plan_execute([{"type": "SYSTEM_MESSAGE", "source": "SYSTEM",
                                               "content": WRAP % "[plan review] Plan mode is on: this turn runs on the planning model."}]),
      "")

# ─── 9. regression: annotations — the title may say anything ───
acid_ok, acid_title, acid_off = ("77777777-8888-9999-0000-111111111111", "77777777-8888-9999-0000-222222222222",
                                 "77777777-8888-9999-0000-333333333333")
pbtxt_cases = [
    (acid_ok, 'title:"Plain" last_user_view_time:{seconds:1 nanos:2} plan_execute:true', True),
    (acid_title, 'title:"plan_execute:true parsing" last_user_view_time:{seconds:1 nanos:2}', False),
    (acid_off, 'title:"say \\"plan_execute:true\\" now" plan_execute:false', False),
    ("77777777-8888-9999-0000-444444444444", 'title:"x"\nplan_execute: true\n', True),
    ("77777777-8888-9999-0000-555555555555", 'title:"x" view:{plan_execute:true}', True),
    ("77777777-8888-9999-0000-666666666666", 'title:"x" not_plan_execute:true my_plan_execute:true', False),
]
for acid, text, want in pbtxt_cases:
    with open(os.path.join(cann, f"{acid}.pbtxt"), "w", encoding="utf-8") as f:
        f.write(text)
    got = usage._annotation_plan_execute(acid, "", [SP])
    check(f"annotation {text[:48]!r} → plan_execute={want}", got is want, got)

# ─── 10. regression: _discover_ls() never scans processes (isolated HOME stays offline) ───
import compat  # noqa: E402  (usage.py put hooks/ on sys.path)
touched = []
real_ls, real_processes = compat.language_server, compat.processes
compat.language_server = lambda *a, **k: touched.append("language_server")
compat.processes = lambda *a, **k: touched.append("processes") or []
saved_env = {k_: os.environ.pop(k_, None) for k_ in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "AGP_USAGE_NO_LS")}
try:
    usage._LS_CACHE = usage._LS_UNSET
    found_ls = usage._discover_ls()
finally:
    compat.language_server, compat.processes = real_ls, real_processes
    usage._LS_CACHE = usage._LS_UNSET
    for k_, v_ in saved_env.items():
        if v_ is not None:
            os.environ[k_] = v_
check("_discover_ls(): no hub file → ('', '') without compat.language_server() or a process scan",
      found_ls == ("", "") and touched == [], (found_ls, touched))

# ─── 11. regression: the executor that was active when the user wrote still makes its own enter_plan_mode call ───
hcid = "88888888-9999-0000-1111-222222222222"
hlogs = os.path.join(SP, "brain", hcid, ".system_generated", "logs")
os.makedirs(hlogs, exist_ok=True)
with open(os.path.join(cann, f"{hcid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"Handback" plan_execute:true')
hsteps = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:00:00Z",
     "content": "<USER_REQUEST>Build</USER_REQUEST>\nModel Selection` from None to Claude Opus 5.5 (High)."},
    call_step(1, ["read_file"]),
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-25T01:00:02Z",
     "content": WRAP % "Plan mode is off: this turn runs on Claude Sonnet 5.5 (High)."},
    call_step(3, ["edit_file"]),
    {"step_index": 4, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:00:04Z",
     "content": "<USER_REQUEST>Actually, change course</USER_REQUEST>"},
    call_step(5, ["enter_plan_mode"]),          # the executor hands back: still its call
    call_step(6, ["read_file"]),                # now the planner
    {"step_index": 7, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T01:00:07Z",
     "content": "<USER_REQUEST>And again</USER_REQUEST>"},
    call_step(8, ["enter_plan_mode"]),          # the executor was not active at this message: the planner's call
]
with open(os.path.join(hlogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s_, ensure_ascii=False) for s_ in hsteps) + "\n")
hsess = usage.parse_session(hcid, hlogs, p, [SP])
opus_h, sonnet_h = "Claude Opus 5.5 (High)", "Claude Sonnet 5.5 (High)"
check("after a user message the executor's enter_plan_mode stays priced as the executor, the next call as the planner",
      [(c.step, c.model) for c in hsess.calls] == [(1, opus_h), (3, sonnet_h), (5, sonnet_h), (6, opus_h), (8, opus_h)],
      [(c.step, c.model) for c in hsess.calls])

# ─── 12. regression: subagent ids come from invoke_subagent results, not from other tools' output ───
rs_real, rs_other, rs_second = ("bbbbbbbb-1111-2222-3333-aaaaaaaa0001", "bbbbbbbb-1111-2222-3333-aaaaaaaa0002",
                                "bbbbbbbb-1111-2222-3333-aaaaaaaa0003")
ts_head = "Created At: 2026-11-25T02:36:36+03:00\nCompleted At: 2026-11-25T02:37:05+03:00\n"
ok_result = ts_head + 'Created the following subagent:\n{\n  "conversationId": "%s",\n  "workspaceUris": []\n}\nIt will message you.'
rcid = "99999999-0000-1111-2222-333333333333"
rlogs = os.path.join(SP, "brain", rcid, ".system_generated", "logs")
os.makedirs(rlogs, exist_ok=True)


def generic(i, content):
    return {"step_index": i, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-25T02:00:%02dZ" % i, "content": content}


def tool(name, **args):
    return {"name": name, "args": args}


rsteps = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-25T02:00:00Z",
     "content": "<USER_REQUEST>Fan out</USER_REQUEST>\nModel Selection` from None to Gemini 4 Neon (High)."},
    {**call_step(1, []), "tool_calls": [tool("run_command", CommandLine="cat log"), tool("read_file", Path="/x"),
                                         tool("invoke_subagent", Role="First", TypeName="explorer", Model="flash")]},
    generic(2, '{"conversationId": "%s"}' % rs_other),                                      # a command's output
    generic(3, 'a file quoting «Created the following subagent:» and {"conversationId": "%s"}' % rs_other),
    generic(4, ok_result % rs_real),
    {**call_step(5, []), "tool_calls": [tool("invoke_subagent", Role="Failing", TypeName="worker", Model="pro"),
                                         tool("invoke_subagent", Role="Working", TypeName="researcher", Model="flash_lite")]},
    generic(6, "Error: could not create the subagent"),                                      # the first one failed
    generic(7, ok_result % rs_second),
]
with open(os.path.join(rlogs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(s_, ensure_ascii=False) for s_ in rsteps) + "\n")
rsess = usage.parse_session(rcid, rlogs, p, [SP])
check("only invoke_subagent results name subagents: another tool's conversationId takes no args, a failed call's args "
      "don't go to the next call's subagent",
      [(sa.conv_id, sa.role, sa.model_arg) for sa in rsess.subagents] == [(rs_real, "First", "flash"),
                                                                           (rs_second, "Working", "flash_lite")],
      [(sa.conv_id, sa.role, sa.model_arg) for sa in rsess.subagents])

# ─── 13. pricing.json: aliases and the first-match order ───
for alias_name, want_label in (("flash_lite", "Gemini 3.5 Flash-Lite"), ("flash", "Gemini 4 Hydrogen"), ("pro", "Gemini 4 Hydrogen")):
    got_alias = p.lookup(p.alias(alias_name) or "")
    check(f"alias {alias_name} resolves to a priced entry ({want_label})",
          bool(got_alias) and got_alias["label"] == want_label and p.is_priced(p.alias(alias_name)), got_alias)
order_cases = [
    ("Gemini 4 Hydrogen (XHigh)", "gemini-4-hydrogen"), ("gemini-4-hydrogen-tiered-xhigh", "gemini-4-hydrogen"),
    ("gemini-flash-worker-agy", "gemini-4-hydrogen"), ("Gemini 3.5 Flash Lite", "gemini-3.5-flash-lite"),
    ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"), ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite"),
    ("gemini-2.5-pro", "gemini-2.5-pro"), ("GPT-OSS 120B (Medium)", "gpt-oss-120b"),
    ("claude-opus-5-5-high", "claude-opus-5.5"), ("Claude Opus 5.5 (High)", "claude-opus-5.5"),
    ("claude-sonnet-5-5-low", "claude-sonnet-5.5"), ("claude-opus-4-5", "claude-opus-4.5"),
    ("claude-sonnet-4-5", "claude-sonnet-4"), ("gemini-4-neon-medium", "gemini-4-neon"),
    ("gemini-pro-orchestrator-neon", "gemini-4-neon"), ("Orchestrator (Carbon)", "gemini-4-carbon"),
]
for raw_name, want_id in order_cases:
    got_m = p.lookup(raw_name)
    check(f"first match for {raw_name} is {want_id}", bool(got_m) and got_m["id"] == want_id, got_m and got_m["id"])
for m_ in p.models:
    check(f"entry {m_['id']}: its id and label resolve to itself (no earlier 'match' shadows it)",
          p.lookup(m_["id"])["id"] == m_["id"] and p.lookup(m_["label"])["id"] == m_["id"],
          (p.lookup(m_["id"])["id"], p.lookup(m_["label"])["id"]))

# ─── 14. speed.py and usage.py read AGY_CLI_AUTO_COMPACT_OVERRIDE (an Antigravity CLI setting) the same way ───
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import speed  # noqa: E402
raw_values = ["", " ", "700000", "800K", "800k", "0.5M", "80%", " 80 % ", "0", "0%", "100%", "101%", "abc", "1e5", "-5", " 400k ",
              "999999999", "512000", "511999", "0.001M", "100", "1.5k", "50.5%", "k", "%"]
bad_pairs = [(r_, w_) for r_ in raw_values for w_ in (512_000, 256_000, 128_000, 80_000, 0, -1)
             if speed._parse_compact_override(r_, w_) != usage.parse_compact_override(r_, w_)]
check("speed._parse_compact_override ≡ usage.parse_compact_override on a table of values and windows", not bad_pairs, bad_pairs[:5])
os.environ["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = "400K"
check("the compaction-limit note says the override is an Antigravity CLI setting",
      "Antigravity CLI" in speed.ctx_limits("gemini-4-neon-high")[2] and "400k compaction limit" in speed.ctx_limits("gemini-4-neon-high")[2],
      speed.ctx_limits("gemini-4-neon-high"))
os.environ.pop("AGY_CLI_AUTO_COMPACT_OVERRIDE", None)
for doc_path in (os.path.join(PLUGIN, "skills", "usage", "SKILL.md"), PRICING):
    with open(doc_path, encoding="utf-8") as f_:
        doc_text = f_.read()
    at = doc_text.find("AGY_CLI_AUTO_COMPACT_OVERRIDE")
    check(f"{os.path.basename(doc_path)}: the override is described as an Antigravity CLI setting",
          at >= 0 and "CLI" in doc_text[max(0, at - 90):at + 110], doc_text[max(0, at - 90):at + 110])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
