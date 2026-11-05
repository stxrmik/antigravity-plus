#!/usr/bin/env python3
"""Antigravity Plus — what the user has said in this conversation.

Reads the conversation transcript incrementally (only bytes added since the last call) and keeps the user's
messages in ~/.gemini/antigravity-plus/state/<conversationId>.json. Shared by:
  * hooks/instructions.py (PreInvocation) — re-shows the user's instructions so they survive long turns and
    context compaction;
  * hooks/guard.py (PreToolUse) — asks before pushing/deploying/committing when the user said not to.
Also detects standing restrictions ("не пушь", "don't commit") and their lifting ("пушь", "commit it").
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
        elif d.get("type") == "CHECKPOINT":
            st["checkpoints"].append(step)
        elif d.get("type") == "PLANNER_RESPONSE" and (d.get("content") or "").strip():
            st["last_agent"] = d["content"][-500:]
    st["offset"] += end + 1
    if save:
        store(cid, st)
    return st


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
