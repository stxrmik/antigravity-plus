#!/usr/bin/env python3
"""Antigravity Plus — progress updates: the user hears what the agent found while it works, not only at the end.

In Antigravity a response without a tool call ends the turn, so Gemini models learn to work in silence: in real
conversations (Nov 2026) 1–2 % of their tool-calling responses carried a word for the user, and a background build
that failed was re-run five times with only «Waiting for task-68» to show for it («Ну и че ты встал, у тебя build
failed»). Text written beside a tool call is shown to the user and keeps the turn going — that is the interim update
Claude gives. hooks/instructions.py asks for one, main agent only (not subagents, not tasks from Claude Code), when:
  * a background command the agent started finished with a failure (hooks/conversation.py: bg[id]["fail"]): read
    the error before re-running it, and tell the user what failed and what changes — once per command;
  * QUIET tool-calling responses in a row went without a word to the user (calls["quiet"]): one or two sentences on
    what it found or finished and what's next — again after QUIET_AGAIN more in the same silent run.
At most PER_MESSAGE hints per user message: every injected block stays in the context until compaction.
Setting progressHints (`memory.py config progress-hints off`). The rule itself: rules/AGENTS.md «Progress updates».
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import conversation as conv

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUIET = 8           # tool-calling responses without a word before the first hint
QUIET_AGAIN = 20    # more of them in the same silent run before the next one
PER_MESSAGE = 3     # hints per user message
OPEN, CLOSE = '<progress_update source="Antigravity Plus">', "</progress_update>"
SAME_RESPONSE = ("Write it as text in the same response as your next tool call — a response without a tool call "
                 "ends your turn.")


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("progressHints", True))
    except Exception:
        return True


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def new_failures(st: dict, pr: dict) -> list:
    """Background commands of this agent that finished with a failure since the last look; marks them seen."""
    told = set(pr.get("fail") or [])
    failed = [(i, it) for i, it in (st.get("bg") or {}).items()
              if it.get("k") == "cmd" and it.get("st") == "done" and it.get("fail") and f"{i}@{it.get('s', 0)}" not in told]
    if failed:
        pr["fail"] = sorted(told | {f"{i}@{it.get('s', 0)}" for i, it in failed})[-40:]
    return failed


def failed_hint(failed: list) -> Optional[str]:
    """A background command failed: read the error first, then tell the user."""
    if not failed:
        return None
    i, it = failed[-1]
    more = f" (and {len(failed) - 1} more)" if len(failed) > 1 else ""
    return (f"{OPEN}\nBackground command «{_clip(it.get('r'), 70)}»{more} finished with a failure: "
            f"«{_clip(it.get('fail'), 100)}». Read the error before you run anything again (its log is named in its "
            "message) and find the cause: re-running it unchanged gives the same result. Tell the user in one or two "
            f"sentences what failed and what you'll do about it. {SAME_RESPONSE}\n{CLOSE}")


def quiet_hint(st: dict, pr: dict) -> Optional[str]:
    """QUIET+ tool-calling responses in a row without a word to the user."""
    calls = st.get("calls") or {}
    quiet, since = int(calls.get("quiet") or 0), calls.get("quiet_from")
    if quiet < QUIET:
        return None
    if pr.get("q_from") == since and quiet < int(pr.get("q_n") or 0) + QUIET_AGAIN:
        return None
    pr.update(q_from=since, q_n=quiet)
    return (f"{OPEN}\nYou've made {quiet} tool calls in a row without a word to the user. Give them a short "
            "update now: one or two sentences, in their language, on what you've found or finished and what you're "
            f"doing next — results with numbers, not «Сейчас сделаю…» alone. {SAME_RESPONSE} Then keep working.\n{CLOSE}")


def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """The hint for this model call, or None. Updates st["prog"] (the caller stores st)."""
    if not st or not st.get("users"):
        return None
    pr = st.setdefault("prog", {})
    failed = new_failures(st, pr)  # seen even when no hint goes out: an old failure is no news later
    if conv.delegated(st, payload) or not setting():
        return None
    turn = len(st["users"])
    if pr.get("msg") != turn:
        pr.update(msg=turn, n=0)
    if int(pr.get("n") or 0) >= PER_MESSAGE:
        return None
    msg = failed_hint(failed) or quiet_hint(st, pr)
    if msg:
        pr["n"] = int(pr.get("n") or 0) + 1
    return msg
