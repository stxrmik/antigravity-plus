"""Scheduled Antigravity tasks (/schedule-task): quota gate, Antigravity's sidecar format in ~/.gemini/config/sidecars,
validation, time zones, and list/remove/overwrite safety for the user's own automations."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

PLUGIN, SP = sys.argv[1], sys.argv[2]
SCRIPT = os.path.join(PLUGIN, "skills", "schedule-task", "scripts", "schedule_task.py")
HOME = os.path.expanduser("~")
SIDECARS = os.path.join(HOME, ".gemini", "config", "sidecars")
CACHE = os.path.join(HOME, ".gemini", "antigravity-plus", "quota.json")
WS = os.path.join(SP, "sample-workspace")
os.makedirs(WS, exist_ok=True)
os.makedirs(os.path.join(HOME, ".gemini", "antigravity"), exist_ok=True)  # the App is set up here

ok = True


def check(name: str, cond: Any, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    msg = f"{'✅' if cond else '❌'} {name}"
    if not cond and detail != "":
        msg += f" -> {str(detail)[-900:]}"
    print(msg)


def iso(secs: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs))


QUOTA_PAYLOAD: Dict[str, Any] = {"response": {"groups": []}}
CALLS: List[str] = []


class FakeLsHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _reply(self, code: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

    def do_GET(self) -> None:
        self._reply(200, {"status": "ok"} if self.path == "/healthz" else {})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-codeium-csrf-token") != "tok-sched":
            self._reply(403, {"code": "permission_denied", "message": "bad csrf token"})
            return
        method = self.path.rsplit("/", 1)[-1]
        CALLS.append(method)
        if method == "RetrieveUserQuotaSummary":
            self._reply(200, QUOTA_PAYLOAD)
            return
        self._reply(404, {"code": "unimplemented"})


def start_fake_ls() -> str:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLsHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"127.0.0.1:{srv.server_address[1]}"


LS_ADDR = start_fake_ls()
_sock = socket.socket()
_sock.bind(("127.0.0.1", 0))
CLOSED_ADDR = f"127.0.0.1:{_sock.getsockname()[1]}"
_sock.close()

# A fixed local zone, so `create` adds the same CRON_TZ= prefix on every machine.
BASE_ENV = dict(os.environ, ANTIGRAVITY_LS_ADDRESS=LS_ADDR, ANTIGRAVITY_CSRF_TOKEN="tok-sched", TZ="Europe/Berlin")


def run_cli(
    *args: str,
    env_overrides: Optional[Dict[str, str]] = None,
    stdin_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    env = dict(BASE_ENV)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )


def set_quota(
    gemini_weekly: Any = 0.64,
    gemini_5h: Any = 0.80,
    gemini_5h_disabled: bool = False,
    gemini_weekly_disabled: bool = False,
    claude_weekly: Any = 0.90,
    claude_5h: Any = 0.95,
) -> None:
    def make_buckets(w: Any, h5: Any, w_dis: bool, h5_dis: bool) -> List[dict]:
        bw: Dict[str, Any] = {"window": "weekly", "resetTime": iso(3 * 86400)}
        if w is not None:
            bw["remainingFraction"] = w
        if w_dis:
            bw["disabled"] = True
        bh: Dict[str, Any] = {"window": "5h", "resetTime": iso(3 * 3600)}
        if h5 is not None:
            bh["remainingFraction"] = h5
        if h5_dis:
            bh["disabled"] = True
        return [bw, bh]

    QUOTA_PAYLOAD["response"] = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen, Orchestrator (Carbon)",
                "buckets": make_buckets(gemini_weekly, gemini_5h, gemini_weekly_disabled, gemini_5h_disabled),
            },
            {
                "displayName": "Claude and GPT models",
                "description": "Models within this group: Claude Sonnet 5.5, Claude Opus 5.5",
                "buckets": make_buckets(claude_weekly, claude_5h, False, False),
            },
        ]
    }


def drop_cache() -> None:
    if os.path.exists(CACHE):
        os.remove(CACHE)


# 1. gate tests against fake RetrieveUserQuotaSummary
set_quota(gemini_weekly=0.64, gemini_5h=0.80)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20")
check(
    "gate exits 0 when above threshold",
    r.returncode == 0 and "RUN: Gemini Models" in r.stdout and "week 64%" in r.stdout and "5h 80%" in r.stdout,
    r.stdout + r.stderr,
)

# Boundary equality & float precision (0.29 * 100 == 28.999999999999996 in IEEE 754)
set_quota(gemini_weekly=0.29, gemini_5h=0.20)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "29", "--min-5h", "20")
check("gate handles exact boundary without float precision false skip", r.returncode == 0 and "RUN:" in r.stdout, r.stdout)

# Weekly below threshold -> exit 3
set_quota(gemini_weekly=0.08, gemini_5h=0.80)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20")
check(
    "gate exits 3 when weekly below threshold",
    r.returncode == 3 and "SKIP: Gemini Models" in r.stdout and "week 8% < 15%" in r.stdout and "resets in" in r.stdout,
    r.stdout + r.stderr,
)

# 5h below threshold -> exit 3
set_quota(gemini_weekly=0.80, gemini_5h=0.10)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20")
check(
    "gate exits 3 when 5h below threshold",
    r.returncode == 3 and "SKIP: Gemini Models" in r.stdout and "5h 10% < 20%" in r.stdout and "resets in" in r.stdout,
    r.stdout + r.stderr,
)

# Proto3 omitted remainingFraction means 0.0 -> exit 3
set_quota(gemini_weekly=None, gemini_5h=0.90)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20")
check(
    "gate treats omitted remainingFraction as 0% (proto3) and exits 3",
    r.returncode == 3 and "SKIP: Gemini Models" in r.stdout and "week 0% < 15%" in r.stdout,
    r.stdout + r.stderr,
)

# disabled: True bucket is ignored even when remainingFraction is omitted
set_quota(gemini_weekly=0.64, gemini_5h=None, gemini_5h_disabled=True)
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20")
check(
    "gate ignores disabled: True bucket (even with missing remainingFraction)",
    r.returncode == 0 and "RUN: Gemini Models" in r.stdout and "week 64%" in r.stdout and "5h" not in r.stdout,
    r.stdout + r.stderr,
)

# --group claude picks Claude group and exits 0 even if Gemini is empty
set_quota(gemini_weekly=None, gemini_5h=None, claude_weekly=0.72, claude_5h=0.55)
r = run_cli("gate", "--group", "claude", "--min-weekly", "15", "--min-5h", "20", "--json")
gate_json = json.loads(r.stdout or "{}")
check(
    "gate --group claude --json evaluates Claude group independently",
    r.returncode == 0 and gate_json.get("decision") == "run" and gate_json.get("group") == "Claude and GPT models",
    r.stdout,
)

# LS unreachable & no cache -> exits 0 proceeding
drop_cache()
r = run_cli("gate", env_overrides={"ANTIGRAVITY_LS_ADDRESS": CLOSED_ADDR})
check(
    "gate exits 0 with quota unavailable when LS unreachable and no cache",
    r.returncode == 0 and "RUN: quota unavailable" in r.stdout,
    r.stdout + r.stderr,
)

# LS unreachable & fresh (< 15 min) quota.json cache in hooks/quota.py format -> uses cache
os.makedirs(os.path.dirname(CACHE), exist_ok=True)
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(
        {
            "at": time.time() - 300,
            "groups": [
                {
                    "name": "Gemini Models",
                    "models": ["gemini 4 carbon"],
                    "buckets": {
                        "weekly": {"left": 0.05, "reset": iso(2 * 86400), "disabled": False},
                        "5h": {"left": 0.90, "reset": iso(3600), "disabled": False},
                    },
                }
            ],
        },
        f,
    )
r = run_cli("gate", "--group", "gemini", "--min-weekly", "15", "--min-5h", "20", env_overrides={"ANTIGRAVITY_LS_ADDRESS": CLOSED_ADDR})
check(
    "gate falls back to fresh (<15 min) quota.json when LS unreachable",
    r.returncode == 3 and "SKIP: Gemini Models" in r.stdout and "week 5% < 15%" in r.stdout,
    r.stdout + r.stderr,
)

# Stale (> 15 min) quota.json cache is ignored
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(
        {
            "at": time.time() - 16 * 60,
            "groups": [
                {
                    "name": "Gemini Models",
                    "models": ["gemini 4 carbon"],
                    "buckets": {"weekly": {"left": 0.01, "reset": iso(86400), "disabled": False}},
                }
            ],
        },
        f,
    )
r = run_cli("gate", "--group", "gemini", env_overrides={"ANTIGRAVITY_LS_ADDRESS": CLOSED_ADDR})
check(
    "gate ignores stale (>15 min) quota.json cache",
    r.returncode == 0 and "RUN: quota unavailable" in r.stdout,
    r.stdout + r.stderr,
)

# 2. create: slug & cron validation
for bad_slug in ("a", "-lead", "UpperCase", "bad_slug", "../escape", "a" * 46, "agp-a"):
    r = run_cli("create", "--slug", bad_slug, "--schedule", "0 9 * * *", "--prompt", "Check status")
    check(f"create rejects invalid slug {bad_slug!r}", r.returncode != 0, r.stdout + r.stderr)

for bad_cron in (
    "",
    "* * * *",
    "* * * * * *",
    "60 * * * *",
    "* 24 * * *",
    "* * 0 * *",
    "* * 32 * *",
    "* * * 0 *",
    "* * * 13 *",
    "* * * * 8",
    "*/0 * * * *",
    "10-5 * * * *",
    "0 9 * * MON",
    "1,,2 * * * *",
    "CRON_TZ= 0 9 * * *",
    "CRON_TZ=../etc 0 9 * * *",
    "CRON_TZ=+02:00 0 9 * * *",
    "CRON_TZ=Europe/Berlin",
    "CRON_TZ=Europe/Berlin 0 9 * *",
):
    r = run_cli("create", "--slug", "valid-slug", "--schedule", bad_cron, "--prompt", "Check status")
    check(f"create rejects bad cron {bad_cron!r}", r.returncode != 0, r.stdout + r.stderr)

# Empty prompt, missing workspace, and wildcard grants rejected
r = run_cli("create", "--slug", "valid-slug", "--schedule", "0 9 * * *", "--prompt", "   ")
check("create rejects empty prompt", r.returncode != 0, r.stdout + r.stderr)
r = run_cli("create", "--slug", "valid-slug", "--schedule", "0 9 * * *", "--prompt", "x",
            "--workspace", os.path.join(SP, "no-such-folder"))
check("create rejects a workspace folder that doesn't exist", r.returncode != 0, r.stdout + r.stderr)
for bad_grant in ("command(*)", "read_file(/)", "write_file(relative/dir)", "mcp(*)", "read_url(*)", "rm -rf /"):
    r = run_cli("create", "--slug", "valid-slug", "--schedule", "0 9 * * *", "--prompt", "x", "--grant", bad_grant)
    check(f"create rejects grant {bad_grant!r}", r.returncode != 0, r.stdout + r.stderr)
check(
    "nothing was written by the rejected creates",
    not os.path.exists(os.path.join(SIDECARS, "agp-valid-slug")),
    os.listdir(SIDECARS) if os.path.isdir(SIDECARS) else "",
)

# Valid complex cron, local zone prefix (TZ=Europe/Berlin), Step 0 gate, workspace & grants in agent_permissions
r = run_cli(
    "create",
    "--slug",
    "nightly-audit",
    "--schedule",
    "0,30  8-18/2  1,15  */3  1-5",
    "--title",
    "Nightly Audit",
    "--group",
    "claude",
    "--min-weekly",
    "10",
    "--min-5h",
    "25",
    "--workspace",
    WS,
    "--grant",
    "command(npm test)",
    "--prompt",
    "Run tests and summarize failures.",
)
sidecar_file = os.path.join(SIDECARS, "agp-nightly-audit", "sidecar.json")
check("create writes agp-<slug>/sidecar.json and exits 0", r.returncode == 0 and os.path.isfile(sidecar_file),
      r.stdout + r.stderr)
check(
    "create tells the user to enable it in the Automations dashboard and that ungranted commands are auto-denied",
    "enables «Nightly Audit» in the Automations dashboard" in r.stdout
    and "[Open Automations Dashboard](sidecar://dashboard)" in r.stdout
    and "auto-deny" in r.stdout,
    r.stdout,
)
sc = json.load(open(sidecar_file, encoding="utf-8"))
args = sc.get("args") or []
sc_prompt = args[4] if len(args) == 5 else ""
gate_line = next((ln.strip() for ln in sc_prompt.splitlines() if "schedule_task.py" in ln and " gate " in ln), "")
perms = sc.get("agent_permissions") or {}
check(
    "sidecar.json follows Antigravity's schedule format (args list, restart_policy always, display_name)",
    sc.get("builtin") == "schedule"
    and isinstance(args, list)
    and args[0] == "CRON_TZ=Europe/Berlin 0,30 8-18/2 1,15 */3 1-5"
    and args[1:4] == ["agentapi", "new-conversation", "--"]
    and sc.get("restart_policy") == "always"
    and sc.get("display_name") == "Nightly Audit"
    and set(sc) <= {"builtin", "args", "restart_policy", "display_name", "description", "agent_permissions"},
    sc,
)
check(
    "prompt has Step 0 gate, Workspace and the task; grants allow exactly the Step 0 command",
    sc_prompt.startswith("Step 0")
    and gate_line.endswith("gate --group claude --min-weekly 10 --min-5h 25")
    and f"Workspace: {WS}" in sc_prompt
    and "Run tests and summarize failures." in sc_prompt
    and perms.get("access_grants") == [f"command({gate_line})", "command(npm test)"]
    and perms.get("workspace_uris") == [pathlib.Path(WS).as_uri()],
    sc,
)

# Explicit zone prefixes are kept as given
r = run_cli("create", "--slug", "utc-task", "--schedule", "CRON_TZ=UTC 15 6 * * *", "--prompt", "UTC job",
            "--min-weekly", "0", "--min-5h", "0")
r2 = run_cli("create", "--slug", "tz-task", "--schedule", "TZ=Europe/Paris 0 7 * * 1", "--prompt", "Paris job")
check(
    "create keeps an explicit CRON_TZ= / TZ= prefix",
    r.returncode == 0 and r2.returncode == 0
    and json.load(open(os.path.join(SIDECARS, "agp-utc-task", "sidecar.json"), encoding="utf-8"))["args"][0]
    == "CRON_TZ=UTC 15 6 * * *"
    and json.load(open(os.path.join(SIDECARS, "agp-tz-task", "sidecar.json"), encoding="utf-8"))["args"][0]
    == "TZ=Europe/Paris 0 7 * * 1",
    r.stdout + r.stderr + r2.stdout + r2.stderr,
)

# Overwrite protection
r = run_cli("create", "--slug", "nightly-audit", "--schedule", "0 9 * * *", "--prompt", "Replaced prompt")
check("create refuses overwrite without --overwrite", r.returncode != 0, r.stdout + r.stderr)
check(
    "original sidecar.json preserved after refused overwrite",
    json.load(open(sidecar_file, encoding="utf-8"))["args"][0] == "CRON_TZ=Europe/Berlin 0,30 8-18/2 1,15 */3 1-5",
)

r = run_cli(
    "create",
    "--slug",
    "agp-nightly-audit",
    "--schedule",
    "15 10 * * 1-5",
    "--prompt",
    "Updated audit task.",
    "--overwrite",
)
sc2 = json.load(open(sidecar_file, encoding="utf-8"))
check(
    "create --overwrite (slug given as its agp- id) replaces the schedule sidecar",
    r.returncode == 0
    and sc2["args"][0] == "CRON_TZ=Europe/Berlin 15 10 * * 1-5"
    and "Updated audit task." in sc2["args"][4]
    and not os.path.exists(os.path.join(SIDECARS, "agp-agp-nightly-audit")),
    r.stdout + r.stderr,
)

# Create with zero thresholds omits Step 0 and its grant
r = run_cli(
    "create",
    "--slug",
    "always-run",
    "--schedule",
    "*/15 * * * 0,7",
    "--min-weekly",
    "0",
    "--min-5h",
    "0",
    "--prompt",
    "Ping health endpoint.",
)
sc_nogate = json.load(open(os.path.join(SIDECARS, "agp-always-run", "sidecar.json"), encoding="utf-8"))
check(
    "create with --min-weekly 0 --min-5h 0 skips Step 0 and grants nothing",
    r.returncode == 0
    and "Step 0" not in sc_nogate["args"][4]
    and sc_nogate["args"][4].strip() == "Ping health endpoint."
    and "agent_permissions" not in sc_nogate,
    sc_nogate,
)

# Create a second task with explicit claude gate via stdin prompt
r = run_cli(
    "create",
    "--slug",
    "claude-sync",
    "--schedule",
    "0 12 * * 1-5",
    "--group",
    "claude",
    "--min-weekly",
    "10",
    "--min-5h",
    "25",
    "--prompt",
    "-",
    stdin_text="Sync issue triage.\n",
)
check("create accepts prompt from stdin ('-')", r.returncode == 0, r.stdout + r.stderr)

# The user's own automation (made with Antigravity's /automation) and a non-schedule sidecar
users_dir = os.path.join(SIDECARS, "daily-pr-check")
os.makedirs(users_dir, exist_ok=True)
users_file = os.path.join(users_dir, "sidecar.json")
users_sc = {
    "builtin": "schedule",
    "args": ["CRON_TZ=Europe/Berlin 0 9 * * *", "agentapi", "new-conversation", "--", "Summarize open pull requests."],
    "restart_policy": "always",
    "display_name": "Daily PR Check",
    "agent_permissions": {"access_grants": ["command(gh pr list)"]},
}
with open(users_file, "w", encoding="utf-8") as f:
    json.dump(users_sc, f)
non_sched_dir = os.path.join(SIDECARS, "custom-panel")
os.makedirs(non_sched_dir, exist_ok=True)
non_sched_file = os.path.join(non_sched_dir, "sidecar.json")
with open(non_sched_file, "w", encoding="utf-8") as f:
    json.dump({"command": "node", "args": ["server.mjs"], "display_name": "Custom Panel"}, f)

# 3. list: schedule sidecars only; ours with their gate, the user's own read-only
r = run_cli("list")
check(
    "list shows schedule sidecars, marks the user's own as read-only and ignores non-schedule sidecars",
    r.returncode == 0
    and "agp-nightly-audit «Nightly Audit»" in r.stdout
    and "agp-always-run" in r.stdout
    and "agp-claude-sync" in r.stdout
    and "custom-panel" not in r.stdout
    and "CRON_TZ=Europe/Berlin 15 10 * * 1-5" in r.stdout
    and "Updated audit task." in r.stdout
    and "daily-pr-check «Daily PR Check»: [CRON_TZ=Europe/Berlin 0 9 * * *] not created by /schedule-task" in r.stdout,
    r.stdout + r.stderr,
)

r = run_cli("list", "--json")
listed = json.loads(r.stdout or "[]")
by_id = {item["id"]: item for item in listed}
check(
    "list --json reads list-shaped args, extracts the gate, workspace and grants, and flags ownership",
    r.returncode == 0
    and set(by_id) == {"agp-always-run", "agp-claude-sync", "agp-nightly-audit", "agp-tz-task", "agp-utc-task",
                       "daily-pr-check"}
    and by_id["agp-nightly-audit"]["gate"] == {"group": "gemini", "min_weekly": 15, "min_5h": 20}
    and by_id["agp-claude-sync"]["gate"] == {"group": "claude", "min_weekly": 10, "min_5h": 25}
    and by_id["agp-always-run"]["gate"] is None
    and by_id["agp-claude-sync"]["ours"] is True
    and by_id["daily-pr-check"]["ours"] is False
    and by_id["daily-pr-check"]["prompt"] == "Summarize open pull requests."
    and by_id["daily-pr-check"]["access_grants"] == ["command(gh pr list)"],
    listed,
)

# 4. dynamic HOME resolution, local zone & schedule helpers, process discovery fallback
_spec = importlib.util.spec_from_file_location("schedule_task", SCRIPT)
assert _spec is not None and _spec.loader is not None
st_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st_mod)

alt_home = os.path.join(SP, "alt-home")
os.makedirs(alt_home, exist_ok=True)
orig_home, orig_userprofile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
try:
    os.environ["HOME"] = alt_home
    os.environ["USERPROFILE"] = alt_home
    expected_dir = os.path.join(alt_home, ".gemini", "config", "sidecars")
    check("sidecars dir resolves dynamically when HOME changes", st_mod._sidecars_dir() == expected_dir, st_mod._sidecars_dir())
finally:
    if orig_home is not None:
        os.environ["HOME"] = orig_home
    if orig_userprofile is not None:
        os.environ["USERPROFILE"] = orig_userprofile

orig_tz = os.environ.get("TZ")
try:
    os.environ["TZ"] = "Europe/Berlin"
    zone_env = st_mod.local_zone()
    os.environ["TZ"] = "CET-1CEST"  # a POSIX rule, not an IANA name: never used as CRON_TZ
    zone_posix = st_mod.local_zone()
finally:
    if orig_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = orig_tz
check(
    "local_zone() reads an IANA $TZ and never returns a POSIX TZ rule",
    zone_env == "Europe/Berlin" and zone_posix != "CET-1CEST",
    (zone_env, zone_posix),
)
check(
    "validate_schedule: no zone known → plain cron; explicit prefix wins over the local zone",
    st_mod.validate_schedule("0 9 * * *", None) == ("0 9 * * *", None)
    and st_mod.validate_schedule(" 0  9 * * * ", "Asia/Tokyo") == ("CRON_TZ=Asia/Tokyo 0 9 * * *", None)
    and st_mod.validate_schedule("CRON_TZ=UTC 0 9 * * *", "Asia/Tokyo") == ("CRON_TZ=UTC 0 9 * * *", None),
)
if st_mod._zone_known("Mars/Olympus") is False:
    check(
        "validate_schedule rejects an unknown time zone",
        st_mod.validate_schedule("CRON_TZ=Mars/Olympus 0 9 * * *", None)[0] is None,
    )

# Verify process/port discovery path when env vars are unset
set_quota(gemini_weekly=0.50, gemini_5h=0.60)
ls_port = int(LS_ADDR.rsplit(":", 1)[1])
orig_procs, orig_ports = st_mod.compat.processes, st_mod.compat.listening_ports
orig_addr = os.environ.pop("ANTIGRAVITY_LS_ADDRESS", None)
orig_tok = os.environ.pop("ANTIGRAVITY_CSRF_TOKEN", None)
try:
    st_mod.compat.processes = lambda name: [(99999, "antigravity language_server --csrf_token=tok-sched")]
    st_mod.compat.listening_ports = lambda pid: [ls_port] if pid == 99999 else []
    disc_res = st_mod.evaluate_gate("gemini", 15, 20)
    check(
        "gate discovers LS via compat.processes + listening_ports when env vars unset",
        disc_res["exit_code"] == 0 and disc_res["source"] == "language_server" and "week 50%" in disc_res["message"],
        disc_res,
    )
finally:
    st_mod.compat.processes, st_mod.compat.listening_ports = orig_procs, orig_ports
    if orig_addr is not None:
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = orig_addr
    if orig_tok is not None:
        os.environ["ANTIGRAVITY_CSRF_TOKEN"] = orig_tok

# 5. remove / overwrite safety: never the user's own automations or other sidecars
r = run_cli("remove", "--slug", "daily-pr-check")
check(
    "remove refuses the user's own automation (no agp- prefix) with exit 2 and keeps it",
    r.returncode == 2
    and "not created by /schedule-task" in r.stderr
    and json.load(open(users_file, encoding="utf-8")) == users_sc,
    r.stdout + r.stderr,
)
r = run_cli("remove", "--slug", "custom-panel")
check(
    "remove refuses a non-agp sidecar with exit 2 and preserves files",
    r.returncode == 2 and os.path.isfile(non_sched_file),
    r.stdout + r.stderr,
)
r = run_cli("create", "--slug", "daily-pr-check", "--schedule", "0 9 * * *", "--prompt", "Mine")
check(
    "create with the slug of a user's automation writes agp-<slug> and leaves theirs alone",
    r.returncode == 0
    and os.path.isfile(os.path.join(SIDECARS, "agp-daily-pr-check", "sidecar.json"))
    and json.load(open(users_file, encoding="utf-8")) == users_sc,
    r.stdout + r.stderr,
)

# create --overwrite refuses to clobber a non-schedule sidecar, {} or malformed sidecar.json under an agp- id
agp_panel_dir = os.path.join(SIDECARS, "agp-custom-panel")
os.makedirs(agp_panel_dir, exist_ok=True)
agp_panel_file = os.path.join(agp_panel_dir, "sidecar.json")
with open(agp_panel_file, "w", encoding="utf-8") as f:
    json.dump({"command": "node", "args": ["server.mjs"], "display_name": "Custom Panel"}, f)
r = run_cli("create", "--slug", "custom-panel", "--schedule", "0 9 * * *", "--prompt", "Take over", "--overwrite")
check(
    "create --overwrite refuses to clobber non-schedule sidecar",
    r.returncode == 2 and json.load(open(agp_panel_file, encoding="utf-8")).get("command") == "node",
    r.stdout + r.stderr,
)
empty_sc_dir = os.path.join(SIDECARS, "agp-empty-panel")
os.makedirs(empty_sc_dir, exist_ok=True)
with open(os.path.join(empty_sc_dir, "sidecar.json"), "w", encoding="utf-8") as _f:
    _f.write("{}")
r_empty = run_cli("create", "--slug", "empty-panel", "--schedule", "0 9 * * *", "--prompt", "Take over", "--overwrite")
bad_sc_dir = os.path.join(SIDECARS, "agp-broken-panel")
os.makedirs(bad_sc_dir, exist_ok=True)
with open(os.path.join(bad_sc_dir, "sidecar.json"), "w", encoding="utf-8") as _f:
    _f.write("{broken")
r_broken = run_cli("create", "--slug", "broken-panel", "--schedule", "0 9 * * *", "--prompt", "Take over", "--overwrite")
check(
    "create --overwrite refuses empty {} and malformed sidecar.json with exit 2",
    r_empty.returncode == 2 and r_broken.returncode == 2,
    (r_empty.returncode, r_empty.stdout + r_empty.stderr, r_broken.returncode, r_broken.stdout + r_broken.stderr),
)

# Folder without sidecar.json also refused with exit 2
bare_dir = os.path.join(SIDECARS, "agp-bare-folder")
os.makedirs(bare_dir, exist_ok=True)
with open(os.path.join(bare_dir, "notes.txt"), "w", encoding="utf-8") as _f:
    _f.write("keep me")
r = run_cli("remove", "--slug", "bare-folder")
check(
    "remove refuses folder missing sidecar.json with exit 2",
    r.returncode == 2 and os.path.isfile(os.path.join(bare_dir, "notes.txt")),
    r.stdout + r.stderr,
)

# Remove valid schedule sidecar succeeds and deletes folder
r = run_cli("remove", "--slug", "nightly-audit")
check(
    "remove deletes its own agp- schedule sidecar folder and exits 0",
    r.returncode == 0 and not os.path.exists(os.path.join(SIDECARS, "agp-nightly-audit")),
    r.stdout + r.stderr,
)

# Remove missing slug fails cleanly
r = run_cli("remove", "--slug", "nightly-audit")
check("remove missing slug exits non-zero", r.returncode != 0, r.stdout + r.stderr)

# A computer with only the Antigravity CLI: no sidecar is written, the user is told why
if os.name != "nt":
    import shutil
    bin_dir = os.path.join(SP, "cli-bin")
    os.makedirs(bin_dir, exist_ok=True)
    fake_agy = os.path.join(bin_dir, "agy")
    with open(fake_agy, "w") as f:
        f.write("#!/bin/sh\necho 1.5.18\n")
    os.chmod(fake_agy, 0o755)
    shutil.rmtree(os.path.join(HOME, ".gemini", "antigravity"))
    r = run_cli("create", "--slug", "cli-only", "--schedule", "0 9 * * *", "--prompt", "x",
                env_overrides={"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")})
    check("CLI only (no App data folder): create refuses and says automations need the App",
          r.returncode != 0 and "Antigravity App" in r.stderr and not os.path.exists(os.path.join(SIDECARS, "agp-cli-only")),
          r.stdout + r.stderr)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
