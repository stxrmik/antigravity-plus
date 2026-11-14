"""/usage & pricing.json: Gemini 4 / 3.x / 2.5 models, ~300k window & ~250k compaction, tiers and price steps."""
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
    ("Gemini 4 Carbon (High)", "gemini-4-carbon", 2.0, 10.0, 0.10, 300_000, 250_000),
    ("Gemini 4 Argon (Medium)", "gemini-4-argon", 2.0, 10.0, 0.10, 300_000, 250_000),
    ("Gemini 4 Hydrogen (XHigh)", "gemini-4-hydrogen", 1.50, 7.50, 0.075, 300_000, 250_000),
    ("Gemini 3.8 Flash (High)", "gemini-3.8-flash", 0.75, 3.75, 0.075, 300_000, 250_000),
    ("gemini-3.8-flash-tiered", "gemini-3.8-flash", 0.75, 3.75, 0.075, 300_000, 250_000),
    ("Gemini 3.6 Flash (Low)", "gemini-3.6-flash", 0.75, 3.75, 0.075, 300_000, 250_000),
    ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 168_000, 128_000),
    ("Gemini 3.5 Flash Lite", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, 168_000, 128_000),
    ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite", 0.25, 1.50, 0.025, 168_000, 128_000),
    ("Gemini 3.1 Pro (High)", "gemini-3-pro", 2.0, 12.0, 0.20, 300_000, 250_000),
    ("gemini-3-flash-preview", "gemini-3-flash", 0.50, 3.0, 0.05, 300_000, 250_000),
    ("gemini-2.5-pro", "gemini-2.5-pro", 1.25, 10.0, 0.125, 300_000, 250_000),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite", 0.10, 0.40, 0.01, 168_000, 128_000),
    ("gemini-2.5-flash", "gemini-2.5-flash", 0.30, 2.50, 0.03, 300_000, 250_000),
    ("Claude Opus 5.5 (High)", "claude-opus-5.5", 4.0, 20.0, 0.20, 1_000_000, 840_000),
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
      and p.alias("flash") == "gemini-3.8-flash"
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

# 4. End-to-end CLI output (--json, markdown, --html) shows 300k window and ~250k compaction for Gemini
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
check("usage.py --json reports window=300000 and compaction=250000 for Gemini",
      ctx.get("window") == 300_000 and ctx.get("compaction") == 250_000 and ctx.get("current") == 230_000,
      ctx)

html_out = os.path.join(SP, "usage.html")
md = subprocess.run([sys.executable, os.path.join(USAGE_DIR, "usage.py"), "--transcript", tf, "--html", html_out],
                    capture_output=True, text=True, encoding="utf-8")
html_body = open(html_out, encoding="utf-8").read() if os.path.isfile(html_out) else ""
check("usage.py markdown & HTML include 300k limit and ~250k compaction",
      "230k / 300k" in md.stdout and "(сжатие ~250k)" in md.stdout and "сжатие ~250k" in html_body,
      md.stdout[:300])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
