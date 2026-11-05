#!/usr/bin/env python3
"""Antigravity Plus — /export: save a conversation as a readable Markdown file (like Claude Code's /export).

  export.py --conversation <id> --out <file.md>      # prompts, answers, tool calls (one line each)
  export.py --conversation <id> --out f.md --full    # + tool outputs (truncated) and thinking
  export.py --conversation <id> --clipboard          # copy to the macOS clipboard instead

Reads ~/.gemini/<product>/brain/<id>/.system_generated/logs/transcript_full.jsonl via the /usage parser.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "usage", "scripts"))
from usage import (AccessDenied, MODEL_CHANGE_RE, PRODUCT_ROOTS, USER_REQ_RE, UUID_RE,  # noqa: E402
                   load_steps, logs_dir_for, parse_ts)

META_RE = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|EPHEMERAL_MESSAGE)>.*?</\1>", re.S)
SKILL_RE = re.compile(r"/(\S+) is a \[Slash Command\]")


def unwrap(v) -> str:
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
    return json.dumps(v, ensure_ascii=False) if v is not None else ""


def one_line(text: str, n: int = 140) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def summarize_call(name: str, args: dict) -> str:
    a = {k: unwrap(v) for k, v in (args or {}).items()}
    if name == "run_command":
        return f"`{one_line(a.get('CommandLine', ''), 160)}`"
    if name in ("read_file", "view_file"):
        rng = f" (строки {a['StartLine']}–{a.get('EndLine', '…')})" if a.get("StartLine") else ""
        return f"`{a.get('Path', a.get('AbsolutePath', ''))}`{rng}"
    if name in ("write_file", "edit_file", "write_to_file", "replace_file_content"):
        return f"`{a.get('Path', a.get('TargetFile', ''))}`"
    if name == "search_web":
        return f"«{one_line(a.get('query', ''), 120)}»"
    if name == "invoke_subagent":
        return f"{a.get('TypeName', '?')} — {one_line(a.get('Role', ''), 60)}"
    if name == "send_message":
        return f"→ {a.get('Recipient', '?')[:8]}… «{one_line(a.get('Message', ''), 80)}»"
    if name == "ask_question":
        try:
            qs = json.loads(a.get("questions", "[]")) if isinstance(a.get("questions"), str) else args.get("questions")
            return f"«{one_line(qs[0].get('question', ''), 100)}»" if qs else ""
        except (ValueError, AttributeError, IndexError, KeyError, TypeError):
            return ""
    rest = {k: v for k, v in a.items() if k not in ("toolSummary", "WaitMsBeforeAsync")}
    return one_line(json.dumps(rest, ensure_ascii=False), 140) if rest else ""


def clean_user(content: str) -> str:
    m = USER_REQ_RE.search(content or "")
    text = m.group(1) if m else META_RE.sub("", content or "")
    return text.strip()


def fence(text: str) -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text.rstrip()}\n{ticks}"


def export(conv: str, logs: str, full: bool, max_out: int) -> str:
    steps = load_steps(logs)
    models: List[str] = []
    title = ""
    first: Optional[dt.datetime] = None
    last: Optional[dt.datetime] = None
    body: List[str] = []
    pending: List[dict] = []  # tool calls waiting for their results
    turn = 0
    agent_header = False

    for st in steps:
        ts = parse_ts(st.get("created_at"))
        if ts:
            first = first or ts
            last = ts
        typ = st.get("type", "")
        content = st.get("content") or ""

        if typ == "USER_INPUT":
            for _old, new in MODEL_CHANGE_RE.findall(content):
                if new.strip() not in models:
                    models.append(new.strip())
            text = clean_user(content)
            if not text:
                continue
            turn += 1
            title = title or one_line(text, 80)
            when = ts.astimezone().strftime("%H:%M") if ts else ""
            skills = SKILL_RE.findall(content)
            body.append(f"\n---\n\n## {turn}. 👤 Пользователь {('· ' + when) if when else ''}\n")
            body.append(text)
            if skills:
                body.append(f"\n<sub>slash-команда: {', '.join('/' + s for s in skills)}</sub>")
            pending = []
            agent_header = False
            continue

        if typ == "PLANNER_RESPONSE":
            if not agent_header:
                body.append("\n### 🤖 Агент\n")
                agent_header = True
            thinking = (st.get("thinking") or "").strip()
            if full and thinking:
                body.append(f"\n<details><summary>💭 Размышления</summary>\n\n{thinking}\n\n</details>")
            if content.strip():
                body.append(f"\n{content.strip()}\n")
            calls = st.get("tool_calls") or []
            pending = list(calls)
            for tc in calls if not full else []:
                body.append(f"- 🔧 **{tc.get('name', '?')}** {summarize_call(tc.get('name', ''), tc.get('args') or {})}")
            continue

        if typ == "GENERIC" and full and pending:
            tc = pending.pop(0)
            out = content.strip()
            out = re.sub(r"^(Created At|Completed At):.*\n?", "", out, flags=re.M).strip()
            if len(out) > max_out:
                out = out[:max_out] + f"\n… [обрезано, всего {len(content)} символов]"
            status = " ⚠️ ошибка" if st.get("status") == "ERROR" else ""
            body.append(f"\n<details><summary>🔧 {tc.get('name', '?')} {summarize_call(tc.get('name', ''), tc.get('args') or {})}"
                        f"{status}</summary>\n\n{fence(out)}\n\n</details>")
            continue

    subs = []
    sub_dir = os.path.join(os.path.dirname(logs), "subagents")
    if os.path.isdir(sub_dir):
        for fn in sorted(os.listdir(sub_dir)):
            try:
                with open(os.path.join(sub_dir, fn), encoding="utf-8") as f:
                    d = json.load(f)
                desc = d.get("subagentDescriptor") or {}
                subs.append(f"- {desc.get('role', '?')} (`{desc.get('typeName', '?')}`) — `{d.get('conversationId', fn[:-5])}`")
            except (OSError, ValueError):
                continue

    head = [f"# {title or 'Разговор Antigravity'}", "",
            f"- **Разговор:** `{conv}`",
            f"- **Экспортировано:** {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    if first:
        head.append(f"- **Период:** {first.astimezone().strftime('%Y-%m-%d %H:%M')} — "
                    f"{(last or first).astimezone().strftime('%H:%M')}")
    if models:
        head.append(f"- **Модели:** {', '.join(models)}")
    head.append(f"- **Ходов:** {turn}")
    if subs:
        body.append("\n---\n\n## Субагенты\n")
        body.extend(subs)
    return "\n".join(head + body).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversation", "-c", help="conversation id (UUID)")
    ap.add_argument("--transcript", help="explicit path to transcript_full.jsonl or its logs dir")
    ap.add_argument("--out", "-o", help="write Markdown to this file")
    ap.add_argument("--clipboard", action="store_true", help="copy to the clipboard (pbcopy)")
    ap.add_argument("--full", action="store_true", help="include tool outputs and thinking")
    ap.add_argument("--max-output", type=int, default=2000, help="with --full: max chars per tool output")
    a = ap.parse_args(argv)

    if a.transcript:
        p = os.path.expanduser(a.transcript)
        logs = p if os.path.isdir(p) else os.path.dirname(p)
        conv = a.conversation or (UUID_RE.findall(logs) or ["?"])[-1]
    elif a.conversation:
        logs = logs_dir_for(a.conversation, PRODUCT_ROOTS)
        conv = a.conversation
        if not logs:
            print(f"Транскрипт для {a.conversation} не найден.", file=sys.stderr)
            return 2
    else:
        ap.error("укажите --conversation <id> или --transcript <path>")
        return 2
    try:
        md = export(conv, logs, a.full, a.max_output)
        sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "hooks"))
        import checks  # noqa: E402 — the same secret patterns as the edit check and the guard
        md = checks.redact(md)
    except AccessDenied as e:
        print(f"Нет доступа на чтение: {e}. Запустите вне песочницы.", file=sys.stderr)
        return 3

    if a.clipboard:
        subprocess.run(["pbcopy"], input=md, text=True, check=False)
        print(f"Скопировано в буфер обмена: {len(md)} символов.")
    if a.out or not a.clipboard:
        out = os.path.abspath(os.path.expanduser(a.out or f"conversation-{conv[:8]}.md"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Сохранено: {out} ({len(md.splitlines())} строк)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
