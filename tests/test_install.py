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
SKILL = os.path.join(os.path.expanduser("~"), ".claude", "skills", "antigravity")
check("no Claude Code here (~/.claude missing): no skill", not os.path.exists(SKILL) and "claude :" not in r.stdout)
os.makedirs(os.path.join(os.path.expanduser("~"), ".claude"))
r = install()
check("Claude Code here: its skill «antigravity» is installed", r.returncode == 0 and "claude :" in r.stdout
      and all(os.path.isfile(os.path.join(SKILL, *p)) for p in (("SKILL.md",), ("scripts", "delegate.py"), ("scripts", "compat.py"))),
      r.stdout + r.stderr)
text = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
want = "-X utf8 ${CLAUDE_SKILL_DIR}" if os.name == "nt" else "python3 ${CLAUDE_SKILL_DIR}"
check(f"the skill's commands start Python as this computer can ({want.split(' $')[0]})",
      text.startswith("---\nname: antigravity\n") and want in text and (os.name != "nt" or "python3 ${" not in text),
      re.findall(r".{20}\$\{CLAUDE_SKILL_DIR\}", text)[:3])
r = subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "delegate.py"), "list"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=60)
check("the installed script runs (compat.py next to it)", r.returncode == 0 and "No delegated tasks yet." in r.stdout,
      r.stdout + r.stderr)
stale = os.path.join(SKILL, "scripts", "old.py"); open(stale, "w").write("x")
r = install()
check("an update keeps compat.py and drops stale files", r.returncode == 0 and not os.path.exists(stale)
      and os.path.isfile(os.path.join(SKILL, "scripts", "compat.py")), r.stdout[-300:])
r = install("--uninstall")
check("uninstall removes the skill too", r.returncode == 0 and not os.path.exists(SKILL), r.stdout)
os.makedirs(SKILL); open(os.path.join(SKILL, "SKILL.md"), "w").write("---\nname: antigravity\n---\nsomeone else's\n")
r = install()
check("another skill of that name is left alone", r.returncode == 0 and "left as it is" in r.stdout
      and open(os.path.join(SKILL, "SKILL.md")).read().endswith("someone else's\n"), r.stdout[-300:])
r = install("--no-claude")
check("--no-claude skips it", r.returncode == 0 and "claude :" not in r.stdout, r.stdout[-300:])
r = install("--uninstall", "--purge")
check("uninstall removes it", r.returncode == 0 and not os.path.exists(DEST), r.stdout)
check("…but never another skill of that name", os.path.isfile(os.path.join(SKILL, "SKILL.md")))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
