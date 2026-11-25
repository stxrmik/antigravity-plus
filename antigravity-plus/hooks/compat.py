#!/usr/bin/env python3
"""Antigravity Plus — what differs between macOS, Linux and Windows, in one place.

  * file locks (fcntl / msvcrt) and an os.replace that waits out Windows' file sharing;
  * paths for the regex checks (forward slashes on Windows);
  * the command that starts Python here: the installer writes it into hooks.json (Antigravity runs hook commands
    with `sh -c` on macOS/Linux and `cmd /c` on Windows, where `python3` often doesn't exist);
  * Antigravity's own executable, which runs as Node with ELECTRON_RUN_AS_NODE=1;
  * Chromium-based browsers for /ui-check;
  * processes with their command lines, and the ports they listen on; the whole process table with parents
    (process_table(), descendants(), ancestor_named());
  * the running Antigravity language server — its address and CSRF token (language_server()).

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

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
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
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


class Proc(NamedTuple):
    """One running process, as process_table() saw it."""
    pid: int
    ppid: int
    name: str          # the executable's file name, lower-case: "powershell.exe", "ffmpeg", "python3.12"
    cmdline: str = ""  # its full command line — only when process_table(cmdlines=True) could read it


def _win_snapshot() -> Dict[int, Proc]:
    """Windows: every process with its parent and executable name, from a Toolhelp snapshot (milliseconds — no
    PowerShell); no command lines."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    out: Dict[int, Proc] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            out[int(entry.th32ProcessID)] = Proc(int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                                                 entry.szExeFile.lower())
            more = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _win_cmdlines() -> Dict[int, Proc]:
    """Windows: every process with its command line, from CIM (PowerShell: a second or two)."""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return {}
    query = ("Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.Name)`t"
             "$($_.CommandLine -replace '[\\r\\n]+', ' ')\" }")  # one line per process, even for a multi-line command
    out: Dict[int, Proc] = {}
    for line in run([ps, "-NoProfile", "-Command", query], timeout=30).splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            pid = int(parts[0])
            out[pid] = Proc(pid, int(parts[1]), parts[2].strip().lower(), parts[3].strip())
    return out


def process_table(cmdlines: bool = False) -> Dict[int, Proc]:
    """Every running process: {pid: Proc(pid, ppid, name, cmdline)}. Command lines only with `cmdlines` (on Windows
    that takes a PowerShell call; without it the table is a quick Toolhelp snapshot). {} when it can't be read."""
    try:
        if WINDOWS:
            table = _win_cmdlines() if cmdlines else {}
            return table or _win_snapshot()
        if LINUX and os.path.isdir("/proc"):
            out: Dict[int, Proc] = {}
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                        stat = f.read()
                    cmd = ""
                    if cmdlines:
                        with open(f"/proc/{entry}/cmdline", "rb") as f:
                            cmd = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
                except OSError:
                    continue
                name, _, rest = stat.partition(" (")[2].rpartition(")")  # «pid (comm) state ppid …», comm may hold ")"
                fields = rest.split()
                if len(fields) >= 2 and fields[1].isdigit():
                    out[int(entry)] = Proc(int(entry), int(fields[1]), name.lower(), cmd)
            return out
        out = {}
        for line in run(["ps", "-axww", "-o", "pid=,ppid=,comm="]).splitlines():  # macOS: comm is the executable's path
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                out[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), os.path.basename(parts[2]).lower())
        if cmdlines:
            for line in run(["ps", "-axww", "-o", "pid=,args="]).splitlines():
                pid, _, cmd = line.strip().partition(" ")
                if pid.isdigit() and int(pid) in out:
                    out[int(pid)] = out[int(pid)]._replace(cmdline=cmd.strip())
        return out
    except Exception:  # a process listing is a nicety: never fail the caller over it
        return {}


def descendants(table: Dict[int, Proc], root: int) -> List[int]:
    """The pids of `root`'s children, their children and so on (not `root` itself), parents before children."""
    kids: Dict[int, List[int]] = {}
    for p in table.values():
        if p.pid != p.ppid:
            kids.setdefault(p.ppid, []).append(p.pid)
    out, todo, seen = [], [root], {root}
    while todo:
        for child in sorted(kids.get(todo.pop(0), [])):
            if child not in seen:
                seen.add(child)
                out.append(child)
                todo.append(child)
    return out


def ancestor_named(table: Dict[int, Proc], pid: int, prefix: str) -> int:
    """The nearest ancestor of `pid` whose executable name starts with `prefix` ("language_server"), 0 if none."""
    seen = set()
    cur = table.get(pid)
    while cur and cur.ppid and cur.ppid not in seen:
        seen.add(cur.ppid)
        cur = table.get(cur.ppid)
        if cur and cur.name.startswith(prefix):
            return cur.pid
    return 0


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


class LanguageServer(NamedTuple):
    """Antigravity's running language server, as language_server() found it."""
    addr: str          # host:port of its plain-HTTP Connect API ("127.0.0.1:59128")
    token: str         # its CSRF token, sent as the x-codeium-csrf-token header
    source: str        # how it was found: "env", "daemon" or "process"
    pid: int = 0       # its process id, 0 when unknown
    version: str = ""  # Antigravity's version ("2.34.0"), "" when unknown


def daemon_dir() -> str:
    """Where the language server describes itself: ~/.gemini/antigravity/daemon/hub_<httpPort>.json."""
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "daemon")


def ls_healthy(addr: str, timeout: float = 1.5) -> bool:
    """True when http://<addr>/healthz answers {"status": "ok"}, as Antigravity's language server does."""
    import urllib.request
    base = addr.strip().rstrip("/")
    base = base if re.match(r"^https?://", base) else "http://" + base
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local: no proxy, no slow proxy lookup
    try:
        with opener.open(base + "/healthz", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        return isinstance(data, dict) and data.get("status") == "ok"
    except Exception:
        return False


def _alive(pid: int) -> bool:
    """False only when a POSIX process `pid` surely doesn't exist. Never probed on Windows, where os.kill(pid, 0)
    would terminate the process."""
    if WINDOWS or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, under another user
        pass
    return True


def daemon_servers() -> List[LanguageServer]:
    """The language servers described in daemon_dir(), newest file first, not checked yet. Antigravity's language
    server writes hub_<httpPort>.json (mode 0600) when it starts: {pid, httpsPort, httpPort, lsVersion, csrfToken}."""
    folder = daemon_dir()
    try:
        names = [n for n in os.listdir(folder) if n.startswith("hub_") and n.endswith(".json")]
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            when = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        port, token, pid = data.get("httpPort"), data.get("csrfToken"), data.get("pid")
        if not isinstance(token, str) or not token or not isinstance(port, int) or not 0 < port < 65536:
            continue
        found.append((when, LanguageServer(f"127.0.0.1:{port}", token, "daemon", pid if isinstance(pid, int) else 0,
                                           str(data.get("lsVersion") or ""))))
    return [ls for _, ls in sorted(found, key=lambda x: x[0], reverse=True)]


def language_server(probe: float = 1.5) -> Optional[LanguageServer]:
    """Antigravity's running language server, or None:
      1. ANTIGRAVITY_LS_ADDRESS + ANTIGRAVITY_CSRF_TOKEN — agent terminals and sidecars get them (hooks and the
         plugin's MCP server don't);
      2. daemon_servers() whose process is alive and whose /healthz answers — the only way from outside since
         Antigravity 2.34, which hands the token to the server in its environment instead of on its command line;
      3. the --csrf_token on its command line and the port among its own that answers /healthz (Antigravity ≤ 2.33).
    `probe` is the /healthz timeout in seconds."""
    addr, token = os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and token:
        return LanguageServer(addr, token, "env", 0, os.environ.get("ANTIGRAVITY_LS_VERSION", ""))
    for ls in daemon_servers():
        if _alive(ls.pid) and ls_healthy(ls.addr, probe):
            return ls
    for pid, line in processes("language_server"):
        if "--csrf_token" not in line or "antigravity" not in line.lower():
            continue
        m = re.search(r"--csrf_token[ =]\"?([^\s\"]+)", line)
        if not m:
            continue
        for port in listening_ports(pid):
            if ls_healthy(f"127.0.0.1:{port}", probe):
                return LanguageServer(f"127.0.0.1:{port}", m.group(1), "process", pid, "")
    return None


def ls_executable(pid: int) -> str:
    """The executable of the language server process `pid` (it also runs agentapi), "" when unknown."""
    if pid <= 0:
        return ""
    for p, line in processes("language_server"):
        if p == pid:
            m = re.match(r"\s*\"?(.*?language_server[\w.-]*)", line)
            return m.group(1) if m else ""
    return ""


def kill_tree(pid: int) -> None:
    """Terminate a process together with its child process group / tree."""
    if not pid or pid <= 0:
        return
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=0x08000000,
        )
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        for _ in range(10):
            try:
                os.killpg(pid, 0)
            except OSError:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            time.sleep(0.05)
