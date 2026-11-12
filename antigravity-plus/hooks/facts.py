#!/usr/bin/env python3
"""Antigravity Plus — facts: what the agent tells the user has to rest on something that happened or that it read.

Agents fill gaps with plausible guesses: «тесты проходят» with no test run since the last edit, a link to a file that
doesn't exist, a `file:line` past the end of the file, a documentation link no search or page ever showed, versions
and prices recalled from training data, a check faked with `python3 -c "print('passed')"` (seen live). Antigravity
Plus holds the agent's final message against the conversation itself:
  * at the end of a turn (hooks/stop.py, once per user message): claims of tests, a build, linters, «проверил —
    работает» or a look at the UI without a successful run after the last change, and of a deploy, push, commit or
    install without the command anywhere in the conversation, «проверил в симуляторе» without the app having run on
    one after the last change; file links and project paths that don't exist; line
    references past the end of a file; links that came from no search, page, file, user message or the project;
    and, in an answer to the user's question, versions and prices found nowhere in the conversation → the agent is
    sent back to check them, or to correct the report and say plainly what it couldn't confirm. Subagents get the
    same check on their reports (except versions and prices);
  * before the next model call after a question that needs facts from outside (versions, prices, «найди», «сравни»,
    documentation) or from the project's code («где у нас…», «как работает…»): a <grounding> hint to look it up
    first and to cite the source of each fact (hooks/instructions.py);
  * the guard refuses a command that only prints success (hooks/guard.py, fake_check()).
Setting factChecks (`memory.py config fact-checks off`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import hashlib
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
SHOWN = 6                 # problems listed in one message
SEEN_LIMIT = 12_000_000   # chars of the conversation kept to look sources up in
LINES_LIMIT = 5_000_000   # bytes read to count a file's lines

# ─────────────────────────────── claims in the agent's report ───────────────────────────────

# kind → (claim, the command that backs it, does it have to come after the last change?)
TEST_CMD = re.compile(r"test|spec\b|jest|vitest|mocha|phpunit|rspec|ctest", re.I)
BUILD_CMD = re.compile(r"\bbuild\b|\bcompile|xcodebuild|\btsc\b|webpack|gradlew?|\bmvn\b|\bmake\b|cmake|swiftc|go\s+build|"
                       r"cargo\s+build|docker\s+(?:compose\s+)?build|next\s+build|vite\s+build|--build\b", re.I)
# kind → (claim, the command that backs it, after the last change?, must it have succeeded?)
CLAIMS: List[Tuple[str, "re.Pattern", Optional["re.Pattern"], bool, bool]] = [
    ("tests",
     re.compile(r"\bтест\w*\s+(?:(?:тоже|все|снова|теперь|уже|успешно)\s+)?(?:прош(?:ли|ёл|ел|ло)|проход\w*|зел[её]н\w*)"
                r"|\bавтотест\w*\s*[:—–-]\s*\d+|\b\d+\s+(?:passed|tests?\s+passed)\b|\bпротестирован\w*"
                r"|\ball\s+(?:the\s+)?tests?\s+pass|\btests?\s+(?:now\s+|all\s+)?(?:pass(?:es|ed)?|are\s+(?:green|passing))\b", re.I),
     TEST_CMD, True, True),
    ("tests_ran",
     re.compile(r"\b(?:прогнал|запустил|прогнали|запустили)\w*\s+(?:\w+\s+){0,3}(?:авто)?тест\w*|\bпрогон\w*\s+(?:\w+\s+){0,2}"
                r"(?:авто)?тест\w*|\bзапущен\w*\s+(?:\w+\s+){0,4}(?:авто)?тест\w*|\bran\s+the\s+tests\b", re.I),
     TEST_CMD, False, False),
    ("build",
     re.compile(r"\b(?:сборка|билд)\s+(?:(?:тоже|теперь|снова|уже)\s+)?(?:прош\w*|проход\w*|успешн\w*|зел[её]н\w*|собирается)"
                r"|\bсобирается\s+без\s+ошибок|\bкомпилируется\s+без|\bbuild\s+(?:now\s+)?(?:pass(?:es|ed)?|succeed(?:s|ed)?|is\s+green)"
                r"|\bcompiles\s+(?:cleanly|without)", re.I),
     BUILD_CMD, True, True),
    ("build_ran",
     re.compile(r"\bзапустил\w*\s+(?:\w+\s+)?сборк\w*|\bзапущен\w*\s+сборк\w*|\bсобрал\w*\s+(?:проект|приложени\w*|образ\w*|"
                r"билд\w*|контейнер\w*)", re.I),
     BUILD_CMD, False, False),
    ("lint",
     re.compile(r"\b(?:линтер\w*|ruff|eslint|swiftlint|mypy|tsc|flake8|pylint|shellcheck)\s+(?:(?:тоже|теперь)\s+)?"
                r"(?:чист\w*|без\s+(?:ошибок|замечаний)|прош\w*|не\s+(?:ругается|находит))"
                r"|\b(?:lint(?:er|ing)?|type-?check(?:ing)?)\s+(?:is\s+)?(?:clean|pass(?:es|ed)?)", re.I),
     re.compile(r"\b(?:ruff|eslint|swiftlint|mypy|tsc|flake8|pylint|shellcheck|stylelint|biome|lint)\b", re.I), True, True),
    ("works",
     re.compile(r"\bпроверил\w*[,:—-]?\s+(?:—\s+)?(?:всё\s+|все\s+)?работает|\bработает\s+корректно\b|\bверифицировал\w*"
                r"|\bпроверил\w*\s+(?:снаружи|локально|вручную|по-настоящему|на\s+реальн\w*|через\s+\w+)"
                r"|\bпроверен[оаы]?\s+(?:на\s+реальн\w*|снаружи|локально|вручную|в\s+браузере|через\s+\w+)"
                r"|\bverified\s+(?:that\s+)?(?:it|everything)\s+works|\bconfirmed\s+(?:that\s+)?(?:it|everything)\s+works", re.I),
     None, True, True),  # any check: hooks/stop.py's VERIFY, curl, ssh
    ("ui",
     re.compile(r"\bпроверил\w*\s+(?:в\s+браузере|на\s+скриншот\w*|на\s+телефон\w*|в[её]рстк\w*)|\bна\s+скриншот\w*\s+(?:видно|вс[её])"
                r"|\bui[-_]check\b(?:\.py)?\s*(?:[:—–-]\s*)?(?:\d{3,4}|без\s+ошибок|чист\w*|ok\b|прош\w*|passed)"
                r"|\b(?:passed|прошли)\s+и\s+ui[-_]check\b|\bchecked\s+(?:it\s+)?in\s+the\s+browser|\bscreenshots?\s+show", re.I),
     re.compile(r"ui_check\.py|playwright|puppeteer|screencapture|--screenshot", re.I), True, True),
    ("mobile",
     re.compile(r"\bпроверил\w*\s+(?:\w+\s+){0,2}(?:в|на)\s+(?:ios[- ]?)?(?:симулятор\w*|эмулятор\w*)"
                r"|\b(?:в|на)\s+(?:симулятор\w*|эмулятор\w*)\s+(?:вс[её]\s+)?(?:работает|выглядит|отображается|запускается|открывается)"
                r"|\bзапустил\w*\s+(?:\w+\s+){0,2}(?:в|на)\s+(?:симулятор\w*|эмулятор\w*)"
                r"|\bприложение\s+(?:успешно\s+)?(?:запуска\w+|работает|открывается)\s+(?:в|на)\s+(?:симулятор\w*|эмулятор\w*)"
                r"|\b(?:checked|verified|tested|ran|launched)\s+(?:it\s+|the\s+app\s+)?(?:in|on)\s+(?:the|an?)\s+(?:ios\s+|android\s+)?(?:simulator|emulator)"
                r"|\b(?:works|runs|launches)\s+(?:fine\s+|correctly\s+)?(?:in|on)\s+(?:the|an?)\s+(?:ios\s+|android\s+)?(?:simulator|emulator)", re.I),
     # the app has to have run or been looked at: listing devices or installing a build isn't that
     re.compile(r"mobile\.py[\"']?\s+(?:run|shot|ui|launch|tap|swipe|type|key|wait|open)\b|\bsimctl\s+(?:launch|openurl|io\s+\S+\s+screenshot)\b"
                r"|\badb(?:\.exe)?\b.*\b(?:screencap|uiautomator|monkey|am\s+start|input\s+(?:tap|swipe|text|keyevent))\b"
                r"|\bandroid\s+(?:run|screen|layout)\b|\bmaestro\b|\bxcodebuild\b.*\btest|\bflutter\s+(?:run|drive)\b"
                r"|\b(?:expo|react-native)\s+run", re.I), True, True),
    ("deploy",
     re.compile(r"\b(?:задеплоил\w*|задеплоено|выкатил\w*|выкачено|развернул\w*\s+на\s+сервер\w*|выложил\w*\s+на\s+сервер|"
                r"залил\w*\s+на\s+сервер|обновил\w*\s+на\s+сервере)|\bdeployed\s+(?:it\s+|the\s+)?(?:to|on)\b|\bis\s+now\s+live\b", re.I),
     re.compile(r"deploy|\b(?:ssh|scp|rsync|kubectl|helm|flyctl|fly|vercel|netlify|heroku|wrangler|railway|kamal)\b|update\.sh", re.I),
     False, True),
    ("push", re.compile(r"\bзапушил\w*|\bзапушено|\bотправил\w*\s+в\s+(?:origin|github|gitlab)\b|\bpushed\s+(?:it\s+|the\s+)?(?:to|branch)\b", re.I),
     re.compile(r"\bgit\s+push\b", re.I), False, True),
    ("commit", re.compile(r"\bзакоммитил\w*|\bзакоммичено|\bсделал\w*\s+коммит|\bcommitted\s+(?:it|the|all|my|your|these|this)\b", re.I),
     re.compile(r"\bgit\s+commit\b", re.I), False, True),
    ("install", re.compile(r"\bустановил\w*\s+(?:зависимост\w*|пакет\w*|библиотек\w*|модул\w*)|\binstalled\s+(?:the\s+)?(?:dependencies|packages?)\b", re.I),
     re.compile(r"\b(?:pip3?|npm|pnpm|yarn|bun|brew|apt(?:-get)?|uv|poetry|gem|cargo|go|composer|pod)\s+(?:install|add|i|sync|get)\b", re.I),
     False, True),
]
WHAT = {"tests": "test run", "tests_ran": "test command", "build": "build", "build_ran": "build command",
        "lint": "linter run", "works": "check (tests, a build, running it)",
        "ui": "look at the page (ui_check.py screenshots or the browser)",
        "mobile": "run on a simulator or an emulator (mobile.py run / shot)", "deploy": "deploy command",
        "push": "`git push`", "commit": "`git commit`", "install": "install command"}
# A claim the sentence hedges, attributes, asks about or quotes isn't the agent's own statement of fact.
HEDGE_BEFORE = re.compile(r"(?:чтобы|если|когда|пока|пусть|должн\w*|может|могут|можно|стоит|нужно|надо|осталось|ожида\w*|"
                          r"предполага\w*|предлагаю|рекомендую|запустите|проверьте|выполните|попробуйте|прогоните|не|to\s+make|"
                          r"so\s+that|if|once|should|will|would|could|can|expect\w*|needs?|not|n't|run|try|consider)\W+"
                          r"(?:\w+\W+){0,3}$", re.I)
# «29 passed», «29 тестов прошли», «29/29 тестов»: a number the latest test output has to show.
NUM_PASSED = re.compile(r"\b(\d+)\s+(?:passed|tests?\s+passed|тест\w*\s+(?:(?:успешно|тоже|уже)\s+)?(?:прош\w*|проход\w*|зел[её]н\w*))|\b(\d+)\s*(?:/|из)\s*\d+\s+"
                        r"(?:тест\w*|tests?)\b|\bавтотест\w*\s*[:—–-]\s*(\d+)", re.I)
SUMMARY = re.compile(r"\b(?:passed|failed|errors?|ok|ran\s+\d+|tests?:|прошл\w*|упал\w*)\b", re.I)
ATTRIBUTED = re.compile(r"по\s+отч[её]ту|по\s+словам|сообщил\w*|сообщает|пишет|reports?\b|according\s+to|test-writer|worker|debugger|"
                        r"code-reviewer|security-auditor|субагент\w*|subagent|researcher|explorer|вы\s+(?:сказали|писали)|"
                        r"you\s+(?:said|wrote)", re.I)
REMOVED = re.compile(r"удал\w*|убрал\w*|переименов\w*|перен[её]с\w*|вместо|был[аио]?\s+в|\bremov\w*|\bdelet\w*|\brenam\w*|"
                     r"\bmoved\b|\binstead\b|\bused\s+to\b|\bold\b|\bстар\w*", re.I)
UNVERIFIED = re.compile(r"не\s+провер\w*|по\s+памяти|не\s+уверен\w*|не\s+нашёл\s+подтвержд\w*|может\s+быть\s+устар\w*|"
                        r"unverified|from\s+memory|not\s+(?:verified|sure|checked)|may\s+be\s+outdated", re.I)

LINK = re.compile(r"\[([^\]\n]*)\]\(<?([^)\s>]+)>?\)")
TICKED = re.compile(r"`([^`\s]{3,200})`")
PATHLIKE = re.compile(r"^(?:\.{1,2}/|~/|[A-Za-z]:[\\/]|/|[\w.@+-]+/)[\w./\\@+-]*[\w@+-](?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)?$")
LINE_REF = re.compile(r"(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L?(\d+))?)$")
# A path, not a git ref or a MIME type: a file extension, an explicit start (./ ../ ~/ /) or a trailing slash.
FILEISH = re.compile(r"\.[A-Za-z0-9]{1,8}$|^(?:\.{1,2}[\\/]|~[\\/]|/|[A-Za-z]:[\\/])|[\\/]$")
TLD = r"(?:com|ru|org|net|io|dev|co|me|info|xyz|site|online|tech|cloud|pro|uk|de|eu|us|su|by|kz|ua|рф)"
NOT_A_PATH = re.compile(r"^[\w-]+(?:\.[\w-]+)*\." + TLD + r"(?:[:/]|$)|[\\/][\w-]+(?:\.[\w-]+)*\." + TLD + r"$|^(?:\.\w+[\\/])+\.\w+$", re.I)
IPV4_OR_DATE = re.compile(r"^v?(?:\d{1,3}(?:\.\d{1,3}){3}|\d{1,2}\.\d{1,2}\.\d{4})$")  # 1.2.3.4, 06.11.2026 (not 25.10.15)
URL = re.compile(r"https?://[^\s<>()\[\]`'\"«»]+", re.I)
LOCAL_HOSTS = re.compile(r"^(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|[\w.-]+\.(?:local|localhost|test|example)|"
                         r"(?:www\.)?example\.(?:com|org|net))(?::\d+)?$", re.I)
VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?:\.\d+)?(?![\w.])|\b(?:верси[яиюей]|version|релиз|release)\s+v?(\d+\.\d+(?:\.\d+)?)\b", re.I)
PRICE = re.compile(r"(?:\$|€|£)\s?\d[\d.,]*|\d[\d.,]*\s?(?:\$|€|£|₽|руб\w*|usd|eur|долл\w*|евро)\b", re.I)
EXIT = re.compile(r"Command exited with code (-?\d+)")
BG = re.compile(r"running as a background task with task id:\s*(\S+)")
BG_DONE = re.compile(r'Task id "([^"]+)" finished with result:\s*Command exited with code (-?\d+)')
BG_CUT = re.compile(r'Task id "([^"]+)" finished with result:\s*The tool.s output was truncated')
FAILED_OUT = re.compile(r"\b\d+\s+(?:failed|errors?)\b|^FAILED\b|^FAIL\b|Traceback \(most recent call last\)|\bBUILD FAILED\b|"
                        r"\bnpm ERR!|\berror(?:\[\w+\])?:\s", re.I | re.M)
PASSED_OUT = re.compile(r"\b\d+\s+passed\b|^OK\b|\ball\s+tests\s+passed\b|\bBUILD SUCCEEDED\b|\bTests:\s+\d+\s+passed", re.I | re.M)


def inferred_code(out: str) -> Optional[int]:
    """When Antigravity cut a background command's output, the exit code isn't there: read it off the output."""
    tail = out[-3000:]
    if FAILED_OUT.search(tail) and not re.search(r"\b0\s+failed\b", tail):
        return 1
    return 0 if PASSED_OUT.search(tail) else None

# ─────────────────────────────── questions that need facts ───────────────────────────────

RESEARCH = re.compile(
    r"\b(?:найди|поищи|выясни|узнай|разузнай|почитай|изучи|исследуй|погугли|загугли)\b|\bпровер\w*,?\s+(?:правда|так\s+ли|"
    r"есть\s+ли|работает\s+ли|поддерживает\s+ли|можно\s+ли|существует\s+ли)|\bкак\w*\s+(?:сейчас\s+)?(?:последн\w*|актуальн\w*|"
    r"свеж\w*|текущ\w*|нов\w*)\s+верси|\bкакие\s+(?:есть|существуют)\s+(?:варианты|способы|альтернатив\w*|аналог\w*)|"
    r"\bчто\s+лучше\b|\bсравни\w*|\bсколько\s+стоит|\bкакие\s+(?:лимиты|тарифы|цены)|\bподдерживает\s+ли\b|\bесть\s+ли\s+в\b|"
    r"\bдокументаци\w*|\bchangelog\b|\brelease\s+notes\b|\bbest\s+practices?\b|\bfind\s+out\b|\blook\s+(?:it\s+)?up\b|"
    r"\bresearch\b|\bsearch\s+(?:for|the\s+web)\b|\bwhat(?:'s|\s+is)\s+the\s+(?:latest|current)\b|\bwhich\s+(?:is|one\s+is)\s+better\b|"
    r"\bcompare\b|\bhow\s+much\s+does\b|\bdoes\s+\S+\s+support\b|\bis\s+it\s+true\b", re.I)
CODE_QUESTION = re.compile(
    r"\bгде\s+(?:у\s+нас\s+|в\s+коде\s+|в\s+проекте\s+)?(?:лежит|лежат|находится|находятся|считается|задаётся|задается|хранится|"
    r"хранятся|описан\w*|определ\w*|вызывается|обрабатывается|настраивается)|\bкак\s+(?:у\s+нас\s+|сейчас\s+)?(?:устроен\w*|"
    r"работает|реализован\w*|считается|обрабатывается)|\bоткуда\s+(?:берётся|берется|приходит|приходят)|\bпочему\s+(?:падает|"
    r"не\s+работает|ломается|тормозит|зависает)|\bwhere\s+(?:is|are|do\s+we)\b.*\b(?:defined|handled|stored|computed|set)\b|"
    r"\bhow\s+does\s+(?:our|the|this)\b", re.I)


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("factChecks", True))
    except Exception:
        return True


def workspace(payload: dict) -> str:
    return next((os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), "")


# ─────────────────────────────── what happened in the conversation ───────────────────────────────

def scan(tp: str) -> dict:
    """The conversation as evidence: {"cmds": [{"s", "cmd", "code"}], "edits": [{"s", "path"}], "browser": [step],
    "seen": the text of everything except the agent's own words (user messages, tool results, system messages),
    lowercased, "users": the user's messages}. A background command gets its exit code when it reports back."""
    ev: dict = {"cmds": [], "edits": [], "browser": [], "users": []}
    seen: List[str] = []
    size = 0
    bg: Dict[str, dict] = {}
    pending: List[dict] = []
    try:
        f = open(tp, encoding="utf-8", errors="replace")
    except OSError:
        return dict(ev, seen="")
    with f:
        for raw in f:
            head = raw[:400]
            kind = next((k for k in ("PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE", "USER_INPUT", "EPHEMERAL_MESSAGE",
                                     "CHECKPOINT") if f'"{k}"' in head), "")
            if not kind or (kind == "PLANNER_RESPONSE" and '"tool_calls"' not in raw):
                continue  # the agent's own words are what's being checked, not evidence
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            step = d.get("step_index", 0)
            content = d.get("content") or ""
            if kind == "PLANNER_RESPONSE":
                pending = []
                for c in d.get("tool_calls") or []:
                    if not isinstance(c, dict):
                        continue
                    name = str(c.get("name") or "")
                    a = c.get("args") if isinstance(c.get("args"), dict) else {}
                    item = {"n": name, "s": step}
                    if name == "run_command":
                        item["cmd"] = str(a.get("CommandLine") or "")
                        seen.append(item["cmd"][:2000].lower())
                    elif name in ("write_file", "edit_file"):
                        ev["edits"].append({"s": step, "path": str(a.get("Path") or a.get("TargetFile") or "")})
                    elif "browser" in name:
                        ev["browser"].append(step)
                    pending.append(item)
                continue
            if kind == "GENERIC":
                item = pending.pop(0) if pending else {}
                if item.get("n") == "run_command":
                    m = EXIT.search(content[:800])
                    entry = {"s": step, "cmd": item.get("cmd", ""), "code": int(m.group(1)) if m else None,
                             "out": content.split("Command output:", 1)[-1][-4000:] if m else ""}
                    b = BG.search(content[:800])
                    if b:
                        bg[b.group(1)] = entry
                    ev["cmds"].append(entry)
            elif kind == "USER_INPUT":
                ev["users"].append({"s": step, "text": conv.user_text(content)})
            for m in BG_DONE.finditer(content):
                if m.group(1) in bg:
                    bg[m.group(1)].update(code=int(m.group(2)), out=content[m.end():m.end() + 20_000][-4000:])
            for m in BG_CUT.finditer(content):  # «finished with result: The tool's output was truncated…»: no exit code
                if m.group(1) in bg and bg[m.group(1)]["code"] is None:
                    tail = content[m.end():m.end() + 60_000].split("Log output:", 1)[-1][-4000:]
                    bg[m.group(1)].update(code=inferred_code(tail), out=tail, inferred=True)
            if size < SEEN_LIMIT:
                piece = content[:400_000].lower()
                seen.append(piece)
                size += len(piece)
    ev["seen"] = "\n".join(seen)
    return ev


# ─────────────────────────────── checking the report ───────────────────────────────

def _plain(answer: str) -> str:
    """The report without code blocks (examples there aren't claims)."""
    return re.sub(r"```.*?(?:```|$)", " ", answer or "", flags=re.S)


def sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]


def claims(answer: str) -> List[Tuple[str, str]]:
    """(kind, sentence) for what the report says happened: tests passed, deployed, pushed…"""
    out = []
    for s in sentences(_plain(answer)):
        if s.rstrip().endswith("?") or ATTRIBUTED.search(s) or UNVERIFIED.search(s):
            continue
        for kind, rx, _proof, _after, _ok in CLAIMS:
            m = rx.search(s)
            if not m:
                continue
            before = s[:m.start()]
            if HEDGE_BEFORE.search(before[-60:]) or before.count("«") > before.count("»") or before.count('"') % 2:
                continue  # «чтобы тесты прошли», «если сборка проходит», a quote
            out.append((kind, s.strip()))
    return out


def code_edit_step(ev: dict, ws: str, exts: Tuple[str, ...]) -> int:
    last = -1
    for e in ev["edits"]:
        p = os.path.realpath(e["path"]) if e["path"] else ""
        if p and os.path.splitext(p)[1].lower() in exts and (not ws or p.startswith(ws + os.sep)):
            last = max(last, e["s"])
    return last


def unbacked(answer: str, ev: dict, ws: str, verify: "re.Pattern", code_ext: Tuple[str, ...],
             web_ext: Tuple[str, ...]) -> List[str]:
    """Claims in the report that the conversation doesn't back: no matching command (after the last change, for
    checks), the latest one failed or hasn't finished, or a number of passed tests its output doesn't show. «Запустил
    тесты» only needs the command to have run."""
    out, done = [], set()
    for kind, sentence in claims(answer):
        proof, after, must_pass = next((c[2], c[3], c[4]) for c in CLAIMS if c[0] == kind)
        if kind == "works":
            proof = re.compile(verify.pattern + r"|\bcurl\b|\bwget\b|\bssh\b|deploy|\bhttpie?\b", re.I)
        since = code_edit_step(ev, ws, web_ext if kind == "ui" else code_ext) if after else -1
        if kind == "ui" and any(s > since for s in ev["browser"]):
            continue
        runs = [c for c in ev["cmds"] if c["s"] > since and proof.search(c["cmd"][:500])]
        quote = sentence if len(sentence) <= 120 else sentence[:119] + "…"
        last = runs[-1] if runs else None
        problem = None
        if not last:
            problem = f"no {WHAT[kind]} " + ("after your last change." if after and since >= 0 else "in this conversation.")
        elif not must_pass:
            pass
        elif last["code"] is None:
            problem = f"{WHAT[kind]} is still running or reported no result; wait for it before claiming this."
        elif last["code"] != 0:
            problem = (f"the latest {WHAT[kind]}" + (" after your last change" if after and since >= 0 else "")
                       + f" failed (`{last['cmd'][:80]}` → " + (f"exit {last['code']}" if not last.get("inferred") else
                                                                    "its output reports failures") + ").")
        elif kind == "tests":
            m = NUM_PASSED.search(sentence)
            n = next((g for g in m.groups() if g), None) if m else None
            near = re.compile(rf"\b{n}\b[^\n]{{0,40}}(?:pass|ok\b|прош|тест|test)|(?:pass|ok\b|прош|тест|test|ran)[^\n]{{0,40}}\b{n}\b",
                              re.I) if n else None
            if near and not any(near.search(c.get("out") or "") for c in runs[-3:] if c["code"] == 0):
                lines = [ln.strip() for ln in (last.get("out") or "").splitlines() if SUMMARY.search(ln)]
                problem = f"the latest test output doesn't show {n}: «{lines[-1][:120] if lines else 'no summary line'}»."
        if problem and (kind, problem) not in done:
            done.add((kind, problem))
            out.append(f"«{quote}» — {problem}")
    return out


def _local(target: str, ws: str) -> Optional[str]:
    """A path in the project or the user's home from a link or `code` in the report, or None (URLs, server paths…)."""
    t = target.strip()
    if t.startswith("file://"):
        p = unquote(urlparse(t).path)
        return p[1:] if re.match(r"^/[A-Za-z]:", p) else p
    if re.match(r"^[a-z][\w+.-]*:", t, re.I) and not re.match(r"^[A-Za-z]:[\\/]", t):
        return None  # http:, sidecar:, mailto:, customization:
    if t.startswith("~"):
        return os.path.expanduser(t)
    if os.path.isabs(t) or re.match(r"^[A-Za-z]:[\\/]", t):
        home = os.path.expanduser("~")
        return t if (ws and t.startswith(ws + os.sep)) or t.startswith(home + os.sep) else None  # /etc/… on a server
    return os.path.join(ws, t) if ws else None


def references(answer: str, ev: dict, ws: str, art: str = "") -> List[str]:
    """File links and project paths in the report that don't exist, and line references past the end of a file.
    A relative link may also point into the conversation's artifact folder (task.md, walkthrough.md)."""
    out, done = [], set()
    gone = " ".join(c["cmd"] for c in ev["cmds"] if re.search(r"\b(rm|mv|git\s+(rm|mv)|del|remove-item|move-item)\b", c["cmd"], re.I))
    for line in _plain(answer).splitlines():
        if REMOVED.search(line):
            continue  # «удалил `old.py`», «переименовал a.py в b.py»
        targets = [m.group(2) for m in LINK.finditer(line)]
        targets += [m.group(1) for m in TICKED.finditer(LINK.sub(" ", line)) if PATHLIKE.match(m.group(1))
                    and re.search(r"[\\/]", m.group(1)) and not re.search(r"[<>*{}$|]", m.group(1))
                    and FILEISH.search(LINE_REF.sub("", m.group(1))) and not NOT_A_PATH.search(LINE_REF.sub("", m.group(1)))]
        for raw in targets:
            if raw.startswith("#"):
                continue  # an anchor in the same page
            ref = LINE_REF.search(raw)
            bare = raw[:ref.start()] if ref else raw
            path = _local(unquote(bare), ws)
            if not path or raw in done:
                continue
            done.add(raw)
            name = os.path.basename(path.rstrip("/\\"))
            shown = os.path.relpath(path, ws) if ws and path.startswith(ws + os.sep) else raw
            if not os.path.exists(path) and art and not os.path.isabs(bare) and not bare.startswith(("~", "file:")):
                path = os.path.join(art, bare) if os.path.exists(os.path.join(art, bare)) else path
            if not os.path.exists(path):
                if name and name not in gone:
                    out.append(f"`{shown}` — no such file or folder.")
                continue
            line_no = int(next((g for g in (ref.groups() if ref else ()) if g), 0) or 0)
            if line_no and os.path.isfile(path):
                try:
                    with open(path, "rb") as fh:
                        data = fh.read(LINES_LIMIT)
                    n = data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)
                except OSError:
                    continue
                if line_no > n:
                    out.append(f"`{shown}` — the file has only {n} lines.")
    return out


def _host_path(url: str) -> Tuple[str, str]:
    p = urlparse(url.rstrip(".,;:!?)»"))
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return host, (host + p.path.rstrip("/")).lower()


def _in_repo(ws: str, needle: str) -> bool:
    if not ws or not needle:
        return False
    try:
        r = subprocess.run(["git", "grep", "--untracked", "-q", "-i", "-F", "-e", needle], cwd=ws, capture_output=True,
                           timeout=4)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sources(answer: str, ev: dict, ws: str) -> List[str]:
    """Links in the report that came from no search, page, file, user message or the project itself."""
    out = []
    users = " ".join(u["text"] for u in ev["users"]).lower()
    for url in dict.fromkeys(u.rstrip(".,;:!?)»") for u in URL.findall(_plain(answer))):
        host, hp = _host_path(url)
        if not host or LOCAL_HOSTS.match(host) or hp in ev["seen"]:
            continue
        if host in users or re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", host) or _in_repo(ws, hp):
            continue  # the user's own service (they named it, an IP) or a link the project itself has
        out.append(f"{url} — this link didn't come from any search, page, file or message here.")
    return out


def recalled(answer: str, ev: dict) -> List[str]:
    """In an answer to the user's question: versions and prices found nowhere in the conversation."""
    found = []
    for s in sentences(_plain(answer)):
        if UNVERIFIED.search(s):
            continue
        for m in list(VERSION.finditer(s)) + list(PRICE.finditer(s)):
            token = (m.group(m.lastindex) if m.lastindex else m.group(0)).strip().rstrip(".,;:").lower()
            if token and not IPV4_OR_DATE.match(token) and token not in ev["seen"] and token not in found:
                found.append(token)
    return found


def problems(payload: dict, st: dict, answer: str, verify: "re.Pattern", code_ext: Tuple[str, ...],
             web_ext: Tuple[str, ...]) -> List[str]:
    tp = conv.transcript_path(payload)
    if not tp or not (answer or "").strip():
        return []
    plain = _plain(answer)
    quick = any(c[1].search(plain) for c in CLAIMS) or LINK.search(plain) or "`" in plain or URL.search(plain) \
        or VERSION.search(plain) or PRICE.search(plain)
    if not quick:
        return []  # nothing in the report to check: don't read the transcript
    ws = workspace(payload)
    ev = scan(tp)
    art = conv.to_path(payload.get("artifactDirectoryPath") or "") if payload.get("artifactDirectoryPath") else ""
    out = unbacked(answer, ev, ws, verify, code_ext, web_ext) + references(answer, ev, ws, art) + sources(answer, ev, ws)
    users = st.get("users") or []
    if users and (RESEARCH.search(users[-1]["text"]) or "?" in users[-1]["text"]):
        facts = recalled(answer, ev)
        if facts:
            out.append(f"{', '.join(facts[:6])} — versions or prices that appear nowhere in this conversation: look "
                       "them up and cite the source, or mark them as unverified («по памяти, не проверял»).")
    return out


def stop_message(payload: dict, st: dict, answer: str, verify: "re.Pattern", code_ext: Tuple[str, ...],
                 web_ext: Tuple[str, ...]) -> Optional[str]:
    """For hooks/stop.py: what in the final report isn't backed, once per user message (subagents: per report).
    Updates st["facts"] (the caller stores st)."""
    if not setting():
        return None
    found = problems(payload, st, answer, verify, code_ext, web_ext)
    if not found:
        return None
    mark = len(st.get("users") or []) or -1  # main agent: per user message; a subagent: per conversation
    seen = st.setdefault("facts", {})
    if seen.get("asked") != mark:
        seen.update(asked=mark, sigs=[])
    sig = hashlib.sha1("\n".join(sorted(found)).encode()).hexdigest()[:12]
    if sig in seen["sigs"]:
        return None  # it already decided to stop with exactly these
    seen["sigs"] = (seen["sigs"] + [sig])[-20:]
    shown = "\n".join(f"- {p}" for p in found[:SHOWN]) + (f"\n- … and {len(found) - SHOWN} more" if len(found) > SHOWN else "")
    return ("Antigravity Plus — facts: parts of your report aren't backed by anything in this conversation:\n" + shown +
            "\nFix the report before you finish: check what you can now (run it, open it, search for it), correct or "
            "remove the rest, and say plainly what you couldn't confirm. Then end your turn again.")


# ─────────────────────────────── before the next model call ───────────────────────────────

def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """A <grounding> hint after a user message that needs facts from outside or from the code (main agent; once per
    message). The caller stores st."""
    if not st or not st.get("users") or not setting():
        return None
    users = conv.with_current(st, payload)
    seen = st.setdefault("facts", {})
    if len(users) <= seen.get("hint", 0):
        return None
    seen["hint"] = len(users)
    text = users[-1]["text"]
    if re.match(r"^\s*/", text):
        return None
    if RESEARCH.search(text):
        body = ("This needs facts from outside the conversation. Look them up before you answer — the `researcher` "
                "subagent (Model: flash) or search_web — and read the primary source (official docs, the changelog, the "
                "repository, the vendor's pricing page), not a search snippet. Cite the link next to each fact, with the "
                "date for versions, prices and limits. If you couldn't confirm something, say so («не нашёл "
                "подтверждения»): never fill a gap with a guess.")
    elif CODE_QUESTION.search(text):
        body = ("Answer from the code, not from memory: find it with `rg -n`, read those lines, and cite `file:line` for "
                "each claim. Run it or read the logs when the question is about behaviour. If it isn't in the code, say so.")
    else:
        return None
    return f'<grounding source="Antigravity Plus">\n{body}\n</grounding>'


# ─────────────────────────────── for the guard ───────────────────────────────

FAKE_CHECK = re.compile(
    r"^\s*(?:echo|printf|write-host|write-output)\s+(?:-\w+\s+)*[\"']?[^\"'|;&>]*\b(?:pass(?:ed|es)?|success(?:ful(?:ly)?)?|"
    r"succeeded|works|verified|all\s+good|прошл[иао]?|пройден\w*|успешн\w*|работает|проверк\w*)\b[^\"'|;&>]*[\"']?\s*$"
    r"|^\s*(?:python3?|py)\s+-c\s+[\"']\s*print\(\s*[fr]?[\"'][^\"']*\b(?:pass(?:ed|es)?|success\w*|succeeded|ok|works|verified|"
    r"прошл\w*|работает|успешн\w*)\b[^\"']*[\"']\s*\)\s*[\"']\s*$"
    r"|^\s*node\s+-e\s+[\"']\s*console\.log\(\s*[\"'`][^\"'`]*\b(?:pass(?:ed|es)?|success\w*|ok|works|verified)\b[^\"'`]*"
    r"[\"'`]\s*\)\s*;?\s*[\"']\s*$", re.I)


def fake_check(cmd: str) -> Optional[str]:
    """A command that only prints success and checks nothing (`echo "tests passed"`, `python3 -c "print('OK')"`)."""
    if not FAKE_CHECK.search(cmd or "") or not setting():
        return None
    return ("🛡️ Antigravity Plus: эта команда только печатает «успех» и ничего не проверяет. Запустите настоящую проверку "
            "(тесты, сборку, саму программу) или прямо скажите в отчёте, что проверить не удалось.")
