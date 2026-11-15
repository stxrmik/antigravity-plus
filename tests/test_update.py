"""Updates: the daily check finds a release (a git clone's remote, or GitHub for an archive install), the agent hears
about it once a day, `update.py run` installs it only when that's safe, `rollback` goes back and forth, autoUpdate
installs in the background. Offline: "GitHub" is a local bare repository and local files."""
import json, os, pathlib, shutil, subprocess, sys, tarfile, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
os.environ.pop("AGP_UPDATE_CHECK", None)          # run_tests turns the check off for every other suite
os.environ["AGP_UPDATE_PANEL"] = "off"            # never restart a running Antigravity's panel from a test
UPDATE = os.path.join(PLUGIN, "skills", "update", "scripts", "update.py")
sys.path.insert(0, os.path.dirname(UPDATE))
import update  # noqa: E402
HOME = os.path.expanduser("~")
DEST = os.path.join(HOME, ".gemini", "config", "plugins", "antigravity-plus")
ok = True
def check(name, got, want):
    global ok
    good = got == want; ok &= good
    print(f"{'✅' if good else '❌'} {name:<70} → {str(got)[:120]!r}" + ("" if good else f" (want {want!r})"))
def git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()
def cli(*args, env=None):
    r = subprocess.run([sys.executable, UPDATE, *args], capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout + r.stderr
def installed():
    return update.version_of(DEST)
def reset_cache():
    if os.path.exists(update.CACHE):
        os.remove(update.CACHE)

STUB = '''import json, os, re, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
home = os.path.expanduser("~")
dest = os.path.join(home, ".gemini", "config", "plugins", "antigravity-plus")
src = os.path.join(HERE, "antigravity-plus")
shutil.rmtree(dest, ignore_errors=True)
shutil.copytree(src, dest)
v = re.search(r'"version"\\s*:\\s*"([^"]+)"', open(os.path.join(src, "plugin.json"), encoding="utf-8").read()).group(1)
data = os.path.join(home, ".gemini", "antigravity-plus")
os.makedirs(data, exist_ok=True)
json.dump({"version": v, "source": HERE, "installed": ["core"]}, open(os.path.join(data, "modules.json"), "w"))
print("Installed Antigravity Plus " + v)
print("  ✅ stub check")
'''
def write_release(folder, version, extra=None):
    os.makedirs(os.path.join(folder, "antigravity-plus", "skills", "core"), exist_ok=True)
    os.makedirs(os.path.join(folder, "antigravity-plus", "sidecars", "usage"), exist_ok=True)
    with open(os.path.join(folder, "install.py"), "w", encoding="utf-8") as f:
        f.write(STUB)
    with open(os.path.join(folder, "antigravity-plus", "plugin.json"), "w", encoding="utf-8") as f:
        f.write('{\n  // a test plugin\n  "name": "antigravity-plus",\n  "version": "%s"\n}\n' % version)
    with open(os.path.join(folder, "antigravity-plus", "skills", "core", "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("core\n")
    for rel, text in (extra or {}).items():
        os.makedirs(os.path.dirname(os.path.join(folder, rel)), exist_ok=True)
        with open(os.path.join(folder, rel), "w", encoding="utf-8") as f:
            f.write(text)

# ── an upstream ("GitHub"), and the user's clone it was installed from ──
work, bare, src = (os.path.join(SP, n) for n in ("upstream", "remote.git", "source"))
os.makedirs(work)
git("init", "-q", cwd=work); git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
write_release(work, "0.21.0", {"antigravity-plus/sidecars/usage/server.mjs": "// v1\n"})
git("add", "-A", cwd=work); git("commit", "-qm", "Antigravity Plus 0.21.0: The base", cwd=work)
git("clone", "-q", "--bare", work, bare, cwd=SP)
git("remote", "add", "origin", bare, cwd=work)
git("clone", "-q", bare, src, cwd=SP)
r = subprocess.run([sys.executable, os.path.join(src, "install.py")], capture_output=True, text=True)
check("the plugin is installed from the clone", installed(), "0.21.0")
check("status before any check", "Not checked" in cli("status")[1], True)

def release(version, subject, extra=None):
    write_release(work, version, extra)
    git("add", "-A", cwd=work); git("commit", "-q", "--allow-empty", "-m", subject, cwd=work); git("push", "-q", "origin", "main", cwd=work)

code, out = cli("check")
check("check: nothing new upstream", (code, "Up to date" in out), (0, True))
release("0.21.0", "Fix a typo in the docs")
check("a commit without a new version isn't a release", update.check()["available"], False)
release("0.22.0", "Antigravity Plus 0.22.0: Shiny updates",
        {"antigravity-plus/sidecars/usage/server.mjs": "// v2\n", "antigravity-plus/skills/newskill/SKILL.md": "new\n"})
found = update.check()
check("check: the new release is found", (found["available"], found["best"]), (True, "0.22.0"))
check("…with its notes from the release commit", found["notes"], ["0.22.0: Shiny updates"])
check("…and the user's clone wasn't changed by the check", update.version_of(os.path.join(src, "antigravity-plus")), "0.21.0")
lines = update.notice()
check("the main agent hears about it", bool(lines) and "Antigravity Plus 0.22.0 is out" in lines[0] and "/update" in lines[0], True)
check("…once a day, not in every conversation", update.notice(), [])
check("subagents and delegated agents never do", update.notice(sub=True), [])

# SessionStart: the notice reaches the context; a stale cache starts the check in the background
ws = os.path.join(SP, "ws"); os.makedirs(ws)
def session_start(env=None):
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "session_start.py")], capture_output=True, text=True,
                       encoding="utf-8", input=json.dumps({"conversationId": "agp-update-test-0001", "workspacePaths": [ws],
                                                           "lastUserInput": "Почини баг"}),
                       env=dict(os.environ, **(env or {})))
    return r.stdout
update.save_cache(told={})
check("SessionStart tells the agent about the release", "Antigravity Plus 0.22.0 is out" in session_start(), True)
update.save_cache(checked=0, attempt=0)
session_start({"AGP_UPDATE_CHECK": "off"})
check("update-check off: SessionStart starts no check", update.load_cache().get("attempt"), 0)
t0 = time.time()
session_start()
deadline = time.time() + 30
while time.time() < deadline and float(update.load_cache().get("checked") or 0) < t0:
    time.sleep(0.3)
check("a stale cache: SessionStart starts the check in the background", float(update.load_cache().get("checked") or 0) >= t0, True)

# run: only when it's safe
with open(os.path.join(src, "install.py"), "a", encoding="utf-8") as f:
    f.write("# local edit\n")
code, out = cli("run")
check("run with uncommitted changes in the clone: stops", (code, "uncommitted changes" in out), (2, True))
check("…and changes nothing", (installed(), update.version_of(os.path.join(src, "antigravity-plus"))), ("0.21.0", "0.21.0"))
git("checkout", "--", "install.py", cwd=src)
git("checkout", "-qb", "feature", cwd=src)
code, out = cli("run")
check("run on another branch: stops", (code, "not main" in out), (2, True))
git("checkout", "-q", "main", cwd=src)
code, out = cli("run")
check("run: updates", code, 0)
check("…from 0.21.0 to 0.22.0 by a fast-forward", "0.21.0 → 0.22.0" in out and "fast-forward" in out, True)
check("…with what's new", "Shiny updates" in out, True)
check("…and says a new command needs a restart", "restart Antigravity to see /newskill" in out, True)
check("…and that the panel's server changed", "usage panel's server changed" in out, True)
check("the clone is at origin/main now", git("rev-parse", "HEAD", cwd=src) == git("rev-parse", "origin/main", cwd=src), True)
check("the installed plugin is 0.22.0", installed(), "0.22.0")
check("the previous version is kept", update.version_of(os.path.join(update.PREVIOUS, "antigravity-plus")), "0.21.0")
check("after the update nothing is announced", (update.status()["available"], update.notice()), (False, []))
code, out = cli("run")
check("run again: nothing to install", (code, "is the latest release" in out), (0, True))

code, out = cli("rollback")
check("rollback: back to 0.21.0", (code, installed()), (0, "0.21.0"))
check("…the rolled-back release isn't offered again", (update.status()["available"], update.notice()), (False, []))
code, out = cli("rollback")
check("rollback again: forward to 0.22.0", (code, installed()), (0, "0.22.0"))

# commits of the user's own on main and a new release upstream: no merge on their behalf
release("0.23.0", "Antigravity Plus 0.23.0: More")
with open(os.path.join(src, "mine.txt"), "w") as f:
    f.write("mine\n")
git("add", "mine.txt", cwd=src); git("commit", "-qm", "my own change", cwd=src)
code, out = cli("run")
check("run with commits of its own and new ones upstream: stops", (code, "commit(s) of its own" in out), (2, True))
check("…installed version unchanged", installed(), "0.22.0")
git("reset", "-q", "--hard", "origin/main", cwd=src)

# autoUpdate: the background check installs a safe release itself, and the agent hears it once
settings = os.path.join(HOME, ".gemini", "antigravity-plus", "settings.json")
with open(settings, "w") as f:
    json.dump({"autoUpdate": True}, f)
update.save_cache(checked=0, attempt=0, told={})
code, _ = cli("background")
check("autoUpdate on: the background check installed 0.23.0", (code, installed()), (0, "0.23.0"))
lines = update.notice()
check("…and the agent hears it updated itself", bool(lines) and "updated itself from 0.22.0 to 0.23.0" in lines[0], True)
check("…once", update.notice(), [])
with open(settings, "w") as f:
    json.dump({"autoUpdate": False}, f)

# doctor, /settings and the usage panel show what's known
release("0.24.0", "Antigravity Plus 0.24.0: Panel badge")
update.check()
sys.path.insert(0, os.path.join(PLUGIN, "skills", "doctor", "scripts"))
import doctor  # noqa: E402
check("/doctor: a new release is shown", "доступна Antigravity Plus 0.24.0" in doctor.update_line(), True)
r = subprocess.run([sys.executable, os.path.join(PLUGIN, "skills", "settings", "scripts", "settings.py"), "show", "--workspace", ws],
                   capture_output=True, text=True, encoding="utf-8")
check("/settings: the version line", "Version: 0.23.0 — 0.24.0 is out: /update" in r.stdout, True)
r = subprocess.run([sys.executable, os.path.join(PLUGIN, "skills", "usage", "scripts", "usage.py"), "--recent", "3"],
                   capture_output=True, text=True, encoding="utf-8")
try:
    up = json.loads(r.stdout).get("update") or {}
except ValueError:
    up = {"stdout": r.stdout[:200], "stderr": r.stderr[:200]}
check("the usage panel's data carries it", (up.get("available"), up.get("latest")), (True, "0.24.0"))

# installed from an archive (no clone): GitHub's plugin.json, commits and archive — local files here
gh = os.path.join(SP, "github"); os.makedirs(gh)
pkg = os.path.join(gh, "pkg"); write_release(os.path.join(pkg, "antigravity-plus-main"), "0.25.0")
with tarfile.open(os.path.join(gh, "main.tar.gz"), "w:gz") as tf:
    tf.add(os.path.join(pkg, "antigravity-plus-main"), arcname="antigravity-plus-main")
shutil.copy(os.path.join(pkg, "antigravity-plus-main", "antigravity-plus", "plugin.json"), os.path.join(gh, "plugin.json"))
with open(os.path.join(gh, "commits.json"), "w") as f:
    json.dump([{"commit": {"message": "Antigravity Plus 0.25.0: From the archive\n\nDetails."}},
               {"commit": {"message": "Antigravity Plus 0.20.0: Old"}}], f)
uri = lambda name: pathlib.Path(os.path.join(gh, name)).as_uri()  # noqa: E731
net = {"AGP_UPDATE_RAW": uri("plugin.json"), "AGP_UPDATE_ARCHIVE": uri("main.tar.gz"), "AGP_UPDATE_COMMITS": uri("commits.json")}
modules = os.path.join(HOME, ".gemini", "antigravity-plus", "modules.json")
rec = json.load(open(modules)); rec["source"] = ""; json.dump(rec, open(modules, "w"))
code, out = cli("check", env=net)
check("no clone: the release is found on GitHub", ("0.25.0" in out, "From the archive" in out, "Old" in out), (True, True, False))
code, out = cli("run", env=net)
check("no clone: run downloads and installs it", (code, installed()), (0, "0.25.0"))
check("…from its own copy, the source from now on", json.load(open(modules)).get("source"), update.MANAGED)
code, out = cli("run", env=net)
check("…and run again finds nothing newer", (code, "is the latest release" in out), (0, True))

evil = os.path.join(SP, "evil"); os.makedirs(evil)
with tarfile.open(os.path.join(evil, "main.tar.gz"), "w:gz") as tf:
    data = b"pwned\n"
    info = tarfile.TarInfo("../outside.txt"); info.size = len(data)
    import io
    tf.addfile(info, io.BytesIO(data))
with open(os.path.join(evil, "plugin.json"), "w") as f:
    f.write('{"version": "9.9.9"}')
rec = json.load(open(modules)); rec["source"] = ""; json.dump(rec, open(modules, "w"))
code, out = cli("run", env={"AGP_UPDATE_RAW": pathlib.Path(os.path.join(evil, "plugin.json")).as_uri(),
                            "AGP_UPDATE_ARCHIVE": pathlib.Path(os.path.join(evil, "main.tar.gz")).as_uri()})
check("an archive with a path outside its folder is refused", (code, "outside its folder" in out), (2, True))
check("…nothing written outside, nothing installed", (os.path.exists(os.path.join(HOME, ".gemini", "antigravity-plus", "outside.txt")), installed()),
      (False, "0.25.0"))

r = subprocess.run([sys.executable, os.path.join(os.path.dirname(PLUGIN), "install.py"), "--help"], capture_output=True, text=True)
check("./install.sh --update exists", "--update" in r.stdout, True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
