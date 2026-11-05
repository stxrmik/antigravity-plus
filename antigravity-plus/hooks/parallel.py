#!/usr/bin/env python3
"""Antigravity Plus — parallel work: keep the agent busy while its subagents and background commands run.

In Antigravity `invoke_subagent` returns at once and the subagent messages the agent when it's done; a run_command
that outlasts WaitMsBeforeAsync goes on in the background the same way. Agents still stop and wait («Жду
завершения ревью…»), poll (manage_subagents/manage_task list, short schedule timers, reading the subagent's
transcript), report results that haven't arrived yet, and do independent work one step at a time.
hooks/conversation.py tracks what the agent started and what reported back; this module turns that into short
ephemeral messages before the next model call (hooks/instructions.py):
  * right after it started subagents or a background command: what runs now, results arrive by themselves, keep
    working on what doesn't depend on them, never use a result before its message arrives;
  * when it polls anyway: stop polling;
  * when a subagent that changes files reports, or one reports while others still run: check the work before
    building on it; what still runs;
  * main agent, once per user message: a fresh task.md plan with PLAN_MIN+ open items and nothing delegated yet →
    launch subagents for the independent items in one step; SEARCH_RUN web-search-only steps in a row → hand the
    rest of the research to the researcher subagent.
For hooks/stop.py: when the main agent ends its turn while its own background work runs and task.md has open items,
stop_message() says what still runs and to do the items that don't depend on it.
Setting parallelHints (`memory.py config parallel-hints off`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from typing import List, Optional

import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PLAN_MIN = 3                # open task.md items before the plan hint
SEARCH_RUN = 3              # web-search-only steps in a row before the research hint
STALE_AGENT = 20 * 60       # a subagent whose transcript hasn't changed for this long isn't working any more
STALE_CMD = 3 * 3600
SHOWN = 6
EDITORS = ("worker", "debugger", "test-writer")  # subagents that change files: check their work
# Background commands meant to keep running (dev servers, watchers, tunnels): nobody waits for them to end.
SERVER = re.compile(
    r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|watch|preview)\b|\b(vite|nodemon|live-server|ngrok|"
    r"uvicorn|gunicorn|hypercorn|daphne|runserver|http\.server|webpack-dev-server)\b|\bnext\s+(dev|start)\b|"
    r"\b(flask|rails|hugo|jekyll|mkdocs)\s+(run|s|server|serve)\b|\bng\s+serve\b|\bphp\s+-S\b|--watch\b|"
    r"\btail\s+-[fF]\b|\bdocker[- ]compose\s+(\S+\s+)*up\b(?!.*\s(-d|--detach)\b)|\bssh\s+(\S+\s+)*-[NL]\b", re.I)
OPEN = '<background_work source="Antigravity Plus">'
CLOSE = "</background_work>"


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("parallelHints", True))
    except Exception:
        return True


def _ts(value) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _clip(text: str, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def kind(it: dict) -> str:
    """The subagent's type without a plugin prefix: antigravity-plus:worker → worker."""
    return str(it.get("t") or "").split(":")[-1].strip().lower()


def running(st: Optional[dict], now: Optional[float] = None) -> List[dict]:
    """Subagents and background commands this agent started that haven't reported back yet (timers aren't work)."""
    now = now or time.time()
    out = []
    for iid, it in ((st or {}).get("bg") or {}).items():
        if it.get("st") != "run" or it.get("k") not in ("agent", "cmd"):
            continue
        started = _ts(it.get("ts")) or now
        if it["k"] == "agent":
            try:
                last = os.path.getmtime(os.path.join(conv.BRAIN, iid, ".system_generated", "logs", "transcript_full.jsonl"))
            except OSError:
                last = started
            if now - max(last, started) > STALE_AGENT:
                continue
        elif now - started > STALE_CMD:
            continue
        server = it["k"] == "cmd" and bool(SERVER.search(it.get("r") or ""))
        out.append(dict(it, id=iid, mins=max(0, int((now - started) // 60)), srv=server))
    return sorted(out, key=lambda it: it.get("s", 0))


def busy(run: List[dict]) -> List[dict]:
    """The work worth waiting for: subagents and commands that end (not dev servers or watchers)."""
    return [it for it in run if not it.get("srv")]


def label(it: dict) -> str:
    if it.get("k") == "cmd":
        return f"command «{_clip(it.get('r') or 'background task', 60)}»"
    name = kind(it) or "subagent"
    role = _clip(it.get("r") or "", 50)
    return name + (f" «{role}»" if role and role.lower().replace("-", " ") != name.replace("-", " ") else "")


def names(items: List[dict]) -> str:
    return ", ".join(label(it) for it in items[:SHOWN]) + (", …" if len(items) > SHOWN else "")


def launched(run: List[dict]) -> str:
    agents = [it for it in run if it.get("k") == "agent"]
    if not agents:  # only commands: they go to the background often, so keep it short
        return (f"{OPEN}\nStill running in the background: {names(run)}. It messages you when it ends — don't poll "
                "it (manage_task list, sleep). Meanwhile do other work from the task; if you need its result to go on, "
                f"end your turn with one line — its message wakes you up.\n{CLOSE}")
    lines = [OPEN, "Working in the background for you now:"]
    for it in run[:SHOWN]:
        since = "started just now" if it.get("mins", 0) < 1 else f"running {it['mins']} min"
        lines.append(f"- {label(it)} — {since}" + (f" (id {it['id']})" if it.get("k") == "agent" else ""))
    if len(run) > SHOWN:
        lines.append(f"- … and {len(run) - SHOWN} more")
    lines.append("Their results arrive by themselves as messages: don't poll (no manage_subagents or manage_task "
                 "list, short schedule timers, sleep, or reading their transcripts), and never report or use a result "
                 "before its message has arrived.")
    lines.append("Keep working now on what doesn't depend on them: your own part of the task, reading the code their "
                 "results will touch, checks you can already run"
                 + (", subagents for other independent parts" if agents else "") + ". End your turn only when "
                 "everything left needs their results — then say in one line who you're waiting for; their message "
                 "wakes you up.")
    lines.append(CLOSE)
    return "\n".join(lines)


def polling(run: List[dict]) -> str:
    return (f"{OPEN}\nChecking on them doesn't make them faster: {names(run)} will message you when done. Do other "
            "work from the task now, or — if everything left needs their results — end your turn with one line "
            f"saying who you're waiting for.\n{CLOSE}")


def reported(done: List[dict], run: List[dict]) -> Optional[str]:
    agents = [it for it in done if it.get("k") == "agent"]
    editors = [it for it in agents if kind(it) in EDITORS]
    if not editors and not (agents and run):
        return None  # a finished search or review with nothing else running: its message says it all
    lines = [OPEN, f"Reported back: {names(agents)}."]
    if editors:
        lines.append("Before you build on it, check the work: open the files it changed and re-run its check — a "
                     "report isn't proof. If something is off, send_message it the correction (it keeps its context) "
                     "or fix it yourself.")
    lines.append(f"Still working: {names(run)}. Their results arrive by themselves." if run else "Nothing else is running.")
    lines.append(CLOSE)
    return "\n".join(lines)


def task_items(payload: dict) -> Optional[tuple]:
    """(open items, mtime) of the conversation's task.md, or None."""
    art = payload.get("artifactDirectoryPath") or os.path.join(conv.BRAIN, str(payload.get("conversationId") or ""))
    path = os.path.join(conv.to_path(art), "task.md")
    try:
        from stop import open_items
        with open(path, encoding="utf-8", errors="replace") as f:
            items = open_items(f.read(200_000))
        return items, os.path.getmtime(path)
    except (OSError, ImportError):
        return None


def plan_hint(payload: dict, st: dict, par: dict) -> Optional[str]:
    n = len(st["users"])
    if par.get("plan") == n:
        return None
    user = st["users"][-1]
    asked = _ts(user.get("ts") or "")
    got = task_items(payload) if asked else None
    if not got or got[1] < asked or len(got[0]) < PLAN_MIN:
        return None
    par["plan"] = n
    if any(it.get("k") == "agent" and it.get("s", -1) > user.get("step", -1) for it in (st.get("bg") or {}).values()):
        return None  # it already delegates in this turn
    return ('<parallel_plan source="Antigravity Plus">\n'
            f"Your plan has {len(got[0])} open items. Before you start, decide which of them can run at the same time. "
            "An item that doesn't need another item's result, has its own files and takes more than a few tool calls "
            "can go to a subagent: `explorer` (search the code), `researcher` (web, docs, APIs), `worker` (build a part "
            "in its own files), `test-writer` (tests for settled code), `code-reviewer` / `security-auditor` (review). "
            "Launch them all in ONE step with full briefs, mark those items «— <agent>» in task.md, and meanwhile do "
            "the item the others depend on yourself. Keep small items, items on the same files and the user's "
            "decisions in your own hands; if nothing here is independent, just carry on.\n</parallel_plan>")


def research_hint(st: dict, par: dict) -> Optional[str]:
    n = len(st["users"])
    runs = (st.get("calls") or {}).get("search_run", 0)
    if runs < SEARCH_RUN or par.get("research") == n:
        return None
    par["research"] = n
    if any(kind(it) == "researcher" and it.get("st") == "run" for it in (st.get("bg") or {}).values()):
        return None
    return ('<parallel_hint source="Antigravity Plus">\n'
            f"That's {runs} rounds of web search in a row. If more research is needed, hand it to the `researcher` "
            "subagent (invoke_subagent, TypeName researcher, Model flash) with the exact questions and what the answer "
            "is for, and meanwhile read or change the code it's for. If you already have what you need, carry on."
            "\n</parallel_hint>")


def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """The ephemeral message for this model call, or None. Updates st["par"] (the caller stores st)."""
    if not st or not setting():
        return None
    par = st.setdefault("par", {})
    bg = st.get("bg") or {}
    run = busy(running(st))
    key = lambda i, it: f"{i}@{it.get('s', 0)}"  # a follow-up via send_message is a new run of the same subagent
    known = {key(i, it) for i, it in bg.items()}
    seen = set(par.get("seen") or [])
    told = set(par.get("done") or [])
    new = [it for it in run if key(it["id"], it) not in seen]
    done = [dict(it, id=i) for i, it in bg.items()
            if it.get("st") == "done" and key(i, it) in seen and key(i, it) not in told]
    par["seen"] = sorted((seen | {key(it["id"], it) for it in run}) & known)
    par["done"] = sorted((told | {key(it["id"], it) for it in done}) & known)
    poll_step = (st.get("calls") or {}).get("poll_step", -1)
    polled = poll_step > par.get("poll", -1)
    par["poll"] = max(par.get("poll", -1), poll_step)

    if new:
        return launched(run)
    if run and polled:
        return polling(run)
    msg = reported(done, run) if done else None
    if msg or not st.get("users"):
        return msg
    return plan_hint(payload, st, par) or research_hint(st, par)


def stop_message(run: List[dict], items: list, shown: int = 8) -> str:
    """For hooks/stop.py: the main agent ended its turn with open task.md items while its background work runs."""
    who = names(run) if run else "background tasks you started"
    verb = "is" if len(run) == 1 else "are"
    listed = "\n".join(f"- [{mark}] {text}" for mark, text in items[:shown])
    more = f"\n… and {len(items) - shown} more" if len(items) > shown else ""
    return (f"Antigravity Plus: you ended your turn while {who} {verb} still working in the background. Their "
            "results arrive by themselves as messages — you don't need to wait for them. task.md still has open items:\n"
            f"{listed}{more}\n"
            "Do the ones that don't depend on their results now and keep task.md updated. If everything left needs "
            "their results, end your turn again with one line saying who you're waiting for.")
