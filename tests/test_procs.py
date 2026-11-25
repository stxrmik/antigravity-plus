"""Agents' commands and the user's own work (hooks/procs.py): the process table and its trees, lowering a process's
priority (never raising it), the background helper lowering an agent command's tree under a stand-in language server,
the guard starting that helper for agentPriority and holding heavy commands back for heavyMax (in Bypass Mode too), and
settings that are refused or read harmlessly. Only processes this test starts are ever touched: every lookup is pinned
to a fake language server (AGP_PROCS_LS_PID; "0" = none)."""
import glob, json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
os.environ["AGP_PROCS_LS_PID"] = "0"  # no language server but the fake ones pinned below: nothing real is touched
sys.path.insert(0, HOOKS)
import compat, procs
ok = True
DEVNULL = subprocess.DEVNULL
HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".gemini", "antigravity-plus", "settings.json")
LOG = os.path.join(HOME, ".gemini", "antigravity-plus", "guard.log")
NEVER = 2 ** 31 - 1  # a pid no process has (Windows: not a multiple of 4; elsewhere above pid_max)
P = compat.Proc
fakes = []  # the fake language servers, killed at the end with everything under them


def check(name, cond, got=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:400]}"))


def wait_for(fn, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while True:
        got = fn()
        if got or time.time() > end:
            return got
        time.sleep(step)


def level_of(pid):
    if compat.WINDOWS:
        return procs.priority(pid)
    try:
        n = os.getpriority(os.PRIO_PROCESS, pid)
    except OSError:
        return "?"
    return "idle" if n >= 19 else "below_normal" if n >= 10 else "normal"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def settings(**values):
    write(SETTINGS, json.dumps(values))


def logged(rule):
    try:
        with open(LOG, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip() and json.loads(ln).get("rule") == rule)
    except OSError:
        return 0


def guard(cmd, env=None, cid="agp-procs-test"):
    payload = {"conversationId": cid, "workspacePaths": [SP],
               "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    started = time.time()
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps(payload), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=dict(os.environ, **(env or {})), timeout=90)
    if r.stderr.strip():
        print("   stderr:", r.stderr.strip()[:300])
    return json.loads(r.stdout or "{}"), time.time() - started


SLEEPER = write(os.path.join(SP, "sleeper.py"), "import sys, time\ntime.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 120)\n")
HOOK_LIKE = write(os.path.join(SP, "hooks", "guard.py"), "import sys, time\ntime.sleep(float(sys.argv[1]))\n")
FAKE_LS = write(os.path.join(SP, "fake_ls.py"), """import json, os, subprocess, sys, time
# stands in for Antigravity's language server: starts the children it is given, prints their pids, waits
pids = []
for spec in json.loads(sys.argv[1]):
    kw = {"start_new_session": True} if spec.get("session") and os.name != "nt" else {}
    env = dict(os.environ, **spec.get("env", {}))
    pids.append(subprocess.Popen(spec["args"], env=env, stdin=subprocess.DEVNULL, **kw).pid)
print(json.dumps(pids), flush=True)
time.sleep(300)
""")
SHELL_ENV = {"FAKE_PY": sys.executable, "FAKE_SLEEPER": SLEEPER}


def agent_shell(session=False):
    """An agent command as the language server starts it: a shell that runs a sleeping Python."""
    if compat.WINDOWS:
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "& $env:FAKE_PY $env:FAKE_SLEEPER 300"]
    else:
        args = ["sh", "-c", '"$FAKE_PY" "$FAKE_SLEEPER" 300; exit 0']
    return {"args": args, "env": SHELL_ENV, "session": session}


def hook_shell():
    """A hook as the language server runs it: Windows `cmd.exe /c …`, elsewhere `sh -c` with hooks/<name>.py."""
    if compat.WINDOWS:
        return {"args": [os.environ.get("COMSPEC") or "cmd.exe", "/c", sys.executable, HOOK_LIKE, "300"]}
    return {"args": ["sh", "-c", '"$FAKE_PY" "$0" 300; exit 0', HOOK_LIKE], "env": SHELL_ENV}


def start_ls(specs):
    """A fake language server with these children: (its pid, the children's pids)."""
    # Windows: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP — its shells share one hidden console, no window pops up
    kw = {"creationflags": 0x08000000 | 0x00000200} if compat.WINDOWS else {"start_new_session": True}
    ls = subprocess.Popen([sys.executable, FAKE_LS, json.dumps(specs)], stdout=subprocess.PIPE, stdin=DEVNULL,
                          text=True, encoding="utf-8", errors="replace", **kw)
    fakes.append(ls)
    return ls.pid, json.loads(ls.stdout.readline() or "[]")


def kids(pid):
    """The pids under `pid` now (procs.tree: on Windows checked against reused pids)."""
    return procs.tree(compat.process_table(), pid)


def kill_all():
    table = compat.process_table()
    pids = []
    for ls in fakes:
        pids += [ls.pid] + (procs.tree(table, ls.pid) if ls.pid in table else [])
    for pid in pids:
        if compat.WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    for ls in fakes:
        try:
            ls.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


try:
    niced = not compat.WINDOWS and os.getpriority(os.PRIO_PROCESS, 0) >= 10

    # ── 1. the process table ──
    child = subprocess.Popen([sys.executable, SLEEPER, "60"], stdin=DEVNULL)
    table = wait_for(lambda: (lambda t: t if child.pid in t else None)(compat.process_table()), 10) or {}
    p = table.get(child.pid)
    check("process_table() sees a child this test started", p is not None, sorted(table)[:20])
    check("…with this test as its parent", p is not None and p.ppid == os.getpid(), p)
    check("…and its executable's name (python…)", p is not None and "python" in p.name, p)
    check("…in a Toolhelp snapshot on Windows / /proc or ps elsewhere: many processes", len(table) > 5, len(table))

    # ── 2. descendants() and ancestor_named() on a made-up table ──
    t = {1: P(1, 0, "launchd"), 10: P(10, 1, "language_server_macos_arm"), 11: P(11, 10, "zsh"), 12: P(12, 11, "ffmpeg"),
         13: P(13, 10, "sh"), 14: P(14, 13, "python3"), 20: P(20, 1, "zsh"), 30: P(30, 31, "a"), 31: P(31, 30, "b")}
    check("descendants(): children and grandchildren, parents first", compat.descendants(t, 10) == [11, 13, 12, 14],
          compat.descendants(t, 10))
    check("descendants() of a leaf or of an unknown pid: none", compat.descendants(t, 12) == [] and compat.descendants(t, 99) == [])
    check("descendants() comes out of a cycle", compat.descendants(t, 30) == [31], compat.descendants(t, 30))
    check("ancestor_named(): the nearest language_server* above a process", compat.ancestor_named(t, 12, "language_server") == 10)
    check("ancestor_named(): 0 outside it, in a cycle, for itself", (compat.ancestor_named(t, 20, "language_server"),
          compat.ancestor_named(t, 30, "zzz"), compat.ancestor_named(t, 10, "language_server")) == (0, 0, 0))
    B = 4_000_000_001  # pids no process can have (Windows checks a child's creation time against its parent's)
    syn = {B: P(B, 1, "language_server.exe"), B + 2: P(B + 2, B, "ffmpeg.exe"), B + 4: P(B + 4, B + 2, "ffmpeg.exe"),
           B + 6: P(B + 6, B, "powershell.exe"), B + 8: P(B + 8, B + 6, "ffmpeg.exe"), B + 10: P(B + 10, B + 6, "Blender.exe"),
           B + 12: P(B + 12, B, "python.exe"), B + 14: P(B + 14, B + 12, "ffprobe.exe")}
    got = sorted(x.pid for x in procs.heavy(syn, [B], ["ffmpeg", "blender"]))
    check("heavy(): a launcher shim and the program it starts count once, every other one counts",
          got == [B + 2, B + 8, B + 10], got)
    if compat.WINDOWS:
        tt = {child.pid: P(child.pid, 0, "powershell.exe"), os.getpid(): P(os.getpid(), child.pid, "python.exe")}
        check("Windows: a process older than its «parent» is no child of it (the dead parent's pid was reused)",
              procs.tree(tt, child.pid) == [], procs.tree(tt, child.pid))
        tt = {os.getpid(): P(os.getpid(), 0, "python.exe"), child.pid: P(child.pid, os.getpid(), "python.exe")}
        check("…a younger one is", procs.tree(tt, os.getpid()) == [child.pid])
    child.kill()
    child.wait()

    # ── 3. lowering a process: never raising it ──
    if niced:
        print(f"⏭️ lowering: skipped — this test already runs at nice {os.getpriority(os.PRIO_PROCESS, 0)}")
    else:
        spawner = ("import subprocess, sys, time\ntime.sleep(1.5)\n"
                   "q = subprocess.Popen([sys.executable, sys.argv[1], '60'])\nprint(q.pid, flush=True)\ntime.sleep(60)\n")
        kid = subprocess.Popen([sys.executable, "-c", spawner, SLEEPER], stdout=subprocess.PIPE, stdin=DEVNULL,
                               text=True, encoding="utf-8", errors="replace")
        time.sleep(0.3)
        check("a new process runs at normal priority", level_of(kid.pid) == "normal", level_of(kid.pid))
        check("lower() lowers it", procs.lower(kid.pid, "below_normal") is True)
        check("…to below normal (Windows BELOW_NORMAL_PRIORITY_CLASS, elsewhere nice ≥ 10)", level_of(kid.pid) == "below_normal",
              level_of(kid.pid))
        check("…once: the second call changes nothing", procs.lower(kid.pid, "below_normal") is False)
        grand = int(kid.stdout.readline() or 0)
        check("what it starts afterwards inherits the lower priority", grand and level_of(grand) == "below_normal",
              (grand, level_of(grand)))
        check("idle goes lower still", procs.lower(kid.pid, "idle") is True and level_of(kid.pid) == "idle", level_of(kid.pid))
        check("…and below_normal never raises it back", procs.lower(kid.pid, "below_normal") is False
              and level_of(kid.pid) == "idle", level_of(kid.pid))
        check("normal is nothing to lower to", procs.lower(grand, "normal") is False and level_of(grand) == "below_normal")
        mine = level_of(os.getpid())
        check("lower() never touches the process that calls it", procs.lower(os.getpid(), "idle") is False
              and level_of(os.getpid()) == mine)
        check("lower() of a pid nobody has: False, no error", procs.lower(NEVER, "idle") is False)
        check("priority() of a pid nobody has: «?»", procs.priority(NEVER) == "?", procs.priority(NEVER))
        for x in (grand, kid.pid):
            if compat.WINDOWS:
                subprocess.run(["taskkill", "/PID", str(x), "/F"], capture_output=True)
            else:
                try:
                    os.kill(x, 9)
                except OSError:
                    pass
        kid.wait()

    # ── 4. three fake language servers: their trees come up while the rest runs ──
    specs_a = [agent_shell(), hook_shell(), {"args": [sys.executable, SLEEPER, "300"]}]
    if compat.LINUX and os.path.exists("/proc/self/autogroup"):
        specs_a.append(agent_shell(session=True))
    ls_a, (root_a, hook_a, mcp_a, *rest_a) = start_ls(specs_a)
    ls_b, (root_b,) = start_ls([agent_shell()])

    def make_ffmpeg():
        """argv + env of a process named ffmpeg[.exe] that sleeps: a copy of this Python (Windows: with its DLLs), else
        (POSIX) a link to it — the kernel shows the link's name — or a copy of `sleep`; None if none of them works."""
        exe = getattr(sys, "_base_executable", "") or sys.executable
        tries = []
        d = os.path.join(SP, "fakebin")
        os.makedirs(d, exist_ok=True)
        copy = os.path.join(d, "ffmpeg.exe" if compat.WINDOWS else "ffmpeg")
        try:
            shutil.copy2(exe, copy)
            for dll in glob.glob(os.path.join(os.path.dirname(exe), "*.dll")) if compat.WINDOWS else []:
                shutil.copy2(dll, d)
            tries.append(([copy, SLEEPER], {"PYTHONHOME": sys.base_prefix}))
        except OSError as e:
            print(f"   (no copy of Python: {e})")
        if not compat.WINDOWS:
            link = os.path.join(SP, "fakelink", "ffmpeg")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            try:
                os.symlink(exe, link)
                tries.append(([link, SLEEPER], {}))
            except OSError:
                pass
            if shutil.which("sleep"):
                sleep = os.path.join(SP, "fakesleep", "ffmpeg")
                os.makedirs(os.path.dirname(sleep), exist_ok=True)
                shutil.copy2(shutil.which("sleep"), sleep)
                tries.append(([sleep], {}))
        for argv, env in tries:
            q = subprocess.Popen(argv + ["20"], env=dict(os.environ, **env), stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
            time.sleep(1.5)
            name = (compat.process_table().get(q.pid) or P(0, 0, "")).name
            good = q.poll() is None and procs.norm(name) == "ffmpeg"
            q.kill()
            q.wait()
            if good:
                return argv, env
            print(f"   (a fake ffmpeg from {argv[0]} shows as «{name}»: next)")
        return None

    fake = make_ffmpeg()
    if fake:
        ls_c, (ffmpeg_c,) = start_ls([{"args": fake[0] + ["300"], "env": fake[1]}])
    trees_up = wait_for(lambda: kids(root_a) and kids(root_b) and kids(hook_a), 40)
    check("the fake language servers' agent commands came up (shell → Python)", trees_up,
          (kids(root_a), kids(root_b), kids(hook_a)))
    os.environ["AGP_PROCS_LS_PID"] = str(ls_a)
    table = compat.process_table()
    check("language_servers(): AGP_PROCS_LS_PID pins one", procs.language_servers(table) == [ls_a])
    check("…a pid that isn't running: none", procs.language_servers(table, pin=NEVER) == [])
    os.environ["AGP_PROCS_LS_PID"] = ""
    write(os.path.join(HOME, ".gemini", "antigravity", "daemon", "hub_41234.json"),
          json.dumps({"pid": os.getpid(), "httpsPort": 41233, "httpPort": 41234, "lsVersion": "2.35.1", "csrfToken": "t"}))
    check("…a stale daemon file whose pid another process holds now: no language server",
          os.getpid() not in procs.language_servers(compat.process_table()))
    os.environ["AGP_PROCS_LS_PID"] = "0"
    trees = dict(procs.agent_trees(table, ls_a))
    check("agent_trees(): the shell is an agent command, with its Python under it", root_a in trees and trees[root_a],
          trees)
    check("…the hook's shell (cmd.exe on Windows, hooks/<name>.py elsewhere) and an MCP server are not",
          hook_a not in trees and mcp_a not in trees, trees)

    # ── 5. `procs.py lower`: the background helper lowers that tree, and only that ──
    if niced:
        print("⏭️ procs.py lower: skipped (this test runs niced)")
    else:
        before = {pid: level_of(pid) for pid in [root_a, hook_a, mcp_a] + kids(root_a) + kids(hook_a)}
        check("before: every process of the fake language server at normal priority",
              set(before.values()) == {"normal"}, before)
        started = time.time()
        r = subprocess.run([sys.executable, os.path.join(HOOKS, "procs.py"), "lower", "--level", "below_normal",
                            "--for", "3", "--ls", str(ls_a)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
        took = time.time() - started
        check("procs.py lower exits after its window (3 s)", r.returncode == 0 and 2.5 <= took < 20, (r.returncode, took, r.stderr[-300:]))
        lowered = [level_of(pid) for pid in [root_a] + kids(root_a)]
        check("…the agent command and what it started run below normal", set(lowered) == {"below_normal"}, lowered)
        kept = [level_of(pid) for pid in [hook_a, mcp_a] + kids(hook_a)]
        check("…the hook and the MCP server keep normal priority", set(kept) == {"normal"}, kept)
        if rest_a:
            path = f"/proc/{rest_a[0]}/autogroup"
            text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            check("Linux: an agent command with a session of its own gets nice 10 for its autogroup too",
                  "nice 10" in text, text.strip())
        r = subprocess.run([sys.executable, os.path.join(HOOKS, "procs.py"), "lower", "--level", "below_normal",
                            "--for", "1", "--ls", str(ls_a)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
        again = [level_of(pid) for pid in [root_a] + kids(root_a)]
        check("…idempotent: a second helper changes nothing", r.returncode == 0 and set(again) == {"below_normal"}, again)
        r = subprocess.run([sys.executable, os.path.join(HOOKS, "procs.py"), "tree", "--ls", str(ls_a)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        check("procs.py tree shows the agent command and its priority", r.returncode == 0 and str(root_a) in r.stdout
              and "below_normal" in r.stdout.replace("nice 10", "below_normal") and "agent command" in r.stdout,
              r.stdout[-600:] + r.stderr[-300:])
        check("…and what isn't one", "not agent commands" in r.stdout and str(mcp_a) in r.stdout, r.stdout[-600:])

    # ── 6. the guard: agentPriority starts the helper, and still lets the command through ──
    if niced:
        print("⏭️ guard and agentPriority: skipped (this test runs niced)")
    else:
        settings(agentPriority="below_normal")
        env_b = {"AGP_PROCS_LS_PID": str(ls_b), "AGP_PROCS_WINDOW": "8"}
        check("before: the agent command of another language server runs at normal priority", level_of(root_b) == "normal")
        out, took = guard("echo hi", env_b)
        check("guard with agentPriority=below_normal still allows the command", out == {"decision": "allow"}, out)
        check("…its helper lowers the language server's agent commands within seconds",
              wait_for(lambda: level_of(root_b) == "below_normal" and all(level_of(x) == "below_normal" for x in kids(root_b)), 20),
              [level_of(x) for x in [root_b] + kids(root_b)])
        check("…guard.log says the helper was started (limit:agent-priority)", logged("limit:agent-priority") == 1,
              logged("limit:agent-priority"))
        out, _ = guard("echo again", env_b)
        check("a second command only tells the running helper (no second start)", out == {"decision": "allow"}
              and logged("limit:agent-priority") == 1 and procs.helper_running(), (out, logged("limit:agent-priority")))
        talk = os.path.join(HOME, ".claude", "skills", "antigravity", "scripts", "delegate.py")
        stamp = os.path.getmtime(procs.KICK)
        time.sleep(0.05)
        out, _ = guard(f'python3 "{talk}" tell "done"', env_b)
        check("a delegated agent's delegate.py tell: auto_approve, the helper isn't told",
              out.get("decision") == "auto_approve" and os.path.getmtime(procs.KICK) == stamp, out)
        check("the helper exits after its window", wait_for(lambda: not procs.helper_running(), 30), procs.helper_running())

        # what it adds to the guard, measured in this process (no Python start-up in the numbers)
        os.environ.update(env_b)
        import guard as guard_mod
        payload = {"conversationId": "agp-procs-cost", "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}}}
        t0 = time.perf_counter()
        guard_mod.limits(payload, "echo hi")
        start_ms = (time.perf_counter() - t0) * 1000
        time.sleep(1.0)  # past the new helper's own start-up
        kicks = []
        for _ in range(15):
            t0 = time.perf_counter()
            guard_mod.limits(payload, "echo hi")
            kicks.append((time.perf_counter() - t0) * 1000)
        settings()
        normal = []
        for _ in range(15):
            t0 = time.perf_counter()
            guard_mod.limits(payload, "echo hi")
            normal.append((time.perf_counter() - t0) * 1000)
        med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
        print(f"ℹ️ guard cost of agentPriority: {start_ms:.1f} ms when it starts the helper, {med(kicks):.2f} ms when it "
              f"tells the running one; settings at normal: {med(normal):.2f} ms")
        check("telling the running helper costs the guard a few milliseconds at most", med(kicks) < 25, kicks)
        check("the helper started from the guard exits too", wait_for(lambda: not procs.helper_running(), 30))
        os.environ["AGP_PROCS_LS_PID"], os.environ["AGP_PROCS_WINDOW"] = "0", ""

    # ── 7. the guard: heavyMax ──
    if not fake:
        print("⏭️ heavyMax with a running heavy process: skipped — no process named ffmpeg could be made here")
    else:
        os.environ["AGP_PROCS_LS_PID"] = str(ls_c)
        found = procs.heavy(compat.process_table(), procs.language_servers(compat.process_table()), ["ffmpeg", "blender"])
        check("heavy() sees the fake ffmpeg under its language server", [x.pid for x in found] == [ffmpeg_c], found)
        r = subprocess.run([sys.executable, os.path.join(HOOKS, "procs.py"), "heavy", "--ls", str(ls_c)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        check("procs.py heavy lists it with its PID", r.returncode == 0 and f"ffmpeg PID {ffmpeg_c}" in r.stdout,
              r.stdout + r.stderr[-300:])
        os.environ["AGP_PROCS_LS_PID"] = "0"
        env_c = {"AGP_PROCS_LS_PID": str(ls_c), "AGP_HEAVY_WAIT": "1"}
        settings(heavyMax=1)
        out, took = guard("ffmpeg -i a.mp4 b.mp4", env_c)
        reason = out.get("reason", "")
        check("heavyMax=1, one heavy process runs: a new ffmpeg is denied", out.get("decision") == "deny", out)
        check("…after the (shortened) wait", took >= 1.0, took)
        check("…naming what runs (program and PID) and what to do instead", f"ffmpeg (PID {ffmpeg_c})" in reason
              and "command_status" in reason and "в цикле" in reason, reason)
        check("…logged as heavy-limit", logged("heavy-limit") == 1, logged("heavy-limit"))
        out, took = guard("ls -la" if not compat.WINDOWS else "Get-ChildItem", env_c)
        check("…a command that starts no heavy program goes through at once", out == {"decision": "allow"} and took < 30, (out, took))
        out, _ = guard("ffmpeg -version", env_c)
        check("…and so does ffmpeg that only prints information", out == {"decision": "allow"}, out)
        out, _ = guard("ssh build-host ffmpeg -i a.mp4 b.mp4", env_c)
        check("…and ffmpeg on another machine (ssh)", out.get("decision") != "deny", out)
        settings(heavyMax=2)
        out, _ = guard("ffmpeg -i a.mp4 b.mp4", env_c, cid="agp-procs-a")
        check("heavyMax=2: the same command is allowed", out == {"decision": "allow"}, out)
        out, _ = guard("ffmpeg -i c.mp4 d.mp4", env_c, cid="agp-procs-b")
        check("…and its slot stays taken until its process shows up: another agent's ffmpeg waits, then is denied",
              out.get("decision") == "deny" and "место уже отдано" in out.get("reason", ""), out)
        settings(heavyMax=1, bypassMode={"on": True, "at": time.time(), "since": "2026-11-25 10:00",
                                         "conversation": "agp-procs-test", "n": 1})
        out, _ = guard("ffmpeg -i a.mp4 b.mp4", env_c, cid="agp-procs-c")
        check("Bypass Mode: heavyMax still holds (a resource limit, not a permission prompt)", out.get("decision") == "deny", out)
        out, _ = guard("echo hi", env_c, cid="agp-procs-c")
        check("…while other commands get Bypass Mode's auto_approve", out.get("decision") == "auto_approve", out)
        settings()
        out, took = guard("ffmpeg -i a.mp4 b.mp4", env_c)
        check("heavyMax 0 (the default): no limit", out == {"decision": "allow"}, out)

    # ── 8. settings: bad values are refused, or read as the harmless ones ──
    mem = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
    settings()
    for pair in (["agent-priority", "fast"], ["agent-priority", ""], ["heavy-max", "-1"], ["heavy-max", "two"],
                 ["heavy-max", "65"], ["heavy-programs", ",,"], ["heavy-programs", "ff mpeg"], ["heavy-programs", "a;rm"]):
        r = subprocess.run([sys.executable, mem, "config"] + pair, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        check(f"memory.py config {' '.join(pair) or '…'}: refused with a clear message", r.returncode == 2
              and "bad setting" in r.stderr and pair[0] in r.stderr, (r.returncode, r.stderr[-200:]))
    with open(SETTINGS, encoding="utf-8") as f:
        check("…and nothing of it was saved", json.load(f) == {})
    r = subprocess.run([sys.executable, mem, "config", "agent-priority", "below-normal", "heavy-max", "2",
                        "heavy-programs", "ffmpeg,Blender.exe"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    saved = json.load(open(SETTINGS, encoding="utf-8"))
    check("good values are saved (below-normal spelled with a dash too)", r.returncode == 0 and saved ==
          {"agentPriority": "below_normal", "heavyMax": 2, "heavyPrograms": ["ffmpeg", "Blender.exe"]}, (saved, r.stderr[-200:]))
    check("…and read back the way the limits compare names", procs.config() == ("below_normal", 2, ["ffmpeg", "blender"]),
          procs.config())
    r = subprocess.run([sys.executable, mem, "config", "heavy-programs", "default"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("heavy-programs default: the built-in list", "xcodebuild" in procs.config()[2] and "make" not in procs.config()[2]
          and "python" not in procs.config()[2], procs.config())
    sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
    import memory
    settings(agentPriority=5, heavyMax=True, heavyPrograms="ffmpeg")
    s = memory.settings()
    check("settings() drops values of the wrong type (a number, a bool, a string)", (s["agentPriority"], s["heavyMax"],
          s["heavyPrograms"]) == ("normal", 0, memory.DEFAULTS["heavyPrograms"]), s)
    s["heavyPrograms"].append("make")
    check("…and hands out a copy of the default list", "make" not in memory.DEFAULTS["heavyPrograms"])
    check("procs.config(): an unknown level, a negative heavyMax, junk names → normal, 0, only real names",
          procs.config({"agentPriority": "turbo", "heavyMax": -3, "heavyPrograms": [1, "FFmpeg.EXE", "a b", "", "ffmpeg"]})
          == ("normal", 0, ["ffmpeg"]))
    check("procs.config() with nothing set: normal, off", procs.config({})[:2] == ("normal", 0))
    settings(agentPriority="idle")
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "skills", "settings", "scripts", "settings.py"), "show",
                        "--workspace", SP], capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("/settings show lists them", "agent-priority idle" in r.stdout and "heavy-max 0" in r.stdout
          and "heavy-programs ffmpeg,blender" in r.stdout, r.stdout[-700:] + r.stderr[-300:])
    settings()
finally:
    kill_all()
    os.environ["AGP_PROCS_LS_PID"] = "0"
    wait_for(lambda: not procs.helper_running(), 20)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
