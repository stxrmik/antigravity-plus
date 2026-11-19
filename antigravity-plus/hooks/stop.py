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
them with ui_check.py screenshots, and a changed screen of a mobile app (SwiftUI, UIKit, Compose, layouts, Flutter,
React Native) by running it on a simulator or an emulator with mobile.py, with the exact command (autoVerify;
`memory.py config auto-verify off`).
And once per conversation, when the main agent changed code in a project without a git repository or without
project rules — or offered that setup to the user instead of doing it — to set it up itself with /git-init and
/init (autoSetup; `memory.py config auto-setup off`).
When nothing of that is left, the final report is held against the conversation (hooks/facts.py; factChecks,
`memory.py config fact-checks off`): «тесты проходят», «задеплоил», file links, line references, links and — in
an answer to a question — versions and prices with nothing to back them send the agent back once to check them
or correct the report; subagents' reports too. Then the main agent is sent back once when it agreed with the user's
objection or guess at a cause («Вы правы», «приношу извинения») without looking at anything in this turn, to check
first and answer on what it finds (hooks/objective.py; objectiveAnswers, `memory.py config objective-answers off`).
Then it is sent back once to save what a future conversation would need — a
lasting thing the user said, what it worked out about servers and deploys, a failure it got past, or after a long
stretch of work — or when it says «💾 Запомнил» without writing a memory file (hooks/memory_hints.py; autoMemory,
`memory.py config auto-memory off`).
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
from typing import Dict, List, Optional, Set, Tuple

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
MOBILE = os.path.join(PLUGIN, "skills", "mobile", "scripts", "mobile.py")
CI_SCRIPT = os.path.join(PLUGIN, "skills", "ci", "scripts", "ci.py")
# Files that are a mobile screen by themselves, and what marks one in code.
MOBILE_LAYOUT = re.compile(r"\.(storyboard|xib)$|[/\\]res[/\\](layout|values|drawable|menu|navigation|color|font|anim)"
                           r"[\w-]*[/\\][^/\\]+\.xml$", re.I)
MOBILE_CODE = {".swift": re.compile(r"\bimport\s+(SwiftUI|UIKit)\b"),
               ".kt": re.compile(r"@Composable\b|androidx\.compose\.|\bsetContentView\(|:\s*(\w+\.)?(Fragment|\w*Activity)\("),
               ".dart": re.compile(r"package:flutter/"),
               ".tsx": re.compile(r"""from\s+['"](react-native|expo[\w/-]*|@react-navigation/[\w-]+)['"]"""),
               ".jsx": re.compile(r"""from\s+['"](react-native|expo[\w/-]*|@react-navigation/[\w-]+)['"]""")}
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
# The last sentence of a longer message that says, in the first person, what the agent is about to do («Исправляем
# расчёт скидки в calc.py.», «Сейчас внесу правку.»). Seen live after a correct diagnosis: the fix never came.
ANNOUNCE_LAST = re.compile(
    r"^\W*(?:(?:сейчас|теперь|далее|затем)\s+)?(?:исправля(?:ю|ем)|исправ(?:лю|им)|чин(?:ю|им)|почин(?:ю|им)|вношу|вносим|"
    r"внес(?:у|[её]м)|переделыва(?:ю|ем)|передела(?:ю|ем)|переписыва(?:ю|ем)|перепиш(?:у|ем)|добавля(?:ю|ем)|добав(?:лю|им)|"
    r"обновля(?:ю|ем)|обнов(?:лю|им)|меня(?:ю|ем)|измен(?:ю|им)|заменя(?:ю|ем)|замен(?:ю|им)|приступа(?:ю|ем)|начина(?:ю|ем)|"
    r"запуска(?:ю|ем)|запущу|запустим|перехо(?:жу|дим)\s+к|займусь|займ[её]мся|реализу(?:ю|ем)|применя(?:ю|ем)|примен(?:ю|им)|"
    r"удаля(?:ю|ем)|удал(?:ю|им)|созда(?:ю|[её]м|м|дим)|напиш(?:у|ем)|выполня(?:ю|ем)|выполн(?:ю|им)|сдела(?:ю|ем)|"
    r"провер(?:ю|им))\b"
    r"|^\W*(?:let me|i'?ll|i will|i'?m going to|i am going to|now i'?ll|next,? i'?ll)\b", re.I)
ANNOUNCE_QUOTE = 240
CONTINUE = re.compile(r"^\W*(продолж|дальше|доделай|давай дальше|go on|continue|keep going|carry on)", re.I)
# A task.md item handed to a subagent: «Найти обработчики — explorer», «… (researcher)», and — only as the item's last
# words, so «Fix: background color» or «Bug: explorer crashes» stay the agent's own — «…: worker», «… (в фоне)».
_SUBAGENTS = r"explorer|researcher|worker|code-reviewer|security-auditor|debugger|test-writer|субагент\w*|subagent"
DELEGATED = re.compile(
    rf"(?:[—–(]|->|→|\s-)\s*(?:antigravity-plus:)?(?:{_SUBAGENTS})\b"
    rf"|(?:[—–(:]|->|→|\s-)\s*(?:in\s+(?:the\s+)?)?(?:antigravity-plus:)?(?:{_SUBAGENTS}|swarm|в\s+фоне|background)\b"
    r"[\s)\]»\"'`.!…]*$",
    re.I,
)
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


def announcement(answer: str) -> str:
    """What the agent said it would do and then didn't: a short message that only announces an action («Сейчас запущу
    тесты.»), or the last sentence of a longer one («…Исправляем расчёт скидки в calc.py.»). "" when there is none."""
    text = answer.strip()
    if not text or WAITING.search(re.sub(r"`[^`]*`", " ", text)):
        return ""
    parts = sentences(text)
    raw, last = (parts[-1][0].strip(), parts[-1][1].strip()) if parts else ("", "")
    if len(text) <= ANNOUNCE_MAX:
        return text if ANNOUNCE.search(text) or ANNOUNCE.search(last) else ""
    if last.endswith("?") or not ANNOUNCE_LAST.search(last):
        return ""
    quote = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)  # links: keep the name, drop the long target
    return quote if len(quote) <= ANNOUNCE_QUOTE else quote[:ANNOUNCE_QUOTE - 1] + "…"


def announced_only(answer: str) -> bool:
    """The agent ended its turn with a message that announces an action it didn't take."""
    return bool(announcement(answer))


def setup_left(payload: dict, turn: dict, st: dict, answer: str) -> List[str]:
    """Project setup the main agent should have done on its own: "git" (no repository) and/or "init" (no project
    rules). Only after it changed code, or offered the setup to the user instead; each step once per conversation."""
    if not setting("autoSetup") or not (turn["files"] or offers_setup(answer)):
        return []
    ws = next((os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)), "")
    if not ws or not os.path.isdir(ws):
        return []
    repo_init = None
    try:
        if os.path.isfile(GIT_INIT):  # the git module may not be installed: then only /init
            sys.path.insert(0, os.path.dirname(GIT_INIT))
            import repo_init
            if repo_init.place_refusal(ws):
                return []  # the home folder, ~/Downloads and the like
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        top = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return []
    done = set(st.get("setup") or []) | set(turn["setup"])
    left = []
    if repo_init and "git" not in done and not top and not repo_init.refusal(ws):
        left.append("git")
    if os.path.isfile(INIT_SKILL) and "init" not in done and \
            not any(os.path.isfile(os.path.join(r, n)) for r in {ws, top} - {""} for n in RULE_FILES):
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


# git commands that can stop half way and leave the repository in a rebase, merge, cherry-pick, revert, am or bisect
GIT_MULTISTEP = re.compile(r"\bgit\b[^|;&\n]*\b(rebase|merge|cherry-pick|revert|am|bisect|pull|stash\s+(pop|apply))\b")

# Commands that check work: tests, builds, linters, or running the changed program itself.
VERIFY = re.compile(
    r"\b(pytest|unittest|tox|nox|mypy|ruff|flake8|pylint|jest|vitest|mocha|playwright|cypress|tsc|eslint|phpunit|rspec|"
    r"xcodebuild|swiftlint|gradlew?|mvn|ctest)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|check|typecheck|e2e)\b"
    r"|\b(go|cargo)\s+(test|build|vet|check|run)\b|\bswift\s+(test|build|run)\b|\bmake\s+(test|check|build|lint|all)\b"
    r"|\bdocker\s+(compose\s+)?(run|exec|build)\b|\bpython3?\s+(-m\s+\w+|[\w./-]+\.py)\b|\b(node|deno|ts-node|tsx)\s+[\w./-]+"
    r"|\b(bash|sh|zsh)\s+[\w./-]+\.sh\b|(^|[\s;&|])\.[/\\][\w./\\-]+|\bcurl\b.*\b(localhost|127\.0\.0\.1)\b|ui_check\.py"
    r"|\bpy\s+(-3\S*\s+)?(-m\s+\w+|[\w./\\:-]+\.py)\b|\bpython3?(\.exe)?\s+(-m\s+\w+|[\w./\\:-]+\.py)\b"
    r"|\b(pwsh|powershell)(\.exe)?\b.*\.ps1\b|\bdotnet\s+(test|build|run)\b|\binvoke-pester\b|\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b.*\b(localhost|127\.0\.0\.1)\b"
    r"|\bflutter\s+(test|analyze|build|drive|run)\b|\bdart\s+(test|analyze)\b|\bdetox\s+(test|build)\b|\bmaestro\s+test\b", re.I)
# Commands that show how a page looks: screenshots in a real browser (a syntax check or curl doesn't).
LOOK = re.compile(r"ui_check\.py|\b(playwright|cypress|puppeteer|selenium|webdriver|screencapture)\b|--screenshot", re.I)
# What a command does for a mobile app. `mobile.py` is this plugin's tool — not a file that merely ends that way.
MOBILE_TOOL = r"(?<![\w-])mobile\.py[\"']?\s+"
# …puts a build on a simulator or an emulator: what was changed before it is what will be seen.
MOBILE_INSTALL = re.compile(MOBILE_TOOL + r"(run|install)\b|\bsimctl\s+install\b|\badb(\.exe)?\b[^|;&]*\sinstall(-multiple)?\s"
                            r"|\bandroid\s+(run|install)\b|\bflutter\s+(run|install)\b|\b(expo|react-native)\s+run", re.I)
# …shows it running: a screenshot.
MOBILE_LOOK = re.compile(MOBILE_TOOL + r"(run|shot)\b|" + MOBILE_TOOL + r"[^|;&]*\s--shot\b|\bsimctl\s+io\s+\S+\s+screenshot\b"
                         r"|\badb(\.exe)?\b[^|;&]*\bscreencap\b|\bandroid\s+screen\s+capture\b", re.I)
# …drives its screens in UI tests — Flutter's widget tests included (a JVM unit test or a plain build doesn't; nor
# does a path with «test» in it).
MOBILE_TESTS = re.compile(r"\bxcodebuild\b.*\s(test|test-without-building)(\s|$)|\bgradlew?(\.bat)?\b.*\b\w*AndroidTest\b"
                          r"|\bflutter\s+(test|drive)\b|\bdetox\s+test\b|\bmaestro\s+test\b", re.I)
_QUOTED_ARG = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SHELL_WRAP = re.compile(r"\b(?:sh|bash|zsh|dash)\s+-c\b|\bcmd(?:\.exe)?\s+/c\b", re.I)
CI_PUSH = re.compile(
    r"(?:^|[;&|\n]\s*)(?:(?:cd|pushd)\s+[^;&|\n]+(?:&&|;)\s*)*(?:env\s+\S+=\S+\s+)*"
    r"(?:git(?:\.exe)?\s+(?:-[^\s;|&]+\s+(?:Q\s+)?)*push\b(?!.*\s(?:-n|--dry-run|--delete|-d|--tags)\b)(?!.*\s:[\w./-]+)"
    r"|gh(?:\.exe)?\s+pr\s+create\b|glab(?:\.exe)?\s+mr\s+create\b)", re.I
)
CI_WATCH = re.compile(r"(?<![\w-])ci\.py[\"']?\s+watch\b|\bgh\s+(?:run\s+watch|pr\s+checks\b.*--watch)\b"
                      r"|\bglab\s+ci\s+(?:status\b.*--live|trace)\b", re.I)
CI_CHECK = re.compile(r"(?<![\w-])ci\.py[\"']?\s+status\b|\bgh\s+(?:run\s+(?:view|list)|pr\s+checks)\b"
                      r"|\bglab\s+ci\s+(?:status|view|get|list)\b", re.I)
CI_STILL_RUNNING = re.compile(
    r'⏳\s*CI|"status":\s*"(?:in_progress|queued|pending|waiting|requested)"|\bin_progress\b|\bqueued\b'
    r'|\b[1-9]\d*\s+pending\s+checks?\b|(?:status|state):\s*(?:running|pending)\b', re.I
)
CI_NONE_OUT = re.compile(r'ℹ️\s*No CI run|ℹ️\s*CI cannot be queried|\bno (?:runs?|pipelines?) found\b', re.I)
NO_CI_WAIT = re.compile(
    r"\bне\s+(?:жди|ждать|отслежива\w*|проверя\w*)\s+(?:ci|пайплайн\w*|экшен\w*|actions?|проверк\w*)"
    r"|\b(?:ci|пайплайн\w*|экшен\w*|actions?)\s+не\s+(?:жди|ждать|отслежива\w*|проверя\w*)"
    r"|\bбез\s+(?:ожидания\s+)?ci\b|\b(?:don'?t|do\s+not|no\s+need\s+to)\s+(?:wait\s+for|watch|track)\s+ci\b", re.I
)
CI_WAIT_ITEM = re.compile(
    r"\b(?:дождаться|отследить|проверить|убедиться|ожидание|wait\s+for|watch|track|verify|check)\b.*"
    r"\b(?:ci|actions?|экшен\w*|пайплайн\w*|pipeline|воркфлоу|workflows?|checks?)\b"
    r"|\b(?:ci|github\s+actions?|gitlab\s+ci|pipeline)\b.*\b(?:зелён\w*|прош\w*|ожидан\w*|watch|pass\w*)\b", re.I
)
# A task.md item that acts outward (deploy, push, commit, release…): never what the agent should start while its
# review or tests still run — seen live: told to do «what doesn't depend on them», it deployed before its review came.
OUTWARD_ITEM = re.compile(
    r"/(?:deploy|commit|pr|release)\b|\b(?:за)?депло\w*|\bвыка[тч]\w*|\bрелиз\w*|\bопубликов\w*|\bпубликац\w*"
    r"|\b(?:за)?пуш\w*|\b(?:за)?коммит\w*|\bвлить\b|\bвле[йт]\w*|\bсмерж\w*|\bмерж\w*"
    r"|\b(?:deploy|push|commit|release|publish|merge|roll\s*out|ship)\w*", re.I)
HELP = re.compile(r"\s(--help|-h)(\s|$)")
BG_STARTED = re.compile(r"running as a background task with task id:\s*(\S+)")
BG_FINISHED = re.compile(r'Task id "([^"]+)" finished with result:\s*Command exited with code (-?\d+)')
BG_CUT = re.compile(r'Task id "([^"]+)" finished with result:\s*The tool.s output was truncated')
HOT_RELOAD = (".tsx", ".jsx")  # React Native: a saved file reaches the running app by itself


def _is_push_cmd(line: str) -> bool:
    s = line[:600]
    cleaned = s if _SHELL_WRAP.search(s) else _QUOTED_ARG.sub("Q", s)
    return bool(CI_PUSH.search(cleaned))


def mobile_command(line: str) -> Optional[dict]:
    """{"install", "look", "tests"} for a command that matters to a mobile app's screens, else None."""
    if HELP.search(line):
        return None
    did = {"install": bool(MOBILE_INSTALL.search(line)), "look": bool(MOBILE_LOOK.search(line)),
           "tests": bool(MOBILE_TESTS.search(line))}
    return dict(did, ok=None) if any(did.values()) else None


def ci_command(line: str) -> Optional[dict]:
    """{"push", "watch", "check"} for a command that pushes to remote or tracks CI, else None."""
    if HELP.search(line):
        return None
    did = {"push": _is_push_cmd(line), "watch": bool(CI_WATCH.search(line)), "check": bool(CI_CHECK.search(line))}
    return dict(did, ok=None, code=None, out="") if any(did.values()) else None


def command_result(raw: str, waiting: List[List[dict]], background: Dict[str, List[dict]]) -> None:
    """Give the commands the agent started their outcome: one result row per call, in order; a command that went on
    in the background gets it when it reports back. A command that failed (or was refused) did nothing."""
    try:
        d = json.loads(raw)
    except ValueError:
        return
    content = d.get("content") or ""
    for m in BG_FINISHED.finditer(content):
        if m.group(1) in background:
            tail = content[m.end():m.end() + 8000].split("Command output:", 1)[-1][-3000:]
            cval = int(m.group(2))
            for rec in background.pop(m.group(1)):
                rec["ok"] = cval == 0
                rec["code"] = cval
                rec["out"] = tail
    for m in BG_CUT.finditer(content):
        if m.group(1) in background:
            tail = content[m.end():m.end() + 8000][-3000:]
            for rec in background.pop(m.group(1)):
                rec["ok"] = True
                rec["code"] = 0
                rec["out"] = tail
    if d.get("type") != "GENERIC" or not waiting:
        return
    records = waiting.pop(0)
    if not records:
        return
    code, later = conv.EXIT.search(content[:3000]), BG_STARTED.search(content[:800])
    out = content.split("Command output:", 1)[-1][-3000:] if code else ""
    if code:
        cval = int(code.group(1))
        for rec in records:
            rec["ok"] = cval == 0
            rec["code"] = cval
            rec["out"] = out
    elif later:
        background[later.group(1)] = records
    else:
        for rec in records:
            rec["ok"] = False
            rec["code"] = None


def mobile_ui(path: str) -> str:
    """"layout" for a file that is a mobile screen by itself (storyboard, Android layout or resources), "code" for
    source that draws one (SwiftUI, UIKit, Compose, Flutter, React Native), "" for anything else."""
    if MOBILE_LAYOUT.search(path):
        return "layout"
    marker = MOBILE_CODE.get(os.path.splitext(path)[1].lower())
    if not marker:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "code" if marker.search(f.read(200_000)) else ""
    except OSError:
        return ""


def mobile_here(paths: List[str], wss: List[str]) -> List[str]:
    """Of these files, the ones that belong to a mobile app this computer can run: an iOS app with Xcode's simulators,
    an Android app with the SDK. A SwiftUI view of a Mac app or a Compose screen of a desktop app isn't one."""
    try:
        sys.path.insert(0, os.path.dirname(MOBILE))
        import mobile
        ios, android = bool(mobile.xcrun()), bool(mobile.adb())
        found = [mobile.detect(w) for w in wss[:2]]
    except Exception:
        return []
    has = {key: any(f[key] for f in found) for key in ("ios", "android", "flutter", "rn")}
    out = []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext in (".swift", ".storyboard", ".xib"):
            fits = ios and (has["ios"] or has["flutter"] or has["rn"])
        elif ext in (".kt", ".xml"):
            fits = android and (has["android"] or has["flutter"] or has["rn"])
        else:
            fits = (ios or android) and has["flutter" if ext == ".dart" else "rn"]
        if fits:
            out.append(p)
    return out


def turn_changes(tp: str, since_step: int, payload: dict) -> dict:
    """What the agent did in this turn (steps after the user's last message): code files written, whether a review
    ran, the code changed after the last check (tests, build, running the program), the web UI changed after
    the agent last looked at it (tests also cover components, but pages and styles only by looking), and the screens
    of a mobile app changed after it last ran the app on a simulator or an emulator (UI tests cover the code, but a
    build alone shows nothing)."""
    files: List[str] = []
    pending: List[str] = []
    unseen: List[str] = []
    story: List[dict] = []                   # screens edited and mobile commands run, in the order they were started
    ci_story: List[dict] = []                # pushes and CI watch/status commands, in order
    waiting: List[List[dict]] = []           # the calls of the latest step, each still to get its result row
    background: Dict[str, List[dict]] = {}
    setup: Set[str] = set()
    reviewed = False
    git_ops = False                          # a git command that can stop half way (rebase, merge, pull, …)
    wss = [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    try:
        f = open(tp, encoding="utf-8", errors="replace")
    except OSError:
        return {"files": [], "reviewed": True, "pending": [], "unseen": [], "mobile": [], "setup": [], "ci": False,
                "git": False}
    with f:
        for raw in f:
            m = STEP.search(raw[:120])
            if not m or int(m.group(1)) <= since_step:
                continue
            if '"tool_calls"' not in raw:
                if any(waiting) or background:
                    command_result(raw, waiting, background)
                continue
            try:
                calls = json.loads(raw).get("tool_calls") or []
            except ValueError:
                continue
            waiting = []
            for c in calls:
                args = c.get("args") or {}
                name = str(c.get("name") or "")
                waiting.append([])
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
                    if GIT_MULTISTEP.search(line):
                        git_ops = True
                    did = mobile_command(line)
                    if did:
                        story.append(did)
                        waiting[-1].append(did)
                    ci_did = ci_command(line)
                    if ci_did:
                        ci_story.append(ci_did)
                        waiting[-1].append(ci_did)
                    if did and (did["look"] or did["tests"]):
                        pending = []
                    elif LOOK.search(line):
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
                    if not any(p.startswith(w + os.sep) for w in wss):
                        continue
                    screen = mobile_ui(p)
                    if screen:
                        story.append({"edit": p})
                    if ext in CODE_EXT:
                        if p not in files:
                            files.append(p)
                        if p not in pending:
                            pending.append(p)
                        if ext in WEB_EXT and not screen and p not in unseen:
                            unseen.append(p)
    return {"files": files, "reviewed": reviewed, "pending": pending, "unseen": unseen, "mobile": unseen_screens(story),
            "setup": sorted(setup), "ci": ci_untracked(ci_story), "git": git_ops}


def ci_untracked(story: List[dict]) -> bool:
    """True when a push / PR creation succeeded in this turn and no CI watch (or finished CI check) followed it."""
    untracked = False
    for step in story:
        out = step.get("out") or ""
        if step["watch"] and step.get("code") in (0, 1):
            if "There were sandbox errors" not in out and "Cannot watch CI" not in out:
                untracked = False  # `watch` finished (green, red with logs, or explicit no-run after appear-timeout)
        elif step["push"] and step["ok"]:
            if "Everything up-to-date" not in out:
                untracked = True
        elif step["check"] and step["ok"]:
            has_run = out.strip() not in ("", "[]") and not CI_NONE_OUT.search(out)
            if has_run and (not CI_STILL_RUNNING.search(out) or "✅ CI passed" in out or "❌ CI failed" in out):
                untracked = False
    return untracked


def ci_left(payload: dict, turn: dict, st: dict) -> Optional[Tuple[str, List[str], str]]:
    """(workspace, workflows, head_sha) when the main agent pushed changes in a repo with active CI and hasn't tracked it."""
    if not setting("autoCi") or not turn.get("ci") or not st.get("users"):
        return None
    if any(NO_CI_WAIT.search(u.get("text") or "") for u in st["users"]):
        return None
    ws = next((os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)), "")
    if not ws or not os.path.isdir(ws):
        return None
    try:
        sys.path.insert(0, os.path.dirname(CI_SCRIPT))
        import ci as citool
        info = citool.repo_ci(ws)
        if not info.get("ready") or citool.commit_skips_ci(info["root"]):
            return None
        head_sha = citool.git(["rev-parse", "HEAD"], info["root"]) or ""
        return ws, info["triggerable"], head_sha
    except Exception:
        return None


def unseen_screens(story: List[dict]) -> List[str]:
    """The screens changed and not looked at since. A look shows what is installed: a screen changed after the last
    install is still the old one in the picture (React Native aside, where a saved file reaches the running app). UI
    tests cover the code of a screen, not a layout file. A command that failed, or hasn't finished, did nothing."""
    unseen: List[str] = []
    old_build: Set[str] = set()
    for step in story:
        path = step.get("edit")
        if path:
            unseen = [p for p in unseen if p != path] + [path]
            if not path.lower().endswith(HOT_RELOAD):
                old_build.add(path)
        elif step.get("ok"):
            if step["install"]:
                old_build.clear()
            if step["look"]:
                unseen = [p for p in unseen if p in old_build]
            if step["tests"]:
                unseen = [p for p in unseen if MOBILE_LAYOUT.search(p)]
    return unseen


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


def _told(st: dict) -> dict:
    """What the reminders already named in this turn (since the user's last message), by kind."""
    t = st.get("stop_told") or {}
    if t.get("users") != len(st["users"]):
        t = {"users": len(st["users"])}
    return t


def told_once(st: dict, review: List[str], verify: List[str], look: List[str], mobile: List[str]) -> tuple:
    """The review / check / look reminders without the files a reminder of the same kind already named in this turn.
    `first_time` compares the whole set of what's left, so when one part changed (the agent ran the tests it was
    told to) the others came again word for word — seen live: «you changed 5 code files … run /review» twice in a
    row for the same files. A second review reminder needs REVIEW_MIN_FILES files it hasn't named yet."""
    t = _told(st)
    fresh = lambda kind, paths: [p for p in paths if p not in (t.get(kind) or [])]  # noqa: E731
    new_review = fresh("review", review)
    review = new_review if not t.get("review") or len(new_review) >= REVIEW_MIN_FILES else []
    return review, fresh("verify", verify), fresh("look", look), fresh("mobile", mobile)


def mark_told(st: dict, **kinds: List[str]) -> None:
    t = _told(st)
    for kind, paths in kinds.items():
        if paths:
            t[kind] = sorted(set(t.get(kind) or []) | set(paths))
    st["stop_told"] = t


def waiting_on_background(payload: dict, st: dict, run: List[dict]) -> Optional[str]:
    """The main agent ended its turn while its own subagents or background commands still run. Waiting is fine when
    everything left needs their results; open task.md items it kept for itself are not."""
    if not st["users"] or not setting("finishTasks"):
        return None
    if payload.get("continuationActive") and st.get("bg_nudge_turn") == len(st["users"]):
        return None
    items = [(mark, text) for mark, text in task_items(payload, st)
             if not DELEGATED.search(text) and not CI_WAIT_ITEM.search(text) and not OUTWARD_ITEM.search(text)]
    tp = conv.transcript_path(payload)
    if not items or handing_over(AGENT_WAIT.sub(" ", last_answer(tp) if tp else "")):
        return None
    msg = parallel.stop_message(run, items, SHOWN)
    if not msg:
        return None
    ids = sorted(str(it.get("id") or "") for it in run)
    key = ["background|" + i for i in ids] or ["background"]
    if not first_time(st, key):
        return None
    st["bg_nudge_turn"] = len(st["users"])
    conv.store(payload["conversationId"], st)
    return msg


def facts_check(payload: dict, st: dict, answer: str) -> Optional[str]:
    """Nothing else is left: what the final report claims without anything in the conversation to back it
    (hooks/facts.py) — for subagents' reports too."""
    try:
        import facts
        msg = facts.stop_message(payload, st, answer, VERIFY, tuple(CODE_EXT), WEB_EXT)
    except Exception as e:  # a check of the report must never trap the agent
        print(f"stop: facts: {e}", file=sys.stderr)
        return None
    if msg:
        conv.store(payload["conversationId"], st)
    return msg


def objective_check(payload: dict, st: dict, answer: str) -> Optional[str]:
    """Nothing else is left: the main agent gave in to the user's objection or guess without checking anything
    (hooks/objective.py)."""
    try:
        import objective
        msg = objective.stop_message(payload, st, answer)
    except Exception as e:  # a check of the answer must never trap the agent
        print(f"stop: objective: {e}", file=sys.stderr)
        return None
    if msg:
        conv.store(payload["conversationId"], st)
    return msg


def memory_check(payload: dict, st: dict, answer: str) -> Optional[str]:
    """Nothing else is left: what the main agent learned in this work and should save (hooks/memory_hints.py)."""
    try:
        import memory_hints
        msg = memory_hints.stop_message(payload, st, answer, handing_over(answer))
    except Exception as e:  # memory is optional: never trap the agent over it
        print(f"stop: memory: {e}", file=sys.stderr)
        return None
    conv.store(payload["conversationId"], st)
    return msg


def git_unfinished(payload: dict) -> Optional[dict]:
    """A rebase, merge, cherry-pick, revert, am or bisect the agent left half done in this turn, or conflict markers
    left in changed files (hooks/gitcheck.py)."""
    try:
        import gitcheck
    except Exception:  # optional, like the other checks
        return None
    for w in payload.get("workspacePaths") or []:
        if isinstance(w, str) and w:
            try:
                found = gitcheck.unfinished(os.path.realpath(conv.to_path(w)))
            except Exception as e:  # never trap the agent over a check
                print(f"stop: git: {e}", file=sys.stderr)
                return None
            if found:
                return found
    return None


def decide(payload: dict) -> Optional[str]:
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return None
    if int(payload.get("executionNum") or 0) >= MAX_PER_TURN:
        return None
    st = conv.load(payload)
    if not st:
        return None
    if conv.delegated(st) and conv.waiting_for_claude(st):
        return None  # it asked Claude Code and waits: the answer arrives as a message and wakes it up
    in_plan = False
    try:
        import orchestrator  # type: ignore[import-not-found]
        before_orch = json.dumps(st, sort_keys=True, ensure_ascii=False)
        fn = getattr(orchestrator, "in_plan_mode", None)
        in_plan = bool(fn(payload, st)) if callable(fn) else False
        if json.dumps(st, sort_keys=True, ensure_ascii=False) != before_orch and isinstance(payload.get("conversationId"), str):
            conv.store(payload["conversationId"], st)
    except Exception:
        in_plan = False
    hints = setting("parallelHints")
    run = parallel.running(st) if hints else []
    work = parallel.busy(run)
    if work and not in_plan:
        return waiting_on_background(payload, st, work)  # its own subagents/commands
    if payload.get("fullyIdle") is False:
        return None  # only its dev server or watcher runs, nothing tracked, or parallel hints are off
    broken = [] if in_plan else broken_files(payload)
    items = task_items(payload, st) if st["users"] and setting("finishTasks") else []  # subagents: no task.md
    tp = conv.transcript_path(payload)
    review: List[str] = []
    unverified: List[str] = []
    unseen: List[str] = []
    mobile: List[str] = []
    setup: List[str] = []
    ci_need: Optional[Tuple[str, List[str], str]] = None
    git_left: Optional[dict] = None
    answer = last_answer(tp) if tp else ""
    announced = announcement(answer) if st["users"] else ""
    handed = conv.delegated(st)  # a task from Claude Code: it reviews the result itself and never pushes from here
    if not in_plan and not broken and not items and st["users"] and tp:
        turn = turn_changes(tp, st["users"][-1].get("step", -1), payload)
        setup = [] if handed else setup_left(payload, turn, st, answer)
        ci_need = None if handed else ci_left(payload, turn, st)
        git_left = git_unfinished(payload) if turn.get("git") and setting("gitChecks") else None
        if setting("autoReview") and not handed and not turn["reviewed"] and len(turn["files"]) >= REVIEW_MIN_FILES:
            review = turn["files"]
        if setting("autoVerify"):
            # A task from Claude Code: Claude Code looks at the screens itself (several of its agents can't share one
            # simulator), so only «run a check» stays — seen live: four delegated agents each sent back to «look at
            # the app» they had been told not to launch, every extra call on a 300k–500k context.
            look_ok = os.path.isfile(UI_CHECK) and not handed  # no /ui-check installed: tests are the check
            unseen = turn["unseen"] if look_ok else []
            mobile = mobile_here(turn["mobile"], [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or []
                                                  if isinstance(w, str)]) if turn["mobile"] and os.path.isfile(MOBILE) and not handed else []
            # the web UI and a mobile app's screens are checked by looking at them
            unverified = [p for p in turn["pending"] if p not in unseen and p not in mobile]
        review, unverified, unseen, mobile = told_once(st, review=review, verify=unverified, look=unseen, mobile=mobile)
    if (not broken and not items and not review and not unverified and not unseen and not mobile
            and not setup and not ci_need and not git_left and not announced):
        return (facts_check(payload, st, answer) or (objective_check(payload, st, answer) if st["users"] else None)
                or (memory_check(payload, st, answer) if st["users"] and not handed and not in_plan else None))
    if handing_over(answer):
        if not (setup and only_setup_questions(answer)):
            return None  # the agent handed the turn to the user on purpose
        review, unverified, unseen, mobile, ci_need = [], [], [], [], None  # it only asked whether to set up the project: just that

    key = ([f"{path}|{p['msg']}" for path, p in broken] + [text for _mark, text in items]
           + [f"review|{p}" for p in review] + [f"verify|{p}" for p in unverified] + [f"look|{p}" for p in unseen]
           + [f"mobile|{p}" for p in mobile]
           + [f"setup|{s}" for s in setup]
           + ([f"ci|{ci_need[0]}|{ci_need[2]}"] if ci_need else [])
           + ([f"git|{git_left['kind']}|{','.join(git_left['conflicts'])}|{len(git_left['markers'])}"] if git_left else [])
           + (["announced|" + answer.strip()[:80]] if announced else []))
    if not first_time(st, key):
        # It already decided to stop with exactly this left: say no more about it, but its report still has to be
        # true (seen live: «Тесты не запускались… Готово, тесты проходят») and rest on a look, not on giving in.
        return facts_check(payload, st, answer) or (objective_check(payload, st, answer) if st["users"] else None)
    if setup:
        st["setup"] = sorted(set(st.get("setup") or []) | set(setup))
    mark_told(st, review=review, verify=unverified, look=unseen, mobile=mobile)
    conv.store(payload["conversationId"], st)

    ws = [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    rel = lambda p: next((os.path.relpath(p, w) for w in ws if p.startswith(w + os.sep)), p)
    listed = lambda paths: ", ".join(rel(p) for p in paths[:SHOWN]) + (", …" if len(paths) > SHOWN else "")
    page = next((p for p in unseen if p.lower().endswith((".html", ".htm"))), "<URL or .html file that shows it>")
    look = (f"run `{compat.python_cmd()} \"{UI_CHECK}\" \"{page}\" --widths 390,1440` (BypassSandbox: true; an app: its dev-server "
            "URL instead of the file), then open the screenshots it prints with read_file, check the layout at both "
            "widths and the console errors it reports, and fix what's broken")
    py = compat.python_cmd()
    app_look = (f"build the app (`{py} \"{MOBILE}\" project \"{ws[0] if ws else '.'}\"` prints the build command), then run "
                f"`{py} \"{MOBILE}\" run --project \"{ws[0] if ws else '.'}\"` (BypassSandbox: true): it installs and launches the "
                "newest build on the running simulator or emulator (`boot` starts one) and takes a screenshot. Open the "
                "screenshot with read_file, go to the screen you changed (`ui`, then `tap --text \"…\"`), and fix what's broken")
    parts = []
    if announced:
        parts.append(f"Antigravity Plus: you wrote «{announced}» and ended your turn without doing it — a message "
                     "doesn't run anything. Do it now with a tool call, then continue with the user's request until it's "
                     "done (or until you really need the user).")
    if git_left:
        import gitcheck
        parts.append("Antigravity Plus: " + gitcheck.describe(git_left) + " Don't end your turn with it half done: "
                     "finish it now (and run the tests), or undo it and say in one line why it's left as it was.")
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
    if mobile and not review:
        parts.append(f"Antigravity Plus: you changed the app's screens ({listed(mobile)}) but haven't looked at them on a "
                     f"simulator or an emulator. Before you report the work done, look yourself: {app_look}. A build or a "
                     "syntax check alone doesn't show how a screen looks. If it can't be shown here (no simulator, it needs "
                     "an account or a server that isn't running), say so in one line.")
    if review:
        parts.append(f"Antigravity Plus: you changed {len(review)} code files in this turn ({listed(review)}"
                     + ") and haven't reviewed them. Before you report the work "
                     "done, run /review on your change yourself (read the review skill's SKILL.md and follow «When you run "
                     "it on your own»), and fix what it confirms. Skip it if the change is mechanical (renames, formatting, "
                     "moved code, config values) or the user asked for speed; then just finish and say why in one line."
                     + (" Also run the project's tests or build after the last change — you haven't checked it yet."
                        if unverified else "")
                     + (f" And look at the web UI you changed: {look}." if unseen else "")
                     + (f" And look at the app's screens you changed: {app_look}." if mobile else ""))
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
    if ci_need:
        ci_ws, ci_wfs, _ = ci_need
        wfs = ", ".join(ci_wfs[:4]) + (f", +{len(ci_wfs) - 4}" if len(ci_wfs) > 4 else "")
        parts.append(f"Antigravity Plus: you pushed changes to the remote, and this repository has CI workflows ({wfs}), "
                     f"but you haven't tracked the CI run. Run `{py} \"{CI_SCRIPT}\" watch --workspace \"{ci_ws}\"` "
                     "(BypassSandbox: true) now. If it moves to the background while CI runs, end your turn with one line "
                     "saying you're waiting for CI — its completion message will wake you up with the result and any "
                     "failure logs. Skip only if the user asked not to wait for CI.")
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
