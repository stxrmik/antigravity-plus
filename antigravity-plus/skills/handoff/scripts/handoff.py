#!/usr/bin/env python3
"""Antigravity Plus — continuity between conversations: recent conversations and /handoff notes.

  handoff.py recent  [--workspace DIR] [--exclude ID] [--limit N]   handoff notes + recent conversations here
                                                                    (Antigravity and Claude Code)
  handoff.py collect --conversation ID [--workspace DIR]            facts about one conversation (Antigravity
                                                                    conversation or Claude Code session id): the
                                                                    user's messages, restrictions in force, the
                                                                    agent's last answer, files, commits, checklist
  handoff.py list    [--workspace DIR]                              handoff notes in the project memory

A handoff note is a project memory (skills/memory/scripts/memory.py) named handoff-<date>-<topic>.md. The next
conversation — here or in Claude Code, which shares the project memory — sees it and picks the work up. Antigravity
conversations come from Antigravity's own index (~/.gemini/antigravity/conversation_summaries.db, opened read-only)
and their transcripts (user messages cached by hooks/conversation.py); Claude Code sessions from
~/.claude/projects/<slug>/<session>.jsonl. Imported by hooks/session_start.py. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
import conversation as conv  # noqa: E402
import memory as memstore  # noqa: E402

AG = os.path.expanduser("~/.gemini/antigravity")
INDEX_DB = os.path.join(AG, "conversation_summaries.db")
SCRIPT = os.path.join(HERE, "handoff.py")
RECENT_DAYS = 45
HEAD, TAIL = 262_144, 600_000   # bytes read from the start / end of a Claude Code session file
NOTE_SLACK = dt.timedelta(minutes=10)
NOTABLE = re.compile(r"\bgit\s+(commit|push|tag)\b|deploy|\bscp\s|\brsync\b.*:|docker\s+push|publish")
CLAUDE_EDITS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
TODO_MARK = {"completed": "x", "in_progress": "/", "pending": " "}
TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                          "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                          "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                          "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


# ─────────────────────────────── helpers ───────────────────────────────

def clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def local(ts: Optional[dt.datetime]) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else "?"


def parse_time(value) -> Optional[dt.datetime]:
    if not value:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(value).strip().replace("Z", "+00:00"))  # 3.9 takes ≤ 6 digits
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def mtime(path: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)


def json_text(escaped: str) -> str:
    """The value of a JSON string literal captured without its quotes."""
    try:
        return json.loads(f'"{escaped}"')
    except ValueError:
        return escaped


def transcript(cid: str) -> str:
    return os.path.join(AG, "brain", cid, ".system_generated", "logs", "transcript_full.jsonl")


def same_dir(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def topic_slug(text: str, words: int = 4) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (text or "").lower().translate(TRANSLIT)).strip("-")
    return "-".join(t.split("-")[:words]) or "work"


def git(args: List[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


# ─────────────────────────────── Antigravity conversations ───────────────────────────────

def index_rows() -> List[dict]:
    """Top-level conversations from Antigravity's own index (what its sidebar shows)."""
    try:
        con = sqlite3.connect(f"file:{INDEX_DB}?immutable=1", uri=True, timeout=1)
        rows = con.execute("SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, "
                           "nesting_depth, status FROM conversation_summaries").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for cid, title, preview, steps, modified, uris, depth, status in rows:
        try:
            paths = [unquote(urlparse(u).path) for u in json.loads(uris or "[]")]
        except ValueError:
            paths = []
        if not depth:
            out.append({"id": cid, "title": title or preview or "", "steps": steps or 0,
                        "modified": parse_time(modified), "paths": paths, "running": "RUNNING" in (status or "")})
    return out


def scan_brain() -> List[dict]:
    """Fallback when the index can't be read: the workspace line our SessionStart hook writes into each transcript."""
    out = []
    try:
        ids = os.listdir(os.path.join(AG, "brain"))
    except OSError:
        return out
    for cid in ids:
        tp = transcript(cid)
        try:
            with open(tp, encoding="utf-8", errors="replace") as f:
                head = f.read(65536)
            when = mtime(tp)
        except OSError:
            continue
        if '"USER_INPUT"' in head:  # subagents have none
            paths = [p.strip() for p in re.findall(r"## Workspace: ([^\\\n]+)", head)]
            out.append({"id": cid, "title": "", "steps": 0, "paths": paths, "modified": when, "running": False})
    return out


def antigravity_conversations(dirs: List[str], exclude: Optional[str] = None) -> List[dict]:
    rows = index_rows() or scan_brain()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENT_DAYS)
    hits = [r for r in rows if r["id"] != exclude and r["modified"] and r["modified"] >= cutoff
            and any(same_dir(p, d) for p in r["paths"] for d in dirs)]
    return sorted(hits, key=lambda r: r["modified"], reverse=True)


def messages(cid: str) -> Optional[dict]:
    """The user's messages of a conversation (cached by hooks/conversation.py)."""
    return conv.load({"conversationId": cid, "transcriptPath": transcript(cid)})


def antigravity_facts(cid: str) -> dict:
    row = next((r for r in index_rows() if r["id"] == cid), {})
    st = messages(cid)
    users = st["users"] if st else []
    files: Dict[str, int] = {}
    cmds: List[str] = []
    answer = ""
    skip = (os.path.join(AG, "brain"), memstore.PLUGIN_MEMORY, memstore.CLAUDE_PROJECTS)
    try:
        with open(transcript(cid), encoding="utf-8", errors="replace") as f:
            for raw in f:
                if '"PLANNER_RESPONSE"' not in raw[:400]:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                calls = d.get("tool_calls") or []
                if not calls and (d.get("content") or "").strip():
                    answer = d["content"]
                for c in calls:
                    args = c.get("args") or {}
                    if c.get("name") in ("write_file", "edit_file"):
                        p = args.get("Path") or args.get("TargetFile") or ""
                        if p and not p.startswith(skip):
                            files[p] = files.get(p, 0) + 1
                    elif c.get("name") == "run_command" and NOTABLE.search(args.get("CommandLine") or ""):
                        cmds.append(args["CommandLine"])
    except OSError:
        pass
    return {"kind": "Antigravity conversation", "id": cid, "title": row.get("title") or "",
            "users": users, "started": parse_time(users[0]["ts"]) if users and users[0].get("ts") else None,
            "modified": row.get("modified"), "steps": row.get("steps") or (st["last_step"] + 1 if st else 0),
            "compactions": len(st["checkpoints"]) if st else 0, "answer": answer, "files": files, "cmds": cmds,
            "checklist": memstore.read_text(os.path.join(AG, "brain", cid, "task.md"), 6000).strip(),
            "checklist_name": "task.md"}


# ─────────────────────────────── Claude Code sessions ───────────────────────────────

def claude_session_files(ws: str) -> List[str]:
    d, store = memstore.project_dir(ws)
    if store != "claude":
        return []
    folder = os.path.dirname(d)
    cutoff = dt.datetime.now().timestamp() - RECENT_DAYS * 86400
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".jsonl")]
    except OSError:
        return []
    return sorted((f for f in files if os.path.getmtime(f) >= cutoff), key=os.path.getmtime, reverse=True)


def find_claude_session(sid: str) -> Optional[str]:
    hits = glob.glob(os.path.join(memstore.CLAUDE_PROJECTS, "*", glob.escape(sid) + ".jsonl"))
    return hits[0] if hits else None


def claude_prompt(d: dict) -> str:
    """The text the user typed, for a user line of a Claude Code session ("" for tool results, meta, commands)."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return ""
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list) and not any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        parts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
    else:
        return ""
    return "\n".join(p for p in parts if p.strip() and not p.lstrip().startswith(("<", "[Image"))).strip()


def claude_summary(path: str) -> dict:
    """Title, first and latest prompt of a session — cheap (head and tail only), for the recent list."""
    first, title, last = "", "", ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(HEAD).decode("utf-8", errors="replace")
            f.seek(max(0, size - TAIL))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    for line in head.split("\n")[:-1]:
        if '"type":"user"' in line.replace(" ", ""):
            try:
                first = claude_prompt(json.loads(line))
            except ValueError:
                continue
            if first:
                break
    t = re.findall(r'"customTitle":\s*"((?:\\.|[^"\\])*)"', tail)
    p = re.findall(r'"lastPrompt":\s*"((?:\\.|[^"\\])*)"', tail)
    title, last = (json_text(t[-1]) if t else ""), (json_text(p[-1]) if p else "")
    # Claude Code touches old session files during its housekeeping, so the file time isn't the last activity:
    # the newest timestamp inside the session is.
    stamps = re.findall(r'"timestamp":\s*"([0-9T:.\-]+Z?)"', tail)
    when = parse_time(stamps[-1]) if stamps else None
    # No "running" flag: an open but idle Claude Code session (say, out of quota) is exactly what to continue.
    return {"kind": "Claude Code", "id": os.path.basename(path)[:-6], "title": title, "first": first, "last": last,
            "modified": when or mtime(path), "steps": 0, "compactions": 0}


def claude_facts(path: str) -> dict:
    users, files, cmds = [], {}, []
    answer, title, todos = "", "", None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t = d.get("type")
            if t == "custom-title":
                title = d.get("customTitle") or title
            elif t == "user":
                text = claude_prompt(d)
                if text:
                    users.append({"ts": d.get("timestamp", ""), "text": text[:conv.MSG_LIMIT]})
            elif t == "assistant" and not d.get("isSidechain"):
                content = (d.get("message") or {}).get("content") or []
                texts = [x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text"]
                if any(x.strip() for x in texts):
                    answer = "\n".join(texts)
                for x in content:
                    if not isinstance(x, dict) or x.get("type") != "tool_use":
                        continue
                    name, inp = x.get("name"), x.get("input") or {}
                    if name in CLAUDE_EDITS and inp.get(CLAUDE_EDITS[name]):
                        p = inp[CLAUDE_EDITS[name]]
                        files[p] = files.get(p, 0) + 1
                    elif name == "Bash" and NOTABLE.search(inp.get("command") or ""):
                        cmds.append(inp["command"])
                    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        todos = inp["todos"]
    checklist = "\n".join(f"- [{TODO_MARK.get(td.get('status'), ' ')}] {td.get('content', '')}"
                          for td in todos or [] if isinstance(td, dict))
    return {"kind": "Claude Code session", "id": os.path.basename(path)[:-6], "title": title, "users": users,
            "started": parse_time(users[0]["ts"]) if users else None, "modified": mtime(path), "steps": 0,
            "compactions": 0, "answer": answer, "files": files, "cmds": cmds, "checklist": checklist,
            "checklist_name": "TodoWrite"}


# ─────────────────────────────── handoff notes & the recent list ───────────────────────────────

def handoff_notes(ws: str) -> List[dict]:
    d, _store = memstore.project_dir(ws)
    notes = []
    for m in memstore.list_memories(d):
        if m["file"].startswith("handoff-"):
            meta, _ = memstore.parse_frontmatter(memstore.read_text(m["path"], 4000))
            m["conversation"] = meta.get("conversation", "")
            notes.append(m)
    return notes


def recent(ws: str, root: Optional[str], exclude: Optional[str], limit: int) -> List[dict]:
    """The newest conversations of this project, Antigravity and Claude Code together."""
    dirs = list(dict.fromkeys([ws] + ([root] if root else [])))
    items = []
    for c in antigravity_conversations(dirs, exclude)[:limit]:
        st = messages(c["id"])
        users = st["users"] if st else []
        items.append({"kind": "Antigravity", "id": c["id"], "title": c["title"], "modified": c["modified"],
                      "steps": c["steps"], "compactions": len(st["checkpoints"]) if st else 0, "active": c["running"],
                      "first": users[0]["text"] if users else "",
                      "last": users[-1]["text"] if len(users) > 1 else ""})
    for path in claude_session_files(ws)[:limit]:
        s = claude_summary(path)
        if s:
            items.append(s)
    return sorted(items, key=lambda x: x["modified"], reverse=True)[:limit]


def start_point(items: List[dict], notes: List[dict]) -> Optional[dict]:
    """What «продолжи» should pick up: the newest handoff note, unless a conversation that finished after it (and
    isn't the one that wrote it) exists — then that conversation. Antigravity conversations still running are skipped."""
    idle = [it for it in items if not it.get("active")]
    note, newest = (notes[0] if notes else None), (idle[0] if idle else None)
    if note:
        written = dt.datetime.fromtimestamp(note["mtime"], dt.timezone.utc)
        if not newest or newest["id"] == note.get("conversation") or written + NOTE_SLACK >= newest["modified"]:
            return {"note": note}
    return {"conversation": newest} if newest else None


def recent_block(ws: str, root: Optional[str] = None, exclude: Optional[str] = None,
                 limit: Optional[int] = None) -> List[str]:
    limit = memstore.settings()["recentConversations"] if limit is None else limit
    if limit <= 0:
        return []
    items = recent(ws, root, exclude, limit)
    notes = handoff_notes(ws)
    start = start_point(items, notes)
    if not start:
        return []
    forget = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
    out = ["\n### Continuing earlier work in this project",
           "If the user asks to continue («продолжи», «на чём остановились», «доделай») without saying what, take the "
           "item marked ▶ (the `handoff` skill has the details):",
           f"1. A note: read the file. A conversation: `{compat.python_cmd()} \"{SCRIPT}\" collect --conversation <id>` prints its "
           "user messages, last answer, changed files and checklist (Antigravity and Claude Code ids alike); don't "
           "read transcripts or session files directly.",
           "2. Check the current state (git status, the files it names), then tell the user in 2–4 lines what you're "
           "picking up — goal, what's left, restrictions — before doing anything.",
           "3. Do the open items, keeping its restrictions. Then update the note's Сделано/Осталось; when nothing is "
           f"left, delete it: `{compat.python_cmd()} \"{forget}\" forget <note name> --scope project --workspace \"{ws}\"`."]
    if "note" in start:
        n = start["note"]
        out.append(f"▶ Start here: handoff note `{n['path']}` — {clip(n['description'], 160)}")
    else:
        c = start["conversation"]
        out.append(f"▶ Start here: {c['kind']} «{clip(c['title'] or c['first'], 70)}», id {c['id']}"
                   + (" (newer than the handoff notes)" if notes else ""))
    if notes:
        out.append("Handoff notes (newest first):")
        for n in notes[:3]:
            src = f" (from conversation {n['conversation']})" if n.get("conversation") else ""
            out.append(f"- {dt.datetime.fromtimestamp(n['mtime']):%Y-%m-%d %H:%M} · {n['file']} — "
                       f"{clip(n['description'], 140)}{src}")
    if items:
        by_conv = {n["conversation"]: n["file"] for n in notes if n.get("conversation")}
        out.append("Recent conversations (newest first):")
        for it in items:
            meta = [f"{it['steps']} steps" if it.get("steps") else "",
                    f"compacted {it['compactions']}×" if it.get("compactions") else "",
                    "running now — don't continue it" if it.get("active") else ""]
            meta = ", ".join(x for x in meta if x)
            out.append(f"- {local(it['modified'])} · {it['kind']} «{clip(it['title'] or it['first'], 70)}»"
                       + (f" · {meta}" if meta else "") + f" · id {it['id']}"
                       + (f" · handoff note: {by_conv[it['id']]}" if it["id"] in by_conv else ""))
            if it["first"]:
                out.append(f"  first: «{clip(it['first'], 140)}»"
                           + (f" · last: «{clip(it['last'], 140)}»" if it["last"] and it["last"] != it["first"] else ""))
    return out


# ─────────────────────────────── /handoff collect ───────────────────────────────

def collect(cid: str, ws: str) -> str:
    if os.path.isfile(transcript(cid)):
        f = antigravity_facts(cid)
    else:
        path = find_claude_session(cid)
        if not path:
            return f"No Antigravity conversation or Claude Code session with id {cid}."
        f = claude_facts(path)
    users = f["users"]
    root = memstore.project_root(ws)
    d, store = memstore.project_dir(ws)
    title = f["title"] or (users[0]["text"] if users else "")
    name = f"handoff-{dt.date.today().isoformat()}-{topic_slug(title)}"

    out = [f"# Handoff facts — {f['kind']} {cid}", f"Title: «{clip(title, 120)}»",
           f"Workspace: {ws} · repo root: {root}",
           f"Started: {local(f['started'])} · last activity: {local(f['modified'])}"
           + (f" · steps: {f['steps']}" if f["steps"] else "")
           + (f" · compacted: {f['compactions']}×" if f["compactions"] else ""),
           f"A note for this work goes to: {os.path.join(d, name + '.md')}"
           + (" (project memory, shared with Claude Code)" if store == "claude" else "")
           + f" — frontmatter name: {name} (shorten the topic part)"]

    out += ["", "## The user's messages (oldest first)"]
    out += [f"{i}. «{clip(u['text'], 600)}»" for i, u in enumerate(users, 1)] or ["(no user messages found)"]

    bans = conv.active_bans(users)
    what = {"push": "no push / deploy / uploads to servers", "commit": "no git commit"}
    out += ["", "## Restrictions still in force (the user hasn't lifted them)"]
    out += [f"- {what[k]} — message {v['n']}: «{clip(v['text'], 200)}»" for k, v in bans.items()] or ["- none detected"]

    if f["answer"].strip():
        ans = f["answer"].strip()
        out += ["", "## The agent's last answer (its own account of where things stood)",
                ans if len(ans) <= 2500 else ans[:2499] + "…"]

    files = f["files"]
    out += ["", f"## Files written or edited ({len(files)})"]
    out += [f"- {p}" + (f" (×{n})" if n > 1 else "") for p, n in list(files.items())[:40]] or ["- none"]
    if len(files) > 40:
        out.append(f"- … +{len(files) - 40} more")
    if f["cmds"]:
        seen: Dict[str, int] = {}
        for c in f["cmds"]:  # the same helper command repeated many times → one line with ×N
            key = clip(c, 70)
            seen[key] = seen.pop(key, 0) + 1
        out += ["", "## Commits, pushes, deploys, uploads run (latest last)"]
        out += [f"- `{k}`" + (f" ×{n}" if n > 1 else "") for k, n in list(seen.items())[-12:]]

    out += ["", "## Git now"]
    branch = git(["branch", "--show-current"], root)
    if branch is None:
        out.append("not a git repository")
    else:
        status = git(["status", "--porcelain"], root) or ""
        out.append(f"branch `{branch or '(detached)'}` · uncommitted files: {len(status.splitlines())}")
        out += [f"  {ln}" for ln in status.splitlines()[:20]]
        if f["started"]:
            log = git(["log", f"--since={f['started'].isoformat()}", "--format=%h %s (%cr)"], root) or ""
            out.append(f"commits since the conversation started: {len(log.splitlines())}")
            out += [f"  {ln}" for ln in log.splitlines()[:15]]

    if f["checklist"]:
        out += ["", f"## Checklist ({f['checklist_name']})", f["checklist"]]

    notes = handoff_notes(ws)
    out += ["", "## Existing handoff notes in this project (update one instead of adding a duplicate)"]
    out += [f"- {n['file']} — {clip(n['description'], 140)}"
            + (f" (from conversation {n['conversation']})" if n.get("conversation") else "") for n in notes] or ["- none"]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("recent")
    sp.add_argument("--workspace", default=os.getcwd())
    sp.add_argument("--exclude")
    sp.add_argument("--limit", type=int, default=5)
    sp = sub.add_parser("collect")
    sp.add_argument("--conversation", required=True)
    sp.add_argument("--workspace", default=os.getcwd())
    sp = sub.add_parser("list")
    sp.add_argument("--workspace", default=os.getcwd())
    a = ap.parse_args(argv)
    ws = os.path.abspath(os.path.expanduser(a.workspace))
    if a.cmd == "recent":
        lines = recent_block(ws, memstore.git_root(ws), a.exclude, a.limit)
        print("\n".join(lines).strip() or "Других разговоров в этом проекте не найдено.")
    elif a.cmd == "collect":
        if not re.fullmatch(r"[\w-]{8,80}", a.conversation):
            ap.error("--conversation must be a conversation or session id")
        print(collect(a.conversation, ws))
    else:
        notes = handoff_notes(ws)
        if not notes:
            print("Заметок о передаче в памяти проекта нет.")
        for n in notes:
            print(f"- {memstore.file_link(n['path'], n['file'])} · {dt.datetime.fromtimestamp(n['mtime']):%Y-%m-%d %H:%M} — "
                  f"{n['description']}" + (f" · разговор {n['conversation']}" if n.get("conversation") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
