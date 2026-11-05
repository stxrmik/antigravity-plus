"""Stop hook: project setup (/git-init, /init) and announce-and-stop answers."""
import json, os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
cid = "agp-setuptest-0001"
art = os.path.join(SP, "su_art"); tp = os.path.join(art, "transcript_full.jsonl")
def clear_state():
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
def setup(ws, steps, answer="Готово.", fresh=True, files=None, repo=False):
    if fresh:
        for d in (ws, art): shutil.rmtree(d, ignore_errors=True)
        os.makedirs(ws, exist_ok=True); os.makedirs(art, exist_ok=True); clear_state()
        for rel, text in (files or {}).items(): open(os.path.join(ws, rel), "w").write(text)
        if repo: subprocess.run(["git", "init", "-q"], cwd=ws)
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-04T10:00:00Z", "content": "<USER_REQUEST>Сделай фичу</USER_REQUEST>"}]
    for i, (name, args) in enumerate(steps, 1):
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": name, "args": args}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": answer})
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def stop(ws):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:300])
    d = json.loads(r.stdout or "{}"); return d.get("reason") if d.get("decision") == "continue" else None
ws = os.path.join(SP, "su_ws")
w = lambda n: ("write_file", {"Path": os.path.join(ws, n), "Content": "x=1\n"})
cmd = lambda c: ("run_command", {"CommandLine": c})
ok = True
def check(name, got, want_git, want_init, want_offer=False, silent=False):
    global ok
    if silent: good = got is None
    else:
        good = bool(got) and ("create the git repository" in (got or "")) == want_git and ("run /init" in (got or "")) == want_init \
               and (("you offered" in (got or "")) == want_offer)
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
setup(ws, [w("a.py"), cmd("pytest -q")]); check("code changed, no git, no rules", stop(ws), True, True)
got = stop(ws); check("second stop: no repeat", got, False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q"), cmd(f'python3 "/x/skills/git-init/scripts/repo_init.py" --path "{ws}"'),
           ("read_file", {"Path": "/x/skills/init/SKILL.md"})]); check("agent did both itself", stop(ws), False, False, silent=True)
setup(ws, [], "Ответил на вопрос. При желании можно один раз запустить `/init`, чтобы сохранить команды.")
check("no changes, but offered /init", stop(ws), True, True, want_offer=True)
setup(ws, [w("a.py"), cmd("pytest -q")], "Готово. Хотите, чтобы я создал AGENTS.md и git-репозиторий?")
check("asked «Хотите, чтобы я создал AGENTS.md…?»", stop(ws), True, True, want_offer=True)
setup(ws, [w("a.py"), cmd("pytest -q")], "Готово. Какой цвет кнопки выбрать — синий или зелёный?")
check("genuine question to the user", stop(ws), False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q")], files={"AGENTS.md": "# rules\n"}, repo=True)
check("repo with AGENTS.md: nothing to set up", stop(ws), False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q")], files={"CLAUDE.md": "# rules\n"})
check("CLAUDE.md counts as rules: only git", stop(ws), True, False)
setup(ws, [], "Ответил на вопрос.")
check("no changes, no offer", stop(ws), False, False, silent=True)
dl = os.path.expanduser("~/Downloads")
os.makedirs(dl, exist_ok=True)
setup(ws, [("write_file", {"Path": os.path.join(dl, "x.py"), "Content": "1"}), cmd("pytest")])
check("workspace ~/Downloads: never", stop(dl), False, False, silent=True)
setup(ws, [w("a.py")])
got = stop(ws); good = bool(got) and "ran no check" in got and "Set up the project" in got; ok &= good
print(f"{'✅' if good else '❌'} {'verify + setup in one message':<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
def ann(name, answer, steps, want):
    global ok
    setup(ws, steps, answer, files={"AGENTS.md": "# r\n"}, repo=True)
    got = stop(ws); good = (bool(got) and "ended your turn without doing it" in got) == want; ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
ann("announced /git-init and stopped (live failure)", "Инициализирую репозиторий Git для проекта с помощью `/git-init`.", [], True)
ann("«Сейчас запущу тесты.» after writing code", "Сейчас запущу тесты.", [w("a.py")], True)
ann("English «Let me run the tests.»", "Let me run the tests.", [], True)
ann("a real final report (long)", "Сделал утилиту wordcount.py: считает слова в файле, путь — аргумент. " * 6, [w("a.py"), cmd("pytest")], False)
ann("short done message", "Готово: wordcount.py и тест, тест проходит.", [w("a.py"), cmd("pytest")], False)
ann("announce with a question", "Запускаю тесты? Или сначала посмотрите код?", [], False)
for d in (ws, art): shutil.rmtree(d, ignore_errors=True)
clear_state()
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
