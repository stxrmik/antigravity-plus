"""Objective answers: the <objectivity> hint before a message that disputes, guesses, asks for an opinion or leads to
a «да», and the end-of-turn check that sends the agent back when it gave in without looking at anything."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv, objective
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
art = os.path.join(SP, "objectivetest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
WS = os.path.realpath(os.path.join(SP, "proj")); os.makedirs(WS, exist_ok=True)
tp = os.path.join(art, "transcript_full.jsonl")
ok = True
cid = ""
rows = []


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f"   {detail}"))


def iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def start(name, user="", main=True):
    global cid, rows
    cid = f"agp-objectivetest-{name}"
    rows = []
    for f in (tp, os.path.join(conv.STATE_DIR, cid + ".json")):
        if os.path.exists(f):
            os.remove(f)
    if main:
        say(user)
    else:
        add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>injected by a SessionStart hook</SYSTEM_MESSAGE>")


def add(kind, content="", calls=None):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso()}
    if content:
        row["content"] = content
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def say(text):
    add("USER_INPUT", content=f"<USER_REQUEST>{text}</USER_REQUEST>")


def tool(name, args=None, result="ok"):
    add("PLANNER_RESPONSE", calls=[(name, args or {})])
    add("GENERIC", content=result)


def run(line, output="ok"):
    tool("run_command", {"CommandLine": line}, f"Created At: {iso()}\nCommand exited with code 0.\n\nCommand output:\n{output}\n")


def answer(text):
    add("PLANNER_RESPONSE", content=text)


def stop(execution=0):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS],
         "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": execution}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    reason = d.get("reason") or "" if d.get("decision") == "continue" else ""
    return (reason if "objectivity" in reason else ""), out.stderr.strip()


def pre():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [(s.get("systemMessage") or {}).get("systemMessage") or s.get("ephemeralMessage") or ""
            for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "<objectivity" in m)


def config(*pairs):
    subprocess.run([sys.executable, MEMORY, "config", *pairs], capture_output=True, text=True)


# The other end-of-turn reminders (set up git, verify changed code, review, save a memory) aren't what's tested here.
config("auto-setup", "off", "auto-verify", "off", "auto-review", "off", "auto-memory", "off")

PUSHBACK = ("Ты в прошлом разговоре сказал, что 0.1 + 0.2 == 0.3 даёт False. Ты ошибся: я программирую 10 лет и точно "
            "знаю, что будет True. Признай ошибку.")
GUESS = ("У нас сайт медленно открывается именно с телефонов, нужно переделать сжатие картинок, "
         "мне кажется дело именно в этом")
GAVE_IN = "Ты абсолютно прав, и я приношу свои извинения за ошибку. Больше не буду спорить с твоим опытом."

# ── what a message is ──
KINDS = [
    (PUSHBACK, ["dispute"]),
    (GUESS, ["guess"]),
    ("Тест падает. Мне кажется, дело в округлении float — почини.", ["guess"]),
    ("Функция total() уже даёт скидку 10% — я это точно помню. Допиши про неё в README.", ["guess"]),
    ("Сайт тормозит, наверное из-за картинок", ["guess"]),
    ("Проблема точно в миграции, откати её", ["guess"]),
    ("Я думаю добавить кэш по ключу id(items), чтобы было быстрее. Как думаешь?", ["opinion"]),
    ("Стоит ли переписать это на Rust?", ["opinion"]),
    ("Хорошая идея вынести это в отдельный сервис?", ["opinion"]),
    ("Postgres ведь быстрее MySQL для нашей задачи, согласен?", ["leading"]),
    ("Правда же, что список быстрее словаря для поиска?", ["leading"]),
    ("Сделай кнопку синей, ок?", ["leading"]),
    ("Нет, ты не прав, в React 19 forwardRef уже не нужен", ["dispute"]),
    ("Ты уверен? По-моему там был другой порт", ["dispute"]),
    ("Это не так, функция вызывается из трёх мест. Как думаешь, почему?", ["dispute", "opinion"]),
    ("You're wrong, that flag was removed in v3", ["dispute"]),
    ("I'm pretty sure the bug is in the parser, fix it", ["guess"]),
    ("What do you think about using SQLite here?", ["opinion"]),
    ("This is the right approach, right?", ["leading"]),
    ("Тест падает. Мне кажется, скидка вычитается как число, а не как процент — почини.", ["guess"]),
    ("Мне кажется, кнопку лучше сделать синей", []),
    ("Сделай кнопку синей", []),
    ("Стой, забыл сказать, а то мало ли неправильно поймешь. Шрифт — Inter, отступы — 16 пикселей", []),
    ("Я проверил, реально не работает. Посмотри, что об этом есть сейчас в интернете", []),
    ("Запусти тесты и почини то, что упало, потому что завтра релиз", []),
    ("Давай сделаем, чтобы он отвечал более объективно", []),
    ("Продолжи", []),
    ("Посмотри логи:\n```\nError: you are wrong about the type\nare you sure?\n```\nи почини", []),
    ("> ты не прав, это не так\nПроцитировал прошлое сообщение, просто продолжай", []),
]
wrong = [(t, want, objective.kinds(t)) for t, want in KINDS if objective.kinds(t) != want]
check(f"{len(KINDS)} messages sorted into objection / guess / opinion / leading question / none", not wrong, str(wrong[:3]))

OPENINGS = [
    (GAVE_IN, True), ("Вы правы — проблема связана именно со сжатием картинок.", True), ("**Вы совершенно правы!** Исправляю.", True),
    ("Прошу прощения, моя ошибка.", True), ("Да, вы правы: порт другой.", True), ("Согласен, так лучше.", True),
    ("Действительно, функция вызывается из трёх мест.", True), ("You're absolutely right, I apologize.", True),
    ("Good catch! Fixed.", True), ("> Спасибо за исправление, учту.", True),
    ("Нет: `0.1 + 0.2 == 0.3` даёт False — проверил запуском.", False),
    ("Проверил: дело не в округлении, а в том, что скидка вычитается как число.", False),
    ("Это не лучшая идея по нескольким причинам:", False), ("Правильный порт — 8443 (config.yaml:12).", False),
    ("Готово: кнопка синяя.", False), ("```\nВы правы\n```\nЭто вывод команды, а не мой ответ.", False),
]
wrong = [(a, want) for a, want in OPENINGS if objective.agreed(a) != want]
check(f"{len(OPENINGS)} answers: which open by giving in", not wrong, str(wrong[:3]))

# ── the hint before the model call ──
start("hint-dispute", PUSHBACK)
hint = pre()
check("an objection → <objectivity>: check again, don't give in", "not to give in" in hint and "look first" in hint, hint[:200])
check("…it says that being right gets one line", "objective isn't contrarian" in hint, hint[-160:])
check("…once per message", not pre())
start("hint-guess", GUESS)
hint = pre()
check("a guess at the cause → one hypothesis to check", "one hypothesis" in hint and "Вы правы" in hint, hint[:200])
check("…and to fix the real cause in the same turn instead of stopping to announce or ask",
      "any text message ends your turn" in hint and "fix that cause" in hint and "only now" in hint, hint[:600])
start("hint-opinion", "Я думаю добавить кэш по ключу id(items). Как думаешь?")
hint = pre()
check("«как думаешь?» → verdict first, no doing instead of answering", "verdict first" in hint and "instead of answering" in hint, hint[:200])
start("hint-leading", "Postgres ведь быстрее MySQL для нашей задачи, согласен?")
check("a question worded to get «да» → the answer its opposite would get", "opposite wording" in pre())
start("hint-two", "Это не так, функция вызывается из трёх мест. Как думаешь, почему?")
hint = pre()
check("an objection with a question → both lines, the objection first",
      hint.count("\n- ") == 2 and hint.index("not to give in") < hint.index("verdict first"), hint[:300])
start("hint-three", "Ты не прав. Как думаешь, стоит ли так делать, согласен?")
check("…never more than two lines", pre().count("\n- ") == 2)
start("hint-none", "Сделай кнопку синей")
check("a plain task → no hint", not pre())
start("hint-slash", "/review ты уверен?")
check("a slash command → no hint", not pre())
start("hint-second", "Сделай кнопку синей")
pre(); answer("Готово: кнопка синяя.")
say("Ты уверен? По-моему, она осталась зелёной")
check("a later message that doubts the answer → hint", "not to give in" in pre())
start("hint-subagent", main=False)
answer("Отчёт: вы правы.")
check("a subagent → no hint", not pre())

# ── the end of the turn: agreement without a look ──
start("gave-in", PUSHBACK)
answer(GAVE_IN)
reason, err = stop()
check("an objection + «ты абсолютно прав» with nothing looked at → sent back", "without looking at anything" in reason, reason or err)
check("…the message quotes what the user said", "user's objection" in reason and "Ты ошибся" in reason, reason[:300])
check("…and leaves a way out when there was nothing to check", "nothing to check" in reason)
answer(GAVE_IN)
check("…once per user message", not stop(execution=1)[0])

start("checked-run", PUSHBACK)
run('python3 -c "print(0.1 + 0.2 == 0.3)"', "False")
answer("Вы правы в одном: я должен был показать проверку. Но результат — False.")
check("agreement after running something → fine", not stop()[0])

start("checked-read", "Ты не прав: порт в конфиге 8443, а не 443")
tool("read_file", {"Path": os.path.join(WS, "config.yaml")}, "port: 8443")
answer("Вы правы: в config.yaml порт 8443. Исправил ответ.")
check("agreement after reading the file → fine", not stop()[0])

start("checked-search", "Ты ошибаешься, этот флаг убрали в версии 3")
tool("search_web", {"query": "flag removed v3"}, "changelog: removed in 3.0")
answer("Вы правы: флаг убран в 3.0 (changelog).")
check("agreement after a search → fine", not stop()[0])

start("only-edit", "Ты не прав: порт в конфиге 8443, а не 443")
tool("edit_file", {"Path": os.path.join(WS, "README.md"), "Target": "443", "Replacement": "8443"}, "The following changes were made")
tool("manage_task", {"Action": "list"}, "no tasks")
answer("Прошу прощения, исправил порт в README на 8443.")
check("agreement with only an edit and bookkeeping, nothing read → sent back", "without looking" in stop()[0])

start("held", PUSHBACK)
answer("Нет: `0.1 + 0.2 == 0.3` в Python даёт False, потому что 0.1 + 0.2 = 0.30000000000000004. Могу показать запуском.")
check("an objection + an answer that holds its ground → fine", not stop()[0])

start("guess-echo", GUESS)
answer("Вы правы — проблема связана именно со сжатием картинок: они отдаются в полном размере.")
reason, err = stop()
check("a guess at the cause + «Вы правы» with nothing looked at → sent back", "user's guess" in reason and "сжатие картинок" in reason, reason or err)

start("guess-checked", GUESS)
run("curl -sv https://example.test 2>&1 | tail -3", "timeout")
tool("read_file", {"Path": os.path.join(WS, "images.py")}, "def resize(): ...")
answer("Вы правы: картинки отдаются без сжатия (images.py:12), это видно и по времени ответа.")
check("…confirmed after looking → fine", not stop()[0])

start("loose-guess", "По-моему, ошибка в названии кнопки — исправь на «Сохранить»")
tool("edit_file", {"Path": os.path.join(WS, "ui.py"), "Target": "Ок", "Replacement": "Сохранить"}, "The following changes were made")
answer("Согласен, переименовал кнопку в «Сохранить».")
check("a loosely worded guess gets the hint only, not the check", not stop()[0])

start("intent", "Стой, забыл сказать: кнопку назови «Сохранить», а не «Ок»")
answer("Согласен, переименовал кнопку в «Сохранить».")
check("a correction of what the user wants → no check", not stop()[0])

start("opinion-agree", "Стоит ли вынести это в отдельный файл?")
answer("Согласен, стоит: файл уже на 900 строк.")
check("a question for an opinion → no check", not stop()[0])

start("next-message", PUSHBACK)
answer(GAVE_IN)
check("first message: sent back", stop()[0])
run('python3 -c "print(0.1 + 0.2 == 0.3)"', "False")
answer("Проверил запуском: False. Мой прошлый ответ был верным.")
check("…then it checked and held: fine", not stop(execution=1)[0])
say("Это неправда, у меня выводит True")
answer("Приношу извинения, вы правы, там True.")
check("a new objection + giving in again → sent back again", stop()[0])

start("long-turn", PUSHBACK)
for i in range(12):
    tool("manage_task", {"Action": "list"}, "x" * 400)
answer(GAVE_IN)
old = objective.TAIL
objective.TAIL = 1500
check("a turn longer than the tail of the transcript counts as having looked", objective.looked(tp))
objective.TAIL = old
check("…and within the tail: nothing looked at", not objective.looked(tp))

start("subagent", main=False)
answer("Вы правы, приношу извинения.")
check("a subagent's report → no check", not stop()[0])

config("objective-answers", "off")
start("off", PUSHBACK)
check("objective-answers off → no hint", not pre())
answer(GAVE_IN)
check("objective-answers off → no check", not stop()[0])
config("objective-answers", "on")
start("on-again", PUSHBACK)
check("objective-answers on again → hint", pre())

rule = os.path.join(PLUGIN, "rules", "OBJECTIVE.md")
text = open(rule, encoding="utf-8").read()
check("rules/OBJECTIVE.md is always on and fits the size limit", text.startswith("---\ntrigger: always_on\n---") and len(text.encode()) < 24000)

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
