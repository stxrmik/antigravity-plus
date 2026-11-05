"""install.py into a throw-away home: copies the plugin, writes this computer's Python command into hooks.json
(UTF-8 mode on Windows), updates in place, uninstalls."""
import json, os, re, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-600:]}"))
def install(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)

r = install()
check("install succeeds", r.returncode == 0 and "Installed Antigravity Plus" in r.stdout, r.stdout + r.stderr)
check("the plugin is copied", os.path.isfile(os.path.join(DEST, "plugin.json")) and
      sorted(os.listdir(os.path.join(DEST, "skills"))) == sorted(os.listdir(os.path.join(PLUGIN, "skills"))))
check("no caches are copied", not any("__pycache__" in dp for dp, _d, _f in os.walk(DEST)))
hooks = open(os.path.join(DEST, "hooks.json"), encoding="utf-8").read()
commands = re.findall(r'"command"\s*:\s*"([^"]+)"', hooks)
want = "-X utf8 hooks/" if os.name == "nt" else "python3 hooks/"
check(f"hook commands run Python as this computer can ({want.strip()})", commands and all(want in c for c in commands), commands)
json.loads(hooks)
stale = os.path.join(DEST, "skills", "removed-skill.txt"); open(stale, "w").write("x")
r = install()
check("an update removes files the source no longer has", r.returncode == 0 and not os.path.exists(stale), r.stdout[-300:])
r = install("--uninstall", "--purge")
check("uninstall removes it", r.returncode == 0 and not os.path.exists(DEST), r.stdout)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
