"""Parallel work: hints while subagents and background commands run, the plan and research hints, and the Stop hook
when the agent ends its turn with its own background work still running."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
MEMORY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
art = os.path.join(SP, "partest"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
tp, task = os.path.join(art, "transcript_full.jsonl"), os.path.join(art, "task.md")
ok = True
cid = ""


def iso(ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def start(name, user="Добавь поддержку INCY", main=True):
    global cid, rows
    cid = f"agp-par-{name}"
    rows = []
    for f in (tp, task, os.path.join(conv.STATE_DIR, cid + ".json")):
        if os.path.exists(f):
            os.remove(f)
    if main:
        add("USER_INPUT", content=f"<USER_REQUEST>{user}</USER_REQUEST>", ago=120)
    else:
        add("SYSTEM_MESSAGE", content="<SYSTEM_MESSAGE>The following system message was injected by a SessionStart hook</SYSTEM_MESSAGE>")


def add(kind, content="", calls=None, ago=0):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso(ago), "content": content}
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def subagent(sid, ago=0):
    """A subagent's own transcript in brain/ (its mtime says whether it still works)."""
    d = os.path.join(conv.BRAIN, sid, ".system_generated", "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "transcript_full.jsonl")
    open(p, "w").write("{}\n")
    t = time.time() - ago
    os.utime(p, (t, t))


def invoke(*specs, ago=0):
    """One step with an invoke_subagent call per (type, role, id), then its results."""
    add("PLANNER_RESPONSE", calls=[("invoke_subagent", {"TypeName": t, "Role": r, "Prompt": "…", "Model": "flash"})
                                   for t, r, _ in specs], ago=ago)
    for _t, _r, sid in specs:
        created(sid, ago=ago)


def created(*sids, ago=0):
    body = "".join(f'{{\n  "conversationId":  "{s}",\n  "logAbsoluteUri":  "file:///x"\n}}\n' for s in sids)
    add("GENERIC", content=f"Created At: {iso(ago)}\nCompleted At: {iso(ago)}\nCreated the following subagent:\n{body}"
                           "The subagent will send you a message when it has completed its task or requires guidance.", ago=ago)
    for s in sids:
        subagent(s, ago)


def background(task_id, desc):
    add("GENERIC", content=f"Created At: {iso()}\nTool is running as a background task with task id: {task_id}\n"
                           f"Task Description: {desc}\nTask logs are available at: file:///x/task.log")


def report(sender):
    add("SYSTEM_MESSAGE", content="The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
                                  f"[Message] timestamp={iso()} sender={sender} priority=MESSAGE_PRIORITY_HIGH content=Done.")


def pre():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "invocationNum": len(rows),
         "workspacePaths": [SP]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p),
                         capture_output=True, text=True, encoding="utf-8")
    msgs = [s["ephemeralMessage"] for s in json.loads(out.stdout or "{}").get("injectSteps", [])]
    return "\n".join(m for m in msgs if "background_work" in m or "parallel_" in m), out.stderr.strip()


def stop(extra=None):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "terminationReason": "NO_TOOL_CALL",
         "fullyIdle": True, "executionNum": 0, "workspacePaths": [SP]}
    p.update(extra or {})
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True,
                         text=True, encoding="utf-8")
    d = json.loads(out.stdout or "{}")
    return (d.get("reason") or "") if d.get("decision") == "continue" else "", out.stderr.strip()


def check(name, got, want_in=(), want_out=(), empty=False):
    global ok
    text, err = got if isinstance(got, tuple) else (got, "")
    good = (not text) if empty else bool(text) and all(w in text for w in want_in) and not any(w in text for w in want_out)
    ok &= good and not err
    print(f"{'✅' if good and not err else '❌'} {name:<58} → {(text.splitlines()[1] if text.count(chr(10)) else text)[:90] or '—'}"
          + (f"   stderr: {err}" if err else ""))


# 1. a launch: what runs now and how to wait; once
start("launch")
invoke(("explorer", "Map the subscription code", "sub-aaaa-0001"))
check("explorer started → background_work", pre(),
      ["<background_work", "explorer «Map the subscription code»", "don't poll", "End your turn only"])
check("same state again → nothing new", pre(), empty=True)

# 2. polling: manage_subagents list, then a short timer (the timer itself isn't work)
add("PLANNER_RESPONSE", calls=[("manage_subagents", {"Action": "list"})])
add("GENERIC", content="You have 1 active subagent(s): [...]")
check("manage_subagents list while it runs → stop polling", pre(), ["doesn't make them faster", "explorer"])
check("…said once", pre(), empty=True)
add("PLANNER_RESPONSE", calls=[("schedule", {"DurationSeconds": 30, "TimerCondition": "sub-aaaa-0001", "Prompt": "check"})])
background(f"{cid}/task-9", "Timer: 30s, Prompt: check")
check("short schedule timer → polling again (timer not listed)", pre(), ["doesn't make them faster"], ["Timer"])
add("PLANNER_RESPONSE", calls=[("read_file", {"Path": os.path.join(conv.BRAIN, "sub-aaaa-0001", ".system_generated", "logs", "transcript.jsonl")})])
check("reading its transcript → polling", pre(), ["doesn't make them faster"])
add("PLANNER_RESPONSE", calls=[("read_file", {"Path": os.path.join(SP, "app.py")}), ("manage_subagents", {"Action": "list"})])
check("real work in the same step → not polling", pre(), empty=True)

# 3. an explorer reports, nothing else runs → its message says it all
report("sub-aaaa-0001")
check("explorer reported, nothing else running → quiet", pre(), empty=True)

# 4. two at once; one reports while the other still works
start("two")
invoke(("explorer", "Find handlers", "sub-bbbb-0001"), ("researcher", "INCY deeplinks", "sub-bbbb-0002"))
check("two subagents in one step → both listed", pre(), ["explorer «Find handlers»", "researcher «INCY deeplinks»"])
report("sub-bbbb-0001")
check("one reports → who still works", pre(), ["Reported back: explorer", "Still working: researcher"])

# 5. a worker reports → check its work
start("worker")
invoke(("worker", "Build the INCY card", "sub-cccc-0001"))
pre()
report("sub-cccc-0001")
check("worker reported → check the files it changed", pre(), ["Reported back: worker", "re-run its check", "Nothing else"])
add("PLANNER_RESPONSE", calls=[("send_message", {"Recipient": "sub-cccc-0001", "Message": "Also add the QR code."})])
check("follow-up via send_message → it works again", pre(), ["<background_work", "worker «Build the INCY card»"])
add("PLANNER_RESPONSE", calls=[("send_message", {"Recipient": "sub-cccc-0001", "Message": "Are you done yet?"})])
check("«are you done yet?» → polling", pre(), ["doesn't make them faster"])

# 6. the Subagents-list form of invoke_subagent; a namespaced type
start("list")
add("PLANNER_RESPONSE", calls=[("invoke_subagent", {"Subagents": [{"TypeName": "antigravity-plus:explorer", "Role": "Area A"},
                                                                    {"TypeName": "code-reviewer", "Role": "Review"}]})])
created("sub-dddd-0001", "sub-dddd-0002")
check("Subagents list in one call → both, plugin prefix dropped", pre(), ["explorer «Area A»", "code-reviewer «Review»"],
      ["antigravity-plus:"])

# 7. a command that went to the background
start("cmd")
add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": "npm test", "WaitMsBeforeAsync": 5000})])
background(f"agp-par-cmd/task-3", "npm test")
check("background command → listed as a command", pre(), ["command «npm test»"])
add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": "sleep 20"})])
check("sleep while it runs → polling", pre(), ["doesn't make them faster"])
report("agp-par-cmd/task-3")
check("command finished → quiet", pre(), empty=True)

# 8. a stale subagent (its transcript hasn't changed for 30 min) isn't waited for
start("stale")
invoke(("explorer", "Old search", "sub-eeee-0001"), ago=1800)
check("subagent silent for 30 min → not running", pre(), empty=True)

# 9. the plan hint: a fresh task.md with 3+ open items and nothing delegated; once per user message
start("plan")
add("PLANNER_RESPONSE", calls=[("write_file", {"Path": task, "Content": "…"})])
open(task, "w").write("## INCY\n- [/] Найти, где собираются deeplinks\n- [ ] Узнать формат incy://\n- [ ] Добавить карточку\n- [ ] Тесты\n")
check("task.md with 4 open items → parallel_plan", pre(), ["<parallel_plan", "4 open items", "ONE step", "researcher"])
check("…once per user message", pre(), empty=True)
start("plan2")
invoke(("researcher", "INCY format", "sub-ffff-0001"))
pre()
open(task, "w").write("## INCY\n- [/] A\n- [ ] B\n- [ ] C\n")
check("already delegating in this turn → no plan hint", pre(), empty=True)
start("plan3")
open(task, "w").write("## INCY\n- [ ] A\n- [ ] B\n")
check("2 open items → no plan hint", pre(), empty=True)

# 10. the research hint after 3 web-search-only steps
start("search")
for q in ("incy deeplink", "incy app store", "incy routing"):
    add("PLANNER_RESPONSE", calls=[("search_web", {"query": q}), ("search_web", {"query": q + " docs"})])
    add("GENERIC", content="The search returned …")
check("3 rounds of web search → hand it to researcher", pre(), ["<parallel_hint", "3 rounds", "researcher"])
add("PLANNER_RESPONSE", calls=[("search_web", {"query": "more"})])
check("…once per user message", pre(), empty=True)
start("search2")
for q in ("a", "b"):
    add("PLANNER_RESPONSE", calls=[("search_web", {"query": q})])
add("PLANNER_RESPONSE", calls=[("read_file", {"Path": "x"})])
add("PLANNER_RESPONSE", calls=[("search_web", {"query": "c"})])
check("searches interrupted by other work → no hint", pre(), empty=True)

# 11. a subagent launching its own helper gets the launch hint, never the plan or research hints
start("sub", main=False)
invoke(("explorer", "Find the fixture", "sub-gggg-0001"))
check("subagent started explorer → background_work", pre(), ["explorer «Find the fixture»"])
for q in ("x", "y", "z"):
    add("PLANNER_RESPONSE", calls=[("search_web", {"query": q})])
check("subagent (e.g. researcher) searching → no research hint", pre(), empty=True)

# 12. Stop: the agent ended its turn while its subagent works
OPEN = "## INCY\n- [/] Узнать формат incy:// — researcher\n- [ ] Добавить карточку в шаблон\n- [ ] Тесты\n"
start("stop")
invoke(("researcher", "INCY format", "sub-hhhh-0001"))
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Жду ответа researcher.")
check("ended turn, subagent runs, own items open → back once", stop(),
      ["researcher «INCY format» is still working", "Добавить карточку", "don't depend on their results"], ["incy:// — researcher"])
check("…same items again → lets it wait", stop({"executionNum": 1}), empty=True)
start("stop2")
invoke(("researcher", "INCY format", "sub-hhhh-0002"))
open(task, "w").write("## INCY\n- [/] Узнать формат incy:// — researcher\n- [x] Карточка\n")
add("PLANNER_RESPONSE", content="Жду ответа researcher.")
check("only delegated items left → waiting is fine", stop(), empty=True)
start("stop3")
invoke(("researcher", "INCY format", "sub-hhhh-0003"))
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Пока researcher ищет: какую иконку поставить для INCY?")
check("agent asks the user → quiet", stop(), empty=True)
start("stop3b")
invoke(("researcher", "INCY format", "sub-hhhh-0004"))
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Карточку сделаю после выбора. Жду вашего ответа по иконке.")
check("«жду вашего ответа» → quiet", stop(), empty=True)
start("stop4")
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Сборка идёт.")
check("fullyIdle false, nothing tracked → background tasks", stop({"fullyIdle": False}), ["background tasks you started"])
start("stop5")
invoke(("code-reviewer", "Review", "sub-hhhh-0005"))
for f in ("a.py", "b.py", "c.py"):
    add("PLANNER_RESPONSE", calls=[("write_file", {"Path": os.path.join(SP, f), "Content": "x = 1\n"})])
add("PLANNER_RESPONSE", content="Ревью идёт.")
check("review running, no task.md → no review/verify nudge", stop(), empty=True)

# 13. a dev server keeps running: nobody waits for it to end
start("server")
add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": "npm run dev", "WaitMsBeforeAsync": 3000})])
background("agp-par-server/task-3", "npm run dev")
check("dev server in the background → no hint", pre(), empty=True)
add("PLANNER_RESPONSE", calls=[("manage_task", {"Action": "status", "TaskId": "agp-par-server/task-3"})])
check("checking the server's logs → not polling", pre(), empty=True)
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Сервер запущен.")
check("only a dev server runs (fullyIdle false) → silent, as before", stop({"fullyIdle": False}), empty=True)
add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": "pytest -q", "WaitMsBeforeAsync": 5000})])
background("agp-par-server/task-9", "pytest -q")
check("tests in the background next to it → listed, server not", pre(), ["command «pytest -q»"], ["npm run dev"])

# 13. parallel-hints off: no hints, and the Stop hook is silent while background tasks run (as before)
subprocess.run([sys.executable, MEMORY, "config", "parallel-hints", "off"], capture_output=True)
start("off")
invoke(("explorer", "Map", "sub-iiii-0001"))
check("parallel-hints off → no background_work", pre(), empty=True)
open(task, "w").write(OPEN)
add("PLANNER_RESPONSE", content="Жду.")
check("parallel-hints off, background tasks running → silent", stop({"fullyIdle": False}), empty=True)
subprocess.run([sys.executable, MEMORY, "config", "parallel-hints", "on"], capture_output=True)

shutil.rmtree(art, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
