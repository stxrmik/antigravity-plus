#!/usr/bin/env python3
"""Antigravity Plus — /context: what fills the current conversation's context window.

/usage says how full the context is; this says what it is made of. It reads the conversation's transcript
(~/.gemini/<product>/brain/<id>/.system_generated/logs/transcript_full.jsonl), keeps only what the model still sees —
the part since the last compaction (a CHECKPOINT step) plus that compaction's summary — systemMessage hook blocks
from SessionStart/PreInvocation persist until compaction (Antigravity 2.32+), while only legacy EPHEMERAL_MESSAGE
steps drop after one step — and splits it into categories: the fixed overhead (system prompt and tool
definitions, rules files, the skills list), the session context, user messages, the model's text and thinking, tool
call arguments, tool outputs per tool, messages from agents and background tasks, hook messages by tag, system
messages.

Token counts per item are estimates (characters → tokens: Latin ≈ 4 chars/token, code ≈ 3.5, Cyrillic ≈ 2.5),
calibrated to the real number Antigravity recorded for the last model call (input_tokens + cache_read_tokens), so the
categories add up to it. The fixed overhead is measured from the conversation's first model call. The window and the
compaction threshold come from skills/usage/pricing.json.

Usage:
  context.py --conversation <id>            # Markdown: categories, the 10 biggest items, tips
  context.py --workspace <dir>              # the newest conversation of that workspace
  context.py --conversation <id> --json
  context.py --transcript /path/to/transcript_full.jsonl

Read-only. Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.dirname(os.path.dirname(HERE))  # …/antigravity-plus/skills
sys.path.insert(0, os.path.join(SKILLS_DIR, "usage", "scripts"))
import usage  # noqa: E402  (the same module installs it: transcript reading, pricing, model per call)

PLUGINS = os.path.expanduser("~/.gemini/config/plugins")
GLOBAL_RULES = ["~/.gemini/GEMINI.md", "~/.gemini/config/AGENTS.md"]
PROJECT_RULES = ["AGENTS.md", "GEMINI.md"]
WORKSPACE_RE = re.compile(r"^## Workspace: (.+?)\s*$", re.M)
CYRILLIC_RE = re.compile("[\u0400-\u04ff]")
WIDE_RE = re.compile("[\u2e80-\U0010ffff]")  # CJK and the like: about a token per character
CODE_CHARS = "{}()[];=<>/\\_|`#$*&+%^~\"'"
TAG_RE = re.compile(r"<([a-z][a-z0-9_]*)\b[^>]*>.*?</\1>", re.S)
BYPASS_NOTE = re.compile(r"^\s*(?:The user turned Bypass Mode (?:ON|OFF)|Bypass Mode was turned (?:ON|OFF) in another conversation)", re.M)
SENDER_RE = re.compile(r"\[Message\][^\n]{0,120}?\bsender=(\S+)")
WARN = 150_000  # = hooks/speed.py CTX_WARN: every step re-reads it all, details from the middle get lost
# The transcript keeps a summary of the model's thinking; what the next call re-reads of it is about twice as much
# (fitted on the prompt growth between consecutive calls of real transcripts; Claude's even more).
THINKING_WEIGHT = 2.0

CATEGORIES = [  # key, Russian name (the report talks to the user)
    ("system", "Системный промпт и описания инструментов"),
    ("rules", "Файлы правил"),
    ("skills", "Список скиллов"),
    ("session", "Контекст сессии (SessionStart)"),
    ("checkpoint", "Сводка после сжатия"),
    ("user", "Сообщения пользователя"),
    ("model_text", "Текст ответов модели"),
    ("thinking", "Размышления модели"),
    ("tool_args", "Аргументы вызовов"),
    ("tool_out", "Вывод инструментов"),
    ("agents", "Сообщения агентов"),
    ("tasks", "Фоновые задачи"),
    ("hooks", "Подсказки хуков"),
    ("system_msg", "Системные сообщения"),
    ("errors", "Ошибки"),
]
NAMES = dict(CATEGORIES)
ORDER = {key: i for i, (key, _) in enumerate(CATEGORIES)}
FIXED = ("system", "rules", "skills")


@dataclass
class Item:
    cat: str  # category key
    sub: str  # tool name or hook tag ("" when the category has no sub-rows)
    label: str
    step: int
    chars: int
    est: float
    phase: str  # fixed · call (in the last call's prompt) · call_only (a hook message live then, gone now) · tail (after it)
    tokens: float = 0.0
    full: bool = False  # read_file without a line range


# ─────────────────────────────── estimates ─────────────────────────────────

def estimate(text: str) -> float:
    """Tokens for `text`, roughly: Latin prose ≈ 4 chars/token, code ≈ 3.5, Cyrillic ≈ 2.5, CJK ≈ 1."""
    if not text:
        return 0.0
    ascii_n = len(text.encode("ascii", "ignore"))
    rest = len(text) - ascii_n
    cyr = len(CYRILLIC_RE.findall(text)) if rest else 0
    wide = len(WIDE_RE.findall(text)) if rest > cyr else 0
    other = max(0, rest - cyr - wide)
    symbols = sum(text.count(ch) for ch in CODE_CHARS)
    ratio = 3.5 if ascii_n and symbols / ascii_n > 0.05 else 4.0
    return ascii_n / ratio + cyr / 2.5 + wide + other / 2.5


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\r\n")
    return text


def frontmatter(text: str) -> Dict[str, str]:
    """name / description / trigger of a SKILL.md or rules file (folded `>-` values joined into one line)."""
    out: Dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    key = ""
    for line in text[3:end if end != -1 else len(text)].splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            out[key] = "" if val in (">", ">-", "|", "|-") else val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def rules_files(workspaces: List[str]) -> List[Tuple[str, str]]:
    """(path, text as Antigravity puts it into the system prompt) of every rules file it loads."""
    paths = sorted(glob.glob(os.path.join(PLUGINS, "*", "rules", "*.md")))
    if not any(os.path.basename(os.path.dirname(os.path.dirname(p))) == "antigravity-plus" for p in paths):
        paths += sorted(glob.glob(os.path.join(SKILLS_DIR, "..", "rules", "*.md")))  # not installed: this copy's
    paths += [os.path.expanduser(p) for p in GLOBAL_RULES]
    for ws in workspaces:
        paths += [os.path.join(ws, name) for name in PROJECT_RULES]
    out, seen = [], set()
    for p in paths:
        real = os.path.realpath(p)
        text = read_text(p) if os.path.isfile(p) and real not in seen else ""
        if not text:
            continue
        seen.add(real)
        trigger = frontmatter(text).get("trigger", "always_on")
        if trigger and trigger != "always_on":
            continue
        out.append((os.path.normpath(p), f"<RULE[{p}]>\n{strip_frontmatter(text)}\n</RULE[{p}]>\n"))
    return out


def skills_list() -> Tuple[int, str]:
    """(count, the skills list as the system prompt shows it: «- name (path): description») for installed plugins."""
    lines = []
    for p in sorted(glob.glob(os.path.join(PLUGINS, "*", "skills", "SKILL.md"))
                    + glob.glob(os.path.join(PLUGINS, "*", "skills", "*", "SKILL.md"))):
        fm = frontmatter(read_text(p))
        if fm.get("name"):
            lines.append(f"- {fm['name']} ({p}): {fm.get('description', '')}\n")
    return len(lines), "".join(lines)


# ─────────────────────────────── transcript ────────────────────────────────

def short(text: str, n: int = 60) -> str:
    return usage.short(text, n)


def rel(path: str, workspaces: List[str]) -> str:
    p = str(path or "")
    if p.startswith("file://"):
        p = unquote(urlparse(p).path)
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
    if not p:
        return ""
    norm = os.path.normpath(p)
    nc = os.path.normcase(norm)
    for ws in workspaces:
        w = os.path.normpath(ws).rstrip("/\\")
        nw = os.path.normcase(w)
        if nw and nc.startswith(nw) and nc[len(nw):len(nw) + 1] in ("/", "\\"):
            return norm[len(w) + 1:]
    home = os.path.normpath(os.path.expanduser("~"))
    nh = os.path.normcase(home)
    return "~" + norm[len(home):] if nh and nc.startswith(nh + os.sep) else norm


def target(name: str, args: dict, workspaces: List[str]) -> Tuple[str, bool]:
    """What a tool call was about, for labels (a path, a command, a query), and whether it read a whole file."""
    a = args if isinstance(args, dict) else {}
    path = a.get("Path") or a.get("AbsolutePath") or a.get("TargetFile") or ""
    if name == "read_file":
        lines = f":{a.get('StartLine', 1)}-{a.get('EndLine', '…')}" if a.get("StartLine") or a.get("EndLine") else ""
        return rel(path, workspaces) + lines, not lines
    if path:
        return rel(path, workspaces), False
    for key in ("CommandLine", "query", "Url", "Role", "TypeName", "Action"):
        if a.get(key):
            return short(str(a[key]), 60), False
    if a.get("Recipient"):
        return "→ " + str(a["Recipient"])[:8], False
    return "", False


def _unwrap_sys(content: str) -> str:
    parts = content.split("<SYSTEM_MESSAGE>")
    body = parts[2] if len(parts) > 2 else parts[-1]
    return re.sub(r"</SYSTEM_MESSAGE>\s*$", "", body).strip()


def system_kind(content: str) -> Tuple[str, str, str]:
    """(category, sub, label) of a SYSTEM_MESSAGE."""
    parts = content.split("<SYSTEM_MESSAGE>")
    body = (parts[2] if len(parts) > 2 else content).strip()
    if "injected by a SessionStart hook" in body[:200]:
        return "session", "", "контекст сессии (SessionStart)"
    m = SENDER_RE.match(body)
    if m:
        sender = m.group(1)
        if "/task-" in sender:
            return "tasks", "", "фоновая задача: уведомление"
        if usage.UUID_RE.match(sender):
            return "agents", "", f"сообщение агента {sender[:8]}"
        return "system_msg", "", f"сообщение от {sender[:20]}"
    if body.startswith("Stop hook blocked termination"):
        return "hooks", "Stop", "хук Stop: возврат к работе"
    return "system_msg", "", "системное сообщение"


def hook_parts(content: str) -> List[Tuple[str, str]]:
    """An ephemeral message split into its hooks' blocks: [(tag, text)], the rest under ""."""
    out, last = [], 0
    for m in TAG_RE.finditer(content):
        if content[last:m.start()].strip():
            out.append(("", content[last:m.start()]))
        out.append((m.group(1), m.group(0)))
        last = m.end()
    if content[last:].strip() or not out:
        out.append(("", content[last:]))
    return out


def find_workspaces(steps: List[dict]) -> List[str]:
    out: List[str] = []
    for st in steps:
        if st.get("type") == "SYSTEM_MESSAGE":
            for ws in WORKSPACE_RE.findall(st.get("content") or ""):
                ws = ws.strip()
                if ws not in out:
                    out.append(ws)
            if out:
                break
    return out


def has_usage(st: dict) -> bool:
    return st.get("type") == "PLANNER_RESPONSE" and any(k in st for k in ("input_tokens", "cache_read_tokens"))


def prompt_of(st: dict) -> int:
    return int(st.get("input_tokens") or 0) + int(st.get("cache_read_tokens") or 0)


def _call_args(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def collect(steps: List[dict], workspaces: List[str]) -> dict:
    """Items of what the model sees, with their phase relative to the last model call."""
    ck = max((i for i, st in enumerate(steps) if st.get("type") == "CHECKPOINT"), default=-1)
    planners = [i for i, st in enumerate(steps) if st.get("type") == "PLANNER_RESPONSE"]
    calls = [i for i in planners if has_usage(steps[i]) and i > ck]
    last = calls[-1] if calls else None
    prev = max((i for i in planners if last is not None and i < last), default=-1)
    final = planners[-1] if planners else -1
    first = next((i for i in planners if has_usage(steps[i])), None)

    items: List[Item] = []
    before_first = 0.0  # estimate of what preceded the first model call (to measure the fixed overhead)
    queue: List[Tuple[str, dict]] = []
    users = 0

    def phase(pos: int, ephemeral: bool) -> str:
        if ephemeral:
            if last is not None and prev < pos < last:
                return "call_only"
            return "tail" if pos > max(final, last if last is not None else -1) else "gone"
        if last is None or pos >= last:
            return "tail"
        return "call"

    for pos, st in enumerate(steps):
        typ = st.get("type", "")
        content = st.get("content") or ""
        if typ == "SYSTEM_MESSAGE" and not _unwrap_sys(content):
            continue  # zero-token user_markdown or empty system message: 0 model tokens
        step = int(st.get("step_index", -1)) if str(st.get("step_index", "")).lstrip("-").isdigit() else -1
        if first is not None and pos < first:
            before_first += estimate(content)
        if typ == "PLANNER_RESPONSE":
            queue = [(tc.get("name") or "?", _call_args(tc.get("args")))
                     for tc in (st.get("tool_calls") or []) if isinstance(tc, dict)]
        elif typ in ("USER_INPUT", "CHECKPOINT"):
            queue = []
        counted = pos >= ck if ck >= 0 else True
        if not counted:
            continue

        def add(cat: str, sub: str, label: str, text: str, ephemeral: bool = False, full: bool = False,
                weight: float = 1.0) -> None:
            if text:
                items.append(Item(cat, sub, label, step, len(text), estimate(text) * weight, phase(pos, ephemeral),
                                  full=full))

        if typ == "CHECKPOINT":
            add("checkpoint", "", "сводка после сжатия", content)
        elif typ == "USER_INPUT":
            users += 1
            add("user", "", f"сообщение пользователя №{users}", content)
        elif typ == "PLANNER_RESPONSE":
            add("model_text", "", "текст ответа модели", content)
            add("thinking", "", "размышления модели", st.get("thinking") or "", weight=THINKING_WEIGHT)
            for name, args in queue:
                what, _ = target(name, args, workspaces)
                add("tool_args", name, f"{name} (аргументы) {what}".rstrip(),
                    name + json.dumps(args, ensure_ascii=False))
        elif typ == "GENERIC":
            name, args = queue.pop(0) if queue else ("?", {})
            what, full = target(name, args, workspaces)
            add("tool_out", name, f"{name} {what}".rstrip(), content, full=full)
        elif typ == "EPHEMERAL_MESSAGE":
            for tag, text in hook_parts(content):
                add("hooks", f"<{tag}>" if tag else "прочее", f"подсказка <{tag}>" if tag else "подсказка", text,
                    ephemeral=True)
        elif typ == "SYSTEM_MESSAGE":
            body = _unwrap_sys(content)
            if 'source="Antigravity Plus"' in body or BYPASS_NOTE.search(body):
                for tag, text in hook_parts(body):
                    if tag:
                        sub, label = f"<{tag}>", f"подсказка <{tag}>"
                    elif BYPASS_NOTE.search(text):
                        sub, label = "bypass_notice", "подсказка Bypass Mode"
                    else:
                        sub, label = "прочее", "подсказка"
                    add("hooks", sub, label, text)
            else:
                cat, sub, label = system_kind(content)
                add(cat, sub, label, content)
        elif typ == "ERROR_MESSAGE":
            add("errors", "", "ошибка", content + str(st.get("error") or ""))
        elif content:
            add("system_msg", "", typ.lower() or "шаг", content)

    return {"items": items, "checkpoint": steps[ck] if ck >= 0 else None,
            "checkpoints": sum(1 for st in steps if st.get("type") == "CHECKPOINT"),
            "last": steps[last] if last is not None else None,
            "first": steps[first] if first is not None else None, "before_first": before_first}


# ─────────────────────────────── analysis ──────────────────────────────────

def analyze(steps: List[dict], conv_id: str, model: str, model_guessed: bool, pricing: "usage.Pricing",
            workspaces: Optional[List[str]] = None) -> dict:
    workspaces = workspaces or find_workspaces(steps)
    c = collect(steps, workspaces)
    items: List[Item] = c["items"]

    fixed: List[Item] = [Item("rules", "", f"правила: {rel(p, workspaces)}", -1, len(t), estimate(t), "fixed")
                         for p, t in rules_files(workspaces)]
    n_skills, skills_text = skills_list()
    if skills_text:
        fixed.append(Item("skills", "", f"список скиллов ({n_skills})", -1, len(skills_text), estimate(skills_text),
                          "fixed"))
    disk = sum(i.est for i in fixed)

    real = prompt_of(c["last"]) if c["last"] else 0
    in_call = sum(i.est for i in items if i.phase in ("call", "call_only"))
    measured = prompt_of(c["first"]) - c["before_first"] if c["first"] else 0.0
    method, scale, overhead = "estimate", 1.0, disk
    if real:
        scale = (real - measured) / in_call if in_call and real > measured > 0 else 0.0
        if 0.33 <= scale <= 3:
            method, overhead = "measured", measured
        else:  # nothing to measure the overhead against (or it doesn't fit): scale everything alike
            method = "proportional"
            scale = real / (disk + in_call) if disk + in_call else 1.0
            overhead = disk * scale
    # the fixed overhead: rules files and the skills list as estimated, the rest is the system prompt + tool schemas
    share = min(1.0, overhead / disk) if disk else 0.0
    for i in fixed:
        i.tokens = i.est * share
    rest = overhead - sum(i.tokens for i in fixed)
    if rest >= 1:
        fixed.insert(0, Item("system", "", "системный промпт и инструменты", -1, 0, rest, "fixed", tokens=rest))
    for i in items:
        i.tokens = i.est * scale

    live = [i for i in items if i.phase in ("call", "tail")]
    tail = sum(i.tokens for i in items if i.phase == "tail")
    dropped = sum(i.tokens for i in items if i.phase == "call_only")
    now = sum(i.tokens for i in fixed) + sum(i.tokens for i in live)

    rows: Dict[Tuple[str, str], List[Item]] = {}
    for i in fixed + live:
        rows.setdefault((i.cat, i.sub if i.cat in ("tool_args", "tool_out", "hooks") else ""), []).append(i)
    cats = []
    for (cat, sub), its in sorted(rows.items(), key=lambda kv: (ORDER[kv[0][0]], -sum(i.tokens for i in kv[1]))):
        tok = sum(i.tokens for i in its)
        cats.append({"key": cat + (":" + sub if sub else ""), "category": cat, "sub": sub,
                     "name": NAMES[cat] + (f" — {sub}" if sub else ""), "tokens": round(tok),
                     "share": round(tok / now, 4) if now else 0.0, "items": len(its)})

    top = sorted((i for i in live if i.cat not in FIXED), key=lambda i: -i.tokens)[:10]
    last_model = model or pricing.cfg.get("default_model", "")
    window = pricing.context_window(last_model)
    compaction = pricing.compaction_threshold(last_model)
    ck = c["checkpoint"]
    return {
        "conversation": conv_id, "model": last_model, "model_guessed": model_guessed,
        "window": window, "compaction": compaction,
        "now": round(now), "fraction": round(now / window, 4) if window else None,
        "real": {"tokens": real, "step": c["last"].get("step_index")} if c["last"] else None,
        "after_last_call": round(tail), "hooks_dropped": round(dropped),
        "calibration": {"method": method, "scale": round(scale, 3), "fixed_overhead": round(overhead),
                        "fixed_estimate_from_files": round(disk)},
        "since_checkpoint": {"step": ck.get("step_index") if ck else None, "checkpoints": c["checkpoints"]},
        "workspaces": workspaces,
        "categories": cats,
        "top": [{"label": i.label, "category": i.cat, "step": i.step, "tokens": round(i.tokens)} for i in top],
        "tips": tips(live, now, compaction, window),
        "rules_files": [{"label": i.label, "tokens": round(i.tokens)} for i in fixed if i.cat == "rules"],
    }


def tips(live: List[Item], now: float, compaction: Optional[int], window: int) -> List[str]:
    """2–4 concrete ways to free the context, biggest first."""
    out: List[Tuple[float, str]] = []

    def total(cat: str, sub: str = "") -> float:
        return sum(i.tokens for i in live if i.cat == cat and (not sub or i.sub == sub))

    reads: Dict[str, List[Item]] = {}
    for i in live:
        if i.cat == "tool_out" and i.sub == "read_file":
            reads.setdefault(re.sub(r":[\d…]+-[\d…]+$", "", i.label[len("read_file "):]), []).append(i)
    again = max(reads.items(), key=lambda kv: sum(i.tokens for i in kv[1]) if len(kv[1]) > 1 else 0, default=None)
    if again and len(again[1]) > 1 and sum(i.tokens for i in again[1]) >= 5000:
        tok = sum(i.tokens for i in again[1])
        out.append((tok, f"`{again[0]}` прочитан {len(again[1])} раз (≈{usage.k(tok)}): перечитывайте только нужный "
                         "диапазон (StartLine/EndLine) — прежний вывод и так остаётся в контексте."))
    whole = max((i for i in live if i.full), key=lambda i: i.tokens, default=None)
    if whole and (whole.tokens >= 8000 or total("tool_out", "read_file") >= 0.15 * now):
        out.append((total("tool_out", "read_file"), f"Чтение файлов целиком (крупнейшее — {whole.label[10:]}, "
                    f"≈{usage.k(whole.tokens)}): сначала `rg -n`, потом read_file с StartLine/EndLine; обзор "
                    "большого файла — `lsp.py symbols`."))
    cmd = max((i for i in live if i.cat == "tool_out" and i.sub == "run_command"), key=lambda i: i.tokens, default=None)
    if cmd and (cmd.tokens >= 8000 or total("tool_out", "run_command") >= 0.15 * now):
        out.append((total("tool_out", "run_command"), f"Длинный вывод команд (крупнейший — шаг {cmd.step}, "
                    f"≈{usage.k(cmd.tokens)}): обрезайте его (`| tail -40`, `| grep`, `git diff --stat`, `--json` с "
                    "нужными полями)."))
    big_write = max((i for i in live if i.cat == "tool_args" and i.sub == "write_file"), key=lambda i: i.tokens,
                    default=None)
    if big_write and (big_write.tokens >= 4000 or total("tool_args", "write_file") >= 0.1 * now):
        out.append((total("tool_args", "write_file"), f"write_file с файлом целиком (≈{usage.k(big_write.tokens)} "
                    f"за раз, шаг {big_write.step}): правьте существующие файлы через edit_file — в контекст "
                    "попадает только изменённый кусок."))
    if total("tool_out", "search_web") >= 0.1 * now:
        out.append((total("tool_out", "search_web"), "Результаты поиска занимают много места: поручайте поиск "
                    "субагенту `researcher` — в контекст вернётся только его вывод."))
    limit = compaction or window
    warn = min(WARN, int(limit * 0.6)) if limit else WARN
    if now >= warn:
        near = (f"сжатие на ≈{usage.k(compaction)}" if compaction else
                f"окно ≈{usage.k(window)}; Antigravity сжимает историю, как только следующий шаг в него не помещается")
        out.append((now, f"Контекст ≈{usage.k(now)} ({near}): каждый шаг перечитывает его целиком. Когда текущая "
                    "задача закончится, продолжайте в новом разговоре через /handoff или сожмите этот сейчас "
                    "командой /compact (её вводит пользователь, Antigravity 2.33+)."))
    out.sort(key=lambda x: -x[0])
    found = [t for _, t in out[:4]]
    return found or ["Ничего не выделяется: контекст распределён ровно, экономить не на чем."]


# ─────────────────────────────── rendering ─────────────────────────────────

def pct(v: float) -> str:
    return f"{v * 100:.1f}%" if v < 0.1 else f"{v * 100:.0f}%"


def render(r: dict) -> str:
    k = usage.k
    out = ["## Что занимает контекст", ""]
    head = f"Модель: {r['model']}" + (" (не названа в транскрипте — по умолчанию)" if r["model_guessed"] else "")
    head += f" · окно {k(r['window'])}" + (f" · сжатие ≈{k(r['compaction'])}" if r.get("compaction") else "")
    out.append(head)
    line = f"Сейчас ≈ **{k(r['now'])}** токенов"
    if r.get("fraction") is not None:
        line += f" ({pct(r['fraction'])} окна"
        line += f", {pct(r['now'] / r['compaction'])} от порога сжатия)" if r.get("compaction") else ")"
    if r["real"]:
        line += f": реально {k(r['real']['tokens'])} на последнем вызове модели (шаг {r['real']['step']})"
        if r["after_last_call"]:
            line += f" + ≈{k(r['after_last_call'])} после него"
    else:
        line += " — только оценка: в транскрипте нет вызова модели с числами"
    ck = r["since_checkpoint"]
    line += "." + (f" Считается с последнего сжатия (шаг {ck['step']}, всего сжатий: {ck['checkpoints']})."
                   if ck["step"] is not None else "")
    out += [line, "", "| Категория | Токены | % |", "|---|--:|--:|"]
    for c in r["categories"]:
        out.append(f"| {c['name']} | {k(c['tokens'])} | {pct(c['share'])} |")
    out += [f"| **Всего** | **{k(r['now'])}** | 100% |", ""]
    if r["top"]:
        out += ["### 10 самых крупных элементов", "", "| # | Что | Шаг | Токены |", "|--:|---|--:|--:|"]
        for n, t in enumerate(r["top"], 1):
            label = t["label"].replace("|", "\\|")
            out.append(f"| {n} | {label} | {t['step']} | {k(t['tokens'])} |")
        out.append("")
    out += ["### Советы", ""] + [f"- {t}" for t in r["tips"]] + [""]
    cal = r["calibration"]
    how = {"measured": "подогнаны под реальное число последнего вызова; постоянная часть измерена по первому вызову",
           "proportional": "пропорционально подогнаны под реальное число последнего вызова",
           "estimate": "без подгонки — в транскрипте нет чисел вызова модели"}[cal["method"]]
    note = (f"<sub>Токены по категориям — оценка: символы → токены (латиница ≈ 4 символа на токен, код ≈ 3.5, "
            f"кириллица ≈ 2.5), {how}. Блоки хуков systemMessage (SessionStart/PreInvocation, Antigravity 2.32+) "
            f"остаются в контексте до сжатия; один шаг живут только устаревшие EPHEMERAL_MESSAGE")
    if r["hooks_dropped"]:
        note += f" (≈{k(r['hooks_dropped'])} из EPHEMERAL_MESSAGE прошлого шага уже выпали)"
    out.append(note + ".</sub>")
    return "\n".join(out)


# ─────────────────────────────── main ──────────────────────────────────────

def newest_of(workspace: str, roots: List[str]) -> Optional[str]:
    """The newest top-level conversation whose session context names `workspace`."""
    want = os.path.normcase(os.path.realpath(os.path.expanduser(workspace)))
    found: List[Tuple[float, str]] = []
    for r in roots:
        for tp in glob.glob(os.path.join(os.path.expanduser(r), "brain", "*", ".system_generated", "logs",
                                         "transcript_full.jsonl")):
            try:
                found.append((os.path.getmtime(tp), tp))
            except OSError:
                continue
    for _, tp in sorted(found, reverse=True):
        try:
            with open(tp, encoding="utf-8", errors="replace") as f:
                head = f.read(262144)
        except OSError:
            continue
        if '"USER_INPUT"' not in head or "Workspace:" not in head:  # subagents have no USER_INPUT
            continue
        for line in head.splitlines():
            if '"SYSTEM_MESSAGE"' not in line[:400]:
                continue
            try:
                st = json.loads(line)
            except ValueError:
                continue
            if not isinstance(st, dict) or st.get("type") != "SYSTEM_MESSAGE":
                continue
            for ws in WORKSPACE_RE.findall(st.get("content") or ""):
                if os.path.normcase(os.path.realpath(ws.strip())) == want:
                    return os.path.normpath(tp).split(os.sep)[-4]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversation", "-c", help="conversation id (UUID)")
    ap.add_argument("--workspace", "-w", help="the newest conversation of this workspace folder")
    ap.add_argument("--transcript", help="explicit path to transcript_full.jsonl (or its logs dir)")
    ap.add_argument("--model", help="model for the window and the compaction threshold (default: the transcript's)")
    ap.add_argument("--pricing", default=usage.DEFAULT_PRICING, help="path to pricing.json")
    ap.add_argument("--root", action="append", help="product data root(s), default: ~/.gemini/antigravity{,-cli,-ide}")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)

    roots = a.root or usage.PRODUCT_ROOTS
    pricing = usage.Pricing(a.pricing)
    if a.transcript:
        p = os.path.expanduser(a.transcript)
        logs = p if os.path.isdir(p) else os.path.dirname(p)
        conv = a.conversation or (usage.UUID_RE.findall(logs) or ["?"])[-1]
    else:
        conv = a.conversation or (newest_of(a.workspace, roots) if a.workspace else None)
        if not conv:
            if a.workspace:
                print(f"Разговоров с рабочей папкой {a.workspace} не найдено в: {', '.join(roots)}", file=sys.stderr)
                return 2
            ap.error("укажите --conversation <id>, --workspace <папка> или --transcript <путь>")
        logs = usage.logs_dir_for(conv, roots)
        if not logs:
            print(f"Транскрипт для {conv} не найден в: {', '.join(roots)}", file=sys.stderr)
            return 2
    try:
        steps = usage.load_steps(logs)
        s = usage.parse_session(conv, logs, pricing, roots, depth=3)  # depth 3: subagents aren't in this context
    except usage.AccessDenied as e:
        print(f"Нет доступа на чтение: {e}. Запустите вне песочницы.", file=sys.stderr)
        return 3
    model = a.model or (s.calls[-1].model if s.calls else "")
    guessed = not a.model and (not s.calls or s.calls[-1].guessed)
    ws_arg = [os.path.realpath(os.path.expanduser(a.workspace))] if a.workspace else None
    r = analyze(steps, conv, model, guessed, pricing, ws_arg)
    print(json.dumps(r, ensure_ascii=False) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
