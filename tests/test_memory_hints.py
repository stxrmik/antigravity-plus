"""Auto-memory: what in a user message sounds lasting, the hint before the next step, the end-of-work check at Stop,
«💾» without a file, and the guard refusing secrets in memory."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv, memory_hints as mh
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
sys.path.insert(0, os.path.dirname(MEMORY)); import memory as memstore
art = os.path.join(SP, "memtest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
WS = os.path.realpath(os.path.join(SP, "proj")); os.makedirs(WS, exist_ok=True)
PROJECT = memstore.project_dir(WS)[0]
tp = os.path.join(art, "transcript_full.jsonl")
ok = True
cid = ""
rows = []


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f"   {detail}"))


def iso(ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def start(name, user="Обнови панель на сервере", main=True):
    global cid, rows
    cid = f"agp-memtest-{name}"
    rows = []
    for f in (tp, os.path.join(conv.STATE_DIR, cid + ".json"), os.path.join(art, "task.md")):
        if os.path.exists(f):
            os.remove(f)
    if main:
        say(user)
    else:
        add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>injected by a SessionStart hook</SYSTEM_MESSAGE>")


def add(kind, content="", calls=None, **extra):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso(), **extra}
    if content:
        row["content"] = content
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def say(text):
    add("USER_INPUT", content=f"<USER_REQUEST>{text}</USER_REQUEST>")


def cmd(line, code=0, output="ok"):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line, "WaitMsBeforeAsync": 5000})])
    add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\nCommand exited with code {code}.\n\nCommand output:\n{output}\n")


def write(path, content="---\nname: x\n---\nfact\n"):
    add("PLANNER_RESPONSE", calls=[("write_file", {"Path": path, "Content": content})])
    add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\nCreated file {path}\n")


def work(n):
    for i in range(n):
        add("PLANNER_RESPONSE", calls=[("read_file", {"Path": os.path.join(WS, f"f{i}.py")})])
        add("GENERIC", content="Showing lines 1-10 of 10\n1: x = 1\n")


def answer(text):
    add("PLANNER_RESPONSE", content=text)


def payload(**extra):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
    p.update(extra)
    return p


def pre():
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(payload()),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [s["ephemeralMessage"] for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "<memory_hint" in m), out.stderr.strip()


def stop(execution=0, **extra):
    p = payload(terminationReason="NO_TOOL_CALL", fullyIdle=True, executionNum=execution, **extra)
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    return (d.get("reason") or "") if d.get("decision") == "continue" else "", out.stderr.strip()


def guard(tool, args):
    p = {"conversationId": cid, "toolCall": {"name": tool, "args": args}, "workspacePaths": [WS]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout or "{}").get("decision")


def config(*pairs):
    subprocess.run([sys.executable, MEMORY, "config", *pairs], capture_output=True, text=True)


# ── what sounds lasting ──
table = [
    ("Запомни: деплой только через ./deploy/update.sh", {"remember!"}),
    ("я же просил не трогать main", {"correction!"}),
    ("хватит спрашивать разрешения на каждую команду", {"correction!"}),
    ("никогда не пушь в main", {"preference!"}),
    ("всегда сначала показывай план", {"preference!"}),
    ("отвечай короче", {"preference!"}),
    ("From now on, reply in Russian", {"remember!", "preference!"}),
    ("I told you not to touch the config", {"correction!"}),
    ("зачем ты удалил этот файл?", {"correction"}),
    ("я работаю на маке с M2", {"about_user"}),
    ("дедлайн в пятницу", {"project"}),
    ("именно так, так и делай", {"confirm"}),
    # task talk, not something to keep
    ("сервер всегда падает после деплоя", set()),
    ("кнопка никогда не работает на айфоне", set()),
    ("там всегда есть ошибка в логах", set()),
    ("хватит на сегодня, спасибо", set()),
    ("сколько можно сэкономить на серверах?", set()),
    ("в следующий раз сделаем", set()),
    ("не запоминай это, просто сделай", set()),
    ("не забудь запушить", set()),
    ("the build always fails on CI", set()),
    ("```\nalways use strict\n```\nпочини билд", set()),
    ("У нас сайт медленно открывается именно с телефонов, нужно переделать сжатие картинок", set()),
]
bad = [(t, w, g) for t, w in table
       for g in [{s["kind"] + ("!" if s["strong"] else "") for s in mh.signals(t)}] if g != w]
check(f"signals: {len(table) - len(bad)} of {len(table)} phrases classified as expected", not bad, bad)

# ── the hint before the next step ──
start("hint", "Запомни: деплой только через ./deploy/update.sh")
hint, err = pre()
check("«запомни» → <memory_hint> with the exact project folder", "<memory_hint" in hint and PROJECT in hint and
      "update.sh" in hint, hint[:300] or err)
check("…once per message", not pre()[0])
say("Теперь поправь вёрстку на главной")
check("a plain task message → no hint", not pre()[0])
say("никогда не пушь в main без меня")
hint, _ = pre()
check("a standing preference → hint for personal memory (feedback)", "type: feedback" in hint and memstore.PERSONAL in hint, hint[:300])
start("hint-sub", main=False)
check("subagent → no hint", not pre()[0])
start("hint-off", "Запомни: прод на Hetzner")
config("auto-memory", "off")
check("auto-memory off → no hint", not pre()[0])
config("auto-memory", "on")

# ── the end-of-work check ──
start("strong", "Запомни: деплой только через ./deploy/update.sh")
answer("Готово.")
reason, err = stop()
check("«запомни» left unsaved → sent back to save it", "memory" in reason and "update.sh" in reason, reason[:200] or err)
check("…and only once for this message", not stop(execution=1)[0])

start("saved", "Запомни: деплой только через ./deploy/update.sh")
write(os.path.join(PROJECT, "deploy-flow.md"))
answer("💾 Запомнил: деплой через ./deploy/update.sh")
check("saved in this turn → no check", not stop()[0])

start("handoff", "Запомни: деплой только через ./deploy/update.sh")
write(os.path.join(PROJECT, "handoff-2026-11-06-panel.md"))
answer("💾 Передача сохранена.")
check("a handoff note isn't a memory: the unsaved «запомни» still counts", "update.sh" in stop()[0])

start("remote")
work(8)
for i in range(3):
    cmd(f"./deploy/ssh.sh 'systemctl status app-{i}'")
cmd("./deploy/update.sh")
answer("Панель обновлена, сервис перезапущен.")
reason, _ = stop()
check("work on servers and deploys → sent back with the commands", "./deploy/ssh.sh" in reason and "./deploy/update.sh" in reason, reason[:300])
say("спасибо, а что с логами?")
work(4)
answer("Логи чистые.")
check("…not again right after the check", not stop()[0])

start("known")
os.makedirs(PROJECT, exist_ok=True)
with open(os.path.join(PROJECT, "deploy.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: deploy\ndescription: how to deploy\nmetadata:\n  type: project\n---\nDeploy: ./deploy/update.sh, shell: ./deploy/ssh.sh\n")
work(8)
for i in range(3):
    cmd(f"./deploy/ssh.sh 'uptime {i}'")
answer("Готово.")
check("commands memory already describes → no check", not stop()[0])
os.remove(os.path.join(PROJECT, "deploy.md"))

start("fixed")
work(6)
for i in range(3):
    cmd(f"docker exec app python3 -c 'import x{i}'", code=1, output="SyntaxError")
cmd("docker exec -i app python3 - < fix.py")
answer("Починил импорт в контейнере.")
reason, _ = stop()
check("a run of failing commands that finally worked → its cause and fix", "kept failing" in reason and "docker exec" in reason, reason[:300])

start("tests")
work(6)
for i in range(3):
    cmd("python3 -m pytest -q", code=1, output="1 failed")
cmd("python3 -m pytest -q")
answer("Тесты проходят.")
check("failing tests the agent then fixed aren't a lesson → no check", not stop()[0])

start("long")
work(64)
answer("Сделал страницу подписки.")
reason, _ = stop()
check("a long stretch of work → one look back", "steps since the last memory check" in reason, reason[:200])

start("question")
work(8)
for i in range(3):
    cmd(f"ssh web1 'cat /etc/app/config-{i}.json | head'")
answer("Конфиг поправлен. Перезапустить сервис сейчас?")
check("the agent asks the user → no check now", not stop()[0])
say("да")
cmd("ssh web1 'systemctl restart app'")
work(3)
answer("Перезапустил.")
reason, _ = stop()
check("…it comes at the next stop, with the earlier work", "ssh" in reason and "commands on servers" in reason, reason[:300])

start("offer", "Сервер web1 — основной, web2 — резервный")
work(4)
answer("Понял. Запомнить это в память?")
reason, _ = stop()
check("asks whether to save instead of deciding → told it's its call", "yours to decide" in reason, reason[:200])

start("claim", "Поправь вёрстку")
work(3)
answer("Готово. 💾 Запомнил: вёрстка на Tailwind")
reason, _ = stop()
check("«💾 Запомнил» without a memory file → sent back to write it", "saves nothing" in reason, reason[:200])
config("auto-memory", "off")
start("claim-off", "Поправь вёрстку")
work(3)
answer("Готово. 💾 Запомнил: вёрстка на Tailwind")
check("…also with auto-memory off", "saves nothing" in stop()[0])
start("off")
work(64)
answer("Готово.")
check("auto-memory off → no end-of-work check", not stop()[0])
config("auto-memory", "on")

start("items", "Запомни: прод на Hetzner")
with open(os.path.join(art, "task.md"), "w", encoding="utf-8") as f:
    f.write("## Задача\n- [x] Шаг 1\n- [ ] Шаг 2\n")
answer("Сделал шаг 1.")
reason, _ = stop()
check("open task.md items come first", "Шаг 2" in reason and "memory" not in reason.split("\n")[0].lower(), reason[:200])

start("sub", main=False)
work(64)
answer("Отчёт: нашёл три обработчика.")
check("subagent → no check", not stop()[0])

# ── secrets never go into memory ──
start("guard")
token = "gh" + "p_" + "Zq7" * 12  # built at run time: no token-like text in the source
check("memory write → auto_approve", guard("write_file", {"Path": os.path.join(PROJECT, "x.md"), "Content": "Деплой через update.sh"}) == "auto_approve")
check("memory write with a token → deny", guard("write_file", {"Path": os.path.join(PROJECT, "x.md"), "Content": f"token: {token}"}) == "deny")
check("memory edit with a token → deny", guard("edit_file", {"Path": os.path.join(memstore.PERSONAL, "x.md"), "Target": "a",
                                                             "Replacement": f"key {token}"}) == "deny")
config("protect-secrets", "off")
check("secret protection off → auto_approve", guard("write_file", {"Path": os.path.join(PROJECT, "x.md"), "Content": f"token: {token}"}) == "auto_approve")
config("protect-secrets", "on")

# ── a memory written into the project (seen live: flash_lite wrote <project>/memory/x.md) ──
MEMO = "---\nname: deploy-flow\ndescription: how to deploy\nmetadata:\n  type: project\n---\nDeploy: ./deploy/update.sh\n"
p = {"conversationId": cid, "workspacePaths": [WS],
     "toolCall": {"name": "write_file", "args": {"Path": os.path.join(WS, "memory", "deploy-flow.md"), "Content": MEMO}}}
out = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p), capture_output=True,
                     text=True, encoding="utf-8")
d = json.loads(out.stdout or "{}")
check("a memory written into the project → deny with the exact memory folder",
      d.get("decision") == "deny" and os.path.join(PROJECT, "deploy-flow.md") in d.get("reason", ""), d)
skill = "---\nname: lint\ndescription: run the linters\n---\nRun ruff.\n"
check("a skill or rule file with frontmatter isn't a memory → not refused",
      guard("write_file", {"Path": os.path.join(WS, ".agents", "skills", "lint", "SKILL.md"), "Content": skill}) != "deny")
os.makedirs(os.path.join(WS, "docs"), exist_ok=True)
with open(os.path.join(WS, "docs", "kept.md"), "w", encoding="utf-8") as f:
    f.write(MEMO)
check("an existing file in that format (the project keeps such files) → not refused",
      guard("write_file", {"Path": os.path.join(WS, "docs", "kept.md"), "Content": MEMO, "Overwrite": True}) != "deny")

# ── recall: the saved memories a new request is about ──
def memo(folder, name, desc, body, mtype="feedback"):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + ".md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: {mtype}\n---\n{body}\n")


def recall_block():
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(payload()),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [s["ephemeralMessage"] for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "<memory_recall" in m), out.stderr.strip()


shutil.rmtree(PROJECT, ignore_errors=True)
memo(PROJECT, "deploy-health-check-method", "панель после деплоя проверяем через curl по ssh, а не через браузер",
     "Панель после деплоя проверяем только через `./deploy/ssh.sh 'curl -s localhost:2053/health'`.")
memo(PROJECT, "handoff-2026-11-06-panel", "Передача: панель", "## Цель\nОбновить панель", "project")
start("recall", "Обнови панель через ./deploy/update.sh и проверь, что она поднялась")
block, err = recall_block()
check("a request about a saved memory → <memory_recall> with its text inline",
      "deploy-health-check-method" in block and "localhost:2053/health" in block, block[:300] or err)
check("…handoff notes aren't recalled (the handoff flow has them)", "handoff-2026" not in block)
check("…once per message", not recall_block()[0])
say("Задеплой новую версию")
check("a loanword with a prefix («задеплой») still finds it", "deploy-health-check-method" in recall_block()[0])
say("Поправь вёрстку на главной странице")
check("an unrelated request → no recall", not recall_block()[0])
start("recall-written", "Запомни: панель проверяем через curl")
write(os.path.join(PROJECT, "deploy-health-check-method.md"))
say("теперь задеплой панель")
check("a memory saved in this conversation isn't recalled back", not recall_block()[0])
start("recall-sub", main=False)
check("subagent → no recall", not recall_block()[0])
config("auto-memory", "off")
start("recall-off", "Обнови панель через ./deploy/update.sh")
check("auto-memory off → no recall", not recall_block()[0])
config("auto-memory", "on")
for i in range(30):  # a big store: words most memories share say nothing
    memo(PROJECT, f"note-{i}", f"проект панель заметка номер {i}", "Панель проекта, разное.", "project")
start("recall-big", "Обнови панель через ./deploy/update.sh и проверь, что она поднялась")
block = recall_block()[0]
check("in a big store a word most memories share doesn't pull them in",
      "deploy-health-check-method" in block and "note-" not in block, block[:400])
shutil.rmtree(PROJECT, ignore_errors=True)

# ── near compaction: save before details get lost ──
start("ctx")
add("PLANNER_RESPONSE", content="…", input_tokens=200_000, cache_read_tokens=30_000, output_tokens=100)
out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(payload()),
                     capture_output=True, text=True, encoding="utf-8")
budget = "\n".join(s["ephemeralMessage"] for s in json.loads(out.stdout or "{}").get("injectSteps", []) if "<context_budget" in s["ephemeralMessage"])
check("near compaction the context hint says to save to memory", "save to memory" in budget, budget[:300])

# ── the session context says how ──
out = subprocess.run([sys.executable, os.path.join(HOOKS, "session_start.py")],
                     input=json.dumps({"conversationId": "agp-memtest-new", "workspacePaths": [WS], "lastUserInput": "привет"}),
                     capture_output=True, text=True, encoding="utf-8")
ctx = json.loads(out.stdout or "{}").get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
check("session context: auto-memory says when to save", "save on your own, as soon as you learn it" in ctx, ctx[-400:])

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
