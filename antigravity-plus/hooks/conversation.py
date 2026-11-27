#!/usr/bin/env python3
"""Antigravity Plus — what the user has said in this conversation.

Reads the conversation transcript incrementally (only bytes added since the last call) and keeps the user's
messages in ~/.gemini/antigravity-plus/state/<conversationId>.json. Shared by:
  * hooks/instructions.py (PreInvocation) — re-shows the user's instructions so they survive long turns and
    context compaction;
  * hooks/guard.py (PreToolUse) — asks before pushing/deploying/committing when the user said not to.
Also detects standing restrictions ("не пушь", "don't commit") and their lifting ("пушь", "commit it"), and tracks
the background work the agent started — subagents, commands that went to the background, timers — what reported
back and how the agent waits, for hooks/parallel.py; how its steps go — the context size, command results
(sandbox failures and their retries outside it, failures in a row), very large reads — for hooks/speed.py; and what
it learns from — memory writes, commands on servers and deploys, failing commands it got past — for
hooks/memory_hints.py. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import compat

STATE_DIR = os.path.expanduser("~/.gemini/antigravity-plus/state")
BRAIN = os.path.expanduser("~/.gemini/antigravity/brain")
MSG_LIMIT = 3000          # chars kept per user message
STATE_TTL_DAYS = 14

HEAD = re.compile(rb'"type":\s*"(USER_INPUT|CHECKPOINT|PLANNER_RESPONSE)"')
STEP = re.compile(rb'"step_index":\s*(\d+)')
# Background work (hooks/parallel.py): invoke_subagent results, commands and timers sent to the background, messages
# back from them, and the ways agents wait for them.
BG_HEAD = re.compile(rb'"type":\s*"(PLANNER_RESPONSE|GENERIC|SYSTEM_MESSAGE)"')
CREATED = b"Created the following subagent"
BG_STARTED = b"running as a background task"
BG_TASK = re.compile(r"running as a background task with task id:\s*(\S+)\s*\n\s*Task Description:\s*([^\n]*)")
AGENT_ID = re.compile(r'"conversationId":\s*"([\w-]{8,80})"')
REPORT = re.compile(rb"\[Message\] timestamp=(\S+) sender=([\w./:-]+)")
GONE_IDLE = re.compile(rb"Subagent ([\w-]{8,80}) has gone idle")
SUBAGENT_ERROR = re.compile(rb'The subagent [^\n]{1,200} with id \\?"([\w-]{8,80})\\?" encountered an error')
STILL_RUNNING = b"content=Background command is still running"
BG_FINISHED_MSG = re.compile(rb'Task id \\?"([^"\\]+)\\?" finished with result:')
# The result a finished background command reports (decoded): what says it failed, the first match is shown
# (hooks/progress.py). An `echo "EXIT=$?"` after a failing build still exits 0, so its words count too.
BG_RESULT = re.compile(r'Task id "?([^"\s]+)"? finished with result:(.*?)(?=\[Message\] timestamp=|</SYSTEM_MESSAGE>|\Z)', re.S)
BG_FAILED = re.compile(r"\bBUILD FAILED\b|^FAILURE: |Traceback \(most recent call last\)|\b[1-9]\d* (?:failed|failing|errors?)\b|"
                       r"^FAILED\b|\bFAILED \(|npm ERR!|^error(?:\[\w+\])?: |^E\s{2,}\w", re.M)
BG_EXIT = re.compile(r"(?:exited with code|exit code:?|\bEXIT\w*=)\s*[1-9]\d*")  # when nothing above says why
PIGGY_POLLS = (3, 10)     # steps that check on running work alongside other calls: polling hints at these counts
STATUS_CHECK = re.compile(r"\b(status|progress|done yet|are you done|finished yet|ready yet|answer now|provide (your|the) "
                          r"(answer|result|report)|how(?:'s| is) it going)\b|как\s+(дела|успехи|прогресс)|готов[оа]?\s*\?|"
                          r"\bстатус|\bну\s+что\b", re.I)
WAIT_CMD = re.compile(r"^\s*(sleep\s+\d|timeout\s+(/t\s+)?\d|start-sleep\b|echo\s+[\"']?(waiting|wait\b|жду))", re.I)
# A delegated agent talking to Claude Code (claude-code/skills/antigravity/scripts/delegate.py): its questions, and
# Claude Code's answers, messages and follow-ups, which arrive as agent messages from "system" (delegate.py ANSWER,
# MESSAGE and FOLLOW_UP start like this).
CLAUDE_ASK = re.compile(r"delegate\.py[\"']?\s+ask\b")
CLAUDE_SAYS = re.compile(rb"sender=system\b[^\n]{0,200}?content=(?:Claude Code answers your question|Claude Code says:|"
                         rb"Follow-up from Claude Code:)")
# Antigravity's native swarm: the coordinator's `swarm` tool adds members (action add: name, brief, n or tasks for a
# numbered group <name>-1..<name>-k) and kills them (action kill: name, a group by its prefix); the members talk on a
# message board (post_message, read_board, swarm_wait), not through [Message] reports.
SWARM_TOOLS = ("swarm", "post_message", "read_board", "swarm_wait")
SWARM_GROUP_MAX = 64      # copies of one numbered group tracked
POLL_TIMER = 180          # schedule timers shorter than this, set while work is running, are polling
BG_KEEP = 60              # finished entries kept in the state
# How the steps go (hooks/speed.py).
TOKENS = re.compile(rb'"(input_tokens|cache_read_tokens)":\s*(\d+)')
CREATED_AT = re.compile(rb'"created_at":\s*"([0-9T:.+Z -]{10,40})"')
EXIT = re.compile(r"Command exited with code (-?\d+)")
SANDBOX_NOTE = "There were sandbox errors"
SANDBOX_DENIED = re.compile(r"operation not permitted|permission denied|not permitted|could not resolve host|"
                            r"nodename nor servname|network is unreachable|read-only file system|sandbox-exec", re.I)
NO_MATCH = re.compile(r"^(rg|grep|egrep|fgrep|git\s+grep|test|\[|diff|cmp|which|command\s+-v|pgrep)\b")
SHOWING = re.compile(rb"Showing lines (\d+)-(\d+) of (\d+)")
BIG_OUTPUT = 30_000       # chars of one tool result that are worth a word (~8k tokens)
BIG_LINES = 600           # a whole file this long, read without a range
SBX_WINDOW = 4            # commands within which a retry outside the sandbox counts as the fix
SUBCOMMANDS = {"git", "npm", "pnpm", "yarn", "bun", "go", "cargo", "make", "pip", "pip3", "uv", "poetry", "brew", "gh",
               "swift", "flutter", "dotnet", "composer", "bundle", "rails", "kubectl", "terraform"}
SCRIPTED = re.compile(r"(python[\d.]*|py|node|deno|ruby|perl|php|bash|sh|zsh|pwsh|powershell|osascript|npx|uvx)")
ABSOLUTE = re.compile(r"[/\\~]|[A-Za-z]:[\\/]")  # /usr/bin/x, ~/bin/x, C:\x, C:/x, \\server\share — on any OS
SEP = re.compile(r"[\\/]")
# What the agent learns from (hooks/memory_hints.py): memory files it writes, commands on servers and deploys, and runs
# of failing commands that finally worked.
MEMORY_PATH = re.compile(r"[/\\]\.gemini[/\\]antigravity-plus[/\\]memory[/\\](?:personal|projects[/\\][^/\\]+)[/\\][^/\\]+\.md$"
                         r"|[/\\]\.claude[/\\]projects[/\\][^/\\]+[/\\]memory[/\\][^/\\]+\.md$", re.I)
REMOTE = re.compile(r"(^|[/\s])(ssh|scp|rsync|sftp|mosh|sshpass|kubectl|helm|ansible\S*|terraform|pulumi|gcloud|doctl|hcloud|"
                    r"flyctl|fly|vercel|netlify|heroku|wrangler|railway|aws|az|kamal)(\s|$)|deploy", re.I)
FIXED_WITHIN = 6          # commands after a run of failures within which a success counts as the way that works
LEARN_KEEP = 40
# Failing tests, builds, linters and CI checks are work in progress, not something about the environment to learn.
CHECK_RUN = re.compile(r"\b(pytest|unittest|tox|nox|jest|vitest|mocha|tsc|eslint|ruff|mypy|flake8|pylint|xcodebuild|"
                       r"swiftlint|phpunit|rspec|ctest)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|check|typecheck)\b"
                       r"|\b(go|cargo|swift|dotnet)\s+(test|build|vet|check)\b|\bmake\s+(test|check|build|lint)\b"
                       r"|(?<![\w-])ci\.py\b|\bgh\s+(run|pr\s+checks)\b|\bglab\s+ci\b", re.I)
REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
TAGS = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|[A-Z_]+_METADATA)>.*?</\1>", re.S)

# Standing instructions worth highlighting (negations, scope limits, "always/only", "until I say").
CONSTRAINT = re.compile(
    r"\bне\s+(надо|нужно|стоит|трогай|меняй|удаляй|ломай|пушь|пуш|коммить|деплой|заливай|запускай|ставь|используй|"
    r"делай|добавляй|переписывай|забудь)|\b(ничего\s+)?не\s+\w+(й|йте|ь|ьте)\b|\bникогда\b|\bвсегда\b|"
    r"\bтолько\b|\bбез\s+\w+|\bпока\s+(что\s+)?(не|без)\b|\bпока\s+я\s+не\b|\bсамое\s+главное\b|\bважно\b|"
    r"\bобязательно\b|\bнельзя\b|\bзапрещ\w*|\bвс[её]\s+внутри\b|\bделай\s+вс[её]\b|"
    r"\b(don'?t|do\s+not|never|always|only|must|make\s+sure|without|until\s+i|no\s+more)\b", re.I)

_PUSH_VERB = r"((за)?(пуш\w*|деплои\w*|деплой\w*)|зале(й|йте)\b|зали(ть|вай)\w*|залив\w*|выкат\w*|опублику\w*|публику\w*)"
BANS = {
    "push": (
        re.compile(r"\bне\s+(надо\s+|нужно\s+|стоит\s+|будем\s+)?" + _PUSH_VERB +
                   r"|\bне\s+трога\w*\s+(сервер|прод)\w*|\bбез\s+(пуша|деплоя|выкатки)\b"
                   r"|\b(don'?t|do\s+not|never)\s+(git\s+)?(push|deploy|ship|publish|release)\w*"
                   r"|\bnot\s+(push|deploy)\w*\s+yet\b|\b(stay|keep\s+it|work)\s+local(ly)?\b", re.I),
        re.compile(r"^\W*(ну|теперь|давай|можно|ок(ей)?|го|всё|все|отлично|супер)?[\s,!]*" + _PUSH_VERB +
                   r"(\s*$|[\s,.!]+(и|на|в|сейчас|теперь|уже|всё|все|это|изменения)\b|[.!]+\s*$)"
                   r"|\b(можно|можешь|давай|разрешаю)\s+(за)?(пушить|пушнуть|деплоить|задеплоить|заливать|залить|"
                   r"выкатывать|выкатить|публиковать)\b|\bна\s+сервер\s+(за)?ле(й|вай)\b|^\s*/pr\b"
                   r"|^\W*(ok|okay|now|go\s+ahead( and)?)?[\s,]*(push|deploy|ship\s+it|publish)\b"
                   r"|\b(you\s+can|go\s+ahead\s+and|please)\s+(push|deploy)\b", re.I),
    ),
    "commit": (
        re.compile(r"\bне\s+(надо\s+|нужно\s+|стоит\s+)?(за)?коммит\w*|\bбез\s+коммит\w*"
                   r"|\b(don'?t|do\s+not|never)\s+commit\w*", re.I),
        re.compile(r"^\W*(ну|теперь|давай|можно|ок(ей)?)?[\s,]*(за)?коммит(ь|ьте|им)\b|^\s*/(commit|pr)\b"
                   r"|\b(можно|можешь|давай)\s+(за)?коммит\w*|^\W*(ok|now)?[\s,]*commit\b|\bgo\s+ahead\s+and\s+commit\b",
                   re.I),
    ),
}


def to_path(p: str) -> str:
    """A workspace path from a file:// URI or a plain path; file:///c%3A/proj on Windows → c:\\proj."""
    if not p.startswith("file://"):
        return os.path.expanduser(p)
    path = unquote(urlparse(p).path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if compat.WINDOWS else path


def transcript_path(payload: dict) -> Optional[str]:
    cands = []
    p = payload.get("transcriptPath")
    if isinstance(p, str) and p:
        p = to_path(p)
        if p.endswith("/transcript.jsonl"):
            cands.append(p[: -len("transcript.jsonl")] + "transcript_full.jsonl")
        cands.append(p)
    cid = payload.get("conversationId")
    if isinstance(cid, str) and re.fullmatch(r"[\w-]{8,80}", cid):
        brains = [os.path.join(d, "brain") for d in compat.data_dirs()]
        if BRAIN not in brains:
            brains.append(BRAIN)
        for brain in brains:  # the CLI may write only transcript.jsonl
            for name in ("transcript_full.jsonl", "transcript.jsonl"):
                cands.append(os.path.join(brain, cid, ".system_generated", "logs", name))
    return next((c for c in cands if os.path.isfile(c)), None)


def user_text(content: str) -> str:
    m = REQUEST.search(content or "")
    text = m.group(1) if m else TAGS.sub("", content or "")
    return text.strip()[:MSG_LIMIT]


def _fresh(tp: str) -> dict:
    return {"transcript": tp, "offset": 0, "users": [], "checkpoints": [], "last_step": -1, "injected": {}}


def load(payload: dict, save: bool = True) -> Optional[dict]:
    """State with every user message so far: {"users": [{"step", "ts", "text"}], "checkpoints": [step, …],
    "last_step": int, "injected": {…}}. None when there's no transcript to read."""
    cid = payload.get("conversationId")
    if not isinstance(cid, str) or not re.fullmatch(r"[\w-]{8,80}", cid):
        return None
    tp = transcript_path(payload)
    if not tp:
        return None
    sp = os.path.join(STATE_DIR, cid + ".json")
    try:
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = _fresh(tp)
    if not isinstance(st, dict) or not all(k in st for k in ("offset", "users", "checkpoints", "last_step")):
        st = _fresh(tp)
    try:
        size = os.path.getsize(tp)
    except OSError:
        return st
    if st.get("transcript") != tp or st.get("offset", 0) > size:
        st = _fresh(tp)
    if size == st["offset"]:
        return st
    with open(tp, "rb") as f:
        f.seek(st["offset"])
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return st
    ws = next((os.path.realpath(to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), "")
    for raw in chunk[:end].split(b"\n"):
        sm = STEP.search(raw[:120])
        if sm:
            st["last_step"] = max(st["last_step"], int(sm.group(1)))
        bm = BG_HEAD.search(raw[:400])
        if bm:
            try:
                track_background(st, bm.group(1), raw)
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
            try:
                track_steps(st, bm.group(1), raw, ws)
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                pass
        hm = HEAD.search(raw[:400])
        if not hm or (hm.group(1) == b"PLANNER_RESPONSE" and b'"tool_calls"' in raw):
            continue  # agent steps matter only as the text the user answered
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        step = d.get("step_index", st["last_step"])
        if d.get("type") == "USER_INPUT":
            text = user_text(d.get("content") or "")
            if text:
                st["users"].append({"step": step, "ts": d.get("created_at", ""), "text": text,
                                    "prev_agent": st.get("last_agent", "")})
                st.setdefault("calls", {}).update(search_run=0, quiet=0)
        elif d.get("type") == "CHECKPOINT":
            st["checkpoints"].append(step)
        elif d.get("type") == "PLANNER_RESPONSE" and (d.get("content") or "").strip():
            st["last_agent"] = d["content"][-500:]
    st["offset"] += end + 1
    if save:
        store(cid, st)
    return st


def track_background(st: dict, kind: bytes, raw: bytes) -> None:
    """Keep st["bg"] = {id: {"k": "agent"|"cmd"|"timer"|"swarm", "t": type, "r": role or description, "s": step,
    "ts": started, "st": "run"|"done"|"killed", "end": ts, "fail": the line of a finished command's result that says it
    failed}} (a swarm member's id is `swarm:<name>`) and st["calls"] =
    {"search_run": web-search-only steps in a row, "poll_step": the last step that only checked on running work (or
    the PIGGY_POLLS-th that checked on it beside other calls: "piggy" counts those while work runs),
    "swarm_ts": when the agent last used the swarm or its board}."""
    bg = st.setdefault("bg", {})
    calls = st.setdefault("calls", {})
    if kind == b"SYSTEM_MESSAGE":
        if CLAUDE_SAYS.search(raw):
            sm = STEP.search(raw[:120])
            st["claude_said"] = int(sm.group(1)) if sm else st["last_step"]
        for m in REPORT.finditer(raw):
            if STILL_RUNNING in raw[m.end():m.end() + 160]:
                continue  # Antigravity's «still running after 300s» notice: not a report, the command goes on
            it = bg.get(m.group(2).decode())
            if it and it.get("st") == "run":
                it.update(st="done", end=m.group(1).decode())
        for m in GONE_IDLE.finditer(raw):
            it = bg.get(m.group(1).decode())
            if it and it.get("st") == "run":
                it.update(st="done", end="")
        for m in SUBAGENT_ERROR.finditer(raw):
            it = bg.get(m.group(1).decode())
            if it and it.get("st") == "run":
                it.update(st="killed", end="")
        for m in BG_FINISHED_MSG.finditer(raw):
            it = bg.get(m.group(1).decode("utf-8", errors="replace"))
            if it and it.get("st") == "run":
                it.update(st="done", end="")
        if BG_FINISHED_MSG.search(raw):
            try:
                text = json.loads(raw).get("content") or ""
            except ValueError:
                text = ""
            for r in BG_RESULT.finditer(text):
                it = bg.get(r.group(1))
                if not it or it.get("k") != "cmd" or it.get("res"):
                    continue  # the first result counts: a later copy of it says nothing new
                it["res"] = True
                out = r.group(2)[-20000:]
                f = BG_FAILED.search(out) or BG_EXIT.search(out)
                if f:
                    it["fail"] = out[f.start():].split("\n", 1)[0].strip()[:100] or "failed"
        return
    if kind == b"GENERIC":
        if CREATED not in raw and BG_STARTED not in raw:
            return
        d = json.loads(raw)
        content = d.get("content") or ""
        head = content[:300]  # the tool's own result starts with it; deeper down it's a file or log being read
        base = {"s": d.get("step_index", st["last_step"]), "ts": d.get("created_at", ""), "st": "run"}
        if CREATED.decode() in head:
            pending = st.get("bg_pending") or []
            for cid in AGENT_ID.findall(content[:30000]):
                spec = pending.pop(0) if pending else {}
                bg[cid] = dict(base, k="agent", t=spec.get("t", ""), r=spec.get("r", ""))
            st["bg_pending"] = pending
        m = BG_TASK.search(content[:800]) if BG_STARTED.decode() in head else None
        if m:
            desc = m.group(2).strip()
            bg[m.group(1)] = dict(base, k="timer" if desc.startswith("Timer:") else "cmd", t="", r=desc[:120])
        _prune_bg(bg)
        return
    if b'"tool_calls"' not in raw:
        return
    d = json.loads(raw)
    tcs = [c for c in d.get("tool_calls") or [] if isinstance(c, dict)]
    step = d.get("step_index", st["last_step"])
    names = [compat.tool(c.get("name")) for c in tcs]
    pending: List[dict] = []
    working = any(it.get("st") == "run" and it.get("k") in ("agent", "cmd", "swarm") for it in bg.values())
    polling = bool(tcs) and working
    checks = False  # a status check on running work, maybe beside real work (seen live: one in each of 36 steps)
    for name, c in zip(names, tcs):
        raw_args = c.get("args")
        a: dict = raw_args if isinstance(raw_args, dict) else {}
        action = str(a.get("Action") or a.get("action") or "list").lower()
        if name in ("manage_task", "manage_subagents") and action in ("list", "status"):
            checks = True
        if name in SWARM_TOOLS:
            calls["swarm_ts"] = d.get("created_at", "")  # the board is how a coordinator waits: never polling
            if name == "swarm":
                track_swarm(bg, a or {}, step, d.get("created_at", ""))
        if name == "invoke_subagent":
            specs = a.get("Subagents") if isinstance(a.get("Subagents"), list) else [a]
            pending += [{"t": str(s.get("TypeName") or ""), "r": str(s.get("Role") or "")[:80]}
                        for s in specs if isinstance(s, dict)]
            polling = False
        elif name == "send_message":
            it = bg.get(str(a.get("Recipient") or ""))
            if it and it.get("st") == "run" and STATUS_CHECK.search(str(a.get("Message") or "")):
                continue  # «are you done yet?» is polling
            polling = False
            if it and it.get("k") == "agent" and it.get("st") != "run":
                it.update(st="run", s=step, ts=d.get("created_at", ""), end="")  # follow-up work for it
        elif name == "manage_subagents":
            if action in ("kill", "kill_all"):
                polling = False
                ids = a.get("ConversationIds") if a.get("ConversationIds") is not None else a.get("ConversationId")
                ids = [str(i) for i in ids] if isinstance(ids, list) else [str(ids or "")]
                for cid, it in bg.items():
                    if it.get("k") == "agent" and it.get("st") == "run" and (action == "kill_all" or cid in ids):
                        it["st"] = "killed"
        elif name == "manage_task":
            if action == "kill":
                polling = False
                it = bg.get(str(a.get("TaskId") or ""))
                if it:
                    it["st"] = "killed"
            elif action not in ("list", "status"):
                polling = False
        elif name == "schedule":
            try:
                secs = float(a.get("DurationSeconds") or 0)
            except (TypeError, ValueError):
                secs = 0.0
            if not 0 < secs < POLL_TIMER:
                polling = False
        elif name == "read_file":
            path = str(a.get("Path") or a.get("AbsolutePath") or "").replace("\\", "/")
            if not (any(f"/{d}/brain/" in path for d in (compat.DESKTOP_DATA, compat.CLI_DATA))
                    and "transcript" in path.rsplit("/", 1)[-1]):
                polling = False  # reading a subagent's transcript to see how it's doing is polling
        elif not (name == "run_command" and WAIT_CMD.search(str(a.get("CommandLine") or ""))):
            polling = False
        if name == "run_command" and CLAUDE_ASK.search(str(a.get("CommandLine") or "")):
            st["claude_ask"] = step  # it asked Claude Code; the answer arrives as a message
    st["bg_pending"] = pending
    calls["piggy"] = calls.get("piggy", 0) + 1 if checks and working else (calls.get("piggy", 0) if working else 0)
    if polling or (checks and calls["piggy"] in PIGGY_POLLS):
        calls["poll_step"] = step
    calls["search_run"] = calls.get("search_run", 0) + 1 if tcs and all(n == "search_web" for n in names) else 0


def track_swarm(bg: dict, a: dict, step: int, ts: str) -> None:
    """A `swarm` tool call: `add` starts members (bg["swarm:<name>"], or <name>-1..<name>-k for `n` copies or a list of
    `tasks`), `kill` ends a member or a whole numbered group. Members stay until killed: they wait on the board between
    tasks, so the coordinator's own board activity (calls["swarm_ts"]) says whether the swarm is still alive."""
    action = str(a.get("action") or a.get("Action") or "").strip().lower()
    name = str(a.get("name") or a.get("Name") or "").strip().lstrip("@")
    if not name or name in ("all", "coordinator"):
        return
    if action == "add":
        tasks = a.get("tasks")
        try:
            k = len(tasks) if isinstance(tasks, list) and tasks else int(a.get("n") or 0)
        except (TypeError, ValueError):
            k = 0
        brief = " ".join(str(a.get("brief") or "").split())
        members = [f"{name}-{i}" for i in range(1, min(k, SWARM_GROUP_MAX) + 1)] if k > 1 else [name]
        for i, member in enumerate(members):
            task = tasks[i] if isinstance(tasks, list) and i < len(tasks) else ""
            role = " ".join(str(task).split()) if isinstance(task, str) and task else brief
            bg["swarm:" + member] = {"k": "swarm", "t": "swarm", "n": member, "r": role[:120], "s": step, "ts": ts,
                                     "st": "run"}
    elif action == "kill":
        group = re.compile(re.escape("swarm:" + name) + r"(-\d+)?$")
        for key, it in bg.items():
            if it.get("k") == "swarm" and it.get("st") == "run" and group.match(key):
                it.update(st="killed", end=ts)
    _prune_bg(bg)


def command_key(cmdline: str, ws: str = "") -> str:
    """What a command runs, stable across retries: `./deploy/ssh.sh`, `docker`, `git push`, `python3 -m pytest`,
    `python3 ./tools/x.py`. A project script is shown relative to the workspace, a system binary by its name. On
    Windows a path may use backslashes, forward slashes or another letter case and still gets the same key."""
    s = re.sub(r"^\s*(cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)\s*)+", "", str(cmdline or ""))
    s = re.sub(r"^\s*((sudo|env|time|nohup|command|exec)\s+|[A-Za-z_]\w*=(\"[^\"]*\"|'[^']*'|\S*)\s+)+", "", s)
    toks = _words(s[:400])
    if not toks:
        return ""

    def place(path: str) -> str:
        if not ABSOLUTE.match(path):
            return path.replace("\\", "/") if os.name == "nt" else path
        if ws and path[0] != "~":
            full, root = os.path.normpath(path), os.path.normpath(ws)
            if os.path.normcase(full).startswith(os.path.normcase(root).rstrip("/\\") + os.sep):
                return "./" + os.path.relpath(full, root).replace("\\", "/")
        return SEP.split(path.rstrip("/\\"))[-1]

    prog = re.sub(r"\.exe$", "", place(toks[0]), flags=re.I)
    name = SEP.split(prog)[-1].lower()
    rest = toks[1:5]
    if name in SUBCOMMANDS:
        words = [t for t in rest if not t.startswith("-")]
        sub = " ".join(words[:2]) if words[:1] == ["run"] and name in ("npm", "pnpm", "yarn", "bun") else "".join(words[:1])
        return f"{prog} {sub}".strip()
    if SCRIPTED.fullmatch(name):
        if rest[:1] == ["-m"] and len(rest) > 1:
            return f"{prog} -m {rest[1]}"
        if rest[:1] in (["-c"], ["-e"]):
            return f"{prog} {rest[0]}"
        script = next((t for t in rest if not t.startswith("-")), "")
        return f"{prog} {place(script)}".strip()
    return prog


def _words(s: str, limit: int = 6) -> List[str]:
    """The first words of a command line. A POSIX shell treats a backslash as an escape; on Windows (PowerShell, cmd)
    it separates the parts of a path, and shlex keeps the quotes around a word, so they are stripped here."""
    posix = os.name != "nt"
    lex = shlex.shlex(s, posix=posix)
    lex.whitespace_split, lex.commenters = True, ""
    words: List[str] = []
    try:
        for w in lex:
            words.append(w[1:-1] if not posix and len(w) > 1 and w[0] == w[-1] and w[0] in "\"'" else w)
            if len(words) == limit:
                break
    except ValueError:  # an unclosed quote further on: the words before it are enough
        pass
    return words or [w.strip("\"'") for w in s.split()[:limit]]


SEARCH_PROG = re.compile(r"^(?:rg|grep|egrep|ag|git\s+grep)\b")
SEARCH_OPTS_WITH_VALUE = {"-g", "--glob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C", "-m", "--max-count",
                          "--include", "--exclude", "--exclude-dir", "-f", "--file", "-d", "--max-depth", "--color",
                          "--colors", "-M", "--max-columns", "-j", "--threads", "--sort", "--sortr"}
TEXT_TARGET = re.compile(r"\.(log|txt|md|json|ya?ml|toml|ini|cfg|conf|csv|html?|xml|lock)$|(^|/)(logs?|tmp)(/|$)", re.I)
NOT_SYMBOL = {"TODO", "FIXME", "XXX", "NOTE", "HACK", "ERROR", "Error", "WARNING", "DEBUG", "INFO", "import", "from",
              "def", "class", "func", "fun", "function", "return", "self", "this", "true", "false", "None", "null",
              "http", "https", "localhost"}


def symbol_search(cmd: str) -> List[str]:
    """Identifiers an rg/grep/git grep looked for in code — «rg -n "OrderRepository" src/», «grep -rn 'def charge'
    api» — the searches a language server answers exactly (skills/lsp). [] for text, logs, piped output."""
    names: List[str] = []
    for part in re.split(r"\s*(?:&&|;|\|\|)\s*", cmd.strip()):
        part = re.sub(r"^cd\s+\S+\s*$", "", part).split(" | ")[0].strip()
        if not SEARCH_PROG.match(part):
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            continue
        tokens = tokens[2:] if tokens[:2] == ["git", "grep"] else tokens[1:]
        pattern, targets, i = None, [], 0
        while i < len(tokens):
            t = tokens[i]
            if t in ("-e", "--regexp") and i + 1 < len(tokens):
                pattern = pattern or tokens[i + 1]
                i += 2
                continue
            if t in SEARCH_OPTS_WITH_VALUE:
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            if pattern is None:
                pattern = t
            else:
                targets.append(t)
            i += 1
        if not pattern or any(TEXT_TARGET.search(x) for x in targets):
            continue
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern.replace("\\b", " ").replace("\\s", " ")):
            looks = (re.search(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", w) or ("_" in w.strip("_"))
                     or re.search(r"\b(def|class|func|fun|function|struct|protocol|interface)\s+" + re.escape(w), pattern))
            if looks and w not in NOT_SYMBOL and w not in names:
                names.append(w)
    return names[:6]


def track_steps(st: dict, kind: bytes, raw: bytes, ws: str = "") -> None:
    """Keep st["calls"] up to date with how the steps go (hooks/speed.py):
    ctx — tokens the latest model call carried (input + cache reads); n_calls / ctx_at — how many model calls were seen
    and the latest one's created_at (hooks/context_window.py reads that call's window by them; n_partial — counted
    from the middle of the conversation); fail_run / fail_last /
    fail_step — commands or edits failing in a row (a success that changes something resets it; a grep without matches
    isn't a failure);
    sbx — {command key: index} of commands that failed inside the terminal sandbox; sbx_fail — the latest such failure;
    sbx_new — keys that then worked with BypassSandbox (to remember for the project); sbx_ok — keys that worked inside
    the sandbox; big — the latest very large tool result (a whole long file, a huge output). For hooks/memory_hints.py:
    remote — [{k: command key, s: step}] of commands on servers and deploys; loop — the current run of 3+ failing
    commands; fixed — [{what, n, s}] such runs that a success soon ended; quiet / quiet_from — tool-calling steps in a
    row without a word to the user, and the first of them (hooks/progress.py); mem_any / mem_saved — the latest step that
    wrote into a memory folder / wrote a memory (not a handoff note); mem_files — the memories written here."""
    calls = st.setdefault("calls", {})
    if kind == b"PLANNER_RESPONSE":
        tokens = dict((k.decode(), int(v)) for k, v in TOKENS.findall(raw[:600] + raw[-600:]))
        if tokens:
            if "n_calls" not in calls and calls.get("ctx"):
                calls["n_partial"] = True  # counted from the middle: the conversation began under an older version
            calls["ctx"] = tokens.get("input_tokens", 0) + tokens.get("cache_read_tokens", 0)
            calls["n_calls"] = int(calls.get("n_calls") or 0) + 1
            head = raw[:600]
            cut = head.find(b'"content"')
            m = CREATED_AT.search(head[:cut] if cut >= 0 else head)
            if m:
                calls["ctx_at"] = m.group(1).decode("ascii", "replace")
        if b'"tool_calls"' not in raw:
            st["calls_pending"] = []
            calls["quiet"] = 0  # a message that ends the turn: the user heard from it
            return
        d = json.loads(raw)
        if d.get("tool_calls"):
            if (d.get("content") or "").strip():
                calls["quiet"] = 0  # a line for the user beside its tool calls
            else:
                calls["quiet"] = calls.get("quiet", 0) + 1  # tool calls without a word (hooks/progress.py)
                if calls["quiet"] == 1:
                    calls["quiet_from"] = d.get("step_index", st["last_step"])
        else:
            calls["quiet"] = 0
        pending = []
        for c in d.get("tool_calls") or []:
            a = c.get("args") if isinstance(c, dict) and isinstance(c.get("args"), dict) else {}
            entry = {"n": compat.tool(c.get("name")) if isinstance(c, dict) else ""}
            if entry["n"] == "run_command":
                entry.update(cmd=str(a.get("CommandLine") or "")[:400], bp=bool(a.get("BypassSandbox")))
            elif entry["n"] == "read_file":
                entry.update(path=str(a.get("Path") or a.get("AbsolutePath") or ""), rng=bool(a.get("StartLine") or a.get("EndLine")))
            elif entry["n"] in ("write_file", "edit_file"):
                entry.update(path=str(a.get("Path") or a.get("TargetFile") or "")[:400])
            pending.append(entry)
        st["calls_pending"] = pending
        return
    if kind != b"GENERIC":
        return
    pending = st.get("calls_pending") or []
    call = pending.pop(0) if pending else {}
    step = st["last_step"]
    name = call.get("n")
    if name == "run_command" and b"Command exited with code" in raw[:2000]:
        content = json.loads(raw).get("content") or ""
        m = EXIT.search(content[:800])
        if not m:
            return
        cmd, code = call.get("cmd", ""), int(m.group(1))
        key = command_key(cmd, ws)
        found = symbol_search(cmd) if code in (0, 1) else []
        if found:  # rg for a symbol: the language server would answer it exactly (hooks/instructions.py → lsp)
            sym = calls.setdefault("sym", {"n": 0})
            sym.update(n=sym.get("n", 0) + 1, step=step, names=(found + [x for x in sym.get("names", []) if x not in found])[:6])
        calls["n_cmd"] = n = calls.get("n_cmd", 0) + 1
        sbx = calls.setdefault("sbx", {})
        out = content[m.end():].split("Command output:", 1)[-1]  # the command's own output, not Antigravity's note
        if key and REMOTE.search(key):
            _learned(calls, "remote", {"k": key, "s": step})
        loop = calls.get("loop")
        if code == 0:
            if loop and n - loop.get("at", 0) <= FIXED_WITHIN:
                _learned(calls, "fixed", {"what": loop.get("what", ""), "n": loop.get("n", 0), "s": step})
            calls.pop("loop", None)
            calls["fail_run"] = 0
            if key and call.get("bp") and key in sbx and n - sbx[key] <= SBX_WINDOW:
                calls.setdefault("sbx_new", []).append({"key": key, "cmd": cmd[:160]})
                sbx.pop(key, None)
            elif key and not call.get("bp"):
                ok = calls.setdefault("sbx_ok", [])
                if key not in ok:
                    ok.append(key)
                    del ok[:-20]
        elif SANDBOX_NOTE in content[:1500] and not call.get("bp") and SANDBOX_DENIED.search(out[:6000]):
            if key:
                sbx[key] = n
                calls["sbx_fail"] = {"key": key, "step": step}
        elif not (code == 1 and NO_MATCH.match(cmd.strip()) and len(out.strip()) < 300):
            _failed(calls, cmd[:120], step)
            if calls["fail_run"] >= 3 and not CHECK_RUN.search(cmd[:300]):  # a success soon after: it found what works
                calls["loop"] = {"what": cmd[:120], "n": calls["fail_run"], "at": n}
        if len(content) > BIG_OUTPUT:
            calls["big"] = {"step": step, "what": f"the output of `{cmd[:60]}`", "chars": len(content)}
    elif name == "read_file":
        m = SHOWING.search(raw[:4000])
        if m and int(m.group(1)) == 1 and m.group(2) == m.group(3) and not call.get("rng") and int(m.group(3)) >= BIG_LINES:
            calls["big"] = {"step": step, "what": f"the whole {os.path.basename(call.get('path', '')) or 'file'} "
                                                  f"({int(m.group(3))} lines)", "chars": len(raw)}
        elif len(raw) > BIG_OUTPUT and not call.get("rng"):  # a range was a deliberate choice
            calls["big"] = {"step": step, "what": f"{len(raw) // 1000}k characters of "
                                                  f"{os.path.basename(call.get('path', '')) or 'a file'}", "chars": len(raw)}
    elif name in ("edit_file", "write_file"):
        head = raw[:1500]
        if b"Encountered error" in head or b"target content not found" in head or b'"status":"ERROR"' in head:
            _failed(calls, f"{name} failed", step)
        else:
            calls["fail_run"] = 0  # a change went in: progress, not a loop
            path = call.get("path", "")
            name = SEP.split(path)[-1]
            if MEMORY_PATH.search(path) and name != "MEMORY.md":
                calls["mem_any"] = step  # handoff notes live there too: they count as a write, not as a memory
                if not name.startswith("handoff-"):
                    calls["mem_saved"] = step
                    files = calls.setdefault("mem_files", [])
                    if name not in files:
                        files.append(name)
                        del files[:-30]


def _learned(calls: dict, kind: str, item: dict) -> None:
    items = calls.setdefault(kind, [])
    items.append(item)
    del items[:-LEARN_KEEP]


def _failed(calls: dict, what: str, step: int) -> None:
    """One more failure in a row; a new run of failures gets a new number (hooks/speed.py hints once per level)."""
    if not calls.get("fail_run"):
        calls["fail_id"] = calls.get("fail_id", 0) + 1
    calls["fail_run"] = calls.get("fail_run", 0) + 1
    calls["fail_last"] = what
    calls["fail_step"] = step


def _prune_bg(bg: dict) -> None:
    """Keep running entries and the BG_KEEP most recent finished ones."""
    done = sorted((it.get("s", 0), k) for k, it in bg.items() if it.get("st") != "run")
    for _s, k in done[: max(0, len(done) - BG_KEEP)]:
        bg.pop(k, None)


def store(cid: str, st: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        sp = os.path.join(STATE_DIR, cid + ".json")
        tmp = f"{sp}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        compat.replace(tmp, sp)
    except OSError:
        pass


def prune_states() -> None:
    """Drop state files of conversations untouched for STATE_TTL_DAYS (called from SessionStart)."""
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    try:
        for e in os.scandir(STATE_DIR):
            if e.name.endswith(".json") and e.stat().st_mtime < cutoff:
                os.remove(e.path)
    except OSError:
        pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:300]


# The first line of a task Claude Code hands over through its skill `antigravity` (claude-code/skills/antigravity,
# scripts/delegate.py writes it). Its follow-ups arrive as agent messages, not as user messages.
DELEGATED_MARK = "[Delegated from Claude Code]"


def delegated(st: Optional[dict], payload: Optional[dict] = None) -> bool:
    """A task Claude Code handed over: every user message so far is its brief, so nobody watches the conversation —
    no setup, review or memory nudges. A person who writes into it takes it over."""
    texts = [u.get("text", "") for u in (st or {}).get("users") or [] if isinstance(u, dict)]
    if not texts and payload:
        cur = payload.get("lastUserInput")
        texts = [user_text(cur)] if isinstance(cur, str) and cur.strip() else []
    return bool(texts) and all(t.lstrip().startswith(DELEGATED_MARK) for t in texts)


def waiting_for_claude(st: Optional[dict]) -> bool:
    """A delegated agent asked Claude Code something (`delegate.py ask`) and no answer has come since: ending its
    turn to wait is right — the answer wakes it up."""
    return int((st or {}).get("claude_ask", -1)) > int((st or {}).get("claude_said", -1))


def with_current(st: Optional[dict], payload: dict) -> List[dict]:
    """The user's messages, plus the payload's lastUserInput when the transcript hasn't recorded it yet."""
    users = list(st["users"]) if st else []
    cur = payload.get("lastUserInput")
    if isinstance(cur, str) and cur.strip():
        text = user_text(cur)
        if not users or _norm(users[-1]["text"]) != _norm(text):
            users.append({"step": st["last_step"] + 1 if st else 0, "ts": "", "text": text})
    return users


# Full access: one grant from the user lets the agent run every plugin command without asking (see rules "Full access").
# Only the user's own messages count — never text from tools, pages or files.
GRANT = re.compile(r"\b(даю|дам|выдаю|разрешаю|включаю|включи|дай|даём|даем|предоставляю)\s+(тебе\s+|вам\s+)?полн\w*\s+доступ"
                   r"|полн\w*\s+доступ\w*\s*[—:-]?\s*(да|разрешаю|даю|ок|включ)"
                   r"|\b(grant|give)\s+(you\s+)?full\s+access|\bfull\s+access\s*[:—-]?\s*(granted|on|yes)\b"
                   r"|^\s*/full-access(\s+on)?\s*$", re.I)
AFFIRM = re.compile(r"^\W*(да|ага|ок(ей)?|согласен|согласна|разрешаю|даю|давай|конечно|можно|yes|ok|sure|go ahead)\b", re.I)
ASKED = re.compile(r"полн\w*\s+доступ|full\s+access", re.I)
REVOKE = re.compile(r"(забира\w*|отзыва\w*|отзову|отозв\w*|отключ\w*|выключ\w*|убер\w*|сними)\s+(у\s+тебя\s+)?полн\w*\s+доступ"
                    r"|без\s+полного\s+доступа|верни\s+(вопросы|подтверждени\w*)|спрашивай\s+(снова|опять|разрешени\w*)"
                    r"|\brevoke\b.*\bfull\s+access|\bfull\s+access\b\s*[:—-]?\s*off\b|^\s*/full-access\s+off\s*$", re.I)
PROJECT_SCOPE = re.compile(r"навсегда|всегда|для\s+(всего\s+|этого\s+)?проекта|во\s+всех\s+разговорах|for\s+this\s+project|\balways\b", re.I)


def access_events(users: List[dict]) -> List[dict]:
    """Grants and revocations of full access in the user's messages, in order."""
    out = []
    for i, u in enumerate(users, 1):
        text = u.get("text") or ""
        if REVOKE.search(text):
            out.append({"n": i, "on": False, "text": text})
        elif GRANT.search(text) or (len(text) <= 60 and AFFIRM.search(text) and ASKED.search(u.get("prev_agent") or "")
                                    and "?" in (u.get("prev_agent") or "")):
            out.append({"n": i, "on": True, "project": bool(PROJECT_SCOPE.search(text)), "text": text})
    return out


def full_access(payload: dict, st: Optional[dict], users: Optional[List[dict]] = None) -> Optional[dict]:
    """{"n", "text", "project": bool, "source"} when full access is on for this conversation, else None.
    Subagents inherit their parent's. A grant "for the project" is kept in settings (fullAccess) for its workspace."""
    if users is None:
        users = with_current(st, payload) if st and st["users"] else []
    ws = next((os.path.realpath(to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), None)
    events = access_events(users)
    if not users:  # a subagent: inherit from the conversation that started it
        parent = _parent(payload.get("conversationId"))
        pst = _state(parent) if parent else None
        if pst and pst["users"]:
            got = full_access({"conversationId": parent, "workspacePaths": payload.get("workspacePaths")}, pst)
            return dict(got, source="parent") if got else None
    if st is not None and events and events[-1]["n"] > st.get("access_applied", 0) and ws:
        last = events[-1]  # a new grant or revocation: keep the project-wide one in step
        st["access_applied"] = last["n"]
        _project_grant(ws, last["on"] and last.get("project"), drop=not last["on"])
    if events:
        last = events[-1]
        return dict(last, source="conversation") if last["on"] else None
    granted = _project_grant(ws, None) if ws else None
    return {"n": 0, "text": "", "project": True, "source": f"project since {granted}"} if granted else None


def _project_grant(ws: str, grant: Optional[bool], drop: bool = False) -> Optional[str]:
    """Read (grant=None), add (True) or remove (drop) the project-wide full-access grant in settings."""
    path = os.path.expanduser("~/.gemini/antigravity-plus/settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    grants = data.get("fullAccess") if isinstance(data.get("fullAccess"), dict) else {}
    if grant is None and not drop:
        return grants.get(ws)
    if grant:
        grants[ws] = time.strftime("%Y-%m-%d")
    elif drop:
        grants.pop(ws, None)
    else:
        return None
    data["fullAccess"] = grants
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        compat.replace(tmp, path)
    except OSError:
        pass
    return grants.get(ws)


# Bypass Mode: the user's own message switches it on or off for every conversation (settings "bypassMode"); the guard
# then approves commands and edits itself and stops only at system-level danger (hooks/guard.py, SYSTEM_RULES).
# Only messages the user typed count — not a brief from Claude Code, not tools, pages or files, not a subagent's task.
BYPASS_ON = re.compile(
    r"^\s*/bypass(\s+on)?\s*[.!]?\s*$"
    r"|(?<!не\s)\b(включи|включаю|вруби|активируй)\s+(мне\s+)?(режим\s+)?(bypass|байпас)"
    r"|(?<!не\s)\b(включи|включаю|вруби|активируй)\s+режим\s+без\s+(вопросов|подтверждений|разрешений)"
    r"|(?<!not\s)(?<!n't\s)\b(enable|turn\s+on|switch\s+on)\s+(the\s+)?bypass"
    r"|^\s*bypass(\s+mode)?\s*[:—-]?\s*(on|вкл\w*)\s*[.!]?\s*$", re.I)
BYPASS_OFF = re.compile(
    r"^\s*/bypass\s+off\s*[.!]?\s*$"
    r"|\b(выключи|выключаю|отключи|отключаю|выруби|деактивируй|убери)\s+(режим\s+)?(bypass|байпас)"
    r"|\b(выключи|отключи|убери)\s+режим\s+без\s+(вопросов|подтверждений|разрешений)"
    r"|\b(disable|turn\s+off|switch\s+off)\s+(the\s+)?bypass"
    r"|^\s*bypass(\s+mode)?\s*[:—-]?\s*(off|выкл\w*)\s*[.!]?\s*$"
    r"|верни\s+(вопросы|подтверждени\w*|запросы\s+разрешени\w*)", re.I)
SLASH_LINK = re.compile(r"\[(/[\w-]+)\]\(slash_command://[^)]*\)")  # a command picked from the menu
SETTINGS = os.path.expanduser("~/.gemini/antigravity-plus/settings.json")


def bypass_events(users: List[dict]) -> List[dict]:
    """Bypass Mode switches in the user's own messages, in order: [{"n", "on", "ts", "text"}]."""
    out = []
    for i, u in enumerate(users, 1):
        text = SLASH_LINK.sub(r"\1", u.get("text") or "")
        if text.lstrip().startswith(DELEGATED_MARK):
            continue  # Claude Code's brief, not the user's words
        on = False if BYPASS_OFF.search(text) else (True if BYPASS_ON.search(text) else None)
        if on is not None:
            out.append({"n": i, "on": on, "ts": u.get("ts") or "", "text": text})
    return out


def _epoch(ts: str) -> float:
    """Transcript time ("2026-11-04T10:00:00Z", fractions allowed) → seconds; 0 when it can't be read."""
    if not isinstance(ts, str):
        return 0.0  # a null or numeric ts in a damaged transcript
    try:
        t = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], ts.strip()).replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(t).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


def bypass_record() -> dict:
    """The saved switch: {"on": bool, "at": epoch of the message that set it, "since", "conversation", "n"} or {}."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            rec = (json.load(f) or {}).get("bypassMode")
    except (OSError, ValueError, AttributeError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _save_bypass(rec: dict) -> bool:
    """Write the switch unless a newer one is already saved (two conversations switching at once). True if written."""
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS + ".lock", "a+", encoding="utf-8") as lk:
            compat.lock(lk)
            try:
                try:
                    with open(SETTINGS, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                data = data if isinstance(data, dict) else {}
                old = data.get("bypassMode") if isinstance(data.get("bypassMode"), dict) else {}
                if float(old.get("at") or 0) >= rec["at"]:
                    return False
                data["bypassMode"] = rec
                tmp = f"{SETTINGS}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                compat.replace(tmp, SETTINGS)
                return True
            finally:
                compat.unlock(lk)
    except (OSError, ValueError, TypeError):
        return False


def bypass_mode(payload: dict, st: Optional[dict], users: Optional[List[dict]] = None) -> Optional[dict]:
    """The saved switch while Bypass Mode is on, else None. A switch in this conversation's user messages that is
    newer than the saved one is applied first — once (st["bypass_applied"]), so an old «/bypass» never undoes a later
    «/bypass off» from another conversation. Subagents have no user messages: they follow the saved switch."""
    if st is not None and st.get("users") and not delegated(st, payload):
        events = bypass_events(users if users is not None else with_current(st, payload))
        last = events[-1] if events else None
        if last and last["n"] > int(st.get("bypass_applied") or 0) and compat.runtime(payload) == "cli" \
                and getattr(compat, "agy_headless", lambda: None)() is not False:
            # `agy -p "…"` / `agy acp`: a program's prompt, not the user typing (like agentapi); and when it can't be
            # told, no switch either — never applied, later either
            st["bypass_applied"] = last["n"]
            if isinstance(payload.get("conversationId"), str):
                store(payload["conversationId"], st)
        elif last and last["n"] > int(st.get("bypass_applied") or 0):
            at = _epoch(last["ts"]) if last["ts"] else time.time()
            _save_bypass({"on": last["on"], "at": at, "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(at)),
                          "conversation": str(payload.get("conversationId") or ""), "n": last["n"]})
            st["bypass_applied"] = last["n"]
            if isinstance(payload.get("conversationId"), str):
                store(payload["conversationId"], st)
    rec = bypass_record()
    return rec if rec.get("on") is True else None


def _state(cid: str) -> Optional[dict]:
    try:
        with open(os.path.join(STATE_DIR, cid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parent(cid) -> str:
    if not isinstance(cid, str):
        return ""
    try:
        import sqlite3
    except ImportError:
        return ""
    for d in compat.data_dirs():
        db = os.path.join(d, "conversation_summaries.db")
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=1)
            try:
                row = con.execute("SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                                  (cid,)).fetchone()
            finally:
                con.close()
            if row and row[0]:
                return row[0]
        except Exception:
            continue
    return ""


def is_constraint(text: str) -> bool:
    return bool(CONSTRAINT.search(text))


def ui_notices_enabled() -> bool:
    """Whether inline user-facing UI badges (metadata.user_markdown) are enabled (default True)."""
    try:
        folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "skills", "memory", "scripts")
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import memory as memstore  # type: ignore[import-not-found]
        s = memstore.settings()
        if "uiNotices" in s:
            return bool(s["uiNotices"])
        with open(memstore.SETTINGS, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("uiNotices"), bool):
            return raw["uiNotices"]
    except Exception:
        pass
    return True


def active_bans(users: List[dict]) -> Dict[str, dict]:
    """{"push"|"commit": {"n": message number (1-based), "text": …}} for restrictions the user set and hasn't lifted."""
    active: Dict[str, dict] = {}
    for i, u in enumerate(users, 1):
        text = u.get("text") or ""
        for kind, (ban, lift) in BANS.items():
            if ban.search(text):
                active[kind] = {"n": i, "text": text}
            elif lift.search(text):
                active.pop(kind, None)
    return active
