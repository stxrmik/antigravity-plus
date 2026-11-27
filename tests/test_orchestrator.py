"""Orchestrator phase & plan checks (hooks/orchestrator.py) and guard.py `exit_plan_mode` enforcement."""
import json
import os
import shutil
import subprocess
import sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)

import client  # type: ignore[import-not-found]
import conversation as conv  # type: ignore[import-not-found]
import coord  # type: ignore[import-not-found]
import orchestrator  # type: ignore[import-not-found]
import stop  # type: ignore[import-not-found]

ws = os.path.realpath(os.path.join(SP, "orch_ws"))
art = os.path.join(SP, "orch_art")
shutil.rmtree(ws, ignore_errors=True)
shutil.rmtree(art, ignore_errors=True)
os.makedirs(ws)
os.makedirs(art)

tp = os.path.join(art, "transcript_full.jsonl")
cid = "agp-orch-test-0001"
other_cid = "agp-orch-other-0002"
ok = True


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(detail)[:300]}"))


def write_transcript(rows, path=tp):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def real(body):
    """A SYSTEM_MESSAGE step's content as Antigravity writes it: a preamble that names the tag itself, then the wrapper."""
    return ("The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as important "
            f"information to pay attention to.\n\n<SYSTEM_MESSAGE>\n{body}\n</SYSTEM_MESSAGE>")


def run_guard(plan_text, extra=None, extra_args=None):
    args = {"plan": plan_text, **(extra_args or {})}
    payload = {"conversationId": cid, "transcriptPath": tp, "workspacePaths": [ws],
               "toolCall": {"name": "exit_plan_mode", "args": args}, **(extra or {})}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PLUGIN)
    if r.stderr.strip():
        print("   stderr:", r.stderr.strip()[:200])
    return json.loads(r.stdout or "{}")


# 1. check_plan: active bans
push_ban_users = [{"n": 1, "text": "Сделай фичу, но ничего не пушь"}]
for action in ("git push origin main", "gh pr create --fill", "./deploy.sh prod",
               "scp dist/app.tgz web:/opt/", "rsync -av dist/ web:/opt/", "kubectl apply -f k8s.yaml"):
    hit = orchestrator.check_plan({}, None, push_ban_users, {"plan": f"1. Edit app.py\n2. Run {action}\n3. Run pytest"})
    check(f"check_plan catches push ban violation: {action}", hit is not None and hit[0] == "orchestrator-ban-in-plan", hit)

for safe_line in ("Do not run git push", "Never ./deploy.sh", "Keep changes local without push",
                  "Skip push unless asked by the user", "No push to origin",
                  "Не пушить изменения на сервер", "Без пуша, не отправлять в remote"):
    hit = orchestrator.check_plan({}, None, push_ban_users, {"plan": f"1. Edit app.py\n2. {safe_line}\n3. Run pytest"})
    check(f"check_plan ignores negated line: {safe_line}", hit is None, hit)

commit_ban_users = [{"n": 1, "text": "Fix bug, don't commit yet"}]
chit = orchestrator.check_plan({}, None, commit_ban_users, {"plan": "1. Edit app.py\n2. Run git commit -m 'fix'\n3. Run pytest"})
check("check_plan catches commit ban violation", chit is not None and chit[0] == "orchestrator-ban-in-plan", chit)
cnote = orchestrator.check_plan({}, None, commit_ban_users, {"plan": "1. Edit app.py\n2. Never run git commit\n3. Run pytest"})
check("check_plan ignores negated commit line", cnote is None, cnote)

# a plan that honours the ban in its own words is no violation; a mention, a quoted message or a local copy isn't a step
for safe_line in ("3. Не делать `git push` — пользователь запретил.", "3. `git push` не выполнять (запрет пользователя).",
                  "3. Без `git push`.", "3. No `git push` (user asked not to).", "3. We will not run git push.",
                  "3. Avoid `git push` until the user says so.", "3. Skip the `git push` step.",
                  "3. `git status` to confirm the tree; nothing to push yet.", "3. `git push` — only after the user's OK.",
                  "3. rsync -a assets/ build/assets/", "3. scp user@host:/var/log/app.log ./logs/",
                  '3. `git commit -m "Add git push retry"`', "## Out of scope\n- git push\n- gh pr create"):
    hit = orchestrator.check_plan({}, None, push_ban_users, {"plan": f"1. Edit app.py\n2. Run pytest\n{safe_line}"})
    check(f"check_plan: not a push step under the ban: {safe_line[:44]}", hit is None, hit)
for step in ("Do not commit, but push the branch: git push -u origin feat", "2. Copy with `rsync -av dist/ web:/opt/app/` "
             "and restart", "3. `git push origin main` (not --force)", "2. npm publish --access public"):
    hit = orchestrator.check_plan({}, None, push_ban_users, {"plan": f"1. Edit app.py\n{step}\n3. Run pytest"})
    check(f"check_plan: still a push step: {step[:44]}", hit is not None and hit[0] == "orchestrator-ban-in-plan", hit)
for safe_line in ("1. Look at the last git commit message to match the style.", "2. Не делать `git commit` — запрет.",
                  "2. Leave the changes uncommitted (no `git commit`).", "2. Без `git commit`."):
    hit = orchestrator.check_plan({}, None, commit_ban_users, {"plan": f"{safe_line}\n3. Run pytest"})
    check(f"check_plan: not a commit step under the ban: {safe_line[:44]}", hit is None, hit)
check("check_plan: the ban reason asks the user (guard turns it into force_ask) and names the step",
      chit is not None and "Подтвердите" in chit[1] and "git commit -m 'fix'" in chit[1], chit)

# 2. check_plan: verification gate (refused once per plan and turn: it needs the conversation's state)
long_body = " ".join(["Update implementation details across multiple functions carefully."] * 6)
unverified = f"1. Modify src/server.py and web/app.ts to handle sessions.\n{long_body}"
plan_n = [0]


def plan_hit(plan_text, users=None):
    plan_n[0] += 1  # a fresh conversation each time: the refuse-once memory stays out of the way
    st_x = {"users": users or [{"step": 0, "ts": "", "text": "Fix auth"}], "checkpoints": [], "last_step": 0}
    return orchestrator.check_plan({"conversationId": f"agp-orch-plan-{plan_n[0]:04d}"}, st_x,
                                   st_x["users"], {"plan": plan_text})


vhit = plan_hit(unverified)
check("check_plan blocks >=300 char source plan with no verification step",
      vhit is not None and vhit[0] == "orchestrator-unverified-plan", vhit)
check("…and its reason says the same plan sent again goes through", vhit is not None and "тот же план" in vhit[1], vhit)

for vstep in ("Run pytest -q", "Run python3 tests/run_tests.py", "Run ruff check and build",
              "Verify with ui_check.py", "Check on emulator via mobile.py", "Run doctor.py",
              "Запустить тесты и проверить сборку", "Run `npx tsc --noEmit` and `npx eslint src`",
              "Run `xcodebuild -scheme App`", "Run `mypy app` and `ruff app`",
              "Запустить `python3 app/main.py --demo` и убедиться, что вывод корректный"):
    ok_plan = f"{unverified}\n2. {vstep}"
    check(f"check_plan accepts verification step: {vstep}", plan_hit(ok_plan) is None)

pad = "\n" + " ".join(["Context: the configuration parser and its options."] * 6)
for docs_plan in ("Обновить README.md и docs/api.md: описать параметры функции load() из src/api.py, добавить пример.",
                  "Update the CHANGELOG.md and the README.md to say Node.js 22 is required.",
                  "1. Describe the --fast flag in docs/cli.md. No code changes."):
    check(f"check_plan: a docs-only plan naming a source file needs no check: {docs_plan[:40]}",
          plan_hit(docs_plan + pad) is None)

short_plan = "1. Edit src/server.py to fix typo."
check("check_plan skips short (<300 char) plan", plan_hit(short_plan) is None)

cid_once = "agp-orch-once-0001"
st_once = {"users": [{"step": 0, "ts": "", "text": "Fix auth"}], "checkpoints": [], "last_step": 0}
first = orchestrator.check_plan({"conversationId": cid_once}, st_once, st_once["users"], {"plan": unverified})
again = orchestrator.check_plan({"conversationId": cid_once}, st_once, st_once["users"], {"plan": unverified})
stored_once = (conv._state(cid_once) or {}).get("orch_plan_check") or {}
check("check_plan refuses a plan once: the same plan again goes through (no deny loop)",
      first is not None and again is None and len(stored_once.get("refused") or []) == 1, (first, again, stored_once))
st_once["users"].append({"step": 9, "ts": "", "text": "Now add sessions"})
later = orchestrator.check_plan({"conversationId": cid_once}, st_once, st_once["users"], {"plan": unverified + " (v2)"})
check("…a new unverified plan in a later user turn is refused again (once)", later is not None, later)
check("check_plan never refuses without a state to remember it in",
      orchestrator.check_plan({}, None, [{"n": 1, "text": "Fix auth"}], {"plan": unverified}) is None)

settings_path = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus", "settings.json")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump({"autoVerify": False}, f)
check("check_plan respects autoVerify: false", plan_hit(unverified) is None)
os.remove(settings_path)

# 3. handoff_message & in_plan_mode
base_payload = {"conversationId": cid, "transcriptPath": tp, "workspacePaths": [ws], "modelName": "Orchestrator (Carbon)"}
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Add feature, но ничего не пушь</USER_REQUEST>"},
    {"step_index": 1, "type": "SYSTEM_MESSAGE", "source": "SYSTEM_SDK",
     "content": "<SYSTEM_MESSAGE>\n<memory>Note mentions «Plan mode is off: this turn runs on the execution model».</memory>\n</SYSTEM_MESSAGE>"},
    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "read_file", "args": {}}]},
])
st = conv.load(base_payload)
check("in_plan_mode is True before Plan mode is off (ignoring SYSTEM_SDK quote)",
      orchestrator.in_plan_mode(base_payload, st))
check("in_plan_mode is False for non-orchestrator model",
      not orchestrator.in_plan_mode({**base_payload, "modelName": "gemini-4-carbon-high"}, st))
check("handoff_message stays quiet before handoff", orchestrator.handoff_message(base_payload, st) is None)

# Handoff to Hydrogen with active push ban + another active agent
coord.record({"conversationId": other_cid, "workspacePaths": [ws]}, "edit_file", os.path.join(ws, "other.py"))
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Add feature, но ничего не пушь</USER_REQUEST>"},
    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Edit a.py\n2. Run pytest"}}]},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model. Carry out the plan.\n</SYSTEM_MESSAGE>"},
])
st = conv.load(base_payload)
hmsg = orchestrator.handoff_message(base_payload, st)
check("in_plan_mode becomes False after Plan mode is off", not orchestrator.in_plan_mode(base_payload, st))
check("handoff_message emits <orchestrator_handoff> with bans and other agents",
      bool(hmsg) and "<orchestrator_handoff" in hmsg and "push" in hmsg and other_cid in hmsg, hmsg)
check("st['orch_handoff_step'] updated and second call returns None",
      st.get("orch_handoff_step") == 3 and orchestrator.handoff_message(base_payload, st) is None, st)

# Handoff without bans/full-access/others returns None even though orch_handoff_step advances
reg_file = os.path.join(coord.COORD_DIR, coord.slug(ws) + ".json")
for p in (reg_file, reg_file + ".lock"):
    if os.path.exists(p):
        os.remove(p)
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Add feature</USER_REQUEST>"},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
])
st_clean = {"users": [{"step": 0, "ts": "", "text": "Add feature"}], "checkpoints": [], "last_step": 3}
check("handoff_message returns None when no bans, full access, or other agents exist",
      orchestrator.handoff_message(base_payload, st_clean) is None and st_clean.get("orch_handoff_step") == 3, st_clean)

# Hand back to planner via enter_plan_mode -> in_plan_mode True & <orchestrator_review>
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Add feature</USER_REQUEST>"},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
    {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "enter_plan_mode", "args": {}}]},
])
st_rev = conv.load(base_payload)
rmsg = orchestrator.handoff_message(base_payload, st_rev)
check("in_plan_mode returns True after enter_plan_mode", orchestrator.in_plan_mode(base_payload, st_rev))
check("handoff_message emits <orchestrator_review> on enter_plan_mode",
      bool(rmsg) and "<orchestrator_review" in rmsg and "git diff" in rmsg, rmsg)
check("review message emitted only once per step", orchestrator.handoff_message(base_payload, st_rev) is None)

# Also test [plan review] SYSTEM_MESSAGE
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Add feature</USER_REQUEST>"},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
    {"step_index": 6, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\n[plan review] Execution finished; review results.\n</SYSTEM_MESSAGE>"},
])
st_rev2 = {"users": [{"step": 0, "ts": "", "text": "Add feature"}], "checkpoints": [], "last_step": 6}
rmsg2 = orchestrator.handoff_message(base_payload, st_rev2)
check("in_plan_mode returns True after [plan review] SYSTEM_MESSAGE", orchestrator.in_plan_mode(base_payload, st_rev2))
check("handoff_message emits <orchestrator_review> on [plan review]",
      bool(rmsg2) and "<orchestrator_review" in rmsg2, rmsg2)

# Configurable Orchestrator (2.34+/2.35): modelName is the planner's own model, annotations/<cid>.pbtxt has plan_execute:true,
# and handoff names a configured executor (e.g. Claude Sonnet 5.5 (High)). Subagent [Message] mentioning [plan review] or
# [plan review due] must NOT trigger handback or <orchestrator_review>.
cfg_cid = "agp-orch-cfg-0001"
cfg_root = os.path.join(SP, "ag_cfg_root")
cfg_logs = os.path.join(cfg_root, "brain", cfg_cid, ".system_generated", "logs")
cfg_ann = os.path.join(cfg_root, "annotations")
os.makedirs(cfg_logs, exist_ok=True)
os.makedirs(cfg_ann, exist_ok=True)
cfg_tp = os.path.join(cfg_logs, "transcript_full.jsonl")
with open(os.path.join(cfg_ann, f"{cfg_cid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"Configurable Orch" plan_execute:true')
cfg_payload = {"conversationId": cfg_cid, "transcriptPath": cfg_tp, "workspacePaths": [ws], "modelName": "Claude Opus 5.5 (High)"}
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Build feature, не коммить</USER_REQUEST>"},
    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "read_file", "args": {}}]},
], path=cfg_tp)
st_cfg = conv.load(cfg_payload)
check("configurable Orchestrator (plan_execute:true annotation): in_plan_mode is True before handoff",
      orchestrator.in_plan_mode(cfg_payload, st_cfg))
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Build feature, не коммить</USER_REQUEST>"},
    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Edit a.py\n2. Run pytest"}}]},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on Claude Sonnet 5.5 (High). Carry out the plan below now.\n</SYSTEM_MESSAGE>"},
    {"step_index": 4, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:00:04Z sender=sub1 priority=MESSAGE_PRIORITY_HIGH content=Subagent checked [plan review] strings.\n</SYSTEM_MESSAGE>"},
    {"step_index": 5, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\n[plan review due] 5 invocations have run.\n</SYSTEM_MESSAGE>"},
], path=cfg_tp)
st_cfg = conv.load(cfg_payload)
h_cfg = orchestrator.handoff_message(cfg_payload, st_cfg)
check("configurable Orchestrator: in_plan_mode is False during execution (ignores [Message] & [plan review due])",
      not orchestrator.in_plan_mode(cfg_payload, st_cfg))
check("configurable Orchestrator: <orchestrator_handoff> captures versioned executor role (Claude Sonnet 5.5 (High))",
      bool(h_cfg) and "<orchestrator_handoff" in h_cfg and "Claude Sonnet 5.5 (High)" in h_cfg and "<orchestrator_review" not in h_cfg, h_cfg)
m_single = orchestrator._handoff_role("<SYSTEM_MESSAGE>Plan mode is off: this turn runs on Claude Sonnet 5.5 (High).</SYSTEM_MESSAGE>")
check("hand-over role drops the trailing </SYSTEM_MESSAGE> on a single-line handoff", m_single == "Claude Sonnet 5.5 (High)", m_single)
# In plan_execute:true plan mode (in_plan=True), stop.decide() skips open task.md & announcement() while keeping facts_check
cfg_art = os.path.join(cfg_root, "art")
os.makedirs(cfg_art, exist_ok=True)
with open(os.path.join(cfg_art, "task.md"), "w", encoding="utf-8") as f:
    f.write("## Plan\n- [ ] Pending step\n")
def clear_cfg_state():
    for sf in os.listdir(conv.STATE_DIR):
        if sf.startswith(cfg_cid):
            os.remove(os.path.join(conv.STATE_DIR, sf))


def cfg_stop(rows, label, want):
    """stop.decide() for the configurable-Orchestrator conversation; want(res) says what it must have answered."""
    clear_cfg_state()
    write_transcript(rows, path=cfg_tp)
    res = stop.decide({"conversationId": cfg_cid, "transcriptPath": cfg_tp, "artifactDirectoryPath": cfg_art,
                       "workspacePaths": [ws], "modelName": "Claude Opus 5.5 (High)",
                       "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0})
    check(f"plan_execute:true stop.decide() ({label})", want(res), res)


U = {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-24T10:00:00Z", "content": "<USER_REQUEST>Plan feature</USER_REQUEST>"}
reply_row = lambda text, i=9: {"step_index": i, "type": "PLANNER_RESPONSE", "created_at": "2026-11-24T10:00:09Z", "content": text}
hand_row = {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-24T10:00:01Z", "content": "",
            "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Edit a.py\n2. Run pytest"}}]}
review_row = {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-11-24T10:00:03Z",
              "content": real("[plan review] Plan mode is on: this turn runs on the planning model. the execution model handed back.")}
# a planning turn that ends WITHOUT handing the plan over, and a [plan review] turn: task.md, announcements and facts are
# checked as in 0.27.1; only a turn that ended by handing the plan over (last tool call exit_plan_mode) skips task.md
# and announcements, because the executor carries on
cfg_stop([U, reply_row("План готов.", 1)], "planning turn, open task.md: nudged",
         lambda r: bool(r) and "Pending step" in r)
cfg_stop([U, reply_row("Сейчас запущу тесты.", 1)], "planning turn, announcement: nudged",
         lambda r: bool(r) and "ended your turn without doing it" in r)
with open(os.path.join(cfg_art, "task.md"), "w", encoding="utf-8") as f:  # (with open items the task.md nudge comes first)
    f.write("## Plan\n- [x] Done step\n")
cfg_stop([U, reply_row("Все тесты проходят.", 1)], "planning turn, unbacked claim: facts check", lambda r: "facts" in (r or ""))
with open(os.path.join(cfg_art, "task.md"), "w", encoding="utf-8") as f:
    f.write("## Plan\n- [ ] Pending step\n")
cfg_stop([U, hand_row, reply_row("План готов.", 2)], "plan handed over, open task.md: not nudged", lambda r: r is None)
cfg_stop([U, hand_row, reply_row("Сейчас запущу тесты.", 2)], "plan handed over, announcement: not nudged", lambda r: r is None)
cfg_stop([U, hand_row, reply_row("Все тесты проходят.", 2)], "plan handed over, unbacked claim: facts check", lambda r: "facts" in (r or ""))
cfg_stop([U, hand_row, review_row, reply_row("План готов.", 4)], "[plan review] turn, open task.md: nudged",
         lambda r: bool(r) and "Pending step" in r)
cfg_stop([U, hand_row, review_row, reply_row("Сейчас запущу тесты.", 4)], "[plan review] turn, announcement: nudged",
         lambda r: bool(r) and "ended your turn without doing it" in r)
cfg_stop([U, hand_row, review_row, {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "x"}}]},
          reply_row("План готов.", 5)], "[plan review] turn that hands a new plan over: not nudged", lambda r: r is None)
check("handed_over: the last tool call of the turn is exit_plan_mode",
      (write_transcript([U, hand_row, reply_row("x", 2)], path=cfg_tp), orchestrator.handed_over(cfg_payload))[1])
check("handed_over: not before the plan is handed over",
      (write_transcript([U, reply_row("x", 1)], path=cfg_tp), not orchestrator.handed_over(cfg_payload))[1])
check("handed_over: not once the executor handed back ([plan review] after exit_plan_mode)",
      (write_transcript([U, hand_row, review_row], path=cfg_tp), not orchestrator.handed_over(cfg_payload))[1])
check("handed_over: not when a later tool call is something else",
      (write_transcript([U, hand_row, {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "read_file", "args": {}}]}],
                        path=cfg_tp), not orchestrator.handed_over(cfg_payload))[1])
check("handed_over: an exit_plan_mode of the previous turn does not count",
      (write_transcript([U, hand_row, {**U, "step_index": 5}], path=cfg_tp), not orchestrator.handed_over(cfg_payload))[1])

# Large (>64 KB) Hydrogen execution turn must still stay in execution mode (in_plan_mode == False)
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Build large feature</USER_REQUEST>"},
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
    {"step_index": 4, "type": "GENERIC", "content": "big output: " + ("x" * 75_000)},
])
st_big = {"users": [{"step": 0, "ts": "", "text": "Build large feature"}], "checkpoints": [], "last_step": 4}
check("in_plan_mode remains False when Hydrogen output exceeds 64 KB", not orchestrator.in_plan_mode(base_payload, st_big))

# New USER_INPUT turn resets phase back to plan mode and ignores earlier turn's enter_plan_mode
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task 1</USER_REQUEST>"},
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
    {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "enter_plan_mode", "args": {}}]},
    {"step_index": 7, "type": "USER_INPUT", "content": "<USER_REQUEST>Task 2</USER_REQUEST>"},
])
st_new = {"users": [{"step": 0, "ts": "", "text": "Task 1"}, {"step": 7, "ts": "", "text": "Task 2"}],
          "checkpoints": [], "last_step": 7}
check("new USER_INPUT turn starts back in plan mode", orchestrator.in_plan_mode(base_payload, st_new))
check("new USER_INPUT turn does not fire old turn's enter_plan_mode review",
      orchestrator.handoff_message(base_payload, st_new) is None)

# ── regressions of the 0.28.0 detection ──
def fresh_state(c):
    for sf in os.listdir(conv.STATE_DIR):
        if sf.startswith(c):
            os.remove(os.path.join(conv.STATE_DIR, sf))


# the annotation: a field, not the words in a title
for title, want in [('fix plan_execute:true parsing', False), ('say \\"x\\" plan_execute:true', False)]:
    txt = f'title:"{title}" last_user_view_time:{{seconds:1795440871 nanos:731000000}}'
    check(f"annotation titled «{title}» is no plan_execute", orchestrator._pbtxt_plan_execute(txt) is want, txt)
check("annotation with the real field after a title that quotes it is plan_execute",
      orchestrator._pbtxt_plan_execute('title:"fix plan_execute:true parsing" last_user_view_time:{seconds:1795440871 nanos:731000000} plan_execute:true'))
check("annotation: plan_execute:true alone is plan_execute", orchestrator._pbtxt_plan_execute("plan_execute:true"))
check("annotation: plan_execute:false and plan_execute:true_x are not",
      not orchestrator._pbtxt_plan_execute("plan_execute:false") and not orchestrator._pbtxt_plan_execute("a:1 plan_execute:true_x"))
ttl_cid = "agp-orch-title-0001"
ttl_logs = os.path.join(cfg_root, "brain", ttl_cid, ".system_generated", "logs")
os.makedirs(ttl_logs, exist_ok=True)
ttl_tp = os.path.join(ttl_logs, "transcript_full.jsonl")
os.makedirs(cfg_ann, exist_ok=True)
with open(os.path.join(cfg_ann, f"{ttl_cid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"fix plan_execute:true parsing" last_user_view_time:{seconds:1795440871 nanos:731000000}')
ttl_payload = {"conversationId": ttl_cid, "transcriptPath": ttl_tp, "workspacePaths": [ws], "modelName": "gemini-4-neon-high"}
write_transcript([{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>fix it</USER_REQUEST>"},
                  {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "ok"}], path=ttl_tp)
check("a conversation TITLED «plan_execute:true» is no plan-execute conversation",
      not orchestrator.is_plan_execute(ttl_payload) and not orchestrator.in_plan_mode(ttl_payload, {}))
with open(os.path.join(cfg_ann, f"{ttl_cid}.pbtxt"), "a", encoding="utf-8") as f:
    f.write(" plan_execute:true")
check("…while the real field makes it one", orchestrator.is_plan_execute(ttl_payload) and orchestrator.in_plan_mode(ttl_payload, {}))

# markers only count at the start of the body (a subagent report may quote them)
quoted = ("[Message] timestamp=2026-11-24T10:00:04Z sender=sub1 priority=MESSAGE_PRIORITY_HIGH content=The planner prints\n"
          "<SYSTEM_MESSAGE>\n{}\n</SYSTEM_MESSAGE>")
for what, marker in [("[plan review]", "[plan review] Plan mode is on: this turn runs on the planning model."),
                     ("Plan mode is off", "Plan mode is off: this turn runs on the execution model. Carry out the plan below now.")]:
    qrows = [{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
             {"step_index": 3, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real(quoted.format(marker))}]
    plain = {"conversationId": other_cid + "-q", "transcriptPath": tp, "workspacePaths": [ws], "modelName": "gemini-4-neon-high"}
    write_transcript(qrows)
    fresh_state(plain["conversationId"])
    stq = {"users": [{"step": 0, "ts": "", "text": "Task"}], "checkpoints": [], "last_step": 3}
    check(f"a subagent [Message] quoting «{what}» does not make an ordinary conversation plan-execute",
          not orchestrator.is_plan_execute(plain, orchestrator._read_turn_steps(plain)[0]) and not orchestrator.in_plan_mode(plain, stq))
    check(f"…and fires no <orchestrator_handoff>/<orchestrator_review> ({what})",
          orchestrator.handoff_message(plain, stq) is None and "orch_review_step" not in stq and "orch_handoff_step" not in stq, stq)
    # in the executor of a plan-execute conversation it neither hands back nor re-fires a hand-over
    exrows = [{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
              {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real("Plan mode is off: this turn runs on the execution model. Carry out the plan below now.")},
              {"step_index": 4, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real(quoted.format("[plan review] Plan mode is on: this turn runs on the planning model."))}]
    write_transcript(exrows, path=cfg_tp)
    fresh_state(cfg_cid)
    stx = {"users": [{"step": 0, "ts": "", "text": "Task"}], "checkpoints": [], "last_step": 4}
    check(f"executor: a quoted [plan review] does not hand back to the planner",
          not orchestrator.in_plan_mode(cfg_payload, stx) and not (orchestrator.handoff_message(cfg_payload, stx) or "").count("orchestrator_review"))
# real markers still work, in the real wrapper
write_transcript([{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
                  {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
                   "content": real("Plan mode is off: this turn runs on the execution model. Carry out the plan below now.\n\n1. Edit a.py")}], path=cfg_tp)
fresh_state(cfg_cid)
st_real = {"users": [{"step": 0, "ts": "", "text": "Task, но ничего не пушь"}], "checkpoints": [], "last_step": 2}
h_real = orchestrator.handoff_message(cfg_payload, st_real)
check("real wrapped hand-over: executor, handoff message says «execution model» (no role cruft)",
      not orchestrator.in_plan_mode(cfg_payload, st_real) and bool(h_real) and "Hand-off to execution model:" in h_real, h_real)
write_transcript([{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
                  {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real("[plan review] Plan mode is on: this turn runs on the planning model. the execution model handed back.")}], path=cfg_tp)
check("real wrapped [plan review]: planning turn", orchestrator.in_plan_mode(cfg_payload, {"users": [], "checkpoints": [], "last_step": 2}))
write_transcript([{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
                  {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real("[plan review due] 5 invocations have run.")}], path=cfg_tp)
check("real wrapped [plan review due] (sent to the executor) is no hand-back",
      orchestrator.handoff_message(cfg_payload, {"users": [], "checkpoints": [], "last_step": 2}) is None)

# the executor's role is bounded
for label, role, want in [("long", "x" * 80, "execution model"), ("odd characters", "A; rm -rf /", "execution model"),
                          ("the execution model", "the execution model", "execution model"),
                          ("versioned", "Claude Sonnet 5.5 (High)", "execution model (Claude Sonnet 5.5 (High))"),
                          ("wrapped in the execution model (…)", "the execution model (Model X-1)", "execution model (Model X-1)")]:
    write_transcript([{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task</USER_REQUEST>"},
                      {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real(f"Plan mode is off: this turn runs on {role}. Carry out the plan.")}], path=cfg_tp)
    fresh_state(cfg_cid)
    h = orchestrator.handoff_message(cfg_payload, {"users": [{"step": 0, "ts": "", "text": "Task, но ничего не пушь"}], "checkpoints": [], "last_step": 2})
    line = (h or "").splitlines()[1] if h else ""
    check(f"executor role ({label}) → «Hand-off to {want}:»", line.startswith(f"Hand-off to {want}:"), line[:140])

# only the current turn counts for the transcript-based detection
old_turn = [{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Task 1</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "p"}}]},
            {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real("Plan mode is off: this turn runs on the execution model. Go.")},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "enter_plan_mode", "args": {"reason": "done"}}]},
            {"step_index": 4, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": real("[plan review] Plan mode is on: this turn runs on the planning model. the execution model handed back.")}]
new_user = {"step_index": 5, "type": "USER_INPUT", "content": "<USER_REQUEST>Task 2, plain question</USER_REQUEST>"}
plain2 = {"conversationId": "agp-orch-plain-0001", "transcriptPath": tp, "workspacePaths": [ws], "modelName": "gemini-4-neon-high"}
write_transcript(old_turn + [new_user, {"step_index": 6, "type": "PLANNER_RESPONSE", "content": "Answer."}])
check("an ordinary question after a plan-execute turn (no annotation, ordinary model): not plan mode",
      not orchestrator.in_plan_mode(plain2, {}))
write_transcript(old_turn)
check("…while the same steps in the CURRENT turn make it plan mode (a /plan turn without an annotation)",
      orchestrator.in_plan_mode(plain2, {}))
write_transcript(old_turn + [new_user, {"step_index": 6, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "p"}}]}])
check("…and an exit_plan_mode after the new message counts for the new turn", orchestrator.in_plan_mode(plain2, {}))
# Two shapes seen in real classic-Orchestrator transcripts (Stop payload without modelName: the transcript decides)
shape_cid = "agp-orch-shape-0001"
shape_art = os.path.join(cfg_root, "shape_art")
os.makedirs(shape_art, exist_ok=True)
with open(os.path.join(shape_art, "task.md"), "w", encoding="utf-8") as f:
    f.write("## Plan\n- [x] Done step\n- [ ] Pending shape step\n")
def shape_stop(rows):
    fresh_state(shape_cid)
    write_transcript(rows)
    return stop.decide({"conversationId": shape_cid, "transcriptPath": tp, "artifactDirectoryPath": shape_art, "workspacePaths": [ws],
                        "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0})
T0 = "2026-11-24T10:00:00Z"
usr = lambda i, text="Build it": {"step_index": i, "type": "USER_INPUT", "created_at": T0, "content": f"<USER_REQUEST>{text}</USER_REQUEST>"}
exit_plan = lambda i: {"step_index": i, "type": "PLANNER_RESPONSE", "created_at": T0, "content": "",
                       "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Edit a.py\n2. Run pytest"}}]}
sysm = lambda i, body: {"step_index": i, "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": T0, "content": real(body)}
said = lambda i, text: {"step_index": i, "type": "PLANNER_RESPONSE", "created_at": T0, "content": text}
HAND = "Plan mode is off: this turn runs on the execution model. Carry out the plan below now."
REVIEW = "[plan review] Plan mode is on: this turn runs on the planning model. the execution model handed back."
sub_report = ("[Message] timestamp=2026-11-24T10:05:00Z sender=agp-orch-sub-0001 priority=MESSAGE_PRIORITY_HIGH content=The log says:\n"
              f"<SYSTEM_MESSAGE>\n{REVIEW}\n</SYSTEM_MESSAGE>\nand earlier:\n<SYSTEM_MESSAGE>\n{HAND}\n</SYSTEM_MESSAGE>")
# 1. the executor phase (after «Plan mode is off», before it hands back): the executor keeps every check — open task.md items
#    send it back, even when a subagent's report quotes the markers
r1 = shape_stop([usr(0), exit_plan(1), sysm(2, HAND), said(3, "Done with step 1.")])
check("classic Orchestrator, executor phase, transcript only: open task.md items send the executor back",
      bool(r1) and "Pending shape step" in r1, r1)
r1b = shape_stop([usr(0), exit_plan(1), sysm(2, HAND), sysm(3, sub_report), said(4, "Done with step 1.")])
check("…also after a subagent report that quotes «[plan review]» and «Plan mode is off»", bool(r1b) and "Pending shape step" in r1b, r1b)
check("…and it is no plan mode, nothing handed over",
      (write_transcript([usr(0), exit_plan(1), sysm(2, HAND), sysm(3, sub_report)]), not orchestrator.in_plan_mode(
          {"conversationId": shape_cid, "transcriptPath": tp, "workspacePaths": [ws]}, {}))[1])
# 2. a planning turn after an earlier plan-execute turn that ends WITHOUT handing a plan over: an announcement is sent back
prev = [usr(0), exit_plan(1), sysm(2, HAND), said(3, "Done."), usr(4, "do everything yourself")]
for label, rows in [("ordinary model, no marker in this turn", prev + [said(5, "Сейчас запущу тесты.")]),
                    ("after a hand-back in the previous turn", [usr(0), exit_plan(1), sysm(2, HAND), sysm(3, REVIEW), usr(4, "go on"),
                                                                said(5, "Сейчас запущу тесты.")])]:
    r2 = shape_stop(rows)
    check(f"a planning turn that ends with an announcement is sent back ({label})",
          bool(r2) and "ended your turn without doing it" in r2, r2)
r2c = shape_stop(prev + [exit_plan(5), said(6, "Сейчас запущу тесты.")])
check("…but not when this turn's plan was handed over (the executor carries on)", r2c is None, r2c)
shutil.rmtree(cfg_root, ignore_errors=True)
for c in (cfg_cid, ttl_cid, other_cid + "-q", "agp-orch-plain-0001", shape_cid):
    fresh_state(c)

# 4. guard.py exit_plan_mode (including Bypass Mode & full access)
for f in os.listdir(conv.STATE_DIR):
    if f.startswith(cid):
        os.remove(os.path.join(conv.STATE_DIR, f))
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z",
     "content": "<USER_REQUEST>/bypass\nничего не пушь</USER_REQUEST>"},
])
d_ban = run_guard("1. Edit app.py\n2. Run git push origin main\n3. Run pytest")
check("guard.py exit_plan_mode asks the user about a banned step even in Bypass Mode",
      d_ban.get("decision") == "force_ask" and "push" in d_ban.get("reason", ""), d_ban)
d_unv = run_guard(unverified)
check("guard.py exit_plan_mode denies unverified source plan even in Bypass Mode",
      d_unv.get("decision") == "deny" and "проверк" in d_unv.get("reason", ""), d_unv)
d_unv_again = run_guard(unverified)
check("guard.py exit_plan_mode lets the same plan through when it is sent again", d_unv_again == {"decision": "allow"},
      d_unv_again)
d_ok = run_guard(f"{unverified}\n2. Run python3 tests/run_tests.py")
check("guard.py exit_plan_mode returns allow (PASS) for valid plan even in Bypass Mode",
      d_ok == {"decision": "allow"}, d_ok)

# Also verify full access (without Bypass Mode) still enforces check_plan and surfaces handoff_message
if os.path.exists(settings_path):
    os.remove(settings_path)
for f in os.listdir(conv.STATE_DIR):
    if f.startswith(cid):
        os.remove(os.path.join(conv.STATE_DIR, f))
write_transcript([
    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z",
     "content": "<USER_REQUEST>Даю полный доступ</USER_REQUEST>"},
    {"step_index": 2, "type": "SYSTEM_MESSAGE", "source": "SYSTEM",
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>"},
])
st_fa = conv.load(base_payload)
h_fa = orchestrator.handoff_message(base_payload, st_fa)
check("handoff_message emits <orchestrator_handoff> when full access is ON",
      bool(h_fa) and "<orchestrator_handoff" in h_fa and "Full access" in h_fa, h_fa)
d_fa_unv = run_guard(unverified)
check("guard.py exit_plan_mode denies unverified plan even under full access",
      d_fa_unv.get("decision") == "deny", d_fa_unv)
d_fa_ok = run_guard(f"{unverified}\n2. Run pytest")
check("guard.py exit_plan_mode returns allow (PASS) for valid plan under full access",
      d_fa_ok == {"decision": "allow"}, d_fa_ok)

# 5. client.py integration: executor(), is_orchestrator_model(), is_plan_execute()
_ex_bundled = orchestrator.executor()
check("executor(): bundled snapshot → gemini-flash-worker-agy",
      _ex_bundled.get("id") == "gemini-flash-worker-agy" and bool(_ex_bundled.get("label")), _ex_bundled)

_old_cc = os.environ.get("AGP_CLIENT_CACHE")
_old_bundled = client.BUNDLED
_custom_cc = os.path.join(art, "orch-client-cache.json")
try:
    with open(_custom_cc, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "at": int(time.time()) if "time" in dir() else 1795784886,
                   "executor": {"id": "custom-exec-1", "label": "Custom Executor"},
                   "models": {"custom-exec-1": {"id": "custom-exec-1", "label": "Custom Executor",
                                                "family": "custom-exec-1", "effort": "", "provider": "google",
                                                "window": 512000}}}, f)
    os.environ["AGP_CLIENT_CACHE"] = _custom_cc
    client._memo.clear()
    check("executor(): follows hand-written snapshot with different executor id",
          orchestrator.executor() == {"id": "custom-exec-1", "label": "Custom Executor"}, orchestrator.executor())

    os.environ["AGP_CLIENT_CACHE"] = os.path.join(art, "orch-missing-client.json")
    client.BUNDLED = os.path.join(art, "orch-missing-bundled.json")
    client._memo.clear()
    check("executor(): no snapshot → EXECUTOR_FALLBACK",
          orchestrator.executor() == orchestrator.EXECUTOR_FALLBACK, orchestrator.executor())
finally:
    if _old_cc is None:
        os.environ.pop("AGP_CLIENT_CACHE", None)
    else:
        os.environ["AGP_CLIENT_CACHE"] = _old_cc
    client.BUNDLED = _old_bundled
    if os.path.exists(_custom_cc):
        os.remove(_custom_cc)
    client._memo.clear()

check("is_orchestrator_model(): true for Orchestrator (Neon), gemini-pro-orchestrator, executor id & label; false for gemini-4-neon-high and ''",
      orchestrator.is_orchestrator_model("Orchestrator (Neon)")
      and orchestrator.is_orchestrator_model("gemini-pro-orchestrator")
      and orchestrator.is_orchestrator_model(_ex_bundled["id"])
      and orchestrator.is_orchestrator_model(_ex_bundled["label"])
      and not orchestrator.is_orchestrator_model("gemini-4-neon-high")
      and not orchestrator.is_orchestrator_model(""))
check("is_plan_execute(): true for executor modelName gemini-flash-worker-agy",
      orchestrator.is_plan_execute({"modelName": "gemini-flash-worker-agy"}))

# Clean up settings/state created during test
if os.path.exists(settings_path):
    os.remove(settings_path)
for c in (cid, other_cid):
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(c):
            os.remove(os.path.join(conv.STATE_DIR, f))
shutil.rmtree(ws, ignore_errors=True)
shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
