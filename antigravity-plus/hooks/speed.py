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
    hand broad searches to explorer, offer /handoff when the task is done, and (near compaction, with autoMemory)
    save to memory what a future conversation needs before compaction drops it; and, once per user message, after one
    very large read or output: read ranges, trim output. The thresholds follow the window Antigravity reports for the
    model (hooks/context_window.py; read from this conversation's latest call when no reading is younger than STALE),
    else the window known at release (ctx_limits);
  * error loops — FAIL_RUN commands or edits failing in a row: stop, read the error, change the approach.
Setting speedHints (`memory.py config speed-hints off`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import compat
import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
STORE = os.path.expanduser("~/.gemini/antigravity-plus/sandbox.json")
AG_CONFIG = os.path.expanduser("~/.gemini/config/config.json")
CTX_WARN = 150_000       # tokens per model call: steps get slower, details from the middle get lost
CTX_HIGH = 220_000       # near the 256k window of Gemini 3.x Flash, where Antigravity compacts the conversation
# Antigravity compacts a conversation when the next model call would no longer fit its window (maxContextTokens,
# counted with the tokens the model reported; verified on 2.33.0, 2026-11-20). Antigravity sets the window on its
# servers and changes it without a release, so the window it reports for the model comes first (real_window()); the
# numbers below were known at release (rechecked 2026-11-25 on 2.35.1): 512k for the Gemini 4 family (Carbon, Argon,
# Hydrogen, Neon, every Orchestrator) since 2026-11-16 — real conversations' last calls before a compaction carried
# 504k–512k, none more —, 256k for Gemini 3.x Flash, 128k for Flash-Lite, 840k for Claude 5.5.
GEMINI4 = re.compile(r"gemini[-_ ]?4|orchestrator|flash[-_]worker|hydrogen|carbon|argon|neon")
CTX4_WARN, CTX4_HIGH = 200_000, 440_000
ASK_AGAIN = 25           # model calls before a window reading that found nothing is tried again
EXECUTOR_RE = re.compile(r"flash[-_ ]worker|\(orchestrator\)", re.I)  # Orchestrator's executor: gemini-flash-worker-agy
FAIL_RUN = 3             # failures in a row before the error-loop hint (again at twice that)
FORGET_AFTER = 2         # successes inside the sandbox that make a remembered command unnecessary
KEEP = 20                # remembered commands per project


def _parse_compact_override(raw: str, window: int) -> Optional[int]:
    """Parse the Antigravity CLI setting AGY_CLI_AUTO_COMPACT_OVERRIDE («700000», «800K», «0.5M», «80%») against
    `window` (the desktop app has no such setting). Kept identical to skills/usage/scripts/usage.py
    `parse_compact_override`."""
    s = (raw or "").strip()
    if not s or window <= 0:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%|k|m)?", s, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    tokens = (int(window * (val / 100.0)) if unit == "%" else int(val * 1000) if unit == "k"
              else int(val * 1_000_000) if unit == "m" else int(val))
    return tokens if 0 < tokens < window else None


def ctx_limits(model: str, window: Optional[int] = None) -> Tuple[int, int, str]:
    """(warn, high, note) context thresholds for `model`: Antigravity compacts when the next call wouldn't fit the
    window. `window` is the one Antigravity reported for the model (real_window()); without it, the window known at
    release — 512k for Gemini 4 and Gemini 3.5 Flash-Lite, 256k for older Gemini, 128k for 2.5/3.1 Flash-Lite, 840k for
    Claude 5.5, 80k for GPT-OSS 120B. A reported window that differs moves the near-compaction threshold with it (the
    same share of the window) and the first hint only down: a bigger window doesn't make a long context cheaper to
    re-read."""
    m = (model or "").lower()
    if re.search(r"gemini[-_ ]?(?:2\.5|3\.1[-_ ]?flash[-_ ]?lite)", m):
        warn, high, win, note = 80_000, 110_000, 128_000, " of the model's 128k window"
    elif re.search(r"gpt[-_ ]?oss", m):
        warn, high, win, note = 50_000, 70_000, 80_000, " of the model's 80k window"
    elif re.search(r"flash[-_ ]?lite", m) or GEMINI4.search(m):  # 3.5 Flash-Lite & Gemini 4/Orchestrator share 512k
        warn, high, win, note = CTX4_WARN, CTX4_HIGH, 512_000, " of the model's 512k window"
    elif "gemini" in m:
        warn, high, win, note = CTX_WARN, CTX_HIGH, 256_000, " of the model's 256k window"
    elif re.search(r"(opus|sonnet)[- ]?5[.-]5", m):
        warn, high, win, note = 180_000, 720_000, 840_000, " of the model's 840k window"
    else:
        warn, high, win, note = CTX_WARN, CTX_HIGH, 0, ""
    if isinstance(window, int) and not isinstance(window, bool) and window >= 4_000 and window != win:
        if win:
            high = int(window * high / win)
            warn = warn if window >= win else max(1, int(window * warn / win))
        else:  # a model this table doesn't know: the shares Gemini 4's thresholds have of its window
            warn, high = min(CTX4_WARN, int(window * 0.6)), int(window * CTX4_HIGH / 512_000)
        win, note = window, f" of the model's {round(window / 1000)}k window"
    if not win:
        return warn, high, ""
    override = _parse_compact_override(os.environ.get("AGY_CLI_AUTO_COMPACT_OVERRIDE", ""), win)
    if override is not None:
        warn = min(warn, max(1, int(override * 0.6)))
        high = min(high, max(1, int(override * 0.88)))
        lim_s = f"{override // 1000}k" if override >= 1000 else str(override)
        note = f" of the {lim_s} compaction limit (Antigravity CLI AGY_CLI_AUTO_COMPACT_OVERRIDE; {win // 1000}k window)"
    return warn, high, note


def cli_session(payload: dict) -> bool:
    """True in an Antigravity CLI session: `agy` is its own language server (random ports, no hub file), so the one
    compat.language_server() finds is Desktop's, which doesn't know this conversation — never ask it about it."""
    runtime = getattr(compat, "runtime", None)
    try:
        if runtime:
            return runtime(payload) == "cli"
    except Exception:
        pass
    tp = str((payload or {}).get("transcriptPath") or "").replace("\\", "/")
    return "/antigravity-cli/" in tp


def real_window(payload: dict, calls: dict, sp: dict, model: str) -> Optional[int]:
    """The window Antigravity gives `model`, None when unknown: the newest recorded reading of a real call
    (hooks/context_window.py) made within its FRESH day, read again from this conversation's latest call when it is missing or older than STALE and that
    call is recent — once per model, and again only ASK_AGAIN calls after a reading that found nothing. Not while this
    conversation's calls weren't all counted (it began under an older version: the count can't pick the latest call).
    An Orchestrator session takes its executor's call as its own reading. Without a fresh reading, the window the
    client lists for the model (hooks/client.py's snapshot — known before the model's first call), then an older
    reading. In an Antigravity CLI session (cli_session()) no language server is asked. Never raises."""
    try:
        import context_window as cw
        found = cw.lookup(model)
        now = time.time()
        if found and now - found[1] <= cw.STALE:
            return found[0]
        k = cw.key(model)
        own = sp.get("win_conv") if isinstance(sp.get("win_conv"), int) else None
        at = cw.epoch(str(calls.get("ctx_at") or ""))
        n = int(calls.get("n_calls") or 0)
        asked = sp.get("win_ask") if isinstance(sp.get("win_ask"), dict) else {}
        if (k and n > 0 and not calls.get("n_partial") and not cli_session(payload) and at is not None and now - at <= cw.FRESH
                and (asked.get("k") != k or n - int(asked.get("n") or 0) >= ASK_AGAIN)):
            sp["win_ask"] = {"k": k, "n": n}
            got = cw.refresh(str(payload.get("conversationId") or ""), n, at, now)
            if got and k.startswith("orchestrator") and any(EXECUTOR_RE.search(name) for name in got[0]):
                own = sp["win_conv"] = got[1]
            found = cw.lookup(model) or found
        if found and now - found[1] <= cw.FRESH:
            return found[0]
        return own or cw.client_window(model) or (found[0] if found else None)
    except Exception:  # a missing module, an odd state, an unreadable file: the numbers known at release apply
        return None


def setting(key: str = "speedHints") -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get(key, True))
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


def _allow_auto_bypass(cmd: str, key: str) -> bool:
    """Whether the guard may set BypassSandbox on `cmd` by itself: not for an inline -c/-e script (its key names no
    program) nor a chain, pipe, redirection or substitution — there it would cover more than the command learned."""
    if not key or key.endswith((" -c", " -e")):
        return False
    s = re.sub(r"^\s*(?:cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*)+", "", str(cmd or ""))
    if compat.WINDOWS:
        s = re.sub(r"^(?:\$env:\w+\s*=\s*'[^']*'\s*;\s*)+", "", s)
    q, i, n = "", 0, len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                q = ""
        elif c == "\\" and i + 1 < n and not (compat.WINDOWS and q == ""):
            i += 1
        elif q == '"':
            if c == '"':
                q = ""
            elif c == "`" or s[i:i + 2] == "$(":
                return False
        elif c in "'\"":
            q = c
        elif c in ";|\r\n&<>`":
            return False
        elif s[i:i + 2] in ("$(", "<(", ">("):
            return False
        i += 1
    return True


def needs_bypass_sandbox(payload: dict, cmd: str, args: Optional[dict] = None) -> bool:
    """True when `cmd` is remembered to fail in the sandbox for this workspace and BypassSandbox isn't set yet."""
    if not cmd or not setting() or sandbox_on() is False or (args or {}).get("BypassSandbox"):
        return False
    ws = workspace(payload)
    if not ws:
        return False
    clean = re.sub(r"^(?:\$env:\w+\s*=\s*'[^']*'\s*;\s*)+", "", cmd) if compat.WINDOWS else cmd
    key = conv.command_key(clean, ws)
    if not _allow_auto_bypass(clean, key):
        return False
    return bool(key and key in known(ws))


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
            if str(it["key"]).endswith((" -c", " -e")):
                continue  # `python3 -c …`: the key says nothing about what ran (chains and pipes are learned by their
                # program — `./deploy/ssh.sh '…' | tail` — and only the overwrite leaves them alone)
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
    model = str(payload.get("modelName") or "")
    warn, high, ctx_note = ctx_limits(model, real_window(payload, calls, sp, model))
    if sp.get("ctx_cp") != len(st.get("checkpoints") or []) or ctx < warn * 0.7:
        sp["ctx_cp"], sp["ctx_level"] = len(st.get("checkpoints") or []), 0  # compacted: the context is small again
    elif sp.get("ctx_level", 0) >= 2 and ctx < high * 0.85:
        sp["ctx_level"] = 1 if ctx >= warn else 0  # switched to a larger-window model (e.g. Gemini → Claude 5.5)
    ctx_level = 2 if ctx >= high else 1 if ctx >= warn else 0
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
            text = (f"The context is ~{ctx // 1000}k tokens{ctx_note} — close to the point where Antigravity "
                    "compacts the conversation (as soon as the next step won't fit) and drops details. Keep task.md "
                    "current so nothing gets lost"
                    + (", save to memory what a future conversation would need and isn't saved yet — how the servers "
                       "and deploys work, the cause of a stubborn failure, the user's preferences (rules: «Memory»)"
                       if main and setting("autoMemory") else "")
                    + (", and at the next natural pause suggest /handoff to continue in a fresh conversation (or the "
                       "user's /compact, which compacts this one at a moment they choose)." if main else "."))
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
