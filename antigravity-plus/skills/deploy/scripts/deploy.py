#!/usr/bin/env python3
"""Antigravity Plus — /deploy: what goes out, how risky it is, and one deploy at a time per project.

  deploy.py plan   --workspace DIR [--conversation ID]   changes since the last recorded deploy (or vs git upstream),
                                                        sorted by risk; how this project deploys; last deploy; lock
  deploy.py record --workspace DIR [--note TEXT] [--conversation ID]   remember a successful deploy (file snapshot)
  deploy.py lock   --workspace DIR --conversation ID [--minutes 30]    take the project's deploy lock (exit 1 if busy)
  deploy.py unlock --workspace DIR --conversation ID

The snapshot makes "what changed since the last deploy" work without git too. State lives in
~/.gemini/antigravity-plus/deploys/<workspace-slug>.json. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
import compat  # noqa: E402  (file locks on macOS, Linux and Windows)

STATE_DIR = os.path.expanduser("~/.gemini/antigravity-plus/deploys")
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "Pods", "DerivedData",
        ".build", ".gradle", ".idea", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_FILE = 5_000_000
JUNK = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db)$|\.(pyc|pyo|log|swp|tmp)$")
RISK = [
    ("data", re.compile(r"(^|/)(migrations?|alembic|prisma)/|\.sql$|schema\.(prisma|rb|sql)$|(^|/)(db|models?|database)\.py$", re.I)),
    ("config", re.compile(r"(^|/)\.env|\.env$|(^|/)(config|settings)[\w.-]*\.(py|js|ts|json|ya?ml|toml|ini)$|nginx[\w.-]*\.conf$|"
                          r"\.service$|(^|/)caddyfile$", re.I)),
    ("dependencies", re.compile(r"(^|/)(requirements[\w.-]*\.txt|pyproject\.toml|poetry\.lock|package(-lock)?\.json|"
                                r"yarn\.lock|pnpm-lock\.yaml|podfile(\.lock)?|go\.(mod|sum)|cargo\.(toml|lock)|gemfile(\.lock)?)$", re.I)),
    ("infrastructure", re.compile(r"(^|/)(dockerfile[\w.-]*|docker-compose[\w.-]*\.ya?ml|compose\.ya?ml|fly\.toml|vercel\.json|"
                                  r"netlify\.toml|procfile|app\.yaml)$|(^|/)(deploy|k8s|kubernetes|helm|terraform|ansible)/|"
                                  r"\.tf$|(^|/)\.github/workflows/", re.I)),
]
DEPLOY_HINTS = re.compile(r"(^|/)(deploy[\w.-]*|release[\w.-]*|publish[\w.-]*)(\.sh|\.py)?$|(^|/)deploy/|(^|/)(dockerfile|"
                          r"docker-compose[\w.-]*\.ya?ml|fly\.toml|vercel\.json|netlify\.toml|procfile|app\.yaml)$", re.I)


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class State:
    def __init__(self, ws: str, write: bool = True):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = os.path.join(STATE_DIR, slug(ws) + ".json")
        self.write = write

    def __enter__(self) -> dict:
        self.lockf = open(self.path + ".lock", "w")
        compat.lock(self.lockf)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("history", [])
        return self.data

    def __exit__(self, *exc) -> None:
        try:
            if self.write:
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                compat.replace(tmp, self.path)
        finally:
            compat.unlock(self.lockf)
            self.lockf.close()


def git(args: List[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def snapshot(ws: str) -> Dict[str, str]:
    files = {}
    for p in walk_files(ws):
        try:
            if os.path.getsize(p) > MAX_FILE:
                continue
            with open(p, "rb") as f:
                files[os.path.relpath(p, ws)] = hashlib.sha1(f.read()).hexdigest()
        except OSError:
            continue
    return files


def walk_files(ws: str):
    for dp, dns, fns in os.walk(ws):
        dns[:] = [d for d in dns if d not in SKIP]
        for fn in fns:
            p = os.path.join(dp, fn)
            if not JUNK.search(p) and not os.path.islink(p):
                yield p


def risk_of(path: str) -> str:
    return next((name for name, rx in RISK if rx.search(path)), "code")


def title_of(cid: str) -> str:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
        import coord
        return coord.conversation_info(cid)[0]
    except Exception:
        return ""


def plan(ws: str, cid: Optional[str]) -> str:
    with State(ws, write=False) as st:
        history = list(st["history"])
        lock = st.get("lock")
    out = [f"# Deploy plan — {ws}"]
    last = history[-1] if history else None
    changed: Dict[str, List[str]] = {}
    if last and last.get("files"):
        now = snapshot(ws)
        old = last["files"]
        for p in sorted(set(now) | set(old)):
            if now.get(p) != old.get(p):
                kind = "added" if p not in old else "deleted" if p not in now else "changed"
                changed.setdefault(risk_of(p), []).append(f"{p} ({kind})")
        out.append(f"Since the last recorded deploy ({time.strftime('%Y-%m-%d %H:%M', time.localtime(last['ts']))}"
                   + (f", commit {last['commit'][:10]}" if last.get("commit") else "") + "):")
    elif git(["rev-parse", "--is-inside-work-tree"], ws) == "true":
        upstream = git(["rev-parse", "--abbrev-ref", "@{u}"], ws)
        files = (git(["diff", "--name-only", f"{upstream}...HEAD"], ws) or "").splitlines() if upstream else []
        files += [ln[3:] for ln in (git(["status", "--porcelain"], ws) or "").splitlines()]
        for p in sorted(set(f for f in files if f)):
            changed.setdefault(risk_of(p), []).append(p)
        log = git(["log", "--oneline", f"{upstream}..HEAD"], ws) if upstream else None
        out.append("No deploy recorded yet; compared with git" + (f" upstream {upstream}" if upstream else "") + ":")
        if log:
            out.append("Commits not pushed yet:\n" + "\n".join("  " + ln for ln in log.splitlines()[:15]))
    else:
        out.append("No deploy recorded yet and no git: can't tell what's new. Files changed in the last 7 days:")
        cutoff = time.time() - 7 * 86400
        for p in walk_files(ws):
            try:
                if os.path.getmtime(p) > cutoff:
                    rel = os.path.relpath(p, ws)
                    changed.setdefault(risk_of(rel), []).append(rel)
            except OSError:
                pass
    if not changed:
        out.append("- nothing changed")
    labels = {"data": "⚠️ data / database schema — back up data before deploying, check migrations",
              "config": "⚠️ configuration / environment — check the server's env and configs match",
              "dependencies": "⚠️ dependencies — the build may change; rebuild images",
              "infrastructure": "⚠️ infrastructure (Docker, compose, CI, deploy scripts)",
              "code": "code"}
    for kind in ("data", "config", "dependencies", "infrastructure", "code"):
        if changed.get(kind):
            items = changed[kind]
            out.append(f"\n## {labels[kind]} ({len(items)})")
            out += [f"- {x}" for x in items[:30]] + ([f"- … +{len(items) - 30} more"] if len(items) > 30 else [])

    hints = []
    for dp, dns, fns in os.walk(ws):
        dns[:] = [d for d in dns if d not in SKIP]
        if dp[len(ws):].count(os.sep) > 3:
            dns[:] = []
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), ws)
            if DEPLOY_HINTS.search(rel):
                hints.append(rel)
    agents = os.path.join(ws, "AGENTS.md")
    notes = []
    try:
        with open(agents, encoding="utf-8", errors="replace") as f:
            notes = [ln.strip() for ln in f if re.search(r"deploy|деплой|выкат|релиз|сервер|server|prod", ln, re.I)][:8]
    except OSError:
        pass
    out.append("\n## How this project deploys")
    out += [f"- file: {h}" for h in sorted(hints)[:15]] or ["- no deploy files found"]
    out += [f"- AGENTS.md: {n}" for n in notes] or ["- AGENTS.md says nothing about deploying"]
    out.append("- also search the project memory: memory.py search \"deploy|деплой|сервер\"")

    out.append("\n## Last deploys")
    out += [f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(h['ts']))}" + (f" · commit {h['commit'][:10]}" if h.get('commit') else "")
            + (f" · {h['note']}" if h.get("note") else "") for h in history[-3:]] or ["- none recorded"]
    if lock and lock.get("until", 0) > time.time() and lock.get("conversation") != cid:
        out.append(f"\n⛔ Another agent is deploying this project now: «{lock.get('title') or 'без названия'}» "
                   f"(conversation {lock.get('conversation')}, until {time.strftime('%H:%M', time.localtime(lock['until']))}). "
                   "Wait, or coordinate through send_message.")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "record", "lock", "unlock"):
        sp = sub.add_parser(name)
        sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
        sp.add_argument("--conversation", required=name in ("lock", "unlock"))
        if name == "record":
            sp.add_argument("--note", default="")
        if name == "lock":
            sp.add_argument("--minutes", type=int, default=30)
    a = ap.parse_args(argv)
    ws = os.path.realpath(os.path.expanduser(a.workspace))
    if a.cmd == "plan":
        print(plan(ws, a.conversation))
        return 0
    with State(ws) as st:
        lock = st.get("lock") or {}
        now = time.time()
        if a.cmd == "lock":
            if lock.get("until", 0) > now and lock.get("conversation") != a.conversation:
                print(f"busy: «{lock.get('title') or 'без названия'}» (conversation {lock.get('conversation')}) is deploying "
                      f"until {time.strftime('%H:%M', time.localtime(lock['until']))}")
                return 1
            st["lock"] = {"conversation": a.conversation, "title": title_of(a.conversation), "until": now + a.minutes * 60}
            print(f"locked until {time.strftime('%H:%M', time.localtime(st['lock']['until']))}")
        elif a.cmd == "unlock":
            if lock.get("conversation") == a.conversation:
                st.pop("lock", None)
            print("unlocked")
        else:
            entry = {"ts": now, "note": a.note, "conversation": a.conversation or "", "commit": git(["rev-parse", "HEAD"], ws) or "",
                     "files": snapshot(ws)}
            for h in st["history"]:
                h.pop("files", None)  # only the latest snapshot is needed
            st["history"] = (st["history"] + [entry])[-5:]
            if lock.get("conversation") == a.conversation:
                st.pop("lock", None)
            print(f"recorded the deploy ({len(entry['files'])} files" + (f", commit {entry['commit'][:10]}" if entry["commit"] else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
