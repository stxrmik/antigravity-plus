#!/usr/bin/env python3
"""Antigravity Plus — PreInvocation hook: keeps the user's instructions in view.

In long conversations early instructions ("самое главное — ничего не пушь", "делай всё в Docker") end up under
hundreds of tool results, and Antigravity's context compaction keeps only a summary of them. Before a model call
this hook re-shows the user's own messages from this conversation — the first request, every ⚠ constraint, the
latest ones — and the restrictions the guard enforces, as an ephemeral message (it is not kept in the
conversation). It fires only when that helps:
  * a new user message arrived and there are earlier ones (connect it with what was said before);
  * the context was compacted since the last reminder;
  * a long turn: LONG_TURN+ steps since the user last wrote, at most every REFRESH steps.
Subagents (no user messages in their transcript) get nothing of that. Everyone, subagents included, also gets the
problems hooks/check_edit.py found in files written since the previous step.

Contract: JSON on stdin (conversationId, transcriptPath, lastUserInput, invocationNum, …) →
{"injectSteps": [{"ephemeralMessage": "…"}]} or {}. Never fails loudly. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import re
import sys
from typing import List, Optional

import conversation as conv

LONG_TURN = 30      # steps since the last user message before a refresher
REFRESH = 30        # min steps between refreshers
BUDGET = 3600       # chars of quoted messages
FIRST_LAST = 900    # chars kept of the first and the latest message
OTHER = 360         # chars kept of any other message
META_ONLY = re.compile(r"^\s*/(usage|cost|doctor|export|memory)\b", re.I)


def clip(text: str, n: int) -> str:
    t = re.sub(r"\s*\n\s*", " ⏎ ", text.strip())
    t = re.sub(r"[ \t]+", " ", t)
    return t if len(t) <= n else t[: n - 1] + "…"


ACCESS_ON = ("Full access is ON{how}: run any plugin command yourself without asking — /commit, /pr, /deploy, "
             "/fix-issue, /simplify, /init, /export, /hooks, AGENTS.md edits — and push or deploy when the task needs "
             "it. The user's explicit restrictions still win, destructive operations still stop at the guard, and you "
             "say in one line what you ran.")
ACCESS_OFF = "Full access was revoked{how}: on-request commands (/commit, /pr, /deploy …) need the user's request again."


def access_line(access: Optional[dict], revoked: Optional[dict]) -> Optional[str]:
    if access:
        how = (f" (message {access['n']})" if access.get("n") else f" for this project ({access.get('source', '')})")
        return ACCESS_ON.format(how=how)
    if revoked:
        return ACCESS_OFF.format(how=f" in message {revoked['n']}")
    return None


def render(users: List[dict], bans: dict, reason: str, compactions: int = 0, handoff_hint: bool = False,
           access: Optional[str] = None) -> str:
    n = len(users)
    last = n - 1
    limit = lambda i: FIRST_LAST if i in (0, last) else OTHER
    flagged = [i for i in range(n) if conv.is_constraint(users[i]["text"])]
    keep = {0, last}
    budget = BUDGET - sum(len(clip(users[i]["text"], limit(i))) for i in keep)
    for i in list(reversed(flagged)) + list(range(last - 1, 0, -1)):  # constraints first, then the most recent
        if i in keep:
            continue
        cost = len(clip(users[i]["text"], limit(i))) + 8
        if cost <= budget:
            keep.add(i)
            budget -= cost

    out = ['<user_instructions source="Antigravity Plus">',
           "The user's own messages in this conversation, oldest first — re-read them before deciding what to do "
           "next. The last one is the current request. Instructions from earlier messages, especially ⚠ "
           "constraints (\"don't…\", \"only…\", \"until I say…\"), still apply unless a later message changed them."]
    if reason == "compaction":
        out.append("The context was just compacted: check your summary and plan against these messages.")
        if handoff_hint:
            out.append(f"This conversation has been compacted {compactions} times. If a lot of work remains, suggest "
                       "once, in one line at a natural pause, that the user saves /handoff and continues in a new "
                       "conversation (cheaper, clean context).")
    elif reason == "long_turn":
        out.append("You have been working for a while: make sure you are still doing what was asked, within these "
                   "constraints, and that nothing requested has been dropped.")
    if access:
        out.append(access)
    if bans:
        what = {"push": "push / deploy / upload to servers", "commit": "git commit"}
        out.append("In force now (the guard asks the user before these): "
                   + "; ".join(f"no {what[k]} — message {v['n']}" for k, v in bans.items()) + ".")
    prev = -1
    for i in sorted(keep):
        if i - prev > 1:
            out.append(f"   … {i - prev - 1} more message(s) not shown")
        mark = "⚠ " if i in flagged and i != last else ""
        tag = " (current request)" if i == last and n > 1 else ""
        out.append(f"{i + 1}.{tag} {mark}«{clip(users[i]['text'], limit(i))}»")
        prev = i
    out.append("</user_instructions>")
    return "\n".join(out)


def decide(payload: dict) -> Optional[str]:
    st = conv.load(payload)
    if not st or not st["users"]:
        return None  # subagent or nothing recorded yet
    users = conv.with_current(st, payload)

    inj = st.get("injected") or {}
    n = len(users)
    last_cp = st["checkpoints"][-1] if st["checkpoints"] else -1
    access = conv.full_access(payload, st, users)
    events = conv.access_events(users)
    revoked = events[-1] if events and not events[-1]["on"] else None
    access_key = (f"on:{access['n']}:{access.get('source')}" if access else (f"off:{revoked['n']}" if revoked else ""))
    reason = None
    if access_key != inj.get("access", ""):
        reason = "access"
    elif last_cp > inj.get("checkpoint", -1):
        reason = "compaction"
    elif n > inj.get("users", 0) and n >= 2 and not META_ONLY.match(users[-1]["text"]):
        reason = "new_message"
    elif (st["last_step"] - users[-1]["step"] >= LONG_TURN
          and st["last_step"] - inj.get("step", -10**9) >= REFRESH):
        reason = "long_turn"
    if not reason:
        if n > inj.get("users", 0):  # remember we've seen it even when we stay quiet
            st["injected"] = {**inj, "users": n}
            conv.store(payload["conversationId"], st)
        return None
    hint = reason == "compaction" and len(st["checkpoints"]) >= 2 and not inj.get("handoff_hint")
    st["injected"] = {**inj, "users": n, "checkpoint": last_cp, "step": st["last_step"], "reason": reason,
                      "handoff_hint": inj.get("handoff_hint") or hint, "access": access_key}
    conv.store(payload["conversationId"], st)
    return render(users, conv.active_bans(users), reason, len(st["checkpoints"]), hint, access_line(access, revoked))


def other_agents(payload: dict) -> Optional[str]:
    """Heartbeat for hooks/coord.py, and the other agents working in this project when that picture changes."""
    import coord
    coord.heartbeat(payload)
    st = conv.load(payload)
    msg = coord.others_message(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def edit_checks(payload: dict) -> Optional[str]:
    """Problems hooks/check_edit.py found in files written since the last step (subagents included)."""
    import check_edit
    return check_edit.take_message(payload)


def main() -> None:
    steps = []
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = None
    for part in (decide, edit_checks, other_agents) if isinstance(payload, dict) else ():
        try:
            msg = part(payload)
        except Exception as e:  # a hook must never break the conversation
            print(f"instructions: {part.__name__}: {e}", file=sys.stderr)
            msg = None
        if msg:
            steps.append({"ephemeralMessage": msg})
    sys.stdout.write(json.dumps({"injectSteps": steps} if steps else {}, ensure_ascii=False))


if __name__ == "__main__":
    main()
