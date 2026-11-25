#!/usr/bin/env python3
r"""Antigravity Plus — Control Center backend (status.py).

Collects read-only state for the sidecar UI (`server.mjs` -> `GET /api/status`):
  * plugin version, installed/unavailable modules, cached update status
  * read-only access state (Bypass Mode, protectSecrets)
  * current plugin settings (`memory.settings()`) and module availability flags
  * active agents across `~/.gemini/antigravity-plus/coord/*.json` (last 10 min)
  * active + recent 5 Claude Code <-> Antigravity bridge tasks
  * live LSP daemons (`~/.gemini/antigravity-plus/lsp/*.json`, dead PIDs filtered, RAM MB)
  * last 15 `guard.log` entries with secrets, URL credentials, and home path masked

Usage:
  status.py [--json]

Also importable (`status.collect()`). Resolves `os.path.expanduser("~")` dynamically
at call time so tests changing `HOME` after import work reliably. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", ".."))
MEMORY_SCRIPTS = os.path.join(PLUGIN, "skills", "memory", "scripts")
if MEMORY_SCRIPTS not in sys.path:
    sys.path.insert(0, MEMORY_SCRIPTS)

memstore: Any = None
try:
    import memory as memstore  # type: ignore[import-not-found,no-redef]  # noqa: E402
except Exception:
    memstore = None

WINDOWS = os.name == "nt"
COORD_ACTIVE_SEC = 10 * 60
COORD_FILES_SEC = 30 * 60
GUARD_TAIL = 15
BRIDGE_RECENT = 5

DEFAULT_SETTINGS: Dict[str, Any] = {
    "autoMemory": True,
    "shareWithClaudeCode": True,
    "recentConversations": 3,
    "finishTasks": True,
    "checkEdits": True,
    "autoReview": True,
    "autoVerify": True,
    "protectSecrets": True,
    "autoSetup": True,
    "parallelHints": True,
    "speedHints": True,
    "factChecks": True,
    "objectiveAnswers": True,
    "autoCi": True,
    "gitChecks": True,
    "updateCheck": True,
    "autoUpdate": False,
    "lspChecks": True,
    "quotaHints": True,
    "uiNotices": True,
}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
VERSION_RE = re.compile(r'"version"\s*:\s*"([^"]+)"')

# Secret masking patterns (aligned with skills/insights/scripts/insights.py)
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(
        r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"
        r"|\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{12,}"
        r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
        r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
        r"|\bAKIA[0-9A-Z]{16}\b"
        r"|\bAIza[0-9A-Za-z_-]{20,}"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+"
        r"|\bglpat-[A-Za-z0-9_-]{16,}"
    ),
]
SECRET_ASSIGN = re.compile(
    r"(?i)(\b[\w.-]*(?:pass(?:word)?|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credentials?|auth)[\w.-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'&;|,)]+)"
)
SECRET_FLAG = re.compile(
    r"(?i)((?:--(?:password|token|api-key|secret)(?:=|\s+)|\b(?:Bearer|Basic|token)\s+))"
    r"(\"[^\"]{3,}\"|'[^']{3,}'|[^\s\"']{6,})"
)
URL_CREDS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{28,}")
# Passwords given to command-line tools as an argument: `mysql -pX`, `sshpass -p X`, `docker login -p X`,
# `redis-cli -a X`, `curl -u user:X`, `--client-secret X`, `aws configure set aws_secret_access_key X`.
CLI_SECRETS = [
    re.compile(r"(?i)(\b(?:mysql|mariadb|mysqldump|mysqladmin|mysqlimport)\b[^|;&\n]*?\s-p)(?=[^\s-])(\S+)"),
    re.compile(r"(?i)(\b(?:sshpass|(?:docker|podman|buildah|helm(?:\s+registry)?|oras)\s+login)\b[^|;&\n]*?\s-p\s+)(\S+)"),
    re.compile(r"(?i)(\bredis-cli\b[^|;&\n]*?\s(?:-a|--pass)\s+)(\S+)"),
    re.compile(r"(?i)(\s(?:-u|--user)\s+[\"']?[^\s:\"']+:)([^\s\"']+)"),
    re.compile(r"(?i)(--[\w-]*(?:secret|passw\w*|token|api[-_]?key|access[-_]?key|private[-_]?key)[\w-]*\s+)"
               r"(\"[^\"]*\"|'[^']*'|[^\s\"'-]\S*)"),
    re.compile(r"(?i)(\bset\s+[\w.:/-]*(?:secret|passw\w*|token|api[-_]?key|access[-_]?key)[\w.:/-]*\s+)(\S+)"),
]


def _num(value: Any, default: float = 0.0) -> float:
    """A number from a state file that may hold anything (another version's format, a half-written edit)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_json_dict(path: str) -> dict:
    d = _read_json(path)
    return d if isinstance(d, dict) else {}


def _tilde(path: str, home: str) -> str:
    """A path for the panel: `~` for the home folder and `/` as the separator on every system (like the plugin's
    other reports), so `C:\\Users\\me\\proj` shows as `~/proj`."""
    if not path:
        return ""
    p = path.replace("\\", "/") if WINDOWS else path
    if home and len(home) > 1:
        h = home.replace("\\", "/") if WINDOWS else home
        if p == h or p.startswith(h.rstrip("/") + "/"):
            return "~" + p[len(h.rstrip("/")):]
    return p


def _mask(text: str, home: str) -> str:
    """Mask secrets, tokens, URL credentials, and home paths in a text string."""
    t = text or ""
    for pat in SECRET_PATTERNS:
        t = pat.sub("***", t)
    t = URL_CREDS.sub(r"\1***@", t)
    for pat in CLI_SECRETS:
        t = pat.sub(lambda m: m.group(1) + "***", t)
    t = SECRET_FLAG.sub(lambda m: m.group(1) + "***", t)
    t = SECRET_ASSIGN.sub(lambda m: m.group(1) + "***", t)
    if home and len(home) > 1:
        t = t.replace(home, "~")
        if os.sep == "\\":
            t = t.replace(home.replace("\\", "/"), "~")

    def _long_one(m: "re.Match[str]") -> str:
        s = m.group(0)
        if UUID_RE.fullmatch(s) or sum(c.isdigit() for c in s) < 4 or sum(c.isalpha() for c in s) < 4:
            return s
        return "***"

    return LONG_TOKEN.sub(_long_one, t)


def _vtuple(v: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def _newer(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and _vtuple(a) > _vtuple(b)


def _plugin_version(modules_data: dict) -> str:
    p = os.path.join(PLUGIN, "plugin.json")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        cleaned = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        try:
            d = json.loads(cleaned)
            if isinstance(d, dict) and d.get("version"):
                return str(d["version"])
        except ValueError:
            pass
        m = VERSION_RE.search(raw)
        if m:
            return m.group(1)
    except OSError:
        pass
    return str(modules_data.get("version") or "")


def _load_settings(agp: str, home: str) -> Tuple[Dict[str, Any], dict, dict]:
    """Return (effective_settings, raw_settings_dict, modules_dict) resolved dynamically from `agp`."""
    defaults = dict(memstore.DEFAULTS) if memstore is not None and hasattr(memstore, "DEFAULTS") else dict(DEFAULT_SETTINGS)
    raw = _read_json_dict(os.path.join(agp, "settings.json"))
    modules = _read_json_dict(os.path.join(agp, "modules.json"))
    if memstore is not None and getattr(memstore, "HOME", None) == home:
        try:
            s = dict(memstore.settings())
            return s, raw, modules
        except Exception:
            pass
    s = dict(defaults)
    s.update({k: raw[k] for k, v in defaults.items() if type(raw.get(k)) is type(v)})
    off = modules.get("off")
    if isinstance(off, dict):
        s.update({k: v for k, v in off.items() if k in defaults and type(v) is type(defaults[k])})
    return s, raw, modules


def _available_flags(modules: dict) -> Dict[str, bool]:
    installed_list = modules.get("installed")
    if isinstance(installed_list, list) and installed_list:
        has_mods = True
        installed = {str(x) for x in installed_list}
    else:
        has_mods = False
        installed = set()

    def _has(mod_id: str, rel_file: str) -> bool:
        if has_mods:
            return mod_id in installed
        return os.path.isfile(os.path.join(PLUGIN, rel_file))

    lsp_script = os.path.isfile(os.path.join(PLUGIN, "skills", "lsp", "scripts", "lsp.py"))
    return {
        "quality": _has("quality", os.path.join("skills", "review", "SKILL.md")),
        "facts": _has("facts", os.path.join("rules", "FACTS.md")),
        "memory": _has("memory", os.path.join("skills", "memory", "SKILL.md")),
        "parallel": _has("parallel", os.path.join("skills", "parallel", "SKILL.md")),
        "speed": ("speed" in installed) if has_mods else True,
        "git": _has("git", os.path.join("skills", "git", "SKILL.md")),
        "lsp": (("lsp" in installed) if has_mods else True) and lsp_script,
        "update": os.path.isfile(os.path.join(PLUGIN, "skills", "update", "scripts", "update.py")),
        "claude": _has("claude", os.path.join("skills", "claude", "SKILL.md")),
    }


def _read_plugin(agp: str, home: str, modules: dict) -> dict:
    version = _plugin_version(modules)
    installed = [str(x) for x in (modules.get("installed") or []) if isinstance(x, str)]
    unavailable = [str(x) for x in (modules.get("unavailable") or []) if isinstance(x, str)]
    cache = _read_json_dict(os.path.join(agp, "update.json"))
    best = str(cache.get("best") or cache.get("latest") or "")
    rolled_back = str(cache.get("rolled_back") or "")
    avail = _newer(best, version) and best != rolled_back
    notes = [str(n) for n in (cache.get("notes") or []) if isinstance(n, str)]
    update = {
        "installed": version,
        "latest": str(cache.get("latest") or ""),
        "best": best,
        "available": avail,
        "checked": cache.get("checked"),
        "notes": notes[:10],
        "error": _mask(str(cache.get("error") or ""), home),
        "kind": str(cache.get("kind") or ""),
    }
    return {
        "version": version,
        "modules": {"installed": installed, "unavailable": unavailable},
        "update": update,
    }


def _read_access(raw_settings: dict, settings_dict: dict) -> dict:
    rec = raw_settings.get("bypassMode")
    bypass = rec if isinstance(rec, dict) else {}
    return {
        "bypass": {
            "on": bool(bypass.get("on") is True),
            "since": str(bypass.get("since") or ""),
            "conversation": str(bypass.get("conversation") or ""),
        },
        "protect_secrets": bool(settings_dict.get("protectSecrets", True)),
    }


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _uri_to_path(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri or "")
    if parsed.scheme != "file":
        return ""
    path = urllib.parse.unquote(parsed.path)
    if WINDOWS and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if path else ""


def _project_slug_map(home: str) -> Dict[str, str]:
    """Map workspace slug -> real folder path from ~/.gemini/config/projects/*.json."""
    out: Dict[str, str] = {}
    proj_dir = os.path.join(home, ".gemini", "config", "projects")
    for p in glob.glob(os.path.join(proj_dir, "*.json")):
        d = _read_json_dict(p)
        resources = ((d.get("projectResources") or {}).get("resources") or [])
        if not isinstance(resources, list):
            continue
        for res in resources:
            if not isinstance(res, dict):
                continue
            uri = str(res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri") or "")
            folder = _uri_to_path(uri)
            if folder:
                out.setdefault(_slug(folder), folder)
                out.setdefault(_slug(os.path.realpath(folder)), os.path.realpath(folder))
    return out


def _infer_workspace(stem: str, files_all: List[str], slug_map: Dict[str, str]) -> str:
    if stem in slug_map:
        return slug_map[stem]
    for p in files_all:
        cur = os.path.dirname(p)
        for _ in range(16):
            if not cur or cur == os.path.dirname(cur):
                break
            if _slug(cur) == stem:
                return cur
            cur = os.path.dirname(cur)
    if files_all:
        try:
            common = os.path.commonpath([os.path.dirname(p) for p in files_all])
            if common:
                return common
        except ValueError:
            pass
    return stem


def _read_agents(agp: str, home: str, now: float) -> List[dict]:
    coord_dir = os.path.join(agp, "coord")
    if not os.path.isdir(coord_dir):
        return []
    slug_map = _project_slug_map(home)
    out: List[dict] = []
    for path in sorted(glob.glob(os.path.join(coord_dir, "*.json"))):
        name = os.path.basename(path)
        if name.startswith("."):
            continue
        stem = name[:-5]
        data = _read_json_dict(path)
        agents = data.get("agents")
        if not isinstance(agents, dict):
            continue

        all_file_paths: List[str] = []
        for info in agents.values():
            if isinstance(info, dict) and isinstance(info.get("files"), dict):
                all_file_paths.extend(str(fp) for fp in info["files"].keys() if fp)
        ws = _infer_workspace(stem, all_file_paths, slug_map)
        ws_short = _tilde(ws, home)
        ws_name = os.path.basename(ws.rstrip("\\/")) or ws_short or stem

        for cid, info in agents.items():
            if not isinstance(info, dict):
                continue
            try:
                last = float(info.get("last") or 0)
            except (TypeError, ValueError):
                continue
            if now - last > COORD_ACTIVE_SEC:
                continue
            raw_files = info.get("files")
            rel_files: List[str] = []
            if isinstance(raw_files, dict):
                ordered = sorted(
                    ((str(fp), _num(ts)) for fp, ts in raw_files.items() if fp),
                    key=lambda pair: -pair[1],
                )
                for fp, ts in ordered:
                    if ts and now - ts > COORD_FILES_SEC:
                        continue
                    rel = os.path.relpath(fp, ws).replace(os.sep, "/") if ws and fp.startswith(ws + os.sep) else _tilde(fp, home)
                    rel_files.append(rel)
            elif isinstance(raw_files, list):
                for fp in raw_files:
                    if isinstance(fp, str) and fp:
                        rel = os.path.relpath(fp, ws).replace(os.sep, "/") if ws and fp.startswith(ws + os.sep) else _tilde(fp, home)
                        rel_files.append(rel)
            out.append({
                "id": str(cid),
                "title": str(info.get("title") or ""),
                "parent": str(info.get("parent") or ""),
                "workspace": ws_short,
                "workspace_name": ws_name,
                "ago_min": max(0, int((now - last) // 60)),
                "files": rel_files[:12],
                "last": last,
            })
    out.sort(key=lambda a: (-a["last"], a["id"]))
    return out


def _open_questions(task: dict) -> bool:
    chat = task.get("chat") or []
    if not isinstance(chat, list):
        return False
    mine = [m for m in chat if isinstance(m, dict) and m.get("from") == "claude"]
    asked = [m for m in chat if isinstance(m, dict) and m.get("from") == "antigravity" and m.get("kind") == "question"]
    if not asked:
        return False
    if any(not isinstance(m.get("step"), int) for m in mine):
        last = max((i for i, m in enumerate(chat) if isinstance(m, dict) and m.get("from") == "claude"), default=-1)
        return any(m in asked for m in chat[last + 1:])
    last_step = max((int(m["step"]) for m in mine), default=-1)
    return any(_num(m.get("step")) >= last_step for m in asked)


def _read_bridge(agp: str, home: str) -> List[dict]:
    items: List[dict] = []
    seen_ids = set()

    delegate_dir = os.path.join(agp, "delegate")
    for p in glob.glob(os.path.join(delegate_dir, "*.json")):
        base = os.path.basename(p)
        if base.startswith(".") or base in ("claude-limits.json", "speeds.json", "settings.json"):
            continue
        d = _read_json_dict(p)
        tid = d.get("id")
        if not isinstance(tid, str) or not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        state = str(d.get("state") or "?")
        if state == "started":
            continue  # a queued task that has started: its own record («the task it became») is listed instead
        waiting = bool(d.get("waiting")) or state == "question" or _open_questions(d)
        status = "question" if waiting and state in ("running", "question", "done") else state
        try:
            started = float(d.get("started") or d.get("queued") or 0)
        except (TypeError, ValueError):
            started = 0.0
        items.append({
            "id": tid,
            "direction": "Claude → AGY",
            "title": str(d.get("title") or tid[:8]),
            "model": str(d.get("label") or d.get("model") or ""),
            "status": status,
            "waiting": waiting,
            "started": started,
            "workspace": _tilde(str(d.get("cwd") or d.get("root") or ""), home),
        })

    claude_dir = os.path.join(agp, "claude-tasks")
    claude_paths = glob.glob(os.path.join(claude_dir, "*.json")) + glob.glob(os.path.join(claude_dir, "*", "task.json"))
    for p in claude_paths:
        if os.path.basename(p).startswith("."):
            continue
        d = _read_json_dict(p)
        tid = d.get("id")
        if not isinstance(tid, str) or not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        state = str(d.get("state") or "?")
        waiting = bool(d.get("waiting")) or state == "question"
        try:
            started = float(d.get("started") or 0)
        except (TypeError, ValueError):
            started = 0.0
        items.append({
            "id": tid,
            "direction": "AGY → Claude",
            "title": str(d.get("title") or tid[:8]),
            "model": str(d.get("claude_model") or d.get("model") or ""),
            "status": state,
            "waiting": waiting,
            "started": started,
            "workspace": _tilde(str(d.get("workspace") or d.get("root") or ""), home),
        })

    # delegate.py's states: running, question, approval (the user has to approve a step in Antigravity), timeout
    # (`wait` gave up, the agent goes on), background (its turn ended, its background work hasn't), queued /
    # starting (it waits for a free slot: run --max-parallel) — all still on.
    active = [t for t in items if t["status"] in ("running", "question", "approval", "timeout", "background", "queued",
                                                  "starting") or t["waiting"]]
    recent = [t for t in items if t not in active]
    active.sort(key=lambda t: (-t["started"], t["id"]))
    recent.sort(key=lambda t: (-t["started"], t["id"]))
    return active + recent[:BRIDGE_RECENT]


def _pid_alive(pid: Any) -> bool:
    try:
        num = int(pid)
    except (TypeError, ValueError):
        return False
    if num <= 0:
        return False
    if WINDOWS:
        try:
            import ctypes
            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel.OpenProcess(0x1000, False, num)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            return bool(ok) and code.value == 259
        except Exception:
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {num}", "/NH"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
                )
                return r.returncode == 0 and str(num) in r.stdout
            except (OSError, subprocess.SubprocessError):
                return False
    try:
        os.kill(num, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _rss_mb(pids: List[int]) -> Optional[float]:
    unique = []
    for p in pids:
        if p and p not in unique and _pid_alive(p):
            unique.append(p)
    if not unique:
        return None
    total_kb = 0
    if WINDOWS:
        for p in unique:
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {p}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
                )
                for line in r.stdout.splitlines():
                    # The last CSV field is the memory in KB: "12,345 K", "12.345 K", "12 345 КБ" (OEM code page)
                    m = re.search(r'"([^"]*)"\s*$', line)
                    digits = re.sub(r"[^\d]", "", m.group(1)) if m else ""
                    if digits:
                        total_kb += int(digits)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return round(total_kb / 1024.0, 1) if total_kb > 0 else None

    for p in unique:
        try:
            r = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(p)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
            )
            if r.returncode == 0:
                for tok in r.stdout.split():
                    if tok.isdigit():
                        total_kb += int(tok)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return round(total_kb / 1024.0, 1) if total_kb > 0 else None


def _is_lsp_daemon(pid: Any) -> bool:
    """The pid in a state file is still lsp.py's daemon, not another process that got the number after a reboot
    (lsp.py starts it as `… lsp.py daemon --key …`). Where `ps` can't tell (Windows), a live pid is enough."""
    if WINDOWS:
        return True
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    if r.returncode != 0:
        return not r.stdout.strip() and r.returncode != 1  # 1 = no such process
    return "lsp.py" in r.stdout and " daemon" in r.stdout


def _read_lsp(agp: str, home: str, now: float) -> List[dict]:
    lsp_dir = os.path.join(agp, "lsp")
    if not os.path.isdir(lsp_dir):
        return []
    out: List[dict] = []
    for p in sorted(glob.glob(os.path.join(lsp_dir, "*.json"))):
        name = os.path.basename(p)
        if name.startswith("."):
            continue
        st = _read_json_dict(p)
        pid = st.get("pid")
        server = st.get("server")
        if not pid or not server:
            continue
        if not _pid_alive(pid) or not _is_lsp_daemon(pid):
            continue
        spid = st.get("server_pid")
        pids = [int(pid)]
        if isinstance(spid, int) and spid > 0 and spid != int(pid):
            pids.append(spid)
        try:
            used = float(st.get("used") or st.get("started") or now)
        except (TypeError, ValueError):
            used = now
        try:
            started = float(st.get("started") or now)
        except (TypeError, ValueError):
            started = now
        out.append({
            "key": name[:-5],
            "server": str(server),
            "lang": str(st.get("lang") or ""),
            "root": _tilde(str(st.get("root") or ""), home),
            "pid": int(pid),
            "server_pid": int(spid) if isinstance(spid, int) and spid > 0 else None,
            "ram_mb": _rss_mb(pids),
            "idle_sec": max(0, int(now - used)),
            "up_sec": max(0, int(now - started)),
        })
    out.sort(key=lambda d: (d["server"], d["root"]))
    return out


def _read_guard(agp: str, home: str) -> List[dict]:
    log_path = os.path.join(agp, "guard.log")
    if not os.path.isfile(log_path):
        return []
    entries: List[dict] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        subj = _mask(str(d.get("subject") or ""), home)
        if len(subj) > 200:
            subj = subj[:199] + "…"
        entries.append({
            "ts": str(d.get("ts") or ""),
            "tool": str(d.get("tool") or ""),
            "rule": str(d.get("rule") or ""),
            "subject": subj,
            "conversation": str(d.get("conversation") or ""),
        })
    return entries[-GUARD_TAIL:][::-1]


def collect(home: Optional[str] = None) -> dict:
    """Collect the Control Center status snapshot dynamically from current `HOME`."""
    resolved_home = os.path.expanduser("~") if home is None else home
    agp = os.path.join(resolved_home, ".gemini", "antigravity-plus")
    now = time.time()
    settings_dict, raw_settings, modules = _load_settings(agp, resolved_home)

    def part(fn: Any, *args: Any, empty: Any = None) -> Any:
        """One malformed state file must not take the whole panel down: that section comes back empty."""
        try:
            return fn(*args)
        except Exception as e:
            print(f"status: {getattr(fn, '__name__', fn)}: {e}", file=sys.stderr)
            return [] if empty is None else empty

    return {
        "ts": now,
        "plugin": part(_read_plugin, agp, resolved_home, modules, empty={}),
        "access": part(_read_access, raw_settings, settings_dict, empty={}),
        "settings": settings_dict,
        "available": part(_available_flags, modules, empty={}),
        "agents": part(_read_agents, agp, resolved_home, now),
        "bridge": part(_read_bridge, agp, resolved_home),
        "lsp": part(_read_lsp, agp, resolved_home, now),
        "guard": part(_read_guard, agp, resolved_home),
    }


def _describe(data: dict) -> str:
    p = data["plugin"]
    acc = data["access"]
    lines = [
        f"Antigravity Plus {p['version'] or '?'} · modules: {len(p['modules']['installed'])}",
        f"Bypass Mode: {'ON' if acc['bypass']['on'] else 'OFF'} · protectSecrets: {'ON' if acc['protect_secrets'] else 'OFF'}",
        f"Active agents: {len(data['agents'])} · Bridge tasks: {len(data['bridge'])} · LSP daemons: {len(data['lsp'])} · Guard entries: {len(data['guard'])}",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description="Antigravity Plus Control Center status")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args(argv)
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else _describe(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
