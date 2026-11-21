#!/usr/bin/env python3
r"""Antigravity Plus — /schedule-task: quota-aware scheduled Antigravity automations.

  schedule_task.py list [--json]
  schedule_task.py gate [--group gemini|claude] [--min-weekly 15] [--min-5h 20] [--json]
  schedule_task.py create --slug SLUG --schedule "[CRON_TZ=<zone>] CRON" --prompt "..." [--title "..."]
                          [--group gemini|claude] [--min-weekly 15] [--min-5h 20]
                          [--workspace DIR] [--grant "command(npm test)" ...] [--overwrite] [--json]
  schedule_task.py remove --slug SLUG

A scheduled automation is an Antigravity sidecar in `~/.gemini/config/sidecars/<id>/sidecar.json`:
`{"builtin": "schedule", "args": ["CRON_TZ=<zone> <cron>", "agentapi", "new-conversation", "--", "<prompt>"],
"restart_policy": "always", "display_name", "description", "agent_permissions": {"access_grants", "workspace_uris"}}`.
Each run starts a fresh conversation; commands that aren't granted are auto-denied there. The tasks this script
creates are named `agp-<slug>`: `remove` and `--overwrite` touch only those, `list` shows the user's own
automations read-only. A new automation runs only after the user enables it in the Automations dashboard.

`gate` queries `RetrieveUserQuotaSummary` from Antigravity's Language Server (falling back to
`~/.gemini/antigravity-plus/quota.json` if fresh within 15 min) and exits `0` to proceed or `3` to skip when the
remaining weekly/5h quota is below threshold.

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
HOOKS = os.path.join(PLUGIN, "hooks")
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import compat  # noqa: E402

SERVICE = "exa.language_server_pb.LanguageServerService"
CACHE_MAX_AGE = 15 * 60
RPC_TIMEOUT = 2.0
PROBE_TIMEOUT = 0.5
ID_PREFIX = "agp-"  # marks the automations this script created
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,44}\Z")
CRON_CHARS_RE = re.compile(r"^[0-9*/,-]+\Z")
TZ_PREFIX_RE = re.compile(r"^(CRON_TZ|TZ)=(\S*)\s+(.*)\Z", re.S)
ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*\Z")
GRANT_RE = re.compile(r"^(command|read_file|write_file|mcp|read_url|execute_url)\((.+)\)\Z", re.S)
SAFE_ARG_RE = re.compile(r"^[\w@%+=:,./\\-]+\Z")
DASHBOARD = "[Open Automations Dashboard](sidecar://dashboard)"
GROUPS = ("gemini", "claude")
GROUP_HINTS = {
    "gemini": ("gemini", "orchestrator", "flash", "hydrogen", "carbon", "argon", "neon"),
    "claude": ("claude", "gpt", "opus", "sonnet"),
}
CRON_RANGES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_SKIP = 3

# Local language server: bypass any system proxy settings (matches hooks/quota.py and mcp/server.py).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _sidecars_dir() -> str:
    """Antigravity's own sidecars: `<configDir>/sidecars/<id>/sidecar.json`, watched live by the language server."""
    return os.path.join(os.path.expanduser("~"), ".gemini", "config", "sidecars")


def _quota_cache_path() -> str:
    return os.environ.get("AGP_QUOTA_CACHE") or os.path.join(
        os.path.expanduser("~"), ".gemini", "antigravity-plus", "quota.json"
    )


def _fmt_num(val: float) -> str:
    if abs(val - round(val)) < 1e-6:
        return str(int(round(val)))
    return f"{val:g}"


def _fmt_pct(frac: float) -> str:
    pct_val = round(frac * 100.0, 6)
    if abs(pct_val - round(pct_val)) < 1e-4:
        return f"{int(round(pct_val))}%"
    return f"{pct_val:.1f}%"


def _parse_num(raw: str) -> Any:
    val = float(raw)
    if abs(val - round(val)) < 1e-6:
        return int(round(val))
    return val


def sidecar_id(slug: str) -> Tuple[Optional[str], Optional[str]]:
    """`nightly-audit` (or `agp-nightly-audit`) → ("agp-nightly-audit", None); (None, error) when it isn't valid."""
    raw = (slug or "").strip() if isinstance(slug, str) else ""
    name = raw[len(ID_PREFIX):] if raw.startswith(ID_PREFIX) else raw
    if not NAME_RE.match(name):
        return None, (
            f"Invalid slug {slug!r}: must match ^[a-z0-9][a-z0-9-]{{1,44}}$ (2-45 lowercase letters, digits or "
            f"hyphens); the task is saved as {ID_PREFIX}<slug>."
        )
    return ID_PREFIX + name, None


def _validate_cron_field(field: str, label: str, lo: int, hi: int) -> Optional[str]:
    if not field or not CRON_CHARS_RE.match(field):
        return f"{label} field {field!r} contains invalid characters (allowed: 0-9*/,-)."
    for part in field.split(","):
        if not part:
            return f"{label} field {field!r} has an empty list item."
        if part.count("/") > 1:
            return f"{label} field {field!r} has invalid step syntax in {part!r}."
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit():
                return f"{label} field {field!r} has non-numeric step {step_str!r}."
            step = int(step_str)
            if step < 1 or step > max(hi, 1):
                return f"{label} field {field!r} step {step} out of range 1..{max(hi, 1)}."
        else:
            base = part
        if base == "*":
            continue
        if "-" in base:
            if base.count("-") != 1:
                return f"{label} field {field!r} has invalid range {base!r}."
            a_str, b_str = base.split("-", 1)
            if not (a_str.isdigit() and b_str.isdigit()):
                return f"{label} field {field!r} has invalid range bounds {base!r}."
            a, b = int(a_str), int(b_str)
            if a < lo or b > hi or a > b:
                return f"{label} field {field!r} range {a}-{b} out of bounds {lo}..{hi}."
            continue
        if not base.isdigit():
            return f"{label} field {field!r} has invalid value {base!r}."
        val = int(base)
        if val < lo or val > hi:
            return f"{label} field {field!r} value {val} out of bounds {lo}..{hi}."
    return None


def validate_cron(expr: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate a 5-field cron expression; return (normalized, error)."""
    fields = (expr or "").strip().split()
    if len(fields) != 5:
        return None, f"Invalid cron schedule {expr!r}: expected 5 fields (min hour dom mon dow), got {len(fields)}."
    for (label, lo, hi), field in zip(CRON_RANGES, fields):
        err = _validate_cron_field(field, label, lo, hi)
        if err:
            return None, f"Invalid cron schedule {expr!r}: {err}"
    return " ".join(fields), None


def _zone_known(zone: str) -> Optional[bool]:
    """Whether `zone` is an IANA time zone this computer knows; None when it can't tell (Windows without tzdata)."""
    if zone in ("UTC", "Etc/UTC"):
        return True
    try:
        import zoneinfo  # Python 3.9+

        names = zoneinfo.available_timezones()
        if names:
            return zone in names
    except Exception:
        pass
    for base in ("/usr/share/zoneinfo", "/var/db/timezone/zoneinfo"):
        if os.path.isdir(base):
            return os.path.isfile(os.path.join(base, *zone.split("/")))
    return None


def local_zone() -> Optional[str]:
    """This computer's IANA time zone (`Europe/Berlin`): $TZ, the /etc/localtime link or /etc/timezone. None when
    the standard library can't tell it reliably (Windows names its zones differently; a copied /etc/localtime)."""
    cands: List[str] = []
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if tz:
        cands.append(tz)
    if not compat.WINDOWS:
        for read in (lambda: os.readlink("/etc/localtime"), lambda: os.path.realpath("/etc/localtime")):
            try:
                cands.append(read())
            except OSError:
                pass
        try:
            with open("/etc/timezone", encoding="utf-8") as f:
                cands.append(f.read().strip())
        except OSError:
            pass
    for cand in cands:
        m = re.search(r"(?:^|/)zoneinfo(?:[.-][^/]*)?/(?:posix/|right/)?(.+)$", cand)
        name = m.group(1) if m else cand
        if ZONE_RE.match(name) and ("/" in name or name == "UTC") and _zone_known(name) is not False:
            return name
    return None


def validate_schedule(expr: str, default_zone: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`[CRON_TZ=<zone> |TZ=<zone> ]<5 cron fields>` → (args[0] for the sidecar, error). Without a prefix the cron
    gets `CRON_TZ=<default_zone>` (the computer's own zone) so it fires in the user's local time."""
    text = (expr or "").strip()
    label, zone = "CRON_TZ", default_zone
    m = TZ_PREFIX_RE.match(text)
    if m:
        label, zone, text = m.group(1), m.group(2), m.group(3)
        if not ZONE_RE.match(zone) or _zone_known(zone) is False:
            return None, (
                f"Invalid time zone {zone!r} in {expr!r}: use an IANA name such as Europe/Berlin or UTC "
                "(not an abbreviation like PST or an offset like +02:00)."
            )
    cron, err = validate_cron(text)
    if err or cron is None:
        return None, err or "Invalid cron schedule."
    return (f"{label}={zone} {cron}" if zone else cron), None


def validate_grant(grant: str) -> Optional[str]:
    """An access grant as Antigravity writes them, never a wildcard: command(<command prefix>), read_file(<abs path>),
    write_file(<abs path>), mcp(<server>/<tool>), read_url(<domain>), execute_url(<domain>)."""
    m = GRANT_RE.match((grant or "").strip())
    if not m:
        return (
            f"Invalid --grant {grant!r}: expected command(<command prefix>), read_file(<absolute path>), "
            "write_file(<absolute path>), mcp(<server>/<tool>), read_url(<domain>) or execute_url(<domain>)."
        )
    kind, value = m.group(1), m.group(2).strip()
    if not value or value in ("*", "/", "/*", "~", "~/") or (kind == "mcp" and value.endswith("*")):
        return f"Refusing --grant {grant!r}: too broad (no wildcards; name the exact command, path, tool or domain)."
    if kind in ("read_file", "write_file") and not (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
        return f"Invalid --grant {grant!r}: {kind} needs an absolute path."
    return None


def _validate_pct(val: float, label: str) -> Optional[str]:
    if math.isnan(val) or math.isinf(val) or val < 0 or val > 100:
        return f"Invalid {label} threshold {val}: must be between 0 and 100."
    return None


# ─────────────────────────────── quota querying ───────────────────────────────

def _base_url(addr: str) -> str:
    cleaned = addr.strip().rstrip("/")
    return cleaned if re.match(r"^https?://", cleaned) else f"http://{cleaned}"


def _discover_ls() -> Optional[Tuple[str, str]]:
    """(address, CSRF token) of Antigravity's running language server: the environment, its
    ~/.gemini/antigravity/daemon/hub_*.json (Antigravity 2.34+) or its command line (≤ 2.33) — compat.language_server()."""
    found = compat.language_server(probe=PROBE_TIMEOUT)
    return (found.addr, found.token) if found else None


def _fetch_ls_quota(ep: Tuple[str, str]) -> Optional[dict]:
    req = urllib.request.Request(
        f"{_base_url(ep[0])}/{SERVICE}/RetrieveUserQuotaSummary",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": ep[1],
        },
    )
    try:
        with OPENER.open(req, timeout=RPC_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _normalize_bucket(raw: dict) -> Dict[str, Any]:
    val = raw.get("left") if "left" in raw else raw.get("remainingFraction")
    try:
        # Proto3 JSON drops zero values: missing remainingFraction means 0.0 left.
        left = float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        left = 0.0
    left = max(0.0, left)
    reset = str(raw.get("resetTime") if "resetTime" in raw else raw.get("reset") or "")
    return {"left": left, "reset": reset, "disabled": bool(raw.get("disabled"))}


def _normalize_groups(data: dict) -> List[Dict[str, Any]]:
    inner = data.get("response")
    resp = inner if isinstance(inner, dict) else data
    raw_groups = resp.get("groups")
    if not isinstance(raw_groups, list):
        return []
    out: List[Dict[str, Any]] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("displayName") or g.get("name") or "?")
        if isinstance(g.get("models"), list):
            models = [str(m).strip().lower() for m in g["models"] if str(m).strip()]
        else:
            desc = str(g.get("description") or "")
            models = [m.strip().lower() for m in desc.split(":", 1)[1].split(",") if m.strip()] if ":" in desc else []
        buckets: Dict[str, Dict[str, Any]] = {}
        raw_buckets = g.get("buckets")
        if isinstance(raw_buckets, dict):
            for k, v in raw_buckets.items():
                if isinstance(v, dict):
                    buckets[str(k).strip().lower()] = _normalize_bucket(v)
        elif isinstance(raw_buckets, list):
            for b in raw_buckets:
                if isinstance(b, dict):
                    key = str(b.get("window") or b.get("bucketId") or "?").strip().lower()
                    buckets[key] = _normalize_bucket(b)
        out.append({"name": name, "models": models, "buckets": buckets})
    return out


def _load_cached_groups(now: float) -> Optional[List[Dict[str, Any]]]:
    try:
        with open(_quota_cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict) or cache.get("failed"):
        return None
    at = cache.get("at")
    if not isinstance(at, (int, float)) or not (0 <= now - at <= CACHE_MAX_AGE):
        return None
    groups = _normalize_groups(cache)
    return groups or None


def _match_group(target: str, groups: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = target.strip().lower()
    for g in groups:
        if key in g["name"].lower():
            return g
    hints = GROUP_HINTS.get(key, (key,))
    for g in groups:
        hay = (g["name"] + " " + " ".join(g["models"])).lower()
        if any(w in hay for w in hints):
            return g
    return None


def _parse_ts(ts: str) -> Optional[float]:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts or "")
    if not m:
        return None
    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))


def _span(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 36000:
        rem = s % 3600 // 60
        return f"{s // 3600} h" + (f" {rem} min" if rem else "")
    if s < 172800:
        return f"{round(s / 3600)} h"
    rem_h = s % 86400 // 3600
    return f"{s // 86400} days" + (f" {rem_h} h" if rem_h else "")


def _until(ts: str, now: float) -> str:
    t = _parse_ts(ts)
    if t is None:
        return ""
    return "now" if t <= now else "in " + _span(t - now)


def evaluate_gate(
    group_key: str,
    min_weekly: float,
    min_5h: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Query LS (or fresh 15-min cache) and decide RUN (0) vs SKIP (3)."""
    now = time.time() if now is None else now
    groups: List[Dict[str, Any]] = []
    source = "unavailable"

    ep = _discover_ls()
    if ep:
        raw = _fetch_ls_quota(ep)
        if raw is not None:
            groups = _normalize_groups(raw)
            source = "language_server"

    if not groups and source != "language_server":
        cached = _load_cached_groups(now)
        if cached:
            groups = cached
            source = "cache"

    thresholds = {"weekly": _parse_num(_fmt_num(min_weekly)), "5h": _parse_num(_fmt_num(min_5h))}
    grp = _match_group(group_key, groups) if groups else None
    if not grp:
        return {
            "decision": "run",
            "exit_code": EXIT_OK,
            "source": "unavailable",
            "group": None,
            "requested_group": group_key,
            "thresholds": thresholds,
            "buckets": {},
            "reasons": [],
            "message": "RUN: quota unavailable — proceeding",
        }

    buckets_info: Dict[str, Any] = {}
    ok_parts: List[str] = []
    failures: List[str] = []

    for bucket_key, label, thresh in (("weekly", "week", min_weekly), ("5h", "5h", min_5h)):
        b = grp["buckets"].get(bucket_key)
        if not b:
            continue
        if b["disabled"]:
            buckets_info[bucket_key] = {"disabled": True, "left_pct": None, "reset": b["reset"], "resets": ""}
            continue
        pct_val = round(b["left"] * 100.0, 6)
        pct_s = _fmt_pct(b["left"])
        when = _until(b["reset"], now)
        buckets_info[bucket_key] = {
            "disabled": False,
            "left_pct": _parse_num(_fmt_num(pct_val)),
            "reset": b["reset"],
            "resets": when,
        }
        ok_parts.append(f"{label} {pct_s}")
        if pct_val < thresh:
            reset_note = f" (resets {when})" if when else ""
            failures.append(f"{label} {pct_s} < {_fmt_num(thresh)}%{reset_note}")

    if failures:
        return {
            "decision": "skip",
            "exit_code": EXIT_SKIP,
            "source": source,
            "group": grp["name"],
            "requested_group": group_key,
            "thresholds": thresholds,
            "buckets": buckets_info,
            "reasons": failures,
            "message": f"SKIP: {grp['name']} — {', '.join(failures)}",
        }

    summary = ", ".join(ok_parts) if ok_parts else "no active limits"
    return {
        "decision": "run",
        "exit_code": EXIT_OK,
        "source": source,
        "group": grp["name"],
        "requested_group": group_key,
        "thresholds": thresholds,
        "buckets": buckets_info,
        "reasons": [],
        "message": f"RUN: {grp['name']} — {summary}",
    }


# ─────────────────────────────── prompt & sidecars ───────────────────────────────

def _shell_arg(arg: str) -> str:
    """One shell word: left as is when it needs no quoting (the usual install path), quoted otherwise."""
    if SAFE_ARG_RE.match(arg):
        return arg
    if compat.WINDOWS:
        return '"' + arg.replace('"', '\\"') + '"'
    return shlex.quote(arg)


def gate_command(group: str, min_weekly: float, min_5h: float) -> str:
    """The Step 0 command — exactly as the prompt tells the scheduled agent to run it and as its grant allows it."""
    return (
        f"{compat.python_cmd()} {_shell_arg(os.path.abspath(__file__))} gate --group {group} "
        f"--min-weekly {_fmt_num(min_weekly)} --min-5h {_fmt_num(min_5h)}"
    )


def build_prompt(raw_prompt: str, gate_cmd: Optional[str], workspace: Optional[str]) -> str:
    sections: List[str] = []
    if gate_cmd:
        sections.append(
            "Step 0 — quota gate (run this command before anything else):\n"
            f"  {gate_cmd}\n"
            "Exit code 3 (`SKIP: ...`): stop right away without edits or further steps and reply with that SKIP line. "
            "Exit code 0 (`RUN: ...`): continue with the task below. If the command is denied or can't run, stop "
            "and reply that the quota gate could not run."
        )
    if workspace:
        sections.append(f"Workspace: {workspace}")
    sections.append(raw_prompt.strip())
    return "\n\n".join(sections) + "\n"


def _extract_gate(prompt: str) -> Optional[Dict[str, Any]]:
    m = re.search(r'schedule_task\.py["\']?\s+gate\b([^\n]*)', prompt)
    if not m:
        return None
    flags = m.group(1)
    grp_m = re.search(r"--group\s+(\S+)", flags)
    wk_m = re.search(r"--min-weekly\s+([0-9]+(?:\.[0-9]+)?)", flags)
    h5_m = re.search(r"--min-5h\s+([0-9]+(?:\.[0-9]+)?)", flags)
    gate: Dict[str, Any] = {}
    if grp_m:
        gate["group"] = grp_m.group(1).strip("'\"")
    if wk_m:
        gate["min_weekly"] = _parse_num(wk_m.group(1))
    if h5_m:
        gate["min_5h"] = _parse_num(h5_m.group(1))
    return gate or None


def _summarize_prompt(prompt: str, max_len: int = 100) -> str:
    text = prompt.strip()
    if text.startswith("Step 0"):
        parts = text.split("\n\n", 1)
        text = parts[1].strip() if len(parts) > 1 else text
    text = re.sub(r"(?m)^Workspace:\s*.+$", "", text).strip()
    if not text:
        text = prompt.strip()
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 3].rstrip() + "..."


def _split_args(args: Any) -> Tuple[str, List[str], str]:
    """A schedule sidecar's args → (cron schedule, command words, prompt of `agentapi new-conversation`)."""
    if not isinstance(args, list) or not args:
        return "", [], ""
    words = [str(a) for a in args]
    cmd = words[1:]
    prompt = ""
    if "--" in cmd:
        prompt = " ".join(cmd[cmd.index("--") + 1:])
    elif cmd[:2] == ["agentapi", "new-conversation"] and len(cmd) > 2:
        prompt = cmd[-1]
    return words[0], cmd, prompt


def _uri_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    return path[1:] if re.match(r"^/[A-Za-z]:/", path) else path


def _read_json_dict(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_tasks() -> List[Dict[str, Any]]:
    """Every scheduled automation; `ours` marks the ones this script created (`agp-…`), the rest are read-only."""
    base = _sidecars_dir()
    if not os.path.isdir(base):
        return []
    tasks: List[Dict[str, Any]] = []
    for sid in sorted(os.listdir(base)):
        sidecar_path = os.path.join(base, sid, "sidecar.json")
        if not os.path.isfile(sidecar_path):
            continue
        data = _read_json_dict(sidecar_path)
        if not data or data.get("builtin") != "schedule":
            continue
        schedule, cmd, prompt = _split_args(data.get("args"))
        perms = data.get("agent_permissions") if isinstance(data.get("agent_permissions"), dict) else {}
        uris = [str(u) for u in perms.get("workspace_uris") or [] if u]
        grants = [str(g) for g in perms.get("access_grants") or [] if g]
        ws_m = re.search(r"(?m)^Workspace:\s*(.+)$", prompt)
        tasks.append({
            "id": sid,
            "ours": sid.startswith(ID_PREFIX),
            "display_name": str(data.get("display_name") or sid),
            "description": str(data.get("description") or ""),
            "schedule": schedule,
            "disabled": bool(data.get("disabled")),
            "gate": _extract_gate(prompt),
            "workspace": _uri_path(uris[0]) if uris else (ws_m.group(1).strip() if ws_m else None),
            "access_grants": grants,
            "prompt_summary": _summarize_prompt(prompt) if prompt else " ".join(cmd),
            "prompt": prompt,
            "path": sidecar_path,
        })
    return tasks


def create_task(
    slug: str,
    schedule: str,
    raw_prompt: str,
    title: Optional[str],
    description: Optional[str],
    group: str,
    min_weekly: float,
    min_5h: float,
    workspace: Optional[str],
    overwrite: bool,
    grants: Optional[List[str]] = None,
) -> Tuple[int, Optional[dict], str]:
    sid, err = sidecar_id(slug)
    if err or sid is None:
        return EXIT_ERROR, None, err or "Invalid slug."
    cron, cron_err = validate_schedule(schedule, local_zone())
    if cron_err or cron is None:
        return EXIT_ERROR, None, cron_err or "Invalid cron schedule."
    for val, lbl in ((min_weekly, "min-weekly"), (min_5h, "min-5h")):
        perr = _validate_pct(val, lbl)
        if perr:
            return EXIT_ERROR, None, perr
    if not raw_prompt or not raw_prompt.strip():
        return EXIT_ERROR, None, "Prompt must not be empty."
    extra = [g.strip() for g in grants or [] if g and g.strip()]
    for g in extra:
        gerr = validate_grant(g)
        if gerr:
            return EXIT_ERROR, None, gerr
    ws_abs = os.path.abspath(os.path.expanduser(workspace)) if workspace else None
    if ws_abs and not os.path.isdir(ws_abs):
        return EXIT_ERROR, None, f"Workspace folder does not exist: {ws_abs}"

    sidecar_dir = os.path.join(_sidecars_dir(), sid)
    sidecar_path = os.path.join(sidecar_dir, "sidecar.json")
    if os.path.islink(sidecar_dir):
        return EXIT_REFUSED, None, f"Refusing to write {sid!r}: {sidecar_dir} is a symbolic link."
    if os.path.exists(sidecar_path):
        existing = _read_json_dict(sidecar_path)
        if existing is None or existing.get("builtin") != "schedule":
            b_val = None if existing is None else existing.get("builtin")
            return EXIT_REFUSED, None, (
                f"Refusing to overwrite {sid!r}: existing sidecar is not a valid schedule sidecar "
                f"(builtin={b_val!r})."
            )
        if not overwrite:
            return EXIT_ERROR, None, (
                f"Scheduled task {sid!r} already exists ({sidecar_path}); pass --overwrite to replace it."
            )
    elif os.path.isdir(sidecar_dir) and os.listdir(sidecar_dir):
        return EXIT_REFUSED, None, (
            f"Refusing to write {sid!r}: folder already exists without a schedule sidecar.json."
        )

    name = sid[len(ID_PREFIX):]
    display_name = title.strip() if title and title.strip() else name.replace("-", " ").title()
    desc = description.strip() if description and description.strip() else _summarize_prompt(raw_prompt, max_len=160)
    gate_cmd = gate_command(group, min_weekly, min_5h) if (min_weekly > 0 or min_5h > 0) else None
    access_grants: List[str] = []
    for g in ([f"command({gate_cmd})"] if gate_cmd else []) + extra:
        if g not in access_grants:
            access_grants.append(g)
    permissions: Dict[str, List[str]] = {}
    if ws_abs:
        permissions["workspace_uris"] = [pathlib.Path(ws_abs).as_uri()]
    if access_grants:
        permissions["access_grants"] = access_grants
    payload: Dict[str, Any] = {
        "builtin": "schedule",
        "args": [cron, "agentapi", "new-conversation", "--", build_prompt(raw_prompt, gate_cmd, ws_abs)],
        "restart_policy": "always",
        "display_name": display_name,
        "description": desc,
    }
    if permissions:
        payload["agent_permissions"] = permissions

    tmp = f"{sidecar_path}.{os.getpid()}.tmp"
    try:
        os.makedirs(sidecar_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        compat.replace(tmp, sidecar_path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return EXIT_ERROR, None, f"Failed to write {sidecar_path}: {e}"

    granted = "; ".join(access_grants + permissions.get("workspace_uris", [])) or "nothing"
    notice = (
        f"It does not run until the user enables «{display_name}» in the Automations dashboard: {DASHBOARD} "
        "(never enable it yourself).\n"
        f"Unattended runs auto-deny every command that isn't granted. Granted: {granted}. "
        "Add what the task needs with --grant (create ... --overwrite), best after one test run of the prompt."
    )
    return EXIT_OK, {"id": sid, "path": sidecar_path, "sidecar": payload, "notice": notice}, (
        f"Created scheduled task {sid!r} «{display_name}» ({cron}) -> {sidecar_path}\n{notice}"
    )


def remove_task(slug: str) -> Tuple[int, str]:
    raw = (slug or "").strip()
    sid, err = sidecar_id(raw)
    if err or sid is None:
        return EXIT_ERROR, err or "Invalid slug."
    base = _sidecars_dir()
    sidecar_dir = os.path.join(base, sid)
    if not os.path.lexists(sidecar_dir):
        if raw != sid and os.path.isfile(os.path.join(base, raw, "sidecar.json")):
            return EXIT_REFUSED, (
                f"Refusing to remove {raw!r}: it was not created by /schedule-task (its tasks are named "
                f"{ID_PREFIX}…). The user disables or deletes it in the Automations dashboard: {DASHBOARD}"
            )
        return EXIT_ERROR, f"Scheduled task {sid!r} not found ({sidecar_dir})."
    if os.path.islink(sidecar_dir):
        return EXIT_REFUSED, f"Refusing to remove {sid!r}: {sidecar_dir} is a symbolic link."
    sidecar_path = os.path.join(sidecar_dir, "sidecar.json")
    if not os.path.isfile(sidecar_path):
        return EXIT_REFUSED, f"Refusing to remove {sid!r}: missing sidecar.json (not a schedule sidecar)."
    data = _read_json_dict(sidecar_path)
    if data is None:
        return EXIT_REFUSED, f"Refusing to remove {sid!r}: invalid or unreadable sidecar.json."
    if data.get("builtin") != "schedule":
        return EXIT_REFUSED, (
            f"Refusing to remove {sid!r}: not a schedule sidecar (builtin={data.get('builtin')!r})."
        )
    try:
        shutil.rmtree(sidecar_dir)
    except OSError as e:
        return EXIT_ERROR, f"Failed to remove {sidecar_dir}: {e}"
    return EXIT_OK, f"Removed scheduled task {sid!r} ({sidecar_dir})."


# ─────────────────────────────── CLI ───────────────────────────────

def _format_gate_summary(gate: Optional[Dict[str, Any]]) -> str:
    if not gate:
        return "none"
    parts = []
    if gate.get("group"):
        parts.append(str(gate["group"]))
    limits = []
    if "min_weekly" in gate:
        limits.append(f"week>={gate['min_weekly']}%")
    if "min_5h" in gate:
        limits.append(f"5h>={gate['min_5h']}%")
    if limits:
        parts.append(", ".join(limits))
    return " ".join(parts) if parts else "none"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="schedule_task.py",
        description="Manage quota-aware scheduled Antigravity automations (~/.gemini/config/sidecars/agp-*).",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="List scheduled Antigravity automations (the user's own read-only).")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")

    p_gate = sub.add_parser("gate", help="Check quota thresholds (exit 0 = RUN, exit 3 = SKIP).")
    p_gate.add_argument("--group", choices=GROUPS, default="gemini", help="Quota group to check (default: gemini).")
    p_gate.add_argument("--min-weekly", type=float, default=15.0, help="Minimum weekly quota %% (default: 15).")
    p_gate.add_argument("--min-5h", type=float, default=20.0, help="Minimum 5-hour quota %% (default: 20).")
    p_gate.add_argument("--json", action="store_true", help="Output JSON.")

    p_create = sub.add_parser("create", help="Create (or with --overwrite replace) an agp-<slug> automation.")
    p_create.add_argument("--slug", required=True, help="Task name ^[a-z0-9][a-z0-9-]{1,44}$; saved as agp-<slug>.")
    p_create.add_argument("--schedule", required=True,
                          help="5-field cron (min hour dom mon dow), optionally prefixed with CRON_TZ=<IANA zone>; "
                               "without a prefix the computer's own zone is added when it can be told.")
    p_create.add_argument("--prompt", required=True, help="Task prompt (or '-' to read stdin).")
    p_create.add_argument("--title", help="Display name (defaults to title-cased slug).")
    p_create.add_argument("--description", help="Short description (defaults to prompt summary).")
    p_create.add_argument("--group", choices=GROUPS, default="gemini", help="Quota group for Step 0 gate.")
    p_create.add_argument("--min-weekly", type=float, default=15.0, help="Minimum weekly quota %% (default: 15).")
    p_create.add_argument("--min-5h", type=float, default=20.0, help="Minimum 5-hour quota %% (default: 20).")
    p_create.add_argument("--workspace", help="Folder the task works in (granted read+write as workspace_uris).")
    p_create.add_argument("--grant", action="append", default=[],
                          help="Extra access grant for unattended runs, e.g. 'command(npm test)' (repeatable).")
    p_create.add_argument("--overwrite", action="store_true", help="Replace an existing agp-<slug> automation.")
    p_create.add_argument("--json", action="store_true", help="Output JSON on success.")

    p_rm = sub.add_parser("remove", help="Remove an agp-<slug> automation created by this script.")
    p_rm.add_argument("--slug", required=True, help="Slug (or agp-<slug> id) of the task to remove.")

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "cmd", None):
        ap.print_help()
        return EXIT_REFUSED

    if a.cmd == "list":
        tasks = list_tasks()
        if a.json:
            print(json.dumps(tasks, indent=2, ensure_ascii=False))
        elif not tasks:
            print(f"No scheduled automations in {_sidecars_dir()}.")
        else:
            for t in tasks:
                state = " (disabled)" if t["disabled"] else ""
                if t["ours"]:
                    detail = f"gate: {_format_gate_summary(t.get('gate'))}"
                else:
                    detail = "not created by /schedule-task: read-only"
                print(f"- {t['id']} «{t['display_name']}»{state}: [{t['schedule']}] {detail} — {t['prompt_summary']}")
        return EXIT_OK

    if a.cmd == "gate":
        for val, lbl in ((a.min_weekly, "min-weekly"), (a.min_5h, "min-5h")):
            perr = _validate_pct(val, lbl)
            if perr:
                print(f"❌ {perr}", file=sys.stderr)
                return EXIT_ERROR
        res = evaluate_gate(a.group, a.min_weekly, a.min_5h)
        if a.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(res["message"])
        return int(res["exit_code"])

    if a.cmd == "create":
        raw_prompt = sys.stdin.read() if a.prompt == "-" else a.prompt
        code, info, msg = create_task(
            slug=a.slug,
            schedule=a.schedule,
            raw_prompt=raw_prompt,
            title=a.title,
            description=a.description,
            group=a.group,
            min_weekly=a.min_weekly,
            min_5h=a.min_5h,
            workspace=a.workspace,
            overwrite=a.overwrite,
            grants=a.grant,
        )
        if code != EXIT_OK:
            print(f"❌ {msg}", file=sys.stderr)
            return code
        if a.json and info is not None:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(msg)
        return EXIT_OK

    if a.cmd == "remove":
        code, msg = remove_task(a.slug)
        if code != EXIT_OK:
            print(f"❌ {msg}", file=sys.stderr)
            return code
        print(msg)
        return EXIT_OK

    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
