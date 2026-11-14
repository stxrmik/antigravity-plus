"""install.py into a throw-away home: copies the plugin, writes this computer's Python command into hooks.json
(UTF-8 mode on Windows), updates in place, uninstalls; the modules: exactly the chosen ones are copied, the rules lose
what points at commands left out, the features of missing modules are off, the choice is saved and reused, the menu,
--without, --list, a module waiting for its tool, Claude Code's skill."""
import json, os, re, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-600:]}"))
def install(*args, stdin=None):
    return subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, input=stdin)
sys.path.insert(0, ROOT)
from modules import BY_ID, IDS, MODULES
HOME = os.path.expanduser("~")
CHOICE = os.path.join(HOME, ".gemini", "antigravity-plus", "modules.json")
listing = lambda sub: sorted(n for n in os.listdir(os.path.join(DEST, sub)) if not n.startswith(".")
                             and (sub != "skills" or os.path.isfile(os.path.join(DEST, sub, n, "SKILL.md"))))
choice = lambda: json.load(open(CHOICE, encoding="utf-8"))

r = install("--all")
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
# modules: exactly the chosen ones
r = install("--modules", "core,git", "--lang", "en")
check("--modules core,git: their skills only, no subagents, no side panel", r.returncode == 0
      and listing("skills") == sorted(BY_ID["core"]["skills"] + BY_ID["git"]["skills"]) and listing("agents") == []
      and not os.path.isdir(os.path.join(DEST, "sidecars", "usage")) and listing("rules") == ["AGENTS.md", "GIT.md"],
      r.stdout[-400:] + str(listing("skills")))
check("without the memory module its settings store stays (the hooks read it), but no /memory skill",
      os.path.isfile(os.path.join(DEST, "skills", "memory", "scripts", "memory.py"))
      and not os.path.exists(os.path.join(DEST, "skills", "memory", "SKILL.md")))
rules = open(os.path.join(DEST, "rules", "AGENTS.md"), encoding="utf-8").read()
check("the installed rules keep the rows of installed commands and lose the others", "| `/git` |" in rules
      and "| `/init` |" in rules and "| `/review` |" not in rules and "| `/mobile` |" not in rules and "sidecar://" not in rules
      and "`/deploy`" not in rules, re.findall(r"\| `/[\w-]+`", rules))
check("…while the source rules keep everything", "| `/review` |" in open(os.path.join(PLUGIN, "rules", "AGENTS.md"), encoding="utf-8").read())
c = choice()
check("the choice is saved with the features of the missing modules turned off", c["installed"] == ["core", "git"]
      and c["off"].get("autoMemory") is False and c["off"].get("parallelHints") is False and c["off"].get("recentConversations") == 0
      and "gitChecks" not in c["off"] and c["source"] == ROOT, c)
r2 = subprocess.run([sys.executable, os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py"), "config"],
                    capture_output=True, text=True, encoding="utf-8")
s = json.loads(r2.stdout or "{}")
check("the hooks' settings follow it: memory, parallel and fact checks off, git checks on", s.get("autoMemory") is False
      and s.get("parallelHints") is False and s.get("factChecks") is False and s.get("gitChecks") is True, s)
check("the summary names what is installed and what isn't", "modules: 2 of " in r.stdout and "not installed:" in r.stdout
      and "change: ./install.sh --choose" in r.stdout, r.stdout[-500:])
r = install()
check("an update reuses the saved choice without asking", r.returncode == 0 and listing("rules") == ["AGENTS.md", "GIT.md"]
      and choice()["installed"] == ["core", "git"], r.stdout[-300:])
r = install("--without", "git")
check("--without git: only the core is left", listing("skills") == sorted(BY_ID["core"]["skills"])
      and listing("rules") == ["AGENTS.md"] and choice()["installed"] == ["core"], listing("skills"))
r = install("--choose", "--lang", "en", stdin="2 7\n\n")
check("--choose: the menu shows every module in detail and toggles them by number",
      choice()["installed"] == ["core", "quality", "git"] and all(m["title"]["en"] in r.stdout for m in MODULES)
      and "tokens of context in every request" in r.stdout and BY_ID["git"]["details"]["en"][:40] in r.stdout.replace("\n      ", " "),
      r.stdout[:600])
before = listing("skills")
r = install("--choose", "--lang", "en", stdin="q\n")
check("the menu, then «q»: nothing changes", "Nothing installed" in r.stdout and listing("skills") == before, r.stdout[-200:])
r = install("--choose", "--lang", "en", stdin="1\nall\n\n")
check("the core can't be switched off; «all» picks every module", "The core is required" in r.stdout
      and set(choice()["installed"]) == set(IDS), r.stdout[-300:])
r = install("--list", "--lang", "ru")
check("--list describes every module (in Russian)", r.returncode == 0 and all(m["title"]["ru"] in r.stdout for m in MODULES)
      and "токенов контекста в каждом запросе" in r.stdout, r.stdout[:300])
r = install("--modules", "core,nonsense")
check("an unknown module is refused", r.returncode != 0 and "Unknown module(s): nonsense" in (r.stdout + r.stderr), r.stdout + r.stderr)
# a module that waits for its tool: the bridge chosen without Claude Code, added by the update after it appears
os.rename(os.path.join(HOME, ".claude"), os.path.join(HOME, ".claude-away"))
os.remove(CHOICE)
shutil.rmtree(DEST)
r = install("--lang", "en")
c = choice()
check("a fresh install without a terminal: no menu, the modules whose tools are here", "claude" not in c["installed"]
      and "claude" in c["unavailable"] and "core" in c["installed"] and "git" in c["installed"], c)
os.rename(os.path.join(HOME, ".claude-away"), os.path.join(HOME, ".claude"))
r = install("--lang", "en")
check("Claude Code appears → the next update adds the bridge by itself", "claude" in choice()["installed"]
      and "Modules added" in r.stdout and "claude :" in r.stdout, r.stdout[-500:])
r = install("--uninstall", "--purge")
check("uninstall removes it", r.returncode == 0 and not os.path.exists(DEST), r.stdout)
check("…but never another skill of that name", os.path.isfile(os.path.join(SKILL, "SKILL.md")))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
