#!/usr/bin/env python3
r"""Antigravity Plus — read-only replay harness over real or synthetic transcripts.

Walks each conversation transcript step by step in an isolated temporary HOME and invokes the plugin's hooks
(session_start.py, instructions.py, guard.py, check_edit.py, stop.py) as Antigravity does, feeding a growing copy of
the transcript. Reports counts per hook, hint tag, Stop continue category, and guard decision/rule; supports saving
baselines (--save), diffing two runs (--compare), and printing matching spots (--show KIND).

Stdlib only, Python 3.8+. Never modifies real ~/.gemini state, real workspaces, or network resources.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import glob
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_PLUGIN = os.path.join(REPO_ROOT, "antigravity-plus")
DEFAULT_TRANSCRIPT_GLOB = os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity", "brain", "*", ".system_generated", "logs", "transcript_full.jsonl"
)

HINT_TAG_RE = re.compile(r'<([a-z_]+) source="Antigravity Plus">')
BYPASS_NOTE_RE = re.compile(
    r"^\s*(?:The user turned Bypass Mode (?:ON|OFF)|Bypass Mode was turned (?:ON|OFF) in another conversation)"
)
WORKSPACE_RE = re.compile(r"^## Workspace: (.+?)\s*$", re.M)
MODEL_CHANGE_RE = re.compile(r"Model Selection`\s+from\s+(.*?)\s+to\s+(.*?)\.(?:\s|$)")
REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
TAGS_RE = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|[A-Z_]+_METADATA)>.*?</\1>", re.S)

STOP_KINDS: List[Tuple[str, "re.Pattern[str]"]] = [
    (k, re.compile(p, re.S))
    for k, p in [
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
    ]
]

SECRET_MASK_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(
        r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{20,}"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+|\bglpat-[A-Za-z0-9_-]{16,}"
    ),
]
HOME = os.path.expanduser("~")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b[\w.-]*(?:pass(?:word)?|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credentials?|auth)[\w.-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'&;|,)]+)"
)
SECRET_FLAG_RE = re.compile(
    r"(?i)((?:--(?:password|token|api-key|secret)(?:=|\s+)|\b(?:Bearer|Basic|token)\s+))"
    r"(\"[^\"]{3,}\"|'[^']{3,}'|[^\s\"']{6,})"
)
URL_CREDS_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{28,}")

# Worker-process globals (initialized once per worker process after HOME is isolated).
_WORKER: Dict[str, Any] = {}


def _mask_excerpt(text: str, limit: int = 200) -> str:
    """Collapse whitespace, mask secret-like tokens, and clip to <= limit chars."""
    t = text or ""
    for pat in SECRET_MASK_PATTERNS:
        t = pat.sub("***", t)
    t = URL_CREDS_RE.sub(r"\1***@", t)
    t = SECRET_FLAG_RE.sub(lambda m: m.group(1) + "***", t)
    t = SECRET_ASSIGN_RE.sub(lambda m: m.group(1) + "***", t)
    t = re.sub(r"«[^»]{1,200}»", "«…»", t)
    if HOME and len(HOME) > 1:
        t = t.replace(HOME, "~")
        if os.sep == "\\":
            t = t.replace(HOME.replace("\\", "/"), "~")

    def _long_token(m: "re.Match[str]") -> str:
        s = m.group(0)
        if UUID_RE.fullmatch(s) or sum(c.isdigit() for c in s) < 4 or sum(c.isalpha() for c in s) < 4:
            return s
        return "***"

    t = LONG_TOKEN_RE.sub(_long_token, t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _extract_user_text(content: str) -> str:
    m = REQUEST_RE.search(content or "")
    text = m.group(1) if m else TAGS_RE.sub("", content or "")
    return text.strip()[:3000]


def _parse_iso_ts(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(cleaned).timestamp()
    except (ValueError, TypeError):
        return None


def _cid_from_path(path: str) -> str:
    parts = os.path.normpath(path).split(os.sep)
    cand = parts[-4] if len(parts) >= 4 and parts[-2] == "logs" and parts[-3] == ".system_generated" else ""
    if re.fullmatch(r"[\w-]{8,80}", cand):
        return cand
    stem = re.sub(r"[^\w-]", "-", os.path.basename(os.path.dirname(os.path.abspath(path))))
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8", errors="replace")).hexdigest()[:10]
    raw = f"{stem[:24]}-{digest}".strip("-")
    return raw if len(raw) >= 8 else f"replay-{digest}"


def _artifact_dir_for(path: str) -> str:
    norm = os.path.normpath(os.path.abspath(path))
    parts = norm.split(os.sep)
    if len(parts) >= 4 and parts[-2] == "logs" and parts[-3] == ".system_generated":
        return os.sep.join(parts[:-3])
    return os.path.dirname(norm)


def load_hooks_config(plugin_dir: str) -> dict:
    """Parse antigravity-plus/hooks.json to resolve hook scripts and tool matchers."""
    cfg_path = os.path.join(plugin_dir, "hooks.json")
    spec: Dict[str, Any] = {
        "session_start": os.path.join(plugin_dir, "hooks", "session_start.py"),
        "instructions": os.path.join(plugin_dir, "hooks", "instructions.py"),
        "guard": os.path.join(plugin_dir, "hooks", "guard.py"),
        "check_edit": os.path.join(plugin_dir, "hooks", "check_edit.py"),
        "stop": os.path.join(plugin_dir, "hooks", "stop.py"),
        "guard_matchers": ["run_command", "write_file", "edit_file", "read_file"],
        "check_edit_matchers": ["read_file", "write_file", "edit_file"],
    }
    try:
        with open(cfg_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return spec
    if not isinstance(raw, dict):
        return spec

    def script_of(entry: dict) -> Optional[str]:
        cmd = str(entry.get("command") or "")
        m = re.search(r"(hooks/[\w.-]+\.py)", cmd)
        if not m:
            return None
        p = os.path.join(plugin_dir, m.group(1))
        return p if os.path.isfile(p) else None

    guard_tools: List[str] = []
    check_tools: List[str] = []
    for block in raw.values():
        if not isinstance(block, dict):
            continue
        for event, items in block.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if event == "SessionStart":
                    p = script_of(item)
                    if p:
                        spec["session_start"] = p
                elif event == "PreInvocation":
                    p = script_of(item)
                    if p:
                        spec["instructions"] = p
                elif event == "Stop":
                    p = script_of(item)
                    if p:
                        spec["stop"] = p
                elif event == "PreToolUse":
                    matcher = str(item.get("matcher") or "")
                    for h in item.get("hooks") or []:
                        if isinstance(h, dict):
                            p = script_of(h)
                            if p and os.path.basename(p) == "guard.py":
                                spec["guard"] = p
                                if matcher:
                                    guard_tools.extend(m for m in matcher.split("|") if m)
                elif event == "PostToolUse":
                    matcher = str(item.get("matcher") or "")
                    for h in item.get("hooks") or []:
                        if isinstance(h, dict):
                            p = script_of(h)
                            if p and os.path.basename(p) == "check_edit.py":
                                spec["check_edit"] = p
                                if matcher:
                                    check_tools.extend(m for m in matcher.split("|") if m)
    if guard_tools:
        spec["guard_matchers"] = list(dict.fromkeys(guard_tools))
    if check_tools:
        spec["check_edit_matchers"] = list(dict.fromkeys(check_tools))
    return spec


def _init_worker(replay_root: str, plugin_dir: str, hooks_cfg: dict) -> None:
    """Isolate HOME and import plugin hook modules inside each worker process."""
    worker_home = os.path.join(replay_root, f"w-{os.getpid()}")
    os.makedirs(os.path.join(worker_home, ".gemini", "antigravity-plus", "state"), exist_ok=True)
    os.environ["HOME"] = worker_home
    os.environ["USERPROFILE"] = worker_home
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["AGP_UPDATE_CHECK"] = "off"
    os.environ["AGP_UPDATE_PANEL"] = "off"
    os.environ["AGP_QUOTA_CHECK"] = "off"
    os.environ["AGP_LSP_DAEMON"] = "0"
    for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_CONVERSATION_ID", "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(var, None)

    hooks_dir = os.path.join(plugin_dir, "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)

    mods: Dict[str, Any] = {}
    for key in ("session_start", "instructions", "guard", "check_edit", "stop"):
        path = hooks_cfg.get(key)
        if isinstance(path, str) and os.path.isfile(path):
            mod_name = f"_agp_replay_{key}"
            mod_spec = importlib.util.spec_from_file_location(mod_name, path)
            if mod_spec and mod_spec.loader:
                mod = importlib.util.module_from_spec(mod_spec)
                mod_spec.loader.exec_module(mod)
                mods[key] = mod

    _WORKER.clear()
    _WORKER.update(home=worker_home, plugin=plugin_dir, cfg=hooks_cfg, mods=mods)


def _reset_worker_home(worker_home: str) -> None:
    for sub in (".gemini", ".claude"):
        target = os.path.join(worker_home, sub)
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
    os.makedirs(os.path.join(worker_home, ".gemini", "antigravity-plus", "state"), exist_ok=True)


def _call_hook(hook_key: str, payload: dict) -> Tuple[dict, str]:
    mod = _WORKER["mods"].get(hook_key)
    script_path = _WORKER["cfg"].get(hook_key) or ""
    if mod is None or not hasattr(mod, "main"):
        return {}, ""
    old_stdin, old_stdout, old_stderr, old_argv = sys.stdin, sys.stdout, sys.stderr, sys.argv
    inp = io.StringIO(json.dumps(payload, ensure_ascii=False))
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = [script_path]
        sys.stdin, sys.stdout, sys.stderr = inp, out, err
        try:
            mod.main()
        except SystemExit:
            pass
    except Exception as exc:
        return {}, f"{hook_key}: {exc}"
    finally:
        sys.argv = old_argv
        sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
    raw = out.getvalue().strip()
    if not raw:
        return {}, err.getvalue().strip()
    try:
        parsed = json.loads(raw)
        return (parsed if isinstance(parsed, dict) else {}), err.getvalue().strip()
    except ValueError:
        return {}, err.getvalue().strip()


def _load_transcript_steps(path: str) -> Tuple[List[dict], int]:
    """Load steps cleanly, surviving blank/corrupt lines and non-dict records."""
    steps: List[Tuple[int, int, dict]] = []
    malformed = 0
    try:
        with open(path, "rb") as f:
            raw_text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return [], 1
    for line_no, line in enumerate(raw_text.splitlines()):
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(d, dict) or not isinstance(d.get("type"), str):
            malformed += 1
            continue
        si = d.get("step_index")
        order_idx = si if isinstance(si, int) else line_no
        steps.append((order_idx, line_no, d))
    steps.sort(key=lambda item: (item[0], item[1]))
    dedup: Dict[int, int] = {}
    ordered: List[dict] = []
    for order_idx, _line_no, d in steps:
        if isinstance(d.get("step_index"), int):
            if order_idx in dedup:
                ordered[dedup[order_idx]] = d
            else:
                dedup[order_idx] = len(ordered)
                ordered.append(d)
        else:
            ordered.append(d)
    return ordered, malformed


def _is_historical_plugin_step(d: dict) -> bool:
    typ = d.get("type") or ""
    content = d.get("content") or ""
    if not isinstance(content, str):
        content = ""
    if typ == "EPHEMERAL_MESSAGE":
        return True
    if typ == "SYSTEM_MESSAGE":
        if "Stop hook blocked termination:" in content or "<antigravity_plus_session_context>" in content:
            return True
    return False


def _project_root_of(path: str) -> Optional[str]:
    """Walk up from an existing file or folder to its project root (.git / AGENTS.md / pyproject.toml / package.json)."""
    p = os.path.abspath(os.path.expanduser(path))
    d = p if os.path.isdir(p) else os.path.dirname(p)
    if not os.path.isdir(d):
        return None
    cur = d
    markers = (".git", "AGENTS.md", "GEMINI.md", "CLAUDE.md", "pyproject.toml", "package.json")
    for _ in range(8):
        if any(os.path.exists(os.path.join(cur, m)) for m in markers):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            break
        cur = up
    return d


def _extract_workspaces(steps: List[dict], explicit_ws: Optional[List[str]] = None) -> List[str]:
    if explicit_ws:
        return [os.path.abspath(os.path.expanduser(w)) for w in explicit_ws if w]
    for st in steps:
        if st.get("type") == "SYSTEM_MESSAGE":
            content = st.get("content") or ""
            if isinstance(content, str) and "antigravity_plus_session_context" in content:
                found = [w.strip() for w in WORKSPACE_RE.findall(content) if w.strip()]
                if found:
                    return found
    inferred: List[str] = []
    for st in steps:
        if st.get("type") != "PLANNER_RESPONSE":
            continue
        raw_calls = st.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            args = _parse_tool_args(tc.get("args"))
            for k in ("Cwd", "Path", "TargetFile", "path"):
                v = args.get(k)
                if not isinstance(v, str) or not v or "/.gemini/" in v.replace("\\", "/"):
                    continue
                if os.path.isabs(v):
                    root = _project_root_of(v)
                    if root and root not in inferred:
                        inferred.append(root)
                        if len(inferred) >= 3:
                            return inferred
    return inferred


def _parse_tool_args(raw_args: Any) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            d = json.loads(raw_args)
            if isinstance(d, dict):
                return d
        except ValueError:
            return {}
    return {}


def _is_task_md_call(args: dict, real_art: str) -> bool:
    p = args.get("Path") or args.get("TargetFile") or args.get("path") or ""
    if not isinstance(p, str) or not p:
        return False
    norm = p.replace("\\", "/")
    if not norm.endswith("/task.md") and norm != "task.md":
        return False
    if real_art:
        real_norm = os.path.normpath(real_art).replace("\\", "/")
        if norm.startswith(real_norm + "/") or "/.gemini/antigravity/brain/" in norm:
            return True
    return "/.gemini/antigravity/brain/" in norm


def _apply_task_md(temp_art: str, tool_name: str, args: dict, created_at: str) -> None:
    task_file = os.path.join(temp_art, "task.md")
    try:
        if tool_name == "write_file":
            content = args.get("Content")
            if isinstance(content, str):
                with open(task_file, "w", encoding="utf-8") as f:
                    f.write(content)
        elif tool_name == "edit_file" and os.path.isfile(task_file):
            target = args.get("Target")
            repl = args.get("Replacement")
            if isinstance(target, str) and isinstance(repl, str) and target:
                with open(task_file, encoding="utf-8", errors="replace") as f:
                    cur = f.read()
                if target in cur:
                    with open(task_file, "w", encoding="utf-8") as f:
                        f.write(cur.replace(target, repl))
        ts = _parse_iso_ts(created_at)
        if ts and os.path.isfile(task_file):
            os.utime(task_file, (ts, ts))
    except OSError:
        pass


def _read_new_guard_rules(log_path: str, offset: int) -> Tuple[List[str], int]:
    if not os.path.isfile(log_path):
        return [], offset
    try:
        size = os.path.getsize(log_path)
        if size <= offset:
            return [], size
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        rules: List[str] = []
        for raw in chunk.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                if isinstance(rec, dict) and rec.get("rule"):
                    rules.append(str(rec["rule"]))
            except ValueError:
                continue
        return rules, size
    except OSError:
        return [], offset


def _read_checks_pending_sig(checks_path: str) -> List[Tuple[str, bool, int]]:
    if not os.path.isfile(checks_path):
        return []
    try:
        with open(checks_path, encoding="utf-8") as f:
            data = json.load(f)
        out: List[Tuple[str, bool, int]] = []
        for e in data.get("pending") or []:
            if isinstance(e, dict):
                out.append((str(e.get("path") or ""), bool(e.get("fixed")), len(e.get("show") or [])))
        return out
    except (OSError, ValueError):
        return []


def _stop_categories(reason: str) -> List[str]:
    found = sorted((m.start(), k) for k, pat in STOP_KINDS for m in [pat.search(reason)] if m)
    return [k for _pos, k in found] or ["other"]


def _extract_hint_tags(msg: str) -> List[str]:
    tags = list(dict.fromkeys(HINT_TAG_RE.findall(msg)))
    if BYPASS_NOTE_RE.search(msg):
        tags.append("bypass_notice")
    return tags or ["other_hint"]


def _replay_one(task: Tuple[str, Optional[str], List[str]]) -> dict:
    tp, want_show, explicit_ws = task
    worker_home = _WORKER["home"]
    _reset_worker_home(worker_home)

    cid = _cid_from_path(tp)
    real_art = _artifact_dir_for(tp)
    conv_dir = os.path.join(worker_home, "replay_conv")
    if os.path.exists(conv_dir):
        shutil.rmtree(conv_dir, ignore_errors=True)
    temp_art = os.path.join(conv_dir, "art")
    os.makedirs(temp_art, exist_ok=True)
    growing_tp = os.path.join(conv_dir, "transcript_full.jsonl")
    with open(growing_tp, "w", encoding="utf-8"):
        pass

    steps, malformed = _load_transcript_steps(tp)
    workspaces = _extract_workspaces(steps, explicit_ws)
    is_main = any(st.get("type") == "USER_INPUT" for st in steps)

    # Check whether task.md is ever written/edited in this transcript; if not and real_art/task.md exists, copy it.
    has_task_writes = any(
        isinstance(tc, dict)
        and tc.get("name") in ("write_file", "edit_file")
        and _is_task_md_call(_parse_tool_args(tc.get("args")), real_art)
        for st in steps
        if st.get("type") == "PLANNER_RESPONSE" and isinstance(st.get("tool_calls"), list)
        for tc in st["tool_calls"]
    )
    real_task_md = os.path.join(real_art, "task.md")
    if not has_task_writes and os.path.isfile(real_task_md):
        try:
            shutil.copy2(real_task_md, os.path.join(temp_art, "task.md"))
        except OSError:
            pass

    guard_log = os.path.join(worker_home, ".gemini", "antigravity-plus", "guard.log")
    checks_file = os.path.join(worker_home, ".gemini", "antigravity-plus", "state", f"{cid}.checks.json")
    guard_offset = 0

    stats: Dict[str, Any] = {
        "cid": cid,
        "main": is_main,
        "steps": len(steps),
        "malformed": malformed,
        "hook_errors": 0,
        "hooks": {
            "session_start": {"calls": 0, "fired": 0},
            "instructions": {"calls": 0, "fired": 0},
            "guard": {"calls": 0, "fired": 0},
            "check_edit": {"calls": 0, "fired": 0},
            "stop": {"calls": 0, "fired": 0},
        },
        "hints": {},
        "stop_reasons": {},
        "guard_decisions": {},
        "guard_rules": {},
        "spots": [],
    }

    base = {
        "conversationId": cid,
        "transcriptPath": growing_tp,
        "artifactDirectoryPath": temp_art,
        "workspacePaths": workspaces,
    }

    guard_matchers = set(_WORKER["cfg"].get("guard_matchers") or [])
    check_matchers = set(_WORKER["cfg"].get("check_edit_matchers") or [])

    session_started = False
    last_user = ""
    current_model = ""
    invocation_num = 0
    execution_num = 0
    pending_calls: List[Tuple[str, dict]] = []

    def append_step(row: dict) -> None:
        with open(growing_tp, "a", encoding="utf-8") as gf:
            gf.write(json.dumps(row, ensure_ascii=False) + "\n")

    def add_spot(tags: Set[str], step_idx: int, label: str, text: str) -> None:
        if not want_show:
            return
        if want_show in tags:
            stats["spots"].append((cid, step_idx, label, _mask_excerpt(text)))

    def run_session_start(step_idx: int) -> None:
        nonlocal session_started
        session_started = True
        payload = dict(base)
        if last_user:
            payload["lastUserInput"] = last_user
        stats["hooks"]["session_start"]["calls"] += 1
        out, err = _call_hook("session_start", payload)
        if err:
            stats["hook_errors"] += 1
        injects = out.get("injectSteps") if isinstance(out, dict) else None
        if isinstance(injects, list) and injects:
            stats["hooks"]["session_start"]["fired"] += 1
            for item in injects:
                if isinstance(item, dict) and isinstance(item.get("systemMessage"), dict):
                    msg = str(item["systemMessage"].get("systemMessage") or "")
                    if msg:
                        add_spot({"session_start", "hook:session_start"}, step_idx, "hook:session_start", msg)
                        append_step(
                            {
                                "step_index": step_idx,
                                "type": "SYSTEM_MESSAGE",
                                "status": "DONE",
                                "content": f"<SYSTEM_MESSAGE>\n{msg}\n</SYSTEM_MESSAGE>",
                            }
                        )

    for idx, st in enumerate(steps):
        typ = st.get("type") or ""
        raw_idx = st.get("step_index")
        step_idx: int = raw_idx if isinstance(raw_idx, int) else idx
        raw_content = st.get("content")
        content: str = raw_content if isinstance(raw_content, str) else ""

        if _is_historical_plugin_step(st):
            continue

        if typ == "USER_INPUT":
            last_user = _extract_user_text(content)
            for _old, new in MODEL_CHANGE_RE.findall(content):
                current_model = new.strip()
            execution_num = 0
            pending_calls = []
            append_step(st)
            if not session_started:
                run_session_start(step_idx)
            continue

        if typ == "CHECKPOINT" or typ == "SYSTEM_MESSAGE":
            append_step(st)
            continue

        if typ == "PLANNER_RESPONSE":
            if not session_started:
                run_session_start(step_idx)

            # PreInvocation runs before the model generates this PLANNER_RESPONSE.
            pre_payload: Dict[str, Any] = {
                **base,
                "lastUserInput": last_user,
                "invocationNum": invocation_num,
            }
            if current_model:
                pre_payload["modelName"] = current_model
            invocation_num += 1
            stats["hooks"]["instructions"]["calls"] += 1
            pre_out, pre_err = _call_hook("instructions", pre_payload)
            if pre_err:
                stats["hook_errors"] += 1
            injects = pre_out.get("injectSteps") if isinstance(pre_out, dict) else None
            if isinstance(injects, list) and injects:
                stats["hooks"]["instructions"]["fired"] += 1
                for item in injects:
                    msg = item.get("ephemeralMessage") if isinstance(item, dict) else None
                    if isinstance(msg, str) and msg.strip():
                        for tag in _extract_hint_tags(msg):
                            stats["hints"][tag] = stats["hints"].get(tag, 0) + 1
                            add_spot(
                                {tag, f"hint:{tag}", "instructions", "hook:instructions"},
                                step_idx,
                                f"hint:{tag}",
                                msg,
                            )

            append_step(st)
            raw_calls = st.get("tool_calls")
            tcs = [c for c in raw_calls if isinstance(c, dict)] if isinstance(raw_calls, list) else []
            if tcs:
                pending_calls = []
                for c in tcs:
                    tname = str(c.get("name") or "")
                    targs = _parse_tool_args(c.get("args"))
                    pending_calls.append((tname, targs))
                    if tname in guard_matchers:
                        g_payload = {**base, "toolCall": {"name": tname, "args": targs}}
                        stats["hooks"]["guard"]["calls"] += 1
                        g_out, g_err = _call_hook("guard", g_payload)
                        if g_err:
                            stats["hook_errors"] += 1
                        decision = str((g_out or {}).get("decision") or "unknown")
                        stats["guard_decisions"][decision] = stats["guard_decisions"].get(decision, 0) + 1
                        new_rules, guard_offset = _read_new_guard_rules(guard_log, guard_offset)
                        for rule in new_rules:
                            stats["guard_rules"][rule] = stats["guard_rules"].get(rule, 0) + 1
                            add_spot(
                                {rule, f"guard:{rule}", decision, f"guard:{decision}", "guard", "hook:guard"},
                                step_idx,
                                f"guard:{decision}:{rule}",
                                str((g_out or {}).get("reason") or f"{tname} ({rule})"),
                            )
                        if decision in ("force_ask", "deny"):
                            stats["hooks"]["guard"]["fired"] += 1
                            if not new_rules:
                                add_spot(
                                    {decision, f"guard:{decision}", "guard", "hook:guard"},
                                    step_idx,
                                    f"guard:{decision}",
                                    str((g_out or {}).get("reason") or tname),
                                )
                        elif decision == "auto_approve" and not new_rules:
                            add_spot(
                                {decision, f"guard:{decision}"},
                                step_idx,
                                f"guard:{decision}",
                                f"{tname}",
                            )
                    if tname in ("write_file", "edit_file") and _is_task_md_call(targs, real_art):
                        _apply_task_md(temp_art, tname, targs, str(st.get("created_at") or ""))
            else:
                # No tool calls: check if next real step is ERROR_MESSAGE (aborted) vs normal NO_TOOL_CALL stop.
                nxt = steps[idx + 1] if idx + 1 < len(steps) else None
                term = "ERROR" if (nxt and nxt.get("type") == "ERROR_MESSAGE") else "NO_TOOL_CALL"
                stop_payload = {
                    **base,
                    "terminationReason": term,
                    "fullyIdle": True,
                    "executionNum": execution_num,
                }
                stats["hooks"]["stop"]["calls"] += 1
                s_out, s_err = _call_hook("stop", stop_payload)
                if s_err:
                    stats["hook_errors"] += 1
                if isinstance(s_out, dict) and s_out.get("decision") == "continue":
                    stats["hooks"]["stop"]["fired"] += 1
                    execution_num += 1
                    reason = str(s_out.get("reason") or "")
                    for cat in _stop_categories(reason):
                        stats["stop_reasons"][cat] = stats["stop_reasons"].get(cat, 0) + 1
                        add_spot(
                            {cat, f"stop:{cat}", "stop", "hook:stop"},
                            step_idx,
                            f"stop:{cat}",
                            reason,
                        )
                else:
                    execution_num = 0
            continue

        if typ == "GENERIC":
            append_step(st)
            if pending_calls:
                tname, targs = pending_calls.pop(0)
                err_str = str(st.get("error") or "")
                if tname in check_matchers and "tool call denied by pre-tool hook" not in err_str:
                    before_pending = _read_checks_pending_sig(checks_file)
                    c_payload = {**base, "toolCall": {"name": tname, "args": targs}, "error": err_str}
                    stats["hooks"]["check_edit"]["calls"] += 1
                    _c_out, c_err = _call_hook("check_edit", c_payload)
                    if c_err:
                        stats["hook_errors"] += 1
                    after_pending = _read_checks_pending_sig(checks_file)
                    if after_pending != before_pending and len(after_pending) >= len(before_pending):
                        stats["hooks"]["check_edit"]["fired"] += 1
                        subj = str(targs.get("Path") or targs.get("TargetFile") or targs.get("path") or "")
                        add_spot(
                            {"check_edit", "hook:check_edit"},
                            step_idx,
                            "hook:check_edit",
                            os.path.basename(subj) or subj,
                        )
            continue

        # Any other step types (e.g. ERROR_MESSAGE): copy cleanly.
        append_step(st)

    return stats


def find_transcripts(explicit: List[str], days: Optional[float], limit: Optional[int]) -> List[str]:
    if explicit:
        paths = [os.path.abspath(os.path.expanduser(p)) for p in explicit]
    else:
        paths = glob.glob(DEFAULT_TRANSCRIPT_GLOB)
    existing: List[Tuple[float, str]] = []
    cutoff = (time.time() - days * 86400.0) if days is not None else None
    for p in paths:
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if cutoff is not None and mt < cutoff:
            continue
        existing.append((mt, p))
    existing.sort(key=lambda item: (-item[0], item[1]))
    out = [p for _mt, p in existing]
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def aggregate_results(plugin_dir: str, results: List[dict]) -> dict:
    summary: Dict[str, Any] = {
        "version": 1,
        "plugin": os.path.abspath(plugin_dir),
        "conversations": len(results),
        "main_conversations": sum(1 for r in results if r["main"]),
        "subagent_conversations": sum(1 for r in results if not r["main"]),
        "steps": sum(r["steps"] for r in results),
        "malformed_lines": sum(r["malformed"] for r in results),
        "hook_errors": sum(r["hook_errors"] for r in results),
        "hooks": {
            k: {"calls": 0, "fired": 0}
            for k in ("session_start", "instructions", "guard", "check_edit", "stop")
        },
        "hints": {},
        "stop_reasons": {},
        "guard_decisions": {},
        "guard_rules": {},
    }
    for r in results:
        for h, vals in r["hooks"].items():
            summary["hooks"][h]["calls"] += vals["calls"]
            summary["hooks"][h]["fired"] += vals["fired"]
        for bucket in ("hints", "stop_reasons", "guard_decisions", "guard_rules"):
            dest = summary[bucket]
            for k, v in r[bucket].items():
                dest[k] = dest.get(k, 0) + v
    return summary


def _normalize_show(kind: Optional[str]) -> Optional[str]:
    if not kind:
        return None
    k = kind.strip()
    if k.startswith("<") and k.endswith(">") and len(k) > 2:
        k = k[1:-1]
    return k.lower()


def _sorted_counts(d: Dict[str, int]) -> List[Tuple[str, int]]:
    return sorted(d.items(), key=lambda item: (-item[1], item[0]))


def format_summary(summary: dict, took: float) -> str:
    lines = [
        f"Antigravity Plus replay: {summary['conversations']} conversations "
        f"({summary['main_conversations']} main, {summary['subagent_conversations']} subagent), "
        f"{summary['steps']} steps, {summary['malformed_lines']} malformed lines, "
        f"{summary['hook_errors']} hook errors ({took:.1f} s)",
        "",
        "Hooks:",
    ]
    for name in ("session_start", "instructions", "guard", "check_edit", "stop"):
        st = summary["hooks"][name]
        extra = ""
        if name == "guard" and summary["guard_decisions"]:
            gd = summary["guard_decisions"]
            parts = [f"{gd[k]} {k}" for k in ("force_ask", "deny", "auto_approve", "allow") if gd.get(k)]
            for k, v in sorted(gd.items()):
                if k not in ("force_ask", "deny", "auto_approve", "allow"):
                    parts.append(f"{v} {k}")
            if parts:
                extra = "  (" + ", ".join(parts) + ")"
        verb = "continue" if name == "stop" else "fired"
        lines.append(f"  {name:<15} {st['calls']:>6} calls  {st['fired']:>6} {verb}{extra}")

    for title, bucket in (
        ("Hints", "hints"),
        ("Stop continue reasons", "stop_reasons"),
        ("Guard rules", "guard_rules"),
    ):
        data = summary[bucket]
        total = sum(data.values())
        lines.append("")
        lines.append(f"{title} ({total} total):")
        if not data:
            lines.append("  (none)")
        else:
            for k, v in _sorted_counts(data):
                lines.append(f"  {k:<22} {v:>6}")
    return "\n".join(lines)


def format_compare(baseline: dict, current: dict, baseline_path: str) -> str:
    lines = [
        "",
        f"Diff vs {baseline_path}:",
        f"  conversations: {baseline.get('conversations', 0)} -> {current['conversations']}  "
        f"steps: {baseline.get('steps', 0)} -> {current['steps']}",
    ]
    if baseline.get("conversations") != current["conversations"]:
        lines.append("  (note: conversation counts differ between baseline and current run)")

    diffs = 0
    sections: List[Tuple[str, Dict[str, int], Dict[str, int]]] = []
    b_hooks = {f"{h}.{m}": int((baseline.get("hooks") or {}).get(h, {}).get(m, 0))
               for h in ("session_start", "instructions", "guard", "check_edit", "stop")
               for m in ("calls", "fired")}
    c_hooks = {f"{h}.{m}": int(current["hooks"][h][m])
               for h in ("session_start", "instructions", "guard", "check_edit", "stop")
               for m in ("calls", "fired")}
    sections.append(("Hooks", b_hooks, c_hooks))
    for label, key in (
        ("Hints", "hints"),
        ("Stop reasons", "stop_reasons"),
        ("Guard decisions", "guard_decisions"),
        ("Guard rules", "guard_rules"),
    ):
        sections.append((label, dict(baseline.get(key) or {}), dict(current.get(key) or {})))

    for title, b_map, c_map in sections:
        keys = sorted(set(b_map) | set(c_map))
        changed = []
        for k in keys:
            bv, cv = int(b_map.get(k, 0)), int(c_map.get(k, 0))
            if bv != cv:
                delta = cv - bv
                sign = "+" if delta > 0 else ""
                changed.append(f"  {k:<24} {bv:>6} -> {cv:>6}  ({sign}{delta})")
                diffs += 1
        if changed:
            lines.append(f"  [{title}]")
            lines.extend("  " + row for row in changed)
    if diffs == 0:
        lines.append("  No differences.")
    return "\n".join(lines)


def run_replay(
    transcripts: List[str],
    plugin_dir: str,
    want_show: Optional[str] = None,
    workers: Optional[int] = None,
    workspaces: Optional[List[str]] = None,
) -> Tuple[dict, List[Tuple[str, int, str, str]], float]:
    started = time.time()
    hooks_cfg = load_hooks_config(plugin_dir)
    if not transcripts:
        return aggregate_results(plugin_dir, []), [], 0.0

    max_workers = workers if workers and workers > 0 else min(len(transcripts), max(1, os.cpu_count() or 4), 8)
    replay_root = tempfile.mkdtemp(prefix="agp-replay-")
    results: List[dict] = []
    try:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=_init_worker,
            initargs=(replay_root, plugin_dir, hooks_cfg),
        ) as pool:
            tasks = [(tp, want_show, list(workspaces or [])) for tp in transcripts]
            for res in pool.map(_replay_one, tasks):
                results.append(res)
    finally:
        shutil.rmtree(replay_root, ignore_errors=True)

    summary = aggregate_results(plugin_dir, results)
    spots: List[Tuple[str, int, str, str]] = []
    for r in results:
        spots.extend(r["spots"])
    spots.sort(key=lambda item: (item[0], item[1], item[2]))
    return summary, spots, time.time() - started


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="replay.py",
        description="Read-only replay of Antigravity transcripts through Antigravity Plus hooks.",
    )
    ap.add_argument("--transcript", action="append", default=[], metavar="PATH",
                    help="Specific transcript_full.jsonl path (repeatable; default: ~/.gemini/antigravity/brain/...)")
    ap.add_argument("--workspace", action="append", default=[], metavar="DIR",
                    help="Override workspacePaths for replayed transcripts (repeatable)")
    ap.add_argument("--days", type=float, default=None, help="Only transcripts modified within the last N days")
    ap.add_argument("--limit", type=int, default=None, help="Maximum number of transcripts (newest first)")
    ap.add_argument("--plugin", default=DEFAULT_PLUGIN, metavar="DIR",
                    help="Plugin directory containing hooks.json (default: repo antigravity-plus/)")
    ap.add_argument("--save", metavar="FILE.json", help="Save JSON summary counts to FILE.json")
    ap.add_argument("--compare", metavar="FILE.json", help="Diff current counts against a saved baseline JSON")
    ap.add_argument("--show", metavar="KIND",
                    help="Print matching spots (cid, step, <=200-char excerpt) for hint/stop/guard kind")
    ap.add_argument("--workers", type=int, default=None, help="Number of parallel worker processes")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            reconf = getattr(stream, "reconfigure", None)
            if callable(reconf):
                reconf(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    plugin_dir = os.path.abspath(os.path.expanduser(args.plugin))
    if not os.path.isdir(plugin_dir):
        print(f"Plugin directory not found: {plugin_dir}", file=sys.stderr)
        return 1

    transcripts = find_transcripts(args.transcript, args.days, args.limit)
    want_show = _normalize_show(args.show)
    summary, spots, took = run_replay(
        transcripts,
        plugin_dir,
        want_show=want_show,
        workers=args.workers,
        workspaces=args.workspace,
    )

    print(format_summary(summary, took))

    if args.save:
        out_path = os.path.abspath(os.path.expanduser(args.save))
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"\nSaved counts to {out_path}")

    if args.compare:
        base_path = os.path.abspath(os.path.expanduser(args.compare))
        try:
            with open(base_path, encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"Cannot read baseline {base_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(baseline, dict):
            print(f"Invalid baseline JSON in {base_path}", file=sys.stderr)
            return 1
        print(format_compare(baseline, summary, args.compare))

    if want_show:
        print(f"\nMatching spots for {want_show} ({len(spots)}):")
        if not spots:
            print("  (none)")
        for cid, step_idx, label, excerpt in spots:
            print(f"  {cid}  step={step_idx}  [{label}] {excerpt}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
