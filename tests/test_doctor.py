"""Tests for doctor.py: Antigravity CLI section, client data section, and CLI-only machine behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(PLUGIN, "skills", "doctor", "scripts"))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))

import compat  # noqa: E402
import doctor  # noqa: E402

ok = True


def check(name: str, cond: bool, got: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {got!r}"))


HOME = os.environ["HOME"]
SAVED_PATH = os.environ.get("PATH", "")

# 1. CLI not found → single informational line, no error
empty_bin = os.path.join(SP, "empty_bin")
os.makedirs(empty_bin, exist_ok=True)
os.environ["PATH"] = empty_bin
if hasattr(compat, "_AGY_VERSION"):
    compat._AGY_VERSION = None
lines = doctor.cli_status()
check("CLI not found -> single line without error",
      lines == ["ℹ️ Antigravity CLI не найден"], lines)

# 2. Fake agy in PATH + settings.json with secret-like value (must never be printed)
fake_bin = os.path.join(SP, "fake_bin")
os.makedirs(fake_bin, exist_ok=True)
if compat.WINDOWS:
    fake_agy = os.path.join(fake_bin, "agy.cmd")
    with open(fake_agy, "w", encoding="utf-8") as f:
        f.write("@echo off\necho agy version 1.5.16\n")
else:
    fake_agy = os.path.join(fake_bin, "agy")
    with open(fake_agy, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho 'agy version 1.5.16'\n")
    os.chmod(fake_agy, 0o755)

os.environ["PATH"] = fake_bin + os.pathsep + SAVED_PATH
if hasattr(compat, "_AGY_VERSION"):
    compat._AGY_VERSION = None

cli_dir = os.path.join(HOME, ".gemini", "antigravity-cli")
os.makedirs(cli_dir, exist_ok=True)
secret_val = "sk-secret-value-never-print-999"
with open(os.path.join(cli_dir, "settings.json"), "w", encoding="utf-8") as f:
    json.dump({"apiKey": secret_val, "theme": "dark"}, f)

lines = doctor.cli_status()
joined = "\n".join(lines)
check("Fake agy -> version shown", "1.5.16" in joined and "✅" in lines[0], lines)
check("CLI settings.json -> readable, secret value not printed",
      "читается" in joined and "apiKey" in joined and secret_val not in joined and "dark" not in joined, lines)
check("CLI info line about missing panel and automations",
      "Расход токенов" in joined and "/schedule-task" in joined, lines)

# Invalid JSON settings.json
with open(os.path.join(cli_dir, "settings.json"), "w", encoding="utf-8") as f:
    f.write("{broken json")
lines_bad_json = doctor.cli_status()
check("CLI settings.json broken -> 'не JSON'",
      any("не JSON" in ln for ln in lines_bad_json), lines_bad_json)

# 3. Machine without Desktop → "Desktop не найден", exit code 0, no error
real_discover = doctor.discover_ls
doctor.discover_ls = lambda: None
view_cli = doctor.antigravity_view(doctor.local_inventory(), None)
check("No Desktop (CLI present) -> 'Desktop не найден' without error",
      len(view_cli) == 1 and "Desktop не найден" in view_cli[0] and "❌" not in view_cli[0], view_cli)

# Subprocess --install-check without Desktop
r_ic = subprocess.run(
    [sys.executable, os.path.join(PLUGIN, "skills", "doctor", "scripts", "doctor.py"), "--install-check"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    env=dict(os.environ, PATH=fake_bin + os.pathsep + SAVED_PATH),
)
check("doctor.py --install-check exits 0 without Desktop",
      r_ic.returncode == 0 and "Traceback" not in r_ic.stdout + r_ic.stderr, (r_ic.returncode, r_ic.stdout[-200:]))

# 4. client.py missing → warning without traceback
saved_client = sys.modules.get("client", ...)
sys.modules["client"] = None  # type: ignore[assignment]
try:
    cl_missing = doctor.client_status(ls=("127.0.0.1:1", "tok", "hub-2.35.2"))
finally:
    if saved_client is ...:
        sys.modules.pop("client", None)
    else:
        sys.modules["client"] = saved_client
check("client.py missing -> warning without traceback",
      len(cl_missing) == 1 and cl_missing[0].startswith("⚠️") and "модуль данных клиента не установлен" in cl_missing[0],
      cl_missing)

# 5. Fake client.py in subprocess (ahead of hooks/ on sys.path): snapshot older than running Desktop
fake_pkg = os.path.join(SP, "fake_client_pkg")
os.makedirs(fake_pkg, exist_ok=True)
with open(os.path.join(fake_pkg, "client.py"), "w", encoding="utf-8") as f:
    f.write(
        "import time\n"
        "def snapshot():\n"
        "    return {'v': 1, 'at': time.time() - 3 * 3600, 'client': '2.35.1', 'source': 'desktop', 'models': []}\n"
        "def stale(snap=None):\n"
        "    return False\n"
        "def version():\n"
        "    return '2.35.1'\n"
    )

probe_code = (
    "import sys; sys.path.insert(0, sys.argv[1]); import doctor\n"
    "sys.path.insert(0, sys.argv[2])\n"
    "print('\\n'.join(doctor.client_status(ls=('127.0.0.1:1', 'tok', 'hub-2.35.2'))))\n"
)
r_old = subprocess.run(
    [sys.executable, "-c", probe_code, os.path.join(PLUGIN, "skills", "doctor", "scripts"), fake_pkg],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
check("Snapshot older than running Desktop -> warning",
      r_old.returncode == 0 and "⚠️" in r_old.stdout and "hub-2.35.2" in r_old.stdout
      and "2.35.1" in r_old.stdout and "3 ч назад" in r_old.stdout,
      (r_old.returncode, r_old.stdout, r_old.stderr))

# Fresh snapshot matching Desktop version
with open(os.path.join(fake_pkg, "client.py"), "w", encoding="utf-8") as f:
    f.write(
        "import time\n"
        "def snapshot():\n"
        "    return {'v': 1, 'at': time.time() - 30, 'client': '2.35.2', 'source': 'desktop', 'models': []}\n"
        "def stale(snap=None):\n"
        "    return False\n"
        "def version():\n"
        "    return '2.35.2'\n"
    )
r_fresh = subprocess.run(
    [sys.executable, "-c", probe_code, os.path.join(PLUGIN, "skills", "doctor", "scripts"), fake_pkg],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
check("Fresh snapshot matching Desktop -> ✅",
      r_fresh.returncode == 0 and "✅" in r_fresh.stdout and "только что" in r_fresh.stdout,
      (r_fresh.returncode, r_fresh.stdout, r_fresh.stderr))

doctor.discover_ls = real_discover
os.environ["PATH"] = SAVED_PATH
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
