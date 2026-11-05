#!/usr/bin/env python3
"""Antigravity Plus — /usage: token usage & API-equivalent cost report.

Reads Antigravity conversation transcripts
(~/.gemini/<product>/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl),
sums per-call token counters (input_tokens, cache_read_tokens, output_tokens),
follows subagent conversations, and prices everything with pricing.json.

Usage:
  usage.py --conversation <id>                 # markdown report for one session
  usage.py --conversation <id> --html out.html # + compact HTML dashboard
  usage.py --all [--days 7]                    # all sessions (needs read access to brain/)
  usage.py --conversation <id> --json          # everything the side panel shows, as JSON
  usage.py --recent 15                         # recent conversations + today / 7-day totals (JSON, cached)
  usage.py --transcript /path/to/transcript_full.jsonl

Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICING = os.path.join(os.path.dirname(HERE), "pricing.json")
PRODUCT_ROOTS = ["~/.gemini/antigravity", "~/.gemini/antigravity-cli", "~/.gemini/antigravity-ide"]
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
MODEL_CHANGE_RE = re.compile(r"Model Selection`\s+from\s+(.*?)\s+to\s+(.*?)\.(?:\s|$)")
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
NBSP = "\u00a0"


# ─────────────────────────────── data model ────────────────────────────────

@dataclass
class Call:
    step: int
    ts: Optional[dt.datetime]
    model: str
    input: int
    cache_read: int
    output: int
    tools: List[str]
    turn: int
    cost: float = 0.0
    cost_no_cache: float = 0.0
    cost_parts: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    guessed: bool = False  # model not named in the transcript → priced as default_model

    @property
    def prompt(self) -> int:
        return self.input + self.cache_read

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.output


@dataclass
class Turn:
    idx: int
    text: str
    ts: Optional[dt.datetime]
    calls: List[Call] = field(default_factory=list)


@dataclass
class Session:
    conv_id: str
    path: str
    calls: List[Call] = field(default_factory=list)
    turns: List[Turn] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    subagents: List["SubagentInfo"] = field(default_factory=list)
    first_ts: Optional[dt.datetime] = None
    last_ts: Optional[dt.datetime] = None
    title: str = ""
    guessed_calls: int = 0  # calls priced with default_model because the transcript never named the model


@dataclass
class SubagentInfo:
    conv_id: str
    role: str
    type_name: str
    model_arg: str
    session: Optional[Session]
    denied: bool = False


# ─────────────────────────────── pricing ───────────────────────────────────

class Pricing:
    def __init__(self, path: str, override_model: Optional[str] = None, service_tier: Optional[str] = None):
        with open(path, encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.path = path
        self.override = override_model
        self.service_tier = service_tier
        self.models = self.cfg.get("models", [])
        self.by_id = {m["id"]: m for m in self.models}

    def lookup(self, model_name: str) -> Optional[dict]:
        name = normalize_model(self.override or model_name or self.cfg.get("default_model", ""))
        if name in self.by_id:
            return self.by_id[name]
        for m in self.models:
            if re.search(m["match"], name, re.I):
                return m
        return None

    def alias(self, subagent_model_arg: str) -> Optional[str]:
        return self.cfg.get("subagent_model_aliases", {}).get(subagent_model_arg)

    def context_window(self, model_name: str) -> int:
        m = self.lookup(model_name)
        return (m or {}).get("context_window") or self.cfg.get("default_context_window", 200000)

    def rates(self, m: dict, ts: Optional[dt.datetime]) -> dict:
        """Prices of model entry `m` for a call made at `ts`: the chosen service tier (it replaces the
        standard prices and their price_changes), then every price change dated on or before that day."""
        t = (m.get("service_tiers") or {}).get(self.service_tier or "")
        r = {**m, "price_changes": [], **t} if t else m
        day = (ts or dt.datetime.now(dt.timezone.utc)).astimezone().strftime("%Y-%m-%d")
        for ch in sorted(r.get("price_changes") or [], key=lambda x: x["from"]):
            if ch["from"] <= day:
                r = {**r, **ch}
        return r

    def price_call(self, c: Call) -> None:
        m = self.lookup(c.model)
        if not m:
            c.cost = c.cost_no_cache = 0.0
            c.cost_parts = (0.0, 0.0, 0.0)
            return
        tier = self.rates(m, c.ts)
        lc = tier.get("long_context")
        if lc and c.prompt > lc.get("threshold", 10**12):
            tier = {**m, **lc}
        in_rate = tier.get("cache_write", tier["input"]) if m.get("uncached_input_as_cache_write") else tier["input"]
        p_in = c.input * in_rate / 1e6
        p_cr = c.cache_read * tier.get("cache_read", tier["input"]) / 1e6
        p_out = c.output * tier["output"] / 1e6
        c.cost_parts = (p_in, p_cr, p_out)
        c.cost = p_in + p_cr + p_out
        c.cost_no_cache = c.prompt * tier["input"] / 1e6 + p_out


def normalize_model(name: str) -> str:
    n = (name or "").strip().lower()
    n = re.sub(r"\(.*?\)", "", n)  # drop "(High)", "(Thinking)" …
    n = re.sub(r"^(anthropic|google|models)/", "", n)
    n = re.sub(r"\s+", "-", n.strip())
    return n


# ─────────────────────────────── parsing ───────────────────────────────────

def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def logs_dir_for(conv_id: str, roots: List[str]) -> Optional[str]:
    for r in roots:
        d = os.path.join(os.path.expanduser(r), "brain", conv_id, ".system_generated", "logs")
        if os.path.isfile(os.path.join(d, "transcript_full.jsonl")) or os.path.isfile(os.path.join(d, "transcript.jsonl")):
            return d
    return None


class AccessDenied(Exception):
    pass


def read_bytes(paths: List[str]) -> bytes:
    out = []
    for fp in paths:
        try:
            with open(fp, "rb") as f:
                out.append(f.read())
        except PermissionError as e:
            raise AccessDenied(fp) from e
        except OSError:
            continue
    return b"".join(out)


def load_steps(logs_dir: str) -> List[dict]:
    # chunks/<kind>/*.jsonl are a byte-wise split of the transcript into 100 KB pieces: a piece can end mid-line or
    # even mid-character, so they're joined before decoding. The main file usually holds the whole transcript too.
    streams: List[bytes] = []
    for kind in ("transcript_full", "transcript"):
        chunks = sorted(glob.glob(os.path.join(logs_dir, "chunks", kind, "*.jsonl")))
        main = os.path.join(logs_dir, f"{kind}.jsonl")
        if chunks or os.path.isfile(main):
            streams = [read_bytes(chunks), read_bytes([main] if os.path.isfile(main) else [])]
            break
    steps: Dict[int, dict] = {}
    extra: List[dict] = []
    for data in streams:
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "step_index" in d:
                steps[d["step_index"]] = d
            elif d not in extra:
                extra.append(d)
    return [steps[k] for k in sorted(steps)] + extra


def short(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def parse_session(conv_id: str, logs_dir: str, pricing: Pricing, roots: List[str],
                  inherited_model: Optional[str] = None, depth: int = 0,
                  seen: Optional[set] = None) -> Session:
    seen = seen if seen is not None else set()
    seen.add(conv_id)
    s = Session(conv_id=conv_id, path=logs_dir)
    model = inherited_model or ""
    turn: Optional[Turn] = None
    pending_subagents: List[dict] = []
    sub_ids: List[Tuple[str, dict]] = []

    for st in load_steps(logs_dir):
        ts = parse_ts(st.get("created_at"))
        if ts:
            s.first_ts = s.first_ts or ts
            s.last_ts = ts
        typ = st.get("type", "")
        content = st.get("content") or ""

        if typ == "USER_INPUT":
            for _old, new in MODEL_CHANGE_RE.findall(content):
                model = new.strip()
                if model not in s.models:
                    s.models.append(model)
            m = USER_REQ_RE.search(content)
            text = m.group(1) if m else content
            if st.get("source") == "USER_EXPLICIT" or m:
                turn = Turn(idx=len(s.turns) + 1, text=text, ts=ts)
                s.turns.append(turn)
                if not s.title:
                    s.title = short(text, 80)
            pending_subagents = []
            continue

        if typ == "PLANNER_RESPONSE":
            pending_subagents = []
            tools = [tc.get("name", "?") for tc in (st.get("tool_calls") or [])]
            for tc in st.get("tool_calls") or []:
                if tc.get("name") == "invoke_subagent":
                    pending_subagents.append(tc.get("args") or {})
            if any(k in st for k in ("input_tokens", "output_tokens", "cache_read_tokens")):
                if turn is None:
                    turn = Turn(idx=1, text="(начало)", ts=ts)
                    s.turns.append(turn)
                if not model:
                    s.guessed_calls += 1
                c = Call(step=st.get("step_index", -1), ts=ts, model=model or pricing.cfg.get("default_model", "?"),
                         input=int(st.get("input_tokens") or 0), cache_read=int(st.get("cache_read_tokens") or 0),
                         output=int(st.get("output_tokens") or 0), tools=tools, turn=turn.idx, guessed=not model)
                pricing.price_call(c)
                s.calls.append(c)
                turn.calls.append(c)
                if c.model not in s.models:
                    s.models.append(c.model)
            continue

        # tool results that follow an invoke_subagent call → look for conversation ids
        if pending_subagents:
            for uid in UUID_RE.findall(content):
                if uid not in seen and all(uid != x for x, _ in sub_ids):
                    args = pending_subagents.pop(0) if pending_subagents else {}
                    sub_ids.append((uid, args))
                    break

    if depth < 3:
        for uid, args in sub_ids:
            sub_dir = logs_dir_for(uid, roots)
            margs = args.get("Model", "inherit") or "inherit"
            sub_model = model if margs == "inherit" else (pricing.alias(margs) or margs)
            sub_sess = None
            denied = False
            if sub_dir and uid not in seen:
                try:
                    sub_sess = parse_session(uid, sub_dir, pricing, roots, inherited_model=sub_model,
                                             depth=depth + 1, seen=seen)
                except AccessDenied:
                    denied = True
                except Exception:  # never let a subagent break the report
                    sub_sess = None
            s.subagents.append(SubagentInfo(uid, args.get("Role", ""), args.get("TypeName", ""), margs, sub_sess, denied))
    return s


# ─────────────────────────────── aggregation ───────────────────────────────

@dataclass
class Totals:
    input: int = 0
    cache_read: int = 0
    output: int = 0
    cost: float = 0.0
    cost_no_cache: float = 0.0
    p_in: float = 0.0
    p_cr: float = 0.0
    p_out: float = 0.0
    calls: int = 0

    def add(self, c: Call):
        self.input += c.input
        self.cache_read += c.cache_read
        self.output += c.output
        self.cost += c.cost
        self.cost_no_cache += c.cost_no_cache
        self.p_in += c.cost_parts[0]
        self.p_cr += c.cost_parts[1]
        self.p_out += c.cost_parts[2]
        self.calls += 1

    def merge(self, o: "Totals"):
        for k in ("input", "cache_read", "output", "cost", "cost_no_cache", "p_in", "p_cr", "p_out", "calls"):
            setattr(self, k, getattr(self, k) + getattr(o, k))

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.output

    @property
    def hit_rate(self) -> float:
        p = self.input + self.cache_read
        return self.cache_read / p if p else 0.0


def totals_of(calls: List[Call]) -> Totals:
    t = Totals()
    for c in calls:
        t.add(c)
    return t


def subagent_totals(s: Session) -> Totals:
    t = Totals()
    for sa in s.subagents:
        if sa.session:
            t.merge(totals_of(sa.session.calls))
            t.merge(subagent_totals(sa.session))
    return t


# ─────────────────────────────── formatting ────────────────────────────────

def n(v: int) -> str:
    return f"{v:,}".replace(",", NBSP)


def k(v: float) -> str:
    if v >= 1e6:
        s = f"{v / 1e6:.2f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if v >= 1e3:
        s = f"{v / 1e3:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(int(v))


def usd(v: float) -> str:
    if v == 0:
        return "$0"
    if v < 0.001:
        return f"${v:.4f}"
    if v < 0.1:
        return f"${v:.3f}"
    return f"${v:,.2f}"


def bar(frac: float, width: int = 10) -> str:
    frac = max(0.0, min(1.0, frac))
    blocks = "▏▎▍▌▋▊▉█"
    full = int(frac * width)
    rem = frac * width - full
    s = "█" * full
    if full < width:
        idx = int(rem * 8)
        s += (blocks[idx - 1] if idx > 0 else "") 
        s += "░" * (width - len(s))
    return s


def ru_plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    word = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {word}"


def duration(a: Optional[dt.datetime], b: Optional[dt.datetime]) -> str:
    if not a or not b:
        return "—"
    sec = int((b - a).total_seconds())
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин"
    return f"{sec // 3600} ч {sec % 3600 // 60} мин"


def render_markdown(s: Session, pricing: Pricing, max_turns: int = 10) -> str:
    own = totals_of(s.calls)
    subs = subagent_totals(s)
    grand = Totals()
    grand.merge(own)
    grand.merge(subs)
    out: List[str] = []
    models = ", ".join(s.models) or "?"
    out.append("### 📊 Использование токенов — эта сессия")
    out.append("")
    out.append(f"**Модель:** {models} · **Ходов:** {len(s.turns)} · **Вызовов модели:** {own.calls}"
               + (f" (+{subs.calls} у субагентов)" if subs.calls else "")
               + f" · **Длительность:** {duration(s.first_ts, s.last_ts)}")
    out.append("")
    tot = grand.total or 1
    out.append("| Тип | Токены | Доля | ≈ API-цена |")
    out.append("|:--|--:|:--|--:|")
    out.append(f"| 🆕 Ввод (без кэша) | {n(grand.input)} | `{bar(grand.input / tot)}` {grand.input / tot:.0%} | {usd(grand.p_in)} |")
    out.append(f"| ♻️ Чтение из кэша | {n(grand.cache_read)} | `{bar(grand.cache_read / tot)}` {grand.cache_read / tot:.0%} | {usd(grand.p_cr)} |")
    out.append(f"| ✍️ Вывод (вкл. thinking) | {n(grand.output)} | `{bar(grand.output / tot)}` {grand.output / tot:.0%} | {usd(grand.p_out)} |")
    out.append(f"| **Итого** | **{n(grand.total)}** | | **{usd(grand.cost)}** |")
    out.append("")

    saved = grand.cost_no_cache - grand.cost
    line = f"**Попадание в кэш:** {grand.hit_rate:.0%}"
    if saved > 0:
        line += f" · **Кэш сэкономил:** {usd(saved)} (без кэша было бы {usd(grand.cost_no_cache)})"
    out.append(line)
    if s.calls:
        last = s.calls[-1]
        peak = max(s.calls, key=lambda c: c.prompt)
        win = pricing.context_window(last.model)
        out.append(f"**Контекст:** сейчас {k(last.prompt)} / {k(win)} `{bar(last.prompt / win)}` "
                   f"{last.prompt / win:.0%} · пик {k(peak.prompt)}")
        avg_turn = grand.cost / max(1, len(s.turns))
        out.append(f"**Средняя цена хода:** {usd(avg_turn)} · **Средний вывод на вызов:** {k(own.output / max(1, own.calls))}")
    out.append("")

    if s.turns:
        shown = s.turns[-max_turns:]
        hdr = "#### По ходам" + (f" (последние {len(shown)} из {len(s.turns)})" if len(shown) < len(s.turns) else "")
        out.append(hdr)
        out.append("")
        out.append("| # | Запрос | Вызовов | Ввод | Кэш | Вывод | $ |")
        out.append("|--:|:--|--:|--:|--:|--:|--:|")
        for t in shown:
            tt = totals_of(t.calls)
            out.append(f"| {t.idx} | {short(t.text, 48).replace('|', '∣')} | {tt.calls} | {k(tt.input)} | "
                       f"{k(tt.cache_read)} | {k(tt.output)} | {usd(tt.cost)} |")
        out.append("")

    if len(s.calls) >= 3:
        top = sorted(s.calls, key=lambda c: c.cost, reverse=True)[:5]
        out.append("#### Самые дорогие вызовы")
        out.append("")
        out.append("| Шаг | Ход | Промпт | Вывод | Инструменты | $ |")
        out.append("|--:|--:|--:|--:|:--|--:|")
        for c in top:
            tools = ", ".join(sorted(set(c.tools))) or "— (ответ)"
            out.append(f"| {c.step} | {c.turn} | {k(c.prompt)} | {k(c.output)} | {short(tools, 40)} | {usd(c.cost)} |")
        out.append("")

    tool_counts: Dict[str, int] = {}
    for c in s.calls:
        for t in c.tools:
            tool_counts[t] = tool_counts.get(t, 0) + 1
    if tool_counts:
        tc = sorted(tool_counts.items(), key=lambda x: -x[1])[:8]
        out.append("**Инструменты:** " + " · ".join(f"`{name}` ×{cnt}" for name, cnt in tc))
        out.append("")

    if s.subagents:
        out.append("#### Субагенты")
        out.append("")
        out.append("| Роль | Тип | Модель | Вызовов | Токены | $ |")
        out.append("|:--|:--|:--|--:|--:|--:|")
        for sa in s.subagents:
            if sa.session:
                st = totals_of(sa.session.calls)
                st.merge(subagent_totals(sa.session))
                out.append(f"| {short(sa.role, 30)} | {sa.type_name} | {sa.model_arg} | {st.calls} | {k(st.total)} | {usd(st.cost)} |")
            else:
                why = "нет доступа (песочница)" if sa.denied else "транскрипт не найден"
                out.append(f"| {short(sa.role, 30)} | {sa.type_name} | {sa.model_arg} | — | {why} | — |")
        out.append("")
        if any(sa.denied for sa in s.subagents):
            out.append("<sub>⚠️ Транскрипты субагентов недоступны из песочницы — перезапустите скрипт с BypassSandbox, чтобы включить их в итог.</sub>")
            out.append("")

    unknown = sorted({c.model for c in s.calls if not pricing.lookup(c.model)})
    notes = [f"Цены из `pricing.json` (обновлён {pricing.cfg.get('_updated', '?')}); это эквивалент стоимости через API, а не ваш реальный счёт."]
    if any((pricing.lookup(c.model) or {}).get("uncached_input_as_cache_write") for c in s.calls):
        notes.append("Для Claude некэшированный ввод считается как запись в кэш (×1.25).")
    if pricing.service_tier not in (None, "standard"):
        notes.append(f"Тариф {pricing.service_tier.capitalize()} — для моделей, у которых он есть в pricing.json, остальные по Standard.")
    if unknown:
        notes.append(f"Нет цены для: {', '.join(unknown)} — добавьте в pricing.json.")
    if s.guessed_calls:
        notes.append(f"Модель не записана в транскрипт (так бывает у разговоров, запущенных через agentapi) — "
                     f"{ru_plural(s.guessed_calls, 'вызов оценён', 'вызова оценены', 'вызовов оценены')} "
                     f"по цене {pricing.cfg.get('default_model', '?')}.")
    out.append("<sub>" + " ".join(notes) + "</sub>")
    return "\n".join(out)


# ─────────────────────────────── HTML dashboard ────────────────────────────

def render_html(s: Session, pricing: Pricing) -> str:
    own = totals_of(s.calls)
    grand = Totals()
    grand.merge(own)
    grand.merge(subagent_totals(s))
    last = s.calls[-1] if s.calls else None
    win = pricing.context_window(last.model) if last else 200000
    peak = max((c.prompt for c in s.calls), default=0)

    # stacked bars per call
    W, H, pad = 640, 150, 4
    calls = s.calls[-120:]
    maxv = max((c.total for c in calls), default=1) or 1
    bw = max(2.0, (W - pad * 2) / max(1, len(calls)))
    rects = []
    for i, c in enumerate(calls):
        x = pad + i * bw
        y = H
        for val, color, label in ((c.cache_read, "#a78bfa", "кэш"), (c.input, "#60a5fa", "ввод"), (c.output, "#34d399", "вывод")):
            h = val / maxv * (H - 10)
            y -= h
            if h > 0:
                rects.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1.0, bw - 1):.1f}" height="{h:.1f}" fill="{color}" rx="1">'
                             f'<title>шаг {c.step} · ход {c.turn}\nкэш {n(c.cache_read)} · ввод {n(c.input)} · вывод {n(c.output)}\n{usd(c.cost)}</title></rect>')
    # context window line
    ctx_y = H - min(1.0, win / maxv) * (H - 10)
    ctx_line = (f'<line x1="0" x2="{W}" y1="{ctx_y:.1f}" y2="{ctx_y:.1f}" stroke="currentColor" stroke-dasharray="4 4" opacity=".35"/>'
                if win <= maxv * 1.05 else "")

    turns_rows = []
    max_turn_cost = max((totals_of(t.calls).cost for t in s.turns), default=0) or 1
    for t in s.turns[-6:]:
        tt = totals_of(t.calls)
        turns_rows.append(
            f'<div class="flex items-center gap-3 text-xs"><span class="w-6 text-right text-[var(--muted-foreground)]">{t.idx}</span>'
            f'<span class="flex-1 truncate" title="{html.escape(short(t.text, 200))}">{html.escape(short(t.text, 70))}</span>'
            f'<span class="w-28 h-2 rounded bg-[var(--border)] overflow-hidden"><span class="block h-2 bg-[var(--primary)]" style="width:{tt.cost / max_turn_cost * 100:.0f}%"></span></span>'
            f'<span class="w-16 text-right tabular-nums">{usd(tt.cost)}</span></div>')

    def kpi(label, value, sub=""):
        return (f'<div class="rounded-lg border border-[var(--border)] p-3"><div class="text-xs text-[var(--muted-foreground)]">{label}</div>'
                f'<div class="text-lg font-semibold tabular-nums">{value}</div><div class="text-xs text-[var(--muted-foreground)]">{sub}</div></div>')

    saved = grand.cost_no_cache - grand.cost
    kpis = "".join([
        kpi("Всего токенов", k(grand.total), f"{own.calls} вызовов · {len(s.turns)} ходов"),
        kpi("≈ API-цена", usd(grand.cost), f"кэш сэкономил {usd(saved)}" if saved > 0 else ""),
        kpi("Попадание в кэш", f"{grand.hit_rate:.0%}", f"вывод {k(grand.output)}"),
        kpi("Контекст", f"{k(last.prompt) if last else 0} / {k(win)}", f"пик {k(peak)}"),
    ])
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script></head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-4">
<div class="bg-[var(--card)] border border-[var(--border)] rounded-xl p-4 shadow-sm space-y-3">
  <div class="flex items-baseline justify-between gap-2">
    <h2 class="font-semibold">📊 Использование · {html.escape(", ".join(s.models) or "?")}</h2>
    <span class="text-xs text-[var(--muted-foreground)]">{duration(s.first_ts, s.last_ts)}</span>
  </div>
  <div class="grid grid-cols-4 gap-2">{kpis}</div>
  <div>
    <div class="flex gap-4 text-xs text-[var(--muted-foreground)] mb-1">
      <span><span class="inline-block w-2 h-2 rounded-sm" style="background:#a78bfa"></span> кэш</span>
      <span><span class="inline-block w-2 h-2 rounded-sm" style="background:#60a5fa"></span> новый ввод</span>
      <span><span class="inline-block w-2 h-2 rounded-sm" style="background:#34d399"></span> вывод</span>
      <span class="ml-auto">токены на каждый вызов модели (наведите для деталей)</span>
    </div>
    <svg viewBox="0 0 {W} {H}" class="w-full" style="height:{H}px" preserveAspectRatio="none">{ctx_line}{''.join(rects)}</svg>
  </div>
  <div class="space-y-1">{''.join(turns_rows)}</div>
</div></body></html>"""


# ─────────────────────────────── --all mode ────────────────────────────────

def render_all(roots: List[str], pricing: Pricing, days: Optional[int], limit: int) -> str:
    rows = []
    errors = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days) if days else None
    for r in roots:
        brain = os.path.join(os.path.expanduser(r), "brain")
        if not os.path.isdir(brain):
            continue
        try:
            ids = os.listdir(brain)
        except PermissionError:
            errors.append(brain)
            continue
        for cid in ids:
            d = os.path.join(brain, cid, ".system_generated", "logs")
            if not os.path.isdir(d):
                continue
            try:
                s = parse_session(cid, d, pricing, roots, depth=99)  # depth=99: don't follow subagents (they are listed themselves)
            except AccessDenied:
                errors.append(d)
                continue
            if not s.calls:
                continue
            if cutoff and s.last_ts and s.last_ts < cutoff:
                continue
            rows.append((s, totals_of(s.calls), os.path.basename(os.path.expanduser(r))))
    if not rows:
        msg = "Нет данных."
        if errors:
            msg += (" Нет доступа на чтение к: " + ", ".join(errors) +
                    ". Запустите команду вне песочницы (BypassSandbox) или укажите --conversation.")
        return msg
    rows.sort(key=lambda x: x[0].last_ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    grand = Totals()
    by_day: Dict[str, Totals] = {}
    by_model: Dict[str, Totals] = {}
    for s, t, _ in rows:
        grand.merge(t)
        for c in s.calls:
            day = (c.ts or s.last_ts).astimezone().strftime("%Y-%m-%d") if (c.ts or s.last_ts) else "?"
            by_day.setdefault(day, Totals()).add(c)
            by_model.setdefault(c.model, Totals()).add(c)
    out = [f"### 📊 Использование — все сессии" + (f" за {days} дн." if days else "")]
    out.append("")
    out.append(f"**Сессий:** {len(rows)} · **Вызовов модели:** {grand.calls} · **Токенов:** {n(grand.total)} · "
               f"**≈ API-цена:** {usd(grand.cost)} · **Кэш:** {grand.hit_rate:.0%}")
    out.append("")
    out.append("#### По дням")
    out.append("")
    out.append("| День | Токены | | $ |")
    out.append("|:--|--:|:--|--:|")
    maxc = max((t.cost for t in by_day.values()), default=1) or 1
    for day in sorted(by_day)[-14:]:
        t = by_day[day]
        out.append(f"| {day} | {k(t.total)} | `{bar(t.cost / maxc, 16)}` | {usd(t.cost)} |")
    out.append("")
    out.append("#### По моделям")
    out.append("")
    out.append("| Модель | Вызовов | Токены | $ |")
    out.append("|:--|--:|--:|--:|")
    for m, t in sorted(by_model.items(), key=lambda x: -x[1].cost):
        out.append(f"| {m} | {t.calls} | {k(t.total)} | {usd(t.cost)} |")
    out.append("")
    out.append(f"#### Сессии (последние {min(limit, len(rows))})")
    out.append("")
    out.append("| Когда | Сессия | Вызовов | Токены | $ |")
    out.append("|:--|:--|--:|--:|--:|")
    for s, t, _ in rows[:limit]:
        when = s.last_ts.astimezone().strftime("%m-%d %H:%M") if s.last_ts else "?"
        out.append(f"| {when} | [{short(s.title or s.conv_id, 50).replace('|', '∣')}](conversation://{s.conv_id}) | "
                   f"{t.calls} | {k(t.total)} | {usd(t.cost)} |")
    if errors:
        out.append("")
        out.append(f"<sub>Нет доступа к: {', '.join(errors)}</sub>")
    return "\n".join(out)


# ─────────────────────────────── JSON (side panel) ─────────────────────────

CACHE_PATH = os.path.expanduser("~/.gemini/antigravity-plus/usage-cache.json")
CACHE_VERSION = 2


def _iso(ts: Optional[dt.datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def _tot(t: Totals) -> dict:
    return {"input": t.input, "cache_read": t.cache_read, "output": t.output, "total": t.total,
            "cost": round(t.cost, 6), "cost_no_cache": round(t.cost_no_cache, 6),
            "saved": round(max(0.0, t.cost_no_cache - t.cost), 6), "hit_rate": round(t.hit_rate, 4),
            "cost_input": round(t.p_in, 6), "cost_cache_read": round(t.p_cr, 6), "cost_output": round(t.p_out, 6),
            "calls": t.calls}


def _all_calls(s: Session) -> List[Call]:
    out = list(s.calls)
    for sa in s.subagents:
        if sa.session:
            out.extend(_all_calls(sa.session))
    return out


def _call(c: Call) -> dict:
    return {"step": c.step, "turn": c.turn, "ts": _iso(c.ts), "model": c.model, "prompt": c.prompt,
            "input": c.input, "cache_read": c.cache_read, "output": c.output, "cost": round(c.cost, 6), "tools": c.tools}


def _context(s: Session, pricing: Pricing) -> Optional[dict]:
    if not s.calls:
        return None
    last = s.calls[-1]
    win = pricing.context_window(last.model)
    return {"current": last.prompt, "peak": max(c.prompt for c in s.calls), "window": win, "model": last.model,
            "fraction": round(last.prompt / win, 4) if win else None}


def session_json(s: Session, pricing: Pricing) -> dict:
    """Everything the side panel shows for one conversation (own calls + subagents)."""
    own, subs = totals_of(s.calls), subagent_totals(s)
    grand = Totals()
    grand.merge(own)
    grand.merge(subs)
    tools: Dict[str, int] = {}
    for c in s.calls:
        for t in c.tools:
            tools[t] = tools.get(t, 0) + 1
    sub_rows = []
    for sa in s.subagents:
        st = Totals()
        if sa.session:
            st.merge(totals_of(sa.session.calls))
            st.merge(subagent_totals(sa.session))
        sub_rows.append({"id": sa.conv_id, "role": sa.role, "type": sa.type_name, "model": sa.model_arg,
                         "denied": sa.denied, "available": sa.session is not None, **_tot(st)})
    return {
        "conversation": s.conv_id, "title": s.title, "models": s.models,
        "first_ts": _iso(s.first_ts), "last_ts": _iso(s.last_ts), "duration": duration(s.first_ts, s.last_ts),
        "totals": _tot(grand), "own": _tot(own), "subagents_totals": _tot(subs),
        "context": _context(s, pricing),
        "turns": [{"idx": t.idx, "text": short(t.text, 160), "ts": _iso(t.ts), **_tot(totals_of(t.calls))} for t in s.turns],
        "calls": [_call(c) for c in s.calls[-400:]],
        "top_calls": [_call(c) for c in sorted(s.calls, key=lambda c: -c.cost)[:5]],
        "tools": dict(sorted(tools.items(), key=lambda x: -x[1])),
        "subagents": sub_rows,
        "unpriced_models": sorted({c.model for c in _all_calls(s) if not pricing.lookup(c.model)}),
        "model_guessed_calls": s.guessed_calls,
        "default_model": pricing.cfg.get("default_model", ""),
        "pricing_updated": pricing.cfg.get("_updated", ""),
    }


def _summary(s: Session, pricing: Pricing) -> dict:
    """Compact per-conversation record for the recent list, with (day|model) cost buckets."""
    calls = _all_calls(s)
    grand = totals_of(calls)
    buckets: Dict[str, List[float]] = {}
    for c in calls:
        ts = c.ts or s.last_ts
        key = f"{ts.astimezone().strftime('%Y-%m-%d') if ts else '?'}|{'?' if c.guessed else ''}{c.model}"
        b = buckets.setdefault(key, [0.0, 0, 0])
        b[0] += c.cost
        b[1] += c.total
        b[2] += 1
    return {"id": s.conv_id, "title": s.title, "first_ts": _iso(s.first_ts), "last_ts": _iso(s.last_ts),
            "cost": round(grand.cost, 6), "tokens": grand.total, "calls": grand.calls, "turns": len(s.turns),
            "models": s.models, "context": _context(s, pricing), "model_guessed": s.guessed_calls > 0,
            "unpriced_models": sorted({c.model for c in calls if not pricing.lookup(c.model)}), "buckets": buckets}


def _signature(logs: str, roots: List[str]) -> str:
    """Cheap change detector: sizes and mtimes of the transcript files, including subagents'."""
    parts = []
    dirs = [logs]
    sub_dir = os.path.join(os.path.dirname(logs), "subagents")
    if os.path.isdir(sub_dir):
        for fn in sorted(os.listdir(sub_dir)):
            sub_logs = logs_dir_for(fn[:-5], roots) if fn.endswith(".json") else None
            if sub_logs:
                dirs.append(sub_logs)
    for d in dirs:
        for fp in sorted(glob.glob(os.path.join(d, "*.jsonl")) + glob.glob(os.path.join(d, "chunks", "*", "*.jsonl"))):
            try:
                st = os.stat(fp)
                parts.append(f"{fp}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                continue
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def recent_json(roots: List[str], pricing: Pricing, limit: int = 15, days: int = 7) -> dict:
    """Recent top-level conversations + today / last-N-days totals, cached by transcript signature."""
    try:
        with open(pricing.path, "rb") as f:
            pricing_sig = hashlib.sha1(f.read() + f"{pricing.override or ''}|{pricing.service_tier or ''}".encode()).hexdigest()
    except OSError:
        pricing_sig = ""
    cache: dict = {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("version") != CACHE_VERSION or cache.get("pricing") != pricing_sig:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    old = cache.get("items") or {}

    entries, sub_ids, errors = [], set(), []
    for r in roots:
        brain = os.path.join(os.path.expanduser(r), "brain")
        try:
            ids = os.listdir(brain)
        except FileNotFoundError:
            continue
        except PermissionError:
            errors.append(brain)
            continue
        for cid in ids:
            if not UUID_RE.fullmatch(cid):
                continue
            base = os.path.join(brain, cid, ".system_generated")
            sd = os.path.join(base, "subagents")
            if os.path.isdir(sd):
                sub_ids.update(fn[:-5] for fn in os.listdir(sd) if fn.endswith(".json"))
            if os.path.isdir(os.path.join(base, "logs")):
                entries.append((cid, os.path.join(base, "logs")))

    items, summaries = {}, []
    for cid, logs in entries:
        if cid in sub_ids:  # counted inside their parent conversation
            continue
        sig = _signature(logs, roots)
        hit = old.get(logs)
        if hit and hit.get("sig") == sig:
            summ = hit.get("summary")
        else:
            try:
                s = parse_session(cid, logs, pricing, roots)
            except AccessDenied:
                errors.append(logs)
                continue
            except Exception:  # one broken transcript must not break the panel
                continue
            summ = _summary(s, pricing) if s.calls else None
        items[logs] = {"sig": sig, "summary": summ}
        if summ:
            summaries.append(summ)
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "pricing": pricing_sig, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass

    now = dt.datetime.now().astimezone()
    day_keys = [(now - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    by_day = {d: [0.0, 0, 0] for d in day_keys}
    by_model: Dict[str, List[float]] = {}
    for summ in summaries:
        for key, (cost, tokens, calls) in (summ.get("buckets") or {}).items():
            day, model = key.split("|", 1)
            if day not in by_day:
                continue
            for b in (by_day[day], by_model.setdefault(model, [0.0, 0, 0])):
                b[0] += cost
                b[1] += tokens
                b[2] += calls

    def agg(b: List[float]) -> dict:
        return {"cost": round(b[0], 6), "tokens": int(b[1]), "calls": int(b[2])}

    period = [sum(b[i] for b in by_day.values()) for i in range(3)]
    summaries.sort(key=lambda x: x.get("last_ts") or "", reverse=True)
    return {
        "generated": now.isoformat(), "days": days,
        "today": agg(by_day[day_keys[-1]]), "period": agg(period),
        "by_day": [{"day": d, **agg(by_day[d])} for d in day_keys],
        # a leading "?" marks calls whose model the transcript never named (priced as default_model)
        "by_model": [{"model": m.lstrip("?"), "guessed": m.startswith("?"), "priced": pricing.lookup(m.lstrip("?")) is not None,
                      **agg(b)} for m, b in sorted(by_model.items(), key=lambda x: -x[1][0])],
        "conversations": [{k_: v for k_, v in summ.items() if k_ != "buckets"} for summ in summaries[:limit]],
        "errors": errors,
        "pricing_updated": pricing.cfg.get("_updated", ""),
    }


# ─────────────────────────────── main ──────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversation", "-c", help="conversation id (UUID)")
    ap.add_argument("--transcript", help="explicit path to transcript_full.jsonl (or its logs dir)")
    ap.add_argument("--all", action="store_true", help="report across all sessions")
    ap.add_argument("--days", type=int, default=None,
                    help="only sessions active in the last N days (implies --all unless a conversation is given)")
    ap.add_argument("--limit", type=int, default=15, help="with --all: max sessions listed")
    ap.add_argument("--turns", type=int, default=10, help="max turns shown in the per-turn table")
    ap.add_argument("--model", help="force pricing model (e.g. 'claude-opus-5.5', 'gemini-3-pro')")
    ap.add_argument("--tier", choices=["standard", "batch", "flex", "priority", "contributor"],
                    help="Gemini API service tier; models without it in pricing.json keep standard prices")
    ap.add_argument("--pricing", default=DEFAULT_PRICING, help="path to pricing.json")
    ap.add_argument("--root", action="append", help="product data root(s), default: ~/.gemini/antigravity{,-cli,-ide}")
    ap.add_argument("--html", help="also write a compact HTML dashboard to this path")
    ap.add_argument("--json", action="store_true", help="print the session as JSON (used by the side panel)")
    ap.add_argument("--recent", type=int, metavar="N", help="JSON: N recent conversations + today / --days totals")
    a = ap.parse_args(argv)

    roots = a.root or PRODUCT_ROOTS
    pricing = Pricing(a.pricing, a.model, a.tier)

    if a.recent:
        print(json.dumps(recent_json(roots, pricing, a.recent, a.days or 7), ensure_ascii=False))
        return 0
    if a.all or (a.days and not a.conversation and not a.transcript):
        print(render_all(roots, pricing, a.days, a.limit))
        return 0

    if a.transcript:
        p = os.path.expanduser(a.transcript)
        logs = p if os.path.isdir(p) else os.path.dirname(p)
        conv = a.conversation or (UUID_RE.findall(logs) or ["?"])[-1]
    elif a.conversation:
        logs = logs_dir_for(a.conversation, roots)
        conv = a.conversation
        if not logs:
            print(f"Транскрипт для {a.conversation} не найден в: {', '.join(roots)}", file=sys.stderr)
            return 2
    else:
        ap.error("укажите --conversation <id>, --transcript <path> или --all")
        return 2

    try:
        s = parse_session(conv, logs, pricing, roots)
    except AccessDenied as e:
        print(f"Нет доступа на чтение: {e}. Запустите вне песочницы.", file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(session_json(s, pricing), ensure_ascii=False))
        return 0

    print(render_markdown(s, pricing, a.turns))
    if a.html:
        os.makedirs(os.path.dirname(os.path.abspath(a.html)), exist_ok=True)
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(render_html(s, pricing))
        print(f"\n<!-- html: {os.path.abspath(a.html)} -->")
    return 0


if __name__ == "__main__":
    sys.exit(main())
