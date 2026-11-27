#!/usr/bin/env python3
"""Antigravity Plus — what the Antigravity client itself says about its models.

The one place the plugin learns from the running client (its language server) which models there are, their
families and efforts, Antigravity's own tags («New», «Leaving Soon»), the agent's default model, each model's
context window, the Orchestrator's executor and the client's version — instead of keeping them by hand.

  * refresh() asks the language server (found by compat.language_server()) GetAvailableModels — a read-only RPC —,
    keeps only facts about models (no quota, plan, name or e-mail) and writes them to CACHE atomically, under a
    lock. A failure, a timeout, an unreadable or empty answer keeps the old file.
  * snapshot() reads CACHE, else BUNDLED (the snapshot shipped with the plugin, made by `client.py export`); it never
    talks to the network and never raises. Hooks only ever call snapshot() and the readers built on it.
  * background() — SessionStart only — starts a detached `client.py refresh` when the snapshot is stale (older than
    TTL, taken from another client version, or the bundled one while a client runs) and returns at once.

Env: AGP_CLIENT_CACHE moves the cache file, AGP_CLIENT_REFRESH=off stops background refreshes (tests).
`agy models` is the fallback source on a machine with the Antigravity CLI only (refresh() alone runs it).
CLI: client.py show | refresh [--force] | export | agy. Works as a copy next to delegate.py too (imports compat from its
own folder, finds client-snapshot.json next to itself). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/.gemini/antigravity-plus/client.json")
BUNDLED = os.path.join(HERE, "client-snapshot.json")
TTL = 6 * 3600
VERSION = 1                # the snapshot format this module reads and writes
AGY_TIMEOUT = 15.0         # seconds for `agy models` (it takes 3–5 s)
RETRY = 600                # seconds between background refresh attempts that found nothing to do it with
SERVICE = "exa.language_server_pb.LanguageServerService"
CHECKPOINTER = "CASCADE_USE_EXPERIMENT_CHECKPOINTER"
PLAN_EXECUTE = "plan-execute-config"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")  # effort suffixes of ids outside groupedModels
VENDORS = ("gemini", "claude", "gpt")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the language server is local: no proxy

_memo: Dict[str, Tuple[Tuple[float, int], dict]] = {}


def cache_path() -> str:
    return os.environ.get("AGP_CLIENT_CACHE") or CACHE


def _norm(text: str) -> str:
    """«Gemini 4 Hydrogen (Low)», gemini_4_hydrogen-low → gemini-4-hydrogen-low (dots stay: gemini-3.8-flash)."""
    return re.sub(r"[^a-z0-9.]+", "-", (text or "").strip().lower()).strip("-")


# ─────────────────────────────── reading ───────────────────────────────

def _valid(data: object) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict) or not data["models"]:
        return False
    v = data.get("v")
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= VERSION


def _read(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    hit = _memo.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        data = {}
    if not _valid(data):
        data = {}
    _memo[path] = (stamp, data)
    return data


def snapshot() -> dict:
    """The cached snapshot, else the bundled one; {} only when neither is readable. Never raises, never networks."""
    try:
        snap = _read(cache_path())
        if snap:
            return snap
        snap = _read(BUNDLED)
        if snap:
            return dict(snap, source="bundled")
    except Exception:
        pass
    return {}


def running_version() -> str:
    """The version of the running client's language server, from its daemon (hub_*.json) file only; "" if none."""
    try:
        for ls in compat.daemon_servers():
            if ls.version and compat._alive(ls.pid):
                return ls.version
    except Exception:
        pass
    return ""


def stale(snap: Optional[dict] = None) -> bool:
    """True when the snapshot should be taken again: none, older than TTL, from another client version than the one
    running, or the bundled one while a client runs."""
    snap = snapshot() if snap is None else snap
    if not snap:
        return True
    running = running_version()
    if snap.get("source") == "bundled" and running:
        return True
    at = snap.get("at")
    if not isinstance(at, (int, float)) or time.time() - float(at) > TTL:
        return True
    return bool(running and snap.get("client") and snap.get("client") != running)


def models() -> Dict[str, dict]:
    m = snapshot().get("models")
    return m if isinstance(m, dict) else {}


def _families() -> Dict[str, dict]:
    f = snapshot().get("families")
    return f if isinstance(f, dict) else {}


def _order(ids: List[str]) -> List[str]:
    sort = snapshot().get("sort") or []
    rank = {mid: i for i, mid in enumerate(sort) if isinstance(mid, str)}
    return sorted(ids, key=lambda mid: (rank.get(mid, len(rank)), mid))


def model(key: str) -> Optional[dict]:
    """A model by its id, its enum (MODEL_PLACEHOLDER_M532: how a payload's modelName or a step's model may name it),
    its label («Gemini 4 Hydrogen (Low)») or a free key (any case, spaces or hyphens, an effort suffix: «gemini 4
    hydrogen low»); a bare family («gemini-4-neon», «orchestrator») gives its first model in the client's order
    (Orchestrator: Carbon). None when unknown.
    An id always beats a label, in this order: the exact id, the enum, an id equal to the key in free form, and only
    then a label. Labels repeat (the client calls gemini-2.5-flash, -2.5-flash-lite, -2.5-flash-thinking and
    gemini-3.5-flash-lite all «Gemini 3.5 Flash Lite»; gemini-4-hydrogen-high and -tiered-high both «Gemini 4
    Hydrogen (High)»): such a label first meets the id that spells it (gemini-3.5-flash-lite, gemini-4-hydrogen-high),
    else the first in the client's order — models sharing a label share their window there, and the family comes
    from each entry's own id, never from the label."""
    if not isinstance(key, str) or not key.strip():
        return None
    ms = models()
    if key in ms:
        return ms[key]
    up = key.strip().upper()
    for mid in _order(list(ms)):
        if str(ms[mid].get("enum") or "").upper() == up:
            return ms[mid]
    want = _norm(key)
    if not want:
        return None
    for mid in _order(list(ms)):
        if _norm(mid) == want:
            return ms[mid]
    for mid in _order(list(ms)):
        if _norm(str(ms[mid].get("label") or "")) == want:
            return ms[mid]
    fams = _families()
    fam = next((f for f in fams if _norm(f) == want or _norm(str(fams[f].get("label") or "")) == want), None)
    if fam:
        efforts = fams[fam].get("efforts") or {}
        if fam == "orchestrator" and efforts.get("carbon") in ms:
            return ms[efforts["carbon"]]
        ids = _order([mid for mid in efforts.values() if mid in ms])
        if ids:
            return ms[ids[0]]
    return None


def default_model() -> str:
    d = snapshot().get("default")
    return d if isinstance(d, str) else ""


def window(key: str) -> Optional[int]:
    """The model's context window (Antigravity compacts past it) from the snapshot; None when unknown."""
    m = model(key)
    w = m.get("window") if m else None
    return w if isinstance(w, int) and not isinstance(w, bool) and w > 0 else None


def executor() -> dict:
    e = snapshot().get("executor")
    return dict(e) if isinstance(e, dict) and e.get("id") else {}


def aliases() -> dict:
    a = snapshot().get("aliases")
    return dict(a) if isinstance(a, dict) else {}


def version() -> str:
    v = snapshot().get("client")
    return v if isinstance(v, str) else ""


def _words(family: str) -> Tuple[str, frozenset, Tuple[float, ...]]:
    """gemini-4-hydrogen → ("gemini", {hydrogen}, (4,)); claude-sonnet-5-5 → ("claude", {sonnet}, (5, 5))."""
    parts = [p for p in re.split(r"-+", _norm(family)) if p]
    gen: List[float] = []
    names = []
    for p in parts:
        if re.fullmatch(r"\d+(?:\.\d+)*", p):
            gen.extend(float(x) for x in p.split("."))
        else:
            names.append(p)
    vendor = names[0] if names and names[0] in VENDORS else ""
    return vendor, frozenset(n for n in names if n != vendor), tuple(gen)


def resolve(family: str, effort: str = "") -> str:
    """The live model id for a rule: ("gemini-4-hydrogen", "low") → gemini-4-hydrogen-low, ("orchestrator", "carbon")
    → gemini-pro-orchestrator. A family the client no longer has → its successor: the family with the same name word
    and vendor (no extra words: sonar, tiered, worker are families of their own) of the newest generation. "" when
    that family has no such effort; with no snapshot at all, the literal <family>-<effort>."""
    family, effort = (family or "").strip().lower(), (effort or "").strip().lower()
    if not family:
        return ""
    fams, ms = _families(), models()
    if not fams or not ms:
        if family == "orchestrator":
            return "gemini-pro-orchestrator" + ("" if effort in ("", "carbon") else "-" + effort)
        return f"{family}-{effort}" if effort else family
    fam = family if family in fams else ""
    if not fam:
        vendor, names, _ = _words(family)
        if names:
            best: Optional[Tuple[Tuple[float, ...], str]] = None
            for f in fams:
                v2, n2, g2 = _words(f)
                if v2 == vendor and n2 == names and (best is None or g2 > best[0]):
                    best = (g2, f)
            fam = best[1] if best else ""
    if not fam:
        return ""
    mid = (fams[fam].get("efforts") or {}).get(effort)
    return mid if isinstance(mid, str) and mid in ms else ""


# ─────────────────────────────── the client ───────────────────────────────

def _int(value: object) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _experiment(exps: dict, name: str) -> dict:
    raw = exps.get(name)
    raw = raw.get("stringValue") if isinstance(raw, dict) else raw
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _window(entry: dict) -> Optional[int]:
    exps = entry.get("modelExperiments")
    exps = exps.get("experiments", exps) if isinstance(exps, dict) else {}
    if not isinstance(exps, dict):
        return None
    win = _int(_experiment(exps, CHECKPOINTER).get("max_token_limit"))
    for name in sorted(exps):  # an override (CASCADE_USE_EXPERIMENT_CHECKPOINTER/claude-1m_840k) wins
        if name.startswith(CHECKPOINTER + "/"):
            win = _int(_experiment(exps, name).get("max_token_limit")) or win
    return win


def _provider(entry: dict) -> str:
    p = str(entry.get("modelProvider") or entry.get("apiProvider") or "")
    p = re.sub(r"^(?:MODEL|API)_PROVIDER_", "", p).lower()
    return p.split("_")[0] if p else ""


def normalize(answer: object, client: str = "", source: str = "desktop") -> dict:
    """GetAvailableModels' answer → a v1 snapshot of facts about models; {} when it lists no model. Keys missing from
    another client version leave their fields empty."""
    r = answer.get("response", answer) if isinstance(answer, dict) else {}
    r = r if isinstance(r, dict) else {}
    raw = r.get("models") if isinstance(r.get("models"), dict) else {}
    grouped = r.get("groupedModels") if isinstance(r.get("groupedModels"), dict) else {}
    out_models: Dict[str, dict] = {}
    families: Dict[str, dict] = {}
    for fid, g in grouped.items():
        if not isinstance(fid, str) or not isinstance(g, dict):
            continue
        families[fid] = {"label": str(g.get("displayName") or ""), "provider": "", "efforts": {}}
    suffixes: Dict[str, set] = {}
    for fid, g in grouped.items():
        if isinstance(fid, str) and isinstance(g, dict):
            levels = g.get("supportedThinkingLevels") if isinstance(g.get("supportedThinkingLevels"), list) else []
            suffixes[fid] = {str(x.get("thinkingSuffix")) for x in levels if isinstance(x, dict) and x.get("thinkingSuffix")}
    by_enum: Dict[str, str] = {}
    plan_exec = ""
    for mid, e in raw.items():
        if not isinstance(mid, str) or not isinstance(e, dict) or mid.startswith("tab_"):
            continue
        label = str(e.get("displayName") or "")
        exps = e.get("modelExperiments")
        exps = exps.get("experiments", exps) if isinstance(exps, dict) else {}
        exps = exps if isinstance(exps, dict) else {}
        fam, effort = "", ""
        orch = PLAN_EXECUTE in exps or bool(re.match(r"^gemini-pro-orchestrator(?:-|$)", mid))
        if orch:
            fam = "orchestrator"
            m = re.search(r"\(\s*([^)]+?)\s*\)", label)
            effort = _norm(m.group(1)) if m else (mid.rsplit("-", 1)[1] if mid.count("-") > 2 else "carbon")
            plan_exec = plan_exec or str(_experiment(exps, PLAN_EXECUTE).get("executionModel") or "")
        else:
            for f in sorted(suffixes, key=len, reverse=True):
                if mid.startswith(f + "-") and mid[len(f) + 1:] in suffixes[f]:
                    fam, effort = f, mid[len(f) + 1:]
                    break
            if not fam:
                m = re.match(r"^(.+?)-(%s)$" % "|".join(EFFORTS), mid)
                fam, effort = (m.group(1), m.group(2)) if m else (mid, "")
        provider = _provider(e)
        f = families.setdefault(fam, {"label": "", "provider": "", "efforts": {}})
        if not f["label"]:
            f["label"] = "Orchestrator" if orch else (re.sub(r"\s*\([^)]*\)\s*$", "", label).strip() if effort else label)
        f["provider"] = f["provider"] or provider
        f["efforts"].setdefault(effort, mid)
        if isinstance(e.get("model"), str):
            by_enum[e["model"]] = mid
        out_models[mid] = {"id": mid, "label": label, "enum": str(e.get("model") or ""), "family": fam, "family_label": "", "effort": effort,
                           "provider": provider, "window": _window(e), "tag": str(e.get("tagTitle") or ""),
                           "note": str(e.get("tagDescription") or ""), "recommended": bool(e.get("recommended"))}
    if not out_models:
        return {}
    for m in out_models.values():
        m["family_label"] = families.get(m["family"], {}).get("label", "")
    sort: List[str] = []
    for group in r.get("agentModelSorts") or []:
        for sub in (group.get("groups") or []) if isinstance(group, dict) else []:
            for mid in (sub.get("modelIds") or []) if isinstance(sub, dict) else []:
                if isinstance(mid, str) and mid not in sort:
                    sort.append(mid)
    tiered = r.get("tieredModelIds") if isinstance(r.get("tieredModelIds"), dict) else {}
    aliases_ = {}
    for name, keyname in (("flash_lite", "flashLite"), ("flash", "flash"), ("pro", "pro")):
        ids = tiered.get(keyname)
        if isinstance(ids, list) and ids and isinstance(ids[0], str):
            aliases_[name] = ids[0]
    ex_id = by_enum.get(plan_exec, "")
    default = r.get("defaultAgentModelId")
    return {"v": VERSION, "at": int(time.time()), "client": client, "source": source,
            "default": default if isinstance(default, str) else "", "aliases": aliases_,
            "executor": {"id": ex_id, "label": out_models[ex_id]["label"]} if ex_id in out_models else {},
            "sort": sort, "models": out_models, "families": families}


def _ask(timeout: float) -> dict:
    """A fresh snapshot from the running client; {} when there is none or it answers nothing useful."""
    ls = compat.language_server()
    if not ls:
        return {}
    req = urllib.request.Request(f"http://{ls.addr}/{SERVICE}/GetAvailableModels", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "x-codeium-csrf-token": ls.token})
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except Exception:
        return {}
    runtime = getattr(compat, "runtime", None)
    try:
        source = "cli" if runtime and runtime() == "cli" else "desktop"
    except Exception:
        source = "desktop"
    return normalize(answer, ls.version or running_version(), source)


def _agy_exe() -> str:
    find = getattr(compat, "agy", None)
    try:
        exe = find() if find else ""
    except Exception:
        exe = ""
    return exe or shutil.which("agy") or ""


def parse_agy_models(text: str) -> List[Tuple[str, str]]:
    """`agy models` → [(id, label)]: one «<modelId>\t<Display name>» per line. [] when any non-empty line isn't one
    (an error message, another format): an unreadable answer teaches nothing."""
    out: List[Tuple[str, str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*([a-z0-9][a-z0-9._-]*)\t+(\S.*?)\s*", line)
        if not m:
            return []
        out.append((m.group(1), m.group(2)))
    return out


def merge_agy(listed: List[Tuple[str, str]], base: dict, version: str = "") -> dict:
    """A snapshot made from `agy models` over `base` (the current snapshot): ids still listed keep what base knew
    (window, family, tag…), new ones come with what their id and label tell, ids no longer listed go."""
    if not listed:
        return {}
    old = base.get("models") if isinstance(base.get("models"), dict) else {}
    fams_old = base.get("families") if isinstance(base.get("families"), dict) else {}
    ms: Dict[str, dict] = {}
    for mid, label in listed:
        if mid in old and isinstance(old[mid], dict):
            ms[mid] = dict(old[mid], label=label or old[mid].get("label", ""))
            continue
        orch = bool(re.match(r"^gemini-pro-orchestrator(?:-|$)", mid))
        fam, effort = "", ""
        if orch:
            m = re.search(r"\(\s*([^)]+?)\s*\)", label)
            fam, effort = "orchestrator", (_norm(m.group(1)) if m else "carbon")
        else:
            m = re.match(r"^(.+?)-(%s)$" % "|".join(EFFORTS), mid)
            fam, effort = (m.group(1), m.group(2)) if m else (mid, "")
        provider = next((p for p, w in (("google", "gemini"), ("anthropic", "claude"), ("openai", "gpt"))
                         if mid.startswith(w)), "")
        ms[mid] = {"id": mid, "label": label, "enum": "", "family": fam, "family_label": "", "effort": effort,
                   "provider": provider, "window": None, "tag": "", "note": "", "recommended": True}
    families: Dict[str, dict] = {}
    for mid, m in ms.items():
        fam = str(m.get("family") or mid)
        f = families.setdefault(fam, {"label": str((fams_old.get(fam) or {}).get("label") or ""),
                                      "provider": str(m.get("provider") or ""), "efforts": {}})
        if not f["label"]:
            f["label"] = "Orchestrator" if fam == "orchestrator" else re.sub(r"\s*\([^)]*\)\s*$", "", m["label"])
        f["efforts"].setdefault(str(m.get("effort") or ""), mid)
        m["family_label"] = m.get("family_label") or f["label"]
    ex = base.get("executor") if isinstance(base.get("executor"), dict) else {}
    al = base.get("aliases") if isinstance(base.get("aliases"), dict) else {}
    default = base.get("default") if base.get("default") in ms else ""
    return {"v": VERSION, "at": int(time.time()), "client": f"cli-{version}" if version else "cli", "source": "cli",
            "default": default, "aliases": {k: v for k, v in al.items() if v in ms},
            "executor": dict(ex) if ex.get("id") in ms or ex.get("id") in old else {},
            "sort": [mid for mid, _ in listed], "models": ms, "families": families}


def _ask_agy(timeout: float = AGY_TIMEOUT) -> dict:
    """A snapshot from `agy models` (the Antigravity CLI, which runs its own language server per run and leaves no
    hub file) merged into the current one; {} without the CLI or on an unreadable answer."""
    exe = _agy_exe()
    if not exe:
        return {}
    try:
        r = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=min(timeout, AGY_TIMEOUT), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if r.returncode != 0:
        return {}
    version_of = getattr(compat, "agy_version", None)
    try:
        version = version_of() if version_of else ""
    except Exception:
        version = ""
    return merge_agy(parse_agy_models(r.stdout), snapshot(), version)


def _write(snap: dict) -> bool:
    path = cache_path()
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a", encoding="utf-8") as lk:
            compat.lock(lk)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=True)
                compat.replace(tmp, path)
            finally:
                compat.unlock(lk)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def refresh(timeout: float = 6.0, force: bool = False) -> dict:
    """Ask the client when the snapshot is stale (or `force`), keep the answer, return snapshot(). A failure keeps the
    old cache."""
    try:
        if force or stale():
            snap = _ask(timeout) or _ask_agy()
            if snap:
                _write(snap)
    except Exception:
        pass
    return snapshot()


def background() -> None:
    """SessionStart: a detached `client.py refresh` when the snapshot is stale; returns at once, never raises."""
    try:
        if os.environ.get("AGP_CLIENT_REFRESH", "").strip().lower() == "off" or not stale():
            return
        mark = cache_path() + ".attempt"
        try:
            if time.time() - os.path.getmtime(mark) < RETRY:
                return
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(mark) or ".", exist_ok=True)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        kw: Dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                 "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": HERE}
        if compat.WINDOWS:
            kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "refresh"], **kw)  # noqa: S603
    except Exception:
        pass


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        snap = snapshot()
        print(json.dumps(dict(snap, stale=stale(snap)) if snap else {}, ensure_ascii=False, indent=1))
        return 0
    if cmd == "refresh":
        snap = refresh(force="--force" in argv)
        return 0 if snap and snap.get("source") != "bundled" else 1
    if cmd == "agy":  # what `agy models` would make of the current snapshot, without saving it
        snap = _ask_agy()
        print(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True))
        return 0 if snap else 1
    if cmd == "export":
        snap = _ask(15.0)
        if not snap:
            print("client.py: Antigravity's language server isn't reachable or lists no model.", file=sys.stderr)
            return 1
        print(json.dumps(dict(snap, source="bundled"), ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    print("usage: client.py show | refresh [--force] | export | agy", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
