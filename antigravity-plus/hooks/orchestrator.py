#!/usr/bin/env python3
"""Antigravity Plus — Orchestrator (Carbon / Argon / Neon planner + Hydrogen executor) phase & plan checks.

In Antigravity's Orchestrator models, the planner works in plan mode and hands off to Hydrogen with
`exit_plan_mode`, after which Antigravity injects a SYSTEM_MESSAGE «Plan mode is off: this turn runs on the
execution model». Hydrogen runs until it hands back with `enter_plan_mode` (or a `[plan review]` SYSTEM_MESSAGE).

This module provides:
  * check_plan(payload, st, users, args) -> (rule, reason) for guard.py on `exit_plan_mode`: a step the user banned
    («ничего не пушь») → "orchestrator-ban-in-plan" (guard asks the user: they may have asked for it since); a plan
    that changes code without any check → "orchestrator-unverified-plan" (guard refuses it, once per plan and turn —
    the same plan sent again goes through, so the planner is never trapped);
  * handoff_message(payload, st)         -> <orchestrator_handoff> or <orchestrator_review> for instructions.py;
  * in_plan_mode(payload, st)            -> whether the current Orchestrator turn is in planning/review mode.

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import conversation as conv
import coord

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
try:
    import memory as memstore
except Exception:
    memstore = None

TAIL_BYTES = 65_536
MAX_SCAN_BYTES = 16 * 1024 * 1024
USER_INPUT_RE = re.compile(rb'"type"\s*:\s*"USER_INPUT"')

# ── the steps a ban covers: commands, not words («nothing to push», «the last git commit message» are no steps) ──
_GIT_OPTS = r"(?:\s+(?:-[cC]\s+\S+|--?[\w-]+(?:=\S+)?))*"
PLAN_PUSH = re.compile(
    rf"\bgit{_GIT_OPTS}\s+push\b|\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b|\b(?:docker|podman)\s+push\b"
    r"|\b(?:npm|pnpm|yarn|bun|cargo|poetry|gem|twine)\s+publish\b"
    r"|\bkubectl\s+(?:apply|create|replace|patch|rollout|set|scale|delete)\b|\bhelm\s+(?:install|upgrade|rollback)\b"
    r"|\b(?:terraform|tofu)\s+(?:apply|destroy)\b|(?:^|[\s`'\"(])\./deploy\S*"
    r"|\b(?:make|just|npm\s+run|pnpm(?:\s+run)?|yarn(?:\s+run)?|bun\s+run)\s+[\w:.-]*(?:deploy|release|publish)\b",
    re.I,
)
PLAN_COMMIT = re.compile(rf"\bgit{_GIT_OPTS}\s+commit\b", re.I)
COPY_CMD = re.compile(r"\b(scp|rsync|sftp)\b([^\n]*)", re.I)   # a copy is a push only to a remote host:path target
REMOTE_PATH = re.compile(r"^(?:[\w.-]+@)?[\w.-]{2,}:(?!//)")
# A negation in the same clause before the step («Не делать `git push`», "No `git push`", "we will not run git push",
# "Skip the `git push` step", «Без git push»), or right after it («`git push` не выполнять», "git push is banned").
NEG_BEFORE = re.compile(
    r"\b(?:don'?t|do\s+not|does\s+not|never|no|not|avoid\w*|skip\w*|won'?t|will\s+not|without|unless|refrain\w*|"
    r"must\s+not|mustn'?t|should\s+not|shouldn'?t|cannot|can'?t|forbid\w*|prohibit\w*|banned|disallow\w*|exclud\w*|"
    r"omit\w*|hold\s+off|instead\s+of|rather\s+than|until\s+(?:the\s+)?user|only\s+(?:after|if|when|once)|"
    r"(?:if|when|once|after)\s+(?:the\s+)?user)\b"
    r"|\b(?:не|нет|без|никак\w*|нельзя|ни\s+в\s+коем)\b|\b(?:запрещ|избега|пропуск|пропуст|исключ|воздерж)\w*"
    r"|\b(?:только|лишь)\s+(?:после|если|по)\b|\b(?:если|когда)\s+пользовател\w*|\bпо\s+(?:запросу|просьбе|команде)\b"
    r"|\bвместо\b",
    re.I,
)
NEG_AFTER = re.compile(
    r"^[\s`'\"«»)\]*_:—–-]*\(?\s*(?:(?:не|нельзя|запрещ\w*|пока\s+не|только\s+(?:после|если|по)|пропуска\w*|пропуст\w*"
    r"|исключ\w*|отмен\w*)\b|(?:is|are)\s+(?:not|banned|forbidden|prohibited|disallowed|off-limits|excluded)\b"
    r"|(?:must|should|will|shall)\s+not\b|(?:mustn'?t|shouldn'?t|won'?t)\b|not\s+(?:now|yet|allowed|permitted|until)\b"
    r"|(?:skip|skipped|forbidden|banned|prohibited|disallowed|excluded|omitted|until|unless)\b"
    r"|only\s+(?:after|if|when|once|on)\b|(?:the\s+)?user\s+(?:said|asked)\s+not\b|пользователь\s+(?:запретил|просил))",
    re.I,
)
# What ends the clause a negation belongs to: «Do not commit, but push: git push» still pushes.
CLAUSE_END = re.compile(r"[;!?]|\.(?:\s|$)|\b(?:but|however|then|otherwise|afterwards?|instead(?!\s+of))\b"
                        r"|\b(?:но|однако|затем|потом|иначе|после\s+этого)\b", re.I)
# A step mentioned, not taken: «the last git commit message», "git push hooks", a command inside a quoted message.
MENTION_AFTER = re.compile(r"^[`'\")\]»]*\s*(?:messages?|msg|history|logs?|style|conventions?|hash(?:es)?|sha|ids?|"
                           r"titles?|subjects?|body|authors?|dates?|graph|objects?|range|count|templates?|hooks?|output|"
                           r"errors?|failures?|rejected|сообщени\w*|истори\w*|стил\w*|х[эе]ш\w*|заголов\w*|шаблон\w*|"
                           r"хук\w*)\b", re.I)
MENTION_BEFORE = re.compile(r"\b(?:last|latest|previous|prior|recent|initial|parent|earlier|existing|older|"
                            r"последн\w*|предыдущ\w*|прошл\w*|существующ\w*)\s*[`'\"«]*\s*$", re.I)
# A section of the plan that lists what won't be done.
OUT_OF_SCOPE = re.compile(r"out\s+of\s+scope|not\s+(?:doing|in\s+scope)|won'?t\s+do|non-goals?|exclusions?|"
                          r"constraints?|restrictions?|вне\s+рамок|не\s+делаем|не\s+входит|исключено|запрещено|"
                          r"ограничени\w*|чего\s+не\s+делать", re.I)
HEADING = re.compile(r"^\s*(?:#{1,6}\s+.*|\*\*[^*]{1,60}\*\*:?\s*|[^\s][^.!?]{0,60}:\s*)$")

# ── does the plan change code, and does it check it ──
SOURCE_FILE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|mts|cts|swift|"
                         r"kt|kts|go|rs|rb|java|c|cc|cpp|h|hpp|cs|php|m|mm|vue|svelte|dart|scala|lua|ex|exs|sh|sql)\b", re.I)
FRAMEWORK = re.compile(r"(?:node|next|nuxt|vue|react|express|three|chart|d3|ember|backbone|angular|svelte|solid|deno|"
                       r"moment|lodash|jquery|socket|electron|nest|meteor|knockout|alpine|preact|gatsby|remix|astro|p5|"
                       r"pixi|leaflet|anime|highlight|marked|day|luxon|axios)\.js", re.I)
DOC_FILE = re.compile(r"\.(?:md|markdown|rst|txt|adoc|org)\b|\b(?:README|CHANGELOG|LICENSE|CONTRIBUTING)\b", re.I)
EDIT_VERB = re.compile(
    r"\b(?:edit|edits|edited|editing|change|changes|changed|changing|modify|modifies|modified|modifying|update|updates|"
    r"updated|updating|fix|fixes|fixed|fixing|implement\w*|refactor\w*|rewrite|rewrites|rewrote|rewriting|add|adds|"
    r"added|adding|patch\w*|create|creates|created|creating|write|writes|wrote|writing|rename\w*|move|moves|moved|"
    r"moving|delete|deletes|deleted|deleting|remove|removes|removed|removing|replace|replaces|replaced|replacing|"
    r"introduce\w*|extract\w*|adjust\w*|tweak\w*|extend\w*|handle|handles|handled|handling|convert\w*|migrate|"
    r"migrates|migrated|migrating|insert\w*|append\w*|split\w*)\b"
    r"|\b(?:измен|поправ|исправ|добав|обнов|перепис|реализ|удал|созда|замен|отредакт|правк|рефактор|вынес|перенес|"
    r"переимен|внедр|обработ|напис|допис|подправ|доработ|встав|убра|убер)\w*",
    re.I,
)
LIST_HEAD = re.compile(r"^\W*(?:\d+[.)]\s*)?[`*_\"']*(?:[\w.-]+/)*[\w.-]+\.\w+[`*_\"']*\s*[:—–-]")
NO_CODE = re.compile(
    r"\bno\s+code\s+changes?\b|\b(?:docs?|documentation)[- ]only\b|\bonly\s+(?:the\s+)?(?:docs|documentation|readme)\b"
    r"|\bwithout\s+(?:touching|changing)\s+(?:the\s+)?code\b|\bcode\s+(?:stays|remains)\s+(?:as\s+is|unchanged)\b"
    r"|\bтолько\s+(?:документац\w*|readme|доки|документы)|\bкод\s+не\s+(?:меня|трога)\w*|\b(?:ничего|ничем)\s+в\s+коде\s+не"
    r"|\bбез\s+изменени\w*\s+(?:в\s+)?код\w*|\bне\s+(?:меняя|трогая)\s+код\w*",
    re.I,
)
VERIFY_PLAN_RE = re.compile(  # in words: «run the tests», «проверить сборку», «убедиться, что …» (+ stop.VERIFY's commands)
    r"\b(?:test\w*|pytest|run_tests|\w*check\w*|\w*lint\w*|build\w*|verif\w*|smoke|e2e|ui[-_]check(?:\.py)?|mobile\.py|"
    r"doctor\.py|tsc|mypy|ruff|pyright|eslint|xcodebuild|gradlew?|mvn|ctest|compil\w*|benchmark\w*)\b"
    r"|тест|провер|сборк|убед\w*|прогон\w*|компил\w*|линтер\w*",
    re.I,
)
MAX_REMEMBERED = 20   # plan hashes refused once per conversation

HANDOFF_OFF_RE = re.compile(r"(?:^|<SYSTEM_MESSAGE>\s*)Plan mode is off:\s*this turn runs on the execution model", re.I)
PLAN_MODE_OFF_RE = re.compile(r"(?:^|<SYSTEM_MESSAGE>\s*)Plan mode is off\b", re.I)


def _auto_verify() -> bool:
    try:
        return bool(memstore.settings().get("autoVerify", True)) if memstore else True
    except Exception:
        return True


def _read_turn_steps(payload: dict) -> Tuple[List[dict], bool]:
    """Read steps from the latest USER_INPUT onward (in 64 KB chunks up to 16 MB).
    Returns (steps, hit_cap) where hit_cap is True if 16 MB was scanned without seeing USER_INPUT."""
    tp = conv.transcript_path(payload)
    if not tp:
        return [], False
    try:
        size = os.path.getsize(tp)
        if size == 0:
            return [], False
        chunks: List[bytes] = []
        offset = size
        scanned = 0
        hit_user = False
        carry = b""
        with open(tp, "rb") as f:
            while offset > 0 and scanned < MAX_SCAN_BYTES:
                start = max(0, offset - TAIL_BYTES)
                f.seek(start)
                buf = f.read(offset - start)
                scanned += len(buf)
                offset = start
                data = buf + carry
                if start > 0:
                    nl = data.find(b"\n")
                    if nl == -1:
                        carry = data
                        continue
                    carry = data[:nl]
                    complete = data[nl + 1:]
                else:
                    carry = b""
                    complete = data
                chunks.append(complete)
                if USER_INPUT_RE.search(complete):
                    hit_user = True
                    break
    except OSError:
        return [], False

    raw = b"\n".join(reversed(chunks))
    steps: List[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            steps.append(obj)
    return steps, (offset > 0 and not hit_user)


def _in_quotes(line: str, pos: int) -> bool:
    """Inside a "…" or «…» string before `pos`: a commit message that merely names a command."""
    head = line[:pos]
    return head.count('"') % 2 == 1 or head.count("«") > head.count("»")


def _negated(line: str, start: int, end: int) -> bool:
    """The step at line[start:end] is one the plan says it won't take."""
    clause = CLAUSE_END.split(line[:start])[-1]
    if NEG_BEFORE.search(" ".join(clause.split()[-10:])):
        return True
    return bool(NEG_AFTER.match(line[end:end + 80]))


def _remote_copies(line: str) -> List[Tuple[int, int]]:
    """scp / rsync / sftp whose destination (the last path-like argument) is a remote host:path."""
    out = []
    for m in COPY_CMD.finditer(line):
        if m.group(1).lower() == "sftp":
            out.append(m.span(1))
            continue
        rest = re.split(r"[`|;&]", m.group(2), maxsplit=1)[0]
        paths = [t.strip("'\"()[]") for t in rest.split() if not t.startswith("-") and ("/" in t or ":" in t)]
        if paths and REMOTE_PATH.match(paths[-1]):
            out.append(m.span(1))
    return out


def banned_step(plan: str, kind: str) -> Optional[str]:
    """The first plan line that takes a step of `kind` ("push" or "commit"), or None."""
    skip = False
    for line in plan.splitlines():
        if HEADING.match(line):
            skip = bool(OUT_OF_SCOPE.search(line))  # «## Out of scope», «Ограничения:» — a list of what won't be done
            continue
        if skip or not line.strip():
            continue
        spans = [m.span() for m in (PLAN_PUSH if kind == "push" else PLAN_COMMIT).finditer(line)]
        if kind == "push":
            spans += _remote_copies(line)
        for start, end in sorted(spans):
            if _in_quotes(line, start) or MENTION_AFTER.match(line[end:]) or MENTION_BEFORE.search(line[:start]):
                continue
            if not _negated(line, start, end):
                return line.strip()
    return None


def _command_checks() -> Optional["re.Pattern[str]"]:
    """stop.py's VERIFY — the commands that count as checking work — so a plan and a finished turn agree."""
    try:
        import stop  # type: ignore[import-not-found]
        return stop.VERIFY
    except Exception:
        return None


def edits_source(plan: str) -> bool:
    """The plan changes code: a line that names a source file with an edit verb (or opens with the file, «`a.py`: …»).
    A docs-only plan that merely names a source file («describe load() from src/api.py in README.md») doesn't."""
    if NO_CODE.search(plan):
        return False
    for line in plan.splitlines():
        files = [m for m in SOURCE_FILE.finditer(line) if not FRAMEWORK.fullmatch(m.group(0))]
        if not files:
            continue
        doc = DOC_FILE.search(line)
        if doc and doc.start() < files[0].start():
            continue  # the line is about a document that mentions code
        if EDIT_VERB.search(line) or LIST_HEAD.match(line):
            return True
    return False


def verifies(plan: str) -> bool:
    checks_re = _command_checks()
    return bool(VERIFY_PLAN_RE.search(plan) or (checks_re and checks_re.search(plan)))


def _refuse_once(payload: dict, st: Optional[dict], users: List[dict], plan: str) -> bool:
    """True the first time: remember the plan (and the user's turn) in the conversation state, so the same plan sent
    again — or any plan later in this turn — goes through. Without a state to remember it in, never refuse."""
    cid = (payload or {}).get("conversationId")
    if not isinstance(st, dict) or not isinstance(cid, str) or not cid:
        return False
    digest = hashlib.sha1(" ".join(plan.split()).encode("utf-8", "replace")).hexdigest()[:12]
    rec = st.get("orch_plan_check") if isinstance(st.get("orch_plan_check"), dict) else {}
    seen = [h for h in rec.get("refused") or [] if isinstance(h, str)]
    turn = len(users or [])
    if digest in seen or rec.get("turn") == turn:
        return False
    st["orch_plan_check"] = {"turn": turn, "refused": (seen + [digest])[-MAX_REMEMBERED:]}
    try:
        conv.store(cid, st)
    except Exception:
        return False
    return True


def check_plan(payload: dict, st: Optional[dict], users: List[dict], args: dict) -> Optional[Tuple[str, str]]:
    """Check an Orchestrator plan before `exit_plan_mode` hands it to Hydrogen."""
    plan = str((args or {}).get("plan") or (args or {}).get("Plan") or "")
    if not plan.strip():
        return None

    bans = conv.active_bans(users)
    for kind, what in (("push", "шаг отправки наружу (push, PR, деплой или загрузка на сервер)"),
                       ("commit", "создание коммита")):
        line = banned_step(plan, kind) if kind in bans else None
        if line:
            said = re.sub(r"\s+", " ", bans[kind]["text"]).strip()
            said = said if len(said) <= 120 else said[:119] + "…"
            return ("orchestrator-ban-in-plan",
                    f"в этом разговоре вы просили: «{said}» (сообщение {bans[kind]['n']}), а план, который планировщик "
                    f"передаёт исполнителю, содержит {what}: `{line[:100]}`. Подтвердите, если это уже можно "
                    "(например, вы сами попросили PR), — или отклоните, и планировщик уберёт этот шаг из плана "
                    "(exit_plan_mode).")

    if (_auto_verify() and len(plan) >= 300 and edits_source(plan) and not verifies(plan)
            and _refuse_once(payload, st, users, plan)):
        return ("orchestrator-unverified-plan",
                "план меняет исходный код, но в нём нет шага проверки (тесты, линтер, сборка или запуск). Добавьте в "
                "план конкретную команду проверки и снова вызовите exit_plan_mode. Если проверка здесь правда не "
                "нужна — отправьте тот же план ещё раз: повторный exit_plan_mode с тем же планом пройдёт.")

    return None


def handoff_message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """Emit one-time `<orchestrator_handoff>` or `<orchestrator_review>` tags when Orchestrator changes phase."""
    steps, _hit_cap = _read_turn_steps(payload)
    if not steps:
        return None

    latest_user_step = -1
    latest_handoff_step = -1
    latest_review_step = -1
    for idx, d in enumerate(steps):
        step_idx = d.get("step_index")
        if not isinstance(step_idx, int):
            step_idx = idx
        typ = d.get("type")
        if typ == "USER_INPUT":
            latest_user_step = max(latest_user_step, step_idx)
        elif typ == "SYSTEM_MESSAGE" and d.get("source") != "SYSTEM_SDK":
            content = d.get("content") or ""
            if HANDOFF_OFF_RE.search(content):
                latest_handoff_step = max(latest_handoff_step, step_idx)
            if "[plan review]" in content.lower():
                latest_review_step = max(latest_review_step, step_idx)
        elif typ == "PLANNER_RESPONSE":
            for tc in d.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name") == "enter_plan_mode":
                    latest_review_step = max(latest_review_step, step_idx)

    prev_handoff = int(st.get("orch_handoff_step", -1)) if st is not None else -1
    prev_review = int(st.get("orch_review_step", -1)) if st is not None else -1

    if st is not None:
        if latest_handoff_step > prev_handoff:
            st["orch_handoff_step"] = latest_handoff_step
        if latest_review_step > prev_review:
            st["orch_review_step"] = latest_review_step

    if (latest_review_step > prev_review
            and latest_review_step > latest_handoff_step
            and latest_review_step > latest_user_step):
        return (
            '<orchestrator_review source="Antigravity Plus">\n'
            "Execution handed back for plan review: verify the actual `git diff`, modified files, and real "
            "test/command outputs before re-planning or concluding.\n"
            "</orchestrator_review>"
        )

    if (latest_handoff_step > prev_handoff
            and latest_handoff_step >= latest_review_step
            and latest_handoff_step > latest_user_step):
        users = conv.with_current(st, payload)
        bans = conv.active_bans(users)
        access = conv.full_access(payload, st, users)
        others = coord.others(payload)
        if not (bans or access or others):
            return None
        lines = [
            '<orchestrator_handoff source="Antigravity Plus">',
            "Hand-off to execution model (Hydrogen): keep these active constraints in view while carrying out the plan:",
        ]
        for kind, info in bans.items():
            what = "push / deploy / upload to servers" if kind == "push" else "git commit"
            lines.append(f"- Active ban (message {info['n']}): no {what}.")
        if access:
            lines.append("- Full access is ON: explicit user bans and destructive guard stops still apply.")
        for a in others[:5]:
            files = ", ".join(a["files"][:6]) + (" …" if len(a["files"]) > 6 else "")
            lines.append(
                f"- Other active agent «{a['title'] or 'без названия'}» (conversation {a['id']})"
                + (f" — changed: {files}" if files else "")
                + ". Do not overwrite their work."
            )
        lines.append("</orchestrator_handoff>")
        return "\n".join(lines)

    return None


def in_plan_mode(payload: dict, st: Optional[dict] = None) -> bool:
    """True when the conversation uses an Orchestrator model and the current turn is in planning/review mode."""
    model = str((payload or {}).get("modelName") or "").lower()
    if "orchestrator" not in model:
        return False
    steps, hit_cap = _read_turn_steps(payload)
    if hit_cap:
        return False
    if not steps:
        return True
    for d in reversed(steps):
        typ = d.get("type")
        if typ == "PLANNER_RESPONSE":
            for tc in d.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name") == "enter_plan_mode":
                    return True
        elif typ == "SYSTEM_MESSAGE" and d.get("source") != "SYSTEM_SDK":
            content = d.get("content") or ""
            if "[plan review]" in content.lower():
                return True
            if PLAN_MODE_OFF_RE.search(content):
                return False
        elif typ == "USER_INPUT":
            return True
    return True
