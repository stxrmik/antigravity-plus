#!/usr/bin/env python3
"""Antigravity Plus installer for macOS, Linux and Windows.

  python3 install.py                      install or update into ~/.gemini/config/plugins/antigravity-plus
  python3 install.py --uninstall          remove the plugin (memory and settings are kept)
  python3 install.py --uninstall --purge  also remove its memory and settings (~/.claude memory is never touched)

On Windows run `install.cmd` (or `py -3 install.py`). Antigravity runs hook commands with `cmd /c` there, where
`python3` often doesn't exist, so the installer writes the Python command that works on this computer into the
installed hooks.json. Stdlib only, Python 3.8+.
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
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "antigravity-plus")
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
DATA = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus")  # memory, settings, state, logs
SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}
WINDOWS = os.name == "nt"


def skipped(name: str) -> bool:
    return name in SKIP or name.endswith((".pyc", ".pyo"))


def sync(src: str, dest: str) -> int:
    """Make `dest` a copy of `src` (like rsync --delete) without removing the folder itself, so a running
    Antigravity doesn't lose it mid-way. Returns how many files changed."""
    changed = 0
    os.makedirs(dest, exist_ok=True)
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if not skipped(d)]
        target = os.path.join(dest, os.path.relpath(dp, src))
        os.makedirs(target, exist_ok=True)
        for fn in fns:
            if skipped(fn):
                continue
            s, d = os.path.join(dp, fn), os.path.join(target, fn)
            if not os.path.exists(d) or not filecmp.cmp(s, d, shallow=False):
                shutil.copy2(s, d)
                changed += 1
    for dp, dns, fns in os.walk(dest, topdown=False):  # drop what the source no longer has
        rel = os.path.relpath(dp, dest)
        source_dir = os.path.join(src, rel)
        for fn in fns:
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


def uninstall(purge: bool) -> int:
    shutil.rmtree(DEST, ignore_errors=True)
    for name in ("state", "probe", "coord", "guard.log", "usage-cache.json"):
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
    print("\nAntigravity check:", flush=True)
    subprocess.run([sys.executable, os.path.join(DEST, "skills", "doctor", "scripts", "doctor.py"), "--install-check"])
    print("\n" + ("First install: restart Antigravity to load the plugin." if first else
                  "Updated: changes apply to new conversations (restart Antigravity if a new skill doesn't show up)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
