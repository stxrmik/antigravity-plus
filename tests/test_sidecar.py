r"""Sidecar 2.0 Control Center: status.py, server.mjs allowlists & routes, sidecar.json, and index.html checks."""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
SIDECAR_DIR = os.path.join(PLUGIN, "sidecars", "usage")
HOOKS_DIR = os.path.join(PLUGIN, "hooks")
STATUS_PY = os.path.join(SIDECAR_DIR, "status.py")
SERVER_MJS = os.path.join(SIDECAR_DIR, "server.mjs")
INDEX_HTML = os.path.join(SIDECAR_DIR, "index.html")
SIDECAR_JSON = os.path.join(SIDECAR_DIR, "sidecar.json")

sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, SIDECAR_DIR)
import checks  # type: ignore[import-not-found]  # noqa: E402

HOME = os.path.expanduser("~")
AGP = os.path.join(HOME, ".gemini", "antigravity-plus")
now = time.time()
ok = True


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(detail)[-700:]}"))


# ─────────────────────────────── 1. Populate synthetic home state ───────────────────────────────

os.makedirs(os.path.join(AGP, "coord"), exist_ok=True)
os.makedirs(os.path.join(AGP, "delegate"), exist_ok=True)
os.makedirs(os.path.join(AGP, "claude-tasks", "ct-nested-01"), exist_ok=True)
os.makedirs(os.path.join(AGP, "lsp"), exist_ok=True)
os.makedirs(os.path.join(HOME, ".gemini", "config", "projects"), exist_ok=True)

ws = os.path.join(HOME, "proj")
os.makedirs(ws, exist_ok=True)

with open(os.path.join(HOME, ".gemini", "config", "projects", "p1.json"), "w", encoding="utf-8") as f:
    json.dump({
        "id": "p1",
        "name": "proj",
        "projectResources": {"resources": [{"folderUri": pathlib.Path(ws).as_uri()}]},
    }, f)

with open(os.path.join(AGP, "modules.json"), "w", encoding="utf-8") as f:
    json.dump({"installed": ["core", "quality", "lsp", "memory"], "unavailable": ["mobile"], "version": "0.24.0"}, f)

with open(os.path.join(AGP, "settings.json"), "w", encoding="utf-8") as f:
    json.dump({
        "autoMemory": False,
        "protectSecrets": True,
        "bypassMode": {"on": True, "since": "2026-11-18 10:00", "conversation": "conv-bypass-1"},
    }, f)

with open(os.path.join(AGP, "update.json"), "w", encoding="utf-8") as f:
    json.dump({
        "checked": now - 30,
        "installed": "0.24.0",
        "latest": "9.9.0",
        "best": "9.9.0",
        "notes": ["9.9.0: Control Center"],
        "kind": "github",
    }, f)

# Active coord agent + stale agent (15 min old)
slug = re.sub(r"[^A-Za-z0-9]", "-", ws)
with open(os.path.join(AGP, "coord", f"{slug}.json"), "w", encoding="utf-8") as f:
    json.dump({
        "agents": {
            "agent-active-1111": {
                "title": "Build Control Center",
                "parent": "",
                "last": now - 60,
                "files": {os.path.join(ws, "app.py"): now - 45, os.path.join(ws, "old.py"): now - 3600},
            },
            "agent-stale-2222": {
                "title": "Old Agent",
                "parent": "",
                "last": now - 900,
                "files": {},
            },
        },
        "seen": {},
    }, f)

# Delegate & Claude tasks (+ non-task metadata files in delegate/)
with open(os.path.join(AGP, "delegate", "speeds.json"), "w", encoding="utf-8") as f:
    json.dump({"at": now, "models": {}}, f)
with open(os.path.join(AGP, "delegate", "claude-limits.json"), "w", encoding="utf-8") as f:
    json.dump({"at": now}, f)
with open(os.path.join(AGP, "delegate", "dlg-active-1.json"), "w", encoding="utf-8") as f:
    json.dump({
        "id": "dlg-active-1",
        "title": "Claude Code: Ask question",
        "label": "Gemini 4 Hydrogen (Low)",
        "cwd": ws,
        "started": now - 40,
        "state": "running",
        "chat": [{"id": "m1", "from": "antigravity", "kind": "question", "step": 2, "text": "Proceed?"}],
    }, f)
for i in range(6):
    with open(os.path.join(AGP, "delegate", f"dlg-done-{i}.json"), "w", encoding="utf-8") as f:
        json.dump({
            "id": f"dlg-done-{i}",
            "title": f"Done task {i}",
            "model": "gemini-4-hydrogen-low",
            "cwd": ws,
            "started": now - 100 - i * 10,
            "state": "done",
            "chat": [],
        }, f)

with open(os.path.join(AGP, "claude-tasks", "ct-nested-01", "task.json"), "w", encoding="utf-8") as f:
    json.dump({
        "id": "ct-nested-01",
        "title": "Refactor module",
        "model": "sonnet",
        "claude_model": "claude-sonnet-4-5",
        "workspace": ws,
        "started": now - 20,
        "state": "running",
    }, f)
with open(os.path.join(AGP, "claude-tasks", "ct-flat-02.json"), "w", encoding="utf-8") as f:
    json.dump({
        "id": "ct-flat-02",
        "title": "Flat done task",
        "model": "haiku",
        "workspace": ws,
        "started": now - 500,
        "state": "done",
    }, f)

# LSP daemons: one alive (a process started as lsp.py starts its daemon), one guaranteed dead PID, and a decoy:
# a live PID that isn't an lsp.py daemon (a reused PID after a reboot) — listed only where ps can't tell (Windows)
dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
dead_proc.wait()
dead_pid = dead_proc.pid
fake_daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "lsp.py", "daemon", "--key",
                                "alive111122223333"])

DAEMON_TOKEN = "fake-daemon-" + "auth-key-9988"
with open(os.path.join(AGP, "lsp", "decoy777788889999.json"), "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "server": "gopls", "lang": "go", "root": ws, "started": now - 60, "used": now - 5}, f)
with open(os.path.join(AGP, "lsp", "alive111122223333.json"), "w", encoding="utf-8") as f:
    json.dump({
        "pid": fake_daemon.pid,
        "server": "pyright",
        "lang": "python",
        "root": ws,
        "transport": "unix",
        "address": "/tmp/fake.sock",
        "token": DAEMON_TOKEN,
        "started": now - 120,
        "used": now - 30,
    }, f)
with open(os.path.join(AGP, "lsp", "dead444455556666.json"), "w", encoding="utf-8") as f:
    json.dump({
        "pid": dead_pid,
        "server": "clangd",
        "lang": "c",
        "root": ws,
        "started": now - 300,
        "used": now - 200,
    }, f)

# Guard log: 18 lines, containing a GitHub token, URL credentials, and home path
FAKE_GHP = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
FAKE_PW = "pw" + "123"
FAKE_URL = f"https://u:{FAKE_PW}@example.com/org/repo.git"
guard_entries = []
for i in range(18):
    subj = (
        f"git clone {FAKE_URL} && curl -H 'Authorization: Bearer {FAKE_GHP}' {os.path.join(HOME, 'secret', '.env')}"
        if i == 17 else f"echo step-{i}"
    )
    guard_entries.append({
        "ts": f"2026-11-18T10:{i:02d}:00",
        "conversation": "conv-1",
        "tool": "run_command",
        "rule": "secret-print" if i == 17 else "bypass:rm-dangerous",
        "subject": subj,
    })
with open(os.path.join(AGP, "guard.log"), "w", encoding="utf-8") as f:
    for row in guard_entries:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─────────────────────────────── 2. Run status.py --json ───────────────────────────────

r = subprocess.run([sys.executable, STATUS_PY, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
check("status.py --json exits 0", r.returncode == 0, r.stderr)
data = json.loads(r.stdout or "{}")

check("plugin version, modules & update available",
      bool(data.get("plugin", {}).get("version"))
      and data["plugin"]["modules"]["installed"] == ["core", "quality", "lsp", "memory"]
      and data["plugin"]["modules"]["unavailable"] == ["mobile"]
      and data["plugin"]["update"]["available"] is True
      and data["plugin"]["update"]["best"] == "9.9.0",
      data.get("plugin"))

check("access shows read-only bypass ON & protect_secrets True",
      data.get("access", {}).get("bypass", {}).get("on") is True
      and data["access"]["bypass"]["since"] == "2026-11-18 10:00"
      and data["access"]["protect_secrets"] is True,
      data.get("access"))

check("settings reflect settings.json and modules_off (facts/speed off)",
      data.get("settings", {}).get("autoMemory") is False
      and data.get("available", {}).get("lsp") is True
      and data.get("available", {}).get("quality") is True
      and data.get("available", {}).get("mobile", False) is False,
      (data.get("settings"), data.get("available")))

agents = data.get("agents") or []
check("coord lists active agent only (skips stale) with tilde path & recent files",
      len(agents) == 1
      and agents[0]["id"] == "agent-active-1111"
      and agents[0]["workspace"] == "~/proj"
      and agents[0]["files"] == ["app.py"],
      agents)

bridge = data.get("bridge") or []
bridge_ids = [b["id"] for b in bridge]
check("bridge lists active Claude->AGY question + active AGY->Claude + up to 5 recent",
      "dlg-active-1" in bridge_ids
      and "ct-nested-01" in bridge_ids
      and len([b for b in bridge if b["status"] not in ("running", "question")]) == 5
      and next(b for b in bridge if b["id"] == "dlg-active-1")["waiting"] is True
      and next(b for b in bridge if b["id"] == "dlg-active-1")["direction"] == "Claude → AGY"
      and next(b for b in bridge if b["id"] == "ct-nested-01")["direction"] == "AGY → Claude",
      bridge)

fake_daemon.kill()
fake_daemon.wait()
lsp_list = [d for d in data.get("lsp") or [] if os.name != "nt" or d.get("server") != "gopls"]
check("lsp lists the live daemon only: skips a dead PID and a live PID that isn't lsp.py's daemon, omits its token",
      len(lsp_list) == 1
      and lsp_list[0]["pid"] == fake_daemon.pid
      and lsp_list[0]["server"] == "pyright"
      and lsp_list[0]["root"] == "~/proj"
      and DAEMON_TOKEN not in r.stdout
      and (os.name == "nt" or (isinstance(lsp_list[0]["ram_mb"], (int, float)) and lsp_list[0]["ram_mb"] > 0)),
      data.get("lsp"))

guard_list = data.get("guard") or []
check("guard.log returns last 15 entries and masks tokens, URL passwords & home path",
      len(guard_list) == 15
      and FAKE_GHP not in r.stdout
      and FAKE_PW not in r.stdout
      and HOME not in guard_list[0]["subject"]
      and "https://***@example.com" in guard_list[0]["subject"]
      and "~/.gemini" not in guard_list[0]["subject"]
      and "~" in guard_list[0]["subject"],
      guard_list[0] if guard_list else None)


# ─────────────────────────────── 3. Dynamic HOME resolution after import ───────────────────────────────

import status as status_mod  # type: ignore[import-not-found]  # noqa: E402

home2 = os.path.join(SP, "home2")
agp2 = os.path.join(home2, ".gemini", "antigravity-plus")
os.makedirs(agp2, exist_ok=True)
with open(os.path.join(agp2, "settings.json"), "w", encoding="utf-8") as f:
    json.dump({"autoMemory": True, "bypassMode": {"on": False}}, f)

os.environ["HOME"] = home2
os.environ["USERPROFILE"] = home2
try:
    d2 = status_mod.collect()
finally:
    os.environ["HOME"] = HOME
    os.environ["USERPROFILE"] = HOME

check("status.collect() resolves os.path.expanduser('~') dynamically after import",
      d2["access"]["bypass"]["on"] is False
      and d2["settings"]["autoMemory"] is True
      and d2["agents"] == []
      and d2["lsp"] == []
      and d2["guard"] == [],
      d2)

# Passwords handed to command-line tools are masked in the guard log too
PW = "hunter2" + "pass"
for line in (f"mysql -u root -p{PW} app", f"curl -u admin:{PW} https://x", f"sshpass -p '{PW}' ssh host",
             f"docker login -u bob -p {PW}", f"redis-cli -a {PW} ping", f"az login --client-secret {PW}",
             f"aws configure set aws_secret_access_key {PW}"):
    masked = status_mod._mask(line, HOME)
    check(f"guard log masks: {line.split(PW)[0].strip()[:40]}", PW not in masked, masked)
check("guard log keeps ordinary flags readable",
      status_mod._mask("mkdir -p build/out && ssh -p 22 host", HOME) == "mkdir -p build/out && ssh -p 22 host")

# One malformed state record must not take the whole snapshot down
home3 = os.path.join(SP, "home3")
agp3 = os.path.join(home3, ".gemini", "antigravity-plus")
os.makedirs(os.path.join(agp3, "coord"), exist_ok=True)
os.makedirs(os.path.join(agp3, "delegate"), exist_ok=True)
with open(os.path.join(agp3, "coord", "broken.json"), "w", encoding="utf-8") as f:
    json.dump({"agents": {"c1": {"last": time.time(), "files": {"/tmp/ws/a.py": "n/a", "/tmp/ws/b.py": {"x": 1}}}}}, f)
with open(os.path.join(agp3, "delegate", "t1.json"), "w", encoding="utf-8") as f:
    json.dump({"id": "t1", "state": "approval", "started": time.time(),
               "chat": [{"from": "claude", "step": 3}, {"from": "antigravity", "kind": "question", "step": "4a"}]}, f)
os.environ["HOME"] = home3
os.environ["USERPROFILE"] = home3
try:
    d3 = status_mod.collect()
finally:
    os.environ["HOME"] = HOME
    os.environ["USERPROFILE"] = HOME
check("malformed coord/delegate records don't break the snapshot; a task awaiting approval stays active",
      isinstance(d3.get("agents"), list) and any(t.get("id") == "t1" and t.get("status") == "approval" for t in d3.get("bridge", [])),
      d3.get("bridge"))


# ─────────────────────────────── 4. Static, JSON, HTML & JS syntax checks ───────────────────────────────

check("sidecar.json passes checks.check_file and keeps required metadata",
      checks.check_file(SIDECAR_JSON, PLUGIN) == []
      and json.load(open(SIDECAR_JSON, encoding="utf-8")).get("display_name") == "Antigravity Plus · Центр управления"
      and json.load(open(SIDECAR_JSON, encoding="utf-8"))["ui_config"]["views"][0]["path"] == "/"
      and json.load(open(SIDECAR_JSON, encoding="utf-8"))["ui_config"]["views"][0]["entrypoint"] == "SIDECAR_UI_ENTRYPOINT_AUX_PANE")

check("status.py passes checks.check_file", checks.check_file(STATUS_PY, PLUGIN) == [], checks.check_file(STATUS_PY, PLUGIN))
check("server.mjs passes checks.check_file", checks.check_file(SERVER_MJS, PLUGIN) == [], checks.check_file(SERVER_MJS, PLUGIN))

html_text = open(INDEX_HTML, encoding="utf-8").read()
scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html_text, re.S)
inline_js = os.path.join(SP, "index_inline.mjs")
with open(inline_js, "w", encoding="utf-8") as f:
    f.write(scripts[0] if scripts else "")

check("index.html inline <script> parses cleanly via checks.check_file",
      len(scripts) == 1 and checks.check_file(inline_js, SP) == [],
      checks.check_file(inline_js, SP))

check("index.html has 4 tabs, copy actions, no backdrop-filter, no external CDNs",
      all(tok in html_text for tok in ("tab-session", "tab-all", "tab-activity", "tab-control",
                                       "Сессия", "Сводка", "Активность", "Управление",
                                       "/update", "/bypass", "/api/status", "/api/toggle", "/api/lsp/stop", "/api/update/check"))
      and "backdrop-filter" not in html_text.lower()
      and not re.search(r"<(?:script|link)\b[^>]+\b(?:src|href)\s*=\s*['\"]https?://", html_text, re.I))


# ─────────────────────────────── 5. Node server.mjs allowlist & route tests ───────────────────────────────

node_info = checks.node()
if node_info:
    node_bin, node_extra_env = node_info
    node_test = os.path.join(SP, "test_server.mjs")
    server_url = pathlib.Path(SERVER_MJS).as_uri()
    node_code = f"""
import assert from 'node:assert/strict';
import {{ readFileSync }} from 'node:fs';
import {{ SAFE_TOGGLES, validateTogglePayload, validateLspTarget, registerRoutes }} from {json.dumps(server_url)};

// 1. Allowlist contents
const expectedAllowed = [
  'autoMemory', 'finishTasks', 'checkEdits', 'autoReview', 'autoVerify',
  'autoSetup', 'parallelHints', 'speedHints', 'progressHints', 'quotaHints', 'factChecks',
  'objectiveAnswers', 'autoCi', 'gitChecks', 'lspChecks', 'updateCheck',
];
assert.equal(SAFE_TOGGLES.size, expectedAllowed.length);
for (const k of expectedAllowed) assert.ok(SAFE_TOGGLES.has(k), `missing ${{k}}`);
for (const forbidden of ['bypassMode', 'protectSecrets', 'fullAccess', 'autoUpdate', 'shareWithClaudeCode', 'recentConversations']) {{
  assert.ok(!SAFE_TOGGLES.has(forbidden), `forbidden key in SAFE_TOGGLES: ${{forbidden}}`);
  assert.equal(validateTogglePayload({{ key: forbidden, value: true }}).ok, false);
}}
assert.equal(validateTogglePayload({{ key: 'autoMemory', value: 'on' }}).ok, false);
assert.equal(validateTogglePayload(null).ok, false);
assert.equal(validateTogglePayload({{ key: 'autoMemory', value: true }}).ok, true);

// 2. LSP target validator
assert.equal(validateLspTarget(undefined).ok, true);
assert.equal(validateLspTarget('all').ok, true);
assert.equal(validateLspTarget('alive111122223333').ok, true);
assert.equal(validateLspTarget('../etc').ok, false);
assert.equal(validateLspTarget('pyright;id').ok, false);
assert.equal(validateLspTarget(123).ok, false);

// 3. Register routes on fake SidecarApp and exercise endpoints end-to-end
class FakeResponse {{
  constructor(body, opts = {{}}) {{
    this.body = body;
    this.contentType = opts.contentType || 'text/plain';
    this.status = opts.status || 200;
  }}
}}
const pages = new Map();
const apis = new Map();
const fakeApp = {{
  page: (p, fn) => pages.set(p, fn),
  api: (p, fn, method = 'POST') => apis.set(`${{method}} ${{p}}`, fn),
}};
registerRoutes(fakeApp, FakeResponse);

for (const route of ['POST /api/recent', 'POST /api/limits', 'POST /api/session',
                     'POST /api/status', 'POST /api/toggle', 'POST /api/lsp/stop', 'POST /api/update/check']) {{
  assert.ok(apis.has(route), `missing route ${{route}}`);
}}

// Forbidden toggle returns 400 JSON error and leaves settings.json untouched
const badToggle = await apis.get('POST /api/toggle')({{ key: 'protectSecrets', value: false }});
assert.equal(badToggle.status, 400);
assert.ok(JSON.parse(badToggle.body).error);
const badBypass = await apis.get('POST /api/toggle')({{ key: 'bypassMode', value: false }});
assert.equal(badBypass.status, 400);

// Allowed toggle updates settings.json while keeping protectSecrets & bypassMode intact
const goodToggle = await apis.get('POST /api/toggle')({{ key: 'autoMemory', value: true }});
assert.equal(goodToggle.ok, true);
assert.equal(goodToggle.settings.autoMemory, true);
const saved = JSON.parse(readFileSync({json.dumps(os.path.join(AGP, "settings.json"))}, 'utf8'));
assert.equal(saved.autoMemory, true);
assert.equal(saved.protectSecrets, true);
assert.equal(saved.bypassMode.on, true);

// POST /api/status (POST so the SDK checks the token) reflects updated autoMemory
assert.ok(!apis.has('GET /api/status'), 'status must not be a tokenless GET route');
const st = await apis.get('POST /api/status')({{}});
assert.equal(st.settings.autoMemory, true);
assert.equal(st.access.bypass.on, true);

// POST /api/lsp/stop validation
const badStop = await apis.get('POST /api/lsp/stop')({{ target: '../evil' }});
assert.equal(badStop.status, 400);

console.log('NODE_SIDECAR_OK');
"""
    with open(node_test, "w", encoding="utf-8") as f:
        f.write(node_code)
    node_env = dict(os.environ, **node_extra_env)
    nr = subprocess.run([node_bin, node_test], env=node_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    check("server.mjs allowlist, 400 rejections, toggle persistence & /api/status route pass in Node",
          nr.returncode == 0 and "NODE_SIDECAR_OK" in nr.stdout,
          nr.stdout + nr.stderr)
else:
    print("ℹ️ Node runtime not found; skipped Node runtime tests for server.mjs")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
