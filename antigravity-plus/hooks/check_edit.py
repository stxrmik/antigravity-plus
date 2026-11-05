#!/usr/bin/env python3
"""Antigravity Plus — PostToolUse hook: checks every file the agent writes (hooks/checks.py) and queues the result.
It also records, for every read_file / write_file / edit_file, the file version the agent saw (hooks/coord.py).

PostToolUse can't talk to the agent, so problems wait in ~/.gemini/antigravity-plus/state/<id>.checks.json until
hooks/instructions.py (PreInvocation) shows them right before the agent's next step; hooks/stop.py re-checks files
that are still broken when the agent wants to finish. Errors only: every syntax error each time, linter errors only
when they are new since the previous check of that file (on the first check a file full of old linter errors is
reported once, as a count). Files outside the workspace, in dependency/build folders, or with no checker are
skipped; `memory.py config check-edits off` turns it off. Output is always {}. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import List, Optional

import checks
import compat
import conversation as conv
import coord

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKIP_PARTS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "Pods", "DerivedData", ".build",
              "__pycache__", ".gradle", "vendor", "Carthage"}
ROOT_MARKERS = (".git", "pyproject.toml", "package.json", ".python-version", "Dockerfile", ".swiftlint.yml",
                "ruff.toml", "setup.cfg", "Package.swift")
LINT_FIRST_MAX = 15   # more old linter errors than this on the first check → report just the count
SHOW = 6              # problems listed per file


class State:
    """~/.gemini/antigravity-plus/state/<id>.checks.json under an exclusive lock (parallel edits run hooks at once)."""

    def __init__(self, cid: str):
        self.path = os.path.join(conv.STATE_DIR, f"{cid}.checks.json")

    def __enter__(self) -> dict:
        os.makedirs(conv.STATE_DIR, exist_ok=True)
        self.lock = open(self.path + ".lock", "w")
        compat.lock(self.lock)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("files", {})
        self.data.setdefault("pending", [])
        return self.data

    def __exit__(self, *exc) -> None:
        try:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            compat.replace(tmp, self.path)
        finally:
            compat.unlock(self.lock)
            self.lock.close()


def enabled() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("checkEdits", True))
    except Exception:
        return True


def protect_secrets() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("protectSecrets", True))
    except Exception:
        return True


def workspace_of(path: str, payload: dict) -> Optional[str]:
    real = os.path.realpath(path)
    for ws in payload.get("workspacePaths") or []:
        if isinstance(ws, str) and ws:
            w = os.path.realpath(conv.to_path(ws))
            if real.startswith(w.rstrip(os.sep) + os.sep):
                return w
    return None


def project_root(path: str, ws: str) -> str:
    """The nearest folder above the file that looks like a project (its configs drive the linters), else the workspace."""
    d = os.path.dirname(os.path.realpath(path))
    while d.startswith(ws):
        if any(os.path.exists(os.path.join(d, m)) for m in ROOT_MARKERS):
            return d
        if d == ws:
            break
        d = os.path.dirname(d)
    return ws


def sig(p: dict) -> str:
    return f"{p['tool']}|{p.get('code', '')}|{p['msg']}"


def record(cid: str, path: str, problems: List[dict]) -> None:
    """Store the check result and queue what the agent should hear about."""
    with State(cid) as st:
        prev = st["files"].get(path)
        syntax = [p for p in problems if p["kind"] == "syntax"]
        lint = [p for p in problems if p["kind"] != "syntax"]
        entry = {"path": path, "show": [], "lint_count": 0, "fixed": False}
        if prev is None:
            entry["show"] = syntax + (lint if len(lint) <= LINT_FIRST_MAX else [])
            entry["lint_count"] = len(lint) if len(lint) > LINT_FIRST_MAX else 0
        else:
            seen = set(prev.get("sigs") or [])
            entry["show"] = syntax + [p for p in lint if sig(p) not in seen]
            entry["fixed"] = not problems and bool(prev.get("bad"))
        st["files"][path] = {"sigs": [sig(p) for p in problems], "bad": bool(problems), "syntax": syntax[:3],
                             "checked": time.time()}
        if entry["show"] or entry["lint_count"] or entry["fixed"]:
            st["pending"] = [e for e in st["pending"] if e["path"] != path] + [entry]


def take_message(payload: dict) -> Optional[str]:
    """Pending results for the agent (called by the PreInvocation hook); clears the queue."""
    cid = payload.get("conversationId")
    if not isinstance(cid, str) or not os.path.isfile(os.path.join(conv.STATE_DIR, f"{cid}.checks.json")):
        return None
    with State(cid) as st:
        pending, st["pending"] = st["pending"], []
    if not pending:
        return None
    wss = [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]

    def rel(p: str) -> str:
        for w in wss:
            if p.startswith(w + os.sep):
                return os.path.relpath(p, w)
        return p

    lines = ['<edit_check source="Antigravity Plus">', "Automatic check of the files you just changed (errors only):"]
    for e in pending:
        if e["fixed"]:
            lines.append(f"✓ {rel(e['path'])} — fixed, no errors now")
            continue
        parts = []
        for p in e["show"][:SHOW]:
            where = ":".join(str(x) for x in (p.get("line"), p.get("col")) if x)
            parts.append(f"{p['tool']} {where + ' ' if where else ''}{p['msg']}" + (f" [{p['code']}]" if p.get("code") else ""))
        if len(e["show"]) > SHOW:
            parts.append(f"… {len(e['show']) - SHOW} more")
        if e["lint_count"]:
            parts.append(f"{e['lint_count']} linter errors in this file, most likely there before your edit — fix only "
                         "the ones your change introduced")
        lines.append(f"✗ {rel(e['path'])} — " + "; ".join(parts))
    if any(not e["fixed"] for e in pending):
        lines.append("Fix what your edits broke before moving on. If an error was there before your change or is "
                     "expected, leave it and say so in one line.")
    lines.append("</edit_check>")
    return "\n".join(lines)


def unresolved_syntax(payload: dict) -> List[tuple]:
    """Files whose last check found syntax errors and that are still broken now (called by the Stop hook)."""
    cid = payload.get("conversationId")
    if not isinstance(cid, str) or not os.path.isfile(os.path.join(conv.STATE_DIR, f"{cid}.checks.json")):
        return []
    out = []
    with State(cid) as st:
        for path, info in st["files"].items():
            if not info.get("syntax"):
                continue
            ws = workspace_of(path, payload)
            now = checks.check_file(path, project_root(path, ws) if ws else None) if os.path.isfile(path) else []
            syntax = [p for p in (now or []) if p["kind"] == "syntax"]
            info.update({"syntax": syntax[:3], "bad": bool(now), "sigs": [sig(p) for p in now or []]})
            if syntax:
                out.append((path, syntax[0]))
    return out


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        call = payload.get("toolCall") or {}
        args = call.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args)
        path = args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        if isinstance(path, str) and len(path) >= 2 and path[0] == path[-1] == '"':
            path = json.loads(path)
        cid = payload.get("conversationId")
        ws = workspace_of(path, payload) if isinstance(path, str) and path else None
        if ws:
            coord.record(payload, call.get("name") or "", path)  # what this agent saw and changed (hooks/coord.py)
        if call.get("name") == "read_file":
            ws = None  # reads are only recorded, not checked
        if (ws and isinstance(cid, str) and not payload.get("error") and enabled()
                and not SKIP_PARTS.intersection(os.path.relpath(os.path.realpath(path), ws).split(os.sep))):
            path = os.path.realpath(path)
            root = project_root(path, ws)
            problems = checks.check_file(path, root)
            secrets = checks.secret_problems(path, root) if protect_secrets() and os.path.isfile(path) else []
            if problems is not None or secrets:
                record(cid, path, (problems or []) + secrets)
    except Exception as e:  # never disturb the conversation
        print(f"check_edit: {e}", file=sys.stderr)
    sys.stdout.write("{}")


if __name__ == "__main__":
    main()
