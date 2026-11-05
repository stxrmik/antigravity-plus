#!/usr/bin/env python3
"""Run Antigravity Plus's offline tests: python3 tests/run_tests.py [test_name …]

Each test runs as its own process in a throw-away home folder, so nothing touches your real ~/.gemini or
~/.claude. No Antigravity needed: the tests feed the hooks the JSON that Antigravity would send.
Works on macOS, Linux and Windows (CI runs all three). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "antigravity-plus")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    names = sys.argv[1:] or sorted(f[:-3] for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    failed = []
    for name in names:
        scratch = tempfile.mkdtemp(prefix=f"agp-{name}-")
        home = os.path.join(scratch, "home")  # each test gets its own empty home folder
        os.makedirs(os.path.join(home, ".gemini", "antigravity-plus", "state"))
        env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com", GIT_CONFIG_NOSYSTEM="1")
        for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
            env.pop(var, None)
        try:
            started = time.time()
            r = subprocess.run([sys.executable, os.path.join(HERE, name + ".py"), PLUGIN, scratch], env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            took = time.time() - started
            good = r.returncode == 0
            print(f"{'PASS' if good else 'FAIL'}  {name}  ({took:.1f} s)")
            if not good:
                failed.append(name)
                print("  " + (r.stdout + r.stderr).strip().replace("\n", "\n  "))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{len(names) - len(failed)} of {len(names)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
