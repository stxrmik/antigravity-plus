#!/usr/bin/env python3
"""Antigravity Plus — what differs between macOS, Linux and Windows, in one place.

  * file locks (fcntl / msvcrt) and an os.replace that waits out Windows' file sharing;
  * paths for the regex checks (forward slashes on Windows);
  * the command that starts Python here: the installer writes it into hooks.json (Antigravity runs hook commands
    with `sh -c` on macOS/Linux and `cmd /c` on Windows, where `python3` often doesn't exist);
  * Antigravity's own executable, which runs as Node with ELECTRON_RUN_AS_NODE=1;
  * Chromium-based browsers for /ui-check;
  * the running Antigravity language server: processes with their command lines, and the ports they listen on.

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Tuple

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

if WINDOWS:
    import msvcrt
    # Antigravity talks to hooks in UTF-8 JSON; Windows pipes default to the ANSI code page, which can't even encode
    # the 🛡️ in the guard's answers — and a hook that crashes answers nothing, which PreToolUse treats as deny.
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
else:
    import fcntl


def lock(fh, exclusive: bool = True) -> None:
    """Wait until `fh` (a lock file opened for writing) is locked. Windows only has exclusive locks."""
    if not WINDOWS:
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:  # LK_LOCK gives up after ten one-second retries; keep waiting
            time.sleep(0.05)


def unlock(fh) -> None:
    if not WINDOWS:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def replace(tmp: str, path: str) -> None:
    """os.replace that retries while another process briefly holds `path` open (Windows refuses then)."""
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not WINDOWS or attempt == 39:
                raise
            time.sleep(0.05)


def slashes(path: str) -> str:
    """Forward slashes for the regex checks on paths: C:\\proj\\.git\\config → C:/proj/.git/config."""
    return path.replace("\\", "/") if WINDOWS else path


def python_cmd() -> str:
    """How to start Python here, as the installer wrote it into hooks.json: "python3" on macOS and Linux, often
    "py -3" or "python" on Windows. Use it in the commands the hooks suggest to the agent."""
    try:
        with open(os.path.join(PLUGIN, "hooks.json"), encoding="utf-8") as f:
            m = re.search(r'"command"\s*:\s*"([^"]+?)\s+hooks/\w+\.py"', f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return "python -X utf8" if WINDOWS else "python3"


def antigravity_node() -> Optional[str]:
    """Antigravity's own executable: with ELECTRON_RUN_AS_NODE=1 it is a Node 24 (used when there's no node)."""
    if MAC:
        helper = "Antigravity.app/Contents/Frameworks/Antigravity Helper.app/Contents/MacOS/Antigravity Helper"
        cands = [os.path.join("/Applications", helper), os.path.expanduser(os.path.join("~/Applications", helper))]
    elif WINDOWS:
        roots = [os.environ.get(v, "") for v in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")]
        cands = [os.path.join(r, *p) for r in roots if r
                 for p in (("Programs", "Antigravity", "Antigravity.exe"), ("Antigravity", "Antigravity.exe"))]
    else:
        cands = ["/usr/share/antigravity/antigravity", "/opt/Antigravity/antigravity", "/opt/antigravity/antigravity",
                 "/usr/lib/antigravity/antigravity"]
    return next((c for c in cands if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def browsers() -> List[str]:
    """Chromium-based browsers that can take headless screenshots: absolute paths first, then names on PATH."""
    if MAC:
        apps = ["Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"]
        paths = [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in apps]
    elif WINDOWS:
        roots = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        rels = [("Google", "Chrome", "Application", "chrome.exe"), ("Chromium", "Application", "chrome.exe"),
                ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                ("Microsoft", "Edge", "Application", "msedge.exe")]
        paths = [os.path.join(r, *rel) for rel in rels for r in roots if r]
    else:
        paths = []
    return paths + ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser",
                    "microsoft-edge", "microsoft-edge-stable", "chrome", "msedge"]


def run(cmd: List[str], timeout: float = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def processes(name: str) -> List[Tuple[int, str]]:
    """(pid, full command line) of running processes whose command line contains `name`."""
    out: List[Tuple[int, str]] = []
    if WINDOWS:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            query = (f"Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%{name}%'\" | "
                     "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
            for line in run([ps, "-NoProfile", "-Command", query], timeout=20).splitlines():
                pid, _, cmd = line.partition("\t")
                if pid.strip().isdigit():
                    out.append((int(pid), cmd.strip()))
        return out
    if LINUX and os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            except OSError:
                continue
            if name in cmd:
                out.append((int(pid), cmd))
        return out
    for line in run(["ps", "-axww", "-o", "pid=,args="]).splitlines():  # macOS: -ww or long lines get cut
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and name in cmd:
            out.append((int(pid), cmd.strip()))
    return out


def listening_ports(pid: int) -> List[int]:
    """TCP ports on 127.0.0.1 the process listens on."""
    if WINDOWS:
        ports = []
        for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4] == str(pid) \
                    and parts[1].startswith("127.0.0.1:"):
                ports.append(int(parts[1].rsplit(":", 1)[1]))
        return ports
    if LINUX and os.path.isdir(f"/proc/{pid}/fd"):
        inodes = set()
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
        ports = []
        try:
            with open("/proc/net/tcp", encoding="ascii") as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) > 9 and p[3] == "0A" and p[9] in inodes and p[1].startswith("0100007F:"):
                        ports.append(int(p[1].split(":")[1], 16))
        except OSError:
            pass
        return ports
    out = run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"])
    return [int(p) for p in re.findall(r"127\.0\.0\.1:(\d+) \(LISTEN\)", out)]
