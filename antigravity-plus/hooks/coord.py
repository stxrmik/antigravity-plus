#!/usr/bin/env python3
"""Antigravity Plus — coordination of agents that work on the same project at the same time.

Several agents (Antigravity conversations in the Agent Manager, their subagents, plus Claude Code or your editor)
can change one project at once. A small registry per workspace,
~/.gemini/antigravity-plus/coord/<workspace-slug>.json, records
  agents: {conversationId: {"title", "title_from", "parent", "last": ts, "files": {path: ts}}}  who changed what, when
  seen:   {conversationId: {path: [mtime_ns, size, ts]}}                         the version each agent last saw
Every hook call of every agent reads it under one lock and most of them rewrite it, so it holds no file contents.
The text of the version an agent saw (for the diff a stale-write refusal shows) lies beside it, one file per file:
  coord/snaps/<workspace-slug>/<conversationId>/<hash of the path>.json   {"path", "v": [mtime_ns, size], "text"}
written only by that agent's own hooks (atomic replace, no lock), at most SNAP_KEEP per agent, trusted only while "v"
is the version in `seen`, and deleted with the agent when the registry forgets it (idle for KEEP) — which is also
when snapshot folders untouched for KEEP that no registry keeps any more are swept up.
The registry is used by
  * hooks/guard.py (PreToolUse): an agent may not write over a file that changed since it last read or wrote it
    (someone else's work would be lost) — it gets a refusal that says who changed it, re-reads and retries;
  * hooks/check_edit.py (PostToolUse on read_file/write_file/edit_file): records what the agent saw and changed;
  * hooks/instructions.py (PreInvocation): heartbeat, and the other agents working here (when that picture changes);
  * /commit: `coord.py mine` — only the files this agent (and its subagents) changed, when others are active.

  coord.py status --workspace DIR            who is working in the project now and on which files
  coord.py mine --conversation ID --workspace DIR   files this conversation and its subagents changed (one per line)

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

import checks
import compat
import conversation as conv

COORD_DIR = os.path.expanduser("~/.gemini/antigravity-plus/coord")
SNAP_DIR = os.path.join(COORD_DIR, "snaps")  # <workspace-slug>/<conversationId>/<path hash>.json
INDEX_DB = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
ACTIVE = 10 * 60          # an agent with activity in the last 10 minutes is working right now
KEEP = 24 * 3600          # forget agents idle for a day (and their snapshots)
LOOK_EVERY = 60           # s between two lookups of an agent in Antigravity's conversation index
SNAP_MAX_BYTES = 64_000   # max file size saved as text snapshot for stale-write diffs
SNAP_KEEP = 30            # max cached snapshots per agent
DIFF_READ_BYTES = 128_000 # max file size read when formatting stale-write diff
DIFF_MAX_LINES = 24       # max unified-diff lines shown in stale-write refusal
SNIPPET_LINES = 16        # max context lines when no prior snap exists
OTHERS_FILES = 8          # changed files listed per agent in <other_agents>
OTHERS_EVERY = 20         # model calls of a conversation between two <other_agents> blocks about changed files only
CID = re.compile(r"[\w-]{8,80}")  # a conversation id that can name a folder


def workspace(payload: dict) -> Optional[str]:
    for w in payload.get("workspacePaths") or []:
        if isinstance(w, str) and w:
            return os.path.realpath(conv.to_path(w))
    return None


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class Registry:
    """The project's registry under an exclusive lock: agents of one project run their hooks concurrently."""

    def __init__(self, ws: str, write: bool = True):
        self.ws = ws
        self.path = os.path.join(COORD_DIR, slug(ws) + ".json")
        self.write = write

    def __enter__(self) -> dict:
        os.makedirs(COORD_DIR, exist_ok=True)
        self.lock = open(self.path + ".lock", "w")
        compat.lock(self.lock, exclusive=self.write)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault("agents", {})
        self.data.setdefault("seen", {})
        return self.data

    def __exit__(self, *exc) -> None:
        gone: List[str] = []
        try:
            if self.write:
                now = time.time()
                gone = [a for a, v in self.data["agents"].items() if now - v.get("last", 0) > KEEP]
                for a in gone:
                    self.data["agents"].pop(a, None)
                    self.data["seen"].pop(a, None)
                # 0.25.0 kept the text snapshots here (megabytes rewritten on every call; now in SNAP_DIR), and an
                # earlier build kept retry counters (retries no longer unlock writes)
                for old in ("snaps", "stale_hits"):
                    self.data.pop(old, None)
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                compat.replace(tmp, self.path)
        finally:
            compat.unlock(self.lock)
            self.lock.close()
        if gone:  # outside the lock: the other agents' hooks don't wait for the file system
            _forget_snaps(self.ws, gone, self.data["agents"])


# ─────────────────────────────── snapshots: the text each agent saw ───────────────────────────────

def _snap_root(ws: str) -> str:
    return os.path.join(SNAP_DIR, slug(ws))


def _snap_dir(ws: str, cid: str) -> Optional[str]:
    return os.path.join(_snap_root(ws), cid) if isinstance(cid, str) and CID.fullmatch(cid) else None


def _snap_file(ws: str, cid: str, path: str) -> Optional[str]:
    folder = _snap_dir(ws, cid)
    return os.path.join(folder, hashlib.sha256(os.fsencode(path)).hexdigest()[:16] + ".json") if folder else None


def _load_snap(ws: str, cid: str, path: str, v: List[int]) -> Optional[str]:
    """The text of `path` this agent saw as version `v` ([mtime_ns, size]); None when that snapshot isn't kept."""
    f = _snap_file(ws, cid, path)
    if not f:
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if (isinstance(d, dict) and d.get("path") == path and isinstance(d.get("v"), list) and d["v"][:2] == list(v[:2])
            and isinstance(d.get("text"), str)):
        return d["text"]
    return None


def _save_snap(ws: str, cid: str, path: str, v: Optional[List[int]], text: Optional[str]) -> None:
    """Keep `text` as this agent's snapshot of version `v` of `path`, up to SNAP_KEEP newest per agent. A version that
    can't be kept (too big, binary, a secret) drops the agent's older snapshot of the file."""
    f = _snap_file(ws, cid, path)
    if not f:
        return
    try:
        if text is None or not v or len(text.encode("utf-8", errors="replace")) > SNAP_MAX_BYTES:
            if os.path.exists(f):
                os.remove(f)
            return
        os.makedirs(os.path.dirname(f), exist_ok=True)
        tmp = f"{f}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"path": path, "v": list(v[:2]), "text": text}, fh, ensure_ascii=False)
            compat.replace(tmp, f)
        except (OSError, ValueError):
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        _trim_snaps(os.path.dirname(f), f)
    except (OSError, ValueError):
        pass  # a snapshot is a cache: without it a refusal shows the file's lines instead of a diff


def _mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _trim_snaps(folder: str, keep: str) -> None:
    """The agent's SNAP_KEEP newest snapshots stay (`keep`, the one just written, always does)."""
    try:
        snaps = [e for e in os.scandir(folder) if e.name.endswith(".json") and e.path != keep]
    except OSError:
        return
    if len(snaps) < SNAP_KEEP:
        return
    for e in sorted(snaps, key=_mtime)[: len(snaps) - (SNAP_KEEP - 1)]:
        try:
            os.remove(e.path)
        except OSError:
            pass


def _forget_snaps(ws: str, gone: List[str], agents: dict) -> None:
    """The registry forgot these agents: their snapshots go too, and then any orphaned ones (_sweep_snaps)."""
    for cid in gone:
        folder = _snap_dir(ws, cid)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    _sweep_snaps(slug(ws), agents)


def _registry_agents(name: str) -> dict:
    """Another workspace's agents, read without its lock (an atomic replace never shows half a file)."""
    try:
        with open(os.path.join(COORD_DIR, name + ".json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d["agents"] if isinstance(d, dict) and isinstance(d.get("agents"), dict) else {}


def _sweep_snaps(here: str, agents: dict) -> None:
    """Snapshot folders untouched for KEEP whose agent no registry keeps (its registry was deleted — a /doctor
    self-test's workspace — or reset, or a forgotten agent's folder couldn't be deleted). Runs only when a registry
    forgets agents, a few times a day, at one stat per agent folder. `here`, `agents`: the calling registry's."""
    now = time.time()
    try:
        roots = [e for e in os.scandir(SNAP_DIR) if e.is_dir()]
    except OSError:
        return
    for root in roots:
        known: Optional[dict] = agents if root.name == here else None
        try:
            folders = [e for e in os.scandir(root.path) if e.is_dir()]
        except OSError:
            continue
        left = len(folders)
        for d in folders:
            if now - (_mtime(d) or now) <= KEEP:
                continue  # a snapshot written in the last day (the registry may not list its agent yet)
            if known is None:
                known = _registry_agents(root.name)
            a = known.get(d.name)
            if isinstance(a, dict) and isinstance(a.get("last"), (int, float)) and now - a["last"] <= KEEP:
                continue  # its agent still works here, it just hasn't read anything new for a day
            shutil.rmtree(d.path, ignore_errors=True)
            left -= 1
        if not left and root.name != here:
            try:
                os.rmdir(root.path)
            except OSError:
                pass


# ─────────────────────────────── agents ───────────────────────────────

def _index_row(cid: str) -> Tuple[str, str, str]:
    """(title, preview, parent conversation id) from Antigravity's own conversation index."""
    dbs = [os.path.join(d, "conversation_summaries.db") for d in compat.data_dirs()]
    if INDEX_DB and INDEX_DB not in dbs:
        dbs.append(INDEX_DB)
    for db in dbs:
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=1)
            try:
                row = con.execute("SELECT title, preview, parent_conversation_id FROM conversation_summaries "
                                  "WHERE conversation_id = ?", (cid,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if row:
            return (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""))
    return ("", "", "")


def conversation_info(cid: str) -> Tuple[str, str]:
    """(title, parent conversation id) from Antigravity's own conversation index (its preview while it has no title)."""
    title, preview, parent = _index_row(cid)
    return title or preview, parent


def _clip(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t[:n].rstrip() + "…" if len(t) > n else t


def request_title(text: str) -> str:
    """A name for a conversation Antigravity hasn't titled yet, from its first user message: the message's start —
    or, for a task Claude Code handed over (its run rules come first, conversation.DELEGATED_MARK), the first line of
    the task itself, named the way the bridge titles it («Claude Code: …»)."""
    text = (text or "").strip()
    if text.startswith(conv.DELEGATED_MARK):
        m = re.search(r"\nTask:[ \t]*\n(.*)", text, re.S)  # delegate.py RULES: «…\n\nTask:\n<brief>»
        first = next((ln.strip(" #*->") for ln in (m.group(1) if m else "").splitlines() if ln.strip(" #*->")), "")
        return "Claude Code: " + _clip(first or "task")
    return _clip(text)


def _named(title: str) -> bool:
    """Worth showing as a title: not empty, and not the start of the bridge's run rules (a delegated task's preview)."""
    return bool(title.strip()) and not title.lstrip().startswith(conv.DELEGATED_MARK)


def _cid(value: object, own: str = "") -> str:
    """A conversation id another agent can be told apart by: a non-empty string that names a folder and isn't `own`.
    Empty, missing, non-string and self-referencing ids are no ids (and "" never matches an entry's empty parent)."""
    v = value.strip() if isinstance(value, str) else ""
    return v if v and v != own and CID.fullmatch(v) else ""


def _agent(data: dict, cid: str, payload: Optional[dict] = None) -> dict:
    """The agent's entry. Its title comes from Antigravity's index ("title_from": "index" — final); until the index
    has one, it is provisional (the index's "preview", the first user message — "request", request_title() — and,
    last of all, the "payload": agentName from HookArgsCommon, which is usually absent and may be the same for
    every agent) and the index is asked again, at most once a minute — as it is for the parent of a subagent.
    The index row also tells a main agent ("main": it has a title and no parent): nothing else makes it a subagent."""
    now = time.time()
    a = data["agents"].setdefault(cid, {"title": "", "parent": "", "last": now, "files": {}})
    p = payload or {}
    p_parent = _cid(p.get("parentConversationId"), cid)
    p_agent = p.get("agentName").strip() if isinstance(p.get("agentName"), str) else ""
    if p_parent and not a.get("main"):
        a["parent"] = a.get("parent") or p_parent
    if (a.get("title_from") != "index" or not a.get("parent")) and now - a.get("looked", 0) > LOOK_EVERY:
        a["looked"] = now  # main agents have no parent: don't ask the index on every call
        title, preview, parent = _index_row(cid)
        par = _cid(parent, cid)
        if _named(title):
            a["title"], a["title_from"] = title.strip(), "index"
        elif a.get("title_from") != "index":
            if _named(preview):
                a["title"], a["title_from"] = preview.strip(), "preview"
            else:
                st = conv._state(cid)
                users = st.get("users") if isinstance(st, dict) else None
                first = users[0].get("text") if isinstance(users, list) and users and isinstance(users[0], dict) else ""
                guess = request_title(str(first or "") or preview or str(a.get("title") or ""))
                if guess:  # better than agentName, which only fills in when nothing else names it
                    a["title"], a["title_from"] = guess, "request"
        if par:
            a["main"] = False
            a["parent"] = par
        elif _named(title):  # the index names it and knows no parent: a main agent, whatever else claims
            a["main"] = True
            a["parent"] = ""
    if p_agent and not _named(str(a.get("title") or "")):
        a["title"], a["title_from"] = p_agent, "payload"
    return a


def _entry(data: dict, aid: str) -> dict:
    e = data["agents"].get(aid)
    return e if isinstance(e, dict) else {}


def family(data: dict, cid: str, payload: Optional[dict] = None) -> set:
    """What works on one task together: this conversation, its ancestors (parent links), and everything below them —
    siblings, subagents of subagents — plus the subagents the conversation itself started and tracked (state "bg").
    Only real parent links count — never an empty id or an id of its own — and a "rootConversationId" alone links
    nothing (a root two main agents share would make them one family). A tracked subagent that is known to be
    someone else's — a main agent, or one with another parent — stays out."""
    fam = {cid}
    agents = data["agents"]
    me = _entry(data, cid)
    p = payload or {}
    up = cid
    parent = "" if me.get("main") else (_cid(me.get("parent"), cid) or _cid(p.get("parentConversationId"), cid))
    while parent and parent not in fam:  # up to the main agent (a cycle in a damaged registry ends here)
        fam.add(parent)
        up = parent
        parent = _cid(_entry(data, up).get("parent"), up)
    st = conv._state(cid) if isinstance(cid, str) and CID.fullmatch(cid) else None
    bg = st.get("bg") if isinstance(st, dict) else None
    if isinstance(bg, dict):
        for sub_id, it in bg.items():
            sub = _cid(sub_id, cid)
            if not sub or not isinstance(it, dict) or it.get("k") != "agent":
                continue
            e = _entry(data, sub)
            if e.get("main") or _cid(e.get("parent"), sub) not in ("", cid):
                continue
            fam.add(sub)
    changed = True
    while changed:  # everything below the family so far
        changed = False
        for other, entry in agents.items():
            if other in fam or not isinstance(entry, dict):
                continue
            par = _cid(entry.get("parent"), other)
            if par and par in fam and not entry.get("main"):
                fam.add(other)
                changed = True
    return fam


def heartbeat(payload: dict) -> None:
    cid, ws = payload.get("conversationId"), workspace(payload)
    if isinstance(cid, str) and ws:
        with Registry(ws) as data:
            _agent(data, cid, payload)["last"] = time.time()


# ─────────────────────────────── stale writes ───────────────────────────────

def version(path: str) -> Optional[List[int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _read_text(path: str, limit: int) -> Optional[str]:
    """Read UTF-8 text up to `limit` bytes; None for missing, oversized, or binary (NUL-byte) files."""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit or b"\0" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _stale_diff(snap_text: Optional[str], disk_text: Optional[str], target: Optional[str]) -> Tuple[str, str]:
    """(text, kind): kind "full" — the whole diff since the agent's snapshot; "partial" — its first DIFF_MAX_LINES
    lines; "same" — the text didn't change; "snippet" — no snapshot, so lines of the current file (around `target`),
    not a diff; "none" — nothing can be shown (too big, binary or unreadable)."""
    if disk_text is None:
        return "", "none"
    if snap_text is not None:
        diff_lines = list(difflib.unified_diff(snap_text.splitlines(), disk_text.splitlines(),
                                               fromfile="seen", tofile="disk", lineterm="", n=2))
        if not diff_lines:
            return "(текст на диске совпадает с вашей версией — обновилось только время изменения файла)", "same"
        if len(diff_lines) <= DIFF_MAX_LINES:
            return "\n".join(diff_lines), "full"
        shown = diff_lines[:DIFF_MAX_LINES] + [f"… (+ ещё {len(diff_lines) - DIFF_MAX_LINES} строк diff не показано)"]
        return "\n".join(shown), "partial"
    lines = disk_text.splitlines()
    if not lines:
        return "(файл на диске пуст)", "snippet"
    lo, hi = 0, min(len(lines), SNIPPET_LINES)
    if target:
        idx = disk_text.find(target)
        if idx >= 0:
            start_ln = disk_text.count("\n", 0, idx)
            end_ln = start_ln + target.count("\n") + 1
            lo = max(0, start_ln - 4)
            hi = min(len(lines), max(lo + SNIPPET_LINES, end_ln + 4))
            if hi - lo > SNIPPET_LINES:
                hi = lo + SNIPPET_LINES
    shown = [f"{n}: {lines[n - 1]}" for n in range(lo + 1, hi + 1)]
    if hi < len(lines) or lo > 0:
        shown.append(f"… (строки {lo + 1}-{hi} из {len(lines)})")
    return "\n".join(shown), "snippet"


def _lost_changes(snap_text: str, disk_text: str, content: str) -> List[str]:
    """What a whole-file write of `content` would undo of the changes made on disk since the agent's snapshot: lines
    added or changed there that `content` drops, and lines removed there that `content` brings back."""
    old, new = snap_text.splitlines(), disk_text.splitlines()
    keep = {ln.rstrip() for ln in content.splitlines()}
    on_disk = {ln.rstrip() for ln in new}
    lost: List[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            lost += [ln for ln in new[j1:j2] if ln.strip() and ln.rstrip() not in keep]
        if tag in ("replace", "delete"):
            lost += [f"{ln.strip()} (удалено на диске)" for ln in old[i1:i2]
                     if len(ln.strip()) >= 3 and ln.rstrip() in keep and ln.rstrip() not in on_disk]
    return lost


def record(payload: dict, tool: str, path: str) -> None:
    """After read_file / write_file / edit_file: remember the version this agent saw (and that it changed the file)."""
    cid, ws = payload.get("conversationId"), workspace(payload)
    if not (isinstance(cid, str) and ws and path) or payload.get("error"):
        return
    path = os.path.realpath(path)
    v = version(path)
    text = None
    if v and not checks.SECRET_FILE.search(compat.slashes(path)):
        text = _read_text(path, SNAP_MAX_BYTES)
        if text is not None and version(path) != v:
            text = None  # it changed while being read: this text may not be version v
    with Registry(ws) as data:
        a = _agent(data, cid, payload)
        now = time.time()
        a["last"] = now
        if v:
            data["seen"].setdefault(cid, {})[path] = v + [now]
        if tool in ("write_file", "edit_file"):
            a["files"][path] = now
    if v:
        _save_snap(ws, cid, path, v, text)  # this agent's own file: no other agent waits on it


def _writer(data: dict, cid: str, path: str, payload: Optional[dict] = None) -> Tuple[Optional[str], float, dict]:
    """(id, when, entry) of the agent outside this conversation's family that changed `path` last."""
    writer, when = None, 0.0
    fam = family(data, cid, payload)
    for other, a in data["agents"].items():
        ts = a.get("files", {}).get(path, 0)
        if other not in fam and ts > when:
            writer, when = other, ts
    return writer, when, (data["agents"].get(writer, {}) if writer else {})


def check_write(payload: dict, path: str, content: Optional[str] = None, target: Optional[str] = None) -> Optional[str]:
    """Refusal text when this agent is about to write over a version of `path` it hasn't seen, or to rewrite the whole
    file (write_file `content`) dropping lines another active agent changed recently; None if it's safe."""
    cid, ws = payload.get("conversationId"), workspace(payload)
    if not (isinstance(cid, str) and ws and path):
        return None
    path = os.path.realpath(path)
    current = version(path)
    if not current:
        return None  # a new file
    with Registry(ws, write=False) as data:
        seen = data["seen"].get(cid, {}).get(path)
        writer, when, writer_info = _writer(data, cid, path, payload)
    if seen is None:
        if writer and time.time() - when < ACTIVE:
            return refusal(path, ws, writer, writer_info, when, never_read=True)
        return None  # never seen and nobody else touched it recently: nothing to protect
    if seen[:2] != current:
        if checks.SECRET_FILE.search(compat.slashes(path)):  # a secret's text never goes into the conversation
            eff_writer = writer if writer and when > seen[2] else None
            return refusal(path, ws, eff_writer, writer_info, when, never_read=False)
        disk_text = _read_text(path, DIFF_READ_BYTES)
        steady = version(path) == current  # the text read is the version checked (no write landed in between)
        lost: List[str] = []
        safe_edit = False
        with Registry(ws, write=True) as data:
            seen = data["seen"].get(cid, {}).get(path)
            if seen is None or seen[:2] == current:
                return None
            writer, when, writer_info = _writer(data, cid, path, payload)
            eff_writer = writer if writer and when > seen[2] else None
            snap_text = _load_snap(ws, cid, path, seen[:2])
            if (steady and snap_text is not None and disk_text is not None and snap_text == disk_text
                    and "�" not in disk_text):
                data["seen"].setdefault(cid, {})[path] = current + [time.time()]
                _save_snap(ws, cid, path, current, disk_text)  # the same text, now as the version on disk
                return None  # only the file's time changed: what the agent saw is what is on disk
            diff_text, kind = _stale_diff(snap_text, disk_text, target)
            if content is not None and steady and snap_text is not None and disk_text is not None:
                lost = _lost_changes(snap_text, disk_text, content)
                if not lost:
                    return None  # the whole-file write keeps every line that changed on disk: nothing of theirs is lost
            # An edit whose Target is still in the file word for word replaces only that text, so it can't undo what
            # changed elsewhere: after this one refusal (with the diff) the same edit may go through. Nothing else
            # is let through without a fresh read_file.
            safe_edit = (content is None and steady and isinstance(target, str) and bool(target.strip())
                         and disk_text is not None and target in disk_text)
            if safe_edit:
                data["seen"].setdefault(cid, {})[path] = current + [time.time()]
                _save_snap(ws, cid, path, current, disk_text)
        return refusal(path, ws, eff_writer, writer_info, when, never_read=False, diff_text=diff_text,
                       diff_kind=kind, refreshed=safe_edit, lost=lost)
    if content is not None and writer and time.time() - when < ACTIVE:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                old = [ln.rstrip() for ln in f if ln.strip()]
        except OSError:
            return None
        keep = {ln.rstrip() for ln in content.splitlines()}
        lost = [ln for ln in old if ln not in keep]
        if lost:
            rel = os.path.relpath(path, ws) if path.startswith(ws + os.sep) else path
            return (f"🛡️ Antigravity Plus: перезапись {rel} целиком удалит строки, а файл {max(1, int((time.time() - when) // 60))} "
                    f"мин назад менял другой агент «{writer_info.get('title') or 'без названия'}» (разговор {writer}): "
                    + "; ".join(f"«{ln.strip()[:60]}»" for ln in lost[:3]) + (" …" if len(lost) > 3 else "")
                    + ". Меняйте файл точечно (edit_file) и оставьте его изменения, а если их правда нужно убрать — "
                    f"договоритесь с ним через send_message (Recipient: {writer}).")
    return None


DIFF_INTRO = {
    "full": "Что изменилось на диске с вашего последнего чтения (diff целиком):",
    "partial": "Начало изменений с вашего последнего чтения (diff обрезан — показаны не все строки, остальное увидите "
               "в read_file):",
    "same": "",
    "snippet": "Сохранённой версии этого файла нет, поэтому diff показать нельзя — ниже текущий текст файла:",
}


def refusal(path: str, ws: str, writer: Optional[str], info: dict, when: float, never_read: bool,
            diff_text: str = "", diff_kind: str = "", refreshed: bool = False, lost: Optional[List[str]] = None) -> str:
    rel = os.path.relpath(path, ws) if path.startswith(ws + os.sep) else path
    if writer:
        ago = max(1, int((time.time() - when) // 60))
        who = f"другой агент «{info.get('title') or 'без названия'}» (разговор {writer}, {ago} мин назад)"
    else:
        who = "кто-то другой после вашего последнего чтения (другой агент, Claude Code, редактор или ваша же команда)"
    head = (f"файл {rel} изменил {who}, а вы его ещё не читали" if never_read
            else f"файл {rel} изменил {who}")
    if lost:
        action = ("Перезапись файла целиком отменила бы эти изменения с диска: "
                  + "; ".join(f"«{ln.strip()[:60]}»" for ln in lost[:3]) + (" …" if len(lost) > 3 else "")
                  + ". Включите их в новый текст (тогда запись пройдёт) или перечитайте файл (read_file) и сделайте "
                    "правку поверх его текущей версии.")
    elif refreshed:
        action = ("Ваш Target по-прежнему есть в файле слово в слово, поэтому эта правка не заденет чужие изменения: "
                  "посмотрите их ниже и повторите правку как есть (или перечитайте файл через read_file).")
    else:
        action = "Чтобы не затереть чужую работу, перечитайте файл (read_file) и сделайте правку поверх его текущей версии."
    msg = (f"🛡️ Antigravity Plus: {head}. {action}"
           + (f" Если вы работаете над одним и тем же, договоритесь с ним через send_message (Recipient: {writer})." if writer else ""))
    if diff_text:
        intro = DIFF_INTRO.get(diff_kind, "")
        msg += f"\n{intro}\n{diff_text}" if intro else f"\n{diff_text}"
    elif diff_kind == "none":
        msg += "\n(Изменения показать нельзя: файл слишком большой, двоичный или не читается — только read_file.)"
    return msg


# ─────────────────────────────── other agents ───────────────────────────────

def others(payload: dict) -> List[dict]:
    """Other agents (not this conversation's family) active in this workspace now, with the files they changed:
    {"id", "title", "ago" (min), "files" (newest first), "changed": {file: when}}."""
    cid, ws = payload.get("conversationId"), workspace(payload)
    if not (isinstance(cid, str) and ws) or not os.path.isfile(os.path.join(COORD_DIR, slug(ws) + ".json")):
        return []
    now, out = time.time(), []
    with Registry(ws, write=False) as data:
        fam = family(data, cid, payload)
        for other, a in data["agents"].items():
            if other in fam or now - a.get("last", 0) > ACTIVE:
                continue
            files = sorted((p for p, ts in a.get("files", {}).items() if now - ts < 3 * ACTIVE),
                           key=lambda p: -a["files"][p])
            rel = {p: os.path.relpath(p, ws) if p.startswith(ws + os.sep) else p for p in files}
            out.append({"id": other, "title": a.get("title") or "", "ago": int((now - a.get("last", 0)) // 60),
                        "files": [rel[p] for p in files], "changed": {rel[p]: a["files"][p] for p in files}})
    return out


OTHERS_RULES = ("Don't overwrite their work: leave their files alone unless your task needs them, re-read a file "
                "before editing it, and coordinate through send_message (Recipient: <conversation id>) when your "
                "work overlaps. Commit only your own files.")


def _files(files: List[str]) -> str:
    return ", ".join(files[:OTHERS_FILES]) + (" …" if len(files) > OTHERS_FILES else "")


def _who(a: dict, active: bool = True) -> str:
    return (f"«{a.get('title') or 'без названия'}» (conversation {a['id']}"
            + (f", active {a['ago']} min ago)" if active else ")"))


def _block(head: str, lines: List[str]) -> str:
    return "\n".join(['<other_agents source="Antigravity Plus">', head] + lines + ["</other_agents>"])


def _full(agents: List[dict]) -> str:
    return _block("Other agents are working in this project right now:",
                  [f"- {_who(a)}" + (f" — changed: {_files(a['files'])}" if a["files"] else "") for a in agents]
                  + [OTHERS_RULES])


def _since(a: dict, since: float) -> List[str]:
    """The agent's files changed since `since` (newest first)."""
    return [f for f in a["files"] if a.get("changed", {}).get(f, 0) >= since]


def others_message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """<other_agents> block for the PreInvocation hook. Antigravity keeps every injected block in the conversation for
    good, so one comes only when the picture changes, and says only what changed:
      * the first time other agents are active here (and after a context compaction): the full list with the files
        each changed and the rules — as before;
      * one joins or leaves (only the ids are compared): at once — who joined (with its files), who stopped, the files
        the others changed since the last block. When the last of them stops: no block (their changes are still in
        the tree, so «commit only your own files» stands, and the guard keeps guarding every file); whoever comes next
        gets the full list again;
      * they only changed files: at most once per OTHERS_EVERY calls of this conversation, just the files changed
        since the last block.
    `st`: the conversation's state (hooks/conversation.py) — counts the calls ("others_calls") and keeps what the last
    block said ("others_shown" {id: title}, "others_at" call, "others_since" time, "others_step"); without one (no
    transcript yet) every call with other agents gets the full list."""
    now = time.time()  # before the registry is read: whatever is recorded after that read is newer than this
    agents = others(payload)
    if st is None:
        return _full(agents) if agents else None
    calls = int(st.get("others_calls") or 0) + 1
    st["others_calls"] = calls
    ids = {a["id"] for a in agents}
    shown = st.get("others_shown")
    legacy = st.pop("others_key", None)  # an earlier build's «id:files|…» of the last block
    if not isinstance(shown, dict):
        shown = {}
        if legacy and {p.split(":", 1)[0] for p in str(legacy).split("|") if p} == ids:
            shown = {a["id"]: a["title"] for a in agents}  # it showed these very agents: no need to repeat them
            st.update(others_at=calls, others_since=now, others_step=int(st.get("last_step", -1)))
    cps = st.get("checkpoints") or []
    compacted = bool(cps) and isinstance(cps[-1], int) and cps[-1] > int(st.get("others_step", -1))
    since = float(st.get("others_since") or 0)
    msg = None
    if set(shown) == ids and not (ids and compacted):
        if ids and calls - int(st.get("others_at") or 0) >= OTHERS_EVERY:
            lines = [f"- {_who(a)}: {_files(new)}" for a in agents for new in [_since(a, since)] if new]
            if lines:
                msg = _block("Since the last update the other agents changed:", lines)
    elif not ids:
        pass  # the last of them stopped working here: see above
    elif not shown or compacted:
        msg = _full(agents)
    else:
        lines = []
        for a in agents:
            if a["id"] not in shown:
                lines.append(f"- {_who(a)} started working here" + (f" — changed: {_files(a['files'])}" if a["files"] else ""))
            elif _since(a, since):
                lines.append(f"- {_who(a)} changed: {_files(_since(a, since))}")
        gone = [i for i in shown if i not in ids]
        lines += [f"- «{shown[i] or 'без названия'}» (conversation {i}) stopped: no activity for {ACTIVE // 60}+ min"
                  for i in gone]
        stay = [a for a in agents if a["id"] in shown]
        if gone and stay:
            lines.append("Still working here: " + ", ".join(_who(a, active=False) for a in stay) + ".")
        msg = _block("Update on the other agents in this project:", lines)
    st["others_shown"] = {a["id"]: a["title"] for a in agents}
    if msg:
        st.update(others_at=calls, others_since=now, others_step=int(st.get("last_step", -1)))
    return msg


def mine(ws: str, cid: str) -> List[str]:
    with Registry(ws, write=False) as data:
        fam = family(data, cid)
        files = {p for a_id in fam for p in data["agents"].get(a_id, {}).get("files", {})}
    return sorted(files)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("status")
    sp.add_argument("--workspace", default=os.getcwd())
    sp = sub.add_parser("mine")
    sp.add_argument("--conversation", required=True)
    sp.add_argument("--workspace", default=os.getcwd())
    a = ap.parse_args(argv)
    ws = os.path.realpath(os.path.expanduser(a.workspace))
    if a.cmd == "mine":
        for p in mine(ws, a.conversation):
            print(os.path.relpath(p, ws) if p.startswith(ws + os.sep) else p)
        return 0
    agents = others({"conversationId": "-", "workspacePaths": [ws]})
    if not agents:
        print("Сейчас в этом проекте других активных агентов нет.")
    for x in agents:
        print(f"- «{x['title'] or 'без названия'}» ({x['id']}, активен {x['ago']} мин назад): "
              + (", ".join(x["files"][:10]) or "файлы не менял"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
