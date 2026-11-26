"""Progress updates (hooks/progress.py): the main agent hears when to tell the user how it's going — after a background
command fails, and after a long run of tool calls without a word — and polling beside other calls counts as polling."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
art = os.path.join(SP, "progtest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
tp = os.path.join(art, "transcript_full.jsonl")
ok = True
cid = ""
rows = []


def iso(ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def start(name, user="Почини сборку", delegated=False):
    global cid, rows
    cid = f"agp-prog-{name}"
    rows = []
    for f in (tp, os.path.join(conv.STATE_DIR, cid + ".json")):
        if os.path.exists(f):
            os.remove(f)
    text = (conv.DELEGATED_MARK + " " + user) if delegated else user
    add("USER_INPUT", content=f"<USER_REQUEST>{text}</USER_REQUEST>", ago=120)


def add(kind, content="", calls=None, ago=0):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso(ago), "content": content}
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def step(text="", calls=(("read_file", {"Path": "/x/app.py"}),), result="Showing lines 1-10 of 10"):
    add("PLANNER_RESPONSE", content=text, calls=list(calls))
    add("GENERIC", content=f"Created At: {iso()}\nCompleted At: {iso()}\n{result}")


def background(task_id, desc):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": desc, "WaitMsBeforeAsync": 500})])
    add("GENERIC", content=f"Created At: {iso()}\nTool is running as a background task with task id: {task_id}\n"
                           f"Task Description: {desc}\nTask logs are available at: file:///x/task.log")


def finished(task_id, output):
    add("SYSTEM_MESSAGE", content="The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
                                  f"[Message] timestamp={iso()} sender={task_id} priority=MESSAGE_PRIORITY_HIGH "
                                  f"content=Task id \"{task_id}\" finished with result:\n{output}\n</SYSTEM_MESSAGE>")


def pre(tag="progress_update"):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "invocationNum": len(rows),
         "workspacePaths": [SP]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    msgs = [(s.get("systemMessage") or {}).get("systemMessage") or "" for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if tag in m), out.stderr.strip()


def check(name, got, want_in=(), want_out=(), empty=False):
    global ok
    text, err = got
    good = (not text) if empty else bool(text) and all(w in text for w in want_in) and not any(w in text for w in want_out)
    ok &= good and not err
    print(f"{'✅' if good and not err else '❌'} {name:<62} → {(text.splitlines()[1] if text.count(chr(10)) else text)[:80] or '—'}"
          + (f"   stderr: {err}" if err else ""))


# 1. a failing background build: one hint with the failing line, once
start("fail")
background(f"{cid}/task-3", "cd android && ./gradlew test; echo EXIT=$?")
check("build still running → no progress hint", pre(), empty=True)
finished(f"{cid}/task-3", "> Task :mobile:compileTestKotlin FAILED\nFAILURE: Build failed with an exception.\n"
                          "BUILD FAILED in 42s\nEXIT=1\nCommand exited with code 0.")
check("it failed (exit 0 after echo) → read the error, tell the user", pre(),
      ["gradlew test", "FAILURE: Build failed", "re-running it unchanged", "same response as your next tool call"])
check("…said once", pre(), empty=True)

# 2. a passing one says nothing
start("pass")
background(f"{cid}/task-3", "python3 -m pytest -q")
finished(f"{cid}/task-3", "120 passed, 0 failed in 3.1s\nCommand exited with code 0.")
check("pytest passed (0 failed) → nothing", pre(), empty=True)
finished(f"{cid}/task-3", "2 failed, 118 passed\nCommand exited with code 1.")  # a second message: already done
check("a late second message for a finished task → nothing", pre(), empty=True)

# 3. silent tool calls: the hint at QUIET, not before; a word for the user resets; again after QUIET_AGAIN more
start("quiet")
for _ in range(7):
    step()
check("7 silent tool calls → nothing yet", pre(), empty=True)
step()
check("8 silent tool calls → give the user an update", pre(), ["8 tool calls", "one or two sentences", "Then keep working"])
for _ in range(5):
    step()
check("…not again within the same silent run", pre(), empty=True)
step(text="Нашёл причину: кэш классов устарел, пересобираю с clean.")
for _ in range(8):
    step()
check("an update, then 8 more silent → hint again", pre(), ["8 tool calls"])
for _ in range(20):
    step()
check("20 more in the same run → once more", pre(), ["28 tool calls"])
for _ in range(30):
    step()
check("3 hints for this user message → no more", pre(), empty=True)
add("USER_INPUT", content="<USER_REQUEST>Ну и что там?</USER_REQUEST>")
for _ in range(8):
    step()
check("a new user message resets the count and the cap", pre(), ["8 tool calls"])

# 4. a turn that ended with a message resets the run
start("ended")
for _ in range(6):
    step()
add("PLANNER_RESPONSE", content="Жду сборку task-5: она проверит исправление.")
for _ in range(3):
    step()
check("6 silent, an answer, 3 silent → nothing", pre(), empty=True)

# 5. not for tasks from Claude Code, and not when switched off
start("delegated", delegated=True)
for _ in range(10):
    step()
check("delegated task → no progress hints", pre(), empty=True)
start("off")
subprocess.run([sys.executable, MEMORY, "config", "progress-hints", "off"], capture_output=True)
for _ in range(10):
    step()
check("progress-hints off → nothing", pre(), empty=True)
subprocess.run([sys.executable, MEMORY, "config", "progress-hints", "on"], capture_output=True)

# 6. checking on a running command beside other calls is polling too (seen live: in each of 36 steps)
start("piggy")
background(f"{cid}/task-3", "python3 tests/run_tests.py")
pre("background_work")  # the launch notice
poll = ("manage_task", {"Action": "status", "TaskId": f"{cid}/task-3"})
step(calls=[poll, ("read_file", {"Path": "/x/a.py"})], result="Task: x\nStatus: RUNNING")
step(calls=[poll, ("read_file", {"Path": "/x/b.py"})], result="Task: x\nStatus: RUNNING")
check("status check beside work, twice → not yet", pre("background_work"), empty=True)
step(calls=[poll, ("read_file", {"Path": "/x/c.py"})], result="Task: x\nStatus: RUNNING")
check("third time → stop polling", pre("background_work"), ["doesn't make them faster", "run_tests.py"])
for _ in range(4):
    step(calls=[poll, ("read_file", {"Path": "/x/d.py"})], result="Task: x\nStatus: RUNNING")
check("…not at every step after that", pre("background_work"), empty=True)


# 7. Antigravity's «still running after 300s» notice is not the command's report: it still runs
start("still")
background(f"{cid}/task-3", "./gradlew assemble")
pre("background_work")
add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>\n[Message] timestamp=" + iso() + f" sender={cid}/task-3 priority=MESSAGE_PRIORITY_HIGH "
                              "content=Background command is still running after 300s: ./gradlew assemble\n</SYSTEM_MESSAGE>")
st = conv.load({"conversationId": cid, "transcriptPath": tp})
check("still-running notice → the command still runs", ("running" if st["bg"][f"{cid}/task-3"]["st"] == "run" else "", ""),
      ["running"])
finished(f"{cid}/task-3", "BUILD SUCCESSFUL in 4m\nCommand exited with code 0.")
st = conv.load({"conversationId": cid, "transcriptPath": tp})
check("…and its real result ends it", ("done" if st["bg"][f"{cid}/task-3"]["st"] == "done" else "", ""), ["done"])

# 8. failures from a task Claude Code handed over don't pile up for when a person writes in
start("taken", delegated=True)
background(f"{cid}/task-3", "npm test")
finished(f"{cid}/task-3", "Tests: 2 failed, 40 passed\nCommand exited with code 1.")
check("delegated: a failed command → no hint", pre(), empty=True)
add("USER_INPUT", content="<USER_REQUEST>Ну и что со сборкой?</USER_REQUEST>")
step()
check("a person writes in → the old failure isn't news", pre(), empty=True)
background(f"{cid}/task-9", "npm run build")
finished(f"{cid}/task-9", "npm ERR! code ELIFECYCLE\nCommand exited with code 1.")
check("…a new one is", pre(), ["npm run build", "npm ERR!"])

sys.exit(0 if ok else 1)
