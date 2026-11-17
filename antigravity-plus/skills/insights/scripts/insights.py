#!/usr/bin/env python3
r"""Antigravity Plus — /insights: what the plugin's hooks did and how the agents reacted, to decide what to tighten
or loosen.

  insights.py [--days 7] [--workspace DIR] [--json]

Reads, never writes:
  * transcripts ~/.gemini/<product>/brain/<id>/.system_generated/logs/transcript_full.jsonl:
      - EPHEMERAL_MESSAGE steps: the PreInvocation hints (`<edit_check source="Antigravity Plus">`, <user_instructions>,
        <other_agents>, <background_work>, <parallel_plan>, <parallel_hint>, <sandbox>, <error_loop>,
        <context_budget>, <code_navigation>, <memory_recall>, <memory_hint>, <grounding>, <objectivity>) and the
        untagged Bypass Mode notices;
      - SYSTEM_MESSAGE «Stop hook blocked termination: Antigravity Plus …»: the Stop hook's blocks, by category from
        their text (hooks/stop.py, facts.py, objective.py, memory_hints.py, parallel.py, gitcheck.py), and what the
        agent did next — tool calls before ending again, or just ending again, and whether the same block came back;
      - tool steps «tool call denied by pre-tool hook: …» (the guard's denials) and «permission check failed …»
        (a prompt that timed out or was refused); a PLANNER_RESPONSE's tool_calls map in order onto the GENERIC
        steps after it;
  * ~/.gemini/antigravity-plus/guard.log — every guard match, one JSON per line {ts, conversation, tool, rule,
    subject} (hooks/guard.py log()); asks are matched to their tool step to see whether the command then ran;
  * ~/.gemini/antigravity-plus/state/<id>.checks.json — files whose last edit check still found errors
    (hooks/check_edit.py: sigs «tool|code|msg»).
Commands are shortened, the home folder shows as ~, and anything token-like is masked. User messages are never
shown: only ≤120-char excerpts of the hooks' own messages, with «quoted» parts dropped. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
AGP = os.path.join(HOME, ".gemini", "antigravity-plus")
GUARD_LOG = os.path.join(AGP, "guard.log")
STATE_DIR = os.path.join(AGP, "state")
DELEGATE_DIR = os.path.join(AGP, "delegate")
MODEL_CACHE = os.path.join(AGP, "conversation-models.json")
PRICING = os.path.join(HERE, "..", "..", "usage", "pricing.json")
PRODUCT_ROOTS = ["~/.gemini/antigravity", "~/.gemini/antigravity-cli", "~/.gemini/antigravity-ide"]

EXCERPT = 120   # chars of a hook message shown
COMMAND = 90    # chars of a command shown
TOP = 10        # rows of top commands
CANDIDATES = 15

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MODEL_CHANGE_RE = re.compile(r"Model Selection`\s+from\s+(.*?)\s+to\s+(.*?)\.(?:\s|$)")  # = usage.py
WORKSPACE_RE = re.compile(r"^## Workspace: (.+?)\s*$", re.M)                         # = session_start.py
SUBAGENT_RE = re.compile(r"Created the following subagent:\s*(\{.*\})", re.S)
HINT_TAG = re.compile(r'<([a-z_]+) source="Antigravity Plus">')
HINT_KINDS = {  # tag → the hook that injects it
    "user_instructions": "instructions.py: the user's messages re-shown",
    "edit_check": "check_edit.py: errors in files just written",
    "other_agents": "coord.py: other agents in the project",
    "background_work": "parallel.py: subagents / commands running",
    "parallel_plan": "parallel.py: split the task",
    "parallel_hint": "parallel.py: hand research off",
    "sandbox": "speed.py: sandbox memory",
    "error_loop": "speed.py: failures in a row",
    "context_budget": "speed.py: context size",
    "code_navigation": "lsp.py: ask the language server",
    "memory_recall": "memory_hints.py: memories for this message",
    "memory_hint": "memory_hints.py: worth remembering",
    "grounding": "facts.py: look facts up",
    "objectivity": "objective.py: straight answer",
    "quota": "quota.py: plan limits getting short",
    "bypass_notice": "instructions.py: Bypass Mode switched",
}
BYPASS_NOTE = re.compile(r"^\s*(The user turned Bypass Mode (ON|OFF)|Bypass Mode was turned (ON|OFF) in another conversation)")
STOP_RE = re.compile(r"(?:^|<SYSTEM_MESSAGE>)\s*Stop hook blocked termination:\s*(.*?)\s*(?:</SYSTEM_MESSAGE>\s*)?$", re.S)
STOP_KINDS: List[Tuple[str, "re.Pattern[str]"]] = [(k, re.compile(p, re.S)) for k, p in [
    ("facts", r"Antigravity Plus — facts:"),
    ("objectivity", r"Antigravity Plus — objectivity:"),
    ("memory", r"Antigravity Plus — memory:"),
    ("memory-claim", r"your reply says you saved a memory"),
    ("announcement", r"and ended your turn without doing it"),
    ("background", r"you ended your turn while .{1,300}? still working in the background"),
    ("syntax", r"files you edited still have syntax errors"),
    ("task.md", r"task\.md still has open items in the current checklist"),
    ("review", r"code files in this turn \(.{0,600}?\) and haven't reviewed them"),
    ("verify", r"you changed code \(.{0,600}?\) but ran no check"),
    ("ui-check", r"you changed the web UI \("),
    ("mobile", r"you changed the app's screens \("),
    ("setup", r"Set up the project now"),
    ("ci", r"you pushed changes to the remote, and this repository has CI workflows"),
    ("git", r"is still in progress in |are still conflicted: |Conflict markers are still in "),
]]
DENIED = "tool call denied by pre-tool hook"
PROMPT_FAILED = "permission check failed"
TIMED_OUT = re.compile(r"timed out waiting for user response", re.I)
REFUSED = re.compile(r"\b(denied|rejected|declined|refused)\b", re.I)
DENY_RULES = {"secret-read", "secret-print", "fake-check", "memory-secret", "memory-elsewhere", "stale-write",
              "git-interactive-patch", "git-interactive", "git-continue-editor", "git-rebase-interactive",
              "gh-auth-login", "gh-web", "gh-prompt"}  # hooks/guard.py decide() + hooks/gitcheck.py: refused, not asked
CHECK_LINE = re.compile(r"^✗ (.+?) — (.*)$")
FIXED_LINE = re.compile(r"^✓ .+ — fixed")
OLD_LINT = re.compile(r"^\d+ linter errors in this file")

# masking: what could be a secret in a command or a hook message
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}|\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{12,}"
               r"|\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
               r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{20,}"
               r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+|\bglpat-[A-Za-z0-9_-]{16,}"),
]
SECRET_ASSIGN = re.compile(r"(?i)(\b[\w.-]*(?:pass(?:word)?|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
                           r"|private[_-]?key|credentials?|auth)[\w.-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'&;|,)]+)")
SECRET_FLAG = re.compile(r"(?i)((?:--(?:password|token|api-key|secret)(?:=|\s+)|\b(?:Bearer|Basic|token)\s+))"
                         r"(\"[^\"]{3,}\"|'[^']{3,}'|[^\s\"']{6,})")
URL_CREDS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{28,}")


# ─────────────────────────────── text helpers ───────────────────────────────

def mask(text: str) -> str:
    """Anything token-like → ***; the home folder → ~."""
    t = text or ""
    for p in SECRET_PATTERNS:
        t = p.sub("***", t)
    t = URL_CREDS.sub(r"\1***@", t)
    t = SECRET_FLAG.sub(lambda m: m.group(1) + "***", t)
    t = SECRET_ASSIGN.sub(lambda m: m.group(1) + "***", t)
    if HOME and len(HOME) > 1:
        t = t.replace(HOME, "~")
        if os.sep == "\\":
            t = t.replace(HOME.replace("\\", "/"), "~")

    def long_one(m: "re.Match[str]") -> str:
        s = m.group(0)
        if UUID_RE.fullmatch(s) or sum(c.isdigit() for c in s) < 4 or sum(c.isalpha() for c in s) < 4:
            return s
        return "***"
    t = LONG_TOKEN.sub(long_one, t)
    return t


def clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def excerpt(text: str, n: int = EXCERPT) -> str:
    """A hook message, safe to show: «quoted» parts (they may hold the user's words) dropped, secrets masked."""
    t = re.sub(r"«[^»]{0,3000}»", "«…»", text or "")
    t = re.sub(r"^Antigravity Plus(?: —|:)\s*", "", t.strip())
    return clip(mask(t), n)


def command(text: str) -> str:
    return clip(mask(text), COMMAND)


def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    """Transcript times are UTC («…Z»); guard.log times are local and naive."""
    if not s:
        return None
    try:
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc) if t.tzinfo else t.astimezone().astimezone(dt.timezone.utc)


def unwrap(v) -> str:
    """Tool args may arrive JSON-encoded twice (as in hooks/guard.py)."""
    if isinstance(v, str):
        s = v.strip()
        if len(s) >= 2 and s[0] == s[-1] == '"':
            try:
                d = json.loads(s)
                if isinstance(d, str):
                    return d
            except ValueError:
                pass
        return v
    return "" if v is None else str(v)


def call_subject(call: dict) -> str:
    args = call.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        return ""
    for k in ("CommandLine", "command", "Path", "TargetFile", "path"):
        if args.get(k):
            return unwrap(args[k])
    return ""


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ─────────────────────────────── transcripts ───────────────────────────────

def transcripts() -> List[Tuple[str, str]]:
    out, seen = [], set()
    for root in PRODUCT_ROOTS:
        for p in glob.glob(os.path.join(os.path.expanduser(root), "brain", "*", ".system_generated", "logs",
                                        "transcript_full.jsonl")):
            cid = os.path.normpath(p).split(os.sep)[-4]
            if cid not in seen:
                seen.add(cid)
                out.append((cid, p))
    return out


def load_steps(path: str) -> List[dict]:
    steps: Dict[int, dict] = {}
    try:
        with open(path, "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("step_index"), int):
            steps[d["step_index"]] = d
    return [steps[k] for k in sorted(steps)]


def stop_kinds(text: str) -> List[str]:
    found = sorted((m.start(), k) for k, p in STOP_KINDS for m in [p.search(text)] if m)
    return [k for _pos, k in found] or ["other"]


class Conv:
    def __init__(self, cid: str, path: str):
        self.cid, self.path = cid, path
        self.main = False                  # has user input (subagents have none)
        self.workspaces: List[str] = []
        self.default_model = ""
        self.children: List[Tuple[str, str, int, List[str]]] = []  # (child id, Model arg, step, workspaces)
        self.events: List[tuple] = []      # (step, kind, data) in step order, for reactions
        self.hints: List[Tuple[int, str, Optional[dt.datetime], str]] = []   # (step, kind, ts, model-at-step)
        self.checks: List[Tuple[int, str]] = []    # edit_check content
        self.blocks: List[dict] = []
        self.calls: List[dict] = []        # tool calls with their result step
        self.step_model: Dict[int, str] = {}
        self.last_model = ""

    def model_at(self, step: int) -> str:
        if step in self.step_model:
            return self.step_model[step]
        return (self.last_model if step >= 10 ** 9 else "") or self.default_model or "unknown"


def _uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    p = unquote(urlparse(uri).path or "")
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    return p


def scan(cid: str, path: str) -> Conv:
    c = Conv(cid, path)
    model = ""
    queue: List[dict] = []
    for st in load_steps(path):
        i, typ = st["step_index"], st.get("type") or ""
        content = st.get("content") or ""
        ts = parse_ts(st.get("created_at"))
        if typ == "USER_INPUT":
            c.main = True
            for _old, new in MODEL_CHANGE_RE.findall(content):
                model = new.strip()
            c.events.append((i, "user", None))
            queue = []
        if model:
            c.step_model[i] = model
            c.last_model = model
        if typ == "SYSTEM_MESSAGE":
            if not c.workspaces and "antigravity_plus_session_context" in content:
                c.workspaces = [w.strip() for w in WORKSPACE_RE.findall(content)]
            m = STOP_RE.search(content)
            if m:
                text = m.group(1)
                block = {"step": i, "ts": ts, "text": text, "kinds": stop_kinds(text),
                         "items": [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]}
                c.blocks.append(block)
                c.events.append((i, "stop", block))
        elif typ == "EPHEMERAL_MESSAGE":
            kinds = list(dict.fromkeys(HINT_TAG.findall(content)))
            if BYPASS_NOTE.search(content):
                kinds.append("bypass_notice")
            for k in kinds:
                c.hints.append((i, k, ts, ""))
            if "edit_check" in kinds:
                c.checks.append((i, content))
        elif typ == "PLANNER_RESPONSE":
            calls = [t for t in (st.get("tool_calls") or []) if isinstance(t, dict)]
            queue = list(calls)
            c.events.append((i, "tools" if calls else "end", len(calls)))
        elif typ == "GENERIC":
            call = queue.pop(0) if queue else {}
            err = str(st.get("error") or "")
            status = st.get("status") or ""
            if DENIED in err:
                outcome = "denied"
            elif PROMPT_FAILED in err:
                outcome = "timeout" if TIMED_OUT.search(err) else "rejected" if REFUSED.search(err) else "prompt-error"
            elif status in ("DONE", "RUNNING"):
                outcome = "ran"
            else:
                outcome = "error"
            name = str(call.get("name") or "")
            c.calls.append({"step": i, "ts": ts, "tool": name, "subject": call_subject(call), "outcome": outcome,
                            "error": err})
            m = SUBAGENT_RE.search(content) if name == "invoke_subagent" or "Created the following subagent" in content else None
            if m:
                try:
                    info = json.loads(m.group(1))
                except ValueError:
                    info = {}
                child = str(info.get("conversationId") or "")
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                uris = [u for u in info.get("workspaceUris") or [] if isinstance(u, str)]
                wss = [_uri_to_path(u) for u in uris]
                if child:
                    c.children.append((child, str(args.get("Model") or ""), i, wss))
    c.hints = [(i, k, ts, c.step_model.get(i, "")) for i, k, ts, _m in c.hints]
    return c


def aliases() -> Dict[str, str]:
    d = read_json(PRICING)
    a = d.get("subagent_model_aliases") if isinstance(d, dict) else None
    return {k: str(v) for k, v in a.items()} if isinstance(a, dict) else {}


def own_model(cid: str) -> str:
    """A conversation whose transcript names no model: the bridge's task record, else /usage's model cache."""
    rec = read_json(os.path.join(DELEGATE_DIR, cid + ".json")) if UUID_RE.fullmatch(cid) else None
    if isinstance(rec, dict) and rec.get("id") == cid and (rec.get("label") or rec.get("model")):
        return str(rec.get("label") or rec.get("model"))
    cache = read_json(MODEL_CACHE)
    return str(cache.get(cid) or "") if isinstance(cache, dict) else ""


def resolve(convs: Dict[str, Conv]) -> None:
    """Default model and workspace per conversation; subagents take them from where they were created."""
    alias = aliases()
    for c in convs.values():
        c.default_model = own_model(c.cid)
    for _ in range(3):  # subagents of subagents
        for parent in convs.values():
            for child, arg, step, wss in parent.children:
                k = convs.get(child)
                if not k:
                    continue
                if not k.workspaces:
                    k.workspaces = wss or parent.workspaces
                if not k.default_model:
                    if arg and arg != "inherit":
                        k.default_model = alias.get(arg, arg)
                    elif parent.model_at(step) != "unknown":
                        k.default_model = parent.model_at(step)


# ─────────────────────────────── analysis ───────────────────────────────

def reactions(c: Conv) -> None:
    """For each Stop block: what the agent did before it ended again, and whether a block came right back."""
    ev = c.events
    for n, (step, kind, block) in enumerate(ev):
        if kind != "stop":
            continue
        tools, ended, nxt = 0, False, None
        for j in range(n + 1, len(ev)):
            s2, k2, d2 = ev[j]
            if k2 == "user":
                break
            if k2 == "stop":
                nxt = d2
                break
            if k2 == "tools":
                tools += d2
            elif k2 == "end":
                ended = True
                if j + 1 < len(ev) and ev[j + 1][1] == "stop":
                    nxt = ev[j + 1][2]
                break
        block["tools"] = tools
        block["reaction"] = "acted" if tools else "re-ended" if ended else "no-reply"
        same = nxt is not None and bool(set(nxt["kinds"]) & set(block["kinds"]))
        block["repeated"] = same
        block["identical"] = same and (norm(nxt["text"]) == norm(block["text"])
                                       or bool(set(nxt["items"]) & set(block["items"])))
        block["next_step"] = nxt["step"] if same else None


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def guard_entries(since: dt.datetime) -> List[dict]:
    out = []
    try:
        with open(GUARD_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("rule"):
            continue
        ts = parse_ts(str(d.get("ts") or ""))
        if ts is None or ts < since:
            continue
        rule = str(d["rule"])
        d["decision"] = "logged (bypass)" if rule.startswith("bypass:") else "deny" if rule in DENY_RULES else "ask"
        d["when"] = ts
        out.append(d)
    return out


def match_calls(entries: List[dict], convs: Dict[str, Conv]) -> None:
    """Each guard.log entry → the tool step it was about (same conversation, tool and command, nearest in time)."""
    used = set()
    for e in entries:
        c = convs.get(str(e.get("conversation") or ""))
        e["step"], e["outcome"] = None, "unknown"
        if not c:
            continue
        subj = str(e.get("subject") or "")
        best, gap = None, None
        for k, call in enumerate(c.calls):
            if (c.cid, k) in used or call["tool"] != e.get("tool") or call["subject"][:300] != subj:
                continue
            g = abs((call["ts"] - e["when"]).total_seconds()) if call["ts"] else 10 ** 9
            if gap is None or g < gap:
                best, gap = k, g
        if best is not None and gap is not None and gap < 6 * 3600:
            used.add((c.cid, best))
            call = c.calls[best]
            e["step"], e["outcome"] = call["step"], call["outcome"]
            e["model"] = c.model_at(call["step"])
        else:
            e["model"] = c.model_at(10 ** 9)


def stats_row() -> dict:
    return {"fired": 0, "conversations": set(), "models": Counter(), "per_conv": Counter()}


def analyze(days: float, workspace: Optional[str]) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    convs: Dict[str, Conv] = {}
    for cid, path in transcripts():
        try:
            if os.path.getmtime(path) < since.timestamp():
                continue
        except OSError:
            continue
        convs[cid] = scan(cid, path)
    resolve(convs)
    only = os.path.realpath(os.path.expanduser(workspace)) if workspace else None
    if only:
        only_nc = os.path.normcase(only)

        def inside(c: Conv) -> bool:
            return any(os.path.normcase(os.path.realpath(w)) == only_nc
                       or os.path.normcase(os.path.realpath(w)).startswith(only_nc.rstrip(os.sep) + os.sep)
                       for w in c.workspaces)
        convs = {k: c for k, c in convs.items() if inside(c)}
    recent = lambda ts: ts is None or ts >= since  # noqa: E731

    hints: Dict[str, dict] = defaultdict(stats_row)
    checkers: Counter = Counter()
    old_lint = fixed = check_msgs = 0
    stop: Dict[str, dict] = defaultdict(lambda: {**stats_row(), "acted": 0, "re-ended": 0, "no-reply": 0,
                                                 "repeated": 0, "identical": 0})
    blocks_all: List[Tuple[Conv, dict]] = []
    transcript_guard = Counter()
    active = set()
    for c in convs.values():
        for step, kind, ts, model in c.hints:
            if not recent(ts):
                continue
            h = hints[kind]
            h["fired"] += 1
            h["conversations"].add(c.cid)
            h["models"][model or c.default_model or "unknown"] += 1
            h["per_conv"][c.cid] += 1
            active.add(c.cid)
        for step, content in c.checks:
            ts = next((t for s, k, t, _m in c.hints if s == step), None)
            if not recent(ts):
                continue
            check_msgs += 1
            for line in content.splitlines():
                line = line.strip()
                if FIXED_LINE.match(line):
                    fixed += 1
                m = CHECK_LINE.match(line)
                if not m:
                    continue
                for part in m.group(2).split("; "):
                    part = part.strip()
                    if OLD_LINT.match(part):
                        old_lint += 1
                    elif part and not part.startswith("…"):
                        checkers[part.split()[0]] += 1
        reactions(c)
        for b in c.blocks:
            if not recent(b["ts"]):
                continue
            active.add(c.cid)
            blocks_all.append((c, b))
            model = c.model_at(b["step"])
            for k in b["kinds"]:
                s = stop[k]
                s["fired"] += 1
                s["conversations"].add(c.cid)
                s["models"][model] += 1
                s[b["reaction"]] += 1
                s["repeated"] += int(b["repeated"])
                s["identical"] += int(b["identical"])
        for call in c.calls:
            if recent(call["ts"]) and call["outcome"] in ("denied", "timeout", "rejected", "prompt-error"):
                transcript_guard[call["outcome"]] += 1
                active.add(c.cid)

    entries = guard_entries(since)
    if only:
        entries = [e for e in entries if e.get("conversation") in convs]
    match_calls(entries, convs)
    rules: Dict[str, dict] = defaultdict(lambda: {**stats_row(), "decision": "", "outcomes": Counter()})
    top_cmds: Counter = Counter()
    for e in entries:
        r = rules[e["rule"]]
        r["decision"] = e["decision"]
        r["fired"] += 1
        r["conversations"].add(e.get("conversation") or "?")
        r["models"][e.get("model") or "unknown"] += 1
        r["outcomes"][e["outcome"]] += 1
        top_cmds[(e["rule"], e["decision"], e.get("tool") or "", command(str(e.get("subject") or "")))] += 1

    still = Counter()
    for p in glob.glob(os.path.join(STATE_DIR, "*.checks.json")):
        cid = os.path.basename(p)[: -len(".checks.json")]
        if only and cid not in convs:
            continue
        d = read_json(p)
        for info in ((d or {}).get("files") or {}).values() if isinstance(d, dict) else []:
            if isinstance(info, dict) and info.get("bad") and float(info.get("checked") or 0) >= since.timestamp():
                for sig in info.get("sigs") or []:
                    still[str(sig).split("|", 1)[0]] += 1

    cands = candidates(blocks_all, entries)
    plain = lambda row: {"fired": row["fired"], "conversations": len(row["conversations"]),  # noqa: E731
                         "models": dict(row["models"].most_common()),
                         **({"max_in_one": row["per_conv"].most_common(1)[0][1],
                             "max_conversation": row["per_conv"].most_common(1)[0][0]} if row["per_conv"] else {})}
    return {
        "since": since.astimezone().isoformat(timespec="minutes"), "days": days,
        "workspace": mask(only) if only else None,
        "scanned": {"conversations": len(convs), "with_hook_activity": len(active), "guard_log_entries": len(entries)},
        "hints": {k: {**plain(v), "hook": HINT_KINDS.get(k, "")}
                  for k, v in sorted(hints.items(), key=lambda kv: -kv[1]["fired"])},
        "stop": {"blocks": len(blocks_all), "conversations": len({c.cid for c, _b in blocks_all}),
                 "by_category": {k: {**plain(v), **{x: v[x] for x in ("acted", "re-ended", "no-reply", "repeated",
                                                                       "identical")}}
                                 for k, v in sorted(stop.items(), key=lambda kv: -kv[1]["fired"])}},
        "guard": {"entries": len(entries),
                  "by_rule": {k: {**plain(v), "decision": v["decision"], "outcomes": dict(v["outcomes"].most_common())}
                              for k, v in sorted(rules.items(), key=lambda kv: -kv[1]["fired"])},
                  "top_commands": [{"rule": r, "decision": d, "tool": t, "command": cmd, "times": n}
                                   for (r, d, t, cmd), n in top_cmds.most_common(TOP)],
                  "transcripts": {"denied_by_hook": transcript_guard["denied"],
                                  "prompts_timed_out": transcript_guard["timeout"],
                                  "prompts_refused": transcript_guard["rejected"],
                                  "prompts_other_error": transcript_guard["prompt-error"],
                                  "asks_then_ran": sum(1 for e in entries if e["decision"] == "ask"
                                                       and e["outcome"] == "ran")}},
        "edit_checks": {"messages": check_msgs, "errors_by_checker": dict(checkers.most_common()),
                        "old_lint_reports": old_lint, "fixed": fixed, "still_failing_by_checker": dict(still.most_common())},
        "candidates": cands,
    }


def candidates(blocks: List[Tuple[Conv, dict]], entries: List[dict]) -> List[dict]:
    """Places worth a manual look, ranked by how strongly they hint at a false positive."""
    out = []
    for c, b in blocks:
        kinds = "/".join(b["kinds"])
        if b["identical"]:
            out.append({"score": 5, "kind": f"stop:{kinds}", "conversation": c.cid, "step": b["step"],
                        "why": f"the same block came back at step {b['next_step']} after the agent "
                               + ("made tool calls" if b["tools"] else "ended again without a tool call"),
                        "excerpt": excerpt(b["text"])})
        elif b["repeated"]:
            out.append({"score": 3, "kind": f"stop:{kinds}", "conversation": c.cid, "step": b["step"],
                        "why": f"a block of the same kind came back at step {b['next_step']}",
                        "excerpt": excerpt(b["text"])})
        elif b["reaction"] == "re-ended":
            out.append({"score": 1, "kind": f"stop:{kinds}", "conversation": c.cid, "step": b["step"],
                        "why": "the agent ended again without a tool call (it judged the block didn't apply, or "
                               "only fixed its wording)", "excerpt": excerpt(b["text"])})
    denials = defaultdict(list)
    asks = defaultdict(list)
    for e in entries:
        if e["decision"] == "deny":
            denials[(e.get("conversation"), e["rule"], e.get("subject"))].append(e)
        elif e["decision"] == "ask":
            asks[e["rule"]].append(e)
    for (cid, rule, subj), es in denials.items():
        if len(es) >= 2:
            out.append({"score": 3, "kind": f"guard:{rule}", "conversation": cid or "?", "step": es[-1].get("step"),
                        "why": f"denied {len(es)} times for the same target in one conversation",
                        "excerpt": command(str(subj or ""))})
    for rule, es in asks.items():
        ran = [e for e in es if e["outcome"] == "ran"]
        if ran:
            e = ran[0]
            out.append({"score": 2, "kind": f"guard:{rule}", "conversation": e.get("conversation") or "?",
                        "step": e.get("step"), "why": f"the user let {len(ran)} of {len(es)} asks run",
                        "excerpt": command(str(e.get("subject") or ""))})
    out.sort(key=lambda d: (-d["score"], d["conversation"], d["step"] if d["step"] is not None else -1))
    return out[:CANDIDATES]


# ─────────────────────────────── output ───────────────────────────────

def models_cell(models: Dict[str, int], n: int = 3) -> str:
    items = list(models.items())
    cell = ", ".join(f"{m} {k}" for m, k in items[:n])
    return cell + (f", +{len(items) - n}" if len(items) > n else "")


def render(r: dict) -> str:
    out = [f"# Antigravity Plus insights — last {r['days']:g} days (since {r['since'][:16].replace('T', ' ')})"]
    if r["workspace"]:
        out.append(f"Workspace: {r['workspace']}")
    s = r["scanned"]
    out.append(f"Scanned {s['conversations']} conversations ({s['with_hook_activity']} with hook activity), "
               f"{s['guard_log_entries']} guard.log entries.")

    out.append("\n## Hints before model calls (PreInvocation)")
    if r["hints"]:
        out.append("| hint | fired | conversations | max in one | models | hook |\n|---|---|---|---|---|---|")
        for k, v in r["hints"].items():
            out.append(f"| {k} | {v['fired']} | {v['conversations']} | {v.get('max_in_one', 0)} | "
                       f"{models_cell(v['models'])} | {v['hook']} |")
    else:
        out.append("None in this period.")

    st = r["stop"]
    out.append(f"\n## Stop hook blocks — {st['blocks']} in {st['conversations']} conversations")
    if st["by_category"]:
        out.append("| category | blocks | conv. | acted | re-ended | no reply | same kind again | same block again | models |\n"
                   "|---|---|---|---|---|---|---|---|---|")
        for k, v in st["by_category"].items():
            out.append(f"| {k} | {v['fired']} | {v['conversations']} | {v['acted']} | {v['re-ended']} | "
                       f"{v['no-reply']} | {v['repeated']} | {v['identical']} | {models_cell(v['models'])} |")
        out.append("acted = tool calls before ending again; re-ended = ended again without one; «same block again» = "
                   "the next end was blocked with the same text or items.")

    g = r["guard"]
    out.append(f"\n## Guard — {g['entries']} logged decisions")
    if g["by_rule"]:
        out.append("| rule | decision | times | conv. | what happened next | models |\n|---|---|---|---|---|---|")
        for k, v in g["by_rule"].items():
            outcomes = ", ".join(f"{o} {n}" for o, n in v["outcomes"].items())
            out.append(f"| {k} | {v['decision']} | {v['fired']} | {v['conversations']} | {outcomes} | "
                       f"{models_cell(v['models'])} |")
        out.append("Top commands:")
        for t in g["top_commands"]:
            out.append(f"- {t['times']}× {t['rule']} ({t['decision']}, {t['tool']}): `{t['command']}`")
    tg = g["transcripts"]
    out.append(f"In transcripts: {tg['denied_by_hook']} tool calls denied by the hook; prompts: "
               f"{tg['asks_then_ran']} asks ran (approved), {tg['prompts_timed_out']} timed out, "
               f"{tg['prompts_refused']} refused, {tg['prompts_other_error']} failed otherwise.")

    e = r["edit_checks"]
    out.append(f"\n## Edit checks — {e['messages']} messages")
    if e["errors_by_checker"] or e["fixed"] or e["old_lint_reports"]:
        out.append("Errors shown by checker: " + (", ".join(f"{k} {n}" for k, n in e["errors_by_checker"].items()) or "—")
                   + f"; old-linter counts {e['old_lint_reports']}; fixed ✓ {e['fixed']}.")
    if e["still_failing_by_checker"]:
        out.append("Still failing at the last check (state): "
                   + ", ".join(f"{k} {n}" for k, n in e["still_failing_by_checker"].items()) + ".")

    out.append("\n## Candidates to review")
    if not r["candidates"]:
        out.append("No false-positive signals in this period.")
    for n, cnd in enumerate(r["candidates"], 1):
        where = f"{cnd['conversation']} step {cnd['step']}" if cnd["step"] is not None else cnd["conversation"]
        out.append(f"{n}. [{cnd['score']}] {cnd['kind']} — {where}: {cnd['why']}. «{cnd['excerpt']}»")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--workspace")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    report = analyze(a.days, a.workspace)
    print(json.dumps(report, ensure_ascii=False, indent=1) if a.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
