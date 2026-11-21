#!/usr/bin/env python3
"""Antigravity Plus — MCP server: the plugin's helpers as real tools with typed arguments.

Agents otherwise compose shell lines like `python3 "…/skills/lsp/scripts/lsp.py" refs file.py:12 name`, and weaker
models break the quoting of paths with spaces. Antigravity starts this server from the plugin's mcp_config.json
(stdio; `${PLUGIN_ROOT}` is the plugin folder) and offers its tools to every agent while the plugin is on:

  lsp_def, lsp_refs, lsp_calls, lsp_hover, lsp_impl, lsp_type,
  lsp_symbols, lsp_find, lsp_check, lsp_rename                                skills/lsp/scripts/lsp.py
  memory_search                                                               skills/memory/scripts/memory.py search
  git_state                                                                   skills/git/scripts/git_state.py status
  quota                                                                       Antigravity's plan limits (read-only RPC)
  ci_status, ci_failure_logs                                                  skills/ci/scripts/ci.py status | logs
  deps_audit                                                                  skills/deps/scripts/deps.py
  ui_check                                                                    skills/ui-check/scripts/ui_check.py
  mobile_ui, mobile_act                                                       skills/mobile/scripts/mobile.py

Each tool runs the script as a subprocess (the scripts are found next to this file, so the installed copy works the
same as the repository) and returns its text output: a long one keeps its start and its end, stderr's end always,
and a json=true result carries the JSON alone. A tool's time limit is the script's own worst case (AGP_MCP_TIMEOUT,
in seconds, caps them all — tests set it low); past it the script is stopped and what it printed so far comes back.
tools/list offers only the tools whose script is installed (a module left out takes its tools along).
Protocol: MCP over stdio — JSON-RPC 2.0, one message per line; initialize (protocol version negotiated), ping,
tools/list, tools/call; unknown methods get -32601, notifications are ignored. Only protocol goes to stdout; logs go
to stderr.

  server.py            serve on stdin/stdout (what Antigravity runs)
  server.py --list     print the tool names (a quick self-check)

Stdlib only, Python 3.8+, macOS / Linux / Windows.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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
    "ci": os.path.join(PLUGIN, "skills", "ci", "scripts", "ci.py"),
    "deps": os.path.join(PLUGIN, "skills", "deps", "scripts", "deps.py"),
    "ui_check": os.path.join(PLUGIN, "skills", "ui-check", "scripts", "ui_check.py"),
    "mobile": os.path.join(PLUGIN, "skills", "mobile", "scripts", "mobile.py"),
}
SERVICE = "exa.language_server_pb.LanguageServerService"
MAX_OUT = 60000          # characters of a tool's output handed back to the agent
ERR_KEEP = 8000          # of those, at least this much of stderr's end (where the error is) when the output is long
KEEP_BYTES = 512 * 1024  # what is kept of each stream while it is read: its first and its last KEEP_BYTES bytes
HIDE: Dict[str, Any] = {"creationflags": 0x08000000 | 0x00000200} if compat.WINDOWS else {"start_new_session": True}  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
LOCAL_UI_EXTS = (".html", ".htm", ".xhtml", ".svg")
SECRET_UI_RE = re.compile(
    r"(^|/)(?:\.env(?:\.(?!example\b|sample\b|template\b|dist\b)[\w.-]+)?|[\w.-]+\.env|"
    r"id_(?:rsa|ed25519|ecdsa|dsa)|[\w.-]+\.(?:pem|key|p12|pfx|keystore|jks)|credentials\.json|"
    r"secrets?\.(?:ya?ml|json|toml))$",
    re.I,
)


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


def _num(args: dict, key: str, lo: float, hi: float, required: bool = False) -> Optional[float]:
    v = args.get(key)
    if v is None or v == "":
        if required:
            raise ToolError(f"`{key}` is required")
        return None
    if isinstance(v, bool):
        raise ToolError(f"`{key}` must be a number")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"`{key}` must be a number, not {v!r}")
    if not math.isfinite(n) or not lo <= n <= hi:
        raise ToolError(f"`{key}` must be between {lo:g} and {hi:g}")
    return n


def _no_dash(val: str, key: str) -> str:
    if val.startswith("-"):
        raise ToolError(f"`{key}` must not start with '-'")
    return val


def _norm(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


def _check_ws_contain(path: str, ws: str, key: str) -> None:
    if not ws:
        return
    try:
        ws_real = os.path.realpath(ws)
        p_real = os.path.realpath(path)
        inside = os.path.commonpath([p_real, ws_real]) == ws_real
    except ValueError:
        inside = False
    if not inside:
        raise ToolError(f"`{key}` must stay inside `workspace` ({ws}): {path}")


def path_in_ws(raw: str, key: str, ws: str) -> str:
    """Normalize an absolute path or resolve a relative one against `workspace`, verifying containment when `ws` is set."""
    path = _norm(raw)
    if not os.path.isabs(path):
        if not ws:
            raise ToolError(f"`{key}` must be an absolute path (or pass `workspace` for a relative one): {raw!r}")
        path = os.path.normpath(os.path.join(ws, path))
    _check_ws_contain(path, ws, key)
    return path


def dir_of(args: dict, key: str, ws: str) -> str:
    """An existing folder: absolute, or relative to `workspace`."""
    raw = _str(args, key, True)
    path = path_in_ws(raw, key, ws)
    if not os.path.isdir(path):
        raise ToolError(f"`{key}` is not a folder: {path}")
    return path


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
    path = path_in_ws(raw, key, ws)
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


def png_out(raw: str, ws: str) -> str:
    """Where a screenshot may be saved: a .png inside the workspace that is new or already a PNG image — so a wrong
    `out` can't replace a source file (mobile.py deletes or overwrites whatever is there)."""
    path = path_in_ws(raw, "out", ws)
    if not path.lower().endswith(".png"):
        raise ToolError(f"`out` must be a .png file (a new one, or an existing PNG inside `workspace`): {path}")
    if os.path.lexists(path):
        is_png = False
        if os.path.isfile(path):  # not a folder, a dangling link or a pipe
            try:
                with open(path, "rb") as f:
                    is_png = f.read(len(PNG_MAGIC)) == PNG_MAGIC
            except OSError:
                pass
        if not is_png:
            raise ToolError(f"`out` already exists and isn't a PNG image, so it won't be overwritten: {path}")
    return path


def has_build(folder: str, depth: int = 4, cap: int = 5000) -> bool:
    """A .app bundle or an .apk within `depth` levels of a folder (as mobile.py looks for one), visiting at most `cap`
    folders."""
    todo, seen = [(folder, 0)], 0
    while todo and seen < cap:
        cur, level = todo.pop()
        seen += 1
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            name = e.name.lower()
            try:
                if name.endswith(".app") and e.is_dir():
                    return True
                if name.endswith(".apk") and e.is_file():
                    return True
                if level < depth and e.is_dir(follow_symlinks=False):
                    todo.append((e.path, level + 1))
            except OSError:
                continue
    return False


def build_of(raw: str, ws: str) -> str:
    """A build for `mobile_act run`, wherever it is (it is only installed on the device; Xcode puts simulator builds
    in ~/Library/Developer/Xcode/DerivedData): an .app bundle, an .apk, an .ipa (mobile.py explains it can't run on a
    simulator), or a folder with an .app or .apk, such as …/Build/Products/Debug-iphonesimulator."""
    path = _norm(raw)
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(ws, path))
    if not os.path.exists(path):
        raise ToolError(f"no such build path: {path}")
    low = path.lower()
    if low.endswith(".app") and os.path.isdir(path):
        return path
    if low.endswith((".apk", ".ipa")) and os.path.isfile(path):
        return path
    if os.path.isdir(path) and not low.endswith(".app") and has_build(path):
        return path
    raise ToolError("`path` must be an .app bundle, an .apk, or a folder with one (…/Build/Products/"
                    f"Debug-iphonesimulator, app/build/outputs/apk/debug): {path}")


# ------------------------------------------------------------------------------------------------- running scripts

Parts = Tuple[str, str, int]  # a stream as text: (start, end, bytes dropped between them); no gap → start is all of it


def time_limit(seconds: float) -> float:
    """A tool's time limit, capped by AGP_MCP_TIMEOUT (seconds) when that is set."""
    try:
        cap = float(os.environ.get("AGP_MCP_TIMEOUT") or 0)
    except ValueError:
        cap = 0.0
    return min(seconds, cap) if cap > 0 else seconds


def _decode(data: bytes) -> str:
    return re.sub(r"\r\n?", "\n", data.decode("utf-8", errors="replace"))


class Capture:
    """One output stream of a script, read on its own thread (a full pipe never blocks the script, and what was read
    survives a timeout): its first and its last KEEP_BYTES bytes, and how much there was."""

    def __init__(self, stream: Any) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    break
                with self.lock:
                    self.total += len(chunk)
                    room = KEEP_BYTES - len(self.head)
                    if room > 0:
                        self.head += chunk[:room]
                        chunk = chunk[room:]
                    if chunk:
                        self.tail += chunk
                        if len(self.tail) > KEEP_BYTES:
                            del self.tail[:len(self.tail) - KEEP_BYTES]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def parts(self) -> Parts:
        with self.lock:
            head, tail = bytes(self.head), bytes(self.tail)
            gap = self.total - len(head) - len(tail)
        if not gap:
            return _decode(head + tail).strip(), "", 0
        return _decode(head).lstrip(), _decode(tail).rstrip(), gap


def _size(p: Parts) -> int:
    return len(p[0]) + len(p[1]) + p[2]


def clip(head: str, tail: str, gap: int, budget: int, head_share: float) -> str:
    """About `budget` characters of a text: its start and its end, cut at line breaks nearby, around a marker that says
    how much is left out. `head` … `tail` have `gap` bytes between them that were never kept (no gap: `head` is all)."""
    if not gap and len(head) <= budget:
        return head
    whole = not gap
    if whole:
        tail = head
    keep_h = min(len(head), int(budget * head_share))
    keep_t = min(len(tail), budget - keep_h)
    start, end = head[:keep_h], tail[len(tail) - keep_t:]
    nl = start.rfind("\n")
    if nl >= 0 and nl > keep_h - 2000:
        start = start[:nl]
    nl = end.find("\n")
    if 0 <= nl < 2000:
        end = end[nl + 1:]
    cut = len(head) - len(start) - len(end) if whole else len(head) - len(start) + len(tail) - len(end) + gap
    return f"{start}\n… [{'about ' if gap else ''}{cut} characters cut here] …\n{end}"


def render(o: Parts, e: Parts) -> str:
    """stdout, then stderr, in about MAX_OUT characters: a long one keeps its start and its end, and stderr's end
    (where the error is) is always there."""
    o_len, e_len = _size(o), _size(e)
    if o_len + e_len <= MAX_OUT:
        return "\n".join(p[0] for p in (o, e) if p[0])
    e_room = min(e_len, max(ERR_KEEP, MAX_OUT - o_len))
    texts = [clip(*o, budget=MAX_OUT - e_room, head_share=0.5), clip(*e, budget=e_room, head_share=0.25)]
    return "\n".join(t for t in texts if t)


def json_result(what: str, code: int, o: Parts, e: Parts) -> Tuple[int, List[str]]:
    """A json=true answer: the JSON alone as the first item, so it parses; stderr's end only when the script failed,
    as an item of its own (a warning beside a good result is dropped). Too big to return whole → an error, not a cut."""
    err = clip(*e, budget=ERR_KEEP, head_share=0.25)
    if _size(o) > MAX_OUT:
        msg = (f"{what}: its JSON is {_size(o)} characters — more than the {MAX_OUT} one tool result carries, and a cut "
               "one wouldn't parse. Narrow the request (a smaller limit, a filter) or ask without json.")
        return (code or 1), [msg] + ([err] if err else [])
    if code == 0 or not err:
        return code, [o[0] or "(no output)"]
    return code, ([o[0], "stderr:\n" + err] if o[0] else [err])


def stop(proc: Any) -> None:
    """Stop a script that ran out of time, with its process group (POSIX) or process tree (Windows). Helpers it put in
    a session of their own — deps.py's and mobile.py's tools, but also the LSP daemon and the Android emulator, which
    are meant to outlive it — can't be told apart from here, so they are left alone."""
    try:
        compat.kill_tree(proc.pid)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(2):
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass


def execute(cmd: List[str], cwd: str, limit: float) -> Tuple[int, Capture, Capture, bool]:
    """Run a command, its output read on threads; past `limit` seconds it is stopped. (exit code, stdout, stderr,
    stopped)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **HIDE)
    out, err = Capture(proc.stdout), Capture(proc.stderr)
    stopped = False
    try:
        proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        stopped = True
        stop(proc)
    until = time.time() + 2.0  # EOF follows the exit at once — unless a helper it left running holds the pipes
    for cap in (out, err):
        cap.thread.join(timeout=max(0.0, until - time.time()))
    return (proc.returncode if proc.returncode is not None else -1), out, err, stopped


def run_script(script: str, argv: List[str], cwd: str, timeout: float, json_out: bool = False,
               findings: Tuple[int, ...] = ()) -> Tuple[int, Any]:
    """Run a skill script: (exit code, its output as text — for json_out, a list of texts with the JSON first).
    `findings`: exit codes that mean «it ran and reports what it found» (a result, not an error) when it printed one."""
    path = SCRIPTS[script]
    if not os.path.isfile(path):
        return 127, f"{os.path.relpath(path, PLUGIN)} isn't installed (the plugin module that owns it was left out)."
    limit = time_limit(timeout)
    what = f"{os.path.basename(path)} {argv[0]}" if argv else os.path.basename(path)
    try:
        code, out, err, stopped = execute([sys.executable, "-X", "utf8", path, *argv], cwd, limit)
    except (OSError, ValueError) as e:
        return 126, f"couldn't run {os.path.basename(path)}: {e}"
    so, se = out.parts(), err.parts()
    if stopped:
        note = f"{what} took longer than {limit:g} s and was stopped."
        text = render(so, se)
        return 124, (f"{text}\n\n[{note} Above is what it printed until then.]" if text else note)
    if code in findings and so[0] and "Traceback (most recent call last)" not in se[0] + se[1]:
        code = 0
    if json_out:
        return json_result(what, code, so, se)
    return code, render(so, se) or "(no output)"


# ------------------------------------------------------------------------------------------------- the tools

POSITION = {
    "file": {"type": "string", "description": "Absolute path of the source file (or relative to `workspace`)."},
    "line": {"type": "integer", "minimum": 1, "description": "1-based line number where `name` appears."},
    "name": {"type": "string", "description": "The symbol's name as written on that line (no quotes, no dots)."},
    "workspace": {"type": "string", "description": "Absolute path of the project folder (optional; output paths "
                                                   "are shown relative to it)."},
}
LSP_TIMEOUT = 240.0  # a language server's first start can take a minute or more (sourcekit, kotlin)


def lsp_position(args: dict, ws_required: bool = False) -> Tuple[List[str], str]:
    ws = workspace_of(args, ws_required)
    path = file_of(args, "file", ws)
    line = _int(args, "line", 1, 10_000_000, True)
    name = _no_dash(_str(args, "name", True), "name")
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
    if len(files) > 50:
        raise ToolError(f"`files` takes at most 50 paths per call (got {len(files)}): split the list")
    paths = [file_of({"files": f}, "files", ws) for f in files]
    argv = ["check", *paths]
    if _bool(args, "errors_only"):
        argv.append("--errors")
    return run_script("lsp", argv, ws or project_dir(paths[0]), LSP_TIMEOUT + 60)


def t_lsp_rename(args: dict) -> Tuple[int, str]:
    apply_edits = _bool(args, "apply")
    pos, cwd = lsp_position(args, ws_required=apply_edits)
    new_name = _str(args, "new_name", True)
    if re.search(r"\s", new_name) or new_name.startswith("-"):
        raise ToolError("`new_name` must be one symbol name without spaces or leading '-'")
    argv = ["rename", *pos, "--to", new_name]
    if apply_edits:
        argv.append("--apply")
    return run_script("lsp", argv, cwd, LSP_TIMEOUT)


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
    # git_state.py: local git calls; `git fetch` up to 90 s; gh's login, repository and pull request up to ~155 s
    return run_script("git", argv, ws, 60 + (90 if "--fetch" in argv else 0) + (160 if "--github" in argv else 0))


def t_ci_status(args: dict) -> Tuple[int, Any]:
    ws = workspace_of(args, True)
    argv = ["status", "--workspace", ws]
    for key, flag in (("branch", "--branch"), ("sha", "--sha"), ("pr", "--pr")):
        val = _str(args, key)
        if val:
            argv.append(f"{flag}={_no_dash(val, key)}")
    limit = _int(args, "limit", 1, 50)
    if limit is not None:
        argv += ["--limit", str(limit)]
    as_json = _bool(args, "json")
    if as_json:
        argv.append("--json")
    # ci.py: git (~12 s), a PR lookup (20 s), the run list (25 s), the jobs of up to 6 runs (25 s each)
    return run_script("ci", argv, ws, 240, json_out=as_json)


def t_ci_failure_logs(args: dict) -> Tuple[int, Any]:
    ws = workspace_of(args, True)
    run = _str(args, "run")
    if run and (run.startswith("-") or re.search(r"\s", run)):
        raise ToolError("`run` must be a single run ID without spaces")
    job = _str(args, "job")
    lines = _int(args, "lines", 10, 2000)
    argv = ["logs"]
    if run:
        argv.append(run)
    argv += ["--workspace", ws]
    if job:
        argv.append(f"--job={job}")
    if lines is not None:
        argv += ["--lines", str(lines)]
    if _bool(args, "full"):
        argv.append("--full")
    # ci.py: git (~16 s), the run list and its jobs (25 s each, twice for GitLab), then the logs: one 45 s call on
    # GitHub, up to 5 failed jobs × (trace 45 s + API fallback 45 s) on GitLab
    return run_script("ci", argv, ws, 600)


def t_deps_audit(args: dict) -> Tuple[int, Any]:
    ws = workspace_of(args, True)
    mode = _str(args, "mode") or "audit"
    if mode not in ("scan", "audit", "outdated", "all"):
        raise ToolError("`mode` must be one of: scan, audit, outdated, all")
    timeout_arg = _int(args, "timeout", 5, 600)
    as_json = _bool(args, "json")
    argv = [mode, "--path", ws]
    if as_json:
        argv.append("--json")
    if timeout_arg is not None:
        argv += ["--timeout", str(timeout_arg)]
    # deps.py's --timeout is per tool, and its tools run in phases (four ecosystems at a time): audit (a venv's
    # package list first, up to 60 s more), then the osv-scanner fallback, then outdated — each phase up to t.
    t = float(timeout_arg or 120)
    phases = {"scan": 0, "audit": 2, "outdated": 1, "all": 3}[mode]
    limit = 120.0 if mode == "scan" else phases * t + (min(60.0, t) if mode in ("audit", "all") else 0.0) + 60.0
    # deps.py exits 1 when it reports findings (vulnerabilities / outdated packages): a result, not an error; 2 when
    # checks couldn't run.
    return run_script("deps", argv, ws, limit, json_out=as_json, findings=(1,))


def t_ui_check(args: dict) -> Tuple[int, str]:
    scan_raw = _str(args, "scan")
    out_raw = _str(args, "out")
    ws = workspace_of(args, required=bool(scan_raw or out_raw))
    raw = _str(args, "target", True)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or re.search(r"\s", raw):
            raise ToolError("`target` URL must start with http:// or https:// and contain no spaces")
        target = parsed.scheme.lower() + raw[len(parsed.scheme):]  # ui_check.py knows a URL by a lowercase scheme
        cwd = ws
    else:
        target = path_in_ws(raw, "target", ws)
        norm_slashes = target.replace("\\", "/")
        if SECRET_UI_RE.search(norm_slashes) or os.path.splitext(target)[1].lower() not in LOCAL_UI_EXTS:
            raise ToolError("`target` file must be an HTML/SVG page (.html, .htm, .xhtml, .svg)")
        if not os.path.isfile(target):
            raise ToolError(f"no such file: {target}")
        cwd = ws or project_dir(target)
    widths = _str(args, "widths") or "390,1440"
    parts = [p.strip() for p in widths.split(",") if p.strip()]
    if not parts or not all(p.isdigit() and 1 <= int(p) <= 10000 for p in parts):
        raise ToolError("`widths` must be comma-separated positive integers, e.g. '390,1440'")
    if len(parts) > 8:
        raise ToolError("`widths` supports at most 8 widths per check")
    height = _int(args, "height", 100, 20000)
    scan_dir = dir_of(args, "scan", ws) if scan_raw else ""
    out_dir = path_in_ws(out_raw, "out", ws) if out_raw else ""
    argv = [target, "--widths", ",".join(parts)]
    if height is not None:
        argv += ["--height", str(height)]
    if out_dir:
        argv.append(f"--out={out_dir}")
    if scan_dir:
        argv.append(f"--scan={scan_dir}")
    # ui_check.py: per width a screenshot and a DOM dump, each a Chrome run of up to 60 s; then the optional scan
    return run_script("ui_check", argv, cwd, 120.0 * len(parts) + 60 + (60 if scan_dir else 0))


def _mobile_target_flags(args: dict) -> Tuple[str, str, List[str]]:
    platform = _str(args, "platform")
    if platform and platform not in ("ios", "android"):
        raise ToolError("`platform` must be 'ios' or 'android'")
    device = _str(args, "device")
    flags: List[str] = []
    if platform == "ios":
        flags.append("--ios")
    elif platform == "android":
        flags.append("--android")
    if device:
        flags.append(f"--device={device}")
    return platform, device, flags


def t_mobile_ui(args: dict) -> Tuple[int, Any]:
    action = _str(args, "action", True)
    if action not in ("devices", "project", "ui", "shot", "logs"):
        raise ToolError("`action` must be one of: devices, project, ui, shot, logs")
    ws = workspace_of(args, True)
    _platform, _device, flags = _mobile_target_flags(args)
    as_json = False
    if action == "devices":
        argv = ["devices"]
    elif action == "project":
        argv = ["project", ws]
    elif action == "ui":
        find = _str(args, "find")
        argv = ["ui", *flags]
        if find:
            argv.append(f"--find={find}")
        if _bool(args, "all"):
            argv.append("--all")
        as_json = _bool(args, "json")
        if as_json:
            argv.append("--json")
    elif action == "shot":
        out_raw = _str(args, "out")
        argv = ["shot", *flags]
        if out_raw:
            argv.append(f"--out={png_out(out_raw, ws)}")
    else:  # logs
        app = _str(args, "app")
        if app and (app.startswith("-") or re.search(r"\s", app)):
            raise ToolError("`app` must be a bundle ID or package name without spaces")
        last = _str(args, "last")
        if last and not re.fullmatch(r"\d+\s*[smh]?", last.lower()):
            raise ToolError("`last` must be a duration like '30s', '2m' or '1h'")
        argv = ["logs"]
        if app:
            argv.append(app)
        argv += flags
        if last:
            argv.append(f"--last={last}")
        if _bool(args, "all"):
            argv.append("--all")
    return run_script("mobile", argv, ws, 120, json_out=as_json)


def t_mobile_act(args: dict) -> Tuple[int, str]:
    action = _str(args, "action", True)
    if action not in ("boot", "run", "tap", "swipe", "type", "key", "open", "wait"):
        raise ToolError("`action` must be one of: boot, run, tap, swipe, type, key, open, wait")
    ws = workspace_of(args, True)
    platform, device, flags = _mobile_target_flags(args)
    shot_flag = ["--shot"] if _bool(args, "shot") else []
    timeout = 60.0 + (45.0 if shot_flag else 0.0)  # --shot: a second's settle, then a screenshot of up to 40 s
    if action == "boot":
        name = device or _str(args, "text")
        argv = ["boot"]
        if name:
            argv.append(_no_dash(name, "device"))
        if platform == "ios":
            argv.append("--ios")
        elif platform == "android":
            argv.append("--android")
        argv += ["--project", ws]
        timeout = 420.0
    elif action == "run":
        argv = ["run", "--project", ws, *flags]
        raw_path = _str(args, "path")
        if raw_path:
            argv.insert(1, build_of(raw_path, ws))
        timeout = 480.0  # mobile.py run: install (adb up to 300 s, simctl 240 s), launch, screenshot, screen, log
    elif action == "tap":
        text = _str(args, "text")
        ident = _str(args, "id")
        x = _num(args, "x", 0, 20000)
        y = _num(args, "y", 0, 20000)
        if (x is None) != (y is None):
            raise ToolError("`tap` requires both `x` and `y` together")
        if (text or ident) and x is not None:
            raise ToolError("pass either `text`/`id` or `x`/`y`, not both")
        argv = ["tap", *flags, *shot_flag]
        if text or ident:
            if text:
                argv.append(f"--text={text}")
            if ident:
                argv.append(f"--id={ident}")
        elif x is not None and y is not None:
            argv += [f"{x:g}", f"{y:g}"]
        else:
            raise ToolError("`tap` requires `text`, `id`, or both `x` and `y`")
        if _bool(args, "long"):
            argv.append("--long")
        nth = _int(args, "nth", 1, 1000)
        if nth is not None:
            argv += ["--nth", str(nth)]
    elif action == "swipe":
        direction = _str(args, "direction").lower()
        text_dir = _str(args, "text").lower()
        x = _num(args, "x", 0, 20000)
        y = _num(args, "y", 0, 20000)
        x2 = _num(args, "x2", 0, 20000)
        y2 = _num(args, "y2", 0, 20000)
        coords = (x, y, x2, y2)
        if any(v is not None for v in coords) and not all(v is not None for v in coords):
            raise ToolError("`swipe` requires all four coordinates `x`, `y`, `x2`, `y2` (or `direction`)")
        if direction and any(v is not None for v in coords):
            raise ToolError("pass either `direction` or `x`/`y`/`x2`/`y2`, not both")
        argv = ["swipe", *flags, *shot_flag]
        if direction or (text_dir and all(v is None for v in coords)):
            d = direction or text_dir
            if d not in ("up", "down", "left", "right"):
                raise ToolError("`swipe` direction must be up, down, left, or right")
            argv.append(d)
        elif all(v is not None for v in coords):
            argv += [f"{v:g}" for v in coords if v is not None]
        else:
            raise ToolError("`swipe` requires `direction` (up/down/left/right) or all four of `x`, `y`, `x2`, `y2`")
    elif action == "type":
        raw_text = args.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            raise ToolError("`text` is required")
        into = _str(args, "into")
        ident = _str(args, "id")
        argv = ["type", *flags, *shot_flag]
        if into:
            argv.append(f"--into={into}")
        if ident:
            argv.append(f"--id={ident}")
        argv += ["--", raw_text]
    elif action == "key":
        key_name = _str(args, "text", True).lower()
        if re.search(r"\s", key_name) or key_name.startswith("-"):
            raise ToolError("`text` must be a single key name without spaces (e.g. 'back', 'home', 'enter')")
        argv = ["key", key_name, *flags, *shot_flag]
    elif action == "open":
        url = _str(args, "url", True)
        if re.search(r"\s", url) or url.startswith("-") or ":" not in url:
            raise ToolError("`url` must be a valid URL or deep link without spaces")
        argv = ["open", url, *flags, *shot_flag]
    else:  # wait
        wtext = _str(args, "text", True)
        wt = _num(args, "timeout", 0.5, 300)
        argv = ["wait", *flags, *shot_flag]
        if _bool(args, "gone"):
            argv.append("--gone")
        if wt is not None:
            argv += ["--timeout", f"{wt:g}"]
        argv += ["--", wtext]
        timeout = float((wt or 15.0) + 30.0) + (45.0 if shot_flag else 0.0)
    return run_script("mobile", argv, ws, timeout)


# ---- quota: Antigravity's language server, read-only

# the language server is local: no proxy (and no slow lookup of the system's proxy settings on macOS)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base_url(addr: str) -> str:
    addr = addr.strip().rstrip("/")
    return addr if re.match(r"^https?://", addr) else f"http://{addr}"


def language_server() -> Optional[Tuple[str, str]]:
    """(address, CSRF token) of Antigravity's running language server: compat.language_server(). Antigravity gives
    this server only PLUGIN_ROOT / PLUGIN_DATA, so on 2.34+ it is found by ~/.gemini/antigravity/daemon/hub_*.json."""
    found = compat.language_server()
    return (found.addr, found.token) if found else None


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
OPEN_WORLD_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
REWRITES_FILES = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
DRIVES_DEVICE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
MOBILE_NOTE = ("Like mobile.py, which needs BypassSandbox: true, it reaches the simulator and adb: simulators and "
               "emulators only — a phone on a cable just when the user asked for it (then name it in `device`).")

TOOLS: List[Dict[str, Any]] = [
    {"name": "lsp_def", "script": "lsp", "run": t_lsp_simple("def"),
     "description": "Where a symbol is defined, asked of the project's language server (pyright, sourcekit-lsp, "
                    "kotlin-lsp, typescript-language-server, gopls, rust-analyzer, clangd…). Give the file and line "
                    "where the name appears.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_refs", "script": "lsp", "run": t_lsp_refs,
     "description": "Every place a symbol is used, each with the function it is in — from the language server, so "
                    "comments, strings and same-named symbols don't count. Use before renaming, deleting or "
                    "changing a signature. Lists files where the name appears in code the server didn't link.",
     "properties": dict(POSITION, limit={"type": "integer", "minimum": 0,
                                         "description": "Reference lines to show (0 = all)."},
                        include_declaration={"type": "boolean", "description": "List the declaration too (default "
                                                                               "true)."}),
     "required": ["file", "line", "name"]},
    {"name": "lsp_calls", "script": "lsp", "run": t_lsp_calls,
     "description": "Who calls a function (call hierarchy), optionally several levels up; with out=true, what the "
                    "function calls instead.",
     "properties": dict(POSITION, depth={"type": "integer", "minimum": 1, "maximum": 5,
                                         "description": "Levels of callers (1–5, default 1)."},
                        out={"type": "boolean", "description": "Outgoing calls: what it calls (default false)."}),
     "required": ["file", "line", "name"]},
    {"name": "lsp_hover", "script": "lsp", "run": t_lsp_simple("hover"),
     "description": "A symbol's type, signature and documentation from the language server.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_impl", "script": "lsp", "run": t_lsp_simple("impl"),
     "description": "Implementations or overrides of an interface, protocol, or abstract symbol from the language "
                    "server.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_type", "script": "lsp", "run": t_lsp_simple("type"),
     "description": "Where a symbol's type is defined (type definition) from the language server.",
     "properties": dict(POSITION), "required": ["file", "line", "name"]},
    {"name": "lsp_symbols", "script": "lsp", "run": t_lsp_symbols,
     "description": "Outline of a file — classes, methods, functions with their line ranges. Read it before "
                    "reading a big file.",
     "properties": {"file": POSITION["file"], "workspace": POSITION["workspace"],
                    "depth": {"type": "integer", "minimum": 0, "description": "Nesting levels shown (0 = all)."},
                    "all": {"type": "boolean", "description": "Also local symbols (default false)."}},
     "required": ["file"]},
    {"name": "lsp_find", "script": "lsp", "run": t_lsp_find,
     "description": "Find a symbol by name across the project (workspace symbols): its kind, container and "
                    "file:line.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of the project folder to search."},
                    "name": {"type": "string", "description": "The symbol's name (or its beginning)."},
                    "kind": {"type": "string", "description": "Only this kind: function, class, method, variable…"},
                    "lang": {"type": "string", "description": "The language when the project has several "
                                                              "(python, typescript, swift, kotlin, go, rust, c…)."}},
     "required": ["workspace", "name"]},
    {"name": "lsp_check", "script": "lsp", "run": t_lsp_check,
     "description": "Type errors and warnings the language server reports for the given files.",
     "properties": {"files": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                              "description": "Absolute file paths (or relative to `workspace`), at most 50."},
                    "workspace": POSITION["workspace"],
                    "errors_only": {"type": "boolean", "description": "Errors only, no warnings (default false)."}},
     "required": ["files"]},
    {"name": "lsp_rename", "script": "lsp", "run": t_lsp_rename,
     "description": "Rename a symbol across the project via the language server. Previews the edits by default; pass "
                    "apply=true (with `workspace`) to rewrite the files on disk.",
     "properties": dict(POSITION,
                        new_name={"type": "string", "description": "New symbol name (single identifier without spaces)."},
                        apply={"type": "boolean", "description": "Write edits to disk (default false: preview only)."}),
     "required": ["file", "line", "name", "new_name"], "annotations": REWRITES_FILES},
    {"name": "memory_search", "script": "memory", "run": t_memory_search,
     "description": "Search what the agent remembers between conversations (personal and this project's "
                    "memories) — before asking the user something an earlier conversation may have settled.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of the project folder."},
                    "query": {"type": "string", "description": "Text or regular expression (case-insensitive)."}},
     "required": ["workspace", "query"]},
    {"name": "git_state", "script": "git", "run": t_git_state,
     "description": "The git repository's state in one look: branch, upstream ahead/behind, an operation in "
                    "progress (rebase, merge…), conflicts, changes, stash, last commits, remotes — and the next "
                    "commands for what it found.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of a folder in the repository."},
                    "fetch": {"type": "boolean", "description": "Fetch from the remote first (network; default "
                                                                "false)."},
                    "github": {"type": "boolean", "description": "Also gh's login and this branch's pull request "
                                                                 "(network; default false)."}},
     "required": ["workspace"], "annotations": OPEN_WORLD_READ_ONLY},
    {"name": "quota", "script": None, "run": t_quota,
     "description": "Antigravity's plan limits right now: per quota group, the weekly and 5-hour fraction left, "
                    "when each resets, and which models share it. Read-only.",
     "properties": {"workspace": {"type": "string", "description": "Optional; not needed."}},
     "required": []},
    {"name": "ci_status", "script": "ci", "run": t_ci_status,
     "description": "Check CI workflow/pipeline runs and jobs (GitHub Actions via gh, GitLab CI via glab) for HEAD, "
                    "a branch, a SHA, or a PR.",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of a folder in the repository."},
                    "branch": {"type": "string", "description": "Filter runs to this branch (optional)."},
                    "sha": {"type": "string", "description": "Filter runs to this commit SHA (optional)."},
                    "pr": {"type": "string", "description": "Pull request / MR number, branch or URL (optional)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "Maximum runs to show (1–50, default 10)."},
                    "json": {"type": "boolean", "description": "Return machine-readable JSON (default false)."}},
     "required": ["workspace"], "annotations": OPEN_WORLD_READ_ONLY},
    {"name": "ci_failure_logs", "script": "ci", "run": t_ci_failure_logs,
     "description": "Fetch and clean failure logs from a CI run/pipeline (defaults to the latest failed run on the "
                    "current branch).",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of a folder in the repository."},
                    "run": {"type": "string", "description": "CI run or pipeline ID (optional)."},
                    "job": {"type": "string", "description": "Filter to a specific job ID or name substring (optional)."},
                    "lines": {"type": "integer", "minimum": 10, "maximum": 2000,
                              "description": "Maximum lines per section (10–2000, default 120)."},
                    "full": {"type": "boolean", "description": "Include passing lines without trimming (default false)."}},
     "required": ["workspace"], "annotations": OPEN_WORLD_READ_ONLY},
    {"name": "deps_audit", "script": "deps", "run": t_deps_audit,
     "description": "Audit project dependencies for known vulnerabilities and outdated packages across Python, "
                    "JS/TS, Rust, Go, Ruby, PHP, Swift, and Gradle using installed tools (plus osv-scanner fallback).",
     "properties": {"workspace": {"type": "string", "description": "Absolute path of the project folder."},
                    "mode": {"type": "string", "enum": ["scan", "audit", "outdated", "all"],
                             "description": "'audit' (vulnerabilities, default), 'outdated' (newer versions), "
                                            "'all' (both), or 'scan' (offline manifest & tool check)."},
                    "json": {"type": "boolean", "description": "Return machine-readable JSON (default false)."},
                    "timeout": {"type": "integer", "minimum": 5, "maximum": 600,
                                "description": "Seconds per tool (5–600, default 120)."}},
     "required": ["workspace"], "annotations": OPEN_WORLD_READ_ONLY},
    {"name": "ui_check", "script": "ui_check", "run": t_ui_check,
     "description": "Render a URL or HTML file in headless Chrome at phone and desktop widths, returning screenshot "
                    "paths, console errors, and optional heavy-GPU effect scan results.",
     "properties": {"target": {"type": "string", "description": "http(s):// URL or existing HTML file path "
                                                                "(absolute, or relative to `workspace`)."},
                    "workspace": {"type": "string", "description": "Absolute path of the project folder (required if "
                                                                   "`target`, `scan`, or `out` is relative)."},
                    "widths": {"type": "string", "description": "Comma-separated viewport widths in px (default "
                                                                "'390,1440')."},
                    "height": {"type": "integer", "minimum": 100, "maximum": 20000,
                               "description": "Viewport height in px (default 2400)."},
                    "scan": {"type": "string", "description": "Folder path to scan CSS/JS/HTML for heavy phone GPU "
                                                              "effects (optional)."},
                    "out": {"type": "string", "description": "Output folder for screenshots (optional)."}},
     "required": ["target"], "annotations": OPEN_WORLD_READ_ONLY},
    {"name": "mobile_ui", "script": "mobile", "run": t_mobile_ui,
     "description": "Mobile simulator/emulator inspection that changes nothing on the device: list devices, inspect "
                    "build setup, dump on-screen UI elements, take a screenshot (saved as a PNG), or read app "
                    "error/crash logs. " + MOBILE_NOTE,
     "properties": {"action": {"type": "string", "enum": ["devices", "project", "ui", "shot", "logs"],
                               "description": "Inspection action to run."},
                    "workspace": {"type": "string", "description": "Absolute path of the project folder."},
                    "platform": {"type": "string", "enum": ["ios", "android"],
                                 "description": "Target running platform (optional)."},
                    "device": {"type": "string", "description": "Device name, UDID or adb serial (optional)."},
                    "find": {"type": "string", "description": "For 'ui': filter elements by text or ID."},
                    "app": {"type": "string", "description": "For 'logs': bundle ID or package name (optional)."},
                    "last": {"type": "string", "description": "For 'logs': time window like '30s', '2m', '1h'."},
                    "all": {"type": "boolean", "description": "For 'ui' or 'logs': include all items/levels."},
                    "json": {"type": "boolean", "description": "For 'ui': return machine-readable JSON."},
                    "out": {"type": "string", "description": "For 'shot': a .png path inside `workspace`, new or an "
                                                           "existing PNG (optional; default: the plugin's shots "
                                                           "folder)."}},
     "required": ["action", "workspace"]},
    {"name": "mobile_act", "script": "mobile", "run": t_mobile_act,
     "description": "Drive an iOS simulator or Android emulator: boot, install & run a build, tap, swipe, type, press "
                    "a key, open a URL/deep link, or wait for text. " + MOBILE_NOTE,
     "properties": {"action": {"type": "string",
                               "enum": ["boot", "run", "tap", "swipe", "type", "key", "open", "wait"],
                               "description": "Interaction action to perform."},
                    "workspace": {"type": "string", "description": "Absolute path of the project folder."},
                    "platform": {"type": "string", "enum": ["ios", "android"],
                                 "description": "Target platform (optional)."},
                    "device": {"type": "string", "description": "Device name, UDID or adb serial (optional)."},
                    "text": {"type": "string", "description": "Label for 'tap', input for 'type', text for 'wait', "
                                                              "key name for 'key', or direction for 'swipe'."},
                    "id": {"type": "string", "description": "Element identifier for 'tap' or 'type'."},
                    "into": {"type": "string", "description": "For 'type': target field label or hint."},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"],
                                  "description": "For 'swipe': direction (alternative to x, y, x2, y2)."},
                    "x": {"type": "number", "minimum": 0, "description": "X coordinate for 'tap' or swipe start."},
                    "y": {"type": "number", "minimum": 0, "description": "Y coordinate for 'tap' or swipe start."},
                    "x2": {"type": "number", "minimum": 0, "description": "End X coordinate for 'swipe'."},
                    "y2": {"type": "number", "minimum": 0, "description": "End Y coordinate for 'swipe'."},
                    "url": {"type": "string", "description": "URL or deep link for 'open'."},
                    "path": {"type": "string", "description": "For 'run': an .app, an .apk or a folder with one, "
                                                            "e.g. Xcode's …/Build/Products/Debug-iphonesimulator "
                                                            "(default: the project's newest build)."},
                    "timeout": {"type": "number", "minimum": 0.5, "maximum": 300,
                                "description": "For 'wait': timeout in seconds (default 15)."},
                    "nth": {"type": "integer", "minimum": 1, "description": "For 'tap': 1-based match index."},
                    "long": {"type": "boolean", "description": "For 'tap': press and hold (default false)."},
                    "gone": {"type": "boolean", "description": "For 'wait': wait until text disappears."},
                    "shot": {"type": "boolean", "description": "Take a screenshot after the action."}},
     "required": ["action", "workspace"], "annotations": DRIVES_DEVICE},
]
BY_NAME = {t["name"]: t for t in TOOLS}  # every tool answers a call — one whose script is missing says so


def available() -> List[Dict[str, Any]]:
    """The tools whose script is installed (a module left out at install time takes its tools along)."""
    return [t for t in TOOLS if not t["script"] or os.path.isfile(SCRIPTS[t["script"]])]


def tool_list() -> List[dict]:
    out = []
    for t in available():
        out.append({"name": t["name"], "description": t["description"],
                    "inputSchema": {"type": "object", "properties": t["properties"], "required": t["required"],
                                    "additionalProperties": False},
                    "annotations": dict(READ_ONLY, **(t.get("annotations") or {}))})
    return out


GUIDE = (  # what instructions() says, for each tool that is there
    ("lsp_def", "For code symbols use lsp_def / lsp_refs / lsp_calls / lsp_hover / lsp_impl / lsp_type with file, "
                "1-based line and name — not rg, which can't tell a call from a comment. lsp_symbols outlines a "
                "file, lsp_find finds a symbol by name, lsp_check lists type errors, lsp_rename previews (or applies "
                "with apply=true) cross-file renames."),
    ("memory_search", "memory_search searches saved memories."),
    ("git_state", "git_state checks repo state."),
    ("quota", "quota shows Antigravity's limits."),
    ("ci_status", "ci_status and ci_failure_logs inspect CI."),
    ("deps_audit", "deps_audit audits dependencies."),
    ("ui_check", "ui_check checks web pages in headless Chrome."),
    ("mobile_ui", "mobile_ui inspects and mobile_act drives simulators/emulators."),
)


def instructions() -> str:
    have = [t["name"] for t in available()]
    writes = [n for n in ("lsp_rename", "mobile_act") if n in have]
    shots = [n for n in ("mobile_ui", "ui_check") if n in have]
    return " ".join(
        [f"Antigravity Plus tools ({len(have)})."] + [text for name, text in GUIDE if name in have]
        + [("All are read-only except " + " and ".join(writes) + "." if writes else "All are read-only.")
           + (f" {' and '.join(shots)} save{'s' if len(shots) == 1 else ''} screenshots." if shots else "")])


# ------------------------------------------------------------------------------------------------- the protocol

class Server:
    def __init__(self, out) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.protocol = PROTOCOLS[0]

    def send(self, msg: dict) -> None:
        # ASCII JSON: a lone surrogate from the request (\ud800) stays an escape instead of breaking the encoding
        try:
            data = json.dumps(msg, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as e:  # the request still gets an answer
            log(f"a reply couldn't be encoded: {e!r}")
            mid = msg.get("id")
            try:
                json.dumps(mid)
            except (TypeError, ValueError, RecursionError):
                mid = None
            data = json.dumps({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603, "message": f"Internal error: the reply couldn't be encoded ({e})"}}, ensure_ascii=True)
        with self.lock:
            try:
                self.out.write((data + "\n").encode("utf-8"))
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
        except (ValueError, RecursionError):  # RecursionError: nested deeper than the parser goes
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
                             "serverInfo": {"name": NAME, "version": version()}, "instructions": instructions()})
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
        texts = out if isinstance(out, list) else [out]  # a json=true result: the JSON, then stderr on its own
        self.reply(mid, {"content": [{"type": "text", "text": str(t)} for t in texts], "isError": code != 0})

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
        print("\n".join(t["name"] for t in available()))
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
