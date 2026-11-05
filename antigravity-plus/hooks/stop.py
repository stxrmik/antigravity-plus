#!/usr/bin/env python3
"""Antigravity Plus — Stop hook: don't leave a task half-done.

When the agent ends its turn normally (terminationReason NO_TOOL_CALL) while the current section of the
conversation's task.md checklist still has open items ([ ] or [/]), the hook sends it back with the list: continue
if the items are part of what the user asked and nothing blocks you; otherwise end again, say why they are open
and mark them (e.g. «— ждёт вашего решения»), which this hook then leaves alone. It stays quiet when:
  * the turn ended for another reason (cancel, error, limits);
  * the agent's last message asks the user something or waits for a decision, approval or review;
  * task.md wasn't updated since the user's last message (an old checklist), unless the user said «продолжай»;
  * it already sent the agent back for the same open items, or MAX_PER_TURN times in this turn;
  * it's a subagent, or finishTasks is off (`memory.py config finish-tasks off`).
It also sends the agent back when a file it edited in this conversation still has syntax errors (hooks/check_edit.py;
re-checked at this moment), with the same exceptions; that part works for subagents too. And when the main agent
changed REVIEW_MIN_FILES+ code files in this turn without running /review (or its subagents), it reminds it once to
review its change before reporting done (autoReview; `memory.py config auto-review off`), and when it changed code
without running tests, a build or the program afterwards, to verify it — a changed web page or styles by looking at
them with ui_check.py screenshots, with the exact command (autoVerify; `memory.py config auto-verify off`).
And once per conversation, when the main agent changed code in a project without a git repository or without
project rules — or offered that setup to the user instead of doing it — to set it up itself with /git-init and
/init (autoSetup; `memory.py config auto-setup off`).
While the main agent's own subagents or background commands still run (hooks/parallel.py) only the open items
count: it is sent back once with what still runs and the items not handed to a subagent («— explorer»), to do those
meanwhile; the review, verification and setup reminders wait until that work has reported back (parallelHints;
`memory.py config parallel-hints off` restores the old silence while background tasks run).

Contract: JSON on stdin (conversationId, transcriptPath, artifactDirectoryPath, terminationReason, fullyIdle,
executionNum, …) → {"decision": "continue", "reason": "…"} or {}. Never fails loudly. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import List, Optional, Set, Tuple

import compat
import conversation as conv
import parallel

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MAX_PER_TURN = 2
REVIEW_MIN_FILES = 3
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts", ".swift", ".kt", ".kts", ".java",
            ".go", ".rs", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".cs", ".sh", ".sql", ".vue",
            ".svelte", ".dart", ".scala", ".lua", ".ex", ".exs", ".html", ".htm", ".css", ".scss", ".sass", ".less"}
WEB_EXT = (".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte")
PAGE_EXT = (".html", ".htm", ".css", ".scss", ".sass", ".less")  # only looking checks these; components also have tests
UI_CHECK = os.path.join(PLUGIN, "skills", "ui-check", "scripts", "ui_check.py")
GIT_INIT = os.path.join(PLUGIN, "skills", "git-init", "scripts", "repo_init.py")
INIT_SKILL = os.path.join(PLUGIN, "skills", "init", "SKILL.md")
RULE_FILES = ("AGENTS.md", "GEMINI.md", "CLAUDE.md")
REVIEWERS = re.compile(r"(^|:)(code-reviewer|security-auditor)$")
REVIEW_SKILLS = ("/skills/review/SKILL.md", "/skills/security-review/SKILL.md")
STEP = re.compile(r'"step_index":\s*(\d+)')
SHOWN = 8                  # open items listed in the reminder
TAIL = 400_000             # bytes of the transcript searched for the agent's last message

SECTION = re.compile(r"^\s*#{1,6}\s")
CHECK = re.compile(r"^\s*[-*+]\s+\[(.)\]\s+(.+?)\s*$")
# Items the agent deliberately left open: blocked, waiting for the user, postponed, optional.
PARKED = re.compile(r"заблокир|ждёт|ждет|ожидает|отложен|позже|не сейчас|по желанию|опционал|пропущен|"
                    r"\b(blocked|waiting|postponed|deferred|later|optional|skipped|on hold)\b", re.I)
# The agent's last message hands the turn to the user: a question, a request to decide, approve or review.
WAITING = re.compile(
    r"\?|подтверд(и|ите)\b|одобр(и|ите)\b|согласу(й|йте)\b|разреш(и|ите)\b|просмотр(и|ите)\b|провер(ь|ьте)\b|"
    r"реш(и|ите)\b|выбер(и|ите)\b|уточн(и|ите)\b|да(й|йте) знать|скаж(и|ите)\b|напиш(и|ите)\b|ответь(те)?\b|"
    r"жду (вашего|твоего|ответа|решения)|нуж(ен|на|но|ны) (ваш|твой|ваше|твоё|ваша|твоя)|"
    r"\blet me know\b|\bplease (confirm|review|approve|check|decide|choose)\b|\bshould i\b|\bdo you want\b|"
    r"\bwould you like\b|\bwhich (one|option)\b", re.I)
# Project setup the agent does on its own, and phrasing that offers it to the user instead.
SETUP_TOPIC = re.compile(r"/init\b|/git-init|AGENTS\.md|git[ -]?init|репозитори|\brepositor(y|ies)\b", re.I)
OFFER = re.compile(r"\b(мож(но|ете)|могу|предлага|рекоменд|стоит|при желании|если (хотите|нужно)|совету|"
                   r"could|can|recommend|suggest|want me to)", re.I)
# A short final message that only announces an action («Инициализирую репозиторий…», «Сейчас запущу тесты»):
# the agent stopped instead of doing it.
ANNOUNCE = re.compile(r"^\W*(сейчас|теперь|далее|приступаю|начинаю|запускаю|запущу|инициализирую|создаю|создам|"
                      r"проверяю|проверю|устанавливаю|установлю|выполняю|выполню|делаю|сделаю|займусь|перехожу|"
                      r"let me|i'?ll|i will|now i|starting|running|creating|initializing|next,? i)\b", re.I)
ANNOUNCE_MAX = 300
CONTINUE = re.compile(r"^\W*(продолж|дальше|доделай|давай дальше|go on|continue|keep going|carry on)", re.I)
# A task.md item handed to a subagent: «Найти обработчики — explorer», «… (researcher)».
DELEGATED = re.compile(r"(?:[—–(]|\s-|→)\s*(explorer|researcher|worker|code-reviewer|security-auditor|debugger|"
                       r"test-writer|субагент\w*|subagent)\b", re.I)
# «Жду ответа researcher», «жду результатов от explorer»: waiting for its own subagents, not for the user.
AGENT_WAIT = re.compile(r"\bжд(у|ём|ем)\s+(ответ\w*|результат\w*|заверш\w*|отч[её]т\w*)(\s+(от|у))?\s+"
                        r"(?!(вас|тебя|ваш\w*|тво\w*)\b)[\w`«»\"'-]+", re.I)


def open_items(text: str) -> List[Tuple[str, str]]:
    """Open items of the current checklist: the last section of task.md that has any checklist items."""
    sections: List[List[str]] = [[]]
    for ln in text.splitlines():
        if SECTION.match(ln):
            sections.append([])
        sections[-1].append(ln)
    for sec in reversed(sections):
        items = [m for m in (CHECK.match(ln) for ln in sec) if m]
        if items:
            return [(m.group(1), m.group(2)) for m in items if m.group(1) in " /" and not PARKED.search(m.group(2))]
    return []


def last_answer(transcript: str) -> str:
    """The agent's latest message in this turn (the one it ended with)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - TAIL))
            lines = f.read().decode("utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    for raw in reversed(lines):
        if '"PLANNER_RESPONSE"' not in raw[:400]:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if (d.get("content") or "").strip():
            return d["content"]
        if d.get("tool_calls"):
            return ""
    return ""


def handing_over(answer: str) -> bool:
    """Does the agent's last message hand the turn to the user: a question, or a request to decide, approve, review?"""
    tail = re.sub(r"\[([^\]]*)\]\([^)]*\)", " ", answer[-700:])  # links: names and targets aren't requests
    tail = re.sub(r"`[^`]*`|\S*[/\\]\S*", " ", tail)               # inline code, paths, URLs
    return bool(WAITING.search(tail))


def sentences(answer: str) -> List[Tuple[str, str]]:
    """(raw, without links/code/paths) sentences of the end of the agent's message."""
    out = []
    for s in re.split(r"(?<=[.!?…])\s+|\n+", answer[-700:]):
        plain = re.sub(r"`[^`]*`|\S*[/\\]\S*", " ", re.sub(r"\[([^\]]*)\]\([^)]*\)", " ", s))
        out.append((s, plain))
    return out


def offers_setup(answer: str) -> bool:
    """The agent suggested the project setup instead of doing it («можно запустить /init», «создать репозиторий?»)."""
    return any(SETUP_TOPIC.search(raw) and (WAITING.search(plain) or OFFER.search(plain)) for raw, plain in sentences(answer))


def only_setup_questions(answer: str) -> bool:
    """Every question in the agent's message is about the project setup (so it isn't really waiting for the user)."""
    asks = [raw for raw, plain in sentences(answer) if WAITING.search(plain)]
    return bool(asks) and all(SETUP_TOPIC.search(raw) for raw in asks)


def announced_only(answer: str) -> bool:
    """The agent ended its turn with a short message that announces an action it didn't take."""
    text = answer.strip()
    if not text or len(text) > ANNOUNCE_MAX:
        return False
    last = sentences(text)[-1][1].strip() if sentences(text) else ""
    return bool(ANNOUNCE.search(text) or ANNOUNCE.search(last)) and not WAITING.search(re.sub(r"`[^`]*`", " ", text))


def setup_left(payload: dict, turn: dict, st: dict, answer: str) -> List[str]:
    """Project setup the main agent should have done on its own: "git" (no repository) and/or "init" (no project
    rules). Only after it changed code, or offered the setup to the user instead; each step once per conversation."""
    if not setting("autoSetup") or not (turn["files"] or offers_setup(answer)):
        return []
    ws = next((os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)), "")
    if not ws or not os.path.isdir(ws):
        return []
    try:
        sys.path.insert(0, os.path.dirname(GIT_INIT))
        import repo_init
        if repo_init.place_refusal(ws):
            return []  # the home folder, ~/Downloads and the like
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ws, capture_output=True, text=True, timeout=10)
        top = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return []
    done = set(st.get("setup") or []) | set(turn["setup"])
    left = []
    if "git" not in done and not top and not repo_init.refusal(ws):
        left.append("git")
    if "init" not in done and not any(os.path.isfile(os.path.join(r, n)) for r in {ws, top} - {""} for n in RULE_FILES):
        left.append("init")
    return left


def timestamp(value: str) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def setting(key: str) -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get(key, True))
    except Exception:
        return True


def task_items(payload: dict, st: dict) -> List[Tuple[str, str]]:
    """Open items of the current task.md checklist, if it belongs to what the user asked now."""
    art = payload.get("artifactDirectoryPath") or os.path.join(conv.BRAIN, payload["conversationId"])
    task = os.path.join(conv.to_path(art), "task.md")
    try:
        with open(task, encoding="utf-8", errors="replace") as f:
            items = open_items(f.read(200_000))
        updated = os.path.getmtime(task)
    except OSError:
        return []
    user = st["users"][-1]
    asked = timestamp(user.get("ts") or "")
    if items and asked and updated < asked and not CONTINUE.match(user["text"]):
        return []  # the checklist is from earlier work, not from what the user asked now
    return items


def broken_files(payload: dict) -> List[tuple]:
    """Files edited in this conversation that still have syntax errors (re-checked now)."""
    if not setting("checkEdits"):
        return []
    import check_edit
    return check_edit.unresolved_syntax(payload)


# Commands that check work: tests, builds, linters, or running the changed program itself.
VERIFY = re.compile(
    r"\b(pytest|unittest|tox|nox|mypy|ruff|flake8|pylint|jest|vitest|mocha|playwright|cypress|tsc|eslint|phpunit|rspec|"
    r"xcodebuild|swiftlint|gradlew?|mvn|ctest)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|check|typecheck|e2e)\b"
    r"|\b(go|cargo)\s+(test|build|vet|check|run)\b|\bswift\s+(test|build|run)\b|\bmake\s+(test|check|build|lint|all)\b"
    r"|\bdocker\s+(compose\s+)?(run|exec|build)\b|\bpython3?\s+(-m\s+\w+|[\w./-]+\.py)\b|\b(node|deno|ts-node|tsx)\s+[\w./-]+"
    r"|\b(bash|sh|zsh)\s+[\w./-]+\.sh\b|(^|[\s;&|])\.[/\\][\w./\\-]+|\bcurl\b.*\b(localhost|127\.0\.0\.1)\b|ui_check\.py"
    r"|\bpy\s+(-3\S*\s+)?(-m\s+\w+|[\w./\\:-]+\.py)\b|\bpython3?(\.exe)?\s+(-m\s+\w+|[\w./\\:-]+\.py)\b"
    r"|\b(pwsh|powershell)(\.exe)?\b.*\.ps1\b|\bdotnet\s+(test|build|run)\b|\binvoke-pester\b|\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b.*\b(localhost|127\.0\.0\.1)\b", re.I)
# Commands that show how a page looks: screenshots in a real browser (a syntax check or curl doesn't).
LOOK = re.compile(r"ui_check\.py|\b(playwright|cypress|puppeteer|selenium|webdriver|screencapture)\b|--screenshot", re.I)


def turn_changes(tp: str, since_step: int, payload: dict) -> dict:
    """What the agent did in this turn (steps after the user's last message): code files written, whether a review
    ran, the code changed after the last check (tests, build, running the program), and the web UI changed after
    the agent last looked at it (tests also cover components, but pages and styles only by looking)."""
    files: List[str] = []
    pending: List[str] = []
    unseen: List[str] = []
    setup: Set[str] = set()
    reviewed = False
    wss = [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    try:
        f = open(tp, encoding="utf-8", errors="replace")
    except OSError:
        return {"files": [], "reviewed": True, "pending": [], "unseen": [], "setup": []}
    with f:
        for raw in f:
            if '"tool_calls"' not in raw:
                continue
            m = STEP.search(raw[:120])
            if not m or int(m.group(1)) <= since_step:
                continue
            try:
                calls = json.loads(raw).get("tool_calls") or []
            except ValueError:
                continue
            for c in calls:
                args = c.get("args") or {}
                name = str(c.get("name") or "")
                if name == "invoke_subagent" and REVIEWERS.search(str(args.get("TypeName") or "")):
                    reviewed = True
                elif name == "read_file" and str(args.get("Path") or "").endswith(REVIEW_SKILLS):
                    reviewed = True
                elif name == "read_file" and str(args.get("Path") or "").endswith("/skills/init/SKILL.md"):
                    setup.add("init")
                elif name == "run_command":
                    line = str(args.get("CommandLine") or "")
                    if "repo_init.py" in line:
                        setup.add("git")
                    if LOOK.search(line):
                        pending, unseen = [], []
                    elif VERIFY.search(line):
                        pending = []
                        unseen = [p for p in unseen if os.path.splitext(p)[1].lower() in PAGE_EXT]
                elif "browser" in name:  # Antigravity's browser agent looked at it
                    pending = [p for p in pending if p not in unseen]
                    unseen = []
                elif name in ("write_file", "edit_file"):
                    p = os.path.realpath(str(args.get("Path") or ""))
                    ext = os.path.splitext(p)[1].lower()
                    if ext in CODE_EXT and any(p.startswith(w + os.sep) for w in wss):
                        if p not in files:
                            files.append(p)
                        if p not in pending:
                            pending.append(p)
                        if ext in WEB_EXT and p not in unseen:
                            unseen.append(p)
    return {"files": files, "reviewed": reviewed, "pending": pending, "unseen": unseen, "setup": sorted(setup)}


def test_commands(payload: dict) -> List[str]:
    """The project's own check commands from AGENTS.md (lines that mention tests, build or lint)."""
    for w in payload.get("workspacePaths") or []:
        try:
            with open(os.path.join(conv.to_path(w), "AGENTS.md"), encoding="utf-8", errors="replace") as f:
                text = f.read(60_000)
        except OSError:
            continue
        cmds = [c for ln in text.splitlines() if re.search(r"test|build|lint|check|тест|сборк", ln, re.I)
                for c in re.findall(r"`([^`\n]{3,80})`", ln)]
        return list(dict.fromkeys(cmds))[:3]
    return []


def first_time(st: dict, key: List[str]) -> bool:
    """False when the agent already decided to stop with exactly this left (since the user's last message)."""
    sig = hashlib.sha1("\n".join(key).encode()).hexdigest()[:12]
    seen = st.get("stop") or {}
    if seen.get("users") != len(st["users"]):
        seen = {"users": len(st["users"]), "sigs": []}
    if sig in seen["sigs"]:
        return False
    seen["sigs"].append(sig)
    st["stop"] = seen
    return True


def waiting_on_background(payload: dict, st: dict, run: List[dict]) -> Optional[str]:
    """The main agent ended its turn while its own subagents or background commands still run. Waiting is fine when
    everything left needs their results; open task.md items it kept for itself are not."""
    if not st["users"] or not setting("finishTasks"):
        return None
    items = [(mark, text) for mark, text in task_items(payload, st) if not DELEGATED.search(text)]
    tp = conv.transcript_path(payload)
    if not items or handing_over(AGENT_WAIT.sub(" ", last_answer(tp) if tp else "")):
        return None
    if not first_time(st, ["background|" + text for _mark, text in items]):
        return None
    conv.store(payload["conversationId"], st)
    return parallel.stop_message(run, items, SHOWN)


def decide(payload: dict) -> Optional[str]:
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return None
    if int(payload.get("executionNum") or 0) >= MAX_PER_TURN:
        return None
    st = conv.load(payload)
    if not st:
        return None
    hints = setting("parallelHints")
    run = parallel.running(st) if hints else []
    work = parallel.busy(run)
    if work or (payload.get("fullyIdle") is False and hints and len(work) == len(run)):
        return waiting_on_background(payload, st, work)  # its own subagents/commands, or background tasks we didn't see start
    if payload.get("fullyIdle") is False:
        return None  # only its dev server or watcher runs, or parallel hints are off: as before
    broken = broken_files(payload)
    items = task_items(payload, st) if st["users"] and setting("finishTasks") else []  # subagents: no task.md
    tp = conv.transcript_path(payload)
    review: List[str] = []
    unverified: List[str] = []
    unseen: List[str] = []
    setup: List[str] = []
    answer = last_answer(tp) if tp else ""
    announced = bool(st["users"]) and announced_only(answer)
    if not broken and not items and st["users"] and tp:
        turn = turn_changes(tp, st["users"][-1].get("step", -1), payload)
        setup = setup_left(payload, turn, st, answer)
        if setting("autoReview") and not turn["reviewed"] and len(turn["files"]) >= REVIEW_MIN_FILES:
            review = turn["files"]
        if setting("autoVerify"):
            unseen = turn["unseen"]
            unverified = [p for p in turn["pending"] if p not in unseen]  # the web UI is checked by looking at it
    if not broken and not items and not review and not unverified and not unseen and not setup and not announced:
        return None
    if handing_over(answer):
        if not (setup and only_setup_questions(answer)):
            return None  # the agent handed the turn to the user on purpose
        review, unverified, unseen = [], [], []  # it only asked whether to set up the project: just that

    key = ([f"{path}|{p['msg']}" for path, p in broken] + [text for _mark, text in items]
           + [f"review|{p}" for p in review] + [f"verify|{p}" for p in unverified] + [f"look|{p}" for p in unseen]
           + [f"setup|{s}" for s in setup] + (["announced|" + answer.strip()[:80]] if announced else []))
    if not first_time(st, key):
        return None  # it already decided to stop with exactly this left
    if setup:
        st["setup"] = sorted(set(st.get("setup") or []) | set(setup))
    conv.store(payload["conversationId"], st)

    ws = [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    rel = lambda p: next((os.path.relpath(p, w) for w in ws if p.startswith(w + os.sep)), p)
    listed = lambda paths: ", ".join(rel(p) for p in paths[:SHOWN]) + (", …" if len(paths) > SHOWN else "")
    page = next((p for p in unseen if p.lower().endswith((".html", ".htm"))), "<URL or .html file that shows it>")
    look = (f"run `{compat.python_cmd()} \"{UI_CHECK}\" \"{page}\" --widths 390,1440` (BypassSandbox: true; an app: its dev-server "
            "URL instead of the file), then open the screenshots it prints with read_file, check the layout at both "
            "widths and the console errors it reports, and fix what's broken")
    parts = []
    if announced:
        parts.append(f"Antigravity Plus: you wrote «{answer.strip()}» and ended your turn without doing it — a message "
                     "doesn't run anything. Do it now with a tool call, then continue with the user's request until it's "
                     "done (or until you really need the user).")
    if broken:
        lines = "\n".join(f"- {rel(path)}: {p['tool']} {p.get('line') or ''} {p['msg']}".replace("  ", " ")
                          for path, p in broken[:SHOWN])
        parts.append("Antigravity Plus: files you edited still have syntax errors:\n" + lines + "\n"
                     "Fix them before finishing (unless the user asked you to leave them, or you're stopping to ask "
                     "about them — then say so in one line).")
    if items:
        shown = "\n".join(f"- [{mark}] {text}" for mark, text in items[:SHOWN])
        more = f"\n… and {len(items) - SHOWN} more" if len(items) > SHOWN else ""
        parts.append(("Antigravity Plus: you" if not broken else "Also, you") +
                     " ended your turn, but task.md still has open items in the current checklist:\n"
                     f"{shown}{more}\n"
                     "If they are part of what the user asked and nothing blocks you, continue now with the next one and "
                     "keep task.md updated. Don't continue if you need the user's decision, approval or information, if an "
                     "item is out of scope or meant for later, or if it needs an outward action the user hasn't approved "
                     "(push, deploy, sending, deleting). In that case end your turn again: say in one line why these items "
                     "are open and mark them in task.md, e.g. «— ждёт вашего решения» or «— отложено».")
    if unverified and not review:
        cmds = test_commands(payload)
        parts.append(f"Antigravity Plus: you changed code ({listed(unverified)}) but ran no check after the last change. "
                     "Before you report the work done, verify it: run the project's tests or build"
                     + (f" (from AGENTS.md: {', '.join('`' + c + '`' for c in cmds)})" if cmds else "")
                     + ", or at least run the changed code"
                     + (f". Also look at the web UI you changed ({listed(unseen)}): {look}" if unseen else "")
                     + ". If it can't be checked here (no tests, needs a device or a server), say so in one line instead.")
    elif unseen and not review:
        parts.append(f"Antigravity Plus: you changed the web UI ({listed(unseen)}) but haven't looked at it. Before you "
                     f"report the work done, look at it yourself: {look}. A syntax check or a command that only prints a "
                     "message doesn't count. If it can't be opened here (it needs a login or a backend that isn't "
                     "running), run its tests instead or say so in one line.")
    if review:
        parts.append(f"Antigravity Plus: you changed {len(review)} code files in this turn ({listed(review)}"
                     + ") and haven't reviewed them. Before you report the work "
                     "done, run /review on your change yourself (read the review skill's SKILL.md and follow «When you run "
                     "it on your own»), and fix what it confirms. Skip it if the change is mechanical (renames, formatting, "
                     "moved code, config values) or the user asked for speed; then just finish and say why in one line."
                     + (" Also run the project's tests or build after the last change — you haven't checked it yet."
                        if unverified else "")
                     + (f" And look at the web UI you changed: {look}." if unseen else ""))
    if setup:
        steps = []
        if "git" in setup:
            steps.append(f"- create the git repository: `{compat.python_cmd()} \"{GIT_INIT}\" --path \"{ws[0]}\"` (your changes so far "
                         "become part of the first commit);")
        if "init" in setup:
            steps.append(f"- write the project rules: run /init — read \"{INIT_SKILL}\" and follow «When you run it on "
                         "your own».")
        parts.append("Antigravity Plus: " + ("you offered the project setup to the user instead of doing it. "
                                             if offers_setup(answer) else "")
                     + "Set up the project now — it's yours to do, no need to ask:\n" + "\n".join(steps)
                     + "\nThen say in one line what you did. Skip a step only if the user said not to.")
    return "\n\n".join(parts)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        reason = decide(payload) if isinstance(payload, dict) else None
    except Exception as e:  # a hook must never trap the agent
        print(f"stop: {e}", file=sys.stderr)
        reason = None
    sys.stdout.write(json.dumps({"decision": "continue", "reason": reason} if reason else {}, ensure_ascii=False))


if __name__ == "__main__":
    main()
