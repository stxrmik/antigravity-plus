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
    return json.loads(out.stdout).get("decision") == "continue", out.stderr.strip()
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

# orchestrator.in_plan_mode skips broken/unverified/setup/review/bg, keeps announced & facts_check
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
    return json.loads(r.stdout or "{}")
plan_skip = not run_plan("План готов.")
plan_facts = "facts" in (run_plan("Все тесты проходят.").get("reason") or "")
plan_ann = "ended your turn without doing it" in (run_plan("Сейчас запущу тесты.").get("reason") or "")
exec_reason = run_plan("План готов.", [
    {"step_index": 3, "type": "SYSTEM_MESSAGE", "created_at": now,
     "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model\n</SYSTEM_MESSAGE>"}
]).get("reason") or ""
plan_exec = "ran no check" in exec_reason
plan_ok = plan_skip and plan_facts and plan_ann and plan_exec
print(f"{'✅' if plan_ok else '❌'} orchestrator.in_plan_mode skips edit/verify → skip={plan_skip}, facts={plan_facts}, ann={plan_ann}, exec={plan_exec}")
ok &= plan_ok

shutil.rmtree(art); os.path.exists(state) and os.remove(state)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
