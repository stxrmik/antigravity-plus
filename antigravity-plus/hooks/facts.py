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
A test run, a build or a linter run is a command that runs one (runs_named()): `ls tests/` or `rg -n "def test_"
tests/` only name tests. A relative path in the report may start at a subfolder of the project (`mcp/server.py` for
`plugin/mcp/server.py`), and a git ref (`origin/v1.2.0`) isn't a path.
Setting factChecks (`memory.py config fact-checks off`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import compat
import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SHOWN = 6                 # problems listed in one message
SEEN_LIMIT = 12_000_000   # chars of the conversation kept to look sources up in
LINES_LIMIT = 5_000_000   # bytes read to count a file's lines
FIND_TIME = 2.0           # seconds the search for a path in the project's subfolders may take (hooks have 10 s)
FIND_BYTES = 2_000_000    # bytes of `git ls-files` output read
WALK_LIMIT = 30_000       # files and folders a walk of a project that isn't a git repository looks at
# Folders a walk leaves out: git's insides, dependencies and build output.
SKIP_DIRS = frozenset({".git", "node_modules", "build", "dist", ".venv", "venv", "DerivedData", ".gradle", "Pods",
                       "__pycache__", ".tox", ".next", "target"})

# ─────────────────────────────── claims in the agent's report ───────────────────────────────

# kind → (claim, the command that backs it, does it have to come after the last change?)
TEST_CMD = re.compile(r"(?<!la)test|spec\b|jest|vitest|mocha|phpunit|rspec|ctest", re.I)  # not `npm i x@latest`
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
    ("ci",
     re.compile(
         r"\b(?:ci|пайплайн\w*|pipeline|github\s+actions?|gitlab\s+ci|(?:ci|actions|github)\s+workflows?)\s+"
         r"(?:(?:тоже|все|теперь|снова|уже|успешно|полностью)\s+)?"
         r"(?:прош(?:ли|ёл|ел|ло)|проход\w*|зел[её]н\w*|успешн\w*|чист\w*|ok\b|passed|succeeded|is\s+green|are\s+green)"
         r"|\b(?:все\s+)?(?:(?:проверки|джобы)\s+в\s+(?:ci|pr|mr|github|gitlab)\s+|экшены\s+(?:в\s+(?:ci|pr|github)\s+)?)"
         r"(?:успешно\s+)?(?:прош(?:ли|ёл|ел)|зел[её]н\w*)"
         r"|\ball\s+(?:ci|pr|mr|github|gitlab|pipeline)\s+checks?\s+(?:have\s+)?pass(?:ed|ing)?\b", re.I),
     re.compile(r"(?<![\w-])ci\.py[\"']?\s+(?:watch|status)\b|\bgh\s+(?:run\s+(?:watch|view|list)|pr\s+checks)\b"
                r"|\bglab\s+ci\s+(?:status|view|trace|get|list)\b", re.I),
     True, True),
    ("install", re.compile(r"\bустановил\w*\s+(?:зависимост\w*|пакет\w*|библиотек\w*|модул\w*)|\binstalled\s+(?:the\s+)?(?:dependencies|packages?)\b", re.I),
     re.compile(r"\b(?:pip3?|npm|pnpm|yarn|bun|brew|apt(?:-get)?|uv|poetry|gem|cargo|go|composer|pod)\s+(?:install|add|i|sync|get)\b", re.I),
     False, True),
    ("perf",
     re.compile(
         r"\bускорил\w*\s+(?:[\w.-]+\s+){0,3}(?:в\s+|на\s+)?\d+(?:[.,]\d+)?\s*(?:раз\w*|x|х|%)"
         r"|\bстал\w*\s+быстрее\s+(?:в\s+|на\s+)\d+(?:[.,]\d+)?\s*(?:раз\w*|x|х|%)"
         r"|\bснизил\w*\s+(?:потребление\s+|расход\s+)?(?:памят\w*|размер\w*|время|задержк\w*)\s+на\s+\d+(?:[.,]\d+)?%"
         r"|\b\d+(?:[.,]\d+)?\s*[xх]\s+faster\b"
         r"|\b(?:sped\s+up|faster|reduced\s+(?:latency|memory|bundle\s+size|runtime))\s+by\s+\d+(?:[.,]\d+)?%",
         re.I,
     ),
     re.compile(r"perf\.py|hyperfine|pytest-benchmark|--benchmark|\btimeit\b|cProfile|perf_counter|(?<![-\w])time\s+\S|wrk\b|"
                r"\bab\s+-n|\bk6\b|lighthouse|\bdu\s+-|\bwc\s+-c|\bstat\s+", re.I),
     True, True),
]
# perf.py's verdict line (`… -> Verdict: clear improvement`) or its --json `"verdict": "…"`.
PERF_VERDICT = re.compile(r"""(?:\bVerdict:[ \t]*|"verdict"\s*:\s*")([^\n"]+)""")
WHAT = {"tests": "test run", "tests_ran": "test command", "build": "build", "build_ran": "build command",
        "lint": "linter run", "works": "check (tests, a build, running it)",
        "ui": "look at the page (ui_check.py screenshots or the browser)",
        "mobile": "run on a simulator or an emulator (mobile.py run / shot)", "deploy": "deploy command",
        "push": "`git push`", "commit": "`git commit`", "ci": "CI check (`ci.py watch` / `gh run`)",
        "install": "install command", "perf": "benchmark measurement (perf.py / hyperfine / timeit)"}
# A claim the sentence hedges, attributes, asks about or quotes isn't the agent's own statement of fact.
HEDGE_BEFORE = re.compile(r"(?:чтобы|если|когда|пока|пусть|должн\w*|может|могут|можно|стоит|нужно|надо|осталось|ожида\w*|"
                          r"предполага\w*|предлагаю|рекомендую|запустите|проверьте|выполните|попробуйте|прогоните|не|to\s+make|"
                          r"so\s+that|if|once|should|will|would|could|can|expect\w*|needs?|not|n't|run|try|consider)\W+"
                          r"(?:\w+\W+){0,3}$", re.I)
# «29 passed», «29 тестов прошли», «29/29 тестов»: a number the latest test output has to show.
NUM_PASSED = re.compile(r"\b(\d+)\s+(?:passed|tests?\s+passed|тест\w*\s+(?:(?:успешно|тоже|уже)\s+)?(?:прош\w*|проход\w*|зел[её]н\w*))|\b(\d+)\s*(?:/|из)\s*\d+\s+"
                        r"(?:тест\w*|tests?)\b|\bавтотест\w*\s*[:—–-]\s*(\d+)", re.I)
SUMMARY = re.compile(r"\b(?:passed|failed|errors?|ok|ran\s+\d+|tests?:|прошл\w*|упал\w*)\b", re.I)
# A test runner's count in its output: «27 passed», «44 of 44 passed», «Ran 27 tests», «Tests: 27 passed», «27 passing»,
# «Executed 27 tests», «27 examples, 0 failures», «OK (27 tests)». An output without one (a later run of something
# else) says nothing about how many passed.
COUNTED = re.compile(r"\b\d+\s+(?:passed|passing|failed|failing|tests?\s+(?:passed|completed|ran|run|ok))\b"
                     r"|\bRan\s+\d+\s+tests?\b|\b\d+\s+(?:of|из)\s+\d+\s+(?:passed|tests?\b|тест\w*)|\bTests?:\s+\d+"
                     r"|\bExecuted\s+\d+\s+tests?\b|\b\d+\s+examples?,\s+\d+\s+failures?|\bOK\s+\(\d+\s+tests?"
                     r"|\b\d+\s+тест\w*", re.I)
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
# …that isn't there may still be a git ref: a remote's branch or tag (`origin/v0.24.0`, `upstream/3.x`), refs/…, HEAD~2
# — or another ref git knows here (`fork/v1.2.0`, asked of git only for a name without a file extension like `.py`).
GIT_REF = re.compile(r"^(?:(?:origin|upstream)/|refs/)[\w.@/+-]+$|^HEAD(?:[~^]\d*|@\{[^{}\s]*\})+$")
LETTER_EXT = re.compile(r"\.(?!x$)[A-Za-z][A-Za-z0-9]{0,7}$")  # `.py`, `.c` — not `2.0.x` (a version branch)
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


def workspaces(payload: dict) -> List[str]:
    return [os.path.realpath(conv.to_path(w)) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w]


def workspace(payload: dict) -> str:
    return next(iter(workspaces(payload)), "")


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


_QUOTED_ARG = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SHELL_WRAP = re.compile(r"\b(?:sh|bash|zsh|dash)\s+-c\b|\bcmd(?:\.exe)?\s+/c\b", re.I)
PUSH_CMD = re.compile(
    r"(?:^|[;&|\n]\s*)(?:(?:cd|pushd)\s+[^;&|\n]+(?:&&|;)\s*)*(?:env\s+\S+=\S+\s+)*"
    r"(?:git(?:\.exe)?\s+(?:-[^\s;|&]+\s+(?:Q\s+)?)*push\b(?!.*\s(?:-n|--dry-run|--delete|-d|--tags)\b)(?!.*\s:[\w./-]+)"
    r"|gh(?:\.exe)?\s+pr\s+create\b|glab(?:\.exe)?\s+mr\s+create\b)", re.I
)
CI_RUNNING_OUT = re.compile(
    r'⏳\s*CI|"status":\s*"(?:in_progress|queued|pending|waiting|requested)"|\bin_progress\b'
    r'|\b[1-9]\d*\s+pending\s+checks?\b|(?:status|state):\s*(?:running|pending)\b', re.I
)
CI_FAILED_OUT = re.compile(
    r'❌\s*CI|"conclusion":\s*"(?:failure|cancelled|timed_out|startup_failure|action_required)"'
    r'|"status":\s*"(?:failed|canceled)"|completed with \'(?:failure|cancelled|timed_out|startup_failure|action_required)\''
    r'|\b[1-9]\d*\s+failing\b|(?:^|\n)\s*[X✗×]\s+\S|(?:pipeline\s+state|status):\s*(?:failed|canceled)\b', re.I
)
CI_NONE_OUT = re.compile(r'ℹ️\s*No CI run|ℹ️\s*CI cannot be queried|\bno (?:runs?|pipelines?) found\b', re.I)


def _is_push_cmd(cmd: str) -> bool:
    s = cmd[:600]
    cleaned = s if _SHELL_WRAP.search(s) else _QUOTED_ARG.sub("Q", s)
    return bool(PUSH_CMD.search(cleaned))


# ─────────────────────────────── what a command runs ───────────────────────────────

# Programs that only list, read, search, print, compare or move files: `ls tests/`, `rg -n "def test_" tests/`,
# `sed -n 1,40p tests/conftest.py` or `mkdir -p tests/fixtures` run no tests, build or linter, whatever they name.
TEXT_TOOLS = frozenset(
    "ls cat head tail less more bat sed awk gawk rg grep egrep fgrep ag ack find fd wc stat file tree du df echo printf "
    "diff cmp comm sort uniq cut tr jq yq nl column basename dirname realpath readlink which whereis type test [ [[ true "
    "false pwd popd read mkdir touch cp mv rm rmdir ln chmod tee xxd od hexdump strings md5 md5sum shasum sha256sum date "
    "sleep clear export unset alias open dir get-content get-childitem select-string".split())
# Words before the program: shell keywords and wrappers that run the rest as it is.
_KEYWORDS = frozenset("if then else elif fi do done while until esac time ! { } exec command builtin nohup noglob".split())
# Wrappers with options, and the options that take a value.
_WRAPPED = {"env": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"),
            "sudo": ("-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D"), "doas": ("-u", "-C"),
            "nice": ("-n", "--adjustment"), "timeout": ("-s", "--signal", "-k", "--kill-after"),
            "xargs": ("-I", "-n", "-P", "-L", "-d", "-s", "-E", "-a", "--max-args", "--max-procs", "--delimiter",
                      "--arg-file", "--max-lines", "--replace")}
_SHELLS = frozenset("sh bash zsh dash ksh fish pwsh powershell cmd".split())
_SSH_VALUE = frozenset("-b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w -B".split())
_GIT_VALUE = frozenset("-C -c --git-dir --work-tree --namespace".split())
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)(\w+)\1")
# Reading what a test run wrote: `tail -50 /tmp/pytest.log` after `pytest > /tmp/pytest.log`, or a log named after tests.
READERS = frozenset("cat tail head less more bat grep egrep rg sed awk type get-content select-string".split())
LOG_FILE = re.compile(r"\.(?:log|out|txt)$", re.I)
REDIRECT = re.compile(r"(?:^|[\s;&|])(?:\d|&)?>>?\s*([^\s;&|<>()'\"]+)|\btee\s+(?:-\w+\s+)*([^\s;&|<>()'\"]+)")


def _simple_commands(cmd: str) -> List[str]:
    """A command line's simple commands: split at ; & && || | ( ) ` and line breaks outside quotes, with heredoc bodies
    left out (data, not commands: a test file written with `cat > tests/test_x.py <<'EOF'`)."""
    lines: List[str] = []
    end: Optional[str] = None
    for ln in cmd[:20_000].split("\n"):
        if end is not None:
            end = None if ln.strip() == end else end
            continue
        lines.append(ln)
        m = _HEREDOC.search(ln)
        end = m.group(2) if m else None
    text = "\n".join(lines)[:4000]
    out: List[str] = []
    cur: List[str] = []
    quote, i = "", 0
    esc = not compat.WINDOWS  # a backslash separates a path's parts there: it escapes nothing
    while i < len(text):
        c = text[i]
        if c == "\\" and esc and quote != "'":
            cur.append(text[i:i + 2])
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in ";|()`\n" or (c == "&" and text[i - 1:i] != ">" and text[i + 1:i + 2] != ">"):  # not 2>&1, &>
            out += ["".join(cur)] + ([c] if c in "()" else [])  # a subshell's bounds: a `cd` in it stays there
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _argv(seg: str) -> List[str]:
    """A simple command's words past variable assignments, shell keywords and wrappers (`env X=1`, `sudo -u app`,
    `time`, `nice -n 5`, `timeout 60`, `xargs -n1`): the program it runs and its arguments."""
    try:
        words = shlex.split(seg.replace("\\", "/") if compat.WINDOWS else seg, posix=True)
    except ValueError:  # an unclosed quote
        words = seg.split()
    i = 0
    while i < len(words):
        w = words[i].lower()
        if w == "command" and words[i + 1:i + 2] in (["-v"], ["-V"]):
            return ["which"]  # `command -v pytest` looks a program up
        if re.match(r"^[A-Za-z_]\w*=", words[i]) or w in _KEYWORDS:
            i += 1
        elif w in _WRAPPED:
            i += 1
            while i < len(words) and (words[i].startswith("-") or (w == "env" and "=" in words[i])):
                i += 2 if words[i] in _WRAPPED[w] else 1
            i += 1 if w == "timeout" else 0  # its duration
        else:
            break
    return words[i:]


def _prog(word: str) -> str:
    """The program's name as TEXT_TOOLS knows it; a script of the project (`./test`, `bin/lint`) keeps its path."""
    w = word.replace("\\", "/")
    if "/" in w and not re.match(r"^/(?:usr/(?:local/)?)?s?bin/|^/opt/homebrew/bin/", w):
        return w.lower()
    return re.sub(r"\.(?:exe|cmd|bat)$", "", w.rsplit("/", 1)[-1].lower())


def _inner(prog: str, args: List[str]) -> Optional[Tuple[str, str]]:
    """What a wrapper runs, and on what: the command of `bash -c '…'`, `ssh host '…'`, `git bisect run …`, `git rebase
    -x …` and `find tests … -exec …` (with find's own arguments: the files it runs come from there) — "" for git's
    other commands and a find that runs nothing — or None for any other program."""
    if prog in _SHELLS:
        for j, a in enumerate(args):
            if prog in ("pwsh", "powershell"):
                if re.fullmatch(r"-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?", a, re.I):
                    return " ".join(args[j + 1:]), ""
            elif prog == "cmd":
                if a.lower() in ("/c", "/k"):
                    return " ".join(args[j + 1:]), ""
            elif re.fullmatch(r"-[a-z]*c[a-z]*", a):  # -c, -lc, -ec
                return (args[j + 1] if j + 1 < len(args) else ""), ""
            if not a.startswith("/" if prog == "cmd" else "-"):
                break
        return None  # `bash tests/run.sh`: the script is what it runs
    if prog == "ssh":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in _SSH_VALUE else 1
        return " ".join(args[j + 1:]), ""  # past the host; "" for a login
    if prog == "git":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in _GIT_VALUE else 1
        sub, rest = (args[j], args[j + 1:]) if j < len(args) else ("", [])
        if sub == "bisect" and rest[:1] == ["run"]:
            return " ".join(rest[1:]), ""
        if sub == "rebase":
            k = next((k for k, a in enumerate(rest[:-1]) if a in ("-x", "--exec")), -1)
            return (rest[k + 1] if k >= 0 else ""), ""
        return "", ""
    if prog in ("find", "fd"):
        runs = ("-exec", "-execdir", "-ok", "-okdir") if prog == "find" else ("-x", "-X", "--exec", "--exec-batch")
        for j, a in enumerate(args):
            if a in runs:
                end = next((k for k in range(j + 1, len(args)) if args[k] in (";", "+")), len(args))
                return " ".join(args[j + 1:end]), " ".join(args[:j])
        return "", ""
    return None


def runs_named(cmd: str, proof: "re.Pattern", where: str = "", depth: int = 0) -> bool:
    """Whether the command runs what `proof` names (tests, a build, a linter) in one of its simple commands — not in an
    argument of a program that only lists, reads or prints text: `ls tests/` and `rg -n "def test_" tests/` aren't
    test runs. The folder a `cd` went to counts for the commands after it (`cd tests && ./run.sh`), and the command of
    `bash -c '…'`, `ssh host '…'` or `find -exec` is looked at as a command of its own."""
    outer: List[str] = []
    for seg in _simple_commands(cmd):
        if seg == "(":
            outer.append(where)
            continue
        if seg == ")":
            where = outer.pop() if outer else where
            continue
        argv = _argv(seg)
        if not argv:
            continue
        prog = _prog(argv[0])
        if prog in ("cd", "pushd", "for", "select"):
            where = (where if prog in ("for", "select") else "") + " " + " ".join(argv[1:])
            continue
        inner = _inner(prog, argv[1:])
        if inner is not None:
            if inner[0] and depth < 3 and runs_named(inner[0], proof, where + " " + inner[1], depth + 1):
                return True
        elif prog not in TEXT_TOOLS and proof.search((where + " " + " ".join(argv))[:1000]):
            return True
    return False


def _written(cmd: str) -> List[str]:
    """Names of the files a command sends its output to (`> out.txt 2>&1`, `| tee test.log`)."""
    names = []
    for m in REDIRECT.finditer(cmd[:4000]):
        target = m.group(1) or m.group(2) or ""
        if target and not target.startswith("/dev/"):
            names.append(os.path.basename(target.replace("\\", "/")))
    return names


def _reads_log(cmd: str, proof: "re.Pattern", written: set) -> bool:
    """Whether the command reads what a test run wrote: a file an earlier test run sent its output to, or a log named
    after tests (`tail -50 test-output.log`)."""
    for seg in _simple_commands(cmd):
        argv = _argv(seg)
        if not argv or _prog(argv[0]) not in READERS:
            continue
        for a in argv[1:]:
            name = os.path.basename(a.replace("\\", "/"))
            if not a.startswith("-") and (name in written or (LOG_FILE.search(name) and proof.search(name))):
                return True
    return False


def code_edit_step(ev: dict, ws: str, exts: Tuple[str, ...]) -> int:
    last = -1
    for e in ev["edits"]:
        p = os.path.realpath(e["path"]) if e["path"] else ""
        if p and os.path.splitext(p)[1].lower() in exts and (not ws or p.startswith(ws + os.sep)):
            last = max(last, e["s"])
    return last


def push_step(ev: dict) -> int:
    return max((c["s"] for c in ev["cmds"] if c.get("code") == 0 and _is_push_cmd(c["cmd"])), default=-1)


RUN_KINDS = ("tests", "tests_ran", "build", "build_ran", "lint")  # backed only by a command that runs one


def _summary_line(out: str, rx: "re.Pattern" = SUMMARY) -> str:
    lines = [ln.strip() for ln in out.splitlines() if rx.search(ln)]
    return lines[-1][:120] if lines else "no summary line"


def count_problem(n: str, ev: dict, runs: List[dict], proof: "re.Pattern") -> Optional[str]:
    """«44 passed» against the outputs of the successful test runs after the last change (`runs`, oldest first) and of
    the logs they wrote: any of them that shows the number backs the claim — the code hasn't changed since, so a later,
    smaller run (one suite after the whole set) doesn't undo it, and an output with no count (a later run of something
    else) doesn't hide it. A number no run after the change shows is reported with the latest count there is."""
    near = re.compile(rf"\b{n}\b[^\n]{{0,40}}(?:pass|ok\b|прош|тест|test)|(?:pass|ok\b|прош|тест|test|ran)[^\n]{{0,40}}\b{n}\b",
                      re.I)
    ran = {id(r) for r in runs}
    shown: List[dict] = []
    written: set = set()
    for c in ev["cmds"]:  # in the order they ran
        if id(c) in ran:
            shown += [c] if c["code"] == 0 else []
            written.update(_written(c["cmd"]))
        elif runs and c["s"] > runs[0]["s"] and c.get("code") == 0 and _reads_log(c["cmd"], proof, written):
            shown.append(c)
    counted = None
    for c in reversed(shown):
        out = c.get("out") or ""
        if near.search(out):
            return None
        if counted is None and COUNTED.search(out):
            counted = out
    if counted is not None:
        return f"the latest test output doesn't show {n}: «{_summary_line(counted, COUNTED)}»."
    return f"the latest test output doesn't show {n}: «{_summary_line(runs[-1].get('out') or '')}»."


def unbacked(answer: str, ev: dict, ws: str, verify: "re.Pattern", code_ext: Tuple[str, ...],
             web_ext: Tuple[str, ...]) -> List[str]:
    """Claims in the report that the conversation doesn't back: no matching command (after the last change, for
    checks), the latest one failed or hasn't finished, or a number of passed tests its output doesn't show. «Запустил
    тесты» only needs the command to have run. Tests, builds and linters are backed by a command that runs them
    (runs_named()), not by one that only names them (`ls tests/`)."""
    out, done = [], set()
    named: Dict[Tuple[str, str], bool] = {}

    def backs(c: dict, kind: str, proof: "re.Pattern") -> bool:
        if kind not in RUN_KINDS:
            return bool(proof.search(c["cmd"][:500]))
        key = (proof.pattern, c["cmd"])
        if key not in named:
            named[key] = runs_named(c["cmd"], proof)
        return named[key]

    for kind, sentence in claims(answer):
        proof, after, must_pass = next((c[2], c[3], c[4]) for c in CLAIMS if c[0] == kind)
        if kind == "works":
            proof = re.compile(verify.pattern + r"|\bcurl\b|\bwget\b|\bssh\b|deploy|\bhttpie?\b", re.I)
        since = code_edit_step(ev, ws, web_ext if kind == "ui" else code_ext) if after else -1
        if kind == "ci":
            since = max(since, push_step(ev))
        if kind == "ui" and any(s > since for s in ev["browser"]):
            continue
        runs = [c for c in ev["cmds"] if c["s"] > since and backs(c, kind, proof)]
        quote = sentence if len(sentence) <= 120 else sentence[:119] + "…"
        last = runs[-1] if runs else None
        problem = None
        after_what = "after your last push/change" if kind == "ci" else "after your last change"
        if not last:
            problem = f"no {WHAT[kind]} " + (f"{after_what}." if after and since >= 0 else "in this conversation.")
        elif not must_pass:
            pass
        elif last["code"] is None:
            problem = f"{WHAT[kind]} is still running or reported no result; wait for it before claiming this."
        elif last["code"] != 0:
            problem = (f"the latest {WHAT[kind]}" + (f" {after_what}" if after and since >= 0 else "")
                       + f" failed (`{last['cmd'][:80]}` → " + (f"exit {last['code']}" if not last.get("inferred") else
                                                                    "its output reports failures") + ").")
        elif kind == "ci":
            out_txt = last.get("out") or ""
            if CI_FAILED_OUT.search(out_txt) and "✅ CI passed" not in out_txt:
                problem = f"the latest {WHAT[kind]} output shows a failure (`{last['cmd'][:80]}`)."
            elif CI_RUNNING_OUT.search(out_txt) and "✅ CI passed" not in out_txt:
                problem = f"the latest {WHAT[kind]} output shows CI is still running (`{last['cmd'][:80]}`); wait with `ci.py watch`."
            elif CI_NONE_OUT.search(out_txt) or (out_txt.strip() in ("", "[]") and re.search(r"\b(?:list|status|checks)\b", last["cmd"], re.I)):
                problem = f"the latest {WHAT[kind]} found no CI run (`{last['cmd'][:80]}`); wait with `ci.py watch` or verify CI triggered."
        elif kind == "perf":  # perf.py exits 0 for any verdict: a speed-up claim needs a «clear improvement» one
            verdicts = [v.strip() for v in PERF_VERDICT.findall(last.get("out") or "")]
            if verdicts and not any(v.lower().startswith("clear improvement") for v in verdicts):
                problem = (f"the latest perf.py measurement doesn't show a clear improvement "
                           f"(«Verdict: {verdicts[-1][:100]}», `{last['cmd'][:60]}`).")
        elif kind == "tests":
            m = NUM_PASSED.search(sentence)
            n = next((g for g in m.groups() if g), None) if m else None
            if n:
                problem = count_problem(n, ev, runs, proof)
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


def _line_count(path: str) -> Optional[int]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(LINES_LIMIT)
    except OSError:
        return None
    return data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)


def _bounded(cmd: List[str], cwd: str, timeout: float, limit: int) -> Tuple[Optional[bytes], bool]:
    """A command's output, stopped after `timeout` seconds or `limit` bytes: (stdout, whether it finished), or (None,
    True) when it can't run here or fails by itself (no git, not a repository)."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except OSError:
        return None, True
    cut: List[bool] = []

    def stop() -> None:
        cut.append(True)
        try:
            p.kill()
        except OSError:
            pass

    timer = threading.Timer(max(timeout, 0.05), stop)
    timer.daemon = True
    timer.start()
    data = b""
    try:
        data = p.stdout.read(limit + 1) if p.stdout else b""
        if len(data) > limit:
            stop()
        p.wait()  # the timer still guards this
    except (OSError, ValueError):
        stop()
        try:
            p.wait(1)
        except subprocess.TimeoutExpired:
            pass
    finally:
        timer.cancel()
        if p.stdout:
            p.stdout.close()
    if cut:
        return data[:limit], False
    return (data, True) if p.returncode == 0 else (None, True)


def _git_listed(root: str, rels: List[str], timeout: float) -> Optional[Tuple[List[str], bool]]:
    """The project's files (tracked and untracked, not ignored: `git ls-files`) under a subfolder that end with one of
    `rels` or lie in such a folder: ([paths relative to root], whether git listed them all), or None outside a git
    repository. Git does the matching (`*/mcp/server.py`), so only those come back."""
    specs: List[str] = []
    for rel in rels:
        glob = re.sub(r"([*?\[])", r"[\1]", rel)
        specs += ["*/" + glob, "*/" + glob + "/*"]
    data, whole = _bounded(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"] + specs, root,
                           timeout, FIND_BYTES)
    if data is None:
        return None
    return [f for f in data.decode("utf-8", "replace").split("\0") if f], whole


def _walk(root: str, rels: List[str], deadline: float, hits: Dict[str, List[str]]) -> bool:
    """Files and folders under `root` whose path ends with one of `rels`, into hits; whether the walk saw all of it
    (it leaves out SKIP_DIRS and stops after WALK_LIMIT entries or at the deadline)."""
    by_name: Dict[str, List[str]] = {}
    for rel in rels:
        by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    seen = 0
    for folder, dirs, files in os.walk(root):
        here = os.path.relpath(folder, root).replace(os.sep, "/")
        here = "" if here == "." else here + "/"
        for n in dirs + files:
            for rel in by_name.get(n, ()):
                if ("/" + here + n).endswith("/" + rel):
                    hits.setdefault(rel, []).append(os.path.join(folder, n))
        seen += len(dirs) + len(files)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if seen > WALK_LIMIT or time.monotonic() > deadline:
            return False
    return True


def in_subfolders(roots: List[str], rels: List[str]) -> Tuple[Dict[str, List[str]], bool]:
    """Files and folders of the project whose path ends with one of `rels`: the agent wrote `mcp/server.py` for
    `<root>/plugin/mcp/server.py`. {rel: [paths]}, and whether the search went through all of every root — git's list
    of files in a git repository, else a walk of the folders; both stop after FIND_TIME seconds."""
    hits: Dict[str, List[str]] = {}
    whole = True
    deadline = time.monotonic() + FIND_TIME
    for root in roots:
        left = deadline - time.monotonic()
        if left <= 0:
            return hits, False
        listed = _git_listed(root, rels, left)
        if listed is None:
            whole = _walk(root, rels, deadline, hits) and whole
            continue
        files, ok = listed
        whole = whole and ok
        for f in files:
            s = "/" + f
            for rel in rels:
                if s.endswith("/" + rel):
                    found = f
                else:
                    k = s.find("/" + rel + "/")
                    if k < 0:
                        continue
                    found = s[1:k + 1 + len(rel)]  # the folder
                path = os.path.join(root, *found.split("/"))
                if path not in hits.get(rel, []) and len(hits.get(rel, [])) < 5 and os.path.exists(path):
                    hits.setdefault(rel, []).append(path)
    return hits, whole


def is_git_ref(token: str, ws: str) -> bool:
    """`fork/v1.2.0`: a branch, remote branch or tag git knows in the workspace (`git rev-parse --verify`)."""
    if not ws or token.startswith("-") or re.search(r"\s", token):
        return False
    data, whole = _bounded(["git", "rev-parse", "--verify", "--quiet", token], ws, 1.0, 4096)
    return bool(data and whole)


def _settle(lost: List[Tuple[int, str, str, int]], found: List[Optional[str]], roots: List[str], ws: str) -> None:
    """Relative paths that are under no workspace root, as (index in found, path as written, shown, line): a git ref
    isn't a path, and a path that starts at a subfolder of the project is fine — unless its line reference is past
    the end of every file it can mean. Nothing is reported for a path the search didn't finish looking for."""
    want: Dict[str, List[int]] = {}
    for k, (i, bare, _shown, _line) in enumerate(lost):
        rel = re.sub(r"^(?:\./)+", "", bare.replace("\\", "/")).rstrip("/")
        if GIT_REF.match(bare):
            found[i] = None
        elif rel and rel != ".." and not rel.startswith("../") and len(want) < 60:
            want.setdefault(rel, []).append(k)
    hits, whole = in_subfolders(roots, list(want)) if want else ({}, True)
    asked = 0
    for rel, ks in want.items():
        paths = hits.get(rel) or []
        for k in ks:
            i, bare, shown, line_no = lost[k]
            if paths:
                files = [p for p in paths if os.path.isfile(p)]
                counts = [n for n in (_line_count(p) for p in files) if n is not None] if line_no else []
                longest = max(counts) if counts else 0
                top_dir = next((r for r in roots if files and files[0].startswith(r + os.sep)), "")
                where = files[0][len(top_dir) + 1:].replace(os.sep, "/") if top_dir else rel
                found[i] = (f"`{shown}` — the file (`{where}`) has only {longest} lines." if counts and line_no > longest
                            else None)
            elif not whole:
                found[i] = None  # the project is too big to look through in time: not a claim it's missing
            elif not LETTER_EXT.search(bare) and asked < 3:
                asked += 1
                found[i] = None if is_git_ref(bare, ws or roots[0]) else found[i]


def references(answer: str, ev: dict, ws: str, art: str = "", roots: Optional[List[str]] = None) -> List[str]:
    """File links and project paths in the report that don't exist, and line references past the end of a file.
    A relative link may also point into the conversation's artifact folder (task.md, walkthrough.md), another
    workspace folder or a subfolder of the project (`mcp/server.py` for `plugin/mcp/server.py`); a git ref
    (`origin/v1.2.0`) isn't a path."""
    roots = [r for r in (roots or [ws]) if r]
    found: List[Optional[str]] = []
    lost: List[Tuple[int, str, str, int]] = []
    done = set()
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
            # as the agent writes paths, with «/» on every system (Windows: `app\missing.py` didn't match its report)
            shown = os.path.relpath(path, ws).replace(os.sep, "/") if ws and path.startswith(ws + os.sep) else raw
            line_no = int(next((g for g in (ref.groups() if ref else ()) if g), 0) or 0)
            relative = not (os.path.isabs(bare) or bare.startswith(("~", "file:")) or re.match(r"^[A-Za-z]:[\\/]", bare))
            if not os.path.exists(path) and relative:
                alts = [os.path.join(r, unquote(bare)) for r in roots if r != ws] + ([os.path.join(art, bare)] if art else [])
                path = next((a for a in alts if os.path.exists(a)), path)
            if not os.path.exists(path):
                if name and name not in gone:
                    lost += [(len(found), unquote(bare), shown, line_no)] if relative else []
                    found.append(f"`{shown}` — no such file or folder.")
                continue
            n = _line_count(path) if line_no and os.path.isfile(path) else None
            if n is not None and line_no > n:
                found.append(f"`{shown}` — the file has only {n} lines.")
    if lost:
        _settle(lost, found, roots, ws)
    return [f for f in found if f]


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
    roots = workspaces(payload)
    ws = roots[0] if roots else ""
    ev = scan(tp)
    art = conv.to_path(payload.get("artifactDirectoryPath") or "") if payload.get("artifactDirectoryPath") else ""
    out = (unbacked(answer, ev, ws, verify, code_ext, web_ext) + references(answer, ev, ws, art, roots)
           + sources(answer, ev, ws))
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
