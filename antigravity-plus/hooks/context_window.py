#!/usr/bin/env python3
"""Antigravity Plus — the context windows Antigravity really gives its models.

Antigravity sets each model's window on its own servers (maxContextTokens) and compacts a conversation as soon as the
next model call wouldn't fit it; the number changes without an app release (Gemini 4 went 256k → 512k on 2026-11-16).
skills/usage/pricing.json and hooks/speed.py carry the numbers known at release. This module keeps what Antigravity
itself reported for recent calls, per model, in ~/.gemini/antigravity-plus/context-windows.json, so /usage, /context,
the Control Center and the context hints follow such a change by themselves:

  * a reading is one model call's generator metadata (language server RPC GetCascadeTrajectoryGeneratorMetadata →
    chatModel.chatStartMetadata.contextWindowMetadata.maxContextTokens, named by chatModel.modelDisplayName and
    responseModel); `generatorMetadataOffset` asks for the latest calls only (~0.3 MB a call: the system prompt
    and the tool schemas come along);
  * only calls made within FRESH seconds are recorded, and the newest call per model wins — reading an old
    conversation never brings an old window back;
  * key() makes «Gemini 4 Neon (High)», gemini-4-neon-high and gemini-4-neon one model.

usage.py records the readings it makes (the side panel reads the shown conversation's latest call); hooks/speed.py
reads its own conversation's latest call once it has made one and the model has no reading younger than STALE — only
the last calls, never more than HOOK_BYTES.
The language server is found the way usage.py finds it — ANTIGRAVITY_LS_ADDRESS + ANTIGRAVITY_CSRF_TOKEN, else
~/.gemini/antigravity/daemon/hub_*.json — never by scanning processes. Failures are silent: callers keep their own
numbers. AGP_WINDOW_CHECK=off stops the readings, AGP_WINDOW_CACHE moves the file (tests).
CLI: `context_window.py [--json]` prints the windows known now. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sys
import time
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import compat

CACHE = os.path.expanduser("~/.gemini/antigravity-plus/context-windows.json")
SERVICE = "exa.language_server_pb.LanguageServerService"
FRESH = 86400            # seconds: a call older than this isn't recorded (Antigravity may have changed the window since)
STALE = 6 * 3600         # seconds: a model whose newest reading is older is read again when its context grows
TIMEOUT = 1.5            # seconds for the RPC (hooks/quota.py's too)
PROBE = 0.4              # seconds for the /healthz probe of a server found by its daemon file
PROBES = 3               # daemon files probed at most (on Windows a stale file's process can't be told dead)
HOOK_BYTES = 4_000_000   # a hook's reading gives up on a bigger answer (one call's entry is ~0.3 MB, the rest ~3 KB each)
KEEP = 200               # models kept in the file
LOWEST, HIGHEST = 4_000, 50_000_000  # a window outside this range is a misreading
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EFFORT_RE = re.compile(r"(?:-tiered)?(?:-(?:minimal|low|medium|high|xhigh|thinking))?$")
PLACEHOLDER_RE = re.compile(r"model[-_ ]placeholder", re.I)  # an enum (MODEL_PLACEHOLDER_M527), not a model's name
TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the language server is local: no proxy

Endpoint = Tuple[str, str]                         # (host:port, CSRF token)
Reading = Tuple[List[str], int, Optional[float]]   # (the model's names, its window, when the call was made)


def off() -> bool:
    return os.environ.get("AGP_WINDOW_CHECK", "").strip().lower() == "off"


def key(model: str) -> str:
    """One name per model, whatever form it comes in: «Gemini 4 Neon (High)», gemini-4-neon-high and gemini-4-neon →
    gemini-4-neon; gemini-4-hydrogen-tiered-high and «Gemini 4 Hydrogen (Orchestrator)» → gemini-4-hydrogen;
    «Orchestrator (Neon)» and gemini-pro-orchestrator-neon → orchestrator-neon (gemini-pro-orchestrator →
    orchestrator-carbon); «Claude Sonnet 5.5 (High)» and claude-sonnet-5-5-high → claude-sonnet-5-5. "" for nothing."""
    n = (model or "").strip().lower()
    n = re.sub(r"\borchestrator\s*\(\s*([a-z0-9]+)\s*\)", r"orchestrator-\1", n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"^(?:anthropic|google|models)/", "", n.strip())
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    n = EFFORT_RE.sub("", n, count=1)
    n = re.sub(r"^gemini-pro-orchestrator(?=-|$)", "orchestrator", n)
    return "orchestrator-carbon" if n == "orchestrator" else n


def epoch(ts: str) -> Optional[float]:
    """A transcript timestamp («2026-11-25T10:32:30Z», with fractions or an offset) in seconds; None if it isn't one."""
    m = TS_RE.match((ts or "").strip())
    if not m:
        return None
    try:
        sec = float(calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7)) + (0, 0, 0)))
    except (ValueError, OverflowError):
        return None
    zone = m.group(7) or "Z"
    if zone != "Z":
        sign = -1 if zone[0] == "+" else 1
        hh, mm = int(zone[1:3]), int(zone[-2:])
        sec += sign * (hh * 3600 + mm * 60)
    return sec


def sane(window: object) -> bool:
    return isinstance(window, int) and not isinstance(window, bool) and LOWEST <= window <= HIGHEST


# ─────────────────────────────── the file ───────────────────────────────

def cache_path() -> str:
    return os.environ.get("AGP_WINDOW_CACHE") or CACHE


def load() -> Dict[str, dict]:
    """{key: {window, at (the call's time), name, seen (when recorded)}}; {} when missing or unreadable."""
    try:
        with open(cache_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return {}
    return {k: v for k, v in models.items() if isinstance(k, str) and isinstance(v, dict) and sane(v.get("window"))
            and isinstance(v.get("at"), (int, float)) and not isinstance(v.get("at"), bool)}


def _save(models: Dict[str, dict]) -> None:
    path = cache_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "models": models}, f, ensure_ascii=False, indent=1, sort_keys=True)
        compat.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def lookup(model: str) -> Optional[Tuple[int, float]]:
    """(window, when the call that showed it was made) of the newest reading for `model`; None without one."""
    k = key(model)
    v = load().get(k) if k else None
    return (int(v["window"]), float(v["at"])) if v else None


def window(model: str) -> Optional[int]:
    """The newest window Antigravity reported for `model`, or None."""
    found = lookup(model)
    return found[0] if found else None


def record(readings: Iterable[Reading], now: Optional[float] = None) -> int:
    """Keep each reading of a call made within FRESH seconds whose model has no newer one; returns how many models
    changed (the file is written only then)."""
    now = time.time() if now is None else now
    fresh = [(names, win, round(at, 3)) for names, win, at in readings  # rounded as stored: compared with the file's
             if sane(win) and at is not None and now - FRESH <= at <= now + 300]
    if not fresh:
        return 0
    with _Locked():
        return _merge(fresh, now)


class _Locked:
    """An exclusive lock on the file's .lock beside it while one process merges its readings in (the hooks of several
    conversations and the side panel write the same file). Without a lock file (an unwritable folder) it goes on
    unlocked: saving would fail there anyway."""

    def __enter__(self) -> "_Locked":
        self.fh = None
        try:
            path = cache_path()
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path + ".lock", "w")
            compat.lock(self.fh)
        except OSError:
            if self.fh:
                self.fh.close()
            self.fh = None
        return self

    def __exit__(self, *exc) -> None:
        if self.fh:
            compat.unlock(self.fh)
            self.fh.close()


def _merge(fresh: List[Reading], now: float) -> int:
    models = load()
    changed = 0
    for names, win, at in sorted(fresh, key=lambda r: r[2]):
        for name in names:
            k = key(name)
            if not k or PLACEHOLDER_RE.search(name):
                continue
            old = models.get(k)
            prev = float(old["at"]) if old else -1.0
            if prev > at or (prev == at and old and old.get("window") == win):
                continue  # a newer call (or this very one) already said
            models[k] = {"window": win, "at": at, "name": str(name)[:80], "seen": round(now, 3)}
            changed += 1
    if changed:
        if len(models) > KEEP:
            models = dict(sorted(models.items(), key=lambda kv: kv[1]["at"])[-KEEP:])
        _save(models)
    return changed


# ─────────────────────────────── Antigravity ───────────────────────────────

def readings(entries: Iterable[dict], when: Optional[Callable[[int], Optional[float]]] = None) -> List[Reading]:
    """The window of every model call in `entries` (generatorMetadata of GetCascadeTrajectoryGeneratorMetadata) with
    its model's names; `when(step index)` gives the call's time (None: unknown — such a reading is never recorded)."""
    out: List[Reading] = []
    for g in entries or []:
        cm = g.get("chatModel") if isinstance(g, dict) else None
        if not isinstance(cm, dict):
            continue
        start = cm.get("chatStartMetadata")
        meta = start.get("contextWindowMetadata") if isinstance(start, dict) else None
        raw = meta.get("maxContextTokens") if isinstance(meta, dict) else None
        if isinstance(raw, bool):
            continue
        try:
            win = int(raw)  # proto3 JSON may write an int64 as a string
        except (TypeError, ValueError):
            continue
        names = [str(cm.get(f) or "") for f in ("modelDisplayName", "responseModel")]
        names = [n for n in names if n.strip() and not PLACEHOLDER_RE.search(n)]
        if not names or not sane(win):
            continue
        steps = [int(i) for i in g.get("stepIndices") or [] if str(i).isdigit()]
        at = when(steps[0]) if when and steps else None
        out.append((names, win, at))
    return out


def endpoint() -> Optional[Endpoint]:
    """Antigravity's language server: the agent terminal's or a sidecar's environment, else the newest live daemon
    file (~/.gemini/antigravity/daemon/hub_*.json, Antigravity 2.34+). Never scans processes."""
    addr, token = os.environ.get("ANTIGRAVITY_LS_ADDRESS", ""), os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if addr and token:
        return addr, token
    try:
        for ls in compat.daemon_servers()[:PROBES]:  # newest first
            if compat._alive(ls.pid) and compat.ls_healthy(ls.addr, PROBE):
                return ls.addr, ls.token
    except Exception:
        pass
    return None


def metadata(ep: Endpoint, conv_id: str, offset: int = 0, timeout: float = TIMEOUT,
             limit: int = 0) -> Optional[List[dict]]:
    """The generator metadata of conversation `conv_id`'s model calls from the `offset`-th on ([] past its last call);
    None on any failure, or when the answer is longer than `limit` bytes (0: no limit)."""
    base = ep[0] if re.match(r"^https?://", ep[0]) else f"http://{ep[0]}"
    body: dict = {"cascadeId": conv_id}
    if offset > 0:
        body["generatorMetadataOffset"] = offset
    req = urllib.request.Request(f"{base}/{SERVICE}/GetCascadeTrajectoryGeneratorMetadata",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                                          "x-codeium-csrf-token": ep[1]})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = r.read(limit + 1) if limit > 0 else r.read()
        if limit > 0 and len(raw) > limit:
            return None
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:  # unreachable, refused, timed out, too long or unreadable
        return None
    if not isinstance(data, dict):
        return None
    found = data.get("generatorMetadata")
    if found is None:
        return []
    return [g for g in found if isinstance(g, dict)] if isinstance(found, list) else None


def latest(conv_id: str, calls: int = 0, ep: Optional[Endpoint] = None, hook: bool = False) -> Optional[dict]:
    """The generator metadata of the conversation's latest model call, or None. `calls` — how many calls it has made —
    lets only the last ones be asked for (an undercount asks for more, an overcount finds nothing and asks again from
    further back). A `hook` never asks from the start of a long conversation and gives up on an answer over
    HOOK_BYTES: it runs before a model call."""
    if off() or not UUID_RE.fullmatch(conv_id or ""):
        return None
    ep = ep or endpoint()
    if not ep:
        return None
    offsets = (max(0, calls - 2), max(0, calls - 40)) + (() if hook else (0,))
    for offset in dict.fromkeys(offsets):
        found = metadata(ep, conv_id, offset, limit=HOOK_BYTES if hook else 0)
        if found is None:
            return None
        if found:
            return found[-1]
    return None


def refresh(conv_id: str, calls: int = 0, call_at: Optional[float] = None,
            now: Optional[float] = None) -> Optional[Reading]:
    """Read the conversation's latest call the way a hook may (hooks/speed.py) and record its window. `call_at` is when
    that call was made (the transcript's created_at). The reading (names, window, time), or None when it couldn't be
    read or the call is older than FRESH."""
    now = time.time() if now is None else now
    at = now if call_at is None else call_at
    if now - at > FRESH:
        return None
    g = latest(conv_id, calls, hook=True)
    found = readings([g], lambda _step: at) if g else []
    if not found:
        return None
    record(found, now)
    return found[0]


def _span(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 86400} d" if s >= 86400 else f"{s // 3600} h" if s >= 3600 else f"{s // 60} min"


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    models = load()
    if "--json" in args:
        print(json.dumps(models, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    if not models:
        print("No context window recorded yet: Antigravity's own numbers appear after the next model calls "
              "(the side panel, /usage and the context hints read them).")
        return 0
    now = time.time()
    for k, v in sorted(models.items()):
        print(f"{k:<34} {v['window'] / 1000:g}k   ({v.get('name') or k}, a call {_span(now - float(v['at']))} ago)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
