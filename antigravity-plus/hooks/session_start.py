#!/usr/bin/env python3
"""Antigravity Plus — SessionStart hook.

Injects a compact snapshot at the start of every conversation, like Claude Code's gitStatus block:
  * git: branch, default branch, ahead/behind, short status, recent commits;
  * project rules: which of AGENTS.md / GEMINI.md exist;
  * project setup the main agent does on its own (autoSetup): create a git repository with /git-init before the
    first change, and run /init when real work starts in a project without rules — with the exact commands;
  * memory: personal and project auto-memory (skills/memory/scripts/memory.py — shared with Claude Code);
  * Claude Code bridge (Antigravity doesn't load these natively): CLAUDE.md / CLAUDE.local.md with their
    @path imports, .claude/rules (always-on inlined, path-scoped listed), ~/.claude/CLAUDE.md, and pointers to
    .claude/commands, .claude/skills and .claude/agents.

Contract: JSON on stdin (conversationId, workspacePaths, …) → JSON on stdout
({"injectSteps": [{"systemMessage": {"systemMessage": "…"}}]} or {}). Never fails loudly.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import compat
import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MAX_WORKSPACES = 3
MAX_STATUS_LINES = 25
MAX_COMMITS = 5
MAX_LISTED = 20
CLAUDE_MD_LIMIT = 8000   # chars of CLAUDE.md inlined per file
IMPORT_LIMIT = 8000      # chars of files CLAUDE.md pulls in with @path, per CLAUDE.md
RULES_LIMIT = 5000       # chars of always-on .claude/rules inlined
USER_CLAUDE_LIMIT = 6000 # chars of ~/.claude/CLAUDE.md
TOTAL_LIMIT = 48000      # hard cap for the whole injected message; the Claude Code part is cut first
IMPORT = re.compile(r"(?<![\w`@/.])@((?:~/|\.{1,2}/|/)?[\w.-][\w./-]*)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def git(args: List[str], cwd: str, timeout: float = 4.0) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def to_path(p: str) -> str:
    if p.startswith("file://"):
        return unquote(urlparse(p).path)
    return os.path.expanduser(p)


def read_text(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit + 1)
    except OSError:
        return ""


def frontmatter_field(text: str, key: str) -> str:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    fm = m.group(1)
    hit = re.search(rf"^{key}:[ \t]*(>-?|\|-?)?[ \t]*(.*)$", fm, re.M)
    if not hit:
        return ""
    if hit.group(1):  # folded / literal block: take the indented lines that follow
        rest = fm[hit.end():].splitlines()
        lines = []
        for ln in rest:
            if ln.startswith((" ", "\t")):
                lines.append(ln.strip())
            elif ln.strip():
                break
        return " ".join(lines)
    return hit.group(2).strip().strip("'\"")


def one_line(text: str, n: int = 110) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def describe(path: str) -> str:
    text = read_text(path, 4000)
    desc = frontmatter_field(text, "description")
    if not desc:
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
        for ln in body.splitlines():
            ln = ln.strip().lstrip("#").strip()
            if ln:
                desc = ln
                break
    return one_line(desc) if desc else ""


def git_block(ws: str) -> List[str]:
    top = git(["rev-parse", "--show-toplevel"], ws)
    if top is None:
        return ["Git: not a git repository."]
    out = []
    branch = git(["branch", "--show-current"], top) or ""
    if not branch:
        head = git(["rev-parse", "--short", "HEAD"], top)
        branch = f"(detached at {head})" if head else "(no commits yet)"
    default = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], top) or ""
    if default.startswith("origin/"):
        default = default[len("origin/"):]
    if not default:
        for cand in ("main", "master", "develop"):
            if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{cand}"], top) is not None:
                default = cand
                break
    line = f"Git: branch `{branch}`"
    if default:
        line += f" · default branch `{default}` (usually the PR base)"
    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], top)
    if upstream:
        counts = git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], top)
        if counts and len(counts.split()) == 2:
            behind, ahead = counts.split()
            line += f" · vs `{upstream}`: ahead {ahead}, behind {behind}"
    elif not branch.startswith("("):
        line += " · not pushed (no upstream)"
    out.append(line)

    status = git(["status", "--porcelain=v1", "--untracked-files=normal"], top, timeout=6.0)
    if status is None:
        out.append("Status: (git status failed or timed out)")
    elif not status.strip():
        out.append("Status: clean")
    else:
        lines = status.splitlines()
        out.append(f"Status ({len(lines)} changed):")
        out.extend("  " + ln for ln in lines[:MAX_STATUS_LINES])
        if len(lines) > MAX_STATUS_LINES:
            out.append(f"  … +{len(lines) - MAX_STATUS_LINES} more (run `git status`)")

    log = git(["log", "-n", str(MAX_COMMITS), "--pretty=format:%h %s (%cr)"], top)
    if log:
        out.append("Recent commits:")
        out.extend("  " + ln for ln in log.splitlines())
    if top != ws:
        out.append(f"Repo root: {top}")
    return out


def same_file(a: str, b: str) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
        return read_text(a, 200000).strip() == read_text(b, 200000).strip()
    except OSError:
        return False


def project_files(ws: str, top: Optional[str]) -> Tuple[List[str], List[str]]:
    """(summary lines, Claude Code bridge lines) for the workspace's instruction files."""
    roots = [ws] + ([top] if top and top != ws else [])
    found: Dict[str, str] = {}
    for r in roots:
        for name in ("AGENTS.md", "GEMINI.md", "CLAUDE.md", "CLAUDE.local.md"):
            p = os.path.join(r, name)
            if name not in found and os.path.isfile(p):
                found[name] = p

    summary: List[str] = []
    native = [n for n in ("AGENTS.md", "GEMINI.md") if n in found]
    if native:
        summary.append("Project rules: " + ", ".join(f"`{n}`" for n in native) + " (loaded by Antigravity).")
    elif "CLAUDE.md" in found or "CLAUDE.local.md" in found:
        summary.append("Project rules: Claude Code's CLAUDE.md (below); no AGENTS.md.")
    else:
        summary.append("Project rules: none yet (no AGENTS.md).")

    bridge: List[str] = []
    skip = [found[n] for n in native]
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        p = found.get(name)
        if not p:
            continue
        if any(same_file(p, s) for s in skip):
            summary.append(f"`{name}` is the same as {native[0]} — already loaded.")
            continue
        text = read_text(p, CLAUDE_MD_LIMIT)
        if not text.strip():
            continue
        kind = "personal, not committed" if name == "CLAUDE.local.md" else "team project rules"
        bridge.append(f"\n### {name} ({kind}, written for Claude Code — follow it as project rules)")
        bridge.append(f"Path: {p}")
        bridge.append(text[:CLAUDE_MD_LIMIT].rstrip())
        if len(text) > CLAUDE_MD_LIMIT:
            bridge.append(f"… [truncated — read the rest of {p}]")
        bridge.extend(claude_imports(text, os.path.dirname(p), skip, [IMPORT_LIMIT], {p}))

    claude_dir = next((os.path.join(r, ".claude") for r in roots if os.path.isdir(os.path.join(r, ".claude"))), None)
    if claude_dir:
        bridge.extend(claude_rules(claude_dir))
        bridge.extend(claude_assets(claude_dir))
    return summary, bridge


def claude_imports(text: str, base: str, skip: List[str], budget: List[int], seen: set, depth: int = 0) -> List[str]:
    """Files a CLAUDE.md pulls in with Claude Code's `@path` import syntax (not inside code), up to 3 levels deep."""
    out: List[str] = []
    for ref in IMPORT.findall(CODE.sub("", text)):
        p = os.path.normpath(os.path.expanduser(ref) if ref.startswith("~/") else os.path.join(base, ref))
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        if any(same_file(p, s) for s in skip):
            out.append(f"(imports `@{ref}` — already loaded by Antigravity)")
            continue
        if budget[0] <= 0:
            out.append(f"(imports `@{ref}` → {p} — not inlined; read it when relevant)")
            continue
        body = read_text(p, budget[0])
        out.append(f"\n#### @{ref} (imported from Claude Code memory → {p})")
        out.append(body[:budget[0]].rstrip())
        if len(body) > budget[0]:
            out.append(f"… [truncated — read the rest of {p}]")
        budget[0] -= len(body)
        if depth < 2:
            out.extend(claude_imports(body, os.path.dirname(p), skip, budget, seen, depth + 1))
    return out


def fm_list(text: str, key: str) -> List[str]:
    """A list value from YAML frontmatter: `key: [a, b]`, `key: a` or `key:` followed by `- a` lines."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    hit = re.search(rf"^{key}:[ \t]*(.*)$", m.group(1), re.M) if m else None
    if not hit:
        return []
    val = hit.group(1).strip()
    if val:
        return [v.strip().strip("'\"") for v in val.strip("[]").split(",") if v.strip()]
    items = []
    for ln in m.group(1)[hit.end():].splitlines()[1:]:
        mm = re.match(r"^\s+-\s*(.+)$", ln)
        if not mm:
            break
        items.append(mm.group(1).strip().strip("'\""))
    return items


def claude_rules(d: str) -> List[str]:
    """Claude Code's .claude/rules: rules without `paths:` are always on (inlined), the rest are path-scoped."""
    rd = os.path.join(d, "rules")
    always, scoped = [], []
    for dp, dns, fns in os.walk(rd):
        dns.sort()
        for fn in sorted(fns):
            if fn.endswith(".md"):
                p = os.path.join(dp, fn)
                text = read_text(p, 12000)
                paths = fm_list(text, "paths")
                (scoped if paths else always).append((p, text, paths))
    out: List[str] = []
    budget = RULES_LIMIT
    if always:
        out.append("\nClaude Code rules (.claude/rules, always on — follow them as project rules):")
        for p, text, _ in always:
            body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S).strip()
            if budget > 0 and body:
                out.append(f"#### {os.path.relpath(p, rd)}")
                out.append(body[:budget])
                budget -= len(body)
            else:
                out.append(f"  - {p} — read it")
    if scoped:
        out.append("\nClaude Code path-scoped rules (.claude/rules) — before working on files matching the globs, "
                   "read the rule file and follow it:")
        for p, _text, paths in scoped[:MAX_LISTED]:
            out.append(f"  - {p} → {', '.join(paths)}")
    return out


def claude_assets(d: str) -> List[str]:
    out: List[str] = []
    cmds_dir = os.path.join(d, "commands")
    cmds = []
    if os.path.isdir(cmds_dir):
        for dp, _dns, fns in os.walk(cmds_dir):
            for fn in sorted(fns):
                if fn.endswith(".md"):
                    rel = os.path.relpath(os.path.join(dp, fn), cmds_dir)[:-3].replace(os.sep, ":")
                    cmds.append((rel, os.path.join(dp, fn)))
    if cmds:
        out.append("\nClaude Code slash commands (.claude/commands) — when the user types `/<name>` or asks for one "
                   "and no Antigravity skill matches, read the file and follow it ($ARGUMENTS = the user's arguments):")
        for name, p in cmds[:MAX_LISTED]:
            desc = describe(p)
            out.append(f"  - /{name} → {p}" + (f" — {desc}" if desc else ""))
        if len(cmds) > MAX_LISTED:
            out.append(f"  … +{len(cmds) - MAX_LISTED} more in {cmds_dir}")

    skills_dir = os.path.join(d, "skills")
    skills = []
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            p = os.path.join(skills_dir, name, "SKILL.md")
            if os.path.isfile(p):
                skills.append((name, p))
    if skills:
        out.append("\nClaude Code skills (.claude/skills, same SKILL.md format) — read the file when its description fits the task:")
        for name, p in skills[:MAX_LISTED]:
            desc = describe(p)
            out.append(f"  - {name} → {p}" + (f" — {desc}" if desc else ""))

    agents_dir = os.path.join(d, "agents")
    agents = []
    if os.path.isdir(agents_dir):
        for fn in sorted(os.listdir(agents_dir)):
            if fn.endswith(".md"):
                agents.append((fn[:-3], os.path.join(agents_dir, fn)))
    if agents:
        out.append("\nClaude Code subagents (.claude/agents) — to use one, invoke a subagent and give it that file's body as its instructions:")
        for name, p in agents[:MAX_LISTED]:
            desc = describe(p)
            out.append(f"  - {name} → {p}" + (f" — {desc}" if desc else ""))
    return out


def user_claude_md() -> List[str]:
    p = os.path.expanduser("~/.claude/CLAUDE.md")
    text = read_text(p, USER_CLAUDE_LIMIT)
    if not text.strip():
        return []
    out = ["\n## ~/.claude/CLAUDE.md (the user's personal Claude Code instructions — follow them in every project)",
           text[:USER_CLAUDE_LIMIT].rstrip()]
    if len(text) > USER_CLAUDE_LIMIT:
        out.append(f"… [truncated — read the rest of {p}]")
    out.extend(claude_imports(text, os.path.dirname(p), [], [IMPORT_LIMIT], {p}))
    return out


def setup_hints(ws: str, top: Optional[str]) -> List[str]:
    """For the main agent: project setup it does on its own (autoSetup) — a git repository and AGENTS.md."""
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        if not memstore.settings().get("autoSetup", True):
            return []
    except Exception:
        pass
    out = []
    script = os.path.join(PLUGIN, "skills", "git-init", "scripts", "repo_init.py")
    try:
        sys.path.insert(0, os.path.dirname(script))
        import repo_init
        if repo_init.place_refusal(os.path.realpath(ws)):
            return []  # the home folder, ~/Downloads and the like: not one project
    except Exception as e:
        print(f"session_start: setup: {e}", file=sys.stderr)
        return []
    if not top:
        if not repo_init.refusal(ws):
            out.append("No git repository yet. Before your first change here, run this command yourself (a "
                       f"run_command call; no need to ask): `{compat.python_cmd()} \"{script}\" --path \"{ws}\"`. It adds a safe "
                       ".gitignore and commits the current state, without secrets. Then go on with the task in the same "
                       "turn, and mention the repository in your final report.")
    roots = [ws] + ([top] if top and top != ws else [])
    if not any(os.path.isfile(os.path.join(r, n)) for r in roots for n in ("AGENTS.md", "GEMINI.md", "CLAUDE.md")):
        out.append("No project rules yet. When you start real work here (you'll change files, not just answer), run "
                   f"/init yourself: read_file \"{os.path.join(PLUGIN, 'skills', 'init', 'SKILL.md')}\" and follow "
                   "«When you run it on your own». In a new or empty project, do it after the first working version. "
                   "Don't stop to announce it: do it and keep working.")
    return out


def memory_section(paths: List[str], tops: Dict[str, str], query: str = "") -> List[str]:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return memstore.session_block(paths, tops, query)
    except Exception as e:  # memory is optional; never lose the rest of the context
        print(f"session_start: memory: {e}", file=sys.stderr)
        return []


def recent_section(ws: str, top: Optional[str], cid: Optional[str]) -> List[str]:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "handoff", "scripts"))
        import handoff
        return handoff.recent_block(ws, top, cid)
    except Exception as e:  # optional, like memory
        print(f"session_start: recent: {e}", file=sys.stderr)
        return []


def is_subagent(payload: dict) -> bool:
    """A subagent's transcript has no user message: its first step is this hook's own message, while a
    conversation the user started already holds the user's request as step 0 when the hook runs."""
    if isinstance(payload.get("lastUserInput"), str) and payload["lastUserInput"].strip():
        return False
    tp = conv.transcript_path(payload)
    if not tp:
        return bool(payload.get("conversationId"))
    try:
        with open(tp, "rb") as f:
            return b'"USER_INPUT"' not in f.read(65536)
    except OSError:
        return False


def build(payload: dict) -> str:
    paths = []
    for p in payload.get("workspacePaths") or []:
        if isinstance(p, str) and p:
            path = to_path(p)
            if os.path.isdir(path) and path not in paths:
                paths.append(path)
    if not paths:
        return ""
    paths = paths[:MAX_WORKSPACES]
    sub = is_subagent(payload)
    head = ["<antigravity_plus_session_context>",
            "Snapshot taken when this conversation started (Antigravity Plus). It does not update — "
            "re-run git commands when you need the current state."]
    if compat.python_cmd() != "python3":  # Windows: the plugin's instructions say python3
        head.append(f"Python on this computer: `{compat.python_cmd()}` — use it wherever the plugin's instructions "
                    "and skills say `python3`.")
    bridge: List[str] = []
    tops: Dict[str, str] = {}
    for ws in paths:
        top = git(["rev-parse", "--show-toplevel"], ws)
        if top:
            tops[ws] = top
        head.append(f"\n## Workspace: {ws}")
        head.extend(git_block(ws))
        summary, claude = project_files(ws, top)
        head.extend(summary)
        if not sub:
            head.extend(setup_hints(ws, top))
        since = conv._project_grant(os.path.realpath(ws), None)
        if since and not sub:
            head.append(f"Full access: ON for this project since {since} — the user allowed you to run every plugin "
                        "command yourself without asking (rules: \"Full access\"). Revoked by «верни вопросы».")
        if not sub:
            head.extend(recent_section(ws, top, payload.get("conversationId")))
        if claude:
            bridge.append(f"\n## Claude Code files in {top or ws}")
            bridge.extend(claude)
    bridge.extend(user_claude_md())
    st = conv.load(payload) if not sub else None
    query = (st["users"][0]["text"] if st and st["users"] else "") or str(payload.get("lastUserInput") or "")
    memory = [] if sub else memory_section(paths, tops, query)  # subagents don't talk to the user or write memory

    core, mem, extra = "\n".join(head), "\n".join(memory), "\n".join(bridge)
    room = TOTAL_LIMIT - len(core) - len(mem) - 200
    if len(extra) > room:
        extra = extra[:max(room, 0)] + "\n… [Claude Code files truncated — read them directly]"
    msg = "\n".join(x for x in (core, extra, mem) if x) + "\n</antigravity_plus_session_context>"
    if len(msg) > TOTAL_LIMIT + 400:
        msg = msg[:TOTAL_LIMIT] + "\n… [session context truncated]\n</antigravity_plus_session_context>"
    conv.prune_states()
    return msg


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        msg = build(payload if isinstance(payload, dict) else {})
    except Exception as e:  # a hook must never break the session
        msg = ""
        print(f"session_start: {e}", file=sys.stderr)
    out = {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]} if msg else {}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
