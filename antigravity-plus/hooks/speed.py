#!/usr/bin/env python3
"""Antigravity Plus — speed and context: fewer wasted steps, a lean context, no loops of failing commands.

Measured on real conversations (8 conversations, 2,079 steps, Gemini 4 Argon High): while the terminal sandbox was
on, a quarter of all commands failed inside it and were re-run with BypassSandbox — the same few commands every
time (a deploy script, docker, Chrome); the median model call carried 136k tokens of context (p90 229k), a third
of all tool output came from a few whole-file reads; and three or more commands failed in a row several times
(multi-line `python3 -c` through ssh and docker exec). hooks/conversation.py records command results, very large
results and the context size as the transcript grows; this module turns them into short hints before the next
model call (hooks/instructions.py):
  * sandbox memory — a command that failed inside the sandbox and then worked with BypassSandbox is remembered for
    the project (~/.gemini/antigravity-plus/sandbox.json); the session context (hooks/session_start.py) lists those
    commands while the sandbox is on, so the agent sets BypassSandbox: true on the first try. A command that works
    inside the sandbox twice is forgotten;
  * context budget — once the context passes CTX_WARN, and again near compaction (CTX_HIGH): keep reads targeted,
    hand broad searches to explorer, offer /handoff when the task is done; and, once per user message, after one
    very large read or output: read ranges, trim output;
  * error loops — FAIL_RUN commands or edits failing in a row: stop, read the error, change the approach.
Setting speedHints (`memory.py config speed-hints off`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional

import compat
import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
STORE = os.path.expanduser("~/.gemini/antigravity-plus/sandbox.json")
AG_CONFIG = os.path.expanduser("~/.gemini/config/config.json")
CTX_WARN = 150_000       # tokens per model call: steps get slower, details from the middle get lost
CTX_HIGH = 220_000       # near the point where Antigravity compacts the conversation
FAIL_RUN = 3             # failures in a row before the error-loop hint (again at twice that)
FORGET_AFTER = 2         # successes inside the sandbox that make a remembered command unnecessary
KEEP = 20                # remembered commands per project


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("speedHints", True))
    except Exception:
        return True


def sandbox_on() -> Optional[bool]:
    """Antigravity's own setting (userSettings.enableTerminalSandbox); None when it can't be read."""
    try:
        with open(AG_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        value = (data.get("userSettings") or data).get("enableTerminalSandbox")
        return value if isinstance(value, bool) else None
    except (OSError, ValueError, AttributeError):
        return None


def workspace(payload: dict) -> str:
    return next((os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), "")


class Store:
    """sandbox.json — {"projects": {workspace: {command key: {"n": confirmations, "ok": successes inside since,
    "example": command, "last": date}}}} — under an exclusive lock (several conversations write it)."""

    def __init__(self, write: bool = True):
        self.write = write

    def __enter__(self) -> dict:
        self.lock = None
        if self.write:
            os.makedirs(os.path.dirname(STORE), exist_ok=True)
            self.lock = open(STORE + ".lock", "w")
            compat.lock(self.lock)
        try:
            with open(STORE, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data.get("projects"), dict):
            self.data["projects"] = {}
        return self.data

    def __exit__(self, *exc) -> None:
        if not self.write:
            return
        try:
            tmp = f"{STORE}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            compat.replace(tmp, STORE)
        except OSError:
            pass
        finally:
            compat.unlock(self.lock)
            self.lock.close()


def known(ws: str) -> Dict[str, dict]:
    """Commands that need BypassSandbox in this workspace."""
    if not ws:
        return {}
    with Store(write=False) as data:
        return dict(data["projects"].get(ws) or {})


def sandbox_lines(ws: str) -> List[str]:
    """For the session context: the commands this project needed BypassSandbox for (only while the sandbox is on)."""
    if sandbox_on() is False or not setting():
        return []
    cmds = known(ws)
    if not cmds:
        return []
    names = sorted(cmds, key=lambda k: -cmds[k].get("n", 0))
    return [f"Terminal sandbox: these commands failed inside it in this project and worked with BypassSandbox — run "
            f"them with `BypassSandbox: true` from the start: {', '.join('`' + k + '`' for k in names)}."]


def update_store(ws: str, calls: dict) -> List[str]:
    """Remember the commands that just needed BypassSandbox, forget the ones that keep working inside the sandbox.
    Returns the newly remembered keys."""
    new = [it for it in calls.get("sbx_new") or [] if isinstance(it, dict) and it.get("key")]
    worked = list(calls.get("sbx_ok") or [])
    calls["sbx_new"], calls["sbx_ok"] = [], []
    if not ws or not (new or worked) or (not new and not set(worked) & set(known(ws))):
        return []  # nothing to change: don't rewrite the file on every step
    fresh: List[str] = []
    with Store() as data:
        project = data["projects"].setdefault(ws, {})
        for key in worked:
            if key in project:
                project[key]["ok"] = project[key].get("ok", 0) + 1
                if project[key]["ok"] >= FORGET_AFTER:
                    project.pop(key)
        for it in new:
            entry = project.setdefault(it["key"], {"n": 0})
            if entry["n"] == 0 and it["key"] not in fresh:
                fresh.append(it["key"])
            entry.update(n=entry["n"] + 1, ok=0, example=it.get("cmd", "")[:160], last=time.strftime("%Y-%m-%d"))
        for key in sorted(project, key=lambda k: project[k].get("last", ""))[:-KEEP]:
            project.pop(key)
        if not project:
            data["projects"].pop(ws, None)
    return fresh


def tag(name: str, text: str) -> str:
    return f'<{name} source="Antigravity Plus">\n{text}\n</{name}>'


def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """The hints for this model call, or None. Updates st["speed"] and drains st["calls"] (the caller stores st)."""
    if not st or not setting():
        return None
    calls = st.setdefault("calls", {})
    sp = st.setdefault("speed", {})
    main = bool(st.get("users"))
    ws = workspace(payload)
    out: List[str] = []

    fresh = update_store(ws, calls)
    fail = calls.get("sbx_fail") or {}
    if fresh:
        out.append(tag("sandbox", ", ".join(f"`{k}`" for k in fresh) + (" fails" if len(fresh) == 1 else " fail") +
                       " inside the terminal sandbox in this project and worked with BypassSandbox. Antigravity Plus "
                       "remembered it for this project: from now on run it with `BypassSandbox: true` right away — new "
                       "conversations get the list at the start."))
        sp["sbx_step"] = max(sp.get("sbx_step", -1), fail.get("step", -1))
    elif fail.get("step", -1) > sp.get("sbx_step", -1):
        sp["sbx_step"] = fail["step"]
        told = sp.setdefault("sbx_told", [])
        if fail.get("key") in known(ws) and fail["key"] not in told:
            told.append(fail["key"])
            out.append(tag("sandbox", f"`{fail['key']}` is known to fail inside the terminal sandbox in this project. "
                                      "Re-run it with `BypassSandbox: true`, and set that from the start for it."))

    run = calls.get("fail_run", 0)
    level = run // FAIL_RUN
    if level == 0 or calls.get("fail_id") != sp.get("loop_id"):
        sp["loop"], sp["loop_id"] = 0, calls.get("fail_id")  # a new run of failures starts from scratch
    if level > sp.get("loop", 0) and level <= 2:
        sp["loop"] = level
        out.append(tag("error_loop", f"{run} commands or edits in a row failed (last: `{calls.get('fail_last', '')}`). "
                       "Stop trying variations: read the last error in full, check the assumption behind it — working "
                       "directory, paths, which environment it runs in, quoting — and change the approach. Code for a "
                       "server or a container goes into a file piped to stdin (`ssh host 'python3 -' < script.py`, "
                       "`docker exec -i <container> python3 - < script.py`) instead of `python3 -c` with nested quotes."
                       + (" A failure you can't explain → the `debugger` subagent." if main else "")))

    ctx = calls.get("ctx", 0)
    if sp.get("ctx_cp") != len(st.get("checkpoints") or []) or ctx < CTX_WARN * 0.7:
        sp["ctx_cp"], sp["ctx_level"] = len(st.get("checkpoints") or []), 0  # compacted: the context is small again
    ctx_level = 2 if ctx >= CTX_HIGH else 1 if ctx >= CTX_WARN else 0
    ctx_told = ctx_level > sp.get("ctx_level", 0)
    if ctx_told:
        sp["ctx_level"] = ctx_level
        if ctx_level == 1:
            text = (f"The context of this conversation is ~{ctx // 1000}k tokens, and every step re-reads all of it: "
                    "steps get slower and details from the middle get lost. Keep it lean from here: find things with "
                    "`rg -n` and read only the lines you need (StartLine/EndLine), trim command output (`| tail -40`, "
                    "`| grep`)" + (", hand broad searches to `explorer`. When the current task is done and more work "
                                   "remains, suggest /handoff to continue in a fresh conversation." if main else "."))
        else:
            text = (f"The context is ~{ctx // 1000}k tokens — close to the point where Antigravity compacts the "
                    "conversation and drops details. Keep task.md current so nothing gets lost"
                    + (", and at the next natural pause suggest /handoff to continue in a fresh conversation."
                       if main else "."))
        out.append(tag("context_budget", text))

    big = calls.get("big") or {}
    if big.get("step", -1) > sp.get("big_step", -1):
        sp["big_step"] = big["step"]
        if main and sp.get("big_user") != len(st["users"]) and not ctx_told:
            sp["big_user"] = len(st["users"])
            out.append(tag("context_budget", f"That put {big.get('what', 'a very large result')} "
                           f"(~{max(1, big.get('chars', 0) // 4000)}k tokens) into the context, and it stays there for "
                           "every later step. When you need part of a file, find it with `rg -n` and read that range "
                           "(StartLine/EndLine); trim long command output with `| tail` or `| grep`."))
    return "\n".join(out) or None
