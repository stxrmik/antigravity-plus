"""Facts: claims in the final report against what happened, file and line references, links from nowhere, versions
and prices recalled from memory, the <grounding> hint before a question, and the guard refusing fake checks."""
import json, os, re, shutil, subprocess, sys, time
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


def bg_start(line, task):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line})])
    add("GENERIC", content=f"Created At: {iso()}\nTool is running as a background task with task id: {cid}/{task}\n"
                           f"Task Description: {line}\n")


def bg_cmd(line, task, result):
    bg_start(line, task)
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


def stop(execution=0, roots=None):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": roots or [WS],
         "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": execution}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    reason = d.get("reason") or "" if d.get("decision") == "continue" else ""
    return (reason if "facts" in reason else ""), out.stderr.strip()


def direct(roots=None):
    """The facts check alone, in this process: no other end-of-turn reminder can come before it and hide what it finds."""
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": roots or [WS]}
    return facts.problems(p, {"users": []}, rows[-1].get("content", ""), re.compile(r"\bpytest\b"), (".py",), (".html",))


def clean(roots=None):
    """Neither the Stop hook nor the facts check itself has anything to say about the report: (ok, what was said)."""
    found, reason = direct(roots), stop(roots=roots)[0]
    return not found and not reason, (found, reason)


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

# A test run is a command that runs tests, not one that only names them (seen live: a passing run, then `ls tests/`
# and `rg … tests/…` became «the latest test runs» and the report «44 of 44 passed» was sent back).
start("bg-then-look")
edit(os.path.join(WS, "app", "sort.py"))
bg_cmd("python3 tests/run_tests.py 2>&1 | tail -50", "task-51",
       "Command exited with code 0.\n\nCommand output:\nPASS  test_sort  (1.1 s)\nPASS  test_replay  (0.4 s)\n\n44 of 44 passed")
cmd("ls tests/", output="run_tests.py\ntest_replay.py\ntest_sort.py")
cmd('rg -n "def replay" tests/test_replay.py', output="12:def replay(rows):")
cmd('rg -n "replay" tests/run_tests.py', output="30:    names = [n for n in names if n != 'replay']")
answer("Готово: `python3 tests/run_tests.py` completed (44 of 44 passed).")
good, said = clean()
check("a passing background run, then `ls tests/` and `rg … tests/…` → the run still backs «44 of 44 passed»", good, said)

start("pytest-then-read")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="........\n45 passed, 1 warning in 1.46s")
for line in ("cat tests/test_sort.py", 'grep -rn "def test_" tests/', "git diff --stat -- tests/",
             "sed -n '1,20p' tests/conftest.py", "find tests -name '*.py' | wc -l", "head -5 tests/test_sort.py"):
    cmd(line, output="tests/test_sort.py:3:def test_sort():\n 1 file changed, 3 insertions(+)")
answer("Сортировка починена, тесты проходят: 45 passed.")
good, said = clean()
check("pytest «45 passed», then cat/grep/git diff/sed/find/head on tests/ → still backed", good, said)

start("later-runs")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="45 passed in 1.2s")
for _ in range(4):
    cmd("python3 -m py_compile tests/test_sort.py", output="")
answer("Тесты проходят (45 passed).")
good, said = clean()
check("later runs that print no test count don't hide the run the «45 passed» is about", good, said)

start("newer-count")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="29 passed in 0.4s")
cmd("python3 -m pytest -q -k sort", output="27 passed, 2 deselected in 0.4s")
answer("Тесты прошли (29 passed).")
good, said = clean()
check("the whole set after the change, then a smaller run (one suite) → the whole set still backs «29 passed»",
      good, said)

start("count-before-change")
cmd("python3 -m pytest -q", output="29 passed in 0.4s")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", output="27 passed in 0.4s")
answer("Тесты прошли (29 passed).")
reason = stop()[0]
check("…but a number shown only before the last change → sent back with the newest count",
      "doesn't show 29" in reason and "27 passed" in reason, reason)

start("test-log")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q > /tmp/sort-run.out 2>&1; echo $?", output="0")
cmd("tail -3 /tmp/sort-run.out", output="45 passed in 1.2s")
answer("Тесты прошли: 45 passed.")
good, said = clean()
check("a test run into a file, then `tail` of that file → the count is read there", good, said)

start("cd-run")
edit(os.path.join(WS, "app", "sort.py"))
cmd("cd tests && ./run_all.sh", output="12 passed")
cmd("bash -lc 'ls tests/'", output="run_all.sh")
answer("Тесты проходят (12 passed).")
good, said = clean()
check("`cd tests && ./run_all.sh` is a test run; `bash -lc 'ls tests/'` isn't", good, said)

start("only-looked")
edit(os.path.join(WS, "app", "sort.py"))
cmd("ls tests/ && cat tests/test_sort.py", output="test_sort.py\ndef test_sort(): ...")
cmd('grep -rn "passed" tests/', output='tests/test_sort.py:9:    assert "29 passed"')
answer("Готово, тесты проходят.")
reason = stop()[0]
check("only `ls`/`cat`/`grep` on tests/ after the change → no test run", "no test run after your last change" in reason, reason)

start("still-running")  # the Stop hook waits for the agent's own background commands first (parallel hints)
edit(os.path.join(WS, "app", "sort.py"))
bg_start("python3 -m pytest -q", "task-8")
cmd("ls tests/", output="test_sort.py")
answer("Тесты прошли.")
reason = "\n".join(direct())
config("parallel-hints", "off")
check("a claim while the test run is still going (an `ls tests/` after it doesn't count) → sent back",
      "still running" in reason and "still running" in stop()[0], reason)
config("parallel-hints", "on")

start("build-then-ls")
edit(os.path.join(WS, "app", "sort.py"))
cmd("npm run build", code=1, output="src/sort.ts(3,1): error TS2304: Cannot find name 'x'.")
cmd("ls build/ && cat build.log", output="index.js")
answer("Сборка проходит.")
reason = stop()[0]
check("a failed build, then `ls build/` → the build failed, not «passes»", "failed" in reason and "npm run build" in reason, reason)

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
start("refs-abs")
answer("Логика — в [X.swift](file:///Users/AnyKit/Sources/X.swift#L1-L2).")
reason = stop()[0]
check("a broken absolute file:// link → still named", "X.swift" in reason and "no such file" in reason, reason)

# Paths from a subfolder of the project: the workspace is the repository root and the agent wrote `mcp/server.py`
# for `plugin/mcp/server.py` (seen live). A git repository (git's file list) and a plain folder (a walk).
REPO = os.path.realpath(os.path.join(SP, "repo"))
for sub, lines in (("plugin/mcp/server.py", 12), ("plugin/hooks/quota.py", 3)):
    os.makedirs(os.path.dirname(os.path.join(REPO, sub)), exist_ok=True)
    with open(os.path.join(REPO, sub), "w", encoding="utf-8") as f:
        f.write("".join(f"line {i}\n" for i in range(1, lines + 1)))
for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"], ["update-ref", "refs/remotes/fork/v0.24.0", "HEAD"],
             ["update-ref", "refs/remotes/fork/2.0.x", "HEAD"]):
    subprocess.run(["git"] + args, cwd=REPO, capture_output=True)
os.makedirs(os.path.join(WS, "lib", "core"), exist_ok=True)
with open(os.path.join(WS, "lib", "core", "engine.py"), "w", encoding="utf-8") as f:
    f.write("".join(f"line {i}\n" for i in range(1, 6)))
start("subfolder")
answer("Поправил `mcp/server.py` и `hooks/quota.py` (см. `mcp/server.py:5`, `./mcp/server.py#L2-L4`), [папка хуков](hooks/).")
good, said = clean([REPO])
check("paths from a subfolder of a git repository resolve, line references within the file are fine", good, said)
start("subfolder-walk")
answer("Движок — `core/engine.py:3`; ещё см. [quota](plugin/hooks/quota.py).")
good, said = clean([WS, REPO])
check("…in a plain folder (a walk), and a relative path in another workspace folder", good, said)
start("subfolder-bad")
answer("См. `mcp/server.py:99`, `mcp/invented.py` и `core/engine.py:30`.")
reason = "\n".join(direct([REPO, WS]))
check("a subfolder path's line past the end → named with the file it means", "`mcp/server.py` — the file (`plugin/mcp/server.py`) "
      "has only 12 lines" in reason and "has only 5 lines" in reason, reason)
check("…an invented path that matches no file anywhere → still named", "`mcp/invented.py` — no such file" in reason, reason)
check("…and the Stop hook says so", "mcp/invented.py" in stop(roots=[REPO, WS])[0])

# Git refs aren't paths: `origin/v0.24.0` was named «no such file or folder» (seen live).
start("git-refs")
answer("Сравнил с `origin/v0.24.0`, `upstream/3.x` и `refs/tags/v1.2.0`; на форке — `fork/v0.24.0` и `fork/2.0.x`, "
       "см. [HEAD~2](HEAD~2).")
good, said = clean([REPO])
check("a remote's branch or tag, refs/…, refs git knows (rev-parse) and HEAD~2 aren't paths", good, said)
start("git-refs-bad")
answer("Ветка `nosuch/v9.9.9` и файл `nosuch/app.py`.")
reason = stop(roots=[REPO])[0]
check("…a name that is neither a ref nor a file → still named", "nosuch/v9.9.9" in reason and "nosuch/app.py" in reason, reason)
# A path cut short on purpose isn't one to look for: `.../12345/a1b2c3d4.jpeg` (the tail of a poster URL) was named
# «no such file or folder», and the agent sent its whole report a second time (seen live).
start("elided-paths")
answer("Постеры: `.../12345/a1b2c3d4.jpeg`, `…/67890/e5f6a7b8.jpeg` и `..../a/b.png`; код — `nosuch/app.py`.")
reason = stop(roots=[REPO])[0]
check("a path elided with «...» or «…» isn't named missing — an invented one still is", "12345" not in reason
      and "67890" not in reason and "a/b.png" not in reason and "nosuch/app.py" in reason, reason)
# A path the agent's own script creates while it runs (a probe's temporary `.agents/hooks.json`) was named «no such
# file or folder», and the agent wrote its whole report a second time (seen live). Written into a file it wrote, or
# into a command — as is, or as the quoted parts of a join — it isn't made up.
start("authored-path")
add("PLANNER_RESPONSE", calls=[("write_file", {"Path": os.path.join(WS, "probe.py"), "Content":
    'HOOKS = os.path.join(ws, ".agents", "hooks.json")\nOUT = "tmp/dump/env.json"\n'})])
add("GENERIC", content="Created file")
cmd("mkdir -p build/out && python3 probe.py > build/out/probe.log")
answer("Добавил `probe.py`: пишет временный `.agents/hooks.json`, дамп в `tmp/dump/env.json`, лог — `build/out/probe.log`; "
       "конфиг в `conf/invented.json`.")
reason = "\n".join(direct())
check("a path the agent wrote into a file or a command isn't named missing", ".agents/hooks.json" not in reason
      and "tmp/dump/env.json" not in reason and "build/out/probe.log" not in reason, reason)
check("…one it wrote nowhere still is", "conf/invented.json" in reason, reason)
start("authored-word")
add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": "cat myconf/app.json.bak"})])
answer("Настройки — в `conf/app.json`.")
check("…a longer name that merely contains it doesn't count", "conf/app.json" in "\n".join(direct()))
# A path the agent read here (the docs of a tool not installed on this machine) and a host written as a path.
start("read-path")
cmd("cat docs/cli.md", output="The CLI keeps its settings in ~/.toolcli/settings.json and caches in cache/toolcli/staging.")
answer("По документации настройки в `~/.toolcli/settings.json`, кэш — `cache/toolcli/staging/`; установщик — "
       "`claude.ai/install.sh` и `antigravity.google/cli/install.sh`.")
check("…a path from what it read, or a host written as a path, isn't named missing", not direct(), direct())
# A report that says the run failed claims no success: «FAIL (41 passed, 1 failed)» was sent back (seen live).
start("own-failure")
edit(os.path.join(WS, "app", "sort.py"))
cmd("python3 -m pytest -q", code=1, output="41 passed, 1 failed in 0.4s")
answer("- `pytest -q` → FAIL (41 passed, 1 failed): один тест ещё падает.")
check("a report of a failed run isn't a claim that tests pass", not direct(), direct())
start("own-failure-still")
edit(os.path.join(WS, "app", "sort.py"))
answer("Готово: 41 passed.")
check("…«41 passed» with no run still is", direct())
# Plain text glued to a code span («state/`guard.log`») is no path to look for (the code span alone is checked).
start("glued-span")
answer("Проверка per-conversation state/`guard.log` и `stats/`; файл — `app/sort.py`.")
check("«word/`code`» isn't read as one path", not any("guard.log" in p for p in direct()), direct())
# This hook's own message names the missing path: it isn't evidence for the next report.
start("own-note")
add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>\nStop hook blocked termination: Antigravity Plus — facts: parts of your "
                              "report aren't backed by anything in this conversation:\n- 99.1.7 — versions or prices"
                              "\n</SYSTEM_MESSAGE>")
check("the Stop hook's own words don't make it into the evidence", "99.1.7" not in facts.scan(tp)["seen"])
# Sent back once over a missing path, the agent fixed one line and kept the other: what it was told already isn't
# told again (no loop of rewritten reports).
start("told-once")
answer("См. `nosuch/a.py` и `nosuch/b.py`.")
first = stop()[0]
answer("См. `nosuch/a.py`.")
check("a problem already named in this turn isn't named again", "nosuch/a.py" in first and not stop(execution=1)[0], first)

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
