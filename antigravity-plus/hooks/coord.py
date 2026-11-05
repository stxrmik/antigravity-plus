#!/usr/bin/env python3
"""Antigravity Plus — coordination of agents that work on the same project at the same time.

Several agents (Antigravity conversations in the Agent Manager, their subagents, plus Claude Code or your editor)
can change one project at once. A small registry per workspace,
~/.gemini/antigravity-plus/coord/<workspace-slug>.json, records
  agents: {conversationId: {"title", "parent", "last": ts, "files": {path: ts}}}   who changed which file, when
  seen:   {conversationId: {path: [mtime_ns, size, ts]}}                           the version each agent last saw
and is used by
  * hooks/guard.py (PreToolUse): an agent may not write over a file that changed since it last read or wrote it
    (someone else's work would be lost) — it gets a refusal that says who changed it, re-reads and retries;
  * hooks/check_edit.py (PostToolUse on read_file/write_file/edit_file): records what the agent saw and changed;
  * hooks/instructions.py (PreInvocation): heartbeat, and the list of other agents working here right now;
  * /commit: `coord.py mine` — only the files this agent (and its subagents) changed, when others are active.

  coord.py status --workspace DIR            who is working in the project now and on which files
  coord.py mine --conversation ID --workspace DIR   files this conversation and its subagents changed (one per line)

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

import compat
import conversation as conv

COORD_DIR = os.path.expanduser("~/.gemini/antigravity-plus/coord")
INDEX_DB = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
ACTIVE = 10 * 60          # an agent with activity in the last 10 minutes is working right now
KEEP = 24 * 3600          # forget agents idle for a day


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
        self.data.setdefault("agents", {})
        self.data.setdefault("seen", {})
        return self.data

    def __exit__(self, *exc) -> None:
        try:
            if self.write:
                now = time.time()
                old = [a for a, v in self.data["agents"].items() if now - v.get("last", 0) > KEEP]
                for a in old:
                    self.data["agents"].pop(a, None)
                    self.data["seen"].pop(a, None)
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                compat.replace(tmp, self.path)
        finally:
            compat.unlock(self.lock)
            self.lock.close()


def conversation_info(cid: str) -> Tuple[str, str]:
    """(title, parent conversation id) from Antigravity's own conversation index."""
    try:
        con = sqlite3.connect(f"file:{INDEX_DB}?immutable=1", uri=True, timeout=1)
        row = con.execute("SELECT title, preview, parent_conversation_id FROM conversation_summaries "
                          "WHERE conversation_id = ?", (cid,)).fetchone()
        con.close()
    except sqlite3.Error:
        row = None
    return ((row[0] or row[1] or ""), (row[2] or "")) if row else ("", "")


def version(path: str) -> Optional[List[int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _agent(data: dict, cid: str) -> dict:
    now = time.time()
    a = data["agents"].setdefault(cid, {"title": "", "parent": "", "last": now, "files": {}})
    if (not a.get("title") or not a.get("parent")) and now - a.get("looked", 0) > 60:
        a["looked"] = now  # main agents have no parent: don't ask the index on every call
        title, parent = conversation_info(cid)
        if not title:  # Antigravity names conversations a bit later; until then, the user's first request
            st = conv._state(cid)
            first = st["users"][0]["text"] if st and st.get("users") else ""
            title = re.sub(r"\s+", " ", first).strip()[:60] + ("…" if len(first) > 60 else "")
        a["title"] = a.get("title") or title
        a["parent"] = a.get("parent") or parent
    return a


def family(data: dict, cid: str) -> set:
    """This conversation, its parent and its subagents: they work on one task together."""
    fam = {cid}
    parent = data["agents"].get(cid, {}).get("parent")
    if parent:
        fam.add(parent)
    for other, a in data["agents"].items():
        if a.get("parent") in fam:
            fam.add(other)
    return fam


def heartbeat(payload: dict) -> None:
    cid, ws = payload.get("conversationId"), workspace(payload)
    if isinstance(cid, str) and ws:
        with Registry(ws) as data:
            _agent(data, cid)["last"] = time.time()


def record(payload: dict, tool: str, path: str) -> None:
    """After read_file / write_file / edit_file: remember the version this agent saw (and that it changed the file)."""
    cid, ws = payload.get("conversationId"), workspace(payload)
    if not (isinstance(cid, str) and ws and path) or payload.get("error"):
        return
    path = os.path.realpath(path)
    v = version(path)
    with Registry(ws) as data:
        a = _agent(data, cid)
        now = time.time()
        a["last"] = now
        if v:
            data["seen"].setdefault(cid, {})[path] = v + [now]
        if tool in ("write_file", "edit_file"):
            a["files"][path] = now


def check_write(payload: dict, path: str, content: Optional[str] = None) -> Optional[str]:
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
        writer, when = None, 0.0
        fam = family(data, cid)
        for other, a in data["agents"].items():
            ts = a.get("files", {}).get(path, 0)
            if other not in fam and ts > when:
                writer, when = other, ts
        writer_info = data["agents"].get(writer, {}) if writer else {}
    if seen is None:
        if writer and time.time() - when < ACTIVE:
            return refusal(path, ws, writer, writer_info, when, never_read=True)
        return None  # never seen and nobody else touched it recently: nothing to protect
    if seen[:2] != current:
        return refusal(path, ws, writer if writer and when > seen[2] else None, writer_info, when, never_read=False)
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


def refusal(path: str, ws: str, writer: Optional[str], info: dict, when: float, never_read: bool) -> str:
    rel = os.path.relpath(path, ws) if path.startswith(ws + os.sep) else path
    if writer:
        ago = max(1, int((time.time() - when) // 60))
        who = f"другой агент «{info.get('title') or 'без названия'}» (разговор {writer}, {ago} мин назад)"
    else:
        who = "кто-то другой после вашего последнего чтения (другой агент, Claude Code, редактор или ваша же команда)"
    head = (f"файл {rel} изменил {who}, а вы его ещё не читали" if never_read
            else f"файл {rel} изменил {who}")
    return (f"🛡️ Antigravity Plus: {head}. Чтобы не затереть чужую работу, перечитайте файл (read_file) и сделайте "
            "правку поверх его текущей версии." + (f" Если вы работаете над одним и тем же, договоритесь с ним "
                                                    f"через send_message (Recipient: {writer})." if writer else ""))


def others(payload: dict) -> List[dict]:
    """Other agents (not this conversation's family) active in this workspace now, with the files they changed."""
    cid, ws = payload.get("conversationId"), workspace(payload)
    if not (isinstance(cid, str) and ws) or not os.path.isfile(os.path.join(COORD_DIR, slug(ws) + ".json")):
        return []
    now, out = time.time(), []
    with Registry(ws, write=False) as data:
        fam = family(data, cid)
        for other, a in data["agents"].items():
            if other in fam or now - a.get("last", 0) > ACTIVE:
                continue
            files = sorted((p for p, ts in a.get("files", {}).items() if now - ts < 3 * ACTIVE),
                           key=lambda p: -a["files"][p])
            out.append({"id": other, "title": a.get("title") or "", "ago": int((now - a.get("last", 0)) // 60),
                        "files": [os.path.relpath(p, ws) if p.startswith(ws + os.sep) else p for p in files]})
    return out


def others_message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """<other_agents> block for the PreInvocation hook, only when the picture changed since the last one."""
    agents = others(payload)
    key = "|".join(f"{a['id']}:{','.join(a['files'][:8])}" for a in agents)
    if st is not None:
        if st.get("others_key", "") == key:
            return None
        st["others_key"] = key
    if not agents:
        return None
    lines = ['<other_agents source="Antigravity Plus">', "Other agents are working in this project right now:"]
    for a in agents:
        files = ", ".join(a["files"][:8]) + (" …" if len(a["files"]) > 8 else "")
        lines.append(f"- «{a['title'] or 'без названия'}» (conversation {a['id']}, active {a['ago']} min ago)"
                     + (f" — changed: {files}" if files else ""))
    lines.append("Don't overwrite their work: leave their files alone unless your task needs them, re-read a file "
                 "before editing it, and coordinate through send_message (Recipient: <conversation id>) when your "
                 "work overlaps. Commit only your own files.")
    lines.append("</other_agents>")
    return "\n".join(lines)


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
