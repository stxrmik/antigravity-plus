#!/usr/bin/env python3
r"""Antigravity Plus — agents' commands must not starve the user's own work.

Seen live: several agents ran renders, ffmpeg and checks at normal priority; the CPU sat at 100 % and the user's own
renders slowed to a third of their speed. Two settings (skills/memory/scripts/memory.py, `memory.py config …`) keep
agents out of the way in every project. Both are resource limits, not permission prompts: they hold in Bypass Mode too.
  * agentPriority: "normal" (the default: nothing changes), "below_normal" or "idle" — agent commands (run_command)
    run at that OS priority. For each command it lets through, the guard (hooks/guard.py) tells the background helper
    `procs.py lower` about it (kick(): a millisecond; it starts the helper when none runs). One helper serves every
    agent: it lowers every agent-command tree — every FAST s for FAST_FOR s after each command, then every SLOW s —
    and exits WINDOW s after the last command. Windows: the priority class (BELOW_NORMAL 0x4000, IDLE 0x40);
    macOS/Linux: nice 10 or 19 — on Linux for every thread (nice is per thread there) and, when the command has a
    session of its own, for its autogroup (with autogroup scheduling, sessions share the CPU first). A process is
    only ever lowered, never raised. What a lowered process starts inherits its priority, so lowering a command's own
    process early covers what it starts later; what it started already is lowered too.
  * heavyMax (0 = off) and heavyPrograms (ffmpeg, blender, HandBrakeCLI, xcodebuild, gradle…) — at most heavyMax
    heavy processes started by agents run at once. A command that starts one (guard.heavy_program()) waits up to WAIT
    s in the guard for a slot (take_slot(); AGP_HEAVY_WAIT) and is refused when none frees up. A slot given to a
    command stays taken until its process shows up (at most SLOT_TTL s), so two agents asking in the same second
    don't both get the last one.

Agent commands are the processes Antigravity's language server (language_server*) starts for them:
  * Windows: its powershell.exe / pwsh.exe children (its cmd.exe children are hooks and sidecars, the rest MCP servers);
  * macOS/Linux: its shell children (bash, zsh, sh, dash, fish, ksh, tcsh, pwsh) whose command line shows no hook
    (hooks/<name>.py) or sidecar (agy-node, sidecar).
The language servers (language_servers()): AGP_PROCS_LS_PID (tests) or `--ls` alone when given; else the nearest
language_server* ancestor of this process and the live ones ~/.gemini/antigravity/daemon describes — a pid counts only
while a language_server* process holds it (a stale file's pid may belong to anything by now). Heavy processes are
counted among all their descendants by executable name (case-insensitive, without .exe); one started by another heavy
process (a launcher shim, its own workers) is part of that one. On Windows a child counts only if it was created after
its parent: the pid of a parent that died may have been given to a new process since.

    procs.py lower --level below_normal|idle [--for 60] [--ls PID]   # the guard's background helper
    procs.py heavy [--ls PID]                                        # the heavy processes agents run now
    procs.py tree [--ls PID]                                         # agent processes and their priority

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import compat

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
STATE = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus", "state")
LOCK = os.path.join(STATE, "procs-lower.lock")   # the running helper holds it
KICK = os.path.join(STATE, "procs-lower.json")   # the guard's latest command: {"level", "at", "until"}
SLOTS = os.path.join(STATE, "heavy-slots.json")  # slots given to heavy commands whose process hasn't shown up yet

LEVELS = ("normal", "below_normal", "idle")
WIN_CLASS = {"below_normal": 0x4000, "idle": 0x40}  # BELOW_NORMAL_PRIORITY_CLASS, IDLE_PRIORITY_CLASS
NICE = {"below_normal": 10, "idle": 19}
# Windows priority classes from the lowest up: idle, below normal, normal, above normal, high, realtime
WIN_RANK = {0x40: 0, 0x4000: 1, 0x20: 2, 0x8000: 3, 0x80: 4, 0x100: 5}
WIN_NAME = {0x40: "idle", 0x4000: "below_normal", 0x20: "normal", 0x8000: "above_normal", 0x80: "high",
            0x100: "realtime"}
QUERY, SET_INFO = 0x1000, 0x0200  # PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SET_INFORMATION
WIN_ROOTS = {"powershell.exe", "pwsh.exe"}
POSIX_ROOTS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "pwsh", "powershell"}
NOT_AGENT = re.compile(r"hooks[\\/]+\w+\.py\b|agy-node|sidecar", re.I)  # a hook's or a sidecar's shell
PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")             # a name heavyPrograms may hold
WINDOW = 60.0     # the helper lowers new commands for this long after the guard's last one (AGP_PROCS_WINDOW)
FAST, FAST_FOR, SLOW = 0.25, 5.0, 2.0
LIFE = 3600.0     # no helper runs longer: the guard's next command starts a fresh one
WAIT = 20.0       # how long a heavy command waits in the guard for a slot (AGP_HEAVY_WAIT)
SLOT_TTL = 10.0   # how long a slot given to a heavy command waits for its process to show up


# ─────────────────────────────── settings ───────────────────────────────

def norm(name: str) -> str:
    """An executable's name as heavyPrograms compares it: the file name, lower-case, without .exe/.bat/.cmd/.com
    (C:/ffmpeg/bin/ffmpeg.exe → ffmpeg, gradlew.bat → gradlew)."""
    base = os.path.basename(str(name).replace("\\", "/").rstrip("/")).strip().lower()
    return re.sub(r"\.(?:exe|bat|cmd|com)$", "", base)


def config(settings: Optional[dict] = None) -> Tuple[str, int, List[str]]:
    """(agentPriority, heavyMax, heavyPrograms) from the plugin's settings (memory.settings() when none are given),
    each bad value read as the harmless one: an unknown level as "normal", a heavyMax below 1 as 0 (off), names that
    are no program names dropped. The names come back as norm() gives them."""
    if settings is None:
        try:
            sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
            import memory
            settings = memory.settings()
        except Exception:
            settings = {}
    level, most, listed = settings.get("agentPriority"), settings.get("heavyMax"), settings.get("heavyPrograms")
    names: List[str] = []
    for item in listed if isinstance(listed, list) else []:
        n = norm(item) if isinstance(item, str) and PROGRAM.match(item.strip()) else ""
        if n and n not in names:
            names.append(n)
    return (level if level in LEVELS else "normal",
            most if isinstance(most, int) and not isinstance(most, bool) and most > 0 else 0, names)


def heavy_name(name: str, names: Sequence[str]) -> bool:
    """Is a process with this executable name one of `names` (norm() forms)?"""
    n = norm(name)
    # Linux shows at most 15 characters of a name (/proc/<pid>/stat)
    return n in names or (compat.LINUX and len(n) == 15 and any(h.startswith(n) for h in names))


# ─────────────────────────────── priorities ───────────────────────────────

_KERNEL: Optional[tuple] = None


def _kernel() -> tuple:
    """(ctypes, wintypes, kernel32) with the functions used here — loaded on Windows only, and only when needed."""
    global _KERNEL
    if _KERNEL is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetPriorityClass.restype = wintypes.DWORD
        k.GetPriorityClass.argtypes = [wintypes.HANDLE]
        k.SetPriorityClass.restype = wintypes.BOOL
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        _KERNEL = (ctypes, wintypes, k)
    return _KERNEL


def _created(pid: int) -> int:
    """Windows: when process `pid` was created (FILETIME ticks); 0 when that can't be read."""
    try:
        ctypes, wintypes, k = _kernel()
        h = k.OpenProcess(QUERY, False, pid)
        if not h:
            return 0
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
                return 0
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return 0


def _win_class(pid: int) -> int:
    """Windows: the priority class of process `pid`, 0 when it can't be read."""
    _ctypes, _wintypes, k = _kernel()
    h = k.OpenProcess(QUERY, False, pid)
    if not h:
        return 0
    try:
        return int(k.GetPriorityClass(h))
    finally:
        k.CloseHandle(h)


def _win_lower(pid: int, cls: int) -> bool:
    _ctypes, _wintypes, k = _kernel()
    h = k.OpenProcess(SET_INFO | QUERY, False, pid)
    if not h:
        return False
    try:
        cur = int(k.GetPriorityClass(h))
        if not cur or WIN_RANK.get(cur, WIN_RANK[0x20]) <= WIN_RANK[cls]:
            return False  # unreadable, or already this low or lower
        return bool(k.SetPriorityClass(h, cls))
    finally:
        k.CloseHandle(h)


def _threads(pid: int) -> List[int]:
    """The ids nice applies to: on Linux every thread of the process (nice is per thread there), else the process."""
    if compat.LINUX:
        try:
            return [int(t) for t in os.listdir(f"/proc/{pid}/task") if t.isdigit()] or [pid]
        except OSError:
            return [pid]
    return [pid]


def _posix_lower(pid: int, nice: int) -> bool:
    changed = False
    for tid in _threads(pid):
        try:
            if os.getpriority(os.PRIO_PROCESS, tid) < nice:
                os.setpriority(os.PRIO_PROCESS, tid, nice)
                changed = True
        except OSError:  # gone, or not ours
            continue
    return changed


def _autogroup(root: int, nice: int) -> bool:
    """Linux with autogroup scheduling (most desktops share the CPU between sessions first, then by nice inside one):
    an agent command with a session of its own (its shell leads the session, as a terminal's does) gets `nice` for
    that session too. Never another session's — Antigravity's own runs the app."""
    if not compat.LINUX:
        return False
    try:
        with open(f"/proc/{root}/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rpartition(")")[2].split()  # state ppid pgrp session …
        if len(fields) < 4 or fields[3] != str(root):
            return False
        path = f"/proc/{root}/autogroup"
        with open(path, encoding="utf-8") as f:
            m = re.search(r"\bnice\s+(-?\d+)", f.read())
        if not m or int(m.group(1)) >= nice:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(nice))
        return True
    except (OSError, ValueError):
        return False


def lower(pid: int, level: str) -> bool:
    """Lower process `pid` to `level` ("below_normal", "idle") when it runs higher; never raise it, never fail (a
    process that is gone or not ours stays as it is). True when it was changed."""
    if level not in WIN_CLASS or pid <= 0 or pid == os.getpid():
        return False
    try:
        return _win_lower(pid, WIN_CLASS[level]) if compat.WINDOWS else _posix_lower(pid, NICE[level])
    except Exception:
        return False


def priority(pid: int) -> str:
    """The priority as `tree` shows it: on Windows the class ("normal", "below_normal", "idle"…), else "nice N";
    "?" when it can't be read."""
    try:
        if compat.WINDOWS:
            return WIN_NAME.get(_win_class(pid), "?")
        return f"nice {os.getpriority(os.PRIO_PROCESS, pid)}"
    except Exception:
        return "?"


# ─────────────────────────────── agent processes ───────────────────────────────

def language_servers(table: Dict[int, compat.Proc], pin: int = 0, slow: bool = False) -> List[int]:
    """Antigravity's language servers on this computer (the module's docstring), the one this process runs under
    first. `slow`: when nothing else finds one, also compat.language_server() (≤ 2.33: its command line)."""
    env = os.environ.get("AGP_PROCS_LS_PID", "").strip()
    if pin or env:
        pid = pin or (int(env) if env.isdigit() else 0)
        return [pid] if pid in table else []
    out: List[int] = []
    up = compat.ancestor_named(table, os.getpid(), "language_server")
    if up:
        out.append(up)
    for ls in compat.daemon_servers():
        p = table.get(ls.pid)
        if p and p.name.startswith("language_server") and ls.pid not in out:
            out.append(ls.pid)
    if not out and slow:
        try:
            found = compat.language_server(probe=1.0)
        except Exception:
            found = None
        if found and found.pid in table and table[found.pid].name.startswith("language_server"):
            out.append(found.pid)
    return out


def tree(table: Dict[int, compat.Proc], root: int) -> List[int]:
    """`root`'s descendants, parents first (compat.descendants) — on Windows only those created after their parent:
    a process whose parent died keeps the dead one's pid as its parent, and Windows soon gives that pid to another."""
    pids = compat.descendants(table, root)
    if not compat.WINDOWS:
        return pids
    born = {root: _created(root)}
    out = []
    for pid in pids:
        parent = table[pid].ppid
        if parent not in born:  # its parent was left out
            continue
        t = _created(pid)
        if t and born[parent] and t < born[parent]:
            continue
        born[pid] = t
        out.append(pid)
    return out


_CMDLINES: Dict[int, Tuple[str, str]] = {}  # pid → (name, command line): a shell's is read once (macOS: ps)


def _cmdlines(table: Dict[int, compat.Proc], pids: List[int]) -> Dict[int, str]:
    """POSIX: the command lines of `pids` (process_table() is read without them: on macOS that takes another ps)."""
    for pid in [p for p in _CMDLINES if p not in table]:
        del _CMDLINES[pid]
    out: Dict[int, str] = {}
    missing = []
    for pid in pids:
        p = table[pid]
        if p.cmdline:
            out[pid] = p.cmdline
        elif pid in _CMDLINES and _CMDLINES[pid][0] == p.name:
            out[pid] = _CMDLINES[pid][1]
        else:
            missing.append(pid)
    if missing and compat.LINUX:
        for pid in missing:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    out[pid] = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            except OSError:
                pass
    elif missing:
        for line in compat.run(["ps", "-ww", "-o", "pid=,args=", "-p", ",".join(map(str, missing))]).splitlines():
            pid_text, _, cmd = line.strip().partition(" ")
            if pid_text.isdigit() and int(pid_text) in missing:
                out[int(pid_text)] = cmd.strip()
    for pid in missing:
        if out.get(pid):
            _CMDLINES[pid] = (table[pid].name, out[pid])
    return out


def agent_trees(table: Dict[int, compat.Proc], ls: int) -> List[Tuple[int, List[int]]]:
    """The agent commands under language server `ls`: (the command's own process, what it started) for each. A shell
    whose command line can't be read is left out: only what is surely an agent command is touched."""
    kids: Dict[int, List[int]] = {}
    for pid in tree(table, ls):
        kids.setdefault(table[pid].ppid, []).append(pid)
    first = kids.get(ls, [])
    if compat.WINDOWS:
        roots = [pid for pid in first if table[pid].name in WIN_ROOTS]
    else:
        shells = [pid for pid in first if table[pid].name.lstrip("-") in POSIX_ROOTS]
        lines = _cmdlines(table, shells) if shells else {}
        roots = [pid for pid in shells if lines.get(pid) and not NOT_AGENT.search(lines[pid])]
    out = []
    for root in roots:
        sub, todo = [], [root]
        while todo:
            for child in kids.get(todo.pop(0), []):
                sub.append(child)
                todo.append(child)
        out.append((root, sub))
    return out


def lower_agents(table: Dict[int, compat.Proc], servers: List[int], level: str) -> int:
    """Lower every agent-command tree of `servers` to `level`; how many processes it changed."""
    changed = 0
    for ls in servers:
        for root, sub in agent_trees(table, ls):
            for pid in [root] + sub:
                changed += lower(pid, level)
            if compat.LINUX:
                _autogroup(root, NICE[level])
    return changed


def heavy(table: Dict[int, compat.Proc], servers: List[int], names: Sequence[str]) -> List[compat.Proc]:
    """The heavy processes agents run now: those named in `names` among the language servers' descendants, one per
    job — a heavy process started by another one (a launcher shim, its own workers) is part of that one."""
    out = []
    for ls in servers:
        under = {ls: False}
        for pid in tree(table, ls):
            p = table[pid]
            hit = heavy_name(p.name, names)
            if hit and not under.get(p.ppid, False):
                out.append(p)
            under[pid] = hit or under.get(p.ppid, False)
    return out


# ─────────────────────────────── heavy slots ───────────────────────────────

def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False))
    compat.replace(tmp, path)


def _slots(now: float) -> List[dict]:
    """The slots given out and not expired (SLOT_TTL)."""
    data = _read_json(SLOTS)
    out = []
    for s in data if isinstance(data, list) else []:
        if (isinstance(s, dict) and isinstance(s.get("at"), (int, float)) and isinstance(s.get("program"), str)
                and 0 <= now - s["at"] < SLOT_TTL):
            out.append({"at": float(s["at"]), "program": s["program"], "conversation": str(s.get("conversation") or ""),
                        "before": [p for p in s.get("before") or [] if isinstance(p, int)]})
    return out


def _waiting(slots: List[dict], running: List[compat.Proc]) -> List[dict]:
    """The slots whose process hasn't shown up yet: each heavy process of a slot's program that wasn't running when
    the slot was given ends that slot (the oldest first); from then on it is counted as running."""
    free = list(running)
    out = []
    for s in sorted(slots, key=lambda x: x["at"]):
        before = set(s["before"])
        hit = next((p for p in free if p.pid not in before and heavy_name(p.name, [s["program"]])), None)
        if hit is None:
            out.append(s)
        else:
            free.remove(hit)
    return out


def take_slot(program: str, limit: int, names: Sequence[str], conversation: str = "",
              servers: Optional[List[int]] = None) -> Optional[Tuple[List[compat.Proc], List[dict]]]:
    """One of `limit` slots for a command that starts the heavy program `program`: None when it got one (taken until
    its process shows up, SLOT_TTL s at most), else (the heavy processes running, the slots still waiting for theirs).
    A conversation's next command ends its own older slots: the last one has ended, or runs in the background, where
    the process count sees it."""
    os.makedirs(STATE, exist_ok=True)
    with open(SLOTS + ".lock", "a+", encoding="utf-8") as lk:
        compat.lock(lk)
        try:
            table = compat.process_table()
            running = heavy(table, language_servers(table) if servers is None else servers, names)
            now = time.time()
            slots = [s for s in _slots(now) if not (conversation and s["conversation"] == conversation
                                                    and now - s["at"] >= 2.0)]
            waiting = _waiting(slots, running)
            got = len(running) + len(waiting) < limit
            if got:
                waiting.append({"at": now, "program": program, "conversation": conversation,
                                "before": [p.pid for p in running]})
            _write_json(SLOTS, waiting)
            return None if got else (running, waiting)
        finally:
            compat.unlock(lk)


# ─────────────────────────────── the background helper ───────────────────────────────

def _try_lock(path: str):
    """`path` opened and locked without waiting; None when another process holds it (or it can't be opened)."""
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if compat.WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _release(fh) -> None:
    try:
        compat.unlock(fh)
    finally:
        fh.close()


def helper_running() -> bool:
    """Does a `procs.py lower` helper run now (it holds LOCK)?"""
    if not os.path.exists(LOCK):
        return False
    fh = _try_lock(LOCK)
    if fh is None:
        return True
    _release(fh)
    return False


def _window() -> float:
    try:
        w = float(os.environ.get("AGP_PROCS_WINDOW", "") or WINDOW)
    except ValueError:
        w = WINDOW
    return max(1.0, min(w, LIFE))


def kick(level: str) -> str:
    """The guard let an agent command through while agentPriority isn't normal: tell the background helper (it
    lowers the command's processes within FAST s), and start one when none runs. "kicked", "started", or "" when
    nothing could be done. Never raises; a millisecond or two — ~10 ms more when it starts the helper."""
    if level not in WIN_CLASS:
        return ""
    try:
        os.makedirs(STATE, exist_ok=True)
        now, window = time.time(), _window()
        with open(KICK, "w", encoding="utf-8") as f:  # in place: the helper may hold it open for reading
            f.write(json.dumps({"level": level, "at": now, "until": now + window}))
        fh = _try_lock(LOCK)
        if fh is None:
            return "kicked"  # the running helper reads it before its next pass
        _release(fh)
        cmd = [sys.executable, os.path.abspath(__file__), "lower", "--level", level, "--for", f"{window:g}"]
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True, "cwd": STATE}  # not the workspace: Windows can't delete a process's folder
        if compat.WINDOWS:
            flags = 0x08000000 | 0x00000008 | 0x00000200  # CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:  # CREATE_BREAKAWAY_FROM_JOB: out of a job object the hook may run in, so it outlives the hook
                subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)
            except OSError:
                subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        return "started"
    except Exception:
        return ""


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _read_kick() -> Optional[dict]:
    for _ in range(3):  # the guard rewrites it in place: a read in the middle of that gets half of it
        try:
            with open(KICK, encoding="utf-8") as f:
                d = json.loads(f.read())
        except OSError:
            return None
        except ValueError:
            time.sleep(0.01)
            continue
        ok = isinstance(d, dict) and all(isinstance(d.get(k), (int, float)) for k in ("at", "until"))
        return d if ok else None
    return None


def _nap(seconds: float, seen: float) -> None:
    """Sleep `seconds`, but wake up within FAST s when the guard writes a new kick."""
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(FAST, left))
        if _mtime(KICK) != seen:
            return


def watch(level: str, window: float, pin: int = 0) -> None:
    """The background helper: lower every agent-command tree, every FAST s for FAST_FOR s after each command the guard
    let through (its kick), then every SLOW s; stop `window` s after the last one (LIFE at most), or as soon as the
    language server it was pinned to (`pin`, AGP_PROCS_LS_PID) is gone. One per user: LOCK keeps a second one out."""
    born = time.time()
    until, last, seen, found, slow_at = born + window, born, 0.0, 0.0, 0.0
    servers: List[int] = []
    while True:
        fh = _try_lock(LOCK)
        if fh is None:
            return  # another helper runs: the kick told it about this command
        try:
            while True:
                stamp = _mtime(KICK)
                if stamp != seen:
                    seen = stamp
                    k = _read_kick()
                    if k:
                        level = k["level"] if k.get("level") in WIN_CLASS else level
                        last, until = max(last, float(k["at"])), max(until, float(k["until"]))
                now = time.time()
                if now >= until or now - born >= LIFE:
                    break
                table = compat.process_table()
                if not servers or now - found > 30 or any(s not in table for s in servers):
                    slow = now - slow_at > 30  # compat.language_server() may start PowerShell: not on every pass
                    servers, found = language_servers(table, pin, slow=slow), now
                    slow_at = now if slow else slow_at
                    if not servers and (pin or os.environ.get("AGP_PROCS_LS_PID")):
                        return  # the language server it watched is gone
                lower_agents(table, servers, level)
                _nap(FAST if time.time() - last < FAST_FOR else SLOW, seen)
        finally:
            _release(fh)
        k = _read_kick()  # a command that came while it was stopping: watch again, unless another helper took over
        if not k or float(k["until"]) <= time.time() or time.time() - born >= LIFE:
            return
        until, last = max(until, float(k["until"])), max(last, float(k["at"]))


# ─────────────────────────────── CLI ───────────────────────────────

def show_tree(pin: int = 0) -> str:
    table = compat.process_table()
    servers = language_servers(table, pin, slow=True)
    level, most, names = config()
    lines = [f"Settings: agentPriority {level} · heavyMax {most or 'off'} · heavyPrograms {', '.join(names) or '—'}",
             f"Helper (procs.py lower): {'running' if helper_running() else 'not running'}"]
    if not servers:
        return "\n".join(lines + ["No Antigravity language server found."])
    for ls in servers:
        lines.append(f"{table[ls].name} {ls}")
        trees = agent_trees(table, ls)
        for root, sub in trees:
            lines.append(f"  {table[root].name} {root} [{priority(root)}] — agent command")
            depth = {root: 1}
            for pid in sub:
                depth[pid] = depth.get(table[pid].ppid, 1) + 1
                mark = " — heavy" if heavy_name(table[pid].name, names) else ""
                lines.append("  " * depth[pid] + f"{table[pid].name} {pid} [{priority(pid)}]{mark}")
        roots = {r for r, _sub in trees}
        others = [pid for pid in tree(table, ls) if table[pid].ppid == ls and pid not in roots]
        if not trees:
            lines.append("  no agent commands run now")
        if others:
            lines.append("  not agent commands (hooks, sidecars, MCP servers; left as they are): "
                         + ", ".join(f"{table[p].name} {p}" for p in others))
    return "\n".join(lines)


def show_heavy(pin: int = 0) -> str:
    table = compat.process_table()
    servers = language_servers(table, pin, slow=True)
    _level, most, names = config()
    found = heavy(table, servers, names)
    waiting = _waiting(_slots(time.time()), found)
    limit = f"heavyMax {most}" if most else "heavyMax off (0): agents may run any number"
    out = [f"Heavy processes agents run now: {len(found)} · {limit} · heavyPrograms {', '.join(names) or '—'}"]
    out += [f"- {norm(p.name)} PID {p.pid}" for p in found] or ["- none"]
    if waiting:
        out.append("Slots given to commands whose process hasn't shown up yet: "
                   + ", ".join(s["program"] for s in waiting))
    if not servers:
        out.append("No Antigravity language server found.")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse  # here, not at the top: the guard imports this module on every command
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("lower", help="the guard's background helper")
    sp.add_argument("--level", choices=LEVELS, required=True)
    sp.add_argument("--for", dest="window", type=float, default=WINDOW)
    sp.add_argument("--ls", type=int, default=0)
    for name in ("heavy", "tree"):
        sub.add_parser(name).add_argument("--ls", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "lower":
        if a.level in WIN_CLASS:
            try:
                watch(a.level, max(0.0, min(a.window, LIFE)), a.ls)
            except Exception:  # a background helper has nobody to tell
                pass
        return 0
    if a.cmd in ("heavy", "tree"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        print(show_heavy(a.ls) if a.cmd == "heavy" else show_tree(a.ls))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
