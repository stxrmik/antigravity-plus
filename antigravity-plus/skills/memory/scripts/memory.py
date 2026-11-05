#!/usr/bin/env python3
"""Antigravity Plus — memory store (auto-memory and /memory), compatible with Claude Code's auto-memory.

Two scopes:
  personal  ~/.gemini/antigravity-plus/memory/personal/   who the user is and how they like to work (every project)
  project   ~/.claude/projects/<slug>/memory/             one per repository: the folder Claude Code's auto-memory
                                                          uses, so both tools share one memory. When sharing is off
                                                          or Claude Code isn't installed:
                                                          ~/.gemini/antigravity-plus/memory/projects/<slug>/
A memory is one Markdown file with frontmatter (name, description, metadata.type). MEMORY.md in the same folder is
the index (one line per memory) that the SessionStart hook puts into every conversation; hooks/memory_sync.py adds
the line for a new memory, so the agent only writes the memory file itself.

  memory.py show   [--workspace DIR]            what the agent remembers + which instruction files it gets
  memory.py paths  [--workspace DIR]            memory folders and settings (JSON)
  memory.py search QUERY [--workspace DIR]      find memories by text (regex, case-insensitive)
  memory.py forget NAME [--scope personal|project] [--workspace DIR]   delete a memory and its index line
  memory.py sync   [--workspace DIR]            add missing index lines, drop lines whose file is gone
  memory.py config [auto-memory on|off] [share-with-claude-code on|off] [recent-conversations N]
                   [finish-tasks on|off] [check-edits on|off] [auto-review on|off] [auto-verify on|off] [protect-secrets on|off]
                   [auto-setup on|off]
                   (settings of the whole plugin live here)

Also imported by the hooks (session_start.py, guard.py, memory_sync.py). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
import compat  # noqa: E402  (atomic writes on Windows, the Python command for hints)

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".gemini", "antigravity-plus")
SETTINGS = os.path.join(DATA, "settings.json")
PLUGIN_MEMORY = os.path.join(DATA, "memory")
PERSONAL = os.path.join(PLUGIN_MEMORY, "personal")
CLAUDE_HOME = os.path.join(HOME, ".claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_HOME, "projects")
PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
INDEX = "MEMORY.md"
TYPES = ("user", "feedback", "project", "reference")
DEFAULTS = {"autoMemory": True, "shareWithClaudeCode": True, "recentConversations": 3, "finishTasks": True,
            "checkEdits": True, "autoReview": True, "autoVerify": True, "protectSecrets": True, "autoSetup": True}

MAX_FILE = 64_000            # bytes read per memory file
INDEX_LINES = 200            # like Claude Code: only the head of MEMORY.md goes into the conversation
INDEX_CHARS = 20_000
PERSONAL_CHARS = 6_000       # personal memories are inlined in full up to this size, then listed
SMART_MIN_LINES = 40         # a bigger project index is trimmed to what the conversation's request is about
SMART_CHARS = 8_000
STOP_WORDS = {"сделай", "сделать", "нужно", "надо", "чтобы", "когда", "который", "которая", "которые", "можно", "давай",
              "пожалуйста", "почему", "сейчас", "потом", "этот", "этого", "этой", "через", "после", "перед", "будет",
              "есть", "были", "было", "the", "and", "for", "with", "that", "this", "from", "into", "please", "make"}
RULE_FILE_LIMIT = 24_000     # Antigravity truncates a rule file above this
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "build", "dist", "target", ".next", "Pods", "DerivedData",
             "__pycache__", "vendor", ".gradle", ".idea", ".build", "out", "coverage"}

FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.S)
LINK = re.compile(r"\]\(<?([^)<>\s]+?\.md)>?\)")


# ─────────────────────────────── settings & locations ───────────────────────────────

def settings() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            s.update({k: d[k] for k, v in DEFAULTS.items() if type(d.get(k)) is type(v)})
    except (OSError, ValueError):
        pass
    return s


def save_settings(**changes) -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d.update(changes)
    os.makedirs(DATA, exist_ok=True)
    _write(SETTINGS, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    return settings()


def slug(path: str) -> str:
    """Claude Code's project folder name: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def git_root(ws: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ws, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def project_root(ws: str) -> str:
    return git_root(ws) or os.path.abspath(ws)


def project_dir(ws: str, s: Optional[dict] = None, root: Optional[str] = None) -> Tuple[str, str]:
    """(folder, store) for the workspace's project memory; store is "claude" (shared) or "plugin"."""
    s = s or settings()
    root = root or project_root(ws)
    if s["shareWithClaudeCode"] and os.path.isdir(CLAUDE_HOME):
        for cand in dict.fromkeys([root, os.path.abspath(ws)]):
            d = os.path.join(CLAUDE_PROJECTS, slug(cand), "memory")
            if os.path.isdir(d):
                return d, "claude"
        return os.path.join(CLAUDE_PROJECTS, slug(root), "memory"), "claude"
    return os.path.join(PLUGIN_MEMORY, "projects", slug(root)), "plugin"


def memory_dir_of(path: str) -> Optional[str]:
    """The memory folder that directly holds `path` (a .md file), or None. Used to auto-approve memory writes."""
    if not path:
        return None
    p = os.path.realpath(os.path.expanduser(path))
    if not p.endswith(".md"):
        return None
    d = os.path.dirname(p)
    if d == os.path.realpath(PERSONAL):
        return d
    if os.path.dirname(d) == os.path.realpath(os.path.join(PLUGIN_MEMORY, "projects")):
        return d
    if os.path.basename(d) == "memory" and os.path.dirname(os.path.dirname(d)) == os.path.realpath(CLAUDE_PROJECTS):
        return d
    return None


def tilde(path: str) -> str:
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + os.sep) else path


def file_link(path: str, label: Optional[str] = None) -> str:
    return f"[{label or tilde(path)}](file://{quote(path)})"


# ─────────────────────────────── reading memories ───────────────────────────────

def _write(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    compat.replace(tmp, path)


def read_text(path: str, limit: int = MAX_FILE) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: Dict[str, str] = {}
    parent = ""
    for ln in m.group(1).splitlines():
        mm = re.match(r"^(\s*)([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if not mm or ln.lstrip().startswith("#"):
            continue
        indent, key, val = mm.groups()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if not indent:
            parent = key if not val else ""
            meta[key] = val
        elif parent == "metadata":
            meta.setdefault(key, val)
    return meta, text[m.end():]


def read_memory(path: str) -> Optional[dict]:
    text = read_text(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    fn = os.path.basename(path)
    return {"file": fn, "path": path, "name": meta.get("name") or fn[:-3],
            "description": meta.get("description", ""), "type": meta.get("type", ""),
            "body": body.strip(), "mtime": mtime}


def list_memories(d: str) -> List[dict]:
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for fn in names:
        if fn.endswith(".md") and fn != INDEX and not fn.startswith("."):
            m = read_memory(os.path.join(d, fn))
            if m:
                out.append(m)
    return sorted(out, key=lambda m: m["mtime"], reverse=True)


def one_line(text: str, n: int = 160) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# ─────────────────────────────── index (MEMORY.md) ───────────────────────────────

def index_line(m: dict) -> str:
    title = re.sub(r"[-_]+", " ", re.sub(r"\.md$", "", m["name"])).strip() or m["file"][:-3]
    title = title[:1].upper() + title[1:]
    desc = one_line(m["description"], 180)
    return f"- [{title}]({m['file']})" + (f" — {desc}" if desc else "")


def indexed_files(text: str) -> set:
    return {os.path.basename(unquote(t)) for t in LINK.findall(text)}


def sync_index(d: str, prune: bool = False) -> Tuple[List[str], List[str]]:
    """Append an index line for every memory MEMORY.md doesn't mention yet. With prune, also drop list lines
    that point at a single file that no longer exists. Never rewrites curated lines. Returns (added, removed)."""
    mems = list_memories(d)
    path = os.path.join(d, INDEX)
    exists = os.path.isfile(path)
    if not mems and not exists:
        return [], []
    lines = read_text(path, 2_000_000).splitlines() if exists else []
    removed: List[str] = []
    if prune:
        keep = []
        for ln in lines:
            links = LINK.findall(ln)
            if (len(links) == 1 and re.match(r"^\s*[-*]\s+\[", ln)
                    and not os.path.exists(os.path.join(d, unquote(links[0])))):
                removed.append(ln)
            else:
                keep.append(ln)
        lines = keep
    have = indexed_files("\n".join(lines))
    added = [index_line(m) for m in sorted(mems, key=lambda m: m["mtime"]) if m["file"] not in have]
    if not added and not removed:
        return [], []
    if not any(ln.strip() for ln in lines):
        lines = ["# Memory index", ""]
    else:
        while not lines[-1].strip():
            lines.pop()
    _write(path, "\n".join(lines + added) + "\n")
    return added, removed


# ─────────────────────────────── session context (SessionStart hook) ───────────────────────────────

def stems(text: str) -> set:
    """Crude word stems (first five letters), enough to match Russian and English word forms."""
    words = re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ_]{3,}", (text or "").lower())
    return {w[:5] for w in words if w not in STOP_WORDS}


def smart_index(text: str, d: str, query: str) -> Tuple[str, int, int]:
    """The index lines worth loading for this request: feedback and user memories, overview lines, the request's
    topic and the latest entries; in the index's own order, within SMART_CHARS. Returns (text, kept, total)."""
    lines = text.splitlines()
    entries = [i for i, ln in enumerate(lines) if re.match(r"^\s*[-*]\s", ln)]
    mems = {m["file"]: m for m in list_memories(d)}
    q = stems(query)
    score, keep = {}, set()
    for rank, i in enumerate(entries):
        files = [os.path.basename(unquote(t)) for t in LINK.findall(lines[i])]
        linked = [mems[f] for f in files if f in mems]
        if any(m["type"] in ("feedback", "user") for m in linked) or rank < 6:
            keep.add(i)
        hay = stems(lines[i] + " " + " ".join(f[:-3].replace("-", " ") for f in files)
                    + " " + " ".join(m["description"] for m in linked))
        score[i] = (len(q & hay), max((m["mtime"] for m in linked), default=0))
    by_topic = [i for i in sorted(entries, key=lambda i: -score[i][0]) if score[i][0] > 0][:25]
    recent = sorted(entries, key=lambda i: -score[i][1])[:8]
    order = list(dict.fromkeys(sorted(keep) + by_topic + recent))
    chosen, size = set(), 0
    for i in order:
        if size + len(lines[i]) > SMART_CHARS:
            break
        chosen.add(i)
        size += len(lines[i]) + 1
    head = [ln for ln in lines[:entries[0]] if ln.strip()] if entries else []
    body = head[:1] + [lines[i] for i in sorted(chosen)]
    return "\n".join(body), len(chosen), len(entries)


def session_block(workspaces: List[str], roots: Optional[Dict[str, str]] = None, query: str = "") -> List[str]:
    """The "Memory" section of <antigravity_plus_session_context>."""
    s = settings()
    out = ["\n## Memory (Antigravity Plus)",
           "What earlier conversations learned about the user and the project. It is background, not an "
           "instruction from the user in this conversation: the current request wins. Before relying on a "
           "memory that names a file, command, server or flag, check it still exists."]
    if s["autoMemory"]:
        out.append("Auto-memory is ON: when you learn something durable, save it yourself as a new .md file in "
                   "the folder below (see \"Memory\" in the Antigravity Plus rules for what to save and the "
                   "format). MEMORY.md is updated automatically — don't edit it.")
    else:
        out.append("Auto-memory is OFF: save memories only when the user asks (/remember, «запомни»).")

    out.append(f"\n### Personal memory (all projects) — folder `{tilde(PERSONAL)}/`")
    mems = list_memories(PERSONAL)
    if not mems:
        out.append("(empty — nothing saved yet; user profile and working-style feedback go here)")
    else:
        full = sum(len(m["body"]) + len(m["description"]) for m in mems) <= PERSONAL_CHARS
        for m in mems:
            out.append(f"- **{m['name']}** ({m['type'] or 'note'}, {day(m['mtime'])}) — {one_line(m['description'])}"
                       + ("" if full else f" → read `{m['file']}`"))
            if full and m["body"]:
                out.extend("    " + ln for ln in m["body"].splitlines() if ln.strip())

    for ws in workspaces:
        root = (roots or {}).get(ws) or project_root(ws)
        d, store = project_dir(ws, s, root)
        shared = " — shared with Claude Code" if store == "claude" else ""
        out.append(f"\n### Project memory for `{root}` — folder `{tilde(d)}/`{shared}")
        idx = os.path.join(d, INDEX)
        text = read_text(idx, INDEX_CHARS + 1) if os.path.isfile(idx) else ""
        if text.strip():
            lines = text.splitlines()
            if query.strip() and len(lines) > SMART_MIN_LINES:
                body, kept, total = smart_index(read_text(idx, 2_000_000), d, query)
                out.append(f"Index (MEMORY.md), {kept} of {total} entries: the ones about this request, how the user "
                           "wants work done, and the latest. Read a memory file when its line is relevant; for anything "
                           f"else search first: `{compat.python_cmd()} \"{os.path.abspath(__file__)}\" search \"<words>\" "
                           f"--workspace \"{ws}\"`.")
                out.append(body.rstrip())
                continue
            head = lines[:INDEX_LINES]
            body = "\n".join(head)[:INDEX_CHARS]
            out.append("Index (MEMORY.md). Read a memory file when its line is relevant to the task:")
            out.append(body.rstrip())
            if len(lines) > INDEX_LINES or len(text) > INDEX_CHARS:
                out.append(f"… [index truncated — read {tilde(idx)} for the rest]")
        else:
            mems = list_memories(d)
            if mems:
                out.append("Memories (read a file when its line is relevant):")
                out.extend(index_line(m) for m in mems[:60])
            else:
                out.append("(empty — nothing saved for this project yet)")
    return out


# ─────────────────────────────── instruction files (for /memory show) ───────────────────────────────

def rule_files(ws: str, root: str) -> List[Tuple[str, str, str]]:
    """(path, scope label, kind) for every instruction file the agent gets in this workspace."""
    out: List[Tuple[str, str, str]] = []
    for p in ("~/.gemini/config/AGENTS.md", "~/.gemini/config/GEMINI.md", "~/.gemini/GEMINI.md"):
        out.append((os.path.expanduser(p), "все проекты", "global"))
    out.append((os.path.join(PLUGIN, "rules", "AGENTS.md"), "все проекты (правила плагина)", "plugin"))
    # walk-up from the workspace to the repo root (these are loaded natively)
    chain, cur = [], os.path.abspath(ws)
    while True:
        chain.append(cur)
        if cur == root or os.path.dirname(cur) == cur or not cur.startswith(root):
            break
        cur = os.path.dirname(cur)
    for dpath in reversed(chain):
        for n in ("AGENTS.md", "GEMINI.md"):
            p = os.path.join(dpath, n)
            if os.path.isfile(p):
                out.append((p, "этот проект", "project"))
    # nested rule files below the root load when the agent works in that folder
    seen = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(x for x in dns if x not in SKIP_DIRS and not x.startswith("."))
        seen += 1
        if seen > 3000 or dp[len(root):].count(os.sep) > 4:
            dns[:] = []
        if dp == root:
            continue
        for n in ("AGENTS.md", "GEMINI.md"):
            if n in fns:
                out.append((os.path.join(dp, n), f"в папке {os.path.relpath(dp, root)}/", "nested"))
    for rd in (".agents/rules", ".agent/rules", "_agents/rules", "_agent/rules"):
        d = os.path.join(root, rd)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".md"):
                    meta, _ = parse_frontmatter(read_text(os.path.join(d, fn), 4000))
                    trig = meta.get("trigger") or "always_on"
                    label = trig + (f": {meta['glob']}" if meta.get("glob") else "")
                    out.append((os.path.join(d, fn), label, "agents-rule"))
    for n, label in (("CLAUDE.md", "этот проект (Claude Code, через хук)"),
                     ("CLAUDE.local.md", "личное, этот проект (Claude Code, через хук)")):
        out.append((os.path.join(root, n), label, "claude"))
    d = os.path.join(root, ".claude", "rules")
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                meta, _ = parse_frontmatter(read_text(os.path.join(d, fn), 4000))
                out.append((os.path.join(d, fn), "Claude Code rule" + (f", paths: {meta['paths']}" if meta.get("paths") else ""), "claude"))
    out.append((os.path.join(CLAUDE_HOME, "CLAUDE.md"), "все проекты (Claude Code, через хук)", "claude-global"))
    return out


def kb(n: int) -> str:
    return f"{n / 1024:.1f} КБ" if n >= 1024 else f"{n} Б"


# ─────────────────────────────── commands ───────────────────────────────

def memory_table(mems: List[dict], limit: int) -> List[str]:
    out = ["| Запись | Тип | Изменена | О чём |", "|---|---|---|---|"]
    for m in mems[:limit]:
        desc = one_line(m["description"], 110).replace("|", "\\|")
        out.append(f"| {file_link(m['path'], m['name'])} | {m['type'] or '—'} | {day(m['mtime'])} | {desc} |")
    if len(mems) > limit:
        out.append(f"\n…и ещё {len(mems) - limit} (показаны последние изменённые)")
    return out


def cmd_show(ws: str) -> str:
    s = settings()
    root = project_root(ws)
    d, store = project_dir(ws, s, root)
    rep = ["## 🧠 Память Antigravity Plus", "",
           f"**Автопамять:** {'включена — агент сам сохраняет важное' if s['autoMemory'] else 'выключена — только по просьбе (/remember)'}"
           f" · **общая с Claude Code:** {'да' if store == 'claude' else 'нет'} · проект: `{root}`"]

    personal = list_memories(PERSONAL)
    rep += ["", "### Личная память (для всех проектов)",
            f"Папка: {file_link(PERSONAL)} · записей: {len(personal)}"]
    rep += memory_table(personal, 30) if personal else ["Пока пусто."]

    proj = list_memories(d)
    rep += ["", "### Память проекта",
            f"Папка: {file_link(d)}" + (" — общая с Claude Code" if store == "claude" else "") + f" · записей: {len(proj)}"]
    idx = os.path.join(d, INDEX)
    if os.path.isfile(idx):
        text = read_text(idx, 2_000_000)
        n = len(text.splitlines())
        warn = n > INDEX_LINES or len(text) > INDEX_CHARS
        rep.append(f"{'⚠️' if warn else '✅'} индекс {file_link(idx, 'MEMORY.md')}: {n} строк, {kb(len(text.encode()))}"
                   + (f" — в разговор попадают первые {INDEX_LINES} строк / {INDEX_CHARS // 1000} тыс. символов; "
                      "сожмите его через «/memory почисти»" if warn else ""))
        missing = [m["file"] for m in proj if m["file"] not in indexed_files(text)]
        if missing:
            rep.append(f"⚠️ не в индексе: {', '.join(missing[:8])}" + (" …" if len(missing) > 8 else "")
                       + " — `memory.py sync` добавит строки")
    rep += memory_table(proj, 25) if proj else ["Пока пусто."]

    rep += ["", "### Инструкции, которые получает агент", "| Файл | Где действует | Размер |", "|---|---|---|"]
    files = list({p: (p, sc, k) for p, sc, k in rule_files(ws, root)}.values())  # walk-up and nested can overlap
    if not any(kind == "project" for _p, _s, kind in files):
        rep.append("| `AGENTS.md` | этот проект | — нет, создайте через `/init` |")
    for path, scope, kind in files:
        if not os.path.isfile(path):
            if path == os.path.expanduser("~/.gemini/config/AGENTS.md"):
                rep.append(f"| `{tilde(path)}` | {scope} | — нет (личные правила пишите в память или сюда) |")
            continue
        size = os.path.getsize(path)
        flag = " ⚠️ обрезается" if size > RULE_FILE_LIMIT and kind not in ("claude", "claude-global") else ""
        rel = os.path.relpath(path, root) if path.startswith(root + os.sep) else tilde(path)
        rep.append(f"| {file_link(path, rel)} | {scope} | {kb(size)}{flag} |")
    return "\n".join(rep)


SCOPE_LABEL = {"personal": "личная", "project": "проект"}
SCOPE_NAME = {"personal": "личная память", "project": "память проекта"}


def scopes(ws: str, scope: Optional[str]) -> List[Tuple[str, str]]:
    out = []
    if scope in (None, "personal"):
        out.append(("personal", PERSONAL))
    if scope in (None, "project"):
        out.append(("project", project_dir(ws)[0]))
    return out


def cmd_search(query: str, ws: str) -> str:
    try:
        rx = re.compile(query, re.I)
    except re.error:
        rx = re.compile(re.escape(query), re.I)
    out = []
    for label, d in scopes(ws, None):
        for m in list_memories(d):
            hay = "\n".join((m["name"], m["description"], m["body"]))
            if not rx.search(hay):
                continue
            out.append(f"- {file_link(m['path'], m['name'])} ({SCOPE_LABEL[label]} · {m['type'] or '—'} · {day(m['mtime'])}) — "
                       f"{one_line(m['description'], 140)}")
            for ln in m["body"].splitlines():
                if rx.search(ln):
                    out.append(f"    > {one_line(ln, 160)}")
                    break
    return "\n".join(out) if out else f"Ничего не найдено по «{query}»."


def find(name: str, ws: str, scope: Optional[str]) -> List[Tuple[str, dict]]:
    key = re.sub(r"\.md$", "", name.strip().lower())
    hits = []
    for label, d in scopes(ws, scope):
        for m in list_memories(d):
            if key in (m["file"][:-3].lower(), m["name"].lower()):
                hits.append((label, m))
    return hits


def cmd_forget(name: str, ws: str, scope: Optional[str]) -> Tuple[int, str]:
    hits = find(name, ws, scope)
    if not hits:
        return 1, f"Записи «{name}» нет. Посмотрите список: memory.py show"
    if len(hits) > 1:
        return 1, "Найдено несколько записей — уточните --scope:\n" + "\n".join(
            f"- {SCOPE_NAME[label]}: {tilde(m['path'])}" for label, m in hits)
    label, m = hits[0]
    os.remove(m["path"])
    d = os.path.dirname(m["path"])
    _added, removed = sync_index(d, prune=True)
    idx = read_text(os.path.join(d, INDEX), 2_000_000)
    left = [ln for ln in idx.splitlines() if m["file"] in indexed_files(ln)]
    msg = f"Удалено: {tilde(m['path'])} ({SCOPE_NAME[label]})."
    if removed:
        msg += f" Из MEMORY.md убрано строк: {len(removed)}."
    if left:
        msg += "\nЕщё упоминается в MEMORY.md (поправьте вручную):\n" + "\n".join(f"  {ln}" for ln in left)
    return 0, msg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("show", "paths", "sync"):
        sp = sub.add_parser(name)
        sp.add_argument("--workspace", default=os.getcwd())
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--workspace", default=os.getcwd())
    sp = sub.add_parser("forget")
    sp.add_argument("name")
    sp.add_argument("--scope", choices=("personal", "project"))
    sp.add_argument("--workspace", default=os.getcwd())
    sp = sub.add_parser("config")
    sp.add_argument("pairs", nargs="*",
                    help="auto-memory on|off, share-with-claude-code on|off, recent-conversations N, "
                         "finish-tasks on|off, check-edits on|off, auto-review on|off, auto-verify on|off, "
                         "protect-secrets on|off, auto-setup on|off")
    a = ap.parse_args(argv)
    ws = os.path.abspath(os.path.expanduser(getattr(a, "workspace", os.getcwd())))

    if a.cmd in (None, "show"):
        print(cmd_show(ws))
    elif a.cmd == "paths":
        s = settings()
        d, store = project_dir(ws, s)
        print(json.dumps({"personal": PERSONAL, "project": d, "projectStore": store, "projectRoot": project_root(ws),
                          **s}, ensure_ascii=False, indent=2))
    elif a.cmd == "search":
        print(cmd_search(a.query, ws))
    elif a.cmd == "forget":
        code, msg = cmd_forget(a.name, ws, a.scope)
        print(msg)
        return code
    elif a.cmd == "sync":
        for label, d in scopes(ws, None):
            added, removed = sync_index(d, prune=True)
            print(f"{label}: {tilde(d)} — добавлено строк {len(added)}, убрано {len(removed)}")
    elif a.cmd == "config":
        keys = {"auto-memory": "autoMemory", "share-with-claude-code": "shareWithClaudeCode",
                "recent-conversations": "recentConversations", "finish-tasks": "finishTasks",
                "check-edits": "checkEdits", "auto-review": "autoReview", "auto-verify": "autoVerify",
                "protect-secrets": "protectSecrets", "auto-setup": "autoSetup"}
        p = a.pairs
        if len(p) % 2:
            ap.error("config takes pairs: <key> <value>")
        changes = {}
        for k, v in zip(p[::2], p[1::2]):
            key = keys.get(k)
            if key == "recentConversations" and v.isdigit():
                changes[key] = int(v)
            elif key and key != "recentConversations" and v in ("on", "off"):
                changes[key] = v == "on"
            else:
                ap.error(f"bad setting: {k} {v} (auto-memory on|off, share-with-claude-code on|off, "
                         "recent-conversations N, finish-tasks on|off, check-edits on|off, auto-review on|off, "
                         "auto-verify on|off, protect-secrets on|off, auto-setup on|off)")
        s = save_settings(**changes) if changes else settings()
        print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
