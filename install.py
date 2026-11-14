#!/usr/bin/env python3
"""Antigravity Plus installer for macOS, Linux and Windows.

  python3 install.py                       install or update into ~/.gemini/config/plugins/antigravity-plus
  python3 install.py --choose              pick the modules again (a menu with a detailed description of each)
  python3 install.py --list                describe every module and exit
  python3 install.py --modules core,git    exactly these modules (core is always in)
  python3 install.py --without mobile,web  the current choice minus these
  python3 install.py --all                 every module
  python3 install.py --no-claude           … without Claude Code's `antigravity` skill (= --without claude)
  python3 install.py --uninstall          remove the plugin (memory and settings are kept)
  python3 install.py --uninstall --purge  also remove its memory and settings (~/.claude memory is never touched)

Modules (modules.py): the first install in a terminal shows a menu; the choice is saved in
~/.gemini/antigravity-plus/modules.json and reused by every update, so `git pull && ./install.sh` asks nothing.
What isn't chosen isn't copied — its skills, subagents, rules and side panel never reach Antigravity's context —
and the hook features it owns stay off (settings forced in modules.json). No menu where nobody can answer: an agent's
terminal (TERM=dumb in Antigravity, CLAUDECODE in Claude Code), CI, or no terminal at all.

When Claude Code is on this computer (~/.claude exists), the module `claude` installs its side of the bridge: the
skill `antigravity` in ~/.claude/skills/antigravity, with which Claude Code hands sub-tasks to Antigravity agents.

On Windows run `install.cmd` (or `py -3 install.py`). Antigravity runs hook commands with `cmd /c` there, where
`python3` often doesn't exist, so the installer writes the Python command that works on this computer into the
installed hooks.json (and into the Claude Code skill). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from typing import Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from modules import BY_ID, IDS, MODULES  # noqa: E402

SRC = os.path.join(HERE, "antigravity-plus")
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
DATA = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus")  # memory, settings, state, logs
CHOICE = os.path.join(DATA, "modules.json")                                   # the modules chosen (read by the hooks)
CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")                    # Claude Code's own folder
CLAUDE_SRC = os.path.join(HERE, "claude-code", "skills", "antigravity")
CLAUDE_DEST = os.path.join(CLAUDE, "skills", "antigravity")
SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}
WINDOWS = os.name == "nt"
WIDTH = 100

TEXT = {
    "ru": {
        "head": "Antigravity Plus — какие модули установить",
        "intro": "Модули добавляют агенту команды, субагентов и правила. Невыбранное не ставится и не занимает "
                 "контекст. Состав можно поменять когда угодно: ./install.sh --choose",
        "required": "обязательно",
        "tokens": "≈{n} токенов контекста в каждом запросе",
        "outside": "вне контекста Antigravity",
        "hints": "постоянного текста нет — только подсказки, когда нужны",
        "missing": "не найдено: {what}",
        "prompt": "Номера через пробел переключают модули (например «7 10»), «все» — выбрать всё, «минимум» — только "
                  "основу, Enter — установить отмеченные, «q» — выйти без установки.",
        "chosen": "Выбрано {n} из {total}.",
        "bad": "Не понял «{x}»: нужны номера от 1 до {total}, «все», «минимум», Enter или «q».",
        "cancel": "Ничего не установлено.",
        "core_fixed": "Основа обязательна — без неё остальное не работает.",
        "no_claude": "Claude Code на этом компьютере не найден (нет ~/.claude) — модуль пропущен.",
        "new": "Добавлены модули (новые в этой версии или появился нужный инструмент): {names}. Убрать: ./install.sh --choose",
    },
    "en": {
        "head": "Antigravity Plus — which modules to install",
        "intro": "Modules give the agent commands, subagents and rules. What you don't choose isn't installed and takes "
                 "no context. Change it any time: ./install.sh --choose",
        "required": "required",
        "tokens": "≈{n} tokens of context in every request",
        "outside": "outside Antigravity's context",
        "hints": "no standing text — only hints when they apply",
        "missing": "not found: {what}",
        "prompt": "Numbers separated by spaces toggle modules (e.g. «7 10»), «all» picks everything, «min» only the "
                  "core, Enter installs the checked ones, «q» quits without installing.",
        "chosen": "{n} of {total} chosen.",
        "bad": "Didn't get «{x}»: numbers 1 to {total}, «all», «min», Enter or «q».",
        "cancel": "Nothing installed.",
        "core_fixed": "The core is required — everything else builds on it.",
        "no_claude": "Claude Code isn't on this computer (no ~/.claude) — module skipped.",
        "new": "Modules added (new in this version, or the tool they need has appeared): {names}. Remove them with ./install.sh --choose",
    },
}


def skipped(name: str) -> bool:
    return name in SKIP or name.endswith((".pyc", ".pyo"))


def sync(src: str, dest: str, extra: Optional[Dict[str, str]] = None) -> int:
    """Make `dest` a copy of `src` (like rsync --delete) without removing the folder itself, so a running
    Antigravity doesn't lose it mid-way. `extra` maps more files into it ({path in dest: source file}).
    Returns how many files changed."""
    changed = 0
    extra = extra or {}
    os.makedirs(dest, exist_ok=True)
    pairs = []
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if not skipped(d)]
        target = os.path.join(dest, os.path.relpath(dp, src))
        os.makedirs(target, exist_ok=True)
        pairs += [(os.path.join(dp, fn), os.path.join(target, fn)) for fn in fns if not skipped(fn)]
    pairs += [(s, os.path.join(dest, rel)) for rel, s in extra.items()]
    for s, d in pairs:
        if not os.path.exists(d) or not filecmp.cmp(s, d, shallow=False):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            changed += 1
    kept = {os.path.normcase(os.path.join(dest, rel)) for rel in extra}
    for dp, dns, fns in os.walk(dest, topdown=False):  # drop what the source no longer has
        rel = os.path.relpath(dp, dest)
        source_dir = os.path.join(src, rel)
        for fn in fns:
            if os.path.normcase(os.path.join(dp, fn)) in kept:
                continue
            if skipped(fn) or not os.path.exists(os.path.join(source_dir, fn)):
                os.remove(os.path.join(dp, fn))
                changed += 1
        if dp != dest and (skipped(os.path.basename(dp)) or not os.path.isdir(source_dir)) and not os.listdir(dp):
            os.rmdir(dp)
    return changed


def python_command() -> Optional[str]:
    """A Python 3.8+ command that works from `cmd /c` / `sh -c` without quoting: no spaces, no path. On Windows it
    runs in UTF-8 mode (-X utf8): Antigravity reads the hooks' answers as UTF-8, and files and pipes default to
    the ANSI code page there."""
    candidates = ["py -3 -X utf8", "python -X utf8", "python3 -X utf8"] if WINDOWS else ["python3", "python"]
    for cand in candidates:
        try:
            r = subprocess.run(cand.split() + ["-c", "import sys; print(sys.version_info >= (3, 8))"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip() == "True":
            return cand
    return None


def write_hooks(dest: str, python: str) -> None:
    path = os.path.join(dest, "hooks.json")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'"python3 (hooks/\w+\.py)"', lambda m: json.dumps(f"{python} {m.group(1)}"), text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ─────────────────────────────── modules ───────────────────────────────

def language(arg: Optional[str]) -> str:
    if arg in ("ru", "en"):
        return arg
    names = [os.environ.get(v, "") for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        names.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    return "ru" if any(n.lower().startswith(("ru", "russian")) for n in names) else "en"


def missing(m: dict) -> List[str]:
    """What a module needs and this computer lacks (hard needs; "gh?" is only a note)."""
    out = []
    for need in m.get("needs") or []:
        if need == "git" and not shutil.which("git"):
            out.append("git")
        elif need == "browser" and not any(os.path.isfile(p) or shutil.which(p) for p in browsers()):
            out.append("Chrome / Chromium / Edge / Brave")
        elif need == "mobile" and not (shutil.which("xcrun") or shutil.which("adb") or android_sdk()):
            out.append("Xcode / Android SDK")
        elif need == "claude" and not os.path.isdir(CLAUDE):
            out.append("Claude Code (~/.claude)")
    return out


def browsers() -> List[str]:
    try:
        sys.path.insert(0, os.path.join(SRC, "hooks"))
        import compat
        return compat.browsers()
    except Exception:
        return []


def android_sdk() -> bool:
    roots = [os.environ.get("ANDROID_HOME", ""), os.environ.get("ANDROID_SDK_ROOT", ""),
             os.path.join(os.path.expanduser("~"), "Library", "Android", "sdk"),
             os.path.join(os.path.expanduser("~"), "Android", "Sdk"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")]
    return any(r and os.path.isdir(os.path.join(r, "platform-tools")) for r in roots)


def description_of(path: str) -> str:
    """The `description` of a SKILL.md or an agent file (plain or folded YAML)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(20000)
    except OSError:
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    lines, out, grab = m.group(1).splitlines(), [], False
    for line in lines:
        if grab:
            if line.startswith((" ", "\t")):
                out.append(line.strip())
                continue
            break
        d = re.match(r"^description:\s*(.*)$", line)
        if d:
            value = d.group(1).strip()
            if value in (">", ">-", "|", "|-", ""):
                grab = True
            else:
                out.append(value)
                break
    return " ".join(out)


def tokens(m: dict) -> int:
    """About how many tokens the module puts into every request: its skills' and agents' descriptions (the agent
    sees that list each turn) and its always-on rules."""
    chars = 0
    for s in m.get("skills") or []:
        chars += len(description_of(os.path.join(SRC, "skills", s, "SKILL.md"))) + 120  # name and path in the list
    for a in m.get("agents") or []:
        chars += len(description_of(os.path.join(SRC, "agents", a + ".md"))) + 60
    for r in m.get("rules") or []:
        try:
            chars += os.path.getsize(os.path.join(SRC, "rules", r))
        except OSError:
            pass
    return int(round(chars / 4 / 50.0)) * 50


def default_choice() -> Set[str]:
    return {m["id"] for m in MODULES if m.get("required") or not missing(m)}


def saved() -> Optional[dict]:
    try:
        with open(CHOICE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("installed"), list) else None
    except (OSError, ValueError):
        return None


def parse_ids(value: str) -> List[str]:
    ids = [x.strip().lower() for x in re.split(r"[\s,]+", value or "") if x.strip()]
    bad = [x for x in ids if x not in BY_ID]
    if bad:
        raise SystemExit(f"Unknown module(s): {', '.join(bad)}. Modules: {', '.join(IDS)} (install.py --list).")
    return ids


def interactive_terminal() -> bool:
    """A person at a terminal: not an agent's terminal (Antigravity sets TERM=dumb, Claude Code CLAUDECODE), not CI."""
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return (tty and os.environ.get("TERM", "") != "dumb" and not os.environ.get("CLAUDECODE")
            and not os.environ.get("CI"))


def wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=WIDTH, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False)


def title_line(i: int, m: dict, on: bool, lang: str) -> str:
    t = TEXT[lang]
    bits = []
    if m.get("required"):
        bits.append(t["required"])
    cost = tokens(m)
    bits.append(t["outside"] if m.get("claude") else t["hints"] if not cost else
                t["tokens"].format(n=f"{cost:,}".replace(",", " ")))
    lack = missing(m)
    if lack:
        bits.append("⚠ " + t["missing"].format(what=", ".join(lack)))
    return f"{i:>2}. [{'x' if on else ' '}] {m['title'][lang]} — {m['summary'][lang]}\n      " + " · ".join(bits)


def describe_all(selected: Set[str], lang: str) -> str:
    t = TEXT[lang]
    out = [t["head"], "", wrap(t["intro"], ""), ""]
    for i, m in enumerate(MODULES, 1):
        out.append(title_line(i, m, m["id"] in selected, lang))
        out.append(wrap(m["details"][lang], "      "))
        out.append("")
    return "\n".join(out)


def menu(selected: Set[str], lang: str, ask=input) -> Optional[Tuple[Set[str], Set[str]]]:
    """The interactive choice: toggles by number until Enter. (chosen, touched by the user), or None = quit."""
    t = TEXT[lang]
    chosen = set(selected)
    touched: Set[str] = set()
    print(describe_all(chosen, lang))
    while True:
        print(wrap(t["prompt"], ""))
        try:
            answer = ask("> ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("q", "quit", "exit", "выход", "й"):
            return None
        if answer == "":
            return chosen, touched
        if answer in ("all", "все", "всё"):
            chosen = {m["id"] for m in MODULES if not (m.get("claude") and missing(m))}
            touched = set(IDS)
        elif answer in ("min", "минимум", "core", "основа"):
            chosen, touched = {"core"}, set(IDS)
        else:
            for x in answer.replace(",", " ").split():
                if not x.isdigit() or not 1 <= int(x) <= len(MODULES):
                    print(t["bad"].format(x=x, total=len(MODULES)))
                    continue
                m = MODULES[int(x) - 1]
                if m.get("required"):
                    print(t["core_fixed"])
                elif m.get("claude") and missing(m):
                    print(t["no_claude"])
                else:
                    chosen ^= {m["id"]}
                    touched.add(m["id"])
        print()
        for i, m in enumerate(MODULES, 1):
            print(f"{i:>2}. [{'x' if m['id'] in chosen else ' '}] {m['title'][lang]}")
        print(t["chosen"].format(n=len(chosen), total=len(MODULES)) + "\n")


def choose(a, lang: str) -> Optional[Tuple[Set[str], List[str], Set[str]]]:
    """The modules to install, the ids added to a saved choice (new in this version, or their tool has appeared),
    and the modules left out only because a tool they need is missing (added later, once it is there)."""
    prev = saved()
    lacking = {m["id"] for m in MODULES if missing(m)}
    if a.modules:
        return finish_choice(set(parse_ids(a.modules)), a), [], set()
    if a.all:
        return finish_choice(set(IDS), a), [], set()
    if prev and not a.choose:
        known = set(prev.get("known") or prev["installed"])
        waiting = {x for x in prev.get("unavailable") or [] if x in BY_ID}
        chosen = {x for x in prev["installed"] if x in BY_ID}
        new = [m["id"] for m in MODULES if (m["id"] not in known or m["id"] in waiting) and m["id"] not in lacking]
        chosen |= set(new)
        later = {x for x in IDS if (x not in known or x in waiting) and x in lacking}
        return finish_choice(chosen, a), new, later
    if os.path.isdir(DEST) and not a.choose:
        return finish_choice(set(IDS), a), [], set()  # installed before modules existed: keep everything it had
    chosen = {x for x in prev["installed"] if x in BY_ID} if prev else default_choice()
    touched: Set[str] = set()
    if a.choose or interactive_terminal():
        picked = menu(chosen, lang)
        if picked is None:
            return None
        chosen, touched = picked
    return finish_choice(chosen, a), [], {x for x in lacking - chosen - touched}


def finish_choice(chosen: Set[str], a) -> Set[str]:
    chosen |= {m["id"] for m in MODULES if m.get("required")}
    for x in parse_ids(a.without or ""):
        if not BY_ID[x].get("required"):
            chosen.discard(x)
    if a.no_claude:
        chosen.discard("claude")
    return chosen


def excluded(chosen: Set[str]) -> Set[str]:
    """Paths (relative to the plugin, forward slashes) of what the modules left out own."""
    out = set()
    for m in MODULES:
        if m["id"] in chosen:
            continue
        keep = m.get("keep") or []
        for s in m.get("skills") or []:
            # a skill whose scripts the hooks need loses only its SKILL.md: Antigravity no longer lists it
            out.add(f"skills/{s}/SKILL.md" if any(k.startswith(f"skills/{s}/") for k in keep) else f"skills/{s}")
        out |= {f"agents/{x}.md" for x in m.get("agents") or []}
        out |= {f"rules/{r}" for r in m.get("rules") or []}
        out |= {f"sidecars/{s}" for s in m.get("sidecars") or []}
    return out


def filter_rules(text: str, skills: Set[str], sidecars: Set[str]) -> str:
    """A rule file without what points at commands that aren't installed: rows of the commands table, links to a
    side panel that isn't there, lines marked `<!-- needs:<skill> -->`, and such commands in comma lists."""
    out = []
    for line in text.splitlines():
        need = re.search(r"\s*<!--\s*needs:([\w-]+)\s*-->", line)
        if need:
            if need.group(1) not in skills:
                continue
            line = line[:need.start()] + line[need.end():]
        row = re.match(r"^\|\s*`/([\w-]+)`", line)
        if row and row.group(1) not in skills:
            continue
        panel = re.search(r"sidecar://antigravity-plus/([\w-]+)/", line)
        if panel and panel.group(1) not in sidecars:
            continue
        if len(re.findall(r"`/[\w-]+`", line)) >= 3:  # a list of commands: drop the missing ones
            line = re.sub(r"`/([\w-]+)`(, )?", lambda m: m.group(0) if m.group(1) in skills else "", line)
            line = re.sub(r",\s*(,|\s(or|and)\b)", r" \2", line).replace("  ", " ").replace(" ,", ",")
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def stage(chosen: Set[str], python: str) -> str:
    """A temporary copy of the plugin with just the chosen modules, ready to sync into place."""
    root = tempfile.mkdtemp(prefix="agp-install-")
    dest = os.path.join(root, "antigravity-plus")
    gone = excluded(chosen)

    def ignore(folder: str, names: List[str]) -> List[str]:
        rel = os.path.relpath(folder, SRC).replace(os.sep, "/")
        rel = "" if rel == "." else rel + "/"
        return [n for n in names if skipped(n) or rel + n in gone]

    shutil.copytree(SRC, dest, ignore=ignore)
    skills = set(os.listdir(os.path.join(dest, "skills")))
    sidecars = set(os.listdir(os.path.join(dest, "sidecars"))) if os.path.isdir(os.path.join(dest, "sidecars")) else set()
    rules = os.path.join(dest, "rules")
    for name in os.listdir(rules):
        path = os.path.join(rules, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new = filter_rules(text, skills, sidecars)
        if new != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
    if python != "python3":
        write_hooks(dest, python)
    return root


def save_choice(chosen: Set[str], version: str, later: Set[str]) -> None:
    off: Dict[str, object] = {}
    for m in MODULES:
        if m["id"] not in chosen:
            off.update(m.get("off") or {})
    record = {"version": version, "at": dt.datetime.now().isoformat(timespec="seconds"),
              "installed": [m["id"] for m in MODULES if m["id"] in chosen], "known": IDS,
              "titles": {m["id"]: m["title"] for m in MODULES}, "off": off, "source": HERE,
              "unavailable": [x for x in IDS if x in later and x not in chosen]}
    os.makedirs(DATA, exist_ok=True)
    tmp = CHOICE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHOICE)


# ─────────────────────────────── Claude Code ───────────────────────────────

def claude_skill_ours() -> bool:
    """~/.claude/skills/antigravity is free, or it is the one this installer put there (never touch another
    skill of the same name)."""
    return not os.path.exists(CLAUDE_DEST) or os.path.isfile(os.path.join(CLAUDE_DEST, "scripts", "delegate.py"))


def install_claude_skill(python: str) -> Optional[str]:
    """Claude Code's side of the bridge: the skill `antigravity`, with compat.py next to its script. Returns a line
    for the summary, or None when Claude Code isn't on this computer."""
    if not os.path.isdir(CLAUDE) or not os.path.isfile(os.path.join(CLAUDE_SRC, "SKILL.md")):
        return None
    if not claude_skill_ours():
        return f"  claude : {CLAUDE_DEST} is another skill named «antigravity» — left as it is"
    changed = sync(CLAUDE_SRC, CLAUDE_DEST, {os.path.join("scripts", "compat.py"): os.path.join(SRC, "hooks", "compat.py")})
    skill = os.path.join(CLAUDE_DEST, "SKILL.md")
    if python != "python3":  # Windows: the skill's commands start Python the way this computer can
        with open(skill, encoding="utf-8") as f:
            text = f.read()
        with open(skill, "w", encoding="utf-8") as f:
            f.write(text.replace("python3 ${CLAUDE_SKILL_DIR}", python + " ${CLAUDE_SKILL_DIR}"))
    if not WINDOWS:
        script = os.path.join(CLAUDE_DEST, "scripts", "delegate.py")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return (f"  claude : {CLAUDE_DEST} ({changed} files updated) — Claude Code hands sub-tasks to Antigravity "
            "on its own (skill «antigravity», new Claude Code sessions)")


def remove_claude_skill() -> Optional[str]:
    if os.path.isdir(CLAUDE_DEST) and claude_skill_ours():
        shutil.rmtree(CLAUDE_DEST, ignore_errors=True)
        return f"  claude : removed {CLAUDE_DEST} (module not chosen)"
    return None


def uninstall(purge: bool) -> int:
    shutil.rmtree(DEST, ignore_errors=True)
    if os.path.isdir(CLAUDE_DEST) and claude_skill_ours():
        shutil.rmtree(CLAUDE_DEST, ignore_errors=True)
        print(f"Removed Claude Code's skill {CLAUDE_DEST}.")
    for name in ("state", "probe", "coord", "guard.log", "usage-cache.json", "delegate"):
        p = os.path.join(DATA, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    if purge:
        shutil.rmtree(DATA, ignore_errors=True)
        print(f"Removed {DEST} and {DATA}. Restart Antigravity.")
    else:
        print(f"Removed {DEST}. Restart Antigravity.")
        if os.path.isdir(os.path.join(DATA, "memory")):
            print(f"Memory kept in {os.path.join(DATA, 'memory')} (delete it with --uninstall --purge).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true", help="remove the plugin")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also remove memory and settings")
    ap.add_argument("--choose", action="store_true", help="pick the modules again in a menu")
    ap.add_argument("--list", action="store_true", help="describe every module and exit")
    ap.add_argument("--modules", help="exactly these modules, comma-separated (core is always in)")
    ap.add_argument("--without", help="the current choice minus these modules")
    ap.add_argument("--all", action="store_true", help="every module")
    ap.add_argument("--no-claude", action="store_true", help="don't install Claude Code's skill «antigravity»")
    ap.add_argument("--lang", choices=("ru", "en"), help="language of the menu and the module descriptions")
    a = ap.parse_args(argv)
    lang = language(a.lang)
    if a.uninstall:
        return uninstall(a.purge)
    if a.list:
        prev = saved()
        print(describe_all(set(prev["installed"]) if prev else default_choice(), lang))
        return 0

    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is required.", file=sys.stderr)
        return 1
    if not os.path.isfile(os.path.join(SRC, "plugin.json")):
        print(f"plugin.json not found in {SRC}", file=sys.stderr)
        return 1
    python = python_command()
    if not python:
        print("Python 3.8+ isn't reachable as " + ("`py -3`, `python` or `python3`" if WINDOWS else "`python3`")
              + " from a terminal; Antigravity's hooks need it. Install it from python.org"
              + (" (keep «Add python.exe to PATH» and the py launcher)" if WINDOWS else "") + ".", file=sys.stderr)
        return 1
    picked = choose(a, lang)
    if picked is None:
        print(TEXT[lang]["cancel"])
        return 0
    chosen, new, later = picked

    first = not os.path.isdir(DEST)
    staged = stage(chosen, python)
    try:
        changed = sync(os.path.join(staged, "antigravity-plus"), DEST)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    if not WINDOWS:
        for sub in ("hooks", "skills"):
            for dp, _dns, fns in os.walk(os.path.join(DEST, sub)):
                for fn in fns:
                    if fn.endswith((".py", ".sh")):
                        p = os.path.join(dp, fn)
                        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    with open(os.path.join(DEST, "plugin.json"), encoding="utf-8") as f:
        version = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
    version_text = version.group(1) if version else "?"
    save_choice(chosen, version_text, later)
    left_out = [m["title"][lang] for m in MODULES if m["id"] not in chosen]
    print(f"Installed Antigravity Plus {version_text} → {DEST} ({changed} files updated)")
    print(f"  modules: {len(chosen)} of {len(MODULES)} — " + ", ".join(m["title"][lang] for m in MODULES if m["id"] in chosen)
          + (f"; not installed: {', '.join(left_out)}" if left_out else "") + " · change: ./install.sh --choose")
    if new:
        print("  " + TEXT[lang]["new"].format(names=", ".join(BY_ID[x]["title"][lang] for x in new)))
    print("  skills : " + " ".join(sorted(os.listdir(os.path.join(DEST, "skills")))))
    agents = sorted(n[:-3] for n in os.listdir(os.path.join(DEST, "agents")) if n.endswith(".md"))
    print("  agents : " + (" ".join(agents) if agents else "—"))
    print(f"  hooks  : run with `{python}` — session context, instructions, guard, edit checks, coordination, Stop")
    if "usage" in chosen:
        print("  panel  : sidecars/usage — side pane «Расход токенов»")
    print(f"  data   : {DATA} (memory, settings, modules.json) + ~/.claude/projects/<project>/memory (shared with Claude Code)")
    claude = install_claude_skill(python) if "claude" in chosen else remove_claude_skill()
    if claude:
        print(claude)
    print("\nAntigravity check:", flush=True)
    subprocess.run([sys.executable, os.path.join(DEST, "skills", "doctor", "scripts", "doctor.py"), "--install-check"])
    print("\n" + ("First install: restart Antigravity to load the plugin." if first else
                  "Updated: changes apply to new conversations (restart Antigravity if a new skill doesn't show up)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
