"""Stop hook: open task.md items send the agent back, questions and parked items don't."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
cid = "agp-stoptest-0001"
art = os.path.join(SP, "stoptest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
tp, task = os.path.join(art, "transcript_full.jsonl"), os.path.join(art, "task.md")
state = os.path.join(conv.STATE_DIR, cid + ".json")
def setup(user, answer, checklist, task_age=0):
    for f in (tp, state):
        if os.path.exists(f): os.remove(f)
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": ts, "content": f"<USER_REQUEST>{user}</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": answer}]
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    open(task, "w").write(checklist)
    os.utime(task, (now - task_age, now - task_age))
def run(extra=None):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "terminationReason": "NO_TOOL_CALL",
         "fullyIdle": True, "executionNum": 0}
    p.update(extra or {})
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True)
    d = json.loads(out.stdout)
    REASONS.append(d.get("reason") or "") if d.get("decision") == "continue" else None
    return d.get("decision") == "continue", out.stderr.strip()
REASONS = []  # every message that sent the agent back in this test: each must end the same way
OPEN = "# Задача\n\n## Сортировка серверов\n- [x] Реализовать auto_sort\n- [/] Добавить эндпоинт\n- [ ] Тесты\n"
cases = [
 ("open items, agent just stopped",              "Сделай сортировку", "Готово: реализовал auto_sort.", OPEN, 0, None, True),
 ("agent asks a question",                       "Сделай сортировку", "Эндпоинт готов. Пушить на сервер?", OPEN, 0, None, False),
 ("agent asks to review (no '?')",               "Сделай сортировку", "Пожалуйста, просмотрите план и подтвердите.", OPEN, 0, None, False),
 ("old checklist, unrelated question now",       "Сколько это стоит в месяц", "Примерно 5$.", OPEN, 600, None, False),
 ("old checklist, user said «продолжай»",        "продолжай", "Сделал эндпоинт.", OPEN, 600, None, True),
 ("user cancelled",                              "Сделай сортировку", "…", OPEN, 0, {"terminationReason": "USER_CANCELED"}, False),
 ("already sent back twice this turn",           "Сделай сортировку", "Готово.", OPEN, 0, {"executionNum": 2}, False),
 ("fullyIdle false, nothing tracked",             "Сделай сортировку", "Жду сборку.", OPEN, 0, {"fullyIdle": False}, False),
 ("background task running, agent asks",         "Сделай сортировку", "Сборка идёт. Пушить потом?", OPEN, 0, {"fullyIdle": False}, False),
 ("everything done",                             "Сделай сортировку", "Готово.", OPEN.replace("[/]", "[x]").replace("[ ]", "[x]"), 0, None, False),
 ("open items parked by the agent",              "Сделай сортировку", "Готово.", OPEN.replace("Тесты", "Тесты — ждёт вашего решения").replace("[/] Добавить эндпоинт", "[x] Добавить эндпоинт"), 0, None, False),
 ("'блок' is a UI word, not 'blocked'",          "Сделай портфолио", "Сделал шапку.", "## Портфолио\n- [x] Шапка\n- [ ] Добавить блок с проектами\n", 0, None, True),
 ("trailing notes section without items",        "Сделай сортировку", "Готово.", OPEN + "\n## Заметки\nНичего особенного.\n", 0, None, True),
 ("old section open, current section done",      "Теперь сделай логи", "Логи готовы.", "## Старое\n- [ ] Брошенный пункт\n\n## Логи\n- [x] Логи\n", 0, None, False),
]
ok = True
for name, user, answer, checklist, age, extra, want in cases:
    setup(user, answer, checklist, age)
    got, err = run(extra)
    ok &= got == want
    print(f"{'✅' if got == want else '❌'} {name:<42} → {'continue' if got else 'stop'}" + (f"   stderr: {err}" if err else ""))
setup("Сделай сортировку", "Готово: реализовал auto_sort.", OPEN, 0)
first, _ = run(); again, _ = run({"executionNum": 1})
print(f"{'✅' if first and not again else '❌'} same open items twice in one turn         → first {'continue' if first else 'stop'}, then {'continue' if again else 'stop'}")
ok &= first and not again

# tracked background task + continuationActive + broadened DELEGATED
def setup_bg(checklist, answer="Жду сборку."):
    setup("Сделай сортировку", answer, checklist, 0)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    rows = [
        {"step_index": 0, "type": "USER_INPUT", "created_at": now, "content": "<USER_REQUEST>Сделай сортировку</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": now, "content": "",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest -q"}}]},
        {"step_index": 2, "type": "GENERIC", "created_at": now,
         "content": f"Created At: {now}\nTool is running as a background task with task id: {cid}/task-1\nTask Description: pytest -q\n"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": now, "content": answer},
    ]
    open(tp, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

setup_bg(OPEN)
bg_first, _ = run({"fullyIdle": False})
open(task, "w", encoding="utf-8").write(OPEN.replace("Тесты", "Написать тесты"))
bg_cont, _ = run({"fullyIdle": False, "continuationActive": True})
print(f"{'✅' if bg_first and not bg_cont else '❌'} continuationActive suppresses bg repeat    → first {'continue' if bg_first else 'stop'}, cont {'continue' if bg_cont else 'stop'}")
ok &= bg_first and not bg_cont

DELEG_ONLY = ("## Задача\n- [x] Каркас\n- [ ] Аудит: worker\n- [ ] Сборка (в фоне)\n"
              "- [ ] Парсер — swarm\n- [ ] Прогон (background)\n")
setup_bg(DELEG_ONLY, "Жду задачи.")
deleg_cont, _ = run({"fullyIdle": False})
print(f"{'✅' if not deleg_cont else '❌'} broadened DELEGATED items allow waiting    → {'continue' if deleg_cont else 'stop'}")
ok &= not deleg_cont

# ordinary items that merely contain those words stay the agent's own work
ORDINARY = ("## Задача\n- [x] Каркас\n- [ ] Fix: background color of the header\n- [ ] CSS: background image on mobile\n"
            "- [ ] Docs: swarm section\n- [ ] Bug: explorer crashes on empty dir\n")
import stop as stop_mod  # noqa: E402
ordinary_hits = [t for t in ORDINARY.splitlines()[2:] if stop_mod.DELEGATED.search(t[6:])]
print(f"{'✅' if not ordinary_hits else '❌'} «Fix: background color» & co. aren't delegated → {ordinary_hits or 'none matched'}")
ok &= not ordinary_hits
setup_bg(ORDINARY, "Жду сборку.")
ord_cont, _ = run({"fullyIdle": False})
print(f"{'✅' if ord_cont else '❌'} open ordinary items while a task runs → nudge → {'continue' if ord_cont else 'stop'}")
ok &= ord_cont

# orchestrator.in_plan_mode skips broken/unverified/setup/review/bg and keeps facts_check, task.md and announcement();
# a planning turn that ended by handing the plan over (last tool call exit_plan_mode) also skips task.md and announcement()
ws_plan = os.path.join(art, "ws_plan"); os.makedirs(ws_plan, exist_ok=True)
bad_py = os.path.join(ws_plan, "bad.py"); open(bad_py, "w", encoding="utf-8").write("def f(:\n    pass\n")
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
base_plan_rows = [
    {"step_index": 0, "type": "USER_INPUT", "created_at": now, "content": "<USER_REQUEST>Спланируй правки</USER_REQUEST>"},
    {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": now, "content": "",
     "tool_calls": [{"name": "write_file", "args": {"Path": bad_py, "Content": "def f(:\n"}}]},
    {"step_index": 2, "type": "GENERIC", "created_at": now, "content": "Wrote bad.py"},
]
open(task, "w", encoding="utf-8").write("## План\n- [x] Изучить код\n")
def run_plan(reply, rows_extra=()):
    rows = base_plan_rows + list(rows_extra) + [
        {"step_index": 3 + len(rows_extra), "type": "PLANNER_RESPONSE", "created_at": now, "content": reply}
    ]
    open(tp, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    if os.path.exists(state): os.remove(state)
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws_plan],
         "modelName": "Orchestrator (Carbon)", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p),
                       capture_output=True, text=True, encoding="utf-8")
    d = json.loads(r.stdout or "{}")
    REASONS.append(d.get("reason") or "") if d.get("decision") == "continue" else None
    return d
plan_skip = not run_plan("План готов.")
plan_facts = "facts" in (run_plan("Все тесты проходят.").get("reason") or "")
HAND = [{"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": now, "content": "",
         "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Fix bad.py\\n2. Run pytest"}}]}]
plan_ann = "ended your turn without doing it" in (run_plan("Сейчас запущу тесты.").get("reason") or "")  # not handed over: as in 0.27.1
plan_ann_handed = not run_plan("Сейчас запущу тесты.", HAND)  # handed over: the executor carries on
exec_reason = run_plan("План готов.", [
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "created_at": now,
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model\n</SYSTEM_MESSAGE>"}
]).get("reason") or ""
plan_exec = "ran no check" in exec_reason
plan_ok = plan_skip and plan_facts and plan_ann and plan_ann_handed and plan_exec
print(f"{'✅' if plan_ok else '❌'} classic Orchestrator: skips edit/verify, keeps ann unless handed over → skip={plan_skip}, facts={plan_facts}, ann={plan_ann}, ann_handed_skip={plan_ann_handed}, exec={plan_exec}")
ok &= plan_ok

# stop.ci_command strips leading VAR=val and env flags/prefixes
for ci_cmd in ("GIT_TERMINAL_PROMPT=0 git push -q", "AGY_ACCOUNT=work git push",
               "env -i git push", "env -i AGY_ACCOUNT=work git push", "env -u FOO git push"):
    ci_hit = stop_mod.ci_command(ci_cmd)
    ci_good = isinstance(ci_hit, dict) and ci_hit.get("push") is True
    ok &= ci_good
    print(f"{'✅' if ci_good else '❌'} ci_command('{ci_cmd}') → {ci_hit}")

# configurable Orchestrator (plan_execute:true annotation) in plan mode: open task.md & announcement() are checked unless
# the plan was handed over; facts_check always
pe_cid = "agp-stop-pe-0001"
pe_root = os.path.join(SP, "stop_pe_root")
pe_logs = os.path.join(pe_root, "brain", pe_cid, ".system_generated", "logs")
pe_ann = os.path.join(pe_root, "annotations")
pe_art = os.path.join(pe_root, "art")
os.makedirs(pe_logs, exist_ok=True); os.makedirs(pe_ann, exist_ok=True); os.makedirs(pe_art, exist_ok=True)
pe_tp = os.path.join(pe_logs, "transcript_full.jsonl")
pe_task = os.path.join(pe_art, "task.md")
pe_state = os.path.join(conv.STATE_DIR, pe_cid + ".json")
with open(os.path.join(pe_ann, f"{pe_cid}.pbtxt"), "w", encoding="utf-8") as f:
    f.write('title:"PE" plan_execute:true')
with open(pe_task, "w", encoding="utf-8") as f:
    f.write("## План\n- [ ] Открытый пункт 1\n- [/] Открытый пункт 2\n")
def run_pe_decide(reply, handed=False):
    if os.path.exists(pe_state):
        os.remove(pe_state)
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": now, "content": "<USER_REQUEST>Спланируй задачу</USER_REQUEST>"}]
    if handed:
        rows.append({"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": now, "content": "",
                     "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": "1. Edit a.py\\n2. Run pytest"}}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "created_at": now, "content": reply})
    with open(pe_tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return keep(stop_mod.decide({
        "conversationId": pe_cid, "transcriptPath": pe_tp, "artifactDirectoryPath": pe_art,
        "workspacePaths": [ws_plan], "modelName": "Claude Opus 5.5 (High)",
        "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0,
    }))
def keep(reason):
    REASONS.append(reason) if reason else None
    return reason
pe_open = "Открытый пункт" in (run_pe_decide("План готов.") or "")
pe_ann = "ended your turn without doing it" in (run_pe_decide("Сейчас запущу тесты.") or "")
pe_open_skip = run_pe_decide("План готов.", handed=True) is None
pe_ann_skip = run_pe_decide("Сейчас запущу тесты.", handed=True) is None
pe_facts_ok = "facts" in (run_pe_decide("Все тесты проходят.", handed=True) or "")
with open(pe_task, "w", encoding="utf-8") as f:  # (with open items the task.md nudge comes first)
    f.write("## План\n- [x] Пункт 1\n- [x] Пункт 2\n")
pe_facts_ok &= "facts" in (run_pe_decide("Все тесты проходят.") or "")
pe_ok = pe_open and pe_ann and pe_open_skip and pe_ann_skip and pe_facts_ok
ok &= pe_ok
print(f"{'✅' if pe_ok else '❌'} plan_execute:true: task.md & ann checked unless the plan was handed over, facts always → "
      f"task={pe_open}, ann={pe_ann}, task_skip_handed={pe_open_skip}, ann_skip_handed={pe_ann_skip}, facts={pe_facts_ok}")
shutil.rmtree(pe_root, ignore_errors=True)
if os.path.exists(pe_state):
    os.remove(pe_state)

# Sent back over its report, an agent wrote the whole report again (seen live: the user saw it twice). Every return,
# whatever its cause, ends with the same steps: the work first, then only what changed.
kinds = {k: any(k in r for r in REASONS) for k in ("task.md", "facts", "ended your turn without doing it", "ran no check",
                                                  "in the background")}
ends = [r for r in REASONS if not r.endswith(stop_mod.AFTER) or r.count(stop_mod.AFTER) != 1]
after_ok = REASONS and not ends and all(kinds.values()) and stop_mod.AFTER.index("1. Do") < stop_mod.AFTER.index("2. Reply")
ok &= bool(after_ok)
print(f"{'✅' if after_ok else '❌'} each of {len(REASONS)} returns ends once with the shared ending (work first, then a short reply) → "
      f"kinds={kinds}, bad={[r[:80] for r in ends]}")
# The same report again after a facts return, and nothing new to say: the hook is quiet (no loop).
with open(task, "w", encoding="utf-8") as f:
    f.write("## План\n- [x] Пункт\n")
setup("Сделай сортировку", "Готово, все тесты проходят.", "## План\n- [x] Пункт\n")
first_facts, _ = run()
note = REASONS[-1] if first_facts else ""
rows = [json.loads(x) for x in open(tp, encoding="utf-8")]
rows += [{"step_index": 2, "type": "SYSTEM_MESSAGE", "content": f"<SYSTEM_MESSAGE>\nStop hook blocked termination: {note}\n</SYSTEM_MESSAGE>"},
         {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Готово, все тесты проходят."}]
open(tp, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
again_facts, _ = run({"executionNum": 1, "continuationActive": True})
quiet_ok = first_facts and "facts" in note and not again_facts
ok &= quiet_ok
print(f"{'✅' if quiet_ok else '❌'} the same report after a facts return → quiet → first {first_facts}, again {again_facts}")

shutil.rmtree(art); os.path.exists(state) and os.remove(state)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
