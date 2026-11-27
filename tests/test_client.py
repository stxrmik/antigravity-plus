"""Tests for antigravity-plus/hooks/client.py (model catalog, cache, refresh, and resolution)."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_VERSION"):
    os.environ.pop(var, None)
sys.path.insert(0, HOOKS)
import client  # noqa: E402
import compat  # noqa: E402

real_processes = compat.processes
compat.processes = lambda name: []  # never touch a real Antigravity process

CACHE = client.cache_path()
ok = True


def check(name, cond, got=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:600]}"))


def reset_cache():
    for suffix in ("", ".attempt", ".lock"):
        p = CACHE + suffix
        if os.path.exists(p):
            os.remove(p)
    client._memo.clear()


def write_cache(data, mtime_offset=0.0):
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, ensure_ascii=False)
    if mtime_offset:
        t = time.time() + mtime_offset
        os.utime(CACHE, (t, t))
    client._memo.clear()


# ─────────────────────────────── 1. bundled snapshot ───────────────────────────────
reset_cache()
bundled_path = os.path.join(HOOKS, "client-snapshot.json")
with open(bundled_path, encoding="utf-8") as f:
    bundled_raw = f.read()
bundled_json = json.loads(bundled_raw)
forbidden = ("quotaInfo", "remainingFraction", "email", "@", "planStatus", "userTier")
leaked = [tok for tok in forbidden if tok in bundled_raw]
check("bundled client-snapshot.json: valid v1, source bundled, has models, no personal/quota fields",
      bundled_json.get("v") == 1 and bundled_json.get("source") == "bundled"
      and isinstance(bundled_json.get("models"), dict) and len(bundled_json["models"]) > 0 and not leaked,
      (bundled_json.get("v"), bundled_json.get("source"), len(bundled_json.get("models") or {}), leaked))
snap_bundled = client.snapshot()
check("snapshot(): falls back to bundled file with source 'bundled' when cache is absent",
      snap_bundled.get("source") == "bundled" and len(snap_bundled.get("models") or {}) > 0, snap_bundled.get("source"))


# ─────────────────────────────── 2. fixture & normalize() ───────────────────────────────
def make_model(label, mid_enum, provider="MODEL_PROVIDER_GOOGLE", window=512000,
               recommended=False, tag="", note="", extra_exps=None):
    exps = {}
    if window is not None:
        exps[client.CHECKPOINTER] = {"stringValue": json.dumps({"max_token_limit": str(window)})}
    if extra_exps:
        exps.update(extra_exps)
    entry = {
        "displayName": label,
        "model": mid_enum,
        "recommended": recommended,
        "tagTitle": tag,
        "tagDescription": note,
        "modelProvider": provider,
        "quotaInfo": {"remainingFraction": 0.75, "resetTime": "2030-01-01T00:00:00Z"},
    }
    if exps:
        entry["modelExperiments"] = {"experiments": exps}
    return entry


FIXTURE = {
    "name": "Test User",
    "email": "user@example.com",
    "userTier": "pro",
    "planStatus": "active",
    "response": {
        "name": "Test User",
        "email": "user@example.com",
        "userTier": "pro",
        "planStatus": "active",
        "models": {
            "gemini-4-hydrogen-low": make_model("Gemini 4 Hydrogen (Low)", "MODEL_PLACEHOLDER_M101"),
            "gemini-4-hydrogen-medium": make_model("Gemini 4 Hydrogen (Medium)", "MODEL_PLACEHOLDER_M102"),
            "gemini-4-hydrogen-high": make_model("Gemini 4 Hydrogen (High)", "MODEL_PLACEHOLDER_M103", recommended=True),
            "gemini-4-hydrogen-xhigh": make_model("Gemini 4 Hydrogen (XHigh)", "MODEL_PLACEHOLDER_M104"),
            "gemini-4-neon-low": make_model("Gemini 4 Neon (Low)", "MODEL_PLACEHOLDER_M201"),
            "gemini-4-neon-medium": make_model("Gemini 4 Neon (Medium)", "MODEL_PLACEHOLDER_M202"),
            "gemini-4-neon-high": make_model("Gemini 4 Neon (High)", "MODEL_PLACEHOLDER_M203", recommended=True),
            "gemini-4-neon-xhigh": make_model("Gemini 4 Neon (XHigh)", "MODEL_PLACEHOLDER_M204"),
            "claude-sonnet-5-5-low": make_model(
                "Claude Sonnet 5.5 (Low)", "MODEL_PLACEHOLDER_M301", provider="MODEL_PROVIDER_ANTHROPIC",
                window=256000, tag="New",
                extra_exps={"CASCADE_USE_EXPERIMENT_CHECKPOINTER/claude-1m_840k": {"stringValue": '{"max_token_limit": "840000"}'}},
            ),
            "claude-sonnet-5-5-medium": make_model(
                "Claude Sonnet 5.5 (Medium)", "MODEL_PLACEHOLDER_M302", provider="MODEL_PROVIDER_ANTHROPIC",
                window=256000, tag="New",
                extra_exps={"CASCADE_USE_EXPERIMENT_CHECKPOINTER/claude-1m_840k": {"stringValue": '{"max_token_limit": "840000"}'}},
            ),
            "claude-sonnet-5-5-high": make_model(
                "Claude Sonnet 5.5 (High)", "MODEL_PLACEHOLDER_M303", provider="MODEL_PROVIDER_ANTHROPIC",
                window=256000, tag="New",
                extra_exps={"CASCADE_USE_EXPERIMENT_CHECKPOINTER/claude-1m_840k": {"stringValue": '{"max_token_limit": "840000"}'}},
            ),
            "gemini-pro-orchestrator": make_model(
                "Orchestrator (Carbon)", "MODEL_PLACEHOLDER_M601",
                extra_exps={client.PLAN_EXECUTE: {"stringValue": '{"executionModel":"MODEL_PLACEHOLDER_M669"}'}},
            ),
            "gemini-pro-orchestrator-neon": make_model("Orchestrator (Neon)", "MODEL_PLACEHOLDER_M602"),
            "gemini-flash-worker-agy": make_model("Gemini 4 Hydrogen (Orchestrator)", "MODEL_PLACEHOLDER_M669"),
            "gemini-4-hydrogen-sonar": make_model("Gemini 4 Hydrogen Sonar", "MODEL_PLACEHOLDER_M701", window=None),
            "gemini-4-hydrogen-tiered-high": make_model("Gemini 4 Hydrogen Tiered (High)", "MODEL_PLACEHOLDER_M702"),
            "gemini-3.8-flash-medium": make_model(
                "Gemini 3.8 Flash (Medium)", "MODEL_PLACEHOLDER_M801", window=256000,
                tag="Leaving Soon", note="Retiring on Nov 30",
            ),
            "tab_autocomplete": make_model("Tab Autocomplete", "MODEL_PLACEHOLDER_M900"),
            "standalone-no-family": make_model("Standalone Model", "MODEL_PLACEHOLDER_M901", provider="MODEL_PROVIDER_OPENAI"),
            "gemini-2.5-flash": make_model("Gemini 3.5 Flash Lite", "MODEL_PLACEHOLDER_M910", window=128000),
            "gemini-2.5-flash-lite": make_model("Gemini 3.5 Flash Lite", "MODEL_PLACEHOLDER_M911", window=128000),
            "gemini-3.5-flash-lite": make_model("Gemini 3.5 Flash Lite", "MODEL_PLACEHOLDER_M912", window=512000),
        },
        "defaultAgentModelId": "gemini-4-neon-high",
        "agentModelSorts": [{"groups": [{"modelIds": [
            "gemini-4-neon-high", "gemini-4-neon-low", "gemini-4-hydrogen-high", "gemini-4-hydrogen-low",
            "gemini-pro-orchestrator", "gemini-pro-orchestrator-neon",
        ]}]}],
        "tieredModelIds": {
            "flashLite": ["gemini-4-hydrogen-low"],
            "flash": ["gemini-4-hydrogen-tiered-high"],
            "pro": ["gemini-4-neon-high"],
        },
        "groupedModels": {
            "gemini-4-hydrogen": {
                "displayName": "Gemini 4 Hydrogen",
                "supportedThinkingLevels": [{"thinkingSuffix": s} for s in ("low", "medium", "high", "xhigh")],
            },
            "gemini-4-neon": {
                "displayName": "Gemini 4 Neon",
                "supportedThinkingLevels": [{"thinkingSuffix": s} for s in ("low", "medium", "high", "xhigh")],
            },
            "claude-sonnet-5-5": {
                "displayName": "Claude Sonnet 5.5",
                "supportedThinkingLevels": [{"thinkingSuffix": s} for s in ("low", "medium", "high")],
            },
            "gemini-3-carbon": {
                "displayName": "Gemini 3 Carbon",
                "supportedThinkingLevels": [{"thinkingSuffix": "high"}],
            },
        },
    },
}

norm = client.normalize(FIXTURE, client="2.35.2", source="desktop")
norm_dump = json.dumps(norm)
norm_leaked = [tok for tok in (*forbidden, "Test User") if tok in norm_dump]
ms = norm.get("models") or {}
check("normalize(): top-level metadata, executor, aliases, sort, default, no personal/quota fields",
      norm.get("v") == 1 and norm.get("client") == "2.35.2" and norm.get("source") == "desktop"
      and norm.get("default") == "gemini-4-neon-high"
      and norm.get("aliases") == {"flash_lite": "gemini-4-hydrogen-low", "flash": "gemini-4-hydrogen-tiered-high", "pro": "gemini-4-neon-high"}
      and norm.get("executor") == {"id": "gemini-flash-worker-agy", "label": "Gemini 4 Hydrogen (Orchestrator)"}
      and (norm.get("sort") or [])[:2] == ["gemini-4-neon-high", "gemini-4-neon-low"]
      and not norm_leaked,
      (norm.get("aliases"), norm.get("executor"), norm_leaked))

claude_h = ms.get("claude-sonnet-5-5-high") or {}
flash_m = ms.get("gemini-3.8-flash-medium") or {}
sonar = ms.get("gemini-4-hydrogen-sonar") or {}
orch_c = ms.get("gemini-pro-orchestrator") or {}
standalone = ms.get("standalone-no-family") or {}
check("normalize(): family, effort, window override, tag, note, provider, tab_ dropped, missing checkpointer → None",
      "tab_autocomplete" not in ms
      and claude_h.get("family") == "claude-sonnet-5-5" and claude_h.get("effort") == "high"
      and claude_h.get("provider") == "anthropic" and claude_h.get("window") == 840000 and claude_h.get("tag") == "New"
      and flash_m.get("tag") == "Leaving Soon" and flash_m.get("note") == "Retiring on Nov 30"
      and sonar.get("window") is None
      and orch_c.get("family") == "orchestrator" and orch_c.get("effort") == "carbon"
      and standalone.get("family") == "standalone-no-family" and standalone.get("effort") == "" and standalone.get("provider") == "openai"
      and (norm.get("families") or {}).get("gemini-3-carbon", {}).get("efforts") == {},
      (claude_h, flash_m, sonar, orch_c, standalone))

partial = client.normalize({"response": {"models": {"m-one": {"displayName": "Model One"}}}})
check("normalize(): partial answer without groupedModels/sorts succeeds; empty answer → {}",
      "m-one" in (partial.get("models") or {}) and client.normalize({}) == {} and client.normalize(None) == {}, partial)

enum_ok = all(
    m.get("enum") == FIXTURE["response"]["models"][mid]["model"]
    for mid, m in ms.items()
)
check("normalize(): each model keeps its fixture enum", enum_ok,
      [(mid, m.get("enum")) for mid, m in list(ms.items())[:3]])


# ─────────────────────────────── 3. fake LS, stale(), refresh() ───────────────────────────────
CSRF = "tok-client"
ls_mode = "ok"


class LS(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, raw):
        data = raw if isinstance(raw, bytes) else json.dumps(raw).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {"status": "ok"}) if self.path == "/healthz" else self.reply(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-codeium-csrf-token") != CSRF:
            return self.reply(403, {"code": "permission_denied"})
        if not self.path.endswith("/GetAvailableModels"):
            return self.reply(200, {})
        if ls_mode == "500":
            return self.reply(500, {"error": "boom"})
        if ls_mode == "bad_json":
            return self.reply(200, b"{not json")
        if ls_mode == "empty":
            return self.reply(200, {"response": {"models": {}}})
        if ls_mode == "slow":
            time.sleep(0.35)
        self.reply(200, FIXTURE)


server = HTTPServer(("127.0.0.1", 0), LS)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]
DAEMON = compat.daemon_dir()
os.makedirs(DAEMON, exist_ok=True)
hub_path = os.path.join(DAEMON, f"hub_{PORT}.json")
with open(hub_path, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "httpPort": PORT, "httpsPort": 1, "lsVersion": "2.35.2", "csrfToken": CSRF}, f)

reset_cache()
check("stale(): bundled snapshot + running LS → True", client.stale() is True)

refreshed = client.refresh(force=True)
check("refresh(): fetches from fake LS, writes cache, fresh cache → stale() False",
      refreshed.get("source") == "desktop" and refreshed.get("client") == "2.35.2"
      and os.path.isfile(CACHE) and client.stale() is False,
      (refreshed.get("source"), refreshed.get("client"), client.stale()))

old_snap = dict(refreshed, at=int(time.time()) - client.TTL - 10)
write_cache(old_snap, mtime_offset=-10)
check("stale(): cache `at` older than TTL → True", client.stale() is True)

mismatch_snap = dict(refreshed, client="2.35.1", at=int(time.time()))
write_cache(mismatch_snap, mtime_offset=-5)
check("stale(): cache `client` != hub lsVersion → True", client.stale() is True)

# Failure modes keep a good cache untouched
write_cache(refreshed, mtime_offset=-2)
with open(CACHE, encoding="utf-8") as f:
    good_bytes = f.read()

for mode_name, tout in (("500", 2.0), ("slow", 0.05), ("bad_json", 2.0), ("empty", 2.0)):
    ls_mode = mode_name
    got_snap = client.refresh(timeout=tout, force=True)
    with open(CACHE, encoding="utf-8") as f:
        after_bytes = f.read()
    check(f"refresh() failure ({mode_name}) keeps good cache untouched",
          after_bytes == good_bytes and got_snap.get("client") == "2.35.2",
          (len(after_bytes), len(good_bytes)))

ls_mode = "ok"
write_cache("{broken json", mtime_offset=2)
check("corrupt cache file = absent (bundled used)", client.snapshot().get("source") == "bundled")

write_cache(dict(refreshed, v=99), mtime_offset=4)
check("cache with v=99 = absent (bundled used)", client.snapshot().get("source") == "bundled")

# Concurrent refresh(force=True) in threads leaves valid JSON
reset_cache()
results = []
threads = [threading.Thread(target=lambda: results.append(client.refresh(force=True))) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
with open(CACHE, encoding="utf-8") as f:
    concurrent_disk = json.load(f)
check("concurrent refresh(force=True) in threads leaves a valid JSON cache",
      len(results) == 3 and all(r.get("client") == "2.35.2" for r in results)
      and concurrent_disk.get("v") == 1 and "gemini-4-neon-high" in (concurrent_disk.get("models") or {}),
      concurrent_disk.get("client"))


# ─────────────────────────────── 4. readers & resolve() ───────────────────────────────
# Ensure cache holds FIXTURE normalized snapshot
write_cache(norm, mtime_offset=6)

check("default_model(), version(), aliases(), executor(), window()",
      client.default_model() == "gemini-4-neon-high"
      and client.version() == "2.35.2"
      and client.aliases().get("pro") == "gemini-4-neon-high"
      and client.executor().get("id") == "gemini-flash-worker-agy"
      and client.window("claude-sonnet-5-5-high") == 840000
      and client.window("gemini-4-hydrogen-sonar") is None,
      (client.default_model(), client.version(), client.aliases(), client.executor(),
       client.window("claude-sonnet-5-5-high"), client.window("gemini-4-hydrogen-sonar")))

check("model(): by id, label, free key, bare family, and bare 'orchestrator' → Carbon",
      (client.model("gemini-4-hydrogen-low") or {}).get("id") == "gemini-4-hydrogen-low"
      and (client.model("Gemini 4 Hydrogen (Low)") or {}).get("id") == "gemini-4-hydrogen-low"
      and (client.model("gemini 4 hydrogen LOW") or {}).get("id") == "gemini-4-hydrogen-low"
      and (client.model("Gemini-4-Neon") or {}).get("id") == "gemini-4-neon-high"
      and (client.model("orchestrator") or {}).get("id") == "gemini-pro-orchestrator"
      and (client.model("model_placeholder_m669") or {}).get("id") == "gemini-flash-worker-agy",
      (client.model("gemini 4 hydrogen LOW"), client.model("Gemini-4-Neon"), client.model("orchestrator"),
       client.model("model_placeholder_m669")))

m25 = client.model("gemini-2.5-flash") or {}
m35_by_label = client.model("Gemini 3.5 Flash Lite") or {}
check("model(): id beats shared label ('Gemini 3.5 Flash Lite')",
      m25.get("id") == "gemini-2.5-flash" and m25.get("window") == 128000 and m25.get("family") == "gemini-2.5-flash"
      and m35_by_label.get("id") == "gemini-3.5-flash-lite" and m35_by_label.get("window") == 512000,
      (m25, m35_by_label))

check("resolve(): exact, orchestrator, successor, missing effort",
      client.resolve("gemini-4-hydrogen", "low") == "gemini-4-hydrogen-low"
      and client.resolve("orchestrator", "carbon") == "gemini-pro-orchestrator"
      and client.resolve("orchestrator", "neon") == "gemini-pro-orchestrator-neon"
      and client.resolve("gemini-3-hydrogen", "high") == "gemini-4-hydrogen-high"
      and client.resolve("gemini-4-hydrogen", "ultra") == "",
      (client.resolve("orchestrator", "carbon"), client.resolve("gemini-3-hydrogen", "high")))

# sonar/tiered/worker are never successors for another family
only_special = {
    "v": 1, "at": int(time.time()), "client": "2.35.2", "source": "desktop",
    "models": {
        "gemini-4-hydrogen-sonar": ms["gemini-4-hydrogen-sonar"],
        "gemini-4-hydrogen-tiered-high": ms["gemini-4-hydrogen-tiered-high"],
        "gemini-flash-worker-agy": ms["gemini-flash-worker-agy"],
    },
    "families": {
        "gemini-4-hydrogen-sonar": {"label": "", "provider": "google", "efforts": {"": "gemini-4-hydrogen-sonar"}},
        "gemini-4-hydrogen-tiered": {"label": "Gemini 4 Hydrogen", "provider": "google", "efforts": {"high": "gemini-4-hydrogen-tiered-high"}},
        "gemini-flash-worker-agy": {"label": "Gemini 4 Hydrogen (Orchestrator)", "provider": "google", "efforts": {"": "gemini-flash-worker-agy"}},
    },
}
write_cache(only_special, mtime_offset=8)
check("resolve(): sonar/tiered/worker are never successors",
      client.resolve("gemini-3-hydrogen", "high") == "" and client.resolve("gemini-3-hydrogen", "") == "",
      (client.resolve("gemini-3-hydrogen", "high"), client.resolve("gemini-3-hydrogen", "")))

# Empty snapshot: missing cache AND missing BUNDLED → literal "<family>-<effort>"
old_bundled = client.BUNDLED
old_env_cache = os.environ.get("AGP_CLIENT_CACHE")
os.environ["AGP_CLIENT_CACHE"] = os.path.join(SP, "does-not-exist.json")
client.BUNDLED = os.path.join(SP, "also-missing.json")
client._memo.clear()
check("resolve(): empty snapshot → literal '<family>-<effort>' and orchestrator literals",
      client.snapshot() == {}
      and client.resolve("gemini-4-hydrogen", "low") == "gemini-4-hydrogen-low"
      and client.resolve("orchestrator", "carbon") == "gemini-pro-orchestrator"
      and client.resolve("orchestrator", "neon") == "gemini-pro-orchestrator-neon",
      (client.snapshot(), client.resolve("gemini-4-hydrogen", "low")))
client.BUNDLED = old_bundled
if old_env_cache is None:
    os.environ.pop("AGP_CLIENT_CACHE", None)
else:
    os.environ["AGP_CLIENT_CACHE"] = old_env_cache
reset_cache()


# ─────────────────────────────── 5. background() ───────────────────────────────
popen_calls = []
real_popen = subprocess.Popen


def fake_popen(*args, **kwargs):
    popen_calls.append((args, kwargs))
    return None


subprocess.Popen = fake_popen
try:
    os.environ["AGP_CLIENT_REFRESH"] = "off"
    client.background()
    count_off = len(popen_calls)

    os.environ.pop("AGP_CLIENT_REFRESH", None)
    client.background()
    count_on = len(popen_calls)
    first_call = popen_calls[0] if popen_calls else ((), {})

    # Immediate second call is throttled by .attempt file
    client.background()
    count_retry = len(popen_calls)
finally:
    subprocess.Popen = real_popen
    os.environ["AGP_CLIENT_REFRESH"] = "off"

detached_ok = bool(first_call[1].get("creationflags")) if compat.WINDOWS else bool(first_call[1].get("start_new_session"))
cmd_ok = (len(first_call[0]) > 0 and len(first_call[0][0]) >= 3
          and first_call[0][0][1] == os.path.abspath(client.__file__)
          and first_call[0][0][2] == "refresh")
check("background(): AGP_CLIENT_REFRESH=off spawns nothing; unset + stale spawns 1 detached Popen",
      count_off == 0 and count_on == 1 and count_retry == 1 and detached_ok and cmd_ok,
      (count_off, count_on, count_retry, detached_ok, first_call))

# ─────────────────────────────── 6. agy models fallback ───────────────────────────────
parsed = client.parse_agy_models(
    "\n  \ngemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)\n\ngemini-pro-orchestrator\t\tOrchestrator (Carbon)\n"
)
check("parse_agy_models(): parses good lines, ignores blanks, rejects any garbage line",
      parsed == [("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
                 ("gemini-pro-orchestrator", "Orchestrator (Carbon)")]
      and client.parse_agy_models("gemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)\nerror: not signed in\n") == []
      and client.parse_agy_models("") == [],
      parsed)

base_for_agy = dict(norm, models={k: v for k, v in norm["models"].items() if k != "gemini-pro-orchestrator-neon"})
agy_listed = [
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
    ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)"),
    ("gemini-5-alpha-medium", "Gemini 5 Alpha (Medium)"),
]
merged = client.merge_agy(agy_listed, base_for_agy, "1.5.18")
m_kept = (merged.get("models") or {}).get("claude-sonnet-5-5-high") or {}
m_new_orch = (merged.get("models") or {}).get("gemini-pro-orchestrator-neon") or {}
m_new = (merged.get("models") or {}).get("gemini-5-alpha-medium") or {}
check("merge_agy(): kept keeps metadata, new gets id/label fields + None window, absent dropped, cli metadata & sort",
      merged.get("source") == "cli" and merged.get("client") == "cli-1.5.18"
      and merged.get("sort") == [mid for mid, _ in agy_listed]
      and list((merged.get("models") or {}).keys()) == [mid for mid, _ in agy_listed]
      and m_kept.get("window") == 840000 and m_kept.get("family") == "claude-sonnet-5-5" and m_kept.get("tag") == "New"
      and m_new_orch.get("family") == "orchestrator" and m_new_orch.get("effort") == "neon" and m_new_orch.get("window") is None
      and m_new.get("family") == "gemini-5-alpha" and m_new.get("effort") == "medium" and m_new.get("window") is None
      and "gemini-4-hydrogen-low" not in (merged.get("models") or {})
      and client.merge_agy([], norm, "1.5.18") == {},
      (merged.get("source"), merged.get("client"), merged.get("sort"), m_kept, m_new_orch, m_new))

# refresh(force=True) with no LS and a fake `agy`
if os.path.exists(hub_path):
    os.remove(hub_path)
reset_cache()

fake_agy_py = os.path.join(SP, "fake_agy.py")
fake_body = (
    "import os, sys\n"
    "mode = os.environ.get('AGP_FAKE_AGY_MODE', 'ok')\n"
    "if mode == 'fail':\n"
    "    sys.exit(1)\n"
    "if mode == 'garbage':\n"
    "    print('error: not signed in')\n"
    "    sys.exit(0)\n"
    "print('gemini-4-hydrogen-low\\tGemini 4 Hydrogen (Low)')\n"
    "print('gemini-pro-orchestrator\\tOrchestrator (Carbon)')\n"
)
if compat.WINDOWS:
    with open(fake_agy_py, "w", encoding="utf-8") as f:
        f.write(fake_body)
    fake_agy_exe = os.path.join(SP, "agy.cmd")
    with open(fake_agy_exe, "w", encoding="utf-8") as f:
        f.write(f'@"{sys.executable}" "{fake_agy_py}" %*\r\n')
else:
    fake_agy_exe = os.path.join(SP, "fake_agy")
    with open(fake_agy_exe, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\n" + fake_body)
    os.chmod(fake_agy_exe, 0o755)

real_agy = getattr(compat, "agy", None)
real_agy_ver = getattr(compat, "agy_version", None)
real_which = client.shutil.which
try:
    compat.agy = lambda: fake_agy_exe
    compat.agy_version = lambda: "1.5.18"
    os.environ["AGP_FAKE_AGY_MODE"] = "ok"
    cli_snap = client.refresh(force=True)
    with open(CACHE, encoding="utf-8") as f:
        cli_bytes = f.read()
    check("refresh(force=True) with no LS falls back to fake agy and writes cli cache",
          cli_snap.get("source") == "cli" and cli_snap.get("client") == "cli-1.5.18"
          and set((cli_snap.get("models") or {}).keys()) == {"gemini-4-hydrogen-low", "gemini-pro-orchestrator"},
          (cli_snap.get("source"), cli_snap.get("client"), list((cli_snap.get("models") or {}).keys())))

    for bad_mode in ("garbage", "fail"):
        os.environ["AGP_FAKE_AGY_MODE"] = bad_mode
        got_bad = client.refresh(force=True)
        with open(CACHE, encoding="utf-8") as f:
            after_bad = f.read()
        check(f"refresh(force=True) with fake agy ({bad_mode}) keeps good cache untouched",
              after_bad == cli_bytes and got_bad.get("source") == "cli",
              (len(after_bad), len(cli_bytes), got_bad.get("source")))

    compat.agy = lambda: ""
    client.shutil.which = lambda name: None
    got_none = client.refresh(force=True)
    with open(CACHE, encoding="utf-8") as f:
        after_none = f.read()
    check("refresh(force=True) with no agy keeps good cache untouched",
          after_none == cli_bytes and got_none.get("source") == "cli",
          (len(after_none), len(cli_bytes), got_none.get("source")))
finally:
    os.environ.pop("AGP_FAKE_AGY_MODE", None)
    if real_agy is not None:
        compat.agy = real_agy
    if real_agy_ver is not None:
        compat.agy_version = real_agy_ver
    client.shutil.which = real_which

# Cleanup
if os.path.exists(hub_path):
    os.remove(hub_path)
compat.processes = real_processes
server.shutdown()
sys.exit(0 if ok else 1)
