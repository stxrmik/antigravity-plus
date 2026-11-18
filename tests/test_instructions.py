"""PreInvocation: the user's messages come back on a new message, in a long turn and after compaction; a ban
(«ничего не пушь») is listed as in force."""
import json, os, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)
cid = "agp-selftest-0001"
tp = os.path.join(SP, "fake_transcript.jsonl")
step = 0
def add(kind, text=""):
    global step
    row = {"step_index": step, "source": "X", "type": kind, "status": "DONE"}
    if kind == "USER_INPUT": row["content"] = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>"
    elif kind == "CHECKPOINT": row["content"] = "# Resuming from a compaction"
    elif text: row["content"] = text
    with open(tp, "a", encoding="utf-8") as f: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    step += 1
ok = True
def _step_msg(step):
    return (step.get("systemMessage") or {}).get("systemMessage") or step.get("ephemeralMessage") or ""

def call(label, want, last=None, has=""):
    global ok
    payload = {"conversationId": cid, "transcriptPath": tp, "lastUserInput": last or ""}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(payload), capture_output=True, text=True).stdout
    d = json.loads(out or "{}")
    steps = d.get("injectSteps") or []
    msg = _step_msg(steps[0]) if steps else None
    is_sys = (not steps) or ("systemMessage" in steps[0] and "ephemeralMessage" not in steps[0])
    good = bool(msg) == want and is_sys and (not has or has in (msg or ""))
    ok &= good
    print(f"{'✅' if good else '❌'} {label:<45} → {'reminder' if msg else '-'}")
add("USER_INPUT", "Сделай консоль VPN на FastAPI")
for _ in range(5): add("PLANNER_RESPONSE")
call("first message, 5 steps", False, "Сделай консоль VPN на FastAPI")
add("USER_INPUT", "И еще, делай все внутри Docker контейнера")
call("a second message arrives", True, "И еще, делай все внутри Docker контейнера", has="Docker")
call("same invocation again", False)
for _ in range(20): add("PLANNER_RESPONSE")
call("20 steps later", False)
for _ in range(15): add("PLANNER_RESPONSE")
call("35 steps since the message (long turn)", True)
for _ in range(10): add("PLANNER_RESPONSE")
call("10 more steps", False)
add("CHECKPOINT")
call("after context compaction", True)
add("USER_INPUT", "/usage")
call("a meta command (/usage)", False, "/usage")
add("USER_INPUT", "Самое главное, пока что ничего не пушь на сервер")
call("a ban («ничего не пушь»)", True, "Самое главное, пока что ничего не пушь на сервер", has="In force now")

# orchestrator_work wires orchestrator.handoff_message (real orchestrator.py)
import io  # noqa: E402
import instructions  # type: ignore[import-not-found]  # noqa: E402
s1 = instructions.make_step("hello", "ui banner")
s2 = instructions.make_step("only model")
s3 = instructions.make_step("", "zero token")
good_steps = (
    s1 == {"systemMessage": {"systemMessage": "hello", "metadata": {"user_markdown": "ui banner"}}}
    and s2 == {"systemMessage": {"systemMessage": "only model"}}
    and s3 == {"systemMessage": {"systemMessage": "", "metadata": {"user_markdown": "zero token"}}}
)
ok &= good_steps
print(f"{'✅' if good_steps else '❌'} {'make_step builds systemMessage + metadata':<45}")

add("SYSTEM_MESSAGE", "Plan mode is off: this turn runs on the execution model")
r_orch = subprocess.run(
    [sys.executable, os.path.join(HOOKS, "instructions.py")],
    input=json.dumps({"conversationId": cid, "transcriptPath": tp, "lastUserInput": ""}),
    capture_output=True, text=True, encoding="utf-8",
)
orch_steps = json.loads(r_orch.stdout or "{}").get("injectSteps", [])
orch_ok = any("orchestrator_handoff" in _step_msg(s) and "no push" in _step_msg(s) for s in orch_steps)
# once per handoff
r_orch2 = subprocess.run(
    [sys.executable, os.path.join(HOOKS, "instructions.py")],
    input=json.dumps({"conversationId": cid, "transcriptPath": tp, "lastUserInput": ""}),
    capture_output=True, text=True, encoding="utf-8",
)
orch_ok &= not any("orchestrator_handoff" in _step_msg(s) for s in json.loads(r_orch2.stdout or "{}").get("injectSteps", []))
ok &= orch_ok
print(f"{'✅' if orch_ok else '❌'} {'orchestrator_work wires handoff_message':<45}")

# uiNotices=true keeps empty-model user_markdown; uiNotices=false omits metadata & drops empty-model steps
settings_path = os.path.expanduser("~/.gemini/antigravity-plus/settings.json")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
import orchestrator as real_orch  # type: ignore[import-not-found]  # noqa: E402
orig_handoff = real_orch.handoff_message
def _call_main_with_ui(enabled: bool) -> dict:
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump({"uiNotices": enabled}, fh)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"conversationId": cid, "transcriptPath": tp, "lastUserInput": ""}))
    sys.stdout = io.StringIO()
    try:
        instructions.main()
        return json.loads(sys.stdout.getvalue() or "{}")
    finally:
        sys.stdin, sys.stdout = old_in, old_out

real_orch.handoff_message = lambda _p, _s: ("", "UI ONLY BANNER")
try:
    d_on = _call_main_with_ui(True)
    d_off = _call_main_with_ui(False)
finally:
    real_orch.handoff_message = orig_handoff
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump({"uiNotices": True}, fh)
ui_steps_on = d_on.get("injectSteps") or []
ui_steps_off = d_off.get("injectSteps") or []
ui_off_ok = (
    any((s.get("systemMessage") or {}).get("systemMessage") == ""
        and ((s.get("systemMessage") or {}).get("metadata") or {}).get("user_markdown") == "UI ONLY BANNER"
        for s in ui_steps_on)
    and not any("metadata" in (s.get("systemMessage") or {}) for s in ui_steps_off)
    and not any((s.get("systemMessage") or {}).get("systemMessage") == "" for s in ui_steps_off)
)
ok &= ui_off_ok
print(f"{'✅' if ui_off_ok else '❌'} {'uiNotices=false omits metadata & empty steps':<45}")

# session_start.py emits second zero-token user_markdown step for update_notice (unless uiNotices=false)
ws_ss = os.path.join(SP, "ws_ss"); os.makedirs(ws_ss, exist_ok=True)
upd_cache = os.path.expanduser("~/.gemini/antigravity-plus/update.json")
with open(upd_cache, "w", encoding="utf-8") as f:
    json.dump({"checked": 9999999999, "latest": "99.0.0", "notes": ["New features"], "told": {}}, f)
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump({"uiNotices": True}, f)
ss_env = {k: v for k, v in os.environ.items() if k != "AGP_UPDATE_CHECK"}
r_ss_on = subprocess.run(
    [sys.executable, os.path.join(HOOKS, "session_start.py")],
    input=json.dumps({"conversationId": "agp-ss-ui-001", "workspacePaths": [ws_ss], "lastUserInput": "Привет"}),
    capture_output=True, text=True, encoding="utf-8", env=ss_env,
)
ss_steps = json.loads(r_ss_on.stdout or "{}").get("injectSteps", [])
ss_ui_ok = (
    len(ss_steps) == 2
    and "99.0.0" in ss_steps[0]["systemMessage"]["systemMessage"]
    and ss_steps[1]["systemMessage"]["systemMessage"] == ""
    and "Доступно обновление Antigravity Plus 99.0.0" in (ss_steps[1]["systemMessage"].get("metadata") or {}).get("user_markdown", "")
)
with open(upd_cache, "w", encoding="utf-8") as f:
    json.dump({"checked": 9999999999, "latest": "99.0.0", "notes": ["New features"], "told": {}}, f)
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump({"uiNotices": False}, f)
r_ss_off = subprocess.run(
    [sys.executable, os.path.join(HOOKS, "session_start.py")],
    input=json.dumps({"conversationId": "agp-ss-ui-002", "workspacePaths": [ws_ss], "lastUserInput": "Привет"}),
    capture_output=True, text=True, encoding="utf-8", env=ss_env,
)
ss_steps_off = json.loads(r_ss_off.stdout or "{}").get("injectSteps", [])
ss_ui_ok &= len(ss_steps_off) == 1 and "99.0.0" in ss_steps_off[0]["systemMessage"]["systemMessage"]
ok &= ss_ui_ok
print(f"{'✅' if ss_ui_ok else '❌'} {'session_start update_notice user_markdown':<45}")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
