#!/usr/bin/env python3
"""Antigravity Plus — /standup: what got done across projects in the last day or week.

  standup.py [--days 1] [--workspace DIR]     all projects, or just one

Per project: Antigravity conversations (from Antigravity's own index) with the first and last request and the
task.md checklist state, Claude Code sessions, git commits and uncommitted changes, open handoff notes.
The agent turns this into a short report. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "handoff", "scripts"))
import handoff  # noqa: E402  (also brings in hooks/conversation.py and the memory store)

CHECK = re.compile(r"^\s*[-*+]\s+\[(.)\]\s+(.+?)\s*$", re.M)


def clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def claude_sessions(since: float) -> List[dict]:
    out = []
    for f in glob.glob(os.path.join(handoff.memstore.CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < since:
                continue
            with open(f, "rb") as fh:
                head = fh.read(65536).decode("utf-8", errors="replace")
        except OSError:
            continue
        cwd = re.search(r'"cwd":\s*"((?:\\.|[^"\\])*)"', head)
        s = handoff.claude_summary(f)
        if s and cwd and s["modified"].timestamp() >= since:  # the real last activity, not the file time
            s["path"] = handoff.json_text(cwd.group(1))
            out.append(s)
    return out


def task_state(cid: str) -> str:
    text = handoff.memstore.read_text(os.path.join(handoff.AG, "brain", cid, "task.md"), 200_000)
    items = CHECK.findall(text)
    if not items:
        return ""
    open_items = [t for mark, t in items if mark in " /"]
    return f"task.md: {len(items) - len(open_items)} done, {len(open_items)} open" + (
        " — " + "; ".join(clip(t, 60) for t in open_items[:3]) if open_items else "")


def project_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=1)
    ap.add_argument("--workspace")
    a = ap.parse_args(argv)
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)
    since = since_dt.timestamp()
    only = os.path.realpath(os.path.expanduser(a.workspace)) if a.workspace else None

    projects: Dict[str, dict] = {}
    def bucket(path: str) -> dict:
        p = os.path.realpath(path)
        return projects.setdefault(p, {"ag": [], "cc": []})

    for r in handoff.index_rows():
        if r["modified"] and r["modified"] >= since_dt and r["paths"]:
            bucket(r["paths"][0])["ag"].append(r)
    for s in claude_sessions(since):
        bucket(s["path"])["cc"].append(s)
    if only:
        projects = {p: v for p, v in projects.items() if p == only or p.startswith(only + os.sep)}

    span = "the last day" if a.days <= 1 else f"the last {a.days:g} days"
    when = (lambda t: handoff.local(t)[11:]) if a.days <= 1 else (lambda t: handoff.local(t)[5:])
    out = [f"# Standup — {span} (since {since_dt.astimezone():%Y-%m-%d %H:%M})"]
    if not projects:
        out.append("No conversations in this period.")
    for path, data in sorted(projects.items(), key=lambda kv: -max([c["modified"].timestamp() for c in kv[1]["ag"] + kv[1]["cc"]] or [0])):
        out.append(f"\n## {project_name(path)} — {path}")
        for c in sorted(data["ag"], key=lambda c: c["modified"]):
            st = handoff.messages(c["id"])
            users = st["users"] if st else []
            line = f"- Antigravity {when(c['modified'])} «{clip(c['title'], 70)}», {c['steps']} steps"
            if users:
                line += f" — asked: «{clip(users[0]['text'], 120)}»"
                if len(users) > 1:
                    line += f" … last: «{clip(users[-1]['text'], 100)}»"
            ts = task_state(c["id"])
            out.append(line + (f"\n  {ts}" if ts else ""))
        for s in sorted(data["cc"], key=lambda s: s["modified"]):
            out.append(f"- Claude Code {when(s['modified'])} «{clip(s['title'], 70)}»"
                       + (f" — last: «{clip(s['last'], 120)}»" if s.get("last") else ""))
        if os.path.isdir(path):
            log = handoff.git(["log", f"--since={since_dt.isoformat()}", "--format=%h %s (%cr)"], path)
            if log is not None:
                commits = log.splitlines()
                out.append(f"Commits: {len(commits)}" + ("".join(f"\n  {c}" for c in commits[:12]) if commits else ""))
                dirty = handoff.git(["status", "--porcelain"], path) or ""
                if dirty.strip():
                    out.append(f"Uncommitted: {len(dirty.splitlines())} files")
            notes = handoff.handoff_notes(path)
            if notes:
                out.append("Open handoff notes: " + "; ".join(f"{n['file']} — {clip(n['description'], 90)}" for n in notes[:3]))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
