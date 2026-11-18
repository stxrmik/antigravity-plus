"""Orchestrator phase & plan checks (hooks/orchestrator.py) and guard.py `exit_plan_mode` enforcement."""
import json
import os
import shutil
import subprocess
import sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)

import conversation as conv  # type: ignore[import-not-found]
import coord  # type: ignore[import-not-found]
import orchestrator  # type: ignore[import-not-found]

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
