#!/usr/bin/env python3
r"""Antigravity Plus — /env: safe .env parity, non-hanging Docker probe, dev ports & runtime checks.

  env_check.py [check]  [--workspace DIR] [--ports 3000,8000] [--json]
  env_check.py dotenv   [--workspace DIR] [--json]
  env_check.py docker   [--workspace DIR] [--json]
  env_check.py ports    [--ports 3000,8000] [--json]

Reads .env / .env.local / .env.example / .env.sample / .env.template in Python and reports ONLY variable names and
statuses — never prints secret values. Probes Docker daemon sockets with a strict 2.0s timeout before invoking the
docker CLI so hung daemons never stall the terminal. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # type: ignore[import-not-found]  # noqa: E402

ACTUAL_ENV_FILES = (".env", ".env.local")
EXAMPLE_ENV_FILES = (".env.example", ".env.sample", ".env.template")

CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".sh")
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", ".tox", ".nox", ".direnv", "site-packages", "__pypackages__",
    "dist", "build", "__pycache__", ".next", ".nuxt", ".turbo", ".cache", "target", "vendor", "coverage",
    "testdata", "fixtures", "__fixtures__", "__mocks__",
}
MAX_CODE_BYTES = 1_000_000

DEFAULT_PORTS = [3000, 5173, 8000, 8080, 5432, 6379]
PORT_HINTS: Dict[int, str] = {
    3000: "Node/Next/React",
    5173: "Vite",
    8000: "Python/FastAPI/Django",
    8080: "HTTP/API",
    5432: "PostgreSQL",
    6379: "Redis",
}

DOCKER_PING_TIMEOUT = 2.0
DOCKER_INFO_TIMEOUT = 3.0
DOCKER_PS_TIMEOUT = 4.0
PORT_PROBE_TIMEOUT = 0.2

BUILTIN_ENV_VARS: Set[str] = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "PWD", "OLDPWD",
    "TMPDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "SYSTEMROOT", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "COMSPEC",
    "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING", "VIRTUAL_ENV", "CONDA_PREFIX",
    "NODE_ENV", "CI", "NO_COLOR", "FORCE_COLOR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "MODE", "DEV", "PROD", "SSR", "BASE_URL",
}
SHELL_BUILTINS: Set[str] = {
    "IFS", "OPTIND", "OPTARG", "LINENO", "RANDOM", "SECONDS", "UID", "EUID",
    "BASH_SOURCE", "FUNCNAME", "SHLVL", "PIPESTATUS", "REPLY", "PPID", "GROUPS",
}

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# An inline comment starts at a '#' preceded by whitespace; `KEY=#ff0000` (no space) is a literal value, not a comment.
_INLINE_COMMENT_RE = re.compile(r"\s#")


def _is_env_dir(path: str) -> bool:
    """A virtualenv / conda env directory (however it is named): skip its library sources, not the project's."""
    return os.path.isfile(os.path.join(path, "pyvenv.cfg")) or os.path.isdir(os.path.join(path, "conda-meta"))

_PLACEHOLDER_WHOLE_RE = re.compile(
    r"^(?:changeme|change[-_]me|replace[-_]me|todo|tbd|fixme|placeholder|dummy|fake|redacted|"
    r"your[_-][a-z0-9_-]*|example|sample|xxx+|\*{3,}|\.{3,}|<[^>]*>|\[[^\]]+\])$",
    re.I,
)
_PLACEHOLDER_PART_RE = re.compile(
    r"(?i)(?:changeme|change[-_]me|replace[-_]me|placeholder|<[a-z0-9_.\s-]+>|"
    r"your[_-](?:api[_-]?key|secret|token|password|pass|key|url|dsn|host|user|id)|"
    r"(?:key|token|secret|password|value|url|id|insert|put|enter)[_-]here$|x{4,}|\*{4,})"
)

_PY_PATTERNS = (
    re.compile(r"""\bos\.environ\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""\bos\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
)
_JS_PATTERNS = (
    re.compile(r"""\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)\b"""),
    re.compile(r"""\bprocess\.env\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""\bimport\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)\b"""),
    re.compile(r"""\bimport\.meta\.env\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
)
_GO_PATTERNS = (
    re.compile(r"""\bos\.(?:Getenv|LookupEnv)\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)"""),
)
_RB_PATTERNS = (
    re.compile(r"""\bENV(?:\.fetch\s*\(|\s*\[)\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
)
_SH_PATTERNS = (
    re.compile(r"""\$\{([A-Z][A-Z0-9_]{2,})(?:[:?=+-][^}]*)?\}"""),
    re.compile(r"""(?<!\\)\$([A-Z][A-Z0-9_]{2,})\b"""),
)
_SH_LOCAL_ASSIGN_RE = re.compile(r"""^\s*(?:local\s+|readonly\s+)?([A-Z][A-Z0-9_]+)=(.*)$""")

_PY_MANIFESTS = (
    "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "Pipfile", "uv.lock", "poetry.lock",
)
_PY_REQ_RE = re.compile(r"^requirements[\w.-]*\.txt$", re.I)


# ─────────────────────────────── dotenv ───────────────────────────────

def _classify_value(val: str) -> str:
    """Return 'empty', 'placeholder', or 'set' without retaining the raw value."""
    s = val.strip()
    if not s:
        return "empty"
    if _PLACEHOLDER_WHOLE_RE.match(s) or _PLACEHOLDER_PART_RE.search(s):
        return "placeholder"
    return "set"


def _consume_quoted(lines: List[str], idx: int, rest: str) -> Tuple[str, int]:
    """Read a single- or double-quoted value starting at rest[0], supporting multi-line values."""
    quote = rest[0]
    buf: List[str] = []
    cur = rest[1:]
    escaped = False
    while True:
        for ch in cur:
            if quote == '"':
                if escaped:
                    buf.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(ch, ch))
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
            if ch == quote:
                return "".join(buf), idx
            buf.append(ch)
        if idx + 1 >= len(lines):
            return "".join(buf), idx
        buf.append("\n")
        idx += 1
        cur = lines[idx]


def parse_env_file(path: str) -> Dict[str, str]:
    """Parse a .env* file into {KEY: status}, discarding all raw secret values immediately."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    out: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        m = _ENV_LINE_RE.match(raw)
        if not m:
            i += 1
            continue
        key = m.group(1)
        raw_val = m.group(2)
        rest = raw_val.lstrip()
        if not rest:
            val = ""
        elif rest[0] in ('"', "'"):
            val, i = _consume_quoted(lines, i, rest)
        else:
            # Search the un-lstripped value so a '#' right after '=' stays literal while ' #…' is a comment.
            cm = _INLINE_COMMENT_RE.search(raw_val)
            val = (raw_val[:cm.start()] if cm else raw_val).strip()
        out[key] = _classify_value(val)
        i += 1
    return out


def _patterns_for_ext(ext: str) -> Tuple["re.Pattern[str]", ...]:
    if ext == ".py":
        return _PY_PATTERNS
    if ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        return _JS_PATTERNS
    if ext == ".go":
        return _GO_PATTERNS
    if ext == ".rb":
        return _RB_PATTERNS
    if ext == ".sh":
        return _SH_PATTERNS
    return ()


def _sh_local_vars(lines: List[str]) -> Set[str]:
    assigned: Set[str] = set()
    for line in lines:
        m = _SH_LOCAL_ASSIGN_RE.match(line)
        if not m:
            continue
        name, rhs = m.group(1), m.group(2).strip()
        if not re.match(rf"""^['"]?\$\{{?{re.escape(name)}\b""", rhs):
            assigned.add(name)
    return assigned


def scan_code_env_refs(workspace: str) -> Tuple[Dict[str, List[str]], int]:
    """Scan source files for env var references -> ({VAR: ['rel/path:line', ...]}, files_scanned)."""
    refs: Dict[str, List[str]] = {}
    seen_locs: Dict[str, Set[str]] = {}
    files_scanned = 0

    for root, dirs, names in os.walk(workspace, topdown=True, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not _is_env_dir(os.path.join(root, d)))
        for name in sorted(names):
            _, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in CODE_EXTS:
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path) or os.path.islink(path):
                continue  # regular files only — a FIFO/device named like code would block read() forever
            try:
                if os.path.getsize(path) > MAX_CODE_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            files_scanned += 1
            rel = os.path.relpath(path, workspace).replace(os.sep, "/")
            patterns = _patterns_for_ext(ext)
            lines = text.splitlines()
            sh_locals = _sh_local_vars(lines) if ext == ".sh" else set()
            for lineno, line in enumerate(lines, 1):
                s = line.lstrip()
                if s.startswith("#") and ext in (".py", ".rb", ".sh"):
                    continue
                if s.startswith("//") and ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go"):
                    continue
                for rx in patterns:
                    for m in rx.finditer(line):
                        var = m.group(1)
                        if var.upper() in BUILTIN_ENV_VARS:
                            continue
                        if ext == ".sh" and (var in SHELL_BUILTINS or var in sh_locals):
                            continue
                        loc = f"{rel}:{lineno}"
                        locs = seen_locs.setdefault(var, set())
                        if loc not in locs:
                            locs.add(loc)
                            bucket = refs.setdefault(var, [])
                            if len(bucket) < 5:
                                bucket.append(loc)
    return refs, files_scanned


def check_dotenv(workspace: str) -> Dict[str, Any]:
    """Compare .env/.env.local vs .env.example/.env.sample/.env.template and code references."""
    env_files: List[str] = []
    effective_env: Dict[str, Tuple[str, str]] = {}
    for fname in ACTUAL_ENV_FILES:
        fpath = os.path.join(workspace, fname)
        if os.path.isfile(fpath):
            env_files.append(fname)
            for k, status in parse_env_file(fpath).items():
                effective_env[k] = (status, fname)

    example_files: List[str] = []
    example_keys: Dict[str, List[str]] = {}
    for fname in EXAMPLE_ENV_FILES:
        fpath = os.path.join(workspace, fname)
        if os.path.isfile(fpath):
            example_files.append(fname)
            for k in parse_env_file(fpath):
                example_keys.setdefault(k, []).append(fname)

    code_refs, code_files_scanned = scan_code_env_refs(workspace)

    expected_keys = sorted(set(example_keys) | set(code_refs))
    missing_in_env: List[Dict[str, Any]] = []
    for k in expected_keys:
        if k not in effective_env:
            sources: List[str] = list(example_keys.get(k, []))
            if k in code_refs:
                sources.extend(f"code ({loc})" for loc in code_refs[k][:2])
            missing_in_env.append({"key": k, "sources": sources})

    missing_in_example: List[Dict[str, Any]] = []
    for k in sorted(effective_env):
        if k not in example_keys:
            _, fname = effective_env[k]
            missing_in_example.append({"key": k, "sources": [fname]})

    empty_or_placeholder: List[Dict[str, str]] = []
    for k in sorted(effective_env):
        status, fname = effective_env[k]
        if status in ("empty", "placeholder"):
            empty_or_placeholder.append({"key": k, "file": fname, "issue": status})

    issues_count = len(missing_in_env) + len(missing_in_example) + len(empty_or_placeholder)
    return {
        "workspace": workspace,
        "env_files": env_files,
        "example_files": example_files,
        "code_files_scanned": code_files_scanned,
        "keys": {
            "env": sorted(effective_env),
            "example": sorted(example_keys),
            "code": sorted(code_refs),
        },
        "missing_in_env": missing_in_env,
        "missing_in_example": missing_in_example,
        "empty_or_placeholder": empty_or_placeholder,
        "status": "issues" if issues_count else "ok",
        "issues_count": issues_count,
    }


# ─────────────────────────────── docker ───────────────────────────────

def _docker_recovery() -> str:
    if compat.MAC:
        return (
            "Recovery (macOS): run `kill -9` on `com.docker.backend` "
            "(`pkill -9 -f com.docker.backend`), then restart with `open -g -a Docker`."
        )
    if compat.LINUX:
        return "Recovery (Linux): restart the Docker daemon (`sudo systemctl restart docker`)."
    return "Recovery (Windows): quit Docker Desktop from the tray/Task Manager and relaunch it."


def _active_docker_context_host() -> str:
    """Resolve the active docker context's endpoint host (Colima, rootless, remote…) without running `docker`.

    Honours DOCKER_CONTEXT, else ~/.docker/config.json's currentContext. Returns '' for the default context
    (or on any error) so the normal socket candidates are used.
    """
    ctx = os.environ.get("DOCKER_CONTEXT", "").strip()
    cfg_dir = os.path.join(os.path.expanduser("~"), ".docker")
    if not ctx:
        try:
            with open(os.path.join(cfg_dir, "config.json"), encoding="utf-8", errors="replace") as f:
                ctx = str(json.load(f).get("currentContext") or "").strip()
        except (OSError, ValueError):
            return ""
    if not ctx or ctx == "default":
        return ""
    # Context metadata lives in ~/.docker/contexts/meta/<sha256(name)>/meta.json; scan rather than hash it.
    meta_root = os.path.join(cfg_dir, "contexts", "meta")
    try:
        entries = os.listdir(meta_root)
    except OSError:
        return ""
    for entry in entries:
        meta_file = os.path.join(meta_root, entry, "meta.json")
        try:
            with open(meta_file, encoding="utf-8", errors="replace") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if str(meta.get("Name") or "") != ctx:
            continue
        host = ""
        endpoints = meta.get("Endpoints")
        if isinstance(endpoints, dict) and isinstance(endpoints.get("docker"), dict):
            host = str(endpoints["docker"].get("Host") or "").strip()
        return host
    return ""


def _docker_target() -> Tuple[str, str]:
    """Determine Docker endpoint: ('unix'|'tcp'|'windows_cli'|'cli'|'none', target)."""
    override = os.environ.get("AGP_DOCKER_SOCK", "").strip()
    if override:
        if override.startswith("unix://"):
            override = override[len("unix://"):]
        return "unix", override

    docker_host = os.environ.get("DOCKER_HOST", "").strip() or _active_docker_context_host()
    if docker_host:
        if docker_host.startswith("unix://"):
            return "unix", docker_host[len("unix://"):]
        if docker_host.startswith(("tcp://", "http://", "https://")):
            return "tcp", docker_host
        if docker_host.startswith("npipe://") and compat.WINDOWS:
            return "windows_cli", docker_host
        return "cli", docker_host

    if compat.WINDOWS:
        return "windows_cli", "npipe"

    candidates = [
        os.path.expanduser("~/.docker/run/docker.sock"),
        "/var/run/docker.sock",
    ]
    for sock_path in candidates:
        if os.path.exists(sock_path):
            return "unix", sock_path
    return "none", ""


def _http_ping_complete(data: bytes) -> bool:
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        return False
    head = data[:sep].decode("latin-1", errors="replace")
    body = data[sep + 4:]
    m = re.search(r"(?im)^Content-Length:\s*(\d+)\s*$", head)
    if m:
        return len(body) >= int(m.group(1))
    first = head.splitlines()[0] if head else ""
    return bool(" 200 " in first and body.strip() == b"OK")


def _parse_ping_response(data: bytes, elapsed_ms: float, target_label: str) -> Dict[str, Any]:
    sep = data.find(b"\r\n\r\n")
    head = (data[:sep] if sep >= 0 else data).decode("latin-1", errors="replace")
    first_line = head.splitlines()[0].strip() if head.splitlines() else ""
    if re.match(r"^HTTP/\d(?:\.\d)?\s+200\b", first_line):
        return {
            "status": "ok",
            "target": target_label,
            "ping_ok": True,
            "ping_ms": round(elapsed_ms, 1),
            "message": f"Docker daemon answered /_ping (200 OK in {elapsed_ms:.0f} ms).",
            "recovery": None,
        }
    return {
        "status": "error",
        "target": target_label,
        "ping_ok": False,
        "ping_ms": round(elapsed_ms, 1),
        "message": f"Docker /_ping returned unexpected response: {first_line or 'empty response'}",
        "recovery": _docker_recovery(),
    }


def _probe_stream_ping(
    connect_fn: Callable[[float], socket.socket],
    target_label: str,
    timeout: float = DOCKER_PING_TIMEOUT,
) -> Dict[str, Any]:
    t0 = time.monotonic()
    deadline = t0 + timeout
    sock: Optional[socket.socket] = None
    connected = False
    try:
        sock = connect_fn(timeout)
        connected = True
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise socket.timeout("connect exhausted timeout")
        sock.settimeout(rem)
        sock.sendall(b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
        buf = bytearray()
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise socket.timeout("recv timed out")
            sock.settimeout(rem)
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if _http_ping_complete(bytes(buf)):
                break
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return _parse_ping_response(bytes(buf), elapsed_ms, target_label)
    except (socket.timeout, TimeoutError):
        msg = (
            "Docker socket accepted connection but daemon did not answer /_ping within 2s — "
            "do not run docker commands or they will hang!"
            if connected
            else "Docker socket did not accept connection within 2s — do not run docker commands or they will hang!"
        )
        return {
            "status": "hung",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "message": msg,
            "recovery": _docker_recovery(),
        }
    except ConnectionRefusedError:
        return {
            "status": "not_running",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": f"Docker socket exists at {target_label} but no daemon is listening (connection refused).",
            "recovery": None,
        }
    except PermissionError:
        return {
            "status": "error",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": f"Permission denied connecting to Docker endpoint {target_label}.",
            "recovery": None,
        }
    except OSError as e:
        return {
            "status": "not_running",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": f"Cannot connect to Docker endpoint {target_label}: {e}",
            "recovery": None,
        }
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _probe_unix_socket(sock_path: str, timeout: float = DOCKER_PING_TIMEOUT) -> Dict[str, Any]:
    target_label = f"unix://{sock_path}"
    if not os.path.exists(sock_path):
        has_cli = bool(shutil.which("docker"))
        return {
            "status": "not_running" if has_cli else "not_installed",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": (
                f"Docker socket not found at {sock_path}."
                if has_cli
                else f"Docker is not installed and socket {sock_path} was not found."
            ),
            "recovery": None,
        }
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is None:
        return _probe_docker_info(target_label, timeout=DOCKER_INFO_TIMEOUT)

    def _connect(rem: float) -> socket.socket:
        s = socket.socket(af_unix, socket.SOCK_STREAM)
        try:
            s.settimeout(rem)
            s.connect(sock_path)
            return s
        except Exception:
            s.close()
            raise

    return _probe_stream_ping(_connect, target_label, timeout=timeout)


def _probe_tcp_endpoint(docker_host: str, timeout: float = DOCKER_PING_TIMEOUT) -> Dict[str, Any]:
    if os.environ.get("DOCKER_TLS_VERIFY", "").strip() not in ("", "0"):
        return _probe_docker_info(docker_host, timeout=DOCKER_INFO_TIMEOUT)
    parsed = urllib.parse.urlparse(docker_host)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 2375

    def _connect(rem: float) -> socket.socket:
        return socket.create_connection((host, port), timeout=rem)

    return _probe_stream_ping(_connect, docker_host, timeout=timeout)


def _probe_docker_info(target_label: str, timeout: float = DOCKER_INFO_TIMEOUT) -> Dict[str, Any]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return {
            "status": "not_installed",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": "Docker CLI is not installed.",
            "recovery": None,
        }
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "hung",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "message": "Docker daemon did not answer `docker info` within 3s — do not run docker commands or they will hang!",
            "recovery": _docker_recovery(),
        }
    except OSError as e:
        return {
            "status": "error",
            "target": target_label,
            "ping_ok": False,
            "ping_ms": None,
            "message": f"Failed to run docker info: {e}",
            "recovery": None,
        }
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if r.returncode == 0:
        return {
            "status": "ok",
            "target": target_label,
            "ping_ok": True,
            "ping_ms": round(elapsed_ms, 1),
            "message": f"Docker daemon responded to `docker info` ({elapsed_ms:.0f} ms).",
            "recovery": None,
        }
    err = (r.stderr or r.stdout or "").strip().splitlines()
    return {
        "status": "not_running",
        "target": target_label,
        "ping_ok": False,
        "ping_ms": None,
        "message": f"Docker daemon is not running ({err[0] if err else f'exit {r.returncode}'}).",
        "recovery": None,
    }


def _parse_containers_json(text: str) -> Optional[List[Dict[str, Any]]]:
    s = text.strip()
    if not s:
        return []
    rows: List[Dict[str, Any]] = []
    if s.startswith("["):
        try:
            loaded = json.loads(s)
        except ValueError:
            return None
        if not isinstance(loaded, list):
            return None
        items = loaded
    else:
        items = []
        for line in s.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                return None
    for item in items:
        if not isinstance(item, dict):
            continue
        state = str(item.get("State") or "").lower()
        status_text = str(item.get("Status") or "")
        rows.append({
            "id": str(item.get("ID") or "")[:12],
            "name": str(item.get("Names") or item.get("Name") or ""),
            "image": str(item.get("Image") or ""),
            "state": state,
            "status": status_text,
            "ports": str(item.get("Ports") or ""),
            "unhealthy": "unhealthy" in status_text.lower() or state == "dead",
        })
    return rows


def _parse_containers_tsv(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 6:
            parts.append("")
        cid, name, image, state, status_text, ports = parts[:6]
        state_l = state.strip().lower()
        rows.append({
            "id": cid.strip()[:12],
            "name": name.strip(),
            "image": image.strip(),
            "state": state_l,
            "status": status_text.strip(),
            "ports": ports.strip(),
            "unhealthy": "unhealthy" in status_text.lower() or state_l == "dead",
        })
    return rows


def _list_containers(target_kind: str, target: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return [], "docker CLI not installed (daemon /_ping succeeded)"
    env = dict(os.environ)
    if target_kind == "unix" and target and os.environ.get("AGP_DOCKER_SOCK"):
        env["DOCKER_HOST"] = f"unix://{target}"

    for fmt, parser in (
        ("json", _parse_containers_json),
        ("{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.Ports}}", _parse_containers_tsv),
    ):
        try:
            r = subprocess.run(
                [docker_bin, "ps", "-a", "--format", fmt],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCKER_PS_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return [], "docker ps timed out after 4s"
        except OSError as e:
            return [], f"failed to run docker ps: {e}"
        if r.returncode == 0:
            parsed = parser(r.stdout)
            if parsed is not None:
                return parsed, None
            if fmt == "json":
                continue  # old Docker CLI prints the literal word "json" per row → fall back to the TSV template
        elif fmt == "json":
            continue
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return [], err[0] if err else f"docker ps exited {r.returncode}"
    return [], "could not parse docker ps output"


def check_docker(timeout: float = DOCKER_PING_TIMEOUT) -> Dict[str, Any]:
    """Probe Docker daemon safely (2s timeout) and list containers only when healthy."""
    kind, target = _docker_target()
    if kind == "none":
        has_cli = bool(shutil.which("docker"))
        result: Dict[str, Any] = {
            "status": "not_running" if has_cli else "not_installed",
            "target": None,
            "ping_ok": False,
            "ping_ms": None,
            "message": (
                "No Docker socket found (~/.docker/run/docker.sock or /var/run/docker.sock); daemon is not running."
                if has_cli
                else "Docker CLI is not installed and no Docker socket was found."
            ),
            "recovery": None,
        }
    elif kind == "unix":
        result = _probe_unix_socket(target, timeout=timeout)
    elif kind == "tcp":
        result = _probe_tcp_endpoint(target, timeout=timeout)
    else:
        result = _probe_docker_info(target, timeout=DOCKER_INFO_TIMEOUT)

    if result["ping_ok"]:
        containers, ps_err = _list_containers(kind, target)
        result["containers"] = containers
        result["unhealthy_containers"] = [c for c in containers if c.get("unhealthy")]
        result["containers_error"] = ps_err
    else:
        result["containers"] = []
        result["unhealthy_containers"] = []
        result["containers_error"] = None
    return result


# ─────────────────────────────── ports ───────────────────────────────

def parse_ports_arg(raw: Optional[str]) -> List[int]:
    if not raw:
        return list(DEFAULT_PORTS)
    ports: List[int] = []
    seen: Set[int] = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValueError(f"invalid port: {item!r}")
        port = int(item)
        if not (1 <= port <= 65535):
            raise ValueError(f"port out of range (1-65535): {port}")
        if port not in seen:
            seen.add(port)
            ports.append(port)
    if not ports:
        raise ValueError("no valid ports specified")
    return ports


def _probe_port(port: int, timeout: float = PORT_PROBE_TIMEOUT) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            return "127.0.0.1"
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if getattr(socket, "has_ipv6", False):
        sock6: Optional[socket.socket] = None
        try:
            sock6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock6.settimeout(timeout)
            if sock6.connect_ex(("::1", port)) == 0:
                return "::1"
        except OSError:
            pass
        finally:
            if sock6 is not None:
                try:
                    sock6.close()
                except OSError:
                    pass
    return None


def _lookup_port_processes(ports: List[int]) -> Dict[int, Tuple[Optional[int], Optional[str]]]:
    """Best-effort PID/command lookup for listening TCP ports; never raises or fails."""
    found: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
    if not ports:
        return found
    target_set = set(ports)

    # Own process check via compat.listening_ports (fast, covers unit tests in same process)
    own_pid = os.getpid()
    try:
        for p in compat.listening_ports(own_pid):
            if p in target_set:
                found[p] = (own_pid, os.path.basename(sys.executable) or "python")
    except Exception:
        pass

    missing = [p for p in ports if p not in found]
    if not missing:
        return found

    if compat.WINDOWS:
        pid_by_port: Dict[int, int] = {}
        try:
            r = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4].isdigit():
                        _, _, port_s = parts[1].rpartition(":")
                        if port_s.isdigit():
                            pnum = int(port_s)
                            if pnum in target_set and pnum not in pid_by_port:
                                pid_by_port[pnum] = int(parts[4])
        except (OSError, subprocess.TimeoutExpired):
            pass
        if pid_by_port:
            names: Dict[int, str] = {}
            try:
                for pid, cmd in compat.processes(""):
                    names[pid] = os.path.basename(cmd.split()[0]) if cmd else ""
            except Exception:
                pass
            for pnum, pid in pid_by_port.items():
                found[pnum] = (pid, names.get(pid) or None)
        return found

    lsof_bin = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.isfile("/usr/sbin/lsof") else None)
    if lsof_bin:
        try:
            port_csv = ",".join(str(p) for p in missing)
            r = subprocess.run(
                [lsof_bin, "-nP", f"-iTCP:{port_csv}", "-sTCP:LISTEN", "-F", "pcn"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
            )
            cur_pid: Optional[int] = None
            cur_cmd: Optional[str] = None
            for line in r.stdout.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    cur_pid = int(line[1:])
                    cur_cmd = None
                elif line.startswith("c"):
                    cur_cmd = line[1:] or None
                elif line.startswith("n"):
                    _, _, port_s = line[1:].rpartition(":")
                    if port_s.isdigit():
                        pnum = int(port_s)
                        if pnum in target_set and pnum not in found:
                            found[pnum] = (cur_pid, cur_cmd)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if compat.LINUX and len(found) < len(ports):
        ss_bin = shutil.which("ss")
        if ss_bin:
            try:
                r = subprocess.run(
                    [ss_bin, "-ltnp"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                )
                for line in r.stdout.splitlines():
                    pm = re.search(r":(\d+)\s", line)
                    if not pm:
                        continue
                    pnum = int(pm.group(1))
                    if pnum not in target_set or pnum in found:
                        continue
                    proc_m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
                    if proc_m:
                        found[pnum] = (int(proc_m.group(2)), proc_m.group(1))
            except (OSError, subprocess.TimeoutExpired):
                pass
    return found


def check_ports(ports: List[int]) -> Dict[str, Any]:
    """Probe localhost TCP ports with 0.2s connect timeout and optional process info."""
    entries: List[Dict[str, Any]] = []
    listening_ports_list: List[int] = []
    for port in ports:
        host = _probe_port(port)
        if host:
            listening_ports_list.append(port)
        entries.append({
            "port": port,
            "listening": bool(host),
            "host": host or "127.0.0.1",
            "hint": PORT_HINTS.get(port),
            "pid": None,
            "process": None,
        })

    if listening_ports_list:
        proc_map = _lookup_port_processes(listening_ports_list)
        for item in entries:
            if item["listening"] and item["port"] in proc_map:
                pid, proc = proc_map[item["port"]]
                item["pid"] = pid
                item["process"] = proc

    return {
        "ports": entries,
        "listening_count": len(listening_ports_list),
    }


# ─────────────────────────────── runtime ───────────────────────────────

def _check_python_runtime(workspace: str) -> Dict[str, Any]:
    try:
        names = set(os.listdir(workspace))
    except OSError:
        names = set()
    manifests = [m for m in _PY_MANIFESTS if m in names] + sorted(
        n for n in names if _PY_REQ_RE.match(n)
    )
    if os.path.isdir(os.path.join(workspace, "requirements")):
        manifests.append("requirements/")

    exes = (
        ("Scripts/python.exe", "Scripts/python3.exe", "bin/python3", "bin/python")
        if compat.WINDOWS
        else ("bin/python3", "bin/python", "Scripts/python.exe")
    )

    for vname in (".venv", "venv"):
        vpath = os.path.join(workspace, vname)
        if not os.path.lexists(vpath):
            continue
        if os.path.islink(vpath) and not os.path.exists(vpath):
            try:
                target = os.readlink(vpath)
            except OSError:
                target = "?"
            return {
                "detected": True,
                "manifests": manifests,
                "venv": vname,
                "status": "broken",
                "detail": f"{vname} is a broken symlink (-> {target})",
            }
        if not os.path.isdir(vpath):
            return {
                "detected": True,
                "manifests": manifests,
                "venv": vname,
                "status": "broken",
                "detail": f"{vname} exists but is not a directory",
            }
        broken_py: Optional[str] = None
        working_py: Optional[str] = None
        for rel in exes:
            py = os.path.join(vpath, *rel.split("/"))
            if os.path.islink(py) and not os.path.exists(py):
                broken_py = rel
                break
            if os.path.isfile(py) and (compat.WINDOWS or os.access(py, os.X_OK)):
                working_py = rel
                break
        if broken_py:
            return {
                "detected": True,
                "manifests": manifests,
                "venv": vname,
                "status": "broken",
                "detail": f"{vname}/{broken_py} is a broken symlink",
            }
        if not working_py:
            return {
                "detected": True,
                "manifests": manifests,
                "venv": vname,
                "status": "broken",
                "detail": f"{vname}/ exists without a working Python executable",
            }
        cfg_path = os.path.join(vpath, "pyvenv.cfg")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        m = re.match(r"^\s*home\s*=\s*(.+?)\s*$", ln, re.I)
                        if m and m.group(1) and not os.path.exists(m.group(1)):
                            return {
                                "detected": True,
                                "manifests": manifests,
                                "venv": vname,
                                "status": "broken",
                                "detail": f"{vname}/pyvenv.cfg points to missing interpreter home ({m.group(1)})",
                            }
            except OSError:
                pass
        return {
            "detected": True,
            "manifests": manifests,
            "venv": vname,
            "status": "ok",
            "detail": f"{vname}/{working_py} ready",
        }

    if manifests:
        return {
            "detected": True,
            "manifests": manifests,
            "venv": None,
            "status": "missing_venv",
            "detail": f"Python manifests ({', '.join(manifests[:3])}) found, no .venv/ present",
        }
    return {
        "detected": False,
        "manifests": [],
        "venv": None,
        "status": "none",
        "detail": "no Python manifests or .venv",
    }


def _check_node_runtime(workspace: str) -> Dict[str, Any]:
    pkg = os.path.join(workspace, "package.json")
    if not os.path.isfile(pkg):
        return {
            "detected": False,
            "manifest": None,
            "node_modules": False,
            "status": "none",
            "detail": "no package.json",
        }
    nm = os.path.join(workspace, "node_modules")
    if os.path.islink(nm) and not os.path.exists(nm):
        return {
            "detected": True,
            "manifest": "package.json",
            "node_modules": False,
            "status": "broken",
            "detail": "node_modules is a broken symlink",
        }
    if not os.path.exists(nm):
        return {
            "detected": True,
            "manifest": "package.json",
            "node_modules": False,
            "status": "missing",
            "detail": "package.json exists but node_modules/ is missing",
        }
    return {
        "detected": True,
        "manifest": "package.json",
        "node_modules": True,
        "status": "ok",
        "detail": "node_modules/ present",
    }


def check_runtime(workspace: str) -> Dict[str, Any]:
    """Basic workspace runtime checks: broken .venv and missing node_modules."""
    py = _check_python_runtime(workspace)
    node = _check_node_runtime(workspace)
    issues: List[str] = []
    if py["status"] == "broken":
        issues.append(py["detail"])
    if node["status"] in ("missing", "broken"):
        issues.append(node["detail"])
    return {
        "python": py,
        "node": node,
        "issues": issues,
        "issues_count": len(issues),
    }


# ─────────────────────────────── rendering ───────────────────────────────

def render_dotenv(data: Dict[str, Any]) -> str:
    lines = [f"## .env parity — {data['workspace']}"]
    env_f = ", ".join(data["env_files"]) or "none"
    ex_f = ", ".join(data["example_files"]) or "none"
    lines.append(f"- Files: env=`{env_f}` · templates=`{ex_f}` · code files scanned={data['code_files_scanned']}")

    if data["issues_count"] == 0:
        if not data["env_files"] and not data["example_files"] and not data["keys"]["code"]:
            lines.append("- ✅ No `.env` files or env variable references found.")
        else:
            lines.append(f"- ✅ All {len(data['keys']['env'])} env variable(s) are in sync and non-placeholder.")
        return "\n".join(lines)

    if data["missing_in_env"]:
        lines.append("- ❌ **Missing in `.env`** (defined in template or referenced in code):")
        for item in data["missing_in_env"]:
            src = ", ".join(item["sources"][:3])
            lines.append(f"  - `{item['key']}` ({src})")
    if data["missing_in_example"]:
        note = " (no `.env.example` found)" if not data["example_files"] else ""
        lines.append(f"- ⚠️ **Missing in `.env.example`**{note} (present in `.env`):")
        for item in data["missing_in_example"]:
            src = ", ".join(item["sources"])
            lines.append(f"  - `{item['key']}` ({src})")
    if data["empty_or_placeholder"]:
        lines.append("- ⚠️ **Empty or placeholder value in `.env`** (values redacted):")
        for item in data["empty_or_placeholder"]:
            lines.append(f"  - `{item['key']}` (`{item['file']}`: {item['issue']})")
    return "\n".join(lines)


def render_docker(data: Dict[str, Any]) -> str:
    lines = ["## Docker"]
    st = data["status"]
    icon = {"ok": "✅", "hung": "❌", "error": "❌", "not_running": "⚠️", "not_installed": "ℹ️"}.get(st, "⚠️")
    lines.append(f"- {icon} Status: `{st}` — {data['message']}")
    if data.get("recovery"):
        lines.append(f"- {data['recovery']}")
    if data.get("containers_error"):
        lines.append(f"- ⚠️ `docker ps`: {data['containers_error']}")
    containers = data.get("containers") or []
    if containers:
        running = sum(1 for c in containers if c.get("state") == "running")
        unhealthy = len(data.get("unhealthy_containers") or [])
        lines.append(f"- Containers: {len(containers)} total ({running} running, {unhealthy} unhealthy)")
        for c in containers[:20]:
            c_icon = "❌" if c.get("unhealthy") else ("🟢" if c.get("state") == "running" else "⚪")
            ports = f" · {c['ports']}" if c.get("ports") else ""
            lines.append(f"  - {c_icon} `{c['name']}` ({c['image']}) — {c['status'] or c['state']}{ports}")
        if len(containers) > 20:
            lines.append(f"  - … and {len(containers) - 20} more (`--json` lists all)")
    elif st == "ok" and not data.get("containers_error"):
        lines.append("- Containers: none")
    return "\n".join(lines)


def render_ports(data: Dict[str, Any]) -> str:
    lines = ["## Ports"]
    entries = data.get("ports") or []
    listening = [p for p in entries if p["listening"]]
    free = [str(p["port"]) for p in entries if not p["listening"]]
    if listening:
        for p in listening:
            hint = f" ({p['hint']})" if p.get("hint") else ""
            proc = ""
            if p.get("process") or p.get("pid"):
                proc = f" — `{p.get('process') or 'pid'}` (pid {p['pid']})" if p.get("pid") else f" — `{p['process']}`"
            lines.append(f"- 🟢 `{p['host']}:{p['port']}`{hint} listening{proc}")
    if free:
        lines.append(f"- ⚪ Free: {', '.join(free)}")
    return "\n".join(lines)


def render_runtime(data: Dict[str, Any]) -> str:
    lines = ["## Runtime"]
    py, node = data["python"], data["node"]
    if py["status"] == "ok":
        lines.append(f"- ✅ Python: {py['detail']}")
    elif py["status"] == "broken":
        lines.append(f"- ❌ Python: {py['detail']}")
    elif py["status"] == "missing_venv":
        lines.append(f"- ℹ️ Python: {py['detail']}")

    if node["status"] == "ok":
        lines.append(f"- ✅ Node: {node['detail']}")
    elif node["status"] in ("missing", "broken"):
        lines.append(f"- ❌ Node: {node['detail']}")

    if py["status"] == "none" and node["status"] == "none":
        lines.append("- ℹ️ No Python or Node manifests detected.")
    return "\n".join(lines)


# ─────────────────────────────── CLI ───────────────────────────────

SUBCMDS = ("check", "dotenv", "docker", "ports")


def _normalize_argv(argv: List[str]) -> List[str]:
    """Ensure subcommand defaults to 'check' even when flags precede or omit it."""
    if not argv:
        return ["check"]
    if "-h" in argv or "--help" in argv:
        for tok in argv:
            if tok in SUBCMDS:
                return [tok, *[a for a in argv if a != tok]]
        return argv

    value_opts = {"--workspace", "--path", "--ports"}
    positional_idx: Optional[int] = None
    skip_next = False
    for i, tok in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if tok in value_opts:
            skip_next = True
            continue
        if any(tok.startswith(opt + "=") for opt in value_opts) or tok.startswith("-"):
            continue
        positional_idx = i
        break

    if positional_idx is None:
        return ["check", *argv]
    cmd = argv[positional_idx]
    if cmd in SUBCMDS and positional_idx != 0:
        return [cmd, *argv[:positional_idx], *argv[positional_idx + 1:]]
    return argv


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", "--path", dest="workspace", default=".",
                        help="workspace folder (default: current directory)")
    common.add_argument("--ports", default=None,
                        help="comma-separated localhost TCP ports (default: 3000,5173,8000,8080,5432,6379)")
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")

    ap = argparse.ArgumentParser(
        prog="env_check.py",
        description="Safe .env parity check (never prints secrets), 2s non-hanging Docker probe, ports & runtime check.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", parents=[common], help="run dotenv + docker + ports + runtime checks (default)")
    sub.add_parser("dotenv", parents=[common], help="compare .env vs .env.example and source code references")
    sub.add_parser("docker", parents=[common], help="probe Docker daemon with 2s timeout and list containers")
    sub.add_parser("ports", parents=[common], help="probe localhost TCP ports")

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(_normalize_argv(raw_argv))

    workspace = os.path.realpath(os.path.expanduser(args.workspace))
    if args.cmd in ("check", "dotenv") and not os.path.isdir(workspace):
        print(f"env_check.py: workspace directory not found: {workspace}", file=sys.stderr)
        return 2

    try:
        port_list = parse_ports_arg(args.ports)
    except ValueError as e:
        print(f"env_check.py: {e}", file=sys.stderr)
        return 2

    if args.cmd == "dotenv":
        res = check_dotenv(workspace)
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False))
        else:
            print(render_dotenv(res))
        return 1 if res["issues_count"] else 0

    if args.cmd == "docker":
        res = check_docker()
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False))
        else:
            print(render_docker(res))
        return 1 if res["status"] in ("hung", "error") else 0

    if args.cmd == "ports":
        res = check_ports(port_list)
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False))
        else:
            print(render_ports(res))
        return 0

    # args.cmd == "check"
    dotenv_res = check_dotenv(workspace)
    docker_res = check_docker()
    ports_res = check_ports(port_list)
    runtime_res = check_runtime(workspace)
    docker_issues = 1 if docker_res["status"] in ("hung", "error") else 0
    total_issues = dotenv_res["issues_count"] + docker_issues + runtime_res["issues_count"]
    exit_code = 1 if total_issues else 0
    report: Dict[str, Any] = {
        "workspace": workspace,
        "dotenv": dotenv_res,
        "docker": docker_res,
        "ports": ports_res,
        "runtime": runtime_res,
        "issues_count": total_issues,
        "exit": exit_code,
    }
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        print("\n\n".join([
            render_dotenv(dotenv_res),
            render_docker(docker_res),
            render_ports(ports_res),
            render_runtime(runtime_res),
        ]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
