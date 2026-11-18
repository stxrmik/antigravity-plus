#!/usr/bin/env python3
"""Antigravity Plus — /db: safe read-only database inspection, EXPLAIN, and migration risk analysis.

  db.py detect     [--workspace DIR] [--json]
  db.py schema     [--workspace DIR] [--target PATH_OR_ENV_VAR] [--table NAME] [--json]
  db.py explain    SQL [--workspace DIR] [--target PATH_OR_ENV_VAR] [--analyze] [--json]
  db.py migrations [--workspace DIR] [--since REF] [--json]

Safety invariants:
- Never prints secrets: a connection URL is shown only as scheme://host:port/dbname, other DB variables only when
  their name is a plain setting (host, port, db, user, sslmode …) and the value looks like one; everything else is ***.
- psql gets its connection through PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE (+ PGSSL* …), never through argv,
  runs with -X -w (no psqlrc, never a password prompt) and PGCONNECT_TIMEOUT, and receives each statement as its own
  `-c` option, so psql never interprets meta-commands or variables in the SQL.
- `schema` opens SQLite in read-only URI mode (`file:<path>?mode=ro` + `PRAGMA query_only = ON`) with a deadline,
  and runs PostgreSQL catalog queries inside `BEGIN READ ONLY` + `SET LOCAL statement_timeout` … `ROLLBACK`.
- `explain` accepts one SELECT / WITH … SELECT / VALUES statement; for PostgreSQL it also rejects every backslash and
  every `$` outside single-quoted literals, and with --analyze a denylist of side-effecting functions.
- Only the workspace's own `.env` and `.env.local` (local wins) and the process environment are used to pick a target
  automatically; any other env file needs `--target FILE:VAR`.
- `migrations` also exposes `analyze_migrations(ws, files=None)` for `/deploy`.

Exit codes:
  0 — success (or clean migrations)
  1 — migration risks found (`migrations` subcommand)
  2 — validation failure (unsafe SQL), missing/invalid target, missing CLI tool, or query error

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", ".tox", ".nox", ".direnv", "__pycache__", "__pypackages__",
    "site-packages", "dist", "build", ".next", "target", "vendor", "Pods", "DerivedData",
    ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
}
WINDOWS = os.name == "nt"
PG_SCHEMA_TIMEOUT = 30.0   # seconds for the whole psql run; the server-side statement_timeout is a bit shorter
PG_EXPLAIN_TIMEOUT = 60.0
SQLITE_DEADLINE = 20.0     # seconds for all SQLite schema/plan queries together
SQLITE_COUNT_BUDGET = 3.0  # seconds per COUNT(*) before the row count is reported as unknown
SQLITE_EXTS = (".db", ".sqlite", ".sqlite3")
ENV_FILE_RE = re.compile(r"^\.env(?:\.[A-Za-z0-9_.-]+)?$", re.I)
TEMPLATE_ENV_RE = re.compile(r"\.(?:example|sample|template|dist)$", re.I)
COMPOSE_FILE_RE = re.compile(r"^(?:docker-)?compose(?:\.[\w.-]+)?\.ya?ml$", re.I)
DB_ENV_NAME_RE = re.compile(
    r"^(?:DATABASE_URL|DATABASE_URI|DATABASE_DSN|DATABASE_[\w]+"
    r"|POSTGRES_[\w]+|POSTGRESQL_[\w]+|PG[\w]*"
    r"|REDIS_URL|REDIS_URI|REDIS_[\w]+"
    r"|DB_URL|DB_URI|DB_DSN|DB_HOST|DB_PORT|DB_NAME|DB_DATABASE|DB_USER|DB_USERNAME|DB_PASSWORD|DB_PASS"
    r"|SQLITE_[\w]+|MYSQL_[\w]+)$",
    re.I,
)
SECRET_VAR_RE = re.compile(r"(?:PASS(?:WORD|WD)?|PWD|PW|SECRET|TOKEN|KEY|AUTH|CREDENTIAL)", re.I)
# Variable names whose value is a plain connection setting (never a secret) — shown verbatim when the value looks safe.
SAFE_DB_VAR_RE = re.compile(
    r"(?:^|_)(?:HOST|HOSTNAME|SERVER|ADDR|ADDRESS|PORT|DB|DBNAME|DATABASE|NAME|SCHEMA|USER|USERNAME|UID|"
    r"SSLMODE|SSL_MODE|SSL|DIALECT|DRIVER|ENGINE|ADAPTER|CHARSET|ENCODING|TIMEZONE|TZ|POOL|"
    r"POOLSIZE|POOL_SIZE|MAXCONN|MAX_CONNECTIONS|MIN_CONNECTIONS|TIMEOUT|CONNECT_TIMEOUT|REGION|CLUSTER)$",
    re.I,
)
# A value simple enough to echo verbatim once the name is a known-safe setting (no spaces, quotes, or shell/url junk).
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+\-]*$")
# Inline secrets that can hide inside a non-URL value (libpq/ODBC key=value blobs, redis flags).
_INLINE_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pass|pwd|pw|sslpassword|secret|token|auth[_-]?token|api[_-]?key)\b\s*[:=]\s*)"
    r"('[^']*'|\"[^\"]*\"|[^;,&\s]+)"
)
_REQUIREPASS_RE = re.compile(r"(?i)(--requirepass[=\s]+)(\S+)")
FORBIDDEN_SQL_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "GRANT", "REVOKE", "VACUUM", "COPY", "CALL",
    "ATTACH", "DETACH", "PRAGMA", "REINDEX", "EXECUTE", "PREPARE",
    "DEALLOCATE", "DO",
}
ALLOWED_START_KEYWORDS = {"SELECT", "WITH", "VALUES"}
# Functions / forms a Postgres EXPLAIN ANALYZE would actually execute with lasting effects (not rolled back, or a lock).
ANALYZE_FORBIDDEN_RE = re.compile(
    r"(?i)\b(?:"
    r"pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_switch_wal|pg_switch_xlog|"
    r"pg_create_(?:physical|logical)_replication_slot|pg_drop_replication_slot|pg_replication_slot_advance|"
    r"pg_create_restore_point|pg_promote|pg_log_backend_memory_contexts|"
    r"pg_stat_reset\w*|pg_advisory_(?:un)?lock\w*|pg_advisory_xact_lock\w*|"
    r"pg_notify|pg_sleep\w*|set_config|nextval|setval|currval|lastval|"
    r"dblink\w*|dblink_exec|lo_(?:create|creat|import|export|unlink|put|from_bytea)|"
    r"txid_current|pg_current_xact_id|pg_import_system_collations"
    r")\s*\(",
)
# FOR UPDATE / FOR SHARE and SELECT ... INTO take locks / create objects even inside a read-only txn's EXPLAIN ANALYZE.
ANALYZE_FOR_LOCK_RE = re.compile(r"(?i)\bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE|KEY\s+SHARE)\b")
SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _slash(path: str) -> str:
    return path.replace("\\", "/")


def _rel(path: str, ws: str) -> str:
    try:
        return _slash(os.path.relpath(path, ws))
    except ValueError:
        return _slash(path)


def _is_env_dir(path: str) -> bool:
    """A virtualenv / conda env (whatever its folder is called): its files are libraries, not the project."""
    return os.path.isfile(os.path.join(path, "pyvenv.cfg")) or os.path.isdir(os.path.join(path, "conda-meta"))


def _walk_shallow(ws: str, max_depth: int = 4) -> List[Tuple[str, str]]:
    """Return sorted (abs_path, rel_path) of regular, non-symlink files in workspace skipping junk dirs and envs."""
    out: List[Tuple[str, str]] = []
    ws_real = os.path.realpath(ws)
    for root, dirs, files in os.walk(ws_real):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")
                         and not _is_env_dir(os.path.join(root, d)))
        rel_dir = os.path.relpath(root, ws_real)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []
        for fn in sorted(files):
            full = os.path.join(root, fn)
            # regular files only: a FIFO or device named like .env.production would block open() forever
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            out.append((full, _rel(full, ws_real)))
    return out


# ─────────────────────────────────────────────── secret masking & .env ───────────────────────────────────────────────

def _split_hostport(hostport: str) -> Tuple[str, str]:
    """Split host[:port] into (host, port), handling [ipv6]:port and bare ipv6."""
    hp = hostport.strip()
    if hp.startswith("[") and "]" in hp:
        end = hp.index("]")
        host = hp[1:end]
        rest = hp[end + 1:]
        port = rest[1:] if rest.startswith(":") else ""
        return host, port
    if ":" in hp and hp.count(":") == 1:
        host, _, port = hp.rpartition(":")
        return host, port
    return hp, ""


def _parse_db_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a DB connection URL into components, splitting credentials at the LAST '@' (passwords may hold ?/#).

    Returns a dict with scheme/host/port/dbname/user/password and any query params; None if it is not a URL.
    """
    s = (url or "").strip()
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://(.*)$", s)
    if not m:
        return None
    scheme = m.group(1)
    rest = m.group(2)
    # Credentials end at the LAST '@' before the path/query; everything after it begins with the host.
    if "@" in rest:
        userinfo, hostpart = rest.rsplit("@", 1)
    else:
        userinfo, hostpart = "", rest
    hp_end = len(hostpart)
    for ch in ("/", "?", "#"):
        idx = hostpart.find(ch)
        if idx != -1 and idx < hp_end:
            hp_end = idx
    hostport = hostpart[:hp_end]
    after = hostpart[hp_end:]
    dbname = ""
    if after.startswith("/"):
        dbname = urllib.parse.unquote(after[1:].split("?", 1)[0].split("#", 1)[0])
    params: Dict[str, str] = {}
    if "?" in after:
        query = after.split("?", 1)[1].split("#", 1)[0]
        for item in query.split("&"):
            if not item:
                continue
            k, _eq, v = item.partition("=")
            params[urllib.parse.unquote(k).lower()] = urllib.parse.unquote(v)
    host, port = _split_hostport(hostport)
    user = password = ""
    if userinfo:
        u, _sep, pw = userinfo.partition(":")
        user = urllib.parse.unquote(u)
        password = urllib.parse.unquote(pw)
    if not password and params.get("password"):
        password = params["password"]
    out = {"scheme": scheme, "host": host, "port": port, "dbname": dbname, "user": user, "password": password}
    for k in ("sslmode", "sslrootcert", "sslcert", "sslkey", "options", "application_name"):
        if params.get(k):
            out[k] = params[k]
    return out


def _display_url(parsed: Dict[str, str]) -> str:
    """Build a safe display string: scheme://host[:port][/dbname] — never credentials or query params."""
    scheme = "postgresql" if parsed["scheme"].lower().startswith("postgres") else parsed["scheme"].split("+", 1)[0]
    host = parsed.get("host") or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    hostport = host + (f":{parsed['port']}" if parsed.get("port") else "")
    db = f"/{parsed['dbname']}" if parsed.get("dbname") else ""
    return f"{scheme}://{hostport}{db}"


def mask_url(url: str) -> str:
    """Reduce a DB URL to scheme://host:port/dbname, dropping credentials and query parameters entirely."""
    parsed = _parse_db_url(url)
    if not parsed:
        return (url or "").strip()
    return _display_url(parsed)


def _url_secret(url: str) -> str:
    """Extract the raw password from a URL if present (never returned to callers; only added to the scrub list)."""
    parsed = _parse_db_url(url)
    if not parsed:
        return ""
    return parsed.get("password") or ""


def mask_secrets(text: str, extra_secrets: Optional[Sequence[str]] = None) -> str:
    """Scrub URL passwords, key=value passwords, redis flags, and known secret literals from output text."""
    if not text:
        return ""
    out = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", lambda m: mask_url(m.group(0)), text)
    out = _INLINE_SECRET_RE.sub(r"\1***", out)
    out = _REQUIREPASS_RE.sub(r"\1***", out)
    out = re.sub(
        r"(?i)(\b(?:pgpassword|rediscli_auth)\s*=\s*)('[^']*'|\"[^\"]*\"|\S+)",
        r"\1***",
        out,
    )
    if extra_secrets:
        ordered = sorted({s for s in extra_secrets if s and len(s) >= 2}, key=len, reverse=True)
        for sec in ordered:
            out = out.replace(sec, "***")
            quoted = urllib.parse.quote(sec, safe="")
            if quoted and quoted != sec and len(quoted) >= 2:
                out = out.replace(quoted, "***")
    return out


def _unquote_env_value(raw: str) -> str:
    val = raw.strip()
    m_dq = re.match(r'^"((?:\\.|[^"\\])*)"(?:\s+#.*)?$', val)
    if m_dq:
        return m_dq.group(1).replace('\\"', '"')
    m_sq = re.match(r"^'([^']*)'(?:\s+#.*)?$", val)
    if m_sq:
        return m_sq.group(1)
    return re.sub(r"\s+#.*$", "", val).strip()


def parse_env_file(path: str) -> List[Dict[str, Any]]:
    """Parse KEY=VALUE entries from a .env* file without executing it."""
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return entries
    if text.startswith("﻿"):  # strip a UTF-8 BOM so the first variable is not lost
        text = text[1:]
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        key, raw_val = s.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        entries.append({"name": key, "value": _unquote_env_value(raw_val), "line": lineno})
    return entries


def _env_var_kind(name: str, value: str) -> str:
    low_v = value.strip().lower()
    low_n = name.lower()
    if low_v.startswith(("postgres://", "postgresql://", "postgresql+")) or low_n.startswith(("postgres", "pg")):
        return "postgres"
    if low_v.startswith(("redis://", "rediss://")) or low_n.startswith("redis"):
        return "redis"
    if low_v.startswith("sqlite:") or low_n.startswith("sqlite"):
        return "sqlite"
    if low_v.startswith("mysql") or low_n.startswith("mysql"):
        return "mysql"
    if SECRET_VAR_RE.search(name):
        return "secret"
    return "db_config"


def _mask_env_value(name: str, value: str) -> str:
    """Allowlist display: a URL becomes scheme://host:port/db; a plain setting shows through; anything else is ***."""
    if not value:
        return ""
    if "://" in value:
        # covers postgres://… and jdbc:postgresql://… (mask_url finds the ://run and drops creds + query)
        return mask_secrets(value) if value.lower().startswith("jdbc:") else mask_url(value)
    if SECRET_VAR_RE.search(name):
        return "***"
    # Secrets can hide inside a non-URL blob (libpq `host=.. password=..`, ODBC `Pwd=..`, redis `--requirepass ..`).
    scrubbed = _REQUIREPASS_RE.sub(r"\1***", _INLINE_SECRET_RE.sub(r"\1***", value))
    if scrubbed != value:
        return scrubbed
    if SAFE_DB_VAR_RE.search(name) and SAFE_VALUE_RE.match(value):
        return value
    return "***"


def _strip_yaml_comment(line: str) -> str:
    in_sq = False
    in_dq = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "#" and not in_sq and not in_dq:
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line


def _unquote_yaml_scalar(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _classify_compose_engine(service: str, image: str) -> str:
    img = (image or "").strip().lower()
    svc = (service or "").strip().lower()
    if re.search(r"(?:^|/)(?:postgres|postgis|timescaledb|pgvector)(?:[:@${\s]|$)", img):
        return "postgres"
    if re.search(r"(?:^|/)(?:redis|valkey|redis-stack)(?:[:@${\s]|$)", img):
        return "redis"
    if not img:
        if "postgres" in svc:
            return "postgres"
        if "redis" in svc:
            return "redis"
    return ""


def parse_compose_file(path: str, rel_file: str) -> List[Dict[str, Any]]:
    """Extract Postgres and Redis service definitions from a docker-compose/compose YAML file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []

    services: Dict[str, Dict[str, Any]] = {}
    in_services = False
    svc_indent: Optional[int] = None
    current_svc: Optional[str] = None
    sub_section = ""
    sub_indent: Optional[int] = None

    for raw in lines:
        cleaned = _strip_yaml_comment(raw)
        if not cleaned.strip() or cleaned.strip() == "---":
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        stripped = cleaned.strip()

        if indent == 0:
            if re.match(r"^services\s*:\s*$", stripped):
                in_services = True
                svc_indent = None
                current_svc = None
                sub_section = ""
                continue
            in_services = False
            current_svc = None
            sub_section = ""
            continue

        if not in_services:
            continue

        if svc_indent is None or indent <= svc_indent:
            m_svc = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(?:#.*)?$", stripped)
            if m_svc and (svc_indent is None or indent == svc_indent):
                svc_indent = indent
                svc_name = m_svc.group(1)
                current_svc = svc_name
                services[svc_name] = {
                    "file": rel_file,
                    "service": svc_name,
                    "image": "",
                    "ports": [],
                    "env": {},
                    "_secrets": {},
                }
                sub_section = ""
                sub_indent = None
                continue

        if not current_svc or svc_indent is None or indent <= svc_indent:
            continue

        svc_data = services[current_svc]
        if sub_indent is not None and indent <= sub_indent and not stripped.startswith("-"):
            sub_section = ""
            sub_indent = None

        if not sub_section or indent == svc_indent + 2:
            m_key = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", stripped)
            if m_key:
                key, rest = m_key.group(1), m_key.group(2).strip()
                if key == "image":
                    svc_data["image"] = _unquote_yaml_scalar(rest)
                    sub_section = ""
                elif key in ("ports", "environment"):
                    sub_section = key
                    sub_indent = indent
                else:
                    sub_section = ""
                continue

        if sub_section == "ports" and stripped.startswith("-"):
            port_val = _unquote_yaml_scalar(stripped[1:].strip())
            if port_val:
                svc_data["ports"].append(port_val)
        elif sub_section == "environment":
            if stripped.startswith("-"):
                item = _unquote_yaml_scalar(stripped[1:].strip())
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    svc_data["_secrets"][k] = v
                    svc_data["env"][k] = _mask_env_value(k, v)
            else:
                m_kv = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", stripped)
                if m_kv:
                    k, v = m_kv.group(1), _unquote_yaml_scalar(m_kv.group(2))
                    svc_data["_secrets"][k] = v
                    svc_data["env"][k] = _mask_env_value(k, v)

    out: List[Dict[str, Any]] = []
    for svc_data in services.values():
        engine = _classify_compose_engine(svc_data["service"], svc_data["image"])
        if engine:
            svc_data["engine"] = engine
            out.append(svc_data)
    return out


# ─────────────────────────────────────────────── detect ───────────────────────────────────────────────

def detect_workspace(ws: str) -> Dict[str, Any]:
    """Detect SQLite files, compose DB services, and .env* DB variables (credentials masked)."""
    ws_real = os.path.realpath(ws)
    files = _walk_shallow(ws_real, max_depth=4)

    sqlite_files: List[Dict[str, Any]] = []
    compose_services: List[Dict[str, Any]] = []
    env_files: List[Dict[str, Any]] = []
    raw_env_vars: List[Dict[str, Any]] = []
    raw_compose: List[Dict[str, Any]] = []
    secrets: List[str] = []

    for abs_p, rel_p in files:
        base = os.path.basename(abs_p)
        low = base.lower()
        if low.endswith(SQLITE_EXTS):
            try:
                size = os.path.getsize(abs_p)
            except OSError:
                size = 0
            sqlite_files.append({"path": rel_p, "abs": abs_p, "size_bytes": size})
        elif COMPOSE_FILE_RE.match(base):
            parsed = parse_compose_file(abs_p, rel_p)
            for svc in parsed:
                raw_compose.append(svc)
                for k, v in svc["_secrets"].items():
                    if SECRET_VAR_RE.search(k) and v:
                        secrets.append(v)
                    sec = _url_secret(v)
                    if sec:
                        secrets.append(sec)
                compose_services.append({k: v for k, v in svc.items() if k != "_secrets"})
        elif ENV_FILE_RE.match(base):
            is_template = bool(TEMPLATE_ENV_RE.search(base))
            entries = parse_env_file(abs_p)
            db_vars: List[Dict[str, str]] = []
            for e in entries:
                name, val = e["name"], e["value"]
                if not DB_ENV_NAME_RE.match(name):
                    continue
                if SECRET_VAR_RE.search(name) and val:
                    secrets.append(val)
                sec = _url_secret(val)
                if sec:
                    secrets.append(sec)
                kind = _env_var_kind(name, val)
                masked = _mask_env_value(name, val)
                db_vars.append({"name": name, "masked_value": masked, "kind": kind})
                if not is_template:
                    raw_env_vars.append({"file": rel_p, "name": name, "value": val, "kind": kind})
            if db_vars:
                env_files.append({"file": rel_p, "is_template": is_template, "variables": db_vars})

    return {
        "workspace": ws_real,
        "sqlite": sqlite_files,
        "compose_services": compose_services,
        "env_files": env_files,
        "_raw_env": raw_env_vars,
        "_raw_compose": raw_compose,
        "_secrets": secrets,
    }


def _public_detect(det: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "workspace": det["workspace"],
        "sqlite": [{"path": s["path"], "size_bytes": s["size_bytes"]} for s in det["sqlite"]],
        "compose_services": det["compose_services"],
        "env_files": det["env_files"],
    }


def render_detect(det: Dict[str, Any]) -> str:
    pub = _public_detect(det)
    lines = [f"# Database detection — {pub['workspace']}", ""]
    if not pub["sqlite"] and not pub["compose_services"] and not pub["env_files"]:
        lines.append("No SQLite files, compose DB services, or .env DB variables found.")
        return "\n".join(lines)

    if pub["sqlite"]:
        lines.append(f"## SQLite files ({len(pub['sqlite'])})")
        for s in pub["sqlite"]:
            lines.append(f"- {s['path']} ({s['size_bytes']} bytes)")
        lines.append("")

    if pub["compose_services"]:
        lines.append(f"## Compose services ({len(pub['compose_services'])})")
        for svc in pub["compose_services"]:
            ports = ", ".join(svc["ports"]) if svc["ports"] else "no published ports"
            img = svc["image"] or "(build)"
            lines.append(f"- {svc['file']} :: `{svc['service']}` ({svc['engine']}, image: {img}, ports: {ports})")
            for k, v in sorted(svc["env"].items()):
                if DB_ENV_NAME_RE.match(k):
                    lines.append(f"  - {k}={v}")
        lines.append("")

    if pub["env_files"]:
        lines.append(f"## Environment files ({len(pub['env_files'])})")
        for ef in pub["env_files"]:
            tag = " (template)" if ef["is_template"] else ""
            lines.append(f"- {ef['file']}{tag}")
            for v in ef["variables"]:
                shown = v["masked_value"] if v["masked_value"] else "(empty)"
                lines.append(f"  - {v['name']} = {shown}")
        lines.append("")

    return "\n".join(lines).rstrip()


# ─────────────────────────────────────────────── target resolution ───────────────────────────────────────────────

_PG_CONN_ENV = (
    ("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"), ("dbname", "PGDATABASE"),
    ("password", "PGPASSWORD"), ("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
    ("sslcert", "PGSSLCERT"), ("sslkey", "PGSSLKEY"), ("options", "PGOPTIONS"), ("application_name", "PGAPPNAME"),
)


def _pg_conn_from_dsn(dsn: str) -> Dict[str, str]:
    """Return a connection dict (host/port/user/password/dbname/ssl…) from a Postgres URL or libpq conninfo."""
    s = dsn.strip()
    parsed = _parse_db_url(s)
    if parsed is not None:
        return {k: v for k, v in parsed.items() if k != "scheme" and v}
    # libpq key=value conninfo string: parse key=value / key='quoted value' pairs without executing it.
    conn: Dict[str, str] = {}
    for m in re.finditer(r"(?i)\b([a-z_]+)\s*=\s*(?:'((?:\\.|[^'])*)'|\"([^\"]*)\"|(\S+))", s):
        key = m.group(1).lower()
        val = next((g for g in (m.group(2), m.group(3), m.group(4)) if g is not None), "")
        if m.group(2) is not None:
            val = val.replace("\\'", "'").replace("\\\\", "\\")
        if key in ("dbname", "database"):
            conn["dbname"] = val
        elif key in ("user", "username"):
            conn["user"] = val
        elif key in ("host", "hostaddr"):
            conn.setdefault("host", val)
        elif key in ("password",):
            conn["password"] = val
        elif key in ("port", "sslmode", "sslrootcert", "sslcert", "sslkey", "options", "application_name"):
            conn[key] = val
    return conn


def _redis_target_from_dsn(dsn: str) -> Tuple[List[str], str]:
    """Build redis-cli args (host/port/db/user/tls — never the password) and the password for REDISCLI_AUTH."""
    s = dsn.strip()
    tls = s.lower().startswith("rediss://")
    parsed = _parse_db_url(s) or {}
    host = parsed.get("host") or "127.0.0.1"
    port = parsed.get("port") or "6379"
    user = parsed.get("user") or ""
    pw = parsed.get("password") or ""
    db = ""
    dbname = parsed.get("dbname") or ""
    if dbname.isdigit():
        db = dbname
    args = ["-h", host, "-p", port]
    if db:
        args.extend(["-n", db])
    if user and user.lower() != "default":
        args.extend(["--user", user])
    if tls:
        args.append("--tls")
    return args, pw


def _sqlite_path_from_url(url: str, ws: str) -> str:
    raw = url.strip()
    if raw.startswith("sqlite:///"):
        p = raw[len("sqlite:///"):]
    elif raw.startswith("sqlite://"):
        p = raw[len("sqlite://"):]
    else:
        p = raw
    p = p.split("?", 1)[0]
    p = urllib.parse.unquote(p)
    if os.path.isabs(p):
        return os.path.realpath(p)
    return os.path.realpath(os.path.join(ws, p))


def _target_from_dsn(val: str, label: str, ws: str, secrets: List[str]) -> Optional[Dict[str, Any]]:
    low = val.strip().lower()
    if low.startswith(("postgres://", "postgresql://", "postgresql+")):
        conn = _pg_conn_from_dsn(val)
        pw = conn.get("password", "")
        all_secrets = list(secrets) + ([pw] if pw else [])
        return {
            "kind": "postgres",
            "mode": "params",
            "label": label,
            "conn": conn,
            "password": pw,
            "secrets": all_secrets,
        }
    if low.startswith(("redis://", "rediss://")):
        args, pw = _redis_target_from_dsn(val)
        all_secrets = list(secrets) + ([pw] if pw else [])
        return {
            "kind": "redis",
            "mode": "dsn",
            "label": label,
            "args": args,
            "password": pw,
            "secrets": all_secrets,
        }
    if low.startswith("sqlite:"):
        path = _sqlite_path_from_url(val, ws)
        return {
            "kind": "sqlite",
            "label": _rel(path, ws),
            "path": path,
            "secrets": secrets,
        }
    return None


def _env_lookup_map(det: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """Auto-detect source: only the root `.env` and `.env.local` (local wins) plus process env (overrides all).

    Returns {VAR: (value, source)} so the chosen target can name the file/variable it came from.
    """
    merged: Dict[str, Tuple[str, str]] = {}
    # .env first, then .env.local so a local override wins; the process environment overrides both.
    for fname in (".env", ".env.local"):
        for item in det["_raw_env"]:
            if item["file"] == fname:
                merged[item["name"]] = (item["value"], fname)
    for k, v in os.environ.items():
        if DB_ENV_NAME_RE.match(k):
            merged[k] = (v, "environment")
    return merged


def _pg_env_target(env_map: Dict[str, Tuple[str, str]], secrets: List[str]) -> Optional[Dict[str, Any]]:
    def g(*names: str) -> str:
        for n in names:
            if env_map.get(n) and env_map[n][0]:
                return env_map[n][0]
        return ""

    host = g("PGHOST", "POSTGRES_HOST")
    db = g("PGDATABASE", "POSTGRES_DB")
    if not (db or host):
        return None
    conn: Dict[str, str] = {}
    for key, val in (("host", host), ("port", g("PGPORT", "POSTGRES_PORT")), ("user", g("PGUSER", "POSTGRES_USER")),
                     ("dbname", db), ("password", g("PGPASSWORD", "POSTGRES_PASSWORD")),
                     ("sslmode", g("PGSSLMODE", "POSTGRES_SSLMODE"))):
        if val:
            conn[key] = val
    pw = conn.get("password", "")
    all_secrets = list(secrets) + ([pw] if pw else [])
    return {
        "kind": "postgres",
        "mode": "params",
        "label": f"postgres env ({db or host})",
        "conn": conn,
        "password": pw,
        "secrets": all_secrets,
    }


def resolve_target(ws: str, target: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve --target (path, env var name, DSN, or compose service) or auto-detect workspace DB."""
    det = detect_workspace(ws)
    secrets = list(det["_secrets"])
    env_map = _env_lookup_map(det)
    plain_env = {k: v for k, (v, _src) in env_map.items()}
    for k, (v, _src) in env_map.items():
        if SECRET_VAR_RE.search(k) and v:
            secrets.append(v)
        sec = _url_secret(v)
        if sec:
            secrets.append(sec)

    def _src_label(var: str, val: str) -> str:
        src = env_map[var][1]
        where = "env" if src == "environment" else src
        return f"{var} in {where} ({mask_url(val)})" if "://" in val else f"{var} in {where}"

    if target:
        t = target.strip()
        # Allow `FILE:VAR` to read a variable from a non-root env file explicitly (auto-detect never reads those).
        file_var = re.match(r"^(\.env[\w.-]*):([A-Za-z_][A-Za-z0-9_]*)$", t)
        if file_var and not os.path.exists(os.path.realpath(os.path.join(ws, t))):
            fname, var = file_var.group(1), file_var.group(2)
            fpath = os.path.realpath(os.path.join(ws, fname))
            val = ""
            if os.path.isfile(fpath):
                for e in parse_env_file(fpath):
                    if e["name"] == var:
                        val = e["value"]
            if not val:
                return None, f"Variable {var} not found (or empty) in {fname}"
            extra = [val, _url_secret(val)]
            resolved = _target_from_dsn(val, f"{var} in {fname} ({mask_url(val)})", ws, secrets + [s for s in extra if s])
            if resolved:
                return resolved, ""
            env_path = os.path.realpath(val if os.path.isabs(val) else os.path.join(ws, val))
            if os.path.isfile(env_path):
                return {"kind": "sqlite", "label": _rel(env_path, ws), "path": env_path, "secrets": secrets}, ""
            return None, f"{fname}:{var} is not a recognized DB DSN or existing SQLite file."

        # 1. Explicit DSN
        if "://" in t:
            resolved = _target_from_dsn(t, mask_url(t), ws, secrets)
            if resolved:
                return resolved, ""
            return None, f"Unsupported DSN scheme in target: {mask_url(t)}"

        # 2. Existing SQLite file path (absolute or relative to ws)
        cand = os.path.realpath(t if os.path.isabs(t) else os.path.join(ws, t))
        if os.path.isfile(cand):
            return {"kind": "sqlite", "label": _rel(cand, ws), "path": cand, "secrets": secrets}, ""

        # 3. Environment variable name (root .env/.env.local or the process environment)
        if t in plain_env and plain_env[t]:
            val = plain_env[t]
            resolved = _target_from_dsn(val, _src_label(t, val), ws, secrets)
            if resolved:
                return resolved, ""
            # Maybe env var holds a SQLite file path
            env_path = os.path.realpath(val if os.path.isabs(val) else os.path.join(ws, val))
            if os.path.isfile(env_path):
                return {"kind": "sqlite", "label": _rel(env_path, ws), "path": env_path, "secrets": secrets}, ""
            return None, f"Environment variable {t} is not a recognized DB DSN or existing SQLite file."

        # 4. Compose service name
        for svc in det["_raw_compose"]:
            if svc["service"] == t:
                return {
                    "kind": svc["engine"],
                    "mode": "compose",
                    "label": f"compose:{svc['service']} ({svc['file']})",
                    "compose_file": os.path.join(det["workspace"], svc["file"]),
                    "service": svc["service"],
                    "svc_env": svc["_secrets"],
                    "env_map": plain_env,
                    "secrets": secrets,
                }, ""

        if t.lower().endswith(SQLITE_EXTS):
            return None, f"SQLite file not found: {t}"
        return None, f"Could not resolve database target '{t}' in {det['workspace']}"

    # Auto-detect priority: URL env vars -> split PG env -> SQLite files -> compose Postgres -> Redis URL -> compose Redis
    for var_name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "PGURL", "DB_URL"):
        if plain_env.get(var_name):
            resolved = _target_from_dsn(plain_env[var_name], _src_label(var_name, plain_env[var_name]), ws, secrets)
            if resolved:
                return resolved, ""

    pg_env = _pg_env_target(env_map, secrets)
    if pg_env:
        return pg_env, ""

    if det["sqlite"]:
        s0 = det["sqlite"][0]
        return {"kind": "sqlite", "label": s0["path"], "path": s0["abs"], "secrets": secrets}, ""

    for svc in det["_raw_compose"]:
        if svc["engine"] == "postgres":
            return {
                "kind": "postgres",
                "mode": "compose",
                "label": f"compose:{svc['service']} ({svc['file']})",
                "compose_file": os.path.join(det["workspace"], svc["file"]),
                "service": svc["service"],
                "svc_env": svc["_secrets"],
                "secrets": secrets,
            }, ""

    for var_name in ("REDIS_URL", "REDIS_URI"):
        if plain_env.get(var_name):
            resolved = _target_from_dsn(plain_env[var_name], _src_label(var_name, plain_env[var_name]), ws, secrets)
            if resolved:
                return resolved, ""

    for svc in det["_raw_compose"]:
        if svc["engine"] == "redis":
            return {
                "kind": "redis",
                "mode": "compose",
                "label": f"compose:{svc['service']} ({svc['file']})",
                "compose_file": os.path.join(det["workspace"], svc["file"]),
                "service": svc["service"],
                "svc_env": svc["_secrets"],
                "secrets": secrets,
            }, ""

    return None, f"No database target detected in {det['workspace']}; pass --target PATH_OR_ENV_VAR."


# ─────────────────────────────────────────────── subprocess DB runners ───────────────────────────────────────────────

def _run_cmd(
    argv: List[str],
    cwd: str,
    stdin_text: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Tuple[int, str, str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin_text,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout:.0f}s"
    except OSError as e:
        return 127, "", str(e)


def _compose_exec_prefix(compose_file: str, service: str, extra_env_names: Sequence[str] = ()) -> Tuple[Optional[List[str]], str]:
    docker = shutil.which("docker")
    docker_compose = shutil.which("docker-compose")
    if docker:
        base = [docker, "compose", "-f", compose_file, "exec", "-T"]
    elif docker_compose:
        base = [docker_compose, "-f", compose_file, "exec", "-T"]
    else:
        return None, "docker / docker-compose is not installed on PATH"
    for name in extra_env_names:
        base.extend(["-e", name])  # forwards value from process env without placing secret in argv
    base.append(service)
    return base, ""


def _resolve_compose_env(raw: str, env_map: Dict[str, str]) -> str:
    """Resolve ${VAR} / ${VAR:-default} templates from env_map/os.environ; return '' if unresolved."""
    if not raw:
        return ""

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        default = m.group(3)
        val = env_map.get(name) if name in env_map else os.environ.get(name)
        if val:
            return val
        if m.group(2) in (":-", "-") and default is not None:
            return default
        return ""

    resolved = re.sub(r"\$\{([A-Za-z_]\w*)(?:(:?-)(.*?))?\}", repl, raw)
    resolved = re.sub(r"\$([A-Za-z_]\w*)", lambda m: env_map.get(m.group(1)) or os.environ.get(m.group(1), ""), resolved)
    if "${" in resolved or not resolved.strip():
        return ""
    return resolved


def _build_psql_cmd(target: Dict[str, Any], extra_psql_args: List[str]) -> Tuple[Optional[List[str]], Dict[str, str], str]:
    env: Dict[str, str] = {}
    if target["mode"] == "compose":
        svc_env = target.get("svc_env") or {}
        env_map = target.get("env_map") or {}
        pw = _resolve_compose_env(svc_env.get("POSTGRES_PASSWORD") or svc_env.get("PGPASSWORD") or "", env_map)
        pass_env = ["PGPASSWORD"] if pw else []
        if pw:
            env["PGPASSWORD"] = pw
            if pw not in target.get("secrets", ()):
                target.setdefault("secrets", []).append(pw)
        prefix, err = _compose_exec_prefix(target["compose_file"], target["service"], pass_env)
        if not prefix:
            return None, env, err
        cmd = prefix + ["psql", "-w"]
        user = _resolve_compose_env(svc_env.get("POSTGRES_USER") or svc_env.get("PGUSER") or "", env_map)
        db = _resolve_compose_env(svc_env.get("POSTGRES_DB") or svc_env.get("PGDATABASE") or "", env_map)
        if user:
            cmd.extend(["-U", user])
        if db:
            cmd.extend(["-d", db])
        cmd.extend(extra_psql_args)
        return cmd, env, ""

    psql = shutil.which("psql")
    if not psql:
        return None, env, "psql is not installed on PATH"
    # Connection details go through PG* environment variables only — never argv (which `ps` would expose).
    conn = target.get("conn") or {}
    for key, env_name in _PG_CONN_ENV:
        if conn.get(key):
            env[env_name] = conn[key]
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    # -w: never prompt for a password (so a password-protected DB fails fast instead of hanging on a TTY).
    cmd = [psql, "-w"] + extra_psql_args
    return cmd, env, ""


def _build_redis_cmd(target: Dict[str, Any], redis_args: List[str]) -> Tuple[Optional[List[str]], Dict[str, str], str]:
    env: Dict[str, str] = {}
    if target["mode"] == "compose":
        svc_env = target.get("svc_env") or {}
        env_map = target.get("env_map") or {}
        pw = _resolve_compose_env(svc_env.get("REDIS_PASSWORD") or "", env_map)
        pass_env = ["REDISCLI_AUTH"] if pw else []
        if pw:
            env["REDISCLI_AUTH"] = pw
            if pw not in target.get("secrets", ()):
                target.setdefault("secrets", []).append(pw)
        prefix, err = _compose_exec_prefix(target["compose_file"], target["service"], pass_env)
        if not prefix:
            return None, env, err
        return prefix + ["redis-cli"] + redis_args, env, ""

    cli = shutil.which("redis-cli")
    if not cli:
        return None, env, "redis-cli is not installed on PATH"
    if target.get("password"):
        env["REDISCLI_AUTH"] = target["password"]
    return [cli] + list(target.get("args") or []) + redis_args, env, ""


# ─────────────────────────────────────────────── schema ───────────────────────────────────────────────

def _q_sqlite_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_schema(target: Dict[str, Any], table_filter: Optional[str]) -> Tuple[int, Dict[str, Any], str]:
    path = target["path"]
    if not os.path.isfile(path):
        return 2, {}, f"SQLite file not found: {path}"
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    conn: Optional[sqlite3.Connection] = None
    deadline = [time.monotonic() + SQLITE_DEADLINE]  # mutable: tightened per COUNT(*) below

    def _progress() -> int:
        return 1 if time.monotonic() > deadline[0] else 0  # non-zero aborts the running query

    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.set_progress_handler(_progress, 20000)  # abort any query that runs past the deadline (e.g. COUNT on a view)
        cur = conn.cursor()
        cur.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        raw_tables = cur.fetchall()
        if table_filter:
            matched = [row for row in raw_tables if row[0] == table_filter]
            if not matched:
                matched = [row for row in raw_tables if row[0].lower() == table_filter.lower()]
            if not matched:
                return 2, {}, f"Table not found in SQLite database: {table_filter}"
            raw_tables = matched

        has_stat1 = bool(
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'").fetchone()
        )
        large_db = os.path.getsize(path) > 100_000_000

        tables: List[Dict[str, Any]] = []
        for tbl_name, tbl_type in raw_tables:
            qt = _q_sqlite_ident(tbl_name)
            cols = []
            for _cid, cname, ctype, notnull, dflt, pk in cur.execute(f"PRAGMA table_info({qt})").fetchall():
                cols.append({
                    "name": cname,
                    "type": ctype or "ANY",
                    "nullable": not bool(notnull),
                    "default": dflt,
                    "primary_key": bool(pk),
                })
            indexes = []
            if tbl_type == "table":
                for _seq, iname, unique, origin, _partial in cur.execute(f"PRAGMA index_list({qt})").fetchall():
                    qi = _q_sqlite_ident(iname)
                    icols = [r[2] for r in cur.execute(f"PRAGMA index_info({qi})").fetchall() if r[2]]
                    indexes.append({"name": iname, "unique": bool(unique), "origin": origin, "columns": icols})

            fks = []
            for row in cur.execute(f"PRAGMA foreign_key_list({qt})").fetchall():
                fks.append({"from": row[3], "to_table": row[2], "to_column": row[4]})

            est_rows: Optional[int] = None
            if has_stat1:
                stat_row = cur.execute("SELECT stat FROM sqlite_stat1 WHERE tbl = ? LIMIT 1", (tbl_name,)).fetchone()
                if stat_row and stat_row[0]:
                    first = str(stat_row[0]).split()[0]
                    if first.isdigit():
                        est_rows = int(first)
            if est_rows is None and not large_db:
                prev = deadline[0]
                deadline[0] = min(prev, time.monotonic() + SQLITE_COUNT_BUDGET)
                try:
                    est_rows = int(cur.execute(f"SELECT COUNT(*) FROM {qt}").fetchone()[0])
                except sqlite3.OperationalError:
                    est_rows = None  # COUNT(*) took too long (e.g. an expensive view) — report rows unknown
                finally:
                    deadline[0] = prev

            tables.append({
                "name": tbl_name,
                "type": tbl_type,
                "estimated_rows": est_rows,
                "columns": cols,
                "indexes": indexes,
                "foreign_keys": fks,
            })

        return 0, {"kind": "sqlite", "target": target["label"], "tables": tables}, ""
    except sqlite3.OperationalError as e:
        if time.monotonic() > deadline[0] or "interrupt" in str(e).lower():
            return 2, {}, f"SQLite schema inspection exceeded {SQLITE_DEADLINE:.0f}s deadline and was aborted."
        return 2, {}, f"SQLite error: {mask_secrets(str(e), target['secrets'])}"
    except sqlite3.Error as e:
        return 2, {}, f"SQLite error: {mask_secrets(str(e), target['secrets'])}"
    finally:
        if conn is not None:
            conn.close()


_PG_SCHEMA_BODY = """\
SELECT 'T', n.nspname, c.relname,
       COALESCE(s.n_live_tup, GREATEST(c.reltuples::bigint, 0))
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY n.nspname, c.relname;
SELECT 'C', n.nspname, c.relname, a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       a.attnotnull,
       replace(replace(COALESCE(pg_get_expr(d.adbin, d.adrelid), ''), E'\\t', ' '), E'\\n', ' '),
       EXISTS (
         SELECT 1 FROM pg_catalog.pg_index i
         WHERE i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY(i.indkey)
       )
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY n.nspname, c.relname, a.attnum;
SELECT 'I', schemaname, tablename, indexname,
       replace(replace(indexdef, E'\\t', ' '), E'\\n', ' ')
FROM pg_catalog.pg_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY schemaname, tablename, indexname;
"""


def _one_line(sql: str) -> str:
    """Line breaks outside '…' literals → spaces (comments are already gone by now): a Windows .cmd/.bat wrapper
    around psql cuts its -c argument at the first line break, which would leave half a statement."""
    out, quoted = [], False
    for ch in sql:
        if ch == "'":
            quoted = not quoted  # '' (an escaped quote) toggles twice
        out.append(" " if ch in "\r\n" and not quoted else ch)
    return "".join(out)


def _pg_readonly_script(body: str) -> str:
    """Wrap fixed catalog SQL in a read-only transaction with a server-side statement timeout, then roll back."""
    return _one_line(
        "SET standard_conforming_strings = on; "
        "BEGIN READ ONLY; "
        "SET LOCAL statement_timeout = '25s'; "
        "SET LOCAL default_transaction_read_only = on; "
        f"{body} "
        "ROLLBACK;"
    )


def _postgres_schema(target: Dict[str, Any], table_filter: Optional[str], ws: str) -> Tuple[int, Dict[str, Any], str]:
    # Catalog SQL is fixed and ours; send it as one -c string so psql never reads meta-commands from stdin.
    script = _pg_readonly_script(_PG_SCHEMA_BODY)
    cmd, env, err = _build_psql_cmd(
        target, ["-X", "-q", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "-c", script]
    )
    if not cmd:
        return 2, {}, err
    code, out, stderr = _run_cmd(cmd, cwd=ws, extra_env=env, timeout=PG_SCHEMA_TIMEOUT)
    if code != 0:
        msg = mask_secrets((stderr or out or "psql failed").strip(), target["secrets"])
        return 2, {}, f"PostgreSQL schema inspection failed: {msg}"

    tables_by_key: Dict[str, Dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        rec = parts[0]
        if rec == "T" and len(parts) >= 4:
            sch, tbl, est = parts[1], parts[2], parts[3]
            key = f"{sch}.{tbl}" if sch != "public" else tbl
            try:
                est_int: Optional[int] = int(est)
            except ValueError:
                est_int = None
            tables_by_key[f"{sch}.{tbl}"] = {
                "name": key,
                "schema": sch,
                "table": tbl,
                "estimated_rows": est_int,
                "columns": [],
                "indexes": [],
            }
        elif rec == "C" and len(parts) >= 8:
            sch, tbl, cname, dtype, notnull, dflt, is_pk = parts[1:8]
            entry = tables_by_key.get(f"{sch}.{tbl}")
            if entry is not None:
                entry["columns"].append({
                    "name": cname,
                    "type": dtype,
                    "nullable": notnull.lower() not in ("t", "true", "1"),
                    "default": dflt if dflt else None,
                    "primary_key": is_pk.lower() in ("t", "true", "1"),
                })
        elif rec == "I" and len(parts) >= 5:
            sch, tbl, iname, idef = parts[1:5]
            entry = tables_by_key.get(f"{sch}.{tbl}")
            if entry is not None:
                entry["indexes"].append({
                    "name": iname,
                    "unique": "UNIQUE INDEX" in idef.upper(),
                    "definition": idef,
                })

    tables = list(tables_by_key.values())
    if table_filter:
        tf_low = table_filter.lower()
        filtered = [t for t in tables if t["name"].lower() == tf_low or t["table"].lower() == tf_low]
        if not filtered:
            return 2, {}, f"Table not found in PostgreSQL database: {table_filter}"
        tables = filtered

    return 0, {"kind": "postgres", "target": target["label"], "tables": tables}, ""


def _redis_schema(target: Dict[str, Any], ws: str) -> Tuple[int, Dict[str, Any], str]:
    cmd_db, env, err = _build_redis_cmd(target, ["DBSIZE"])
    if not cmd_db:
        return 2, {}, err
    c1, o1, e1 = _run_cmd(cmd_db, cwd=ws, extra_env=env, timeout=15.0)
    if c1 != 0:
        msg = mask_secrets((e1 or o1 or "redis-cli failed").strip(), target["secrets"])
        return 2, {}, f"Redis inspection failed: {msg}"

    cmd_info, env2, _ = _build_redis_cmd(target, ["INFO", "keyspace"])
    assert cmd_info is not None
    c2, o2, e2 = _run_cmd(cmd_info, cwd=ws, extra_env=env2, timeout=15.0)
    if c2 != 0:
        msg = mask_secrets((e2 or o2 or "redis-cli failed").strip(), target["secrets"])
        return 2, {}, f"Redis INFO keyspace failed: {msg}"

    dbsize: Optional[int] = None
    m_sz = re.search(r"(\d+)", o1.strip())
    if m_sz:
        dbsize = int(m_sz.group(1))

    keyspaces: List[Dict[str, str]] = []
    for line in o2.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        db_name, stats = s.split(":", 1)
        keyspaces.append({"db": db_name.strip(), "stats": stats.strip()})

    return 0, {
        "kind": "redis",
        "target": target["label"],
        "dbsize": dbsize,
        "keyspaces": keyspaces,
    }, ""


def render_schema(data: Dict[str, Any]) -> str:
    kind = data["kind"]
    lines = [f"# Schema — {data['target']} ({kind})", ""]
    if kind == "redis":
        lines.append(f"DBSIZE: {data['dbsize'] if data.get('dbsize') is not None else 'unknown'} keys")
        if data.get("keyspaces"):
            lines.append("Keyspace:")
            for ks in data["keyspaces"]:
                lines.append(f"- {ks['db']}: {ks['stats']}")
        else:
            lines.append("Keyspace: (empty)")
        return "\n".join(lines)

    tables = data.get("tables") or []
    if not tables:
        lines.append("No user tables found.")
        return "\n".join(lines)

    for tbl in tables:
        est = f"~{tbl['estimated_rows']} rows" if tbl.get("estimated_rows") is not None else "rows unknown"
        t_kind = f" [{tbl['type']}]" if tbl.get("type") and tbl["type"] != "table" else ""
        lines.append(f"## {tbl['name']}{t_kind} ({est})")
        for col in tbl.get("columns") or []:
            flags: List[str] = []
            if col.get("primary_key"):
                flags.append("PK")
            if not col.get("nullable"):
                flags.append("NOT NULL")
            if col.get("default") is not None:
                flags.append(f"DEFAULT {col['default']}")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"- {col['name']}: {col['type']}{suffix}")
        if tbl.get("indexes"):
            lines.append("Indexes:")
            for idx in tbl["indexes"]:
                if "columns" in idx:
                    uniq = "UNIQUE " if idx.get("unique") else ""
                    cols = ", ".join(idx["columns"]) or "(expression)"
                    lines.append(f"- {idx['name']}: {uniq}({cols})")
                else:
                    lines.append(f"- {idx['name']}: {idx.get('definition', '')}")
        if tbl.get("foreign_keys"):
            lines.append("Foreign keys:")
            for fk in tbl["foreign_keys"]:
                lines.append(f"- {fk['from']} -> {fk['to_table']}({fk['to_column']})")
        lines.append("")
    return "\n".join(lines).rstrip()


# ─────────────────────────────────────────────── explain & SQL safety ───────────────────────────────────────────────

def _tokenize_sql(sql: str) -> Tuple[Optional[List[Tuple[str, str, int]]], str]:
    """Tokenize SQL into (kind, text, start_offset). Rejects unclosed comments/strings."""
    if "\x00" in sql:
        return None, "NUL byte is not allowed in SQL"
    tokens: List[Tuple[str, str, int]] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        # Whitespace
        if ch.isspace():
            j = i + 1
            while j < n and sql[j].isspace():
                j += 1
            tokens.append(("WS", sql[i:j], i))
            i = j
            continue
        # Line comment --
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            j = n if j == -1 else j
            tokens.append(("COMMENT", sql[i:j], i))
            i = j
            continue
        # Block comment /* ... */ (supports nesting)
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if sql[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif sql[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth != 0:
                return None, "Unclosed /* block comment in SQL"
            tokens.append(("COMMENT", sql[i:j], i))
            i = j
            continue
        # Postgres escape string E'...'
        if ch in ("e", "E") and i + 1 < n and sql[i + 1] == "'":
            j = i + 2
            closed = False
            while j < n:
                if sql[j] == "\\" and j + 1 < n:
                    j += 2
                elif sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                    else:
                        j += 1
                        closed = True
                        break
                else:
                    j += 1
            if not closed:
                return None, "Unclosed escape string literal in SQL"
            tokens.append(("STRING", sql[i:j], i))
            i = j
            continue
        # Standard single-quoted string '...'
        if ch == "'":
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                    else:
                        j += 1
                        closed = True
                        break
                else:
                    j += 1
            if not closed:
                return None, "Unclosed string literal in SQL"
            tokens.append(("STRING", sql[i:j], i))
            i = j
            continue
        # Double-quoted identifier "..."
        if ch == '"':
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                    else:
                        j += 1
                        closed = True
                        break
                else:
                    j += 1
            if not closed:
                return None, "Unclosed quoted identifier in SQL"
            tokens.append(("QIDENT", sql[i:j], i))
            i = j
            continue
        # Backtick identifier `...`
        if ch == "`":
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == "`":
                    if j + 1 < n and sql[j + 1] == "`":
                        j += 2
                    else:
                        j += 1
                        closed = True
                        break
                else:
                    j += 1
            if not closed:
                return None, "Unclosed backtick identifier in SQL"
            tokens.append(("BTIDENT", sql[i:j], i))
            i = j
            continue
        # Postgres dollar-quoted string $tag$...$tag$
        if ch == "$":
            m_tag = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m_tag:
                delim = m_tag.group(0)
                end = sql.find(delim, i + len(delim))
                if end == -1:
                    return None, "Unclosed dollar-quoted string in SQL"
                j = end + len(delim)
                tokens.append(("STRING", sql[i:j], i))
                i = j
                continue
        # Word / keyword
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in ("_", "$")):
                j += 1
            tokens.append(("WORD", sql[i:j], i))
            i = j
            continue
        # Semicolon
        if ch == ";":
            tokens.append(("SEMI", ";", i))
            i += 1
            continue
        # Backslash (disallow psql meta-commands outside literals/comments)
        if ch == "\\":
            tokens.append(("BACKSLASH", "\\", i))
            i += 1
            continue
        # Punctuation / numbers / operators
        tokens.append(("OTHER", ch, i))
        i += 1
    return tokens, ""


def validate_readonly_sql(sql: str, allow_backticks: bool = False, dialect: str = "generic") -> Tuple[bool, str, str]:
    """Validate that SQL is a single strictly read-only SELECT / WITH ... SELECT / VALUES statement.

    dialect="postgres" additionally rejects every `$` that is not inside a single-quoted literal or comment
    (no dollar-quoted strings, no `$n` parameters, no `$`-identifiers), closing the lexer-disagreement bypass
    where a fake dollar-quoted string hides extra statements from this validator but not from psql.
    """
    tokens, err = _tokenize_sql(sql or "")
    if tokens is None:
        return False, "", err

    meaningful = [(k, v, pos) for k, v, pos in tokens if k not in ("WS", "COMMENT")]
    if not meaningful:
        return False, "", "Empty SQL query"

    if not allow_backticks and any(k == "BTIDENT" for k, _v, _pos in meaningful):
        return False, "", "Backtick identifiers are not allowed in read-only SQL (use double quotes)"

    # Reject psql backslash meta-commands outside string literals/comments
    if any(k == "BACKSLASH" for k, _v, _pos in meaningful):
        return False, "", "Backslash commands are not allowed in read-only EXPLAIN"

    if dialect == "postgres":
        # Any `$` outside a single-quoted literal/comment is forbidden: dollar-quoted strings (tokenized as a STRING
        # beginning with `$`), bare `$`/`$n` params (OTHER token), and `$`-bearing identifiers (WORD with `$`).
        for k, v, _pos in meaningful:
            if k == "STRING" and v.startswith("$"):
                return False, "", "Dollar-quoted strings are not allowed in read-only Postgres EXPLAIN"
            if k == "OTHER" and v == "$":
                return False, "", "'$' (dollar-quote or parameter) is not allowed in read-only Postgres EXPLAIN"
            if k == "WORD" and "$" in v:
                return False, "", "'$' in an identifier is not allowed in read-only Postgres EXPLAIN"

    # Allow at most one trailing semicolon
    semi_indices = [idx for idx, (k, _v, _pos) in enumerate(meaningful) if k == "SEMI"]
    if len(semi_indices) > 1 or (semi_indices and semi_indices[0] != len(meaningful) - 1):
        return False, "", "Multi-statement SQL is rejected; only a single read-only statement is allowed"
    if semi_indices and semi_indices[0] == 0:
        return False, "", "Empty SQL statement before ';'"

    # Strip trailing semicolon from meaningful stream and rebuild comment-free SQL
    last_semi_pos = meaningful[-1][2] if semi_indices else len(sql)
    rebuilt_parts: List[str] = []
    for k, v, pos in tokens:
        if pos >= last_semi_pos:
            break
        rebuilt_parts.append(" " if k == "COMMENT" else v)
    cleaned = "".join(rebuilt_parts).strip()
    if semi_indices:
        meaningful = meaningful[:-1]

    # Check opening keyword (allow optional outer parens like (SELECT 1))
    first_word = ""
    for k, v, _pos in meaningful:
        if k == "OTHER" and v == "(":
            continue
        if k == "WORD":
            first_word = v.upper()
        break
    if first_word not in ALLOWED_START_KEYWORDS:
        got = first_word or meaningful[0][1]
        return False, "", f"Only SELECT / WITH ... SELECT / VALUES queries are allowed (got {got!r})"

    words = [v.upper() for k, v, _pos in meaningful if k == "WORD"]
    for w in words:
        if w in FORBIDDEN_SQL_KEYWORDS:
            return False, "", f"Mutating/DDL SQL keyword {w!r} is forbidden in read-only EXPLAIN"

    if first_word == "WITH" and not any(w in ("SELECT", "VALUES") for w in words[1:]):
        return False, "", "WITH query must contain SELECT or VALUES"

    # Reject SELECT ... INTO <new_table> at paren depth 0
    depth = 0
    for k, v, _pos in meaningful:
        if k == "OTHER":
            if v == "(":
                depth += 1
            elif v == ")":
                depth = max(0, depth - 1)
        elif k == "WORD" and depth == 0 and v.upper() == "INTO":
            return False, "", "SELECT ... INTO is forbidden in read-only EXPLAIN"

    return True, cleaned, ""


def _analyze_denylist_error(cleaned: str) -> str:
    """Return a reason if EXPLAIN ANALYZE would run a side-effecting function or take a row lock, else ''.

    ANALYZE actually executes the statement; functions like pg_terminate_backend / nextval / dblink_exec and
    FOR UPDATE/SHARE locks are NOT undone by the surrounding ROLLBACK, so they are refused outright.
    """
    bare = _strip_sql_comments_and_strings(cleaned)  # blank out string/comment bodies so only real code is scanned
    m = ANALYZE_FORBIDDEN_RE.search(bare)
    if m:
        name = m.group(0).rstrip("(").strip()
        return f"function {name}() may have lasting side effects and cannot run under --analyze"
    if ANALYZE_FOR_LOCK_RE.search(bare):
        return "FOR UPDATE / FOR SHARE takes row locks and cannot run under --analyze"
    return ""


def _run_explain(ws: str, target_arg: Optional[str], sql: str, analyze: bool) -> Tuple[int, Dict[str, Any], str]:
    valid, cleaned, err = validate_readonly_sql(sql, allow_backticks=False)
    bt_only_err = (not valid and "Backtick identifiers" in err)
    if not valid and not bt_only_err:
        return 2, {}, f"Rejected unsafe SQL: {err}"

    target, t_err = resolve_target(ws, target_arg)
    if not target:
        return (2, {}, f"Rejected unsafe SQL: {err}") if bt_only_err else (2, {}, t_err)

    if bt_only_err:
        if target["kind"] != "sqlite":
            return 2, {}, f"Rejected unsafe SQL: {err}"
        valid, cleaned, err = validate_readonly_sql(sql, allow_backticks=True)
        if not valid:
            return 2, {}, f"Rejected unsafe SQL: {err}"

    if target["kind"] == "redis":
        return 2, {}, "EXPLAIN is not supported for Redis targets"

    if target["kind"] == "sqlite":
        path = target["path"]
        if not os.path.isfile(path):
            return 2, {}, f"SQLite file not found: {path}"
        uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA query_only = ON;")
            rows = conn.execute(f"EXPLAIN QUERY PLAN {cleaned}").fetchall()
            depths: Dict[Any, int] = {}
            plan_lines = []
            for r in rows:
                depth = depths.get(r[1], -1) + 1
                depths[r[0]] = depth
                plan_lines.append(f"{'  ' * depth}{r[3]}")
            note = "SQLite does not support EXPLAIN ANALYZE; showing EXPLAIN QUERY PLAN." if analyze else ""
            return 0, {
                "kind": "sqlite",
                "target": target["label"],
                "sql": cleaned,
                "analyze": False,
                "note": note,
                "plan": plan_lines,
            }, ""
        except sqlite3.Error as e:
            return 2, {}, f"SQLite EXPLAIN failed: {mask_secrets(str(e), target['secrets'])}"
        finally:
            if conn is not None:
                conn.close()

    # Postgres: re-validate with the strict dialect (no backslash, no `$` outside single-quoted literals).
    valid, cleaned, err = validate_readonly_sql(sql, allow_backticks=False, dialect="postgres")
    if not valid:
        return 2, {}, f"Rejected unsafe SQL: {err}"
    if analyze:
        deny = _analyze_denylist_error(cleaned)
        if deny:
            return 2, {}, f"Rejected unsafe SQL for --analyze: {deny}"

    opts = "ANALYZE, BUFFERS, FORMAT TEXT" if analyze else "FORMAT TEXT, BUFFERS"
    # One -c string, pure SQL (validated: no backslash, no stray ';'), so psql never interprets a meta-command.
    pg_script = (
        "SET standard_conforming_strings = on; "
        "BEGIN READ ONLY; "
        "SET LOCAL statement_timeout = '55s'; "
        "SET LOCAL default_transaction_read_only = on; "
        f"EXPLAIN ({opts}) {_one_line(cleaned)}; "
        "ROLLBACK;"
    )
    cmd, env, c_err = _build_psql_cmd(
        target, ["-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "-c", pg_script]
    )
    if not cmd:
        return 2, {}, c_err
    code, out, stderr = _run_cmd(cmd, cwd=ws, extra_env=env, timeout=PG_EXPLAIN_TIMEOUT)
    if code != 0:
        msg = mask_secrets((stderr or out or "psql failed").strip(), target["secrets"])
        return 2, {}, f"PostgreSQL EXPLAIN failed: {msg}"

    plan_lines = [mask_secrets(ln, target["secrets"]) for ln in out.splitlines() if ln.strip()]
    return 0, {
        "kind": "postgres",
        "target": target["label"],
        "sql": cleaned,
        "analyze": analyze,
        "note": "",
        "plan": plan_lines,
    }, ""


def render_explain(data: Dict[str, Any]) -> str:
    lines = [f"# EXPLAIN — {data['target']} ({data['kind']})", f"SQL: {data['sql']}"]
    if data.get("note"):
        lines.append(f"Note: {data['note']}")
    lines.append("")
    lines.extend(data.get("plan") or ["(no query plan rows returned)"])
    return "\n".join(lines)


# ─────────────────────────────────────────────── migrations ───────────────────────────────────────────────

def _risk(
    rel_file: str,
    line: int,
    severity: str,
    kind: str,
    operation: str,
    message: str,
    alternative: str,
) -> Dict[str, Any]:
    return {
        "file": _slash(rel_file),
        "line": max(1, int(line)),
        "severity": severity,
        "kind": kind,
        "operation": operation,
        "message": message,
        "alternative": alternative,
    }


def _blank_preserving_newlines(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _strip_sql_comments_and_strings(sql: str) -> str:
    """Replace SQL comments and string literal bodies with spaces while keeping line numbers exact."""
    tokens, _err = _tokenize_sql(sql)
    if tokens is None:
        # Fallback if malformed quotes/comments: strip line comments only
        return re.sub(r"--[^\n]*", lambda m: _blank_preserving_newlines(m.group(0)), sql)
    parts: List[str] = []
    for kind, val, _pos in tokens:
        if kind == "COMMENT":
            parts.append(_blank_preserving_newlines(val))
        elif kind == "STRING":
            # Keep quotes so syntax shape remains visible without exposing literal words
            inner = _blank_preserving_newlines(val[1:-1]) if len(val) >= 2 else ""
            parts.append("'" + inner + "'")
        else:
            parts.append(val)
    return "".join(parts)


def _line_at(text: str, pos: int) -> int:
    return text[:max(0, pos)].count("\n") + 1


# A (possibly schema-qualified, possibly quoted) table reference — quoted parts may contain spaces.
_TBL_REF = r'(?:"[^"]*"|`[^`]*`|\[[^\]]*\]|\w+)(?:\.(?:"[^"]*"|`[^`]*`|\[[^\]]*\]|\w+))*'
# Functions whose value changes per call: a column DEFAULT using one still forces a full-table rewrite when NOT NULL.
_VOLATILE_DEFAULT_RE = re.compile(
    r"(?i)\b(?:gen_random_uuid|uuid_generate_v\d|uuid|now|clock_timestamp|statement_timestamp|"
    r"transaction_timestamp|localtimestamp|current_timestamp|timeofday|random|nextval|newid|sysdate|unix_timestamp)\b"
)


def _norm_ident(raw: str) -> str:
    """Normalize a (possibly quoted / schema-qualified) identifier to its lowercased final component."""
    raw = raw.strip()
    last = raw.rsplit(".", 1)[-1].strip()
    if last[:1] in ('"', "`") and last[-1:] in ('"', "`"):
        return last[1:-1].lower()
    if last[:1] == "[" and last[-1:] == "]":
        return last[1:-1].lower()
    return last.lower()


def _strip_goose_down(sql: str) -> str:
    """Blank out goose / sql-migrate Down sections (keeping line numbers) so rollback DDL is not flagged."""
    if not re.search(r"(?im)^\s*--\s*\+(?:goose|migrate)\s+Down\b", sql):
        return sql
    out_lines: List[str] = []
    in_down = False
    for line in sql.splitlines(keepends=True):
        if re.match(r"(?i)^\s*--\s*\+(?:goose|migrate)\s+Down\b", line):
            in_down = True
        elif re.match(r"(?i)^\s*--\s*\+(?:goose|migrate)\s+Up\b", line):
            in_down = False
        out_lines.append(re.sub(r"[^\n]", " ", line) if in_down else line)
    return "".join(out_lines)


def _split_top_level_commas(stmt: str, base_offset: int) -> List[Tuple[str, int]]:
    """Split a SQL clause on commas at parenthesis depth 0, returning (chunk, abs_offset)."""
    chunks: List[Tuple[str, int]] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(stmt):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            chunks.append((stmt[start:idx], base_offset + start))
            start = idx + 1
    chunks.append((stmt[start:], base_offset + start))
    return chunks


def _analyze_sql_text(sql: str, rel_file: str, base_line: int = 1) -> List[Dict[str, Any]]:
    """Detect high-risk DDL/migration operations in SQL text."""
    masked = _strip_sql_comments_and_strings(_strip_goose_down(sql))
    risks: List[Dict[str, Any]] = []

    # Tables created in this same file: an index built on one of them locks nothing live, so it is not flagged.
    created_tables: Set[str] = set()
    for m in re.finditer(
        rf"\bCREATE\s+(?:(?:GLOBAL|LOCAL|TEMP(?:ORARY)?|UNLOGGED)\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_TBL_REF})",
        masked, re.I,
    ):
        created_tables.add(_norm_ident(m.group(1)))

    # Split statements on `;` while keeping exact offsets in `masked`
    stmts: List[Tuple[str, int]] = []
    start = 0
    for idx, ch in enumerate(masked):
        if ch == ";":
            stmts.append((masked[start:idx], start))
            start = idx + 1
    if start < len(masked):
        stmts.append((masked[start:], start))

    for stmt, offset in stmts:
        if not stmt.strip():
            continue

        def ln(local_pos: int) -> int:
            return base_line - 1 + _line_at(masked, offset + local_pos)

        # 1. DROP TABLE
        for m in re.finditer(r"\bDROP\s+TABLE\b", stmt, re.I):
            risks.append(_risk(
                rel_file, ln(m.start()), "high", "drop_table", "DROP TABLE",
                "Drops table permanently (data loss, not rollback-safe during rolling deploy).",
                "Stop reading/writing the table in app code first, archive/back up, then drop in a later deploy.",
            ))

        # 1b. DROP SCHEMA (… CASCADE drops every table/view in it)
        for m in re.finditer(r"\bDROP\s+SCHEMA\b", stmt, re.I):
            cascade = bool(re.search(r"\bCASCADE\b", stmt, re.I))
            risks.append(_risk(
                rel_file, ln(m.start()), "high", "drop_schema",
                "DROP SCHEMA CASCADE" if cascade else "DROP SCHEMA",
                "Drops the schema" + (" and every object in it (CASCADE) — large, irreversible data loss." if cascade
                                      else " permanently."),
                "Confirm the schema is unused, back it up, and drop it in a dedicated maintenance step.",
            ))

        # 2. TRUNCATE
        for m in re.finditer(r"\bTRUNCATE\b(?:\s+TABLE\b)?", stmt, re.I):
            risks.append(_risk(
                rel_file, ln(m.start()), "high", "truncate", "TRUNCATE",
                "Truncates table data irreversibly and acquires ACCESS EXCLUSIVE lock.",
                "Delete in small batches or archive rows instead of TRUNCATE in a live migration.",
            ))

        # 3. CREATE [UNIQUE] INDEX without CONCURRENTLY — unless it is on a table created in this same file.
        for m in re.finditer(
            rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY\b)(?:\s+(?:IF\s+NOT\s+EXISTS\s+)?{_TBL_REF})?"
            rf"\s+ON\s+(?:ONLY\s+)?({_TBL_REF})",
            stmt, re.I,
        ):
            if _norm_ident(m.group(1)) in created_tables:
                continue
            risks.append(_risk(
                rel_file, ln(m.start()), "high", "create_index_no_concurrently",
                "CREATE INDEX without CONCURRENTLY",
                "Blocks writes on the table while building the index.",
                "Use CREATE INDEX CONCURRENTLY (outside a transaction block).",
            ))
        # CREATE INDEX whose target table we could not parse (no ON clause captured): still flag conservatively.
        for m in re.finditer(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY\b)(?![^;]*\bON\b)", stmt, re.I):
            risks.append(_risk(
                rel_file, ln(m.start()), "high", "create_index_no_concurrently",
                "CREATE INDEX without CONCURRENTLY",
                "Blocks writes on the table while building the index.",
                "Use CREATE INDEX CONCURRENTLY (outside a transaction block).",
            ))

        # 4. ALTER TABLE operations
        if re.match(r"^\s*ALTER\s+TABLE\b", stmt, re.I):
            for clause, c_off in _split_top_level_commas(stmt, offset):
                def cln(pos: int, base: int = c_off) -> int:
                    return base_line - 1 + _line_at(masked, base + pos)

                # DROP COLUMN (or ALTER TABLE ... DROP <col>, excluding DROP CONSTRAINT/DEFAULT/NOT NULL/etc.)
                m_drop = re.search(
                    rf"(?:^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_TBL_REF}\s+|^\s*)DROP\s+"
                    r"(?:COLUMN\s+(?:IF\s+EXISTS\s+)?|(?:IF\s+EXISTS\s+)?(?!CONSTRAINT\b|INDEX\b|PRIMARY\b|FOREIGN\b|"
                    r"CHECK\b|DEFAULT\b|NOT\b|EXPRESSION\b|IDENTITY\b|POLICY\b|RULE\b|TRIGGER\b|PARTITION\b)[\w\"`]+)",
                    clause,
                    re.I,
                )
                if m_drop:
                    risks.append(_risk(
                        rel_file, cln(m_drop.start()), "high", "drop_column", "DROP COLUMN",
                        "Drops column permanently; older running app code still selecting it will fail.",
                        "Stop referencing the column in code (expand/contract), deploy, then drop in a follow-up migration.",
                    ))

                ident = r'(?:"[^"]+"|`[^`]+`|\[\w+\]|\w+)'
                # ADD COLUMN ... NOT NULL — unsafe without a DEFAULT, or with a VOLATILE default (still rewrites).
                m_add = re.search(rf"\bADD\s+(?:COLUMN\s+)?(?!CONSTRAINT\b|PRIMARY\b|UNIQUE\b|FOREIGN\b|CHECK\b|INDEX\b){ident}", clause, re.I)
                if m_add and re.search(r"\bNOT\s+NULL\b", clause, re.I):
                    has_default = bool(re.search(r"\bDEFAULT\b", clause, re.I))
                    if not has_default:
                        risks.append(_risk(
                            rel_file, cln(m_add.start()), "high", "add_not_null_no_default",
                            "ADD COLUMN ... NOT NULL without DEFAULT",
                            "Fails on non-empty tables (or requires full-table rewrite/lock on older DB engines).",
                            "Add the column nullable, backfill in batches, add a DEFAULT or NOT VALID check, then set NOT NULL.",
                        ))
                    elif _VOLATILE_DEFAULT_RE.search(clause):
                        risks.append(_risk(
                            rel_file, cln(m_add.start()), "high", "add_not_null_volatile_default",
                            "ADD COLUMN ... NOT NULL DEFAULT <volatile>",
                            "A volatile DEFAULT (e.g. gen_random_uuid()/now()) forces a full-table rewrite under lock.",
                            "Add the column nullable, backfill the value in batches, then set DEFAULT and NOT NULL.",
                        ))

                # ALTER COLUMN ... TYPE / MODIFY COLUMN
                m_type = re.search(rf"\bALTER\s+(?:COLUMN\s+)?{ident}\s+(?:SET\s+DATA\s+)?TYPE\b", clause, re.I)
                if m_type:
                    risks.append(_risk(
                        rel_file, cln(m_type.start()), "high", "alter_column_type",
                        "ALTER COLUMN ... TYPE",
                        "Rewrites the entire table under an ACCESS EXCLUSIVE lock and breaks old code expecting prior type.",
                        "Create a new column, dual-write/backfill, switch reads, then drop the old column.",
                    ))
                else:
                    m_mod = re.search(rf"\bMODIFY\s+(?:COLUMN\s+)?{ident}", clause, re.I)
                    if m_mod:
                        risks.append(_risk(
                            rel_file, cln(m_mod.start()), "medium", "alter_column_type",
                            "MODIFY COLUMN",
                            "Changing column definition may rewrite the table and lock writes.",
                            "Verify online DDL or use a dual-column expand/contract migration.",
                        ))

                # RENAME COLUMN / RENAME [COLUMN] <col> TO / RENAME TO (COLUMN keyword is optional in Postgres/MySQL)
                m_ren = re.search(rf"\bRENAME\s+(?:(?:COLUMN\s+)?{ident}\s+TO\b|TO\b)", clause, re.I)
                if m_ren:
                    op_name = "RENAME TABLE" if re.search(r"(?i)\bRENAME\s+TO\b", m_ren.group(0)) else "RENAME COLUMN"
                    risks.append(_risk(
                        rel_file, cln(m_ren.start()), "high", "rename", op_name,
                        f"{op_name} breaks running application instances immediately during rolling deploys.",
                        "Add the new column/table or view, dual-write, deploy new code, then drop the old name.",
                    ))

                # ALTER COLUMN ... SET NOT NULL
                m_snn = re.search(rf"\bALTER\s+(?:COLUMN\s+)?{ident}\s+SET\s+NOT\s+NULL\b", clause, re.I)
                if m_snn:
                    risks.append(_risk(
                        rel_file, cln(m_snn.start()), "medium", "set_not_null",
                        "ALTER COLUMN ... SET NOT NULL",
                        "Scans the entire table under an ACCESS EXCLUSIVE lock to verify no NULLs.",
                        "In PostgreSQL 12+, add CHECK (col IS NOT NULL) NOT VALID, VALIDATE CONSTRAINT, then SET NOT NULL.",
                    ))

    return risks


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return f"*.{node.func.attr}"
    return ""


def _kw_map(node: ast.Call) -> Dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in node.keywords if kw.arg}


def _is_const_bool(node: Optional[ast.AST], expected: bool) -> bool:
    return isinstance(node, ast.Constant) and node.value is expected


def _is_const_none(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _extract_str_literals(node: Optional[ast.AST]) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    if node is None:
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append((node.value, node.lineno))
    elif isinstance(node, ast.Call):
        cname = _call_name(node)
        if cname.endswith(".text") or cname == "text":
            if node.args:
                out.extend(_extract_str_literals(node.args[0]))
    elif isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            out.extend(_extract_str_literals(elt))
    return out


def _analyze_python_migration(src: str, rel_file: str) -> List[Dict[str, Any]]:
    """Analyze Alembic and Django Python migration files via AST (with downgrade() skipped)."""
    try:
        tree = ast.parse(src, rel_file)
    except SyntaxError:
        return []

    risks: List[Dict[str, Any]] = []
    # Track the Django Migration atomic flag and the Alembic batch-operation variables (`with ... as batch_op:`).
    django_atomic_false = False
    batch_vars: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == "atomic" and _is_const_bool(stmt.value, False):
                            django_atomic_false = True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ctx = item.context_expr
                if (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute)
                        and ctx.func.attr == "batch_alter_table" and isinstance(item.optional_vars, ast.Name)):
                    batch_vars.add(item.optional_vars.id)

    def _col_arg(node: ast.Call, kws: Dict[str, ast.AST]) -> Optional[ast.AST]:
        col = kws.get("column")
        if col is not None:
            return col
        for a in node.args:  # first sa.Column(...) positional — works for both op.add_column and batch_op.add_column
            if isinstance(a, ast.Call):
                return a
        return None

    def visit_node(node: ast.AST) -> None:
        # Skip Alembic downgrade() so normal rollbacks (op.drop_column/op.drop_table) aren't flagged
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "downgrade":
            return

        if isinstance(node, ast.Call):
            cname = _call_name(node)
            kws = _kw_map(node)

            # ── Alembic op.* and batch_op.* (method matched on any op/batch receiver) ──
            recv, _dot, method = cname.rpartition(".")
            is_alembic_op = bool(method) and (recv == "op" or recv in batch_vars)
            op_label = f"{recv}.{method}" if is_alembic_op else cname

            if is_alembic_op and method == "drop_table":
                risks.append(_risk(
                    rel_file, node.lineno, "high", "drop_table", op_label,
                    "Drops table permanently (data loss, not rollback-safe during rolling deploy).",
                    "Stop reading/writing the table in app code first, back up, then drop in a later deploy.",
                ))
            elif is_alembic_op and method == "drop_column":
                risks.append(_risk(
                    rel_file, node.lineno, "high", "drop_column", op_label,
                    "Drops column permanently; older running app code still selecting it will fail.",
                    "Ignore/stop referencing the column in app code first, deploy, then drop in a later migration.",
                ))
            elif is_alembic_op and method == "add_column":
                col_node = _col_arg(node, kws)
                if isinstance(col_node, ast.Call):
                    ckws = _kw_map(col_node)
                    nullable_false = _is_const_bool(ckws.get("nullable"), False)
                    has_server_default = "server_default" in ckws and not _is_const_none(ckws["server_default"])
                    is_pk = _is_const_bool(ckws.get("primary_key"), True)
                    if nullable_false and not has_server_default and not is_pk:
                        risks.append(_risk(
                            rel_file, col_node.lineno, "high", "add_not_null_no_default",
                            f"{op_label}(..., nullable=False) without server_default",
                            "Fails on non-empty tables (SQLAlchemy `default=` is ORM-only and sets no DB DEFAULT).",
                            "Set server_default=... or add with nullable=True, backfill, then alter to NOT NULL.",
                        ))
            elif is_alembic_op and method == "create_index":
                if not _is_const_bool(kws.get("postgresql_concurrently"), True):
                    risks.append(_risk(
                        rel_file, node.lineno, "high", "create_index_no_concurrently",
                        f"{op_label} without postgresql_concurrently=True",
                        "Blocks table writes during index creation in PostgreSQL.",
                        "Pass postgresql_concurrently=True inside `with op.get_context().autocommit_block():`.",
                    ))
            elif is_alembic_op and method == "alter_column":
                if "new_column_name" in kws and not _is_const_none(kws["new_column_name"]):
                    risks.append(_risk(
                        rel_file, node.lineno, "high", "rename", f"{op_label}(..., new_column_name=...)",
                        "Renames column in place, breaking older app code during rolling deploy.",
                        "Add the new column, dual-write/backfill, deploy new reads, then drop the old column.",
                    ))
                if "type_" in kws and not _is_const_none(kws["type_"]):
                    risks.append(_risk(
                        rel_file, node.lineno, "high", "alter_column_type", f"{op_label}(..., type_=...)",
                        "Changing column type may rewrite the table under ACCESS EXCLUSIVE lock.",
                        "Add a new column with the target type, backfill, switch reads, then drop the old column.",
                    ))
                if _is_const_bool(kws.get("nullable"), False):
                    risks.append(_risk(
                        rel_file, node.lineno, "medium", "set_not_null", f"{op_label}(..., nullable=False)",
                        "Setting NOT NULL scans the table under an ACCESS EXCLUSIVE lock.",
                        "Add a NOT VALID CHECK (col IS NOT NULL) constraint, validate it, then set NOT NULL.",
                    ))
            elif is_alembic_op and method == "rename_table":
                risks.append(_risk(
                    rel_file, node.lineno, "high", "rename", op_label,
                    "Renames table in place, breaking older app instances during rolling deploy.",
                    "Create a new table/view or dual-write before removing the old table.",
                ))
            elif is_alembic_op and method == "execute":
                sql_node = kws.get("sqltext") or (node.args[0] if node.args else None)
                for sql_str, s_line in _extract_str_literals(sql_node):
                    risks.extend(_analyze_sql_text(sql_str, rel_file, base_line=s_line))

            # ── Django migrations.* ──
            elif cname in ("migrations.DeleteModel", "DeleteModel"):
                risks.append(_risk(
                    rel_file, node.lineno, "high", "drop_table", "migrations.DeleteModel",
                    "Drops model table permanently (data loss, not rollback-safe).",
                    "Stop referencing the model in app code first, archive/back up, then delete in a later deploy.",
                ))
            elif cname in ("migrations.RemoveField", "RemoveField"):
                risks.append(_risk(
                    rel_file, node.lineno, "high", "drop_column", "migrations.RemoveField",
                    "Drops column permanently; older running code still selecting it will fail.",
                    "Deploy code ignoring the field first (e.g. SeparateDatabaseAndState), then drop in a later deploy.",
                ))
            elif cname in ("migrations.AddField", "AddField"):
                field_node = kws.get("field") or (node.args[2] if len(node.args) > 2 else None)
                if isinstance(field_node, ast.Call):
                    fname = _call_name(field_node)
                    if not fname.endswith("ManyToManyField"):
                        fkws = _kw_map(field_node)
                        nullable = _is_const_bool(fkws.get("null"), True)
                        has_default = ("default" in fkws and not _is_const_none(fkws["default"])) or (
                            "db_default" in fkws and not _is_const_none(fkws["db_default"])
                        )
                        if not nullable and not has_default:
                            risks.append(_risk(
                                rel_file, field_node.lineno, "high", "add_not_null_no_default",
                                "migrations.AddField NOT NULL without default",
                                "Fails on existing rows when adding a non-nullable column without default or null=True.",
                                "Add null=True (or db_default/default), backfill existing rows, then tighten to null=False.",
                            ))
            elif cname in ("migrations.AddIndex", "AddIndex"):
                risks.append(_risk(
                    rel_file, node.lineno, "high", "create_index_no_concurrently",
                    "migrations.AddIndex (non-concurrent)",
                    "Creates index while locking table writes.",
                    "Use AddIndexConcurrently from django.contrib.postgres.operations with atomic = False.",
                ))
            elif cname.endswith("AddIndexConcurrently"):
                if not django_atomic_false:
                    risks.append(_risk(
                        rel_file, node.lineno, "high", "create_index_no_concurrently",
                        "AddIndexConcurrently without atomic = False",
                        "AddIndexConcurrently fails inside a transaction unless `atomic = False` is set on Migration.",
                        "Set `atomic = False` on the Migration class.",
                    ))
            elif cname in ("migrations.AlterField", "AlterField"):
                risks.append(_risk(
                    rel_file, node.lineno, "medium", "alter_column_type", "migrations.AlterField",
                    "Altering field type/constraints may rewrite the table and lock writes.",
                    "Verify generated SQL (`sqlmigrate`); use expand/contract for column type changes.",
                ))
            elif cname in ("migrations.RenameField", "RenameField", "migrations.RenameModel", "RenameModel"):
                op_label = "migrations.RenameField" if "Field" in cname else "migrations.RenameModel"
                risks.append(_risk(
                    rel_file, node.lineno, "high", "rename", op_label,
                    f"{op_label} breaks running app instances during rolling deploy.",
                    "Add the new field/model, dual-write and backfill, switch reads, then remove the old one.",
                ))
            elif cname in ("migrations.RunSQL", "RunSQL"):
                sql_node = kws.get("sql") or (node.args[0] if node.args else None)
                for sql_str, s_line in _extract_str_literals(sql_node):
                    risks.extend(_analyze_sql_text(sql_str, rel_file, base_line=s_line))

        for child in ast.iter_child_nodes(node):
            visit_node(child)

    visit_node(tree)
    return risks


def _is_down_migration(rel_path: str) -> bool:
    """A rollback migration (golang-migrate `*.down.sql`, dbmate/others): its DROPs are expected, not risks."""
    base = os.path.basename(_slash(rel_path)).lower()
    return base.endswith(".down.sql") or base.endswith(".rollback.sql") or ".down." in base


def _is_migration_file(rel_path: str, abs_path: Optional[str] = None) -> bool:
    norm = _slash(rel_path)
    low = norm.lower()
    base = os.path.basename(low)
    parts = [p for p in low.split("/") if p]
    if _is_down_migration(low):
        return False
    if low.endswith(".sql"):
        if any(seg in ("migrations", "migration", "alembic", "prisma", "db", "flyway", "liquibase") for seg in parts[:-1]):
            return True
        if "migration" in base:
            return True
        return False
    if low.endswith(".py") and base != "__init__.py":
        if "versions" in parts[:-1] or "migrations" in parts[:-1]:
            return True
        if abs_path and os.path.isfile(abs_path):
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    head = f.read(4096)
                if "from alembic import" in head or "from django.db import migrations" in head:
                    return True
            except OSError:
                pass
    return False


def analyze_migrations(ws: str, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Analyze workspace migrations (or a specific list of files) for high-risk schema operations."""
    ws_real = os.path.realpath(ws)
    targets: List[Tuple[str, str]] = []

    if files is not None:
        seen = set()
        for item in files:
            if not item:
                continue
            abs_p = os.path.realpath(item if os.path.isabs(item) else os.path.join(ws_real, item))
            if os.path.isdir(abs_p):
                for sub_abs, _sub_rel in _walk_shallow(abs_p, max_depth=8):
                    if sub_abs in seen:
                        continue
                    rel_p = _rel(sub_abs, ws_real)
                    if _is_migration_file(rel_p, sub_abs):
                        seen.add(sub_abs)
                        targets.append((sub_abs, rel_p))
                continue
            if not os.path.isfile(abs_p) or abs_p in seen:
                continue
            seen.add(abs_p)
            rel_p = _rel(abs_p, ws_real)
            # When explicit files are passed (e.g. from deploy.py), analyze all .sql files and migration .py files
            if _is_down_migration(rel_p):
                continue  # rollback scripts are expected to drop/revert; never flag them
            if rel_p.lower().endswith(".sql") or _is_migration_file(rel_p, abs_p):
                targets.append((abs_p, rel_p))
    else:
        for abs_p, rel_p in _walk_shallow(ws_real, max_depth=8):
            if _is_migration_file(rel_p, abs_p):
                targets.append((abs_p, rel_p))

    risks: List[Dict[str, Any]] = []
    for abs_p, rel_p in sorted(targets, key=lambda pair: pair[1]):
        try:
            if os.path.getsize(abs_p) > 2_000_000:
                continue
            with open(abs_p, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        if rel_p.lower().endswith(".sql"):
            risks.extend(_analyze_sql_text(src, rel_p))
        elif rel_p.lower().endswith(".py"):
            risks.extend(_analyze_python_migration(src, rel_p))

    risks.sort(key=lambda r: (SEV_RANK.get(r["severity"], 9), r["file"], r["line"]))
    return risks


def _files_since_ref(ws: str, ref: str) -> Tuple[Optional[List[str]], str]:
    # A ref beginning with '-' would otherwise be read by git as an option (e.g. --output=FILE): refuse it.
    if ref.startswith("-"):
        return None, f"Invalid git ref (must not start with '-'): {ref}"
    c1, o1, e1 = _run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=ws)
    if c1 != 0 or o1.strip() != "true":
        return None, f"Not a git repository: {ws}"
    # `--end-of-options` stops git from treating the ref as a flag even if the guard above is bypassed.
    c2, o2, e2 = _run_cmd(
        ["git", "-c", "core.quotePath=false", "diff", "--name-only", "--relative", "--end-of-options", ref], cwd=ws
    )
    if c2 != 0:
        return None, (e2 or o2 or f"Invalid git ref: {ref}").strip()
    c3, o3, _e3 = _run_cmd(["git", "ls-files", "--others", "--exclude-standard"], cwd=ws)
    paths = [ln.strip() for ln in (o2.splitlines() + (o3.splitlines() if c3 == 0 else [])) if ln.strip()]
    return sorted(set(paths)), ""


def _run_migrations(ws: str, since: Optional[str]) -> Tuple[int, Dict[str, Any], str]:
    ws_real = os.path.realpath(ws)
    explicit_files: Optional[List[str]] = None
    scanned_files: List[str] = []

    if since:
        changed, err = _files_since_ref(ws_real, since)
        if changed is None:
            return 2, {}, err
        explicit_files = changed
        for item in changed:
            abs_p = os.path.realpath(os.path.join(ws_real, item))
            if _is_down_migration(item):
                continue
            if os.path.isfile(abs_p) and (item.lower().endswith(".sql") or _is_migration_file(item, abs_p)):
                scanned_files.append(_slash(item))
    else:
        scanned_files = [
            rel_p for abs_p, rel_p in _walk_shallow(ws_real, max_depth=8)
            if _is_migration_file(rel_p, abs_p)
        ]

    risks = analyze_migrations(ws_real, explicit_files)
    high = sum(1 for r in risks if r["severity"] == "high")
    medium = sum(1 for r in risks if r["severity"] == "medium")
    report = {
        "workspace": ws_real,
        "since": since or "",
        "files_scanned": len(scanned_files),
        "files": scanned_files,
        "risks": risks,
        "summary": {"total": len(risks), "high": high, "medium": medium},
    }
    return (1 if risks else 0), report, ""


def render_migrations(report: Dict[str, Any]) -> str:
    s = report["summary"]
    since_note = f" (since {report['since']})" if report.get("since") else ""
    lines = [f"# Migration risk analysis — {report['workspace']}{since_note}"]
    if report["files_scanned"] == 0 and s["total"] == 0:
        lines.append("No migration files found.")
        return "\n".join(lines)
    if s["total"] == 0:
        lines.append(f"Scanned {report['files_scanned']} migration file(s); no high-risk operations found.")
        return "\n".join(lines)

    lines.append(f"Scanned {report['files_scanned']} migration file(s); found {s['total']} risk(s) ({s['high']} high, {s['medium']} medium).")
    lines.append("")
    for r in report["risks"]:
        lines.append(f"- [{r['severity']}] {r['file']}:{r['line']} — {r['operation']}: {r['message']}")
        lines.append(f"  Safer: {r['alternative']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────── CLI ───────────────────────────────────────────────

def _emit_error(msg: str, as_json: bool) -> int:
    safe_msg = mask_secrets(msg)
    if as_json:
        print(json.dumps({"error": safe_msg}, indent=1, ensure_ascii=False))
    else:
        print(f"db.py: {safe_msg}", file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", "--path", dest="workspace", default=os.getcwd(), help="project directory")
    common.add_argument("--json", action="store_true", help="JSON output")

    ap = argparse.ArgumentParser(
        prog="db.py",
        description="Safe read-only database inspection, EXPLAIN, and migration risk analysis.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", parents=[common], help="locate SQLite files, compose DBs, and .env* DB vars (masked)")

    sp_schema = sub.add_parser("schema", parents=[common], help="inspect read-only schema, indexes, and row estimates")
    sp_schema.add_argument("--target", default=None, help="SQLite path, env var name, or compose service")
    sp_schema.add_argument("--table", default=None, help="filter to a specific table")

    sp_explain = sub.add_parser("explain", parents=[common], help="run EXPLAIN on a strictly read-only SQL query")
    sp_explain.add_argument("sql", metavar="SQL", help="read-only SQL query (SELECT / WITH ... SELECT / VALUES)")
    sp_explain.add_argument("--target", default=None, help="SQLite path, env var name, or compose service")
    sp_explain.add_argument("--analyze", action="store_true", help="include EXPLAIN ANALYZE on PostgreSQL (inside ROLLBACK)")

    sp_mig = sub.add_parser("migrations", parents=[common], help="scan migrations for high-risk operations")
    sp_mig.add_argument("--since", default=None, help="analyze only migration files changed since git REF")

    args = ap.parse_args(argv)
    ws = os.path.realpath(os.path.expanduser(args.workspace))
    if not os.path.isdir(ws):
        return _emit_error(f"no such workspace directory: {ws}", args.json)

    if args.cmd == "detect":
        det = detect_workspace(ws)
        if args.json:
            print(json.dumps(_public_detect(det), indent=1, ensure_ascii=False))
        else:
            print(render_detect(det))
        return 0

    if args.cmd == "schema":
        target, err = resolve_target(ws, args.target)
        if not target:
            return _emit_error(err, args.json)
        if target["kind"] == "sqlite":
            code, data, qerr = _sqlite_schema(target, args.table)
        elif target["kind"] == "postgres":
            code, data, qerr = _postgres_schema(target, args.table, ws)
        else:
            code, data, qerr = _redis_schema(target, ws)
        if code != 0:
            return _emit_error(qerr, args.json)
        if args.json:
            print(json.dumps(data, indent=1, ensure_ascii=False))
        else:
            print(render_schema(data))
        return 0

    if args.cmd == "explain":
        code, data, eerr = _run_explain(ws, args.target, args.sql, args.analyze)
        if code != 0:
            return _emit_error(eerr, args.json)
        if args.json:
            print(json.dumps(data, indent=1, ensure_ascii=False))
        else:
            print(render_explain(data))
        return 0

    if args.cmd == "migrations":
        code, report, merr = _run_migrations(ws, args.since)
        if code == 2:
            return _emit_error(merr, args.json)
        if args.json:
            print(json.dumps(report, indent=1, ensure_ascii=False))
        else:
            print(render_migrations(report))
        return code

    return 2


if __name__ == "__main__":
    sys.exit(main())
