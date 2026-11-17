#!/usr/bin/env python3
"""Antigravity Plus — MCP server: the plugin's helpers as real tools with typed arguments.

Agents otherwise compose shell lines like `python3 "…/skills/lsp/scripts/lsp.py" refs file.py:12 name`, and weaker
models break the quoting of paths with spaces. Antigravity starts this server from the plugin's mcp_config.json
(stdio; `${PLUGIN_ROOT}` is the plugin folder) and offers its tools to every agent while the plugin is on:

  lsp_def, lsp_refs, lsp_calls, lsp_hover, lsp_symbols, lsp_find, lsp_check   skills/lsp/scripts/lsp.py
  memory_search                                                               skills/memory/scripts/memory.py search
  git_state                                                                   skills/git/scripts/git_state.py status
  quota                                                                       Antigravity's plan limits (read-only RPC)

Each tool runs the script as a subprocess (the scripts are found next to this file, so the installed copy works the
same as the repository) with a timeout, and returns its text output. Protocol: MCP over stdio — JSON-RPC 2.0, one
message per line; initialize (protocol version negotiated), ping, tools/list, tools/call; unknown methods get
-32601, notifications are ignored. Only protocol goes to stdout; logs go to stderr.

  server.py            serve on stdin/stdout (what Antigravity runs)
  server.py --list     print the tool names (a quick self-check)

Stdlib only, Python 3.8+, macOS / Linux / Windows.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # noqa: E402

NAME = "antigravity-plus"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
SCRIPTS = {
    "lsp": os.path.join(PLUGIN, "skills", "lsp", "scripts", "lsp.py"),
    "memory": os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py"),
    "git": os.path.join(PLUGIN, "skills", "git", "scripts", "git_state.py"),
}
SERVICE = "exa.language_server_pb.LanguageServerService"
MAX_OUT = 60000          # characters of a tool's output handed back to the agent
HIDE: Dict[str, Any] = {"creationflags": 0x08000000} if compat.WINDOWS else {}  # CREATE_NO_WINDOW


def version() -> str:
    try:
        with open(os.path.join(PLUGIN, "plugin.json"), encoding="utf-8") as f:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
        return m.group(1) if m else "0"
    except OSError:
        return "0"


def log(msg: str) -> None:
    try:
        sys.stderr.write(f"[{NAME} mcp] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


class ToolError(Exception):
    """A problem with the call itself (bad arguments): returned to the agent as an isError result."""


# ------------------------------------------------------------------------------------------------- arguments

def _str(args: dict, key: str, required: bool = False) -> str:
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ToolError(f"`{key}` is required")
        return ""
    if not isinstance(v, str):
        raise ToolError(f"`{key}` must be a string")
    return v.strip()


def _int(args: dict, key: str, lo: int, hi: int, required: bool = False) -> Optional[int]:
    v = args.get(key)
    if v is None or v == "":
        if required:
            raise ToolError(f"`{key}` is required")
        return None
    if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
        raise ToolError(f"`{key}` must be an integer")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ToolError(f"`{key}` must be an integer, not {v!r}")
    if not lo <= n <= hi:
        raise ToolError(f"`{key}` must be between {lo} and {hi}")
    return n


def _bool(args: dict, key: str) -> bool:
    v = args.get(key)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _norm(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


def workspace_of(args: dict, required: bool = False) -> str:
    ws = _str(args, "workspace", required)
    if not ws:
        return ""
    ws = _norm(ws)
    if not os.path.isabs(ws):
        raise ToolError(f"`workspace` must be an absolute folder path, not {ws!r}")
    if not os.path.isdir(ws):
        raise ToolError(f"`workspace` is not a folder: {ws}")
    return ws


def file_of(args: dict, key: str, ws: str) -> str:
    """An existing file: absolute, or relative to the workspace."""
    raw = _str(args, key, True)
    path = _norm(raw)
    if not os.path.isabs(path):
        if not ws:
            raise ToolError(f"`{key}` must be an absolute path (or pass `workspace` for a relative one): {raw!r}")
        path = os.path.normpath(os.path.join(ws, path))
    if not os.path.isfile(path):
        raise ToolError(f"no such file: {path}")
    return path


def project_dir(path: str) -> str:
    """The folder a file's paths are shown relative to when no workspace was given: its repository, else its folder."""
    d = os.path.dirname(path)
    cur = d
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            return d
        cur = up


# ------------------------------------------------------------------------------------------------- running scripts

def run_script(script: str, argv: List[str], cwd: str, timeout: float) -> Tuple[int, str]:
    path = SCRIPTS[script]
    if not os.path.isfile(path):
        return 127, f"{os.path.relpath(path, PLUGIN)} isn't installed (the plugin module that owns it was left out)."
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", path, *argv], cwd=cwd or None, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=env, **HIDE)
    except subprocess.TimeoutExpired:
        what = f"{os.path.basename(path)} {argv[0]}" if argv else os.path.basename(path)
        return 124, f"{what} took longer than {int(timeout)} s and was stopped."
    except OSError as e:
        return 126, f"couldn't run {os.path.basename(path)}: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if err:
        out = (out + "\n" + err).strip() if out else err
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n… (cut: {len(out) - MAX_OUT} more characters)"
    return r.returncode, out or "(no output)"


# ------------------------------------------------------------------------------------------------- the tools

POSITION = {
    "file": {"type": "string", "description": "Absolute path of the source file (or relative to `workspace`)."},
    "line": {"type": "integer", "minimum": 1, "description": "1-based line number where `name` appears."},
    "name": {"type": "string", "description": "The symbol's name as written on that line (no quotes, no dots)."},
    "workspace": {"type": "string", "description": "Absolute path of the project folder (optional; output paths "
                                                   "are shown relative to it)."},
}
LSP_TIMEOUT = 240.0  # a language server's first start can take a minute or more (sourcekit, kotlin)


def lsp_position(args: dict) -> Tuple[List[str], str]:
    ws = workspace_of(args)
    path = file_of(args, "file", ws)
    line = _int(args, "line", 1, 10_000_000, True)
    name = _str(args, "name", True)
    if re.search(r"\s", name):
        raise ToolError("`name` is one symbol name without spaces")
    return [f"{path}:{line}", name], ws or project_dir(path)


def t_lsp_simple(cmd: str) -> Callable[[dict], Tuple[int, str]]:
    def run(args: dict) -> Tuple[int, str]:
        pos, cwd = lsp_position(args)
        return run_script("lsp", [cmd, *pos], cwd, LSP_TIMEOUT)
    return run


def t_lsp_refs(args: dict) -> Tuple[int, str]:
    pos, cwd = lsp_position(args)
    argv = ["refs", *pos]
    limit = _int(args, "limit", 0, 100000)
    if limit is not None:
        argv += ["--limit", str(limit)]
    if "include_declaration" in args and args["include_declaration"] not in (None, "") and not _bool(args, "include_declaration"):
        argv.append("--no-decl")
    return run_script("lsp", argv, cwd, LSP_TIMEOUT)


def t_lsp_calls(args: dict) -> Tuple[int, str]:
    pos, cwd = lsp_position(args)
    argv = ["calls", *pos]
    depth = _int(args, "depth", 1, 5)
    if depth is not None:
        argv += ["--depth", str(depth)]
    if _bool(args, "out"):
        argv.append("--out")
    return run_script("lsp", argv, cwd, LSP_TIMEOUT)


def t_lsp_symbols(args: dict) -> Tuple[int, str]:
    ws = workspace_of(args)
    path = file_of(args, "file", ws)
    argv = ["symbols", path]
    depth = _int(args, "depth", 0, 20)
    if depth is not None:
        argv += ["--depth", str(depth)]
    if _bool(args, "all"):
        argv.append("--all")
    return run_script("lsp", argv, ws or project_dir(path), LSP_TIMEOUT)


def t_lsp_find(args: dict) -> Tuple[int, str]:
    ws = workspace_of(args, True)
    name = _str(args, "name", True)
    argv = ["find"]
    kind = _str(args, "kind")
    if kind:
        argv += ["--kind", kind]
    lang = _str(args, "lang")
    if lang:
        argv += ["--lang", lang]
    argv += ["--", name]
    return run_script("lsp", argv, ws, LSP_TIMEOUT)


def t_lsp_check(args: dict) -> Tuple[int, str]:
    ws = workspace_of(args)
    files = args.get("files")
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list) or not files:
        raise ToolError("`files` is a non-empty list of file paths")
    paths = [file_of({"file": f}, "file", ws) for f in files[:50]]
    argv = ["check", *paths]
    if _bool(args, "errors_only"):
        argv.append("--errors")
    return run_script("lsp", argv, ws or project_dir(paths[0]), LSP_TIMEOUT + 60)


def t_memory_search(args: dict) -> Tuple[int, str]:
    ws = workspace_of(args, True)
    query = _str(args, "query", True)
    return run_script("memory", ["search", "--workspace", ws, "--", query], ws, 30)


def t_git_state(args: dict) -> Tuple[int, str]:
    ws = workspace_of(args, True)
    argv = ["status", "--path", ws]
    if _bool(args, "fetch"):
        argv.append("--fetch")
    if _bool(args, "github"):
        argv.append("--github")
    return run_script("git", argv, ws, 120 if len(argv) > 3 else 45)


# ---- quota: Antigravity's language server, read-only

# the language server is local: no proxy (and no slow lookup of the system's proxy settings on macOS)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base_url(addr: str) -> str:
    addr = addr.strip().rstrip("/")
    return addr if re.match(r"^https?://", addr) else f"http://{addr}"


def _healthy(addr: str) -> bool:
    try:
        with OPENER.open(f"{_base_url(addr)}/healthz", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", errors="replace") or "{}").get("status") == "ok"
    except Exception:
        return False


def language_server() -> Optional[Tuple[str, str]]:
    """(address, CSRF token) of Antigravity's running language server: the environment, else its process."""
    addr, tok = os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and tok:
        return addr, tok
    for pid, line in compat.processes("language_server"):
        if "--csrf_token" not in line or "antigravity" not in line.lower():
            continue
        m = re.search(r"--csrf_token[ =]\"?([^\s\"]+)", line)
        if not m:
            continue
        for port in compat.listening_ports(pid):
            if _healthy(f"127.0.0.1:{port}"):
                return f"127.0.0.1:{port}", m.group(1)
    return None


def rpc(ls: Tuple[str, str], method: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"{_base_url(ls[0])}/{SERVICE}/{method}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": ls[1],
        },
    )
    with OPENER.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
    return data if isinstance(data, dict) else {}


def _until(ts: str, now: float) -> str:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts or "")
    if not m:
        return ""
    s = int(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)) - now)
    if s <= 0:
        return "now"
    if s < 5400:
        return f"in {max(1, round(s / 60))} min"
    if s < 172800:
        return f"in {s // 3600} h" + (f" {s % 3600 // 60} min" if s < 36000 and s % 3600 // 60 else "")
    return f"in {s // 86400} days" + (f" {s % 86400 // 3600} h" if s % 86400 // 3600 else "")


WINDOW_LABELS = {"weekly": "week", "5h": "5 h"}


def quota_text(data: dict, now: float) -> str:
    inner = data.get("response")
    resp = inner if isinstance(inner, dict) else data
    groups = [g for g in (resp.get("groups") or []) if isinstance(g, dict)]
    if not groups:
        return "Antigravity reports no quota groups (signed out, or a plan without limits)."
    lines = ["Antigravity quota left (models in a group share its weekly and 5-hour limits):"]
    for g in groups:
        desc = str(g.get("description") or "")
        models = desc.split(":", 1)[1].strip() if ":" in desc else ""
        parts = []
        for b in g.get("buckets") or []:
            if not isinstance(b, dict):
                continue
            label = WINDOW_LABELS.get(str(b.get("window") or ""), str(b.get("window") or b.get("bucketId") or "?"))
            if b.get("disabled"):
                parts.append(f"{label}: not applied now")
                continue
            try:
                raw = b.get("remainingFraction")
                left = float(raw) if raw is not None else 0.0  # proto3 JSON drops zeros: missing means used up
            except (TypeError, ValueError):
                left = 0.0
            when = _until(str(b.get("resetTime") or ""), now)
            pct = f"{left * 100:.1f}%" if 0 < left < 0.01 else f"{left * 100:.0f}%"
            parts.append(f"{label} {pct} left" + (f" (resets {when})" if when else "")
                         + (" — USED UP" if left <= 0 else ""))
        lines.append(f"- {g.get('displayName') or '?'}: " + ("; ".join(parts) or "no limits reported")
                     + (f" — models: {models}" if models else ""))
    return "\n".join(lines)


def t_quota(args: dict) -> Tuple[int, str]:
    ls = language_server()
    if not ls:
        return 1, "Antigravity's language server isn't reachable from here (is Antigravity running and signed in?)."
    try:
        data = rpc(ls, "RetrieveUserQuotaSummary", {})
    except urllib.error.HTTPError as e:
        return 1, f"Antigravity's RetrieveUserQuotaSummary failed: HTTP {e.code} {e.reason}"
    except (OSError, ValueError) as e:
        return 1, f"couldn't ask Antigravity for the quota: {e}"
    return 0, quota_text(data, time.time())


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS: List[Dict[str, Any]] = [
    {"name": "lsp_def", "run": t_lsp_simple("def"),
     "description": "Where a symbol is defined, asked of the project's language server (pyright, sourcekit-lsp, "
                    "kotlin-lsp, typescript-language-server, gopls, rust-analyzer, clangd…). Give the file and line "
                    "where the name appears.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_refs", "run": t_lsp_refs,
     "description": "Every place a symbol is used, each with the function it is in — from the language server, so "
                    "comments, strings and same-named symbols don't count. Use before renaming, deleting or "
                    "changing a signature. Lists files where the name appears in code the server didn't link.",
     "properties": dict(POSITION, limit={"type": "integer", "minimum": 0,
                                         "description": "Reference lines to show (0 = all)."},
                        include_declaration={"type": "boolean", "description": "List the declaration too (default "
                                                                               "true)."}),
     "required": ["file", "line", "name"]},
    {"name": "lsp_calls", "run": t_lsp_calls,
     "description": "Who calls a function (call hierarchy), optionally several levels up; with out=true, what the "
                    "function calls instead.",
     "properties": dict(POSITION, depth={"type": "integer", "minimum": 1, "maximum": 5,
                                         "description": "Levels of callers (1–5, default 1)."},
                        out={"type": "boolean", "description": "Outgoing calls: what it calls (default false)."}),
     "required": ["file", "line", "name"]},
    {"name": "lsp_hover", "run": t_lsp_simple("hover"),
     "description": "A symbol's type, signature and documentation from the language server.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_symbols", "run": t_lsp_symbols,
     "description": "Outline of a file — classes, methods, functions with their line ranges. Read it before "
                    "reading a big file.",
     "properties": {"file": POSITION["file"], "workspace": POSITION["workspace"],
                    "depth": {"type": "integer", "minimum": 0, "description": "Nesting levels shown (0 = all)."},
                    "all": {"type": "boolean", "description": "Also local symbols (default false)."}},
     "required": ["file"]},
    {"name": "lsp_find", "run": t_lsp_find,
     "description": "Find a symbol by name across the project (workspace symbols): its kind, container and "
                    "file:line.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of the project folder to search."},
                    "name": {"type": "string", "description": "The symbol's name (or its beginning)."},
                    "kind": {"type": "string", "description": "Only this kind: function, class, method, variable…"},
                    "lang": {"type": "string", "description": "The language when the project has several "
                                                              "(python, typescript, swift, kotlin, go, rust, c…)."}},
     "required": ["workspace", "name"]},
    {"name": "lsp_check", "run": t_lsp_check,
     "description": "Type errors and warnings the language server reports for the given files.",
     "properties": {"files": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                              "description": "Absolute file paths (or relative to `workspace`)."},
                    "workspace": POSITION["workspace"],
                    "errors_only": {"type": "boolean", "description": "Errors only, no warnings (default false)."}},
     "required": ["files"]},
    {"name": "memory_search", "run": t_memory_search,
     "description": "Search what the agent remembers between conversations (personal and this project's "
                    "memories) — before asking the user something an earlier conversation may have settled.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of the project folder."},
                    "query": {"type": "string", "description": "Text or regular expression (case-insensitive)."}},
     "required": ["workspace", "query"]},
    {"name": "git_state", "run": t_git_state,
     "description": "The git repository's state in one look: branch, upstream ahead/behind, an operation in "
                    "progress (rebase, merge…), conflicts, changes, stash, last commits, remotes — and the next "
                    "commands for what it found.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of a folder in the repository."},
                    "fetch": {"type": "boolean", "description": "Fetch from the remote first (network; default "
                                                                "false)."},
                    "github": {"type": "boolean", "description": "Also gh's login and this branch's pull request "
                                                                 "(network; default false)."}},
     "required": ["workspace"]},
    {"name": "quota", "run": t_quota,
     "description": "Antigravity's plan limits right now: per quota group, the weekly and 5-hour fraction left, "
                    "when each resets, and which models share it. Read-only.",
     "properties": {"workspace": {"type": "string", "description": "Optional; not needed."}},
     "required": []},
]
BY_NAME = {t["name"]: t for t in TOOLS}


def tool_list() -> List[dict]:
    out = []
    for t in TOOLS:
        out.append({"name": t["name"], "description": t["description"],
                    "inputSchema": {"type": "object", "properties": t["properties"], "required": t["required"],
                                    "additionalProperties": False},
                    "annotations": dict(READ_ONLY, openWorldHint=t["name"] == "git_state")})
    return out


INSTRUCTIONS = ("Antigravity Plus tools. For a symbol in code use lsp_def / lsp_refs / lsp_calls / lsp_hover with "
                "the file's absolute path, the 1-based line and the name — not rg, which can't tell a call from a "
                "comment. lsp_symbols outlines a file, lsp_find finds a symbol by name, lsp_check lists type "
                "errors. memory_search searches saved memories, git_state shows the repository's state, quota "
                "shows Antigravity's limits. All are read-only.")


# ------------------------------------------------------------------------------------------------- the protocol

class Server:
    def __init__(self, out) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.protocol = PROTOCOLS[0]

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def reply(self, mid: Any, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def error(self, mid: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    def handle_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            self.error(None, -32700, "Parse error")
            return
        if isinstance(msg, list):
            if not msg:
                self.error(None, -32600, "Invalid Request")
                return
            for m in msg:
                self.dispatch(m)
        else:
            self.dispatch(msg)

    def dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self.error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
            return
        method, mid = msg.get("method"), msg.get("id")
        is_request = "id" in msg and mid is not None
        if not isinstance(method, str):
            if is_request and ("result" not in msg and "error" not in msg):
                self.error(mid, -32600, "Invalid Request")
            return  # a response to something we never asked, or junk: ignore
        raw = msg.get("params")
        if raw is not None and not isinstance(raw, dict):
            if is_request:
                self.error(mid, -32602, "Invalid params: expected an object")
            return
        params: Dict[str, Any] = raw or {}
        if not is_request:
            return  # notifications: initialized, cancelled, roots/list_changed… nothing to do
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            self.protocol = asked if asked in PROTOCOLS else PROTOCOLS[0]
            info = params.get("clientInfo")
            client: Dict[str, Any] = info if isinstance(info, dict) else {}
            log(f"initialize from {client.get('name') or '?'} {client.get('version') or ''}: protocol {asked or '?'}"
                f" → {self.protocol}")
            self.reply(mid, {"protocolVersion": self.protocol, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": NAME, "version": version()}, "instructions": INSTRUCTIONS})
        elif method == "ping":
            self.reply(mid, {})
        elif method == "tools/list":
            self.reply(mid, {"tools": tool_list()})
        elif method == "tools/call":
            name = params.get("name")
            tool = BY_NAME.get(name) if isinstance(name, str) else None
            if not tool:
                self.error(mid, -32602, f"Unknown tool: {name}")
                return
            args = params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                self.error(mid, -32602, "arguments must be an object")
                return
            threading.Thread(target=self.call, args=(mid, tool, args), daemon=True).start()
        elif method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                   "prompts/list": "prompts"}[method]
            self.reply(mid, {key: []})
        else:
            self.error(mid, -32601, f"Method not found: {method}")

    def call(self, mid: Any, tool: dict, args: dict) -> None:
        started = time.time()
        try:
            code, out = tool["run"](args)
        except ToolError as e:
            code, out = 2, f"{tool['name']}: {e}"
        except Exception as e:  # a bug here must not take the server down
            log(f"{tool['name']} crashed: {e!r}")
            code, out = 1, f"{tool['name']} failed inside the server: {e}"
        log(f"{tool['name']} → exit {code} in {time.time() - started:.1f} s")
        self.reply(mid, {"content": [{"type": "text", "text": out}], "isError": code != 0})

    def serve(self, inp) -> None:
        while True:
            try:
                line = inp.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            try:
                self.handle_line(line)
            except Exception as e:  # never die on one bad message
                log(f"bad message: {e!r}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "--list":
        print("\n".join(t["name"] for t in TOOLS))
        return 0
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print() anywhere must never corrupt the protocol stream
    log(f"started (plugin {PLUGIN}, Python {sys.version.split()[0]})")
    try:
        Server(out).serve(sys.stdin.buffer)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
