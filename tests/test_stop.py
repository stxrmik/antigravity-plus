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
 ("background task running: do the rest meanwhile", "Сделай сортировку", "Жду сборку.", OPEN, 0, {"fullyIdle": False}, True),
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
shutil.rmtree(art); os.path.exists(state) and os.remove(state)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
