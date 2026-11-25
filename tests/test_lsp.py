"""Code navigation through language servers (skills/lsp): lsp.py against a fake server (tests/probe/fake_lsp.py) in a
project whose path has a space and non-ASCII letters — definition, references with the function each is in (and the
text the server didn't link), callers over two levels, hover, outline, symbol search, a name on the wrong line, a
bare name and an ambiguous one, diagnostics, rename (preview, then --apply), files changed on disk between commands,
a server still listing files (answers wait for it), the warm background server (reused, restarted for newer code,
stopped when idle), --no-daemon, a language with no server; project roots per language; and the hooks: the session
context lines and an edit's new type errors reaching the agent through <edit_check> — only from a running server."""
import json, os, re, socket, subprocess, sys, threading, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
LSP = os.path.join(PLUGIN, "skills", "lsp", "scripts", "lsp.py")
FAKE = os.path.join(HERE, "probe", "fake_lsp.py")
HOOKS = os.path.join(PLUGIN, "hooks")
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp")
FLOG = os.path.join(SP, "fake-methods.log")
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-1500:]}"))
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
ENV = dict(os.environ, FAKE_LSP_LOG=FLOG, AGP_LSP_IDLE="60")
def lsp(*args, cwd=None, env=None, timeout=90):
    r = subprocess.run([sys.executable, LSP, *args], cwd=cwd or PROJ, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env or ENV, timeout=timeout)
    return r.returncode, r.stdout + r.stderr
def states():
    out = []
    for n in sorted(os.listdir(STATE)) if os.path.isdir(STATE) else []:
        if n.endswith(".json"):
            with open(os.path.join(STATE, n), encoding="utf-8") as f:
                out.append(json.load(f))
    return out
def alive(pid):
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# a project (a space and non-ASCII letters in its path) served by the fake server
PROJ = os.path.realpath(os.path.join(SP, "my proj ü"))
write(os.path.join(PROJ, "pyrightconfig.json"), "{}\n")
write(os.path.join(PROJ, "pkg", "pyproject.toml"), "[project]\nname = 'pkg'\n")
write(os.path.join(PROJ, "pkg", "service.py"),
      "class XpService:\n    def __init__(self, session):\n        self.session = session\n\n"
      "    def award(self, user, amount):\n        return amount\n\n\n"
      "def helper():\n    return XpService(None).award(1, 2)\n")
write(os.path.join(PROJ, "pkg", "comments.py"),
      "from pkg.service import XpService\n\n\ndef create_comment(session):\n    # award is in this comment\n"
      "    title = \"😀 привет\"; return XpService(session).award(5, 1)\n\n\n"
      "def delete_comment(session):\n    return create_comment(session)\n")
write(os.path.join(PROJ, "pkg", "bad.py"), "def broken():\n    return oops\n")
write(os.path.join(PROJ, "unresolved", "run.py"), "import service\nservice.XpService(1).award(1, 1)\n")
write(os.path.join(PROJ, "a", "one.py"), "def dup():\n    return 1\n")
write(os.path.join(PROJ, "b", "two.py"), "def dup():\n    return 2\n")
git("init", "-q", cwd=PROJ); git("add", ".", cwd=PROJ); git("commit", "-qm", "base", cwd=PROJ)
os.makedirs(os.path.join(HOME, ".gemini", "antigravity-plus"), exist_ok=True)
with open(os.path.join(HOME, ".gemini", "antigravity-plus", "lsp.json"), "w", encoding="utf-8") as f:
    json.dump({"python": {"name": "fakepy", "command": [sys.executable, FAKE], "ready": "pyright", "startup": 30}}, f)

sys.path.insert(0, os.path.dirname(LSP))
import lsp as L  # noqa: E402

# ---- roots per language
mono = os.path.join(SP, "mono")
for rel in ("pyrightconfig.json", "server/pyproject.toml", "server/app/x.py", "android/settings.gradle.kts",
            "android/app/build.gradle.kts", "android/app/src/A.kt", "ios/Pkg/Package.swift", "ios/Pkg/Sources/B.swift",
            "ios/App/C.swift", "web/package.json", "web/src/d.ts"):
    write(os.path.join(mono, rel), "x\n")
os.makedirs(os.path.join(mono, ".git"))
mono = os.path.realpath(mono)
r = lambda rel, lang: L.find_root(os.path.join(mono, rel), lang)
check("python: the outermost pyrightconfig.json beats a nearer pyproject.toml", r("server/app/x.py", "python") == (mono, "pyrightconfig.json"), r("server/app/x.py", "python"))
check("kotlin: the Gradle settings folder, not the module", r("android/app/src/A.kt", "kotlin")[0] == os.path.join(mono, "android"), r("android/app/src/A.kt", "kotlin"))
check("swift: the nearest Package.swift; without one, the repository", r("ios/Pkg/Sources/B.swift", "swift")[0] == os.path.join(mono, "ios", "Pkg")
      and r("ios/App/C.swift", "swift")[0] == mono, (r("ios/Pkg/Sources/B.swift", "swift"), r("ios/App/C.swift", "swift")))
write(os.path.join(mono, "buildServer.json"), "{}\n")
check("swift: buildServer.json at the top (xcode-build-server) covers the app and its packages",
      r("ios/Pkg/Sources/B.swift", "swift") == (mono, "buildServer.json"), r("ios/Pkg/Sources/B.swift", "swift"))
check("typescript: the nearest package.json", r("web/src/d.ts", "typescript")[0] == os.path.join(mono, "web"))
check("no file named: the Gradle root found below the repository", L.find_root_below(mono, "kotlin") == (os.path.join(mono, "android"), "settings.gradle.kts"), L.find_root_below(mono, "kotlin"))
check("UTF-16 columns: an emoji counts twice", L.lsp_col("😀 award", 2) == 3 and L.py_col("😀 award", 3) == 2)
check("file URIs round-trip a space and non-ASCII letters", L.uri_path(L.path_uri(os.path.join(PROJ, "pkg", "service.py"))) == os.path.join(PROJ, "pkg", "service.py"))

# ---- the commands, through the background server
code, out = lsp("servers")
check("servers: python → the configured server, its root and why", code == 0 and "python (" in out and "fakepy" in out
      and "pyrightconfig.json" in out, out)
code, out = lsp("refs", "pkg/service.py:5", "award")
check("refs: every use with the function it is in, the definition marked", code == 0
      and "references to award: 3 in 2 files" in out and "[definition]" in out and "— in helper" in out
      and "— in create_comment" in out and "XpService.award" not in out.split("\n")[0], out)
check("refs: a column after an emoji and Cyrillic is in characters (51), not UTF-16 units (52)", "  6:51  title" in out, out)
check("refs: the comment line isn't a reference", "is in this comment" not in out, out)
check("refs: code the server didn't link is named, not hidden", "Not linked by the server" in out
      and "unresolved" in out and "run.py (1)" in out, out)
first = states()
check("a background server keeps the language server warm", len(first) == 1 and first[0]["server"] == "fakepy"
      and alive(first[0]["server_pid"]), first)
code, out = lsp("def", "pkg/comments.py:6", "award")
check("def: from a call to the method's line", code == 0 and re.search(r"pkg[/\\]service\.py:5:9\s+def award", out), out)
check("the second command reuses it (same pid)", [s["pid"] for s in states()] == [s["pid"] for s in first], states())
code, out = lsp("hover", "pkg/service.py:9", "helper")
check("hover: the signature, code fences gone", code == 0 and "def helper():" in out and "```" not in out, out)
code, out = lsp("symbols", "pkg/service.py")
check("symbols: classes with their methods, functions, line ranges", code == 0 and "class XpService  1-6" in out
      and "  method award  5-6" in out and "function helper  9-10" in out, out)
code, out = lsp("find", "award")
check("find: the symbol, its container and place", code == 0 and re.search(r"method XpService\.award\s+pkg[/\\]service\.py:5", out), out)
code, out = lsp("refs", "pkg/service.py:7", "award")
check("a name on a nearby line is found, and said", code == 0 and "is on line 5, not 7" in out and "3 in 2 files" in out, out)
code, out = lsp("refs", "helper")
check("a bare name: looked up in the symbol index", code == 0 and "references to helper:" in out and "comments" not in out.split("\n")[0], out)
code, out = lsp("refs", "dup")
check("an ambiguous name: exit 3 with the candidates as <file>:<line>", code == 3 and re.search(r"a[/\\]one\.py:1", out)
      and re.search(r"b[/\\]two\.py:1", out), out)
code, out = lsp("hover", "b", "dup")
check("<folder> <name>: the symbol declared there", code == 0 and re.search(r"dup at b[/\\]two\.py:1", out), out)
code, out = lsp("refs", "no/such.py:3", "x")
check("a missing file is an error, not a symbol lookup", code == 2 and "no such file" in out, out)
code, out = lsp("calls", "pkg/service.py:5", "award", "--depth", "2")
check("calls: callers, and their callers one level down", code == 0 and "← helper" in out and "← create_comment" in out
      and re.search(r"\n    ← delete_comment\s+pkg[/\\]comments\.py:10", out), out)
code, out = lsp("calls", "pkg/comments.py:4", "create_comment", "--out")
check("calls --out: what the function calls, where it is defined", code == 0 and "→ XpService" in out and "→ XpService.award" in out
      or "→ award" in out, out)
code, out = lsp("check", "pkg/bad.py", "pkg/service.py")
check("check: errors with line, column and code; a clean file says so", code == 0 and "2:12  error  \"oops\" is not defined  [reportUndefinedVariable]" in out
      and re.search(r"pkg[/\\]service\.py: no problems", out), out)

# files changed on disk between commands: the server hears about them
write(os.path.join(PROJ, "pkg", "extra.py"), "from pkg.service import XpService\n\ndef more():\n    return XpService(0).award(0, 0)\n")
with open(os.path.join(PROJ, "pkg", "service.py"), "a", encoding="utf-8") as f:
    f.write("\n\ndef again():\n    return XpService(None).award(3, 3)\n")
code, out = lsp("refs", "pkg/service.py:5", "award")
check("a new file and an edited open file count at once", code == 0 and "references to award: 5 in 3 files" in out
      and "— in more" in out and "— in again" in out, out)
with open(FLOG, encoding="utf-8") as f:
    methods = f.read().split()
check("the server was told: didChangeWatchedFiles for the new file, didChange for the open one",
      "workspace/didChangeWatchedFiles" in methods and "textDocument/didChange" in methods, methods[-20:])

# rename: a preview first, then --apply
code, out = lsp("rename", "pkg/service.py:5", "award", "--to", "grant")
with open(os.path.join(PROJ, "pkg", "comments.py"), encoding="utf-8") as f:
    untouched = "award(5, 1)" in f.read()
check("rename: a preview lists the edits and changes nothing", code == 0 and "rename award → grant: 5 edits in 3 files" in out
      and "Preview only" in out and untouched, out)
code, out = lsp("rename", "pkg/service.py:5", "award", "--to", "grant", "--apply")
texts = {n: open(os.path.join(PROJ, "pkg", n), encoding="utf-8").read() for n in ("service.py", "comments.py", "extra.py")}
check("rename --apply: every place rewritten, the emoji line intact", code == 0 and "Applied: 5 edits in 3 files" in out
      and "def grant(self" in texts["service.py"] and "😀 привет\"; return XpService(session).grant(5, 1)" in texts["comments.py"]
      and "award" not in texts["extra.py"], (out, texts))
with open(os.path.join(PROJ, "unresolved", "run.py"), encoding="utf-8") as f:
    check("rename leaves code the server didn't link alone", "award(1, 1)" in f.read())
code, out = lsp("refs", "pkg/service.py:5", "grant")
check("after the rename the server sees the new name", code == 0 and "references to grant: 5 in 3 files" in out, out)
git("checkout", "-q", "--", ".", cwd=PROJ)
os.remove(os.path.join(PROJ, "pkg", "extra.py"))

# hooks: an edit's NEW errors only (the committed file had one already), from the running server
with open(os.path.join(PROJ, "pkg", "bad.py"), "a", encoding="utf-8") as f:
    f.write("\n\ndef worse():\n    return oops + 1\n")
probs = L.edit_problems(os.path.join(PROJ, "pkg", "bad.py"))
check("edit_problems: only the error the edit added (not the one in HEAD)", len(probs) == 1 and probs[0]["line"] == 6
      and probs[0]["tool"] == "fakepy" and "oops" in probs[0]["msg"], probs)
sys.path.insert(0, HOOKS)
import conversation as conv  # noqa: E402
cid = "lsp-hook-test-0001"
payload = {"conversationId": cid, "workspacePaths": [PROJ], "toolCall": {"name": "edit_file", "args": {"Path": os.path.join(PROJ, "pkg", "bad.py")}}}
r = subprocess.run([sys.executable, os.path.join(HOOKS, "check_edit.py")], input=json.dumps(payload), capture_output=True,
                   text=True, encoding="utf-8", env=ENV, timeout=60)
import check_edit  # noqa: E402
msg = check_edit.take_message({"conversationId": cid, "workspacePaths": [PROJ]}) or ""
check("check_edit → <edit_check>: the language server's new error reaches the agent", r.stdout.strip() == "{}"
      and "fakepy 6:12" in msg and "oops" in msg, (r.stdout, r.stderr, msg))
st_file = check_edit.State(cid)
with st_file as data:
    lsp_before = [p for e in data["files"].values() for p in e.get("types") or []]
check("…and is remembered as the language server's own word about the file", any("oops" in p["msg"] for p in lsp_before), lsp_before)
bad = os.path.join(PROJ, "pkg", "bad.py")
check_edit.record(cid, os.path.realpath(bad), [], None)
msg = check_edit.take_message({"conversationId": cid, "workspacePaths": [PROJ]}) or ""
check("the server couldn't say this time (None): no false «fixed», the error still counts", "fixed" not in msg, msg)
check_edit.record(cid, os.path.realpath(bad), [], [])
msg = check_edit.take_message({"conversationId": cid, "workspacePaths": [PROJ]}) or ""
check("…and when it checks and finds nothing: «fixed»", "fixed" in msg, msg)
git("checkout", "-q", "--", ".", cwd=PROJ)
check("a file back to its committed text: nothing the edit added (the old error isn't new)", L.edit_problems(bad) == [],
      L.edit_problems(bad))
check("edits_match: the old name where the server put it — or a refusal", L.edits_match(
    "a = award\n", [{"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}, "newText": "grant"}],
    "award", "grant") and not L.edits_match(
    "a = other\n", [{"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}, "newText": "grant"}],
    "award", "grant"))

# a server still listing the project's files: references wait for it (else only open files would count)
code, out = lsp("stop", "--all")
check("stop --all: the background server and the language server are gone", code == 0 and "fakepy" in out and not states()
      and not alive(first[0]["server_pid"]), (out, states()))
slow = dict(ENV, FAKE_LSP_READY_DELAY="1.5")
code, out = lsp("refs", "pkg/service.py:5", "award", env=slow)
check("a cold start waits until the server has listed the files: complete on the first try", code == 0
      and "references to award: 3 in 2 files" in out, out)
check("edit_problems never starts a server", not L.edit_problems(os.path.join(SP, "mono", "server", "app", "x.py")))

# diagnostics published late: never the previous version's answer
lsp("stop", "--all")
late = dict(ENV, FAKE_LSP_PUBLISH_DELAY="0.6")
code, out = lsp("check", "pkg/bad.py", env=late)
write(os.path.join(PROJ, "pkg", "bad.py"), "def broken():\n    return 1\n")
code2, out2 = lsp("check", "pkg/bad.py", env=late)
check("check after a fix waits for the new version's diagnostics (no stale error)", "1 errors" in out
      and re.search(r"pkg[/\\]bad\.py: no problems", out2), (out, out2))
git("checkout", "-q", "--", ".", cwd=PROJ)

# a server that asks the client something while the client writes a big file: no deadlock
lsp("stop", "--all")
write(os.path.join(PROJ, "pkg", "big.py"), "def first():\n    return 1\n" + "x = 1  # padding\n" * 12000)
code, out = lsp("symbols", "pkg/big.py", env=dict(ENV, FAKE_LSP_FLOOD="1"), timeout=60)
check("a big file opened while the server floods its log and asks a question: answered", code == 0 and "function first" in out, out[-500:])
os.remove(os.path.join(PROJ, "pkg", "big.py"))

# files changed while the server starts reach it with the first command
lsp("stop", "--all")
open(FLOG, "w").close()
starting = subprocess.Popen([sys.executable, LSP, "refs", "pkg/service.py:5", "award"], cwd=PROJ, env=dict(ENV, FAKE_LSP_READY_DELAY="2.5"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
deadline = time.time() + 15
while time.time() < deadline and not (states() or [{}])[0].get("server_pid"):
    time.sleep(0.05)
write(os.path.join(PROJ, "pkg", "late.py"), "from pkg.service import XpService\nXpService(0).award(0, 0)\n")
out = starting.communicate(timeout=90)[0]
with open(FLOG, encoding="utf-8") as f:
    told = "workspace/didChangeWatchedFiles" in f.read()
check("a file created while the server started is announced to it with the first command", starting.returncode == 0 and told, out)
os.remove(os.path.join(PROJ, "pkg", "late.py"))

# a server that keeps indexing: only the first command waits (its «wait»), later ones answer at once with a note
lsp("stop", "--all")
CFG = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp.json")
with open(CFG, encoding="utf-8") as f:
    cfg_saved = f.read()
with open(CFG, "w", encoding="utf-8") as f:
    json.dump({"python": {"name": "fakepy", "command": [sys.executable, FAKE], "wait": 2}}, f)
busy_env = dict(ENV, FAKE_LSP_PROGRESS="1")
t0 = time.time(); code, out = lsp("refs", "pkg/service.py:5", "award", env=busy_env); first_took = time.time() - t0
t0 = time.time(); code2, out2 = lsp("refs", "pkg/service.py:5", "award", env=busy_env); second_took = time.time() - t0
check("still indexing: the first command waits about its «wait», the next answers at once — both say so",
      code == 0 and code2 == 0 and first_took >= 1.8 and second_took < first_took and "still busy: Indexing" in out
      and "still busy: Indexing" in out2, (round(first_took, 2), round(second_took, 2), out2[-300:]))
lsp("stop", "--all")
with open(CFG, "w", encoding="utf-8") as f:
    f.write(cfg_saved)

# publishes for two versions sent back to back can arrive crossed: the older one never replaces the newer one
race_root = os.path.realpath(os.path.join(SP, "race"))
race_file = os.path.join(race_root, "bad.py")
write(race_file, "def a():\n    return oops\n")
L.SERVERS["fake-race"] = {"command": [sys.executable, FAKE], "ready": "pyright", "wait": 5, "startup": 30, "open": True}
os.environ.update(FAKE_LSP_PUBLISH_DELAY="0.02", FAKE_LSP_PUBLISH_JITTER="0.004")
race = L.Session("fake-race", "python", race_root)
race.start()
race.wait_ready(10)
race.open(race_file)
base_text, crossed = "def a():\n    return oops\n", 0
for i in range(20):
    write(race_file, "def a():\n    return oops\n\ndef b():\n    return oops + 1\n" + "\n" * (i % 2))
    race.open(race_file)                                  # the disk's new text (as sync() sends it)…
    got = race.diagnostics(race_file, base_text, timeout=3)  # …then the committed one (as edit_check asks)
    crossed += got is None or len(got) != 1
race.stop()
for var in ("FAKE_LSP_PUBLISH_DELAY", "FAKE_LSP_PUBLISH_JITTER"):
    os.environ.pop(var, None)
check("crossed publishes: the answer is always the requested version's (20 of 20)", crossed == 0, crossed)

# state left by a daemon that died (reboot, kill -9): forgotten — and nothing killed by its old pids
sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
key = L.daemon_key("fakepy", PROJ)
fake_state = {"pid": sleeper.pid, "server_pid": sleeper.pid, "server": "fakepy", "lang": "python", "root": PROJ,
              "transport": "tcp", "address": 9, "token": "x", "version": L.VERSION, "started": time.time(), "used": time.time()}
L.write_state(key, fake_state)
code, out = lsp("stop", "--all")
check("stop: a dead daemon's state is dropped, and the process now holding its old pid is left alone",
      not states() and sleeper.poll() is None, (out, states()))
sleeper.kill()
L.write_state(key, dict(fake_state, pid=111))
L.cleanup(key, 222)
kept = bool(states())
L.cleanup(key, 111)
check("cleanup removes a daemon's files only while they are still that daemon's", kept and not states())

# newer code: the old background server is replaced
lsp("symbols", "pkg/service.py")
st = states()[0]
with open(os.path.join(STATE, [n for n in os.listdir(STATE) if n.endswith(".json")][0]), "w", encoding="utf-8") as f:
    json.dump(dict(st, version="old"), f)
code, out = lsp("symbols", "pkg/service.py")
check("a background server from older code is restarted", code == 0 and states() and states()[0]["pid"] != st["pid"]
      and states()[0]["version"] != "old", (out, states()))

# idle: it stops by itself
lsp("stop", "--all")
idle = dict(ENV, AGP_LSP_IDLE="2.5")
code, out = lsp("symbols", "pkg/service.py", env=idle)
pid = (states() or [{}])[0].get("server_pid")
deadline = time.time() + 30  # the state goes first (a new command starts afresh), the server's shutdown follows
while time.time() < deadline and (states() or (pid and alive(pid))):
    time.sleep(0.2)
check("idle for AGP_LSP_IDLE seconds: it stops with its language server", code == 0 and pid and not states() and not alive(pid), (out, states()))

# concurrent write_state calls from the same process never collide on the temp file
lsp("stop", "--all")
ws_errors = []
def write_burst(offset):
    for n in range(100):
        try:
            L.write_state("race-probe", {"pid": os.getpid(), "server": "fakepy", "used": offset + n})
        except Exception as exc:  # noqa: BLE001
            ws_errors.append(repr(exc))
threads = [threading.Thread(target=write_burst, args=(i * 1000,)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("write_state is safe across concurrent threads in the same process",
      not ws_errors and (L.read_state("race-probe") or {}).get("server") == "fakepy", ws_errors)
L.cleanup("race-probe")

# cold start under concurrent commands preserves server_pid and succeeds
p1 = subprocess.Popen([sys.executable, LSP, "refs", "pkg/service.py:5", "award"], cwd=PROJ, env=ENV,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
p2 = subprocess.Popen([sys.executable, LSP, "symbols", "pkg/service.py"], cwd=PROJ, env=ENV,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
o1 = p1.communicate(timeout=90)[0]
o2 = p2.communicate(timeout=90)[0]
st_now = (states() or [{}])[0]
check("concurrent cold-start commands share one daemon with server_pid intact",
      p1.returncode == 0 and p2.returncode == 0 and "no background server" not in o1 + o2 and st_now.get("server_pid"),
      (p1.returncode, p2.returncode, st_now, o1[-300:], o2[-300:]))

# if the language server dies, ping reports gone and the next command restarts cleanly without waiting
L.kill_tree(int(st_now["server_pid"]))
deadline = time.time() + 10
while time.time() < deadline and L.ping(st_now) is not None:
    time.sleep(0.05)
t0 = time.time(); code, out = lsp("symbols", "pkg/service.py"); recover_took = time.time() - t0
check("dead server: ping stops answering ok and the next command restarts without fallback",
      L.ping(st_now) is None and code == 0 and "class XpService" in out and "no background server" not in out
      and recover_took < 10 and (states() or [{}])[0].get("pid") != st_now.get("pid"),
      (round(recover_took, 2), out, states()))
lsp("stop", "--all")

# if a daemon vanishes right after ensure_daemon's ping or before answering run, run_command cleans up and retries on a fresh daemon
for mode in ("ping", "run"):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(10)
    port = srv.getsockname()[1]
    def mock_drop(srv=srv, mode=mode):
        while True:
            conn, _ = srv.accept()
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            op = json.loads(buf.decode("utf-8")).get("op")
            if op == "ping":
                conn.sendall(b'{"ok": true}\n')
                conn.close()
                if mode == "ping":
                    break
                continue
            conn.close()
            break
        srv.close()
    t_srv = threading.Thread(target=mock_drop, daemon=True)
    t_srv.start()
    L.write_state(key, {"pid": 999999, "server": "fakepy", "lang": "python", "root": PROJ, "transport": "tcp",
                        "address": port, "token": "t", "version": L.VERSION, "started": time.time(), "used": time.time()})
    code, out = lsp("symbols", "pkg/service.py")
    t_srv.join(timeout=5)
    check(f"daemon closing on {mode}: run_command retries on a fresh daemon instead of falling back",
          code == 0 and "class XpService" in out and "no background server" not in out and states(), (code, out, states()))
    lsp("stop", "--all")

# a broken server command that fails on startup fails fast with exit 1 instead of hanging 15 s or printing a traceback
CFG = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp.json")
with open(CFG, encoding="utf-8") as f:
    cfg_saved = f.read()
with open(CFG, "w", encoding="utf-8") as f:
    json.dump({"python": {"name": "fakepy", "command": [sys.executable, "-c", "import sys; sys.exit(1)"]}}, f)
t0 = time.time(); code, out = lsp("symbols", "pkg/service.py"); fail_took = time.time() - t0
with open(CFG, "w", encoding="utf-8") as f:
    f.write(cfg_saved)
check("broken server command: fails fast cleanly with exit 1 and no traceback",
      code == 1 and fail_took < 8 and "Traceback" not in out
      and ("stopped while answering initialize" in out or "didn't start" in out or "can't start" in out),
      (round(fail_took, 2), code, out))
lsp("stop", "--all")

# --no-daemon, a language with no server, the session context
code, out = lsp("hover", "pkg/service.py:9", "helper", "--no-daemon")
check("--no-daemon: answers, and leaves nothing running", code == 0 and "def helper():" in out and not states(), (out, states()))
write(os.path.join(PROJ, "tool.lua"), "local x = 1\n")
code, out = lsp("symbols", "tool.lua", env=dict(ENV, PATH=os.path.dirname(sys.executable)))
check("no server for the language: exit 2 with the install command", code == 2 and "lua-language-server" in out, out)
lines = L.session_lines(PROJ)
check("session context: the language, its server and the exact command", lines and "python → fakepy" in lines[0]
      and L.me() in "\n".join(lines) and "refs <file>:<line> <name>" in "\n".join(lines), lines)
# session start: the block reaches the agent (and subagents); the hint after two rg searches for symbols, once
r = subprocess.run([sys.executable, os.path.join(HOOKS, "session_start.py")], capture_output=True, text=True,
                   encoding="utf-8", input=json.dumps({"conversationId": "lsp-session-0001", "workspacePaths": [PROJ],
                                                       "lastUserInput": "привет"}), env=ENV, timeout=60)
ctx = json.loads(r.stdout or "{}").get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
check("session start: «Code navigation through language servers» with the command", "python → fakepy" in ctx
      and "refs <file>:<line> <name>" in ctx and LSP in ctx, (ctx[-1500:], r.stderr))
tp = os.path.join(SP, "transcript_full.jsonl")
rows = []
def add(kind, content="", calls=None):
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if content:
        row["content"] = content
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
def run(line, out="pkg/service.py:5:    def award(self, user, amount):"):
    add("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line, "WaitMsBeforeAsync": 5000})])
    add("GENERIC", content=f"Command exited with code 0.\n\nCommand output:\n{out}\n")
def pre():
    p = {"conversationId": "lsp-hint-test-0001", "transcriptPath": tp, "workspacePaths": [PROJ]}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(p), env=ENV,
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    return "\n".join(m for x in json.loads(out.stdout or "{}").get("injectSteps", [])
                     for m in [(x.get("systemMessage") or {}).get("systemMessage") or x.get("ephemeralMessage") or ""]
                     if "<code_navigation" in m), out.stderr
add("USER_INPUT", content="<USER_REQUEST>Переименуй award</USER_REQUEST>")
run('rg -n "timed out after" pkg/')
run('rg -n "XpService" pkg/')
hint, err = pre()
check("one rg for a symbol, one for text: no hint yet", hint == "", (hint, err))
run("rg -n 'def award|award\\(' pkg")
hint, err = pre()
check("the second rg for a symbol: the language server's commands, with the servers here", "<code_navigation" in hint
      and "`XpService`" in hint and "python → fakepy" in hint and f"{LSP}\" refs" in hint, (hint, err))
run('rg -n "helper_name" pkg/')
hint, err = pre()
check("…once per conversation", hint == "", hint)

# a custom lsp.json entry whose script path does not exist is skipped by server_command() instead of shadowing a working server
missing_script = os.path.join(SP, "nonexistent_lsp_server.py")
saved_py_lang = dict(L.LANGS["python"])
saved_servers = dict(L.SERVERS)
with open(CFG, encoding="utf-8") as f:
    cfg_saved2 = f.read()
try:
    L.SERVERS["fakepy"] = {"command": [sys.executable, FAKE], "ready": "pyright", "startup": 30, "open": True}
    L.LANGS["python"] = dict(L.LANGS["python"], servers=("fakepy",))
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump({"python": {"name": "missing-script-py", "command": [sys.executable, missing_script]}}, f)
    L._FOUND.clear()
    L.apply_config()
    check("custom lsp.json with missing script path is skipped by server_command() and does not shadow fakepy",
          L.server_command("missing-script-py") is None and L.pick_server("python") == "fakepy",
          (L.server_command("missing-script-py"), L.pick_server("python"), L.LANGS["python"]["servers"]))
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump({"python": {"name": "missing-bin-py", "command": ["/nonexistent/fake-lsp-bin"]}}, f)
    L._FOUND.clear()
    L.apply_config()
    check("custom lsp.json with missing binary is skipped by server_command() and does not shadow fakepy",
          L.server_command("missing-bin-py") is None and L.pick_server("python") == "fakepy",
          (L.server_command("missing-bin-py"), L.pick_server("python"), L.LANGS["python"]["servers"]))
finally:
    with open(CFG, "w", encoding="utf-8") as f:
        f.write(cfg_saved2)
    L.LANGS["python"] = saved_py_lang
    L.SERVERS.clear()
    L.SERVERS.update(saved_servers)
    L._FOUND.clear()

# command_problem(): a valid command of your own is never rejected; only a missing executable, or an absolute (or ~)
# path argument that doesn't exist, is — relative scripts, flags and flag values are left alone
cmd_dir = os.path.join(SP, "cmdcheck")
cmd_js, cmd_jar = os.path.join(cmd_dir, "server.js"), os.path.join(cmd_dir, "x.jar")
for fp in (cmd_js, cmd_jar, os.path.join(HOME, "bin", "xlsp")):
    write(fp, "stub\n")
os.chmod(os.path.join(HOME, "bin", "xlsp"), 0o755)
valid = {
    "relative script + --stdio": [sys.executable, "server.js", "--stdio"],
    "java -jar with a relative jar": [sys.executable, "-jar", "x.jar"],
    "relative executable ./bin/srv": ["./bin/srv", "--stdio"],
    "existing absolute script + --log=…": [sys.executable, cmd_js, "--log=" + os.path.join(cmd_dir, "x.log")],
    "existing absolute jar": [sys.executable, "-jar", cmd_jar],
    "a flag's value that doesn't exist yet": [sys.executable, cmd_js, "--log-file", os.path.join(cmd_dir, "new.log")],
    "a URL argument": [sys.executable, cmd_js, "--server", "https://example.invalid/x"],
    "~/bin/xlsp": ["~/bin/xlsp", "--stdio"],
    "a Windows-style path as a flag value": [sys.executable, cmd_js, "--root", "C:\\\\work\\\\nope"],
}
if os.name != "nt":
    valid["/usr/bin/env python3 -m pylsp"] = ["/usr/bin/env", "python3", "-m", "pylsp"]
for label, cmd in valid.items():
    check(f"command_problem accepts {label}", L.command_problem(cmd) == "", L.command_problem(cmd))
invalid = {
    "an absolute executable that doesn't exist": ["/nonexistent/fake-lsp-bin", "--stdio"],
    "an executable that is not on PATH": ["no-such-lsp-binary-xyz"],
    "a missing absolute script": [sys.executable, os.path.join(cmd_dir, "gone.js")],
    "a missing ~ script": [sys.executable, "~/gone-lsp-script.js"],
    "a missing absolute jar after -jar": [sys.executable, "-jar", os.path.join(cmd_dir, "gone.jar")],
}
for label, cmd in invalid.items():
    check(f"command_problem rejects {label}", L.command_problem(cmd) != "", L.command_problem(cmd))
# …and the user is told why, instead of the config being ignored in silence
saved_py_lang3, saved_servers3 = dict(L.LANGS["python"]), dict(L.SERVERS)
try:
    L.SERVERS["broken-py"] = {"command": ["/nonexistent/fake-lsp-bin", "--stdio"], "install": "your lsp.json"}
    L.LANGS["python"] = dict(L.LANGS["python"], servers=("broken-py",))
    L._FOUND.clear()
    hint = L.install_hint("python")
    check("a refused lsp.json command is named in the install hint, with the reason and the file to fix",
          L.pick_server("python") is None and "broken-py" in hint and "/nonexistent/fake-lsp-bin" in hint
          and "lsp.json" in hint, hint)
finally:
    L.LANGS["python"] = saved_py_lang3
    L.SERVERS.clear()
    L.SERVERS.update(saved_servers3)
    L._FOUND.clear()
    L._REJECTED.clear()

lsp("stop", "--all")
check("nothing left running", not states(), states())
sys.exit(0 if ok else 1)
