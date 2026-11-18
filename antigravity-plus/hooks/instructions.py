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
problems hooks/check_edit.py found in files written since the previous step, the other agents working in the
project (hooks/coord.py), hints that keep it working while its subagents and background commands run
(hooks/parallel.py), and hints on the sandbox, the context budget and loops of failing commands (hooks/speed.py).
The main agent also hears when the plan's limits of its model get short (hooks/quota.py).
When the user switches Bypass Mode («/bypass», «/bypass off»), this hook applies it for every conversation
(hooks/conversation.py) and tells the agent once; a switch made in another conversation is mentioned once too.
The main agent also gets, with a new user message, the saved memories it is about, and hears when the message sounds
worth remembering (hooks/memory_hints.py), asks for facts it has to look up and cite (hooks/facts.py), or calls for
a straight answer — an objection, a guess at a cause, a request for an opinion (hooks/objective.py).

Contract: JSON on stdin (conversationId, transcriptPath, lastUserInput, invocationNum, …) →
{"injectSteps": [{"systemMessage": {"systemMessage": "…"}}]} (optionally with
"metadata": {"user_markdown": "…"}) or {}. Never fails loudly. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, List, Optional, Tuple, Union

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


BYPASS_ON = ("The user turned Bypass Mode ON (message {n}). From now on, in every conversation and until they turn it "
             "off, Antigravity Plus approves your commands, edits and reads itself — no permission prompts. It still "
             "asks the user before system-level actions: sudo or admin rights, system folders and OS settings, disks, "
             "shutdown, autostart, the Keychain and private SSH keys, wiping the home folder or a disk, and changes to "
             "your own permissions; their explicit bans («не пушь») still hold. Your rules don't change: commits, "
             "pushes, deploys and deleting data still need the user's request. Confirm in one line, in the user's "
             "language, and say it stays on until «/bypass off».")
BYPASS_OFF = ("The user turned Bypass Mode OFF (message {n}): permission prompts are back as before, in every "
              "conversation. Confirm in one line, in the user's language.")
BYPASS_ELSEWHERE = ("Bypass Mode was turned {state} in another conversation: {what}")
BYPASS_UI_ON = ("🛡️ **Antigravity Plus · Bypass Mode включён** — запросы разрешений отключены во всех разговорах "
                "(кроме системных действий и ваших запретов). Выключить: `/bypass off`")
BYPASS_UI_OFF = "🔒 **Antigravity Plus · Bypass Mode выключен** — стандартные запросы разрешений возвращены."


def make_step(model_text: str = "", user_md: str = "") -> dict:
    """Build an Antigravity 2.32 injectSteps entry (systemMessage + optional UI-only metadata.user_markdown)."""
    body: dict = {"systemMessage": model_text}
    if user_md:
        body["metadata"] = {"user_markdown": user_md}
    return {"systemMessage": body}


def ui_notices_enabled() -> bool:
    return conv.ui_notices_enabled()


def bypass_work(payload: dict) -> Optional[Union[str, Tuple[str, str]]]:
    """Bypass Mode (hooks/conversation.py): apply a switch the user just typed here, and tell the agent once when it
    changed — here or in another conversation. Subagents and Claude Code's delegated agents are never told."""
    st = conv.load(payload)
    if not st or not st["users"] or conv.delegated(st, payload):
        return None
    users = conv.with_current(st, payload)
    before = int(st.get("bypass_applied") or 0)
    now = "on" if conv.bypass_mode(payload, st, users) else "off"
    msg = None
    user_md = ""
    if int(st.get("bypass_applied") or 0) > before:
        ev = next(e for e in reversed(conv.bypass_events(users)) if e["n"] == st["bypass_applied"])
        if (now == "on") == ev["on"]:  # unless a later switch from another conversation won
            msg = (BYPASS_ON if ev["on"] else BYPASS_OFF).format(n=ev["n"])
            user_md = BYPASS_UI_ON if ev["on"] else BYPASS_UI_OFF
    if msg is None and st.get("bypass_seen") not in (None, now):
        msg = BYPASS_ELSEWHERE.format(state=now.upper(), what=(
            "Antigravity Plus approves your commands and edits itself and asks the user only before system-level "
            "actions (rules: \"Bypass Mode\")." if now == "on" else "permission prompts are back as before."))
    if st.get("bypass_seen") != now:
        st["bypass_seen"] = now
        conv.store(payload["conversationId"], st)
    if msg and user_md:
        return msg, user_md
    return msg


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


def parallel_work(payload: dict) -> Optional[str]:
    """What runs in the background for this agent and how not to idle meanwhile (subagents included)."""
    import parallel
    st = conv.load(payload)
    msg = parallel.message(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def orchestrator_work(payload: dict) -> Optional[Union[str, Tuple[str, str]]]:
    """Orchestrator planner/executor handoff hints (hooks/orchestrator.py, when available)."""
    try:
        import orchestrator  # type: ignore[import-not-found]
    except ImportError:
        return None
    st = conv.load(payload)
    before = json.dumps(st, sort_keys=True, ensure_ascii=False) if st is not None else None
    fn = getattr(orchestrator, "handoff_message", None)
    msg: Any = fn(payload, st) if callable(fn) else None
    if st is not None and isinstance(payload.get("conversationId"), str):
        if json.dumps(st, sort_keys=True, ensure_ascii=False) != before:
            conv.store(payload["conversationId"], st)
    return msg


def speed_work(payload: dict) -> Optional[str]:
    """Sandbox memory, the context budget and loops of failing commands (subagents included)."""
    import speed
    st = conv.load(payload)
    msg = speed.message(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def quota_work(payload: dict) -> Optional[Tuple[str, str]]:
    """The plan's limits of the conversation's model when they get short (main agent only; hooks/quota.py)."""
    import quota
    st = conv.load(payload)
    msg = quota.message_with_ui(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def lsp_work(payload: dict) -> Optional[str]:
    """The agent searched code for symbol names with rg twice: once per conversation, the language server's commands
    (skills/lsp; subagents included)."""
    import os
    st = conv.load(payload)
    sym = ((st or {}).get("calls") or {}).get("sym") or {}
    if not st or sym.get("n", 0) < 2 or st.get("lsp_told"):
        return None
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "lsp", "scripts")
    import check_edit
    if not os.path.isfile(os.path.join(folder, "lsp.py")) or not check_edit.enabled("lspChecks"):
        return None
    sys.path.insert(0, folder)
    import lsp
    ws = next((conv.to_path(w) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), "")
    msg = lsp.search_hint(ws, sym.get("names") or []) if ws else None
    st["lsp_told"] = True
    if isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def memory_work(payload: dict) -> Optional[str]:
    """Memory for a new user message (main agent only; hooks/memory_hints.py): the saved memories it is about, and
    whether it sounds lasting enough to save along the way."""
    import memory_hints
    st = conv.load(payload)
    parts = []
    # a task from Claude Code gets what memory knows, but no nudges to save: that memory is Claude Code's too
    for part in (memory_hints.recall,) + (() if conv.delegated(st, payload) else (memory_hints.message,)):
        try:
            parts.append(part(payload, st))
        except Exception as e:  # one half failing must not hide the other
            print(f"instructions: {part.__name__}: {e}", file=sys.stderr)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return "\n".join(p for p in parts if p) or None


def facts_work(payload: dict) -> Optional[str]:
    """A question that needs facts from outside or from the code: look it up and cite the source (hooks/facts.py)."""
    import facts
    st = conv.load(payload)
    msg = facts.message(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def objective_work(payload: dict) -> Optional[str]:
    """A message that disputes what the agent said, guesses at a cause, asks for its opinion or is worded to get a
    «да»: what an honest answer to it takes (hooks/objective.py)."""
    import objective
    st = conv.load(payload)
    msg = objective.message(payload, st)
    if st is not None and isinstance(payload.get("conversationId"), str):
        conv.store(payload["conversationId"], st)
    return msg


def main() -> None:
    steps = []
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = None
    show_ui = ui_notices_enabled()
    if isinstance(payload, dict):
        for part in (bypass_work, decide, edit_checks, other_agents, parallel_work, orchestrator_work, speed_work,
                     quota_work, lsp_work, memory_work, facts_work, objective_work):
            try:
                res = part(payload)
            except Exception as e:  # a hook must never break the conversation
                print(f"instructions: {part.__name__}: {e}", file=sys.stderr)
                res = None
            if isinstance(res, tuple):
                model_text = str(res[0] or "")
                user_md = str(res[1] or "") if show_ui else ""
            elif isinstance(res, str):
                model_text, user_md = res, ""
            else:
                model_text, user_md = "", ""
            if model_text or user_md:
                steps.append(make_step(model_text, user_md))
    sys.stdout.write(json.dumps({"injectSteps": steps} if steps else {}, ensure_ascii=False))


if __name__ == "__main__":
    main()
