"""Offline test suite for Antigravity Plus hooks inside Antigravity CLI (`agy`) sessions."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)
import compat
import conversation as conv

ok = True


def check(name: str, cond: bool, got: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {got}"))


home = os.path.expanduser("~")
desk_dir = os.path.join(home, ".gemini", "antigravity")
cli_dir = os.path.join(home, ".gemini", "antigravity-cli")
settings_path = os.path.join(home, ".gemini", "antigravity-plus", "settings.json")

# CLI-only machine: ensure ~/.gemini/antigravity does not exist
shutil.rmtree(desk_dir, ignore_errors=True)
shutil.rmtree(cli_dir, ignore_errors=True)

ws = os.path.join(SP, "cli_ws")
os.makedirs(ws, exist_ok=True)
sample_py = os.path.join(ws, "app.py")
with open(sample_py, "w", encoding="utf-8") as f:
    f.write("def greet():\n    return 'hi'\n")


def make_cli_session(cid: str, user_msgs: list[str]) -> tuple[str, str]:
    art = os.path.join(cli_dir, "brain", cid)
    logs = os.path.join(art, ".system_generated", "logs")
    os.makedirs(logs, exist_ok=True)
    tp = os.path.join(logs, "transcript.jsonl")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    rows: list[str] = []
    step = 0
    for msg in user_msgs:
        rows.append(json.dumps({
            "step_index": step,
            "type": "USER_INPUT",
            "created_at": now,
            "content": f"<USER_REQUEST>{msg}</USER_REQUEST>",
        }, ensure_ascii=False))
        step += 1
    rows.append(json.dumps({
        "step_index": step,
        "type": "PLANNER_RESPONSE",
        "created_at": now,
        "content": "Working on it.",
    }, ensure_ascii=False))
    step += 1
    # Unknown step type and a malformed line (no transcript_full.jsonl)
    rows.append(json.dumps({
        "step_index": step,
        "type": "CLI_UNKNOWN_STEP",
        "created_at": now,
        "content": "<USER_REQUEST>should be ignored</USER_REQUEST>",
    }, ensure_ascii=False))
    rows.append("{malformed json line")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return art, tp


def base_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
        env.pop(var, None)
    env.update(extra)
    return env


def run_hook(script: str, payload: dict, **env_extra: str) -> tuple[int, dict | None, float, str, str]:
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, os.path.join(HOOKS, script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=base_env(**env_extra),
        timeout=10,
    )
    elapsed = time.monotonic() - t0
    parsed = None
    try:
        parsed = json.loads(r.stdout)
    except ValueError:
        pass
    return r.returncode, parsed, elapsed, r.stdout, r.stderr


cid_main = "cli-runtime-0001"
art_main, tp_main = make_cli_session(cid_main, ["List files in workspace", "Check app.py"])
common = {
    "conversationId": cid_main,
    "workspacePaths": [ws],
    "transcriptPath": tp_main,
    "artifactDirectoryPath": art_main,
    "modelName": "gemini-3.6-flash-medium",
    "lastUserInput": "Check app.py",
}

# ---- 1. All hooks exit 0, print valid JSON, finish in < 5 s ----
rc, out, dt, raw, err = run_hook("session_start.py", common)
check("session_start.py exits 0, valid JSON, < 5s", rc == 0 and isinstance(out, dict) and dt < 5.0,
      f"rc={rc} dt={dt:.2f}s err={err!r} out={raw[:120]!r}")

# 7. session_start in a CLI session does not mention Control Center / sidecar / Automations
ss_text = raw.lower()
check("session_start CLI output omits Control Center / sidecar / Automations",
      not any(w in ss_text for w in ("control center", "sidecar", "automations")),
      raw[:200])

rc, out, dt, raw, err = run_hook("instructions.py", dict(common, invocationNum=1, initialNumSteps=3))
check("instructions.py exits 0, valid JSON, < 5s", rc == 0 and isinstance(out, dict) and dt < 5.0,
      f"rc={rc} dt={dt:.2f}s err={err!r}")

rc, out, dt, raw, err = run_hook(
    "guard.py",
    dict(common, stepIdx=3, toolCall={"name": "run_command", "args": {"CommandLine": "ls", "Cwd": ws, "WaitMsBeforeAsync": 5000}}),
)
check("guard.py (run_command ls) exits 0, valid JSON, < 5s, allows ls",
      rc == 0 and isinstance(out, dict) and dt < 5.0 and out.get("decision") == "allow",
      f"rc={rc} dt={dt:.2f}s out={out} err={err!r}")

doc_tools = [
    ("write_to_file", {"TargetFile": sample_py, "Overwrite": True, "CodeContent": "def greet():\n    return 'hi'\n", "Description": "write"}),
    ("view_file", {"AbsolutePath": sample_py, "StartLine": 1, "EndLine": 5}),
    ("replace_file_content", {
        "TargetFile": sample_py, "Instruction": "update", "Description": "update",
        "AllowMultiple": False, "TargetContent": "return 'hi'", "ReplacementContent": "return 'hello'",
        "StartLine": 1, "EndLine": 2,
    }),
]
for tname, targs in doc_tools:
    rc, out, dt, raw, err = run_hook("guard.py", dict(common, stepIdx=4, toolCall={"name": tname, "args": targs}))
    check(f"guard.py ({tname}) exits 0, valid JSON, < 5s",
          rc == 0 and isinstance(out, dict) and dt < 5.0 and "decision" in out,
          f"rc={rc} dt={dt:.2f}s out={out} err={err!r}")

rc, out, dt, raw, err = run_hook(
    "check_edit.py",
    dict(common, stepIdx=5, error="", toolCall={"name": "write_to_file", "args": {"TargetFile": sample_py, "CodeContent": "def greet():\n    return 'hi'\n", "Overwrite": True}}),
)
check("check_edit.py exits 0, valid JSON, < 5s", rc == 0 and isinstance(out, dict) and dt < 5.0,
      f"rc={rc} dt={dt:.2f}s out={out} err={err!r}")

rc, out, dt, raw, err = run_hook(
    "stop.py",
    dict(common, executionNum=1, terminationReason="model_stop", error="", fullyIdle=True),
)
check("stop.py exits 0, valid JSON, < 5s", rc == 0 and isinstance(out, dict) and dt < 5.0,
      f"rc={rc} dt={dt:.2f}s out={out} err={err!r}")

# ---- 2. Dangerous command (rm -rf ~) via PreToolUse in a CLI payload is not allowed ----
rc, out, dt, raw, err = run_hook(
    "guard.py",
    dict(common, stepIdx=6, toolCall={"name": "run_command", "args": {"CommandLine": "rm -rf ~", "Cwd": ws, "WaitMsBeforeAsync": 5000}}),
)
check("guard.py blocks `rm -rf ~` in CLI payload",
      rc == 0 and isinstance(out, dict) and out.get("decision") not in ("allow", "auto_approve"),
      f"out={out}")

# ---- 3. compat.runtime() & payload without transcriptPath ----
desk_tp = os.path.join(desk_dir, "brain", "desk-0001", ".system_generated", "logs", "transcript_full.jsonl")
check("compat.runtime(CLI payload) == 'cli'", compat.runtime(common) == "cli", compat.runtime(common))
check("compat.runtime(Desktop payload) == 'desktop'", compat.runtime({"transcriptPath": desk_tp}) == "desktop",
      compat.runtime({"transcriptPath": desk_tp}))

no_tp_common = {
    "conversationId": "cli-no-tp-0001",
    "workspacePaths": [ws],
    "modelName": "gemini-3.6-flash-medium",
    "lastUserInput": "hi",
}
no_tp_cases = [
    ("session_start.py", no_tp_common),
    ("instructions.py", dict(no_tp_common, invocationNum=0, initialNumSteps=0)),
    ("guard.py", dict(no_tp_common, stepIdx=0, toolCall={"name": "run_command", "args": {"CommandLine": "ls", "Cwd": ws}})),
    ("check_edit.py", dict(no_tp_common, stepIdx=0, error="", toolCall={"name": "view_file", "args": {"AbsolutePath": sample_py}})),
    ("stop.py", dict(no_tp_common, executionNum=1, terminationReason="model_stop", error="", fullyIdle=True)),
]
for script, pl in no_tp_cases:
    rc, out, dt, raw, err = run_hook(script, pl)
    check(f"{script} without transcriptPath does not crash",
          rc == 0 and isinstance(out, dict) and dt < 5.0,
          f"rc={rc} dt={dt:.2f}s out={raw[:120]!r} err={err!r}")

# ---- 4. conversation.load() on CLI transcript ----
st = conv.load(common, save=False)
users = [u["text"] for u in (st or {}).get("users", [])]
check("conversation.load() finds the 2 CLI user messages and ignores unknown/malformed lines",
      st is not None and users == ["List files in workspace", "Check app.py"],
      users)

# ---- 5. Bypass Mode with AGP_AGY_HEADLESS=1 vs 0 ----
def reset_settings() -> None:
    if os.path.exists(settings_path):
        os.remove(settings_path)


def read_bypass_on() -> bool:
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        return bool((data.get("bypassMode") or {}).get("on"))
    except (OSError, ValueError, AttributeError):
        return False


reset_settings()
cid_hl = "cli-bypass-headless-1"
art_hl, tp_hl = make_cli_session(cid_hl, ["/bypass"])
rc, out, dt, raw, err = run_hook(
    "instructions.py",
    {
        "conversationId": cid_hl,
        "workspacePaths": [ws],
        "transcriptPath": tp_hl,
        "artifactDirectoryPath": art_hl,
        "modelName": "gemini-3.6-flash-medium",
        "lastUserInput": "/bypass",
        "invocationNum": 0,
        "initialNumSteps": 1,
    },
    AGP_AGY_HEADLESS="1",
)
check("AGP_AGY_HEADLESS=1: /bypass does not turn bypassMode on",
      rc == 0 and not read_bypass_on(),
      f"rc={rc} bypass_on={read_bypass_on()} err={err!r}")
hl_payload = {"conversationId": cid_hl, "workspacePaths": [ws], "transcriptPath": tp_hl,
              "artifactDirectoryPath": art_hl, "modelName": "gemini-3.6-flash-medium", "lastUserInput": "/bypass",
              "invocationNum": 1, "initialNumSteps": 1}
rc, out, dt, raw, err = run_hook("instructions.py", hl_payload, AGP_AGY_HEADLESS="0")
check("…that headless /bypass is marked handled: a later interactive call doesn't apply it",
      rc == 0 and not read_bypass_on(), f"rc={rc} bypass_on={read_bypass_on()} err={err!r}")

for state, label in (("?", "AGP_AGY_HEADLESS=? (can't tell)"), ("", "not under agy at all (real process check)")):
    reset_settings()
    cid_u = f"cli-bypass-unknown-{'q' if state else 'real'}"
    art_u, tp_u = make_cli_session(cid_u, ["/bypass"])
    rc, out, dt, raw, err = run_hook("instructions.py", dict(hl_payload, conversationId=cid_u, transcriptPath=tp_u,
                                                             artifactDirectoryPath=art_u, invocationNum=0),
                                     AGP_AGY_HEADLESS=state)
    check(f"{label}: a CLI /bypass is not applied", rc == 0 and not read_bypass_on(),
          f"rc={rc} bypass_on={read_bypass_on()} err={err!r}")

reset_settings()
cid_int = "cli-bypass-interactive-1"
art_int, tp_int = make_cli_session(cid_int, ["/bypass"])
rc, out, dt, raw, err = run_hook(
    "instructions.py",
    {
        "conversationId": cid_int,
        "workspacePaths": [ws],
        "transcriptPath": tp_int,
        "artifactDirectoryPath": art_int,
        "modelName": "gemini-3.6-flash-medium",
        "lastUserInput": "/bypass",
        "invocationNum": 0,
        "initialNumSteps": 1,
    },
    AGP_AGY_HEADLESS="0",
)
check("AGP_AGY_HEADLESS=0: /bypass turns bypassMode on",
      rc == 0 and read_bypass_on(),
      f"rc={rc} bypass_on={read_bypass_on()} err={err!r}")
reset_settings()

# ---- 6. hooks.json matchers match every synonym in compat.tool_names(<canonical>) ----
with open(os.path.join(PLUGIN, "hooks.json"), encoding="utf-8") as f:
    hooks_cfg = json.load(f)

matcher_failures: list[str] = []
for hname, events in hooks_cfg.items():
    for ev in ("PreToolUse", "PostToolUse"):
        for block in events.get(ev, []):
            matcher = block.get("matcher", "")
            if not matcher:
                continue
            rx = re.compile(matcher)
            canonicals = {compat.tool(part) for part in matcher.split("|") if part}
            for canon in sorted(canonicals):
                for syn in compat.tool_names(canon):
                    if not rx.fullmatch(syn):
                        matcher_failures.append(f"{hname}.{ev}: {matcher!r} misses {syn!r} (from {canon})")

check("hooks.json PreToolUse/PostToolUse matchers match all compat.tool_names synonyms",
      not matcher_failures,
      matcher_failures)

# ---- check_edit.py / memory_sync.py with the docs' older tool and argument names ----
cid_ce = "cli-check-edit-0001"
art_ce, tp_ce = make_cli_session(cid_ce, ["fix the module"])
broken = os.path.join(ws, "broken_mod.py")
with open(broken, "w", encoding="utf-8") as f:
    f.write("def f(:\n    pass\n")
ce_payload = {"conversationId": cid_ce, "workspacePaths": [ws], "transcriptPath": tp_ce,
              "artifactDirectoryPath": art_ce, "modelName": "gemini-4-hydrogen-low", "error": "",
              "toolCall": {"name": "write_to_file", "args": {"TargetFile": broken, "CodeContent": "def f(:\n"}}}
rc, out, dt, raw, err = run_hook("check_edit.py", ce_payload)
rc2, out2, dt2, raw2, err2 = run_hook("instructions.py", {k: v for k, v in ce_payload.items() if k not in ("toolCall", "error")})
check("check_edit: write_to_file {TargetFile} of a broken .py → the next model call hears about it",
      rc == 0 and rc2 == 0 and "broken_mod.py" in raw2, f"rc={rc}/{rc2} err={err!r}/{err2!r} out={raw2[:300]!r}")
rc, out, dt, raw, err = run_hook("check_edit.py", dict(ce_payload, toolCall={"name": "view_file",
                                                                             "args": {"AbsolutePath": broken}}))
check("check_edit: view_file {AbsolutePath} runs cleanly", rc == 0 and raw.strip() == "{}", f"rc={rc} err={err!r}")

mem_dir = os.path.join(home, ".gemini", "antigravity-plus", "memory", "personal")
os.makedirs(mem_dir, exist_ok=True)
note = os.path.join(mem_dir, "cli-note.md")
with open(note, "w", encoding="utf-8") as f:
    f.write("---\nname: cli-note\ndescription: a note written from a CLI session\n---\nbody\n")
rc, out, dt, raw, err = run_hook("memory_sync.py", dict(ce_payload, toolCall={"name": "write_to_file",
                                                                              "args": {"TargetFile": note}}))
try:
    index = open(os.path.join(mem_dir, "MEMORY.md"), encoding="utf-8").read()
except OSError:
    index = ""
check("memory_sync: write_to_file {TargetFile} of a memory → MEMORY.md lists it", rc == 0 and "cli-note" in index,
      f"rc={rc} err={err!r} index={index[:200]!r}")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
