#!/usr/bin/env python3
"""Antigravity Plus — what the user has said in this conversation.

Reads the conversation transcript incrementally (only bytes added since the last call) and keeps the user's
messages in ~/.gemini/antigravity-plus/state/<conversationId>.json. Shared by:
  * hooks/instructions.py (PreInvocation) — re-shows the user's instructions so they survive long turns and
    context compaction;
  * hooks/guard.py (PreToolUse) — asks before pushing/deploying/committing when the user said not to.
Also detects standing restrictions ("не пушь", "don't commit") and their lifting ("пушь", "commit it"), and tracks
the background work the agent started — subagents, commands that went to the background, timers — what reported
back and how the agent waits, for hooks/parallel.py.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import compat

STATE_DIR = os.path.expanduser("~/.gemini/antigravity-plus/state")
BRAIN = os.path.expanduser("~/.gemini/antigravity/brain")
MSG_LIMIT = 3000          # chars kept per user message
STATE_TTL_DAYS = 14

HEAD = re.compile(rb'"type":\s*"(USER_INPUT|CHECKPOINT|PLANNER_RESPONSE)"')
STEP = re.compile(rb'"step_index":\s*(\d+)')
# Background work (hooks/parallel.py): invoke_subagent results, commands and timers sent to the background, messages
# back from them, and the ways agents wait for them.
BG_HEAD = re.compile(rb'"type":\s*"(PLANNER_RESPONSE|GENERIC|SYSTEM_MESSAGE)"')
CREATED = b"Created the following subagent"
BG_STARTED = b"running as a background task"
BG_TASK = re.compile(r"running as a background task with task id:\s*(\S+)\s*\n\s*Task Description:\s*([^\n]*)")
AGENT_ID = re.compile(r'"conversationId":\s*"([\w-]{8,80})"')
REPORT = re.compile(rb"\[Message\] timestamp=(\S+) sender=([\w./:-]+)")
GONE_IDLE = re.compile(rb"Subagent ([\w-]{8,80}) has gone idle")
STATUS_CHECK = re.compile(r"\b(status|progress|done yet|are you done|finished yet|ready yet|answer now|provide (your|the) "
                          r"(answer|result|report)|how(?:'s| is) it going)\b|как\s+(дела|успехи|прогресс)|готов[оа]?\s*\?|"
                          r"\bстатус|\bну\s+что\b", re.I)
WAIT_CMD = re.compile(r"^\s*(sleep\s+\d|timeout\s+(/t\s+)?\d|start-sleep\b|echo\s+[\"']?(waiting|wait\b|жду))", re.I)
POLL_TIMER = 180          # schedule timers shorter than this, set while work is running, are polling
BG_KEEP = 60              # finished entries kept in the state
REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
TAGS = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|[A-Z_]+_METADATA)>.*?</\1>", re.S)

# Standing instructions worth highlighting (negations, scope limits, "always/only", "until I say").
CONSTRAINT = re.compile(
    r"\bне\s+(надо|нужно|стоит|трогай|меняй|удаляй|ломай|пушь|пуш|коммить|деплой|заливай|запускай|ставь|используй|"
    r"делай|добавляй|переписывай|забудь)|\b(ничего\s+)?не\s+\w+(й|йте|ь|ьте)\b|\bникогда\b|\bвсегда\b|"
    r"\bтолько\b|\bбез\s+\w+|\bпока\s+(что\s+)?(не|без)\b|\bпока\s+я\s+не\b|\bсамое\s+главное\b|\bважно\b|"
    r"\bобязательно\b|\bнельзя\b|\bзапрещ\w*|\bвс[её]\s+внутри\b|\bделай\s+вс[её]\b|"
    r"\b(don'?t|do\s+not|never|always|only|must|make\s+sure|without|until\s+i|no\s+more)\b", re.I)

_PUSH_VERB = r"((за)?(пуш\w*|деплои\w*|деплой\w*)|зале(й|йте)\b|зали(ть|вай)\w*|залив\w*|выкат\w*|опублику\w*|публику\w*)"
BANS = {
    "push": (
        re.compile(r"\bне\s+(надо\s+|нужно\s+|стоит\s+|будем\s+)?" + _PUSH_VERB +
                   r"|\bне\s+трога\w*\s+(сервер|прод)\w*|\bбез\s+(пуша|деплоя|выкатки)\b"
                   r"|\b(don'?t|do\s+not|never)\s+(git\s+)?(push|deploy|ship|publish|release)\w*"
                   r"|\bnot\s+(push|deploy)\w*\s+yet\b|\b(stay|keep\s+it|work)\s+local(ly)?\b", re.I),
        re.compile(r"^\W*(ну|теперь|давай|можно|ок(ей)?|го|всё|все|отлично|супер)?[\s,!]*" + _PUSH_VERB +
                   r"(\s*$|[\s,.!]+(и|на|в|сейчас|теперь|уже|всё|все|это|изменения)\b|[.!]+\s*$)"
                   r"|\b(можно|можешь|давай|разрешаю)\s+(за)?(пушить|пушнуть|деплоить|задеплоить|заливать|залить|"
                   r"выкатывать|выкатить|публиковать)\b|\bна\s+сервер\s+(за)?ле(й|вай)\b|^\s*/pr\b"
                   r"|^\W*(ok|okay|now|go\s+ahead( and)?)?[\s,]*(push|deploy|ship\s+it|publish)\b"
                   r"|\b(you\s+can|go\s+ahead\s+and|please)\s+(push|deploy)\b", re.I),
    ),
    "commit": (
        re.compile(r"\bне\s+(надо\s+|нужно\s+|стоит\s+)?(за)?коммит\w*|\bбез\s+коммит\w*"
                   r"|\b(don'?t|do\s+not|never)\s+commit\w*", re.I),
        re.compile(r"^\W*(ну|теперь|давай|можно|ок(ей)?)?[\s,]*(за)?коммит(ь|ьте|им)\b|^\s*/(commit|pr)\b"
                   r"|\b(можно|можешь|давай)\s+(за)?коммит\w*|^\W*(ok|now)?[\s,]*commit\b|\bgo\s+ahead\s+and\s+commit\b",
                   re.I),
    ),
}


def to_path(p: str) -> str:
    """A workspace path from a file:// URI or a plain path; file:///c%3A/proj on Windows → c:\\proj."""
    if not p.startswith("file://"):
        return os.path.expanduser(p)
    path = unquote(urlparse(p).path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if compat.WINDOWS else path


def transcript_path(payload: dict) -> Optional[str]:
    cands = []
    p = payload.get("transcriptPath")
    if isinstance(p, str) and p:
        p = to_path(p)
        if p.endswith("/transcript.jsonl"):
            cands.append(p[: -len("transcript.jsonl")] + "transcript_full.jsonl")
        cands.append(p)
    cid = payload.get("conversationId")
    if isinstance(cid, str) and re.fullmatch(r"[\w-]{8,80}", cid):
        cands.append(os.path.join(BRAIN, cid, ".system_generated", "logs", "transcript_full.jsonl"))
    return next((c for c in cands if os.path.isfile(c)), None)


def user_text(content: str) -> str:
    m = REQUEST.search(content or "")
    text = m.group(1) if m else TAGS.sub("", content or "")
    return text.strip()[:MSG_LIMIT]


def _fresh(tp: str) -> dict:
    return {"transcript": tp, "offset": 0, "users": [], "checkpoints": [], "last_step": -1, "injected": {}}


def load(payload: dict, save: bool = True) -> Optional[dict]:
    """State with every user message so far: {"users": [{"step", "ts", "text"}], "checkpoints": [step, …],
    "last_step": int, "injected": {…}}. None when there's no transcript to read."""
    cid = payload.get("conversationId")
    if not isinstance(cid, str) or not re.fullmatch(r"[\w-]{8,80}", cid):
        return None
    tp = transcript_path(payload)
    if not tp:
        return None
    sp = os.path.join(STATE_DIR, cid + ".json")
    try:
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = _fresh(tp)
    if not isinstance(st, dict) or not all(k in st for k in ("offset", "users", "checkpoints", "last_step")):
        st = _fresh(tp)
    try:
        size = os.path.getsize(tp)
    except OSError:
        return st
    if st.get("transcript") != tp or st.get("offset", 0) > size:
        st = _fresh(tp)
    if size == st["offset"]:
        return st
    with open(tp, "rb") as f:
        f.seek(st["offset"])
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return st
    for raw in chunk[:end].split(b"\n"):
        sm = STEP.search(raw[:120])
        if sm:
            st["last_step"] = max(st["last_step"], int(sm.group(1)))
        bm = BG_HEAD.search(raw[:400])
        if bm:
            try:
                track_background(st, bm.group(1), raw)
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
        hm = HEAD.search(raw[:400])
        if not hm or (hm.group(1) == b"PLANNER_RESPONSE" and b'"tool_calls"' in raw):
            continue  # agent steps matter only as the text the user answered
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        step = d.get("step_index", st["last_step"])
        if d.get("type") == "USER_INPUT":
            text = user_text(d.get("content") or "")
            if text:
                st["users"].append({"step": step, "ts": d.get("created_at", ""), "text": text,
                                    "prev_agent": st.get("last_agent", "")})
                st.setdefault("calls", {})["search_run"] = 0
        elif d.get("type") == "CHECKPOINT":
            st["checkpoints"].append(step)
        elif d.get("type") == "PLANNER_RESPONSE" and (d.get("content") or "").strip():
            st["last_agent"] = d["content"][-500:]
    st["offset"] += end + 1
    if save:
        store(cid, st)
    return st


def track_background(st: dict, kind: bytes, raw: bytes) -> None:
    """Keep st["bg"] = {id: {"k": "agent"|"cmd"|"timer", "t": type, "r": role or description, "s": step, "ts": started,
    "st": "run"|"done"|"killed", "end": ts}} and st["calls"] = {"search_run": web-search-only steps in a row,
    "poll_step": the last step that only checked on running work}."""
    bg = st.setdefault("bg", {})
    calls = st.setdefault("calls", {})
    if kind == b"SYSTEM_MESSAGE":
        for m in REPORT.finditer(raw):
            it = bg.get(m.group(2).decode())
            if it and it.get("st") == "run":
                it.update(st="done", end=m.group(1).decode())
        for m in GONE_IDLE.finditer(raw):
            it = bg.get(m.group(1).decode())
            if it and it.get("st") == "run":
                it.update(st="done", end="")
        return
    if kind == b"GENERIC":
        if CREATED not in raw and BG_STARTED not in raw:
            return
        d = json.loads(raw)
        content = d.get("content") or ""
        head = content[:300]  # the tool's own result starts with it; deeper down it's a file or log being read
        base = {"s": d.get("step_index", st["last_step"]), "ts": d.get("created_at", ""), "st": "run"}
        if CREATED.decode() in head:
            pending = st.get("bg_pending") or []
            for cid in AGENT_ID.findall(content[:3000]):
                spec = pending.pop(0) if pending else {}
                bg[cid] = dict(base, k="agent", t=spec.get("t", ""), r=spec.get("r", ""))
            st["bg_pending"] = pending
        m = BG_TASK.search(content[:800]) if BG_STARTED.decode() in head else None
        if m:
            desc = m.group(2).strip()
            bg[m.group(1)] = dict(base, k="timer" if desc.startswith("Timer:") else "cmd", t="", r=desc[:120])
        _prune_bg(bg)
        return
    if b'"tool_calls"' not in raw:
        return
    d = json.loads(raw)
    tcs = [c for c in d.get("tool_calls") or [] if isinstance(c, dict)]
    step = d.get("step_index", st["last_step"])
    names = [str(c.get("name") or "") for c in tcs]
    pending: List[dict] = []
    polling = bool(tcs) and any(it.get("st") == "run" and it.get("k") in ("agent", "cmd") for it in bg.values())
    for name, c in zip(names, tcs):
        a = c.get("args") if isinstance(c.get("args"), dict) else {}
        action = str(a.get("Action") or "list").lower()
        if name == "invoke_subagent":
            specs = a.get("Subagents") if isinstance(a.get("Subagents"), list) else [a]
            pending += [{"t": str(s.get("TypeName") or ""), "r": str(s.get("Role") or "")[:80]}
                        for s in specs if isinstance(s, dict)]
            polling = False
        elif name == "send_message":
            it = bg.get(str(a.get("Recipient") or ""))
            if it and it.get("st") == "run" and STATUS_CHECK.search(str(a.get("Message") or "")):
                continue  # «are you done yet?» is polling
            polling = False
            if it and it.get("k") == "agent" and it.get("st") != "run":
                it.update(st="run", s=step, ts=d.get("created_at", ""), end="")  # follow-up work for it
        elif name == "manage_subagents":
            if action in ("kill", "kill_all"):
                polling = False
                ids = a.get("ConversationId")
                ids = [str(i) for i in ids] if isinstance(ids, list) else [str(ids or "")]
                for cid, it in bg.items():
                    if it.get("k") == "agent" and it.get("st") == "run" and (action == "kill_all" or cid in ids):
                        it["st"] = "killed"
        elif name == "manage_task":
            if action == "kill":
                polling = False
                it = bg.get(str(a.get("TaskId") or ""))
                if it:
                    it["st"] = "killed"
            elif action not in ("list", "status"):
                polling = False
        elif name == "schedule":
            try:
                secs = float(a.get("DurationSeconds") or 0)
            except (TypeError, ValueError):
                secs = 0.0
            if not 0 < secs < POLL_TIMER:
                polling = False
        elif name == "read_file":
            path = str(a.get("Path") or "").replace("\\", "/")
            if not ("/antigravity/brain/" in path and "transcript" in path.rsplit("/", 1)[-1]):
                polling = False  # reading a subagent's transcript to see how it's doing is polling
        elif not (name == "run_command" and WAIT_CMD.search(str(a.get("CommandLine") or ""))):
            polling = False
    st["bg_pending"] = pending
    if polling:
        calls["poll_step"] = step
    calls["search_run"] = calls.get("search_run", 0) + 1 if tcs and all(n == "search_web" for n in names) else 0


def _prune_bg(bg: dict) -> None:
    """Keep running entries and the BG_KEEP most recent finished ones."""
    done = sorted((it.get("s", 0), k) for k, it in bg.items() if it.get("st") != "run")
    for _s, k in done[: max(0, len(done) - BG_KEEP)]:
        bg.pop(k, None)


def store(cid: str, st: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        sp = os.path.join(STATE_DIR, cid + ".json")
        tmp = f"{sp}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        compat.replace(tmp, sp)
    except OSError:
        pass


def prune_states() -> None:
    """Drop state files of conversations untouched for STATE_TTL_DAYS (called from SessionStart)."""
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    try:
        for e in os.scandir(STATE_DIR):
            if e.name.endswith(".json") and e.stat().st_mtime < cutoff:
                os.remove(e.path)
    except OSError:
        pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:300]


def with_current(st: Optional[dict], payload: dict) -> List[dict]:
    """The user's messages, plus the payload's lastUserInput when the transcript hasn't recorded it yet."""
    users = list(st["users"]) if st else []
    cur = payload.get("lastUserInput")
    if isinstance(cur, str) and cur.strip():
        text = user_text(cur)
        if not users or _norm(users[-1]["text"]) != _norm(text):
            users.append({"step": st["last_step"] + 1 if st else 0, "ts": "", "text": text})
    return users


# Full access: one grant from the user lets the agent run every plugin command without asking (see rules "Full access").
# Only the user's own messages count — never text from tools, pages or files.
GRANT = re.compile(r"\b(даю|дам|выдаю|разрешаю|включаю|включи|дай|даём|даем|предоставляю)\s+(тебе\s+|вам\s+)?полн\w*\s+доступ"
                   r"|полн\w*\s+доступ\w*\s*[—:-]?\s*(да|разрешаю|даю|ок|включ)"
                   r"|\b(grant|give)\s+(you\s+)?full\s+access|\bfull\s+access\s*[:—-]?\s*(granted|on|yes)\b"
                   r"|^\s*/full-access(\s+on)?\s*$", re.I)
AFFIRM = re.compile(r"^\W*(да|ага|ок(ей)?|согласен|согласна|разрешаю|даю|давай|конечно|можно|yes|ok|sure|go ahead)\b", re.I)
ASKED = re.compile(r"полн\w*\s+доступ|full\s+access", re.I)
REVOKE = re.compile(r"(забира\w*|отзыва\w*|отзову|отозв\w*|отключ\w*|выключ\w*|убер\w*|сними)\s+(у\s+тебя\s+)?полн\w*\s+доступ"
                    r"|без\s+полного\s+доступа|верни\s+(вопросы|подтверждени\w*)|спрашивай\s+(снова|опять|разрешени\w*)"
                    r"|\brevoke\b.*\bfull\s+access|\bfull\s+access\b\s*[:—-]?\s*off\b|^\s*/full-access\s+off\s*$", re.I)
PROJECT_SCOPE = re.compile(r"навсегда|всегда|для\s+(всего\s+|этого\s+)?проекта|во\s+всех\s+разговорах|for\s+this\s+project|\balways\b", re.I)


def access_events(users: List[dict]) -> List[dict]:
    """Grants and revocations of full access in the user's messages, in order."""
    out = []
    for i, u in enumerate(users, 1):
        text = u.get("text") or ""
        if REVOKE.search(text):
            out.append({"n": i, "on": False, "text": text})
        elif GRANT.search(text) or (len(text) <= 60 and AFFIRM.search(text) and ASKED.search(u.get("prev_agent") or "")
                                    and "?" in (u.get("prev_agent") or "")):
            out.append({"n": i, "on": True, "project": bool(PROJECT_SCOPE.search(text)), "text": text})
    return out


def full_access(payload: dict, st: Optional[dict], users: Optional[List[dict]] = None) -> Optional[dict]:
    """{"n", "text", "project": bool, "source"} when full access is on for this conversation, else None.
    Subagents inherit their parent's. A grant "for the project" is kept in settings (fullAccess) for its workspace."""
    if users is None:
        users = with_current(st, payload) if st and st["users"] else []
    ws = next((os.path.realpath(to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), None)
    events = access_events(users)
    if not users:  # a subagent: inherit from the conversation that started it
        parent = _parent(payload.get("conversationId"))
        pst = _state(parent) if parent else None
        if pst and pst["users"]:
            got = full_access({"conversationId": parent, "workspacePaths": payload.get("workspacePaths")}, pst)
            return dict(got, source="parent") if got else None
    if st is not None and events and events[-1]["n"] > st.get("access_applied", 0) and ws:
        last = events[-1]  # a new grant or revocation: keep the project-wide one in step
        st["access_applied"] = last["n"]
        _project_grant(ws, last["on"] and last.get("project"), drop=not last["on"])
    if events:
        last = events[-1]
        return dict(last, source="conversation") if last["on"] else None
    granted = _project_grant(ws, None) if ws else None
    return {"n": 0, "text": "", "project": True, "source": f"project since {granted}"} if granted else None


def _project_grant(ws: str, grant: Optional[bool], drop: bool = False) -> Optional[str]:
    """Read (grant=None), add (True) or remove (drop) the project-wide full-access grant in settings."""
    path = os.path.expanduser("~/.gemini/antigravity-plus/settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    grants = data.get("fullAccess") if isinstance(data.get("fullAccess"), dict) else {}
    if grant is None and not drop:
        return grants.get(ws)
    if grant:
        grants[ws] = time.strftime("%Y-%m-%d")
    elif drop:
        grants.pop(ws, None)
    else:
        return None
    data["fullAccess"] = grants
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        compat.replace(tmp, path)
    except OSError:
        pass
    return grants.get(ws)


def _state(cid: str) -> Optional[dict]:
    try:
        with open(os.path.join(STATE_DIR, cid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parent(cid) -> str:
    if not isinstance(cid, str):
        return ""
    try:
        import sqlite3
        db = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=1)
        row = con.execute("SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                          (cid,)).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def is_constraint(text: str) -> bool:
    return bool(CONSTRAINT.search(text))


def active_bans(users: List[dict]) -> Dict[str, dict]:
    """{"push"|"commit": {"n": message number (1-based), "text": …}} for restrictions the user set and hasn't lifted."""
    active: Dict[str, dict] = {}
    for i, u in enumerate(users, 1):
        text = u.get("text") or ""
        for kind, (ban, lift) in BANS.items():
            if ban.search(text):
                active[kind] = {"n": i, "text": text}
            elif lift.search(text):
                active.pop(kind, None)
    return active
