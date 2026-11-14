#!/usr/bin/env python3
"""Antigravity Plus installer for macOS, Linux and Windows.

  python3 install.py                      install or update into ~/.gemini/config/plugins/antigravity-plus
  python3 install.py --no-claude          … without Claude Code's `antigravity` skill
  python3 install.py --uninstall          remove the plugin (memory and settings are kept)
  python3 install.py --uninstall --purge  also remove its memory and settings (~/.claude memory is never touched)

When Claude Code is on this computer (~/.claude exists), its side of the bridge is installed too: the skill
`antigravity` in ~/.claude/skills/antigravity, with which Claude Code hands sub-tasks to Antigravity agents
(claude-code/skills/antigravity).

On Windows run `install.cmd` (or `py -3 install.py`). Antigravity runs hook commands with `cmd /c` there, where
`python3` often doesn't exist, so the installer writes the Python command that works on this computer into the
installed hooks.json (and into the Claude Code skill). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "antigravity-plus")
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
DATA = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus")  # memory, settings, state, logs
CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")                    # Claude Code's own folder
CLAUDE_SRC = os.path.join(HERE, "claude-code", "skills", "antigravity")
CLAUDE_DEST = os.path.join(CLAUDE, "skills", "antigravity")
SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}
WINDOWS = os.name == "nt"


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
    ap.add_argument("--no-claude", action="store_true", help="don't install Claude Code's skill «antigravity»")
    a = ap.parse_args(argv)
    if a.uninstall:
        return uninstall(a.purge)

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

    first = not os.path.isdir(DEST)
    changed = sync(SRC, DEST)
    if python != "python3":
        write_hooks(DEST, python)
    if not WINDOWS:
        for sub in ("hooks", "skills"):
            for dp, _dns, fns in os.walk(os.path.join(DEST, sub)):
                for fn in fns:
                    if fn.endswith((".py", ".sh")):
                        p = os.path.join(dp, fn)
                        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    with open(os.path.join(DEST, "plugin.json"), encoding="utf-8") as f:
        version = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
    print(f"Installed Antigravity Plus {version.group(1) if version else '?'} → {DEST} ({changed} files updated)")
    print("  skills : " + " ".join(sorted(os.listdir(os.path.join(DEST, "skills")))))
    print("  agents : " + " ".join(sorted(n[:-3] for n in os.listdir(os.path.join(DEST, "agents")) if n.endswith(".md"))))
    print(f"  hooks  : run with `{python}` — session context, instructions, guard, edit checks, coordination, Stop")
    print("  panel  : sidecars/usage — side pane «Расход токенов»")
    print(f"  data   : {DATA} (memory, settings) + ~/.claude/projects/<project>/memory (shared with Claude Code)")
    claude = None if a.no_claude else install_claude_skill(python)
    if claude:
        print(claude)
    print("\nAntigravity check:", flush=True)
    subprocess.run([sys.executable, os.path.join(DEST, "skills", "doctor", "scripts", "doctor.py"), "--install-check"])
    print("\n" + ("First install: restart Antigravity to load the plugin." if first else
                  "Updated: changes apply to new conversations (restart Antigravity if a new skill doesn't show up)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
