#!/usr/bin/env python3
"""Antigravity Plus — plan limits: the main agent hears when its quota is getting short.

Antigravity's plan limits come in quota groups («Gemini Models», «Claude and GPT models»), each with a weekly and a
5-hour window (language server RPC RetrieveUserQuotaSummary — the same reading the usage side panel and Claude Code's
delegate.py use). Before a model call of the main agent (hooks/instructions.py) this module reads them — with a new
user message, otherwise at most every RECHECK seconds; one reading is shared by all conversations for CACHE_TTL
seconds in ~/.gemini/antigravity-plus/quota.json — finds the group the conversation's model (payload modelName)
draws on, and says so when that group is
  * tight — weekly < 15 % left, 5 h < 10 % left, or the week spent far ahead of time (pace < PACE_TIGHT), or
  * exhausted — a window used up,
with the numbers, the reset time and what makes the rest last (flash subagents, a cheaper model, targeted reads,
/handoff). Once per level and quota window per conversation; tight → exhausted is told again. Subagents and tasks
delegated from Claude Code get nothing. Any failure (no language server, a timeout, an odd answer) is silent.
Setting quotaHints (`memory.py config quota-hints off`); AGP_QUOTA_CHECK=off turns the reading off (tests).
CLI: `quota.py [--fresh] [--json]` prints the groups as this module sees them. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

import compat
import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CACHE = os.path.expanduser("~/.gemini/antigravity-plus/quota.json")
SERVICE = "exa.language_server_pb.LanguageServerService"
CACHE_TTL = 300          # seconds one reading (or one failure) is shared by every conversation
RECHECK = 600            # seconds between readings within one turn of a conversation
TIMEOUT = 1.5            # seconds for the RPC
PROBE = 0.5              # seconds per /healthz probe while looking for the language server
WEEK = 7 * 86400.0
WEEK_TIGHT = 0.15        # weekly fraction left below which the group is tight
H5_TIGHT = 0.10          # 5-hour fraction left below which the group is tight
CRIT_LEFT = 0.05         # fraction left at or below which a zero-token critical UI badge fires once
PACE_TIGHT = 0.6         # weekly fraction left ÷ fraction of the week left: spending far ahead of time
NEW_WINDOW = 3600        # a reset this much later than the one already told about = a new window (readings jitter)
LEVELS = {"tight": 1, "exhausted": 2}
FAMILIES = (("claude", "gpt", "opus", "sonnet"), ("gemini", "orchestrator", "flash", "hydrogen", "carbon", "argon", "neon"))

Endpoint = Tuple[str, str]  # (host:port, CSRF token)
# the language server is local: no proxy (and no slow lookup of the system's proxy settings on macOS)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("quotaHints", True))
    except Exception:
        return True


# ─────────────────────────────── reading the limits ───────────────────────────────

def rpc(ep: Endpoint, method: str, timeout: float = TIMEOUT) -> dict:
    """POST {} to the language server; raises on any failure."""
    base = ep[0] if re.match(r"^https?://", ep[0]) else f"http://{ep[0]}"
    req = urllib.request.Request(f"{base}/{SERVICE}/{method}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                                          "x-codeium-csrf-token": ep[1]})
    with OPENER.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def discover() -> Optional[Endpoint]:
    """Antigravity's running language server, found by its process (as doctor.py and delegate.py find it)."""
    for pid, line in compat.processes("language_server"):
        if "--csrf_token" not in line or "antigravity" not in line.lower():
            continue
        tok = re.search(r"--csrf_token[ =]\"?([^\s\"]+)", line)
        if not tok:
            continue
        for port in compat.listening_ports(pid):
            try:
                with OPENER.open(f"http://127.0.0.1:{port}/healthz", timeout=PROBE) as r:
                    if json.loads(r.read().decode("utf-8") or "{}").get("status") == "ok":
                        return f"127.0.0.1:{port}", tok.group(1)
            except Exception:
                continue
    return None


def parse(data: dict) -> List[dict]:
    """RetrieveUserQuotaSummary → [{name, models (lower-case families), buckets {window: {left, reset, disabled}}}]."""
    groups = []
    for g in ((data.get("response") or {}).get("groups") or []):
        if not isinstance(g, dict):
            continue
        desc = str(g.get("description") or "")
        names = [n.strip().lower() for n in desc.split(":", 1)[1].split(",")] if ":" in desc else []
        buckets = {}
        for b in g.get("buckets") or []:
            if isinstance(b, dict):
                buckets[str(b.get("window") or b.get("bucketId") or "?")] = {
                    # proto3 JSON drops zeros: a bucket without remainingFraction has nothing left
                    "left": float(b.get("remainingFraction") or 0.0), "reset": str(b.get("resetTime") or ""),
                    "disabled": bool(b.get("disabled"))}
        groups.append({"name": str(g.get("displayName") or "?"), "models": names, "buckets": buckets})
    return groups


def cache_path() -> str:
    return os.environ.get("AGP_QUOTA_CACHE") or CACHE


def load_cache() -> dict:
    try:
        with open(cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data: dict) -> None:
    try:
        path = cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        compat.replace(tmp, path)
    except OSError:
        pass


def groups_now(now: Optional[float] = None, fresh: bool = False) -> List[dict]:
    """The plan's quota groups: from the shared cache while it is younger than CACHE_TTL, else from the language
    server (ANTIGRAVITY_LS_ADDRESS/ANTIGRAVITY_CSRF_TOKEN, or process discovery). [] on any failure — remembered for
    CACHE_TTL too, so a missing server isn't looked for on every step."""
    now = time.time() if now is None else now
    cache = load_cache()
    at = cache.get("at")
    if not fresh and isinstance(at, (int, float)) and 0 <= now - at < CACHE_TTL:
        return [g for g in cache.get("groups") or [] if isinstance(g, dict) and isinstance(g.get("buckets"), dict)]
    addr, tok = os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    ep: Optional[Endpoint] = (addr, tok) if addr and tok else discover()
    if ep:
        try:
            groups = parse(rpc(ep, "RetrieveUserQuotaSummary"))
            save_cache({"at": now, "groups": groups})
            return groups
        except Exception:
            pass
    save_cache({"at": now, "groups": [], "failed": True})
    return []


# ─────────────────────────────── judging them ───────────────────────────────

def parse_ts(ts: str) -> Optional[float]:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts or "")
    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))) if m else None


def span(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 36000:
        return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")
    if s < 172800:
        return f"{round(s / 3600)} h"
    return f"{s // 86400} days" + (f" {s % 86400 // 3600} h" if s % 86400 // 3600 else "")


def until(ts: str, now: float) -> str:
    t = parse_ts(ts)
    return "" if t is None else ("now" if t <= now else "in " + span(t - now))


def span_ru(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} с"
    if s < 5400:
        return f"{round(s / 60)} мин"
    if s < 36000:
        return f"{s // 3600} ч" + (f" {s % 3600 // 60} мин" if s % 3600 // 60 else "")
    if s < 172800:
        return f"{round(s / 3600)} ч"
    return f"{s // 86400} дн." + (f" {s % 86400 // 3600} ч" if s % 86400 // 3600 else "")


def until_ru(ts: str, now: float) -> str:
    t = parse_ts(ts)
    return "" if t is None else ("сейчас" if t <= now else "через " + span_ru(t - now))


def pct(v: float) -> str:
    return f"{v * 100:.1f}%" if 0 < v < 0.1 else f"{v * 100:.0f}%"


def family(label: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", label).strip().lower()


def group_for(model: str, groups: List[dict]) -> Optional[dict]:
    """The quota group `model` draws on: by its family («gemini 4 carbon») or whole label («orchestrator (carbon)»),
    as delegate.group_of() matches them, else by the maker's name in a model id («claude-sonnet-5-5-high»)."""
    fam, whole = family(model), (model or "").strip().lower()
    g = next((g for g in groups if (whole and whole in g["models"]) or (fam and fam in g["models"])), None)
    if g or not whole:
        return g
    for words in FAMILIES:
        if any(w in whole for w in words):
            return next((g for g in groups
                         if any(w in (g["name"] + " " + " ".join(g["models"])).lower() for w in words)), None)
    return None


def state(group: dict, now: float) -> dict:
    """{status: exhausted | tight | ok, week, h5 (fractions left or None), pace, reset (of the window that matters),
    window (the weekly window's reset, which keys «already told»), text}."""
    wk, h5 = group["buckets"].get("weekly"), group["buckets"].get("5h")
    if h5 and h5.get("disabled"):  # the 5-hour window doesn't apply while the weekly one is used up
        h5 = None
    week, five = (wk["left"] if wk else None), (h5["left"] if h5 else None)
    wreset = parse_ts(wk["reset"]) if wk else None
    share = min(max((wreset - now) / WEEK, 0.0), 1.0) if wreset else None
    pace = week / share if week is not None and share and share > 0.02 else None
    wtime, htime = (wk or {}).get("reset", ""), (h5 or {}).get("reset", "")
    window = wtime or htime
    w_level = 2 if (week is not None and week <= 0) else (1 if ((week is not None and week < WEEK_TIGHT)
                                                                or (pace is not None and pace < PACE_TIGHT)) else 0)
    h_level = 2 if (five is not None and five <= 0) else (1 if (five is not None and five < H5_TIGHT) else 0)
    if (week is not None and week <= 0) or (five is not None and five <= 0):
        weekly = week is not None and week <= 0
        reset = wtime if weekly else htime
        return {"status": "exhausted", "week": week, "h5": five, "pace": pace, "reset": reset, "window": window,
                "w_level": w_level, "h_level": h_level, "w_window": wtime, "h_window": htime,
                "text": f"the {'weekly' if weekly else '5-hour'} limit is used up — resets "
                        f"{until(reset, now) or 'later'}"}
    parts = []
    if week is not None:
        parts.append(f"week {pct(week)} left" + (f" (resets {until(wtime, now)})" if wtime else ""))
    if five is not None:
        parts.append(f"5 h {pct(five)} left" + (f" (resets {until(htime, now)})" if htime else ""))
    if week is not None and pace is not None and share is not None:
        parts.append(f"{pct(1 - week)} of the week's quota spent with {pct(1 - share)} of the week gone")
    tight = ((week is not None and week < WEEK_TIGHT) or (five is not None and five < H5_TIGHT)
             or (pace is not None and pace < PACE_TIGHT))
    reset = htime if five is not None and five < H5_TIGHT else wtime
    return {"status": "tight" if tight else "ok", "week": week, "h5": five, "pace": pace, "reset": reset,
            "window": window, "w_level": w_level, "h_level": h_level, "w_window": wtime, "h_window": htime,
            "text": ", ".join(parts) or "limits not reported"}


def render(model: str, group: dict, st: dict, others: List[Tuple[dict, dict]]) -> str:
    gemini = any(w in (group["name"] + " " + " ".join(group["models"])).lower() for w in FAMILIES[1])
    spare = [f"«{g['name']}» ({s['text']})" for g, s in others if s["status"] == "ok"]
    switch = ("a model from " + ", ".join(spare) + " draws on a separate quota" if spare else "")
    if gemini:
        # Antigravity books each call at its API price: per token Hydrogen counts 1, Neon 2, Carbon ~4 (2026-11-19). In
        # a benchmark on 2.33.0 (2026-11-20) Gemini 4 Hydrogen (Medium) got every task right on a third to a sixth of
        # the quota Neon (High), the Orchestrators and Carbon (High) used per task, and 2–5× faster.
        cheaper = ("a cheaper model: Gemini 4 Hydrogen (Medium) — it did every task of a benchmark right on a third to "
                   "a sixth of the quota Neon (High), Orchestrator or Carbon used, 2–5× faster" + (f"; {switch}" if switch else ""))
    else:
        cheaper = switch or "a cheaper model of this group"
    head = (f"Plan limits of «{group['name']}», which this conversation's model ({model}) draws on: {st['text']}.")
    if st["status"] == "exhausted":
        body = ("The model may stop answering until then. Finish the current step, keep task.md current and save "
                "/handoff so the work can continue; tell the user now, in one line, with the reset time, and suggest "
                f"switching to {cheaper}.")
    else:
        body = ("To make the rest last: hand routine parts (searches, docs, tests) to subagents with Model: flash; "
                "keep reads targeted (`rg -n`, line ranges) and command output trimmed; suggest to the user, once and "
                f"in one line with these numbers, switching to {cheaper}; and before the limit, at a natural pause, "
                "save /handoff so the work survives it.")
    return f'<quota source="Antigravity Plus">\n{head}\n{body}\n</quota>'


def render_ui(group: dict, st: dict, now: float, critical: bool = False) -> str:
    """One-line user-facing Markdown badge for tight, critical (<= 5%), or exhausted quota."""
    link = "[Расход токенов](sidecar://antigravity-plus/usage/)"
    if st["status"] == "exhausted":
        weekly = st["week"] is not None and st["week"] <= 0
        kind = "недельный лимит 0%" if weekly else "5-часовой лимит 0%"
        rst = until_ru(st["reset"], now)
        return f"⚠️ **Квота «{group['name']}» исчерпана** — {kind}" + (f" (сброс {rst})" if rst else "") + f". {link}"
    parts = []
    if st["week"] is not None:
        rst = until_ru(st["w_window"], now)
        parts.append(f"неделя {pct(st['week'])}" + (f" (сброс {rst})" if rst else ""))
    if st["h5"] is not None:
        rst = until_ru(st["h_window"], now)
        parts.append(f"5ч {pct(st['h5'])}" + (f" (сброс {rst})" if rst else ""))
    is_crit = critical or any(v is not None and 0 < v <= CRIT_LEFT for v in (st["week"], st["h5"]))
    icon = "🔴" if is_crit else "⏳"
    title = "на исходе" if is_crit else "заканчивается"
    detail = ", ".join(parts) or "лимит на исходе"
    return f"{icon} **Квота «{group['name']}» {title}** — {detail}. {link}"


def _crit_seen(entry: dict, bucket: str, ts: Optional[float]) -> bool:
    if bucket not in entry:
        return False
    prev = entry[bucket]
    if ts is not None and isinstance(prev, (int, float)) and ts - prev > NEW_WINDOW:
        return False
    return True


def _evaluate(payload: dict, st: Optional[dict], with_ui: bool) -> Optional[Tuple[str, str]]:
    """Evaluate quota for this model call; returns (model_msg, user_md) or None. Mutates st."""
    if not st or not st.get("users") or conv.delegated(st, payload):
        return None  # subagents and tasks from Claude Code: nobody to save the quota for
    if os.environ.get("AGP_QUOTA_CHECK", "").lower() == "off" or not setting():
        return None
    model = str(payload.get("modelName") or "").strip()
    if not model:
        return None
    q = st.setdefault("quota", {})
    users = len(conv.with_current(st, payload))
    now = time.time()
    checked = q.get("checked") if isinstance(q.get("checked"), (int, float)) else 0
    if users <= int(q.get("users") or 0) and 0 <= now - checked < RECHECK:
        return None
    q["users"], q["checked"] = users, now
    groups = groups_now(now)
    group = group_for(model, groups)
    if not group:
        return None
    s = state(group, now)
    told: Dict[str, dict] = q.setdefault("told", {})
    seen = told.get(group["name"]) or {}
    w_ts, h_ts = parse_ts(s["w_window"]), parse_ts(s["h_window"])
    seen_w_ts, seen_h_ts = seen.get("w_window"), seen.get("h_window")
    seen_w = int(seen.get("w_level") or 0)
    seen_h = int(seen.get("h_level") or 0)
    if w_ts is not None and isinstance(seen_w_ts, (int, float)) and w_ts - seen_w_ts > NEW_WINDOW:
        seen_w = 0  # the weekly window rolled over
    if h_ts is not None and isinstance(seen_h_ts, (int, float)) and h_ts - seen_h_ts > NEW_WINDOW:
        seen_h = 0  # the 5-hour window rolled over
    if s["w_level"] > seen_w or s["h_level"] > seen_h:
        told[group["name"]] = {
            "w_level": max(seen_w, s["w_level"]),
            "w_window": w_ts if w_ts is not None else seen_w_ts,
            "h_level": max(seen_h, s["h_level"]),
            "h_window": h_ts if h_ts is not None else seen_h_ts,
        }
        others = [(g, state(g, now)) for g in groups if g is not group]
        model_msg = render(model, group, s, others)
        user_md = ""
        if with_ui:
            user_md = render_ui(group, s, now)
            for bucket, left, b_ts in (("weekly", s["week"], w_ts), ("5h", s["h5"], h_ts)):
                if left is not None and 0 < left <= CRIT_LEFT:
                    crit_entry = st.setdefault("quota_ui_crit", {}).setdefault(group["name"], {})
                    crit_entry[bucket] = b_ts if b_ts is not None else True
        return model_msg, user_md
    if with_ui and conv.ui_notices_enabled():
        raw_crit = st.get("quota_ui_crit")
        crit_store = raw_crit if isinstance(raw_crit, dict) else {}
        raw_entry = crit_store.get(group["name"])
        crit_entry = raw_entry if isinstance(raw_entry, dict) else {}
        hits = []
        for bucket, left, b_ts, seen_lvl in (("weekly", s["week"], w_ts, seen_w), ("5h", s["h5"], h_ts, seen_h)):
            if seen_lvl >= 1 and left is not None and 0 < left <= CRIT_LEFT and not _crit_seen(crit_entry, bucket, b_ts):
                hits.append((bucket, b_ts))
        if hits:
            target = st.setdefault("quota_ui_crit", {}).setdefault(group["name"], {})
            for bucket, b_ts in hits:
                target[bucket] = b_ts if b_ts is not None else True
            return "", render_ui(group, s, now, critical=True)
    return None


def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """The quota hint for this model call, or None. Updates st["quota"] (the caller stores st)."""
    res = _evaluate(payload, st, with_ui=False)
    return res[0] if res and res[0] else None


def message_with_ui(payload: dict, st: Optional[dict]) -> Optional[Tuple[str, str]]:
    """(model_msg, user_md) for this model call, or None. Emits ("", user_md) once when a bucket drops <= 5%."""
    return _evaluate(payload, st, with_ui=True)


def main() -> int:
    fresh, as_json = "--fresh" in sys.argv[1:], "--json" in sys.argv[1:]
    now = time.time()
    groups = groups_now(now, fresh=fresh)
    rows = [{"name": g["name"], **state(g, now)} for g in groups]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("Plan limits: not available (Antigravity's language server didn't answer).")
    else:
        for r in rows:
            print(f"{r['name']}: {r['status']} — {r['text']}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
