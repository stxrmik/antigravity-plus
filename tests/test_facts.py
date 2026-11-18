"""Facts: claims in the final report against what happened, file and line references, links from nowhere, versions
and prices recalled from memory, the <grounding> hint before a question, and the guard refusing fake checks."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv, facts
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
art = os.path.join(SP, "factstest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
WS = os.path.realpath(os.path.join(SP, "proj")); os.makedirs(os.path.join(WS, "app"), exist_ok=True)
with open(os.path.join(WS, "app", "sort.py"), "w", encoding="utf-8") as f:
    f.write("".join(f"line {i}\n" for i in range(1, 41)))
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


def start(name, user="Почини сортировку серверов", main=True):
    global cid, rows
    cid = f"agp-factstest-{name}"
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


def cmd(line, code=0, output="ok"):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line})])
    add("GENERIC", content=f"Created At: {iso()}\nCommand exited with code {code}.\n\nCommand output:\n{output}\n")


def bg_cmd(line, task, result):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line})])
    add("GENERIC", content=f"Created At: {iso()}\nTool is running as a background task with task id: {cid}/{task}\n"
                           f"Task Description: {line}\n")
    add("SYSTEM_MESSAGE", content=f"<SYSTEM_MESSAGE>\n[Message] timestamp={iso()} sender={cid}/{task} "
                                  f"content=Task id \"{cid}/{task}\" finished with result:\n{result}\n</SYSTEM_MESSAGE>")


def edit(path):
    add("PLANNER_RESPONSE", calls=[("edit_file", {"Path": path, "Target": "a", "Replacement": "b"})])
    add("GENERIC", content="The following changes were made")


def search(results):
    add("PLANNER_RESPONSE", calls=[("search_web", {"query": "x"})])
    add("GENERIC", content=results)


def answer(text):
    add("PLANNER_RESPONSE", content=text)


def stop(execution=0):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS],
         "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": execution}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    reason = d.get("reason") or "" if d.get("decision") == "continue" else ""
    return (reason if "facts" in reason else ""), out.stderr.strip()


def pre():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [WS]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [(s.get("systemMessage") or {}).get("systemMessage") or s.get("ephemeralMessage") or ""
            for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "<grounding" in m)


def guard(line):
    p = {"conversationId": cid, "toolCall": {"name": "run_command", "args": {"CommandLine": line}}, "workspacePaths": [WS]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout or "{}").get("decision")


def config(*pairs):
    subprocess.run([sys.executable, MEMORY, "config", *pairs], capture_output=True, text=True)


# The other end-of-turn reminders (set up git, verify changed code, review) come first in a bare folder; this test
# looks at the report check alone.
config("auto-setup", "off", "auto-verify", "off", "auto-review", "off")

# ── claims of work against the commands that ran ──
start("no-tests")
edit(os.path.join(WS, "app", "sort.py"))
answer("Готово: сортировка починена, все тесты проходят.")
reason, err = stop()
check("«тесты проходят» with no test run → sent back", "тесты проходят" in reason and "no test run" in reason, reason or err)
check("…once for the same report", not stop(execution=1)[0])

start("passed")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="29 passed in 0.4s")
answer("Готово: сортировка починена, тесты проходят (29 passed).")
check("a passing test run after the last edit backs it", not stop()[0])

start("stale")
cmd("python3 -m pytest -q", output="29 passed in 0.4s")
edit(os.path.join(WS, "app", "sort.py"))
answer("Тесты проходят.")
check("a test run before the last edit doesn't count", "after your last change" in stop()[0])

start("failed")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", code=1, output="1 failed, 28 passed in 0.5s")
answer("Сортировка починена, тесты прошли.")
check("the latest test run failed → says so", "failed" in stop()[0])

start("number")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="27 passed in 0.4s")
answer("Тесты прошли (`29 passed`).")
reason = stop()[0]
check("«29 passed» when the output says 27 → the real summary is quoted", "doesn't show 29" in reason and "27 passed" in reason, reason)

start("background")
edit(os.path.join(WS, "app", "sort.py"))
bg_cmd("docker compose --profile test run --rm test", "task-7",
       "The tool's output was truncated due to being too long.\nLog output:\n<truncated 900 lines>\n1 failed, 21 passed in 0.6s")
answer("Полный прогон тестов в Docker — **22 passed**.")
check("a background run whose cut output reports a failure → not a pass", "failed" in stop()[0] or "doesn't show" in stop()[0])

start("ran")
cmd("docker compose --profile test run --rm test", output="building…")
answer("Запустил прогон тестов в Docker, жду завершения.")
check("«запустил тесты» only needs the command to have run", not stop()[0])

start("deploy")
answer("Задеплоил панель на сервер, всё обновлено.")
check("«задеплоил» with no deploy command → sent back", "deploy command" in stop()[0])
start("deploy-ok")
cmd("./deploy/update.sh", output="done")
answer("Задеплоил панель на сервер.")
check("…backed by ./deploy/update.sh", not stop()[0])

config("auto-verify", "on")
start("declined")  # seen live: told to verify, the agent stops again and still claims the tests pass
edit(os.path.join(WS, "app", "sort.py"))
answer("Готово, тесты проходят.")
first = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], capture_output=True, text=True, encoding="utf-8",
                       input=json.dumps({"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art,
                                         "workspacePaths": [WS], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
                                         "executionNum": 0})).stdout
answer("Тесты не запускались по требованию пользователя. Готово, тесты проходят.")
check("after the verify reminder, a claim it still makes gets the facts check",
      "ran no check" in first and "тесты проходят" in stop(execution=1)[0], first[:200])
config("auto-verify", "off")

start("hedged")
answer("Чтобы тесты прошли, нужно поправить фикстуру. Запустите тесты: они должны пройти. test-writer сообщил, что тесты проходят.")
check("conditions, suggestions and someone else's report aren't claims", not stop()[0])

# ── /perf performance claims ──
for cname, claim_txt in [
    ("ru-times", "Ускорил сортировку в 3 раза."),
    ("ru-faster", "Стало быстрее на 40%."),
    ("ru-mem", "Снизил потребление памяти на 25%."),
    ("en-times", "Sorting is now 2.5x faster."),
    ("en-pct", "Reduced latency by 35%."),
]:
    start(f"perf-unbacked-{cname}")
    edit(os.path.join(WS, "app", "sort.py"))
    answer(claim_txt)
    reason = stop()[0]
    check(f"unbacked perf claim caught ({cname})", "benchmark measurement" in reason, reason)

for bname, bench_cmd, claim_txt in [
    ("perf-py", "python3 perf.py --runs 10", "Ускорил сортировку в 3 раза."),
    ("hyperfine", "hyperfine 'python3 app/sort.py'", "Sorting is now 2x faster."),
    ("timeit", "python3 -m timeit -s 'from app import sort'", "Снизил задержку на 25% — стало быстрее на 25%."),
]:
    start(f"perf-backed-{bname}")
    edit(os.path.join(WS, "app", "sort.py"))
    cmd(bench_cmd, output="mean: 12.4 ms")
    answer(claim_txt)
    check(f"perf claim backed by {bname} allowed", not stop()[0])

for vname, verdict, caught in [
    ("inconclusive", "inconclusive (only 5 runs per side: a verdict needs at least 10)", True),
    ("regression", "clear regression", True),
    ("improvement", "clear improvement", False),
]:
    start(f"perf-verdict-{vname}")
    edit(os.path.join(WS, "app", "sort.py"))
    cmd("python3 skills/perf/scripts/perf.py compare --base-cmd 'python3 a.py' --new-cmd 'python3 b.py'",
        output=f"Speedup: 1.40x faster\nIQR overlap: no -> Verdict: {verdict}")
    answer("Ускорил сортировку в 1.4 раза.")
    reason = stop()[0]
    check(f"perf.py verdict «{vname}» {'refutes' if caught else 'backs'} a speed-up claim",
          ("clear improvement" in reason) == caught, reason)

start("perf-stale")
cmd("hyperfine 'python3 app/sort.py'", output="mean: 10 ms")
edit(os.path.join(WS, "app", "sort.py"))
answer("Ускорил сортировку в 2 раза.")
check("benchmark before last edit doesn't count", "after your last change" in stop()[0])

start("perf-hedged")
edit(os.path.join(WS, "app", "sort.py"))
answer("Это должно ускорить сортировку в 2 раза; should be 2x faster; по отчёту researcher стало быстрее на 30%.")
check("hedged / attributed perf claim allowed", not stop()[0])

# ── references ──
start("refs")
answer("Поправил [`app/sort.py`](app/sort.py) и [`app/missing.py`](app/missing.py), см. `app/sort.py:120`.")
reason = stop()[0]
check("a link to a file that doesn't exist → named", "app/missing.py" in reason and "no such file" in reason, reason)
check("a line past the end of the file → named with the real length", "only 40 lines" in reason, reason)
start("refs-ok")
answer("Логика в [`app/sort.py:12`](app/sort.py:12), удалил старый `app/old_sort.py`; ветка `origin/main`, тип `text/html`, "
       "на сервере `sites-available/example.com`.")
check("real files, removed files, git refs, MIME types and server paths aren't flagged", not stop()[0])
with open(os.path.join(art, "walkthrough.md"), "w", encoding="utf-8") as f:
    f.write("# walkthrough\n")
start("refs-artifact")
answer("Подробности — в [walkthrough](walkthrough.md).")
check("a link to an artifact of the conversation resolves", not stop()[0])

# ── links and recalled facts ──
start("url", "Какая последняя версия fastcache?")
answer("Последняя версия — 25.10.15, см. https://github.com/acme/fastcache/releases/tag/v25.10.15.")
reason = stop()[0]
check("a link no search showed → flagged", "github.com/acme/fastcache/releases" in reason, reason)
check("…and a version found nowhere in the conversation", "25.10.15" in reason and "versions or prices" in reason, reason)
start("url-ok", "Какая последняя версия fastcache?")
search("Results: https://github.com/acme/fastcache/releases/tag/v25.10.15 — fastcache v25.10.15 released 2025-10-15")
answer("Последняя версия — 25.10.15 (релиз от 2025-10-15): https://github.com/acme/fastcache/releases/tag/v25.10.15")
check("a version and a link that came from the search → fine", not stop()[0])
start("price", "Сколько стоит такой сервер в месяц?")
answer("Примерно $5 в месяц у Hetzner — по памяти, не проверял.")
check("a price marked «по памяти, не проверял» isn't flagged", not stop()[0])
start("own-host", "Проверь панель на panel.myshop.example.ru")
cmd("curl -s https://panel.myshop.example.ru/health", output='{"status":"ok"}')
answer("Панель отвечает: https://panel.myshop.example.ru/health → ok.")
check("a link to the user's own service (named by the user, curled) isn't flagged", not stop()[0])

# ── subagents, settings ──
start("sub", main=False)
answer("Done: сортировка работает. Verified: тесты проходят.")
check("a subagent's report gets the same check", "тесты проходят" in stop()[0])
config("fact-checks", "off")
start("off")
answer("Готово, все тесты проходят.")
check("fact-checks off → no check", not stop()[0])
config("fact-checks", "on")

# ── <grounding> before a question ──
start("ground-web", "Найди, какая сейчас актуальная версия fastcache и что в ней поменялось")
hint = pre()
check("a research request → <grounding> to look it up and cite links", "researcher" in hint and "primary source" in hint, hint[:200])
check("…once per message", not pre())
start("ground-code", "Где у нас считается цена подписки?")
check("a question about the code → <grounding> to cite file:line", "file:line" in pre())
start("ground-none", "Сделай кнопку синей")
check("a plain task → no hint", not pre())

# ── the guard refuses checks that check nothing ──
start("guard")
for line in ('echo "All tests passed"', "python3 -c \"print('Tests passed')\"", "echo 'Проверка пройдена'"):
    check(f"fake check refused: {line}", guard(line) == "deny")
for line in ("python3 -m pytest -q && echo ok", "echo ok > status.txt", "echo $PATH", 'echo "Building…"'):
    check(f"real command allowed: {line}", guard(line) != "deny")

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
