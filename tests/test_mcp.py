"""MCP server (mcp/server.py + mcp_config.json): JSON-RPC 2.0 protocol, tool schemas, git_state, memory_search,
quota (HTTP + proxy bypass + errors), all 7 lsp_* tools against fake_lsp.py, bad arguments, missing scripts, and
clean EOF shutdown."""
from __future__ import annotations

import http.server
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PLUGIN, SP = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(PLUGIN, "mcp", "server.py")
MCP_CFG = os.path.join(PLUGIN, "mcp_config.json")
LSP = os.path.join(PLUGIN, "skills", "lsp", "scripts", "lsp.py")
MEMORY_PY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
FAKE_LSP = os.path.join(HERE, "probe", "fake_lsp.py")
HOME = os.path.expanduser("~")
LSP_STATE = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp")
LSP_CFG = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp.json")

EXPECTED_TOOLS = [
    "lsp_def",
    "lsp_refs",
    "lsp_calls",
    "lsp_hover",
    "lsp_symbols",
    "lsp_find",
    "lsp_check",
    "memory_search",
    "git_state",
    "quota",
]

ok = True


def check(name: str, cond: Any, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    msg = f"{'✅' if cond else '❌'} {name}"
    if not cond and detail != "":
        msg += f" → {str(detail)[-1500:]}"
    print(msg)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def lsp_states() -> List[dict]:
    out = []
    if os.path.isdir(LSP_STATE):
        for n in sorted(os.listdir(LSP_STATE)):
            if n.endswith(".json"):
                with open(os.path.join(LSP_STATE, n), encoding="utf-8") as f:
                    out.append(json.load(f))
    return out


class McpClient:
    """Line-delimited JSON-RPC 2.0 client over real stdio pipes."""

    def __init__(self, server_py: str, env: Optional[dict] = None, cwd: Optional[str] = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", server_py],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.err_lines: List[str] = []
        self._next_id = 1
        self._t_out = threading.Thread(target=self._read_out, daemon=True)
        self._t_err = threading.Thread(target=self._read_err, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _read_out(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self.lines.put(None)
                return
            self.lines.put(line)

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        while True:
            line = self.proc.stderr.readline()
            if not line:
                return
            self.err_lines.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send_json(self, obj: Any) -> None:
        self.send_raw((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def read_msg(self, timeout: float = 30.0) -> dict:
        raw = self.lines.get(timeout=timeout)
        if raw is None:
            raise EOFError("MCP server closed stdout unexpectedly")
        return json.loads(raw.decode("utf-8"))

    def request(self, method: str, params: Any = None, timeout: float = 30.0, req_id: Any = None) -> dict:
        mid = self._next_id if req_id is None else req_id
        if req_id is None:
            self._next_id += 1
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_json(msg)
        return self.read_msg(timeout=timeout)

    def notify(self, method: str, params: Any = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_json(msg)

    def initialize(self, version: str = "2025-06-18") -> dict:
        rep = self.request(
            "initialize",
            {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1.0"}},
        )
        self.notify("notifications/initialized")
        return rep

    def call_tool(self, name: str, arguments: Any = None, timeout: float = 60.0) -> Tuple[bool, str, dict]:
        params: Dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        rep = self.request("tools/call", params, timeout=timeout)
        res = rep.get("result") or {}
        content = res.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        return bool(res.get("isError")), text, rep

    def close(self, timeout: float = 10.0) -> int:
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        rc = self.proc.wait(timeout=timeout)
        self._t_out.join(timeout=2)
        self._t_err.join(timeout=2)
        return rc

    def stderr_text(self) -> str:
        return "\n".join(self.err_lines)


# ------------------------------------------------------------------------------------------------- 1. config & CLI

with open(MCP_CFG, encoding="utf-8") as f:
    cfg = json.load(f)
tools_cfg = ((cfg.get("mcpServers") or {}).get("tools")) or {}
check(
    "mcp_config.json defines `tools` with python3 -X utf8 ${PLUGIN_ROOT}/mcp/server.py",
    tools_cfg.get("command") == "python3"
    and tools_cfg.get("args") == ["-X", "utf8", "${PLUGIN_ROOT}/mcp/server.py"],
    cfg,
)

r_list = subprocess.run(
    [sys.executable, "-X", "utf8", SERVER, "--list"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=15,
)
check(
    "server.py --list prints all 10 tool names",
    r_list.returncode == 0 and r_list.stdout.strip().splitlines() == EXPECTED_TOOLS,
    (r_list.returncode, r_list.stdout, r_list.stderr),
)

r_help = subprocess.run(
    [sys.executable, "-X", "utf8", SERVER, "--help"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=15,
)
check("server.py --help exits 0", r_help.returncode == 0 and "mcp_config.json" in r_help.stdout, r_help.stdout)


# ------------------------------------------------------------------------------------------------- 2. protocol negotiation & errors

cli = McpClient(SERVER)
try:
    rep_24 = cli.request(
        "initialize",
        {"protocolVersion": "2024-11-05", "clientInfo": {"name": "c", "version": "1"}},
    )
    res_24 = rep_24.get("result") or {}
    check(
        "initialize negotiates supported version (2024-11-05)",
        res_24.get("protocolVersion") == "2024-11-05"
        and (res_24.get("serverInfo") or {}).get("name") == "antigravity-plus"
        and (res_24.get("capabilities") or {}).get("tools") == {"listChanged": False}
        and bool(res_24.get("instructions")),
        rep_24,
    )

    rep_unk = cli.request("initialize", {"protocolVersion": "2099-01-01"})
    check(
        "initialize falls back to newest supported version on unknown protocolVersion",
        (rep_unk.get("result") or {}).get("protocolVersion") == "2025-06-18",
        rep_unk,
    )

    cli.notify("notifications/initialized")
    cli.send_raw(b"   \n")
    rep_ping = cli.request("ping")
    check(
        "notifications/initialized and blank lines emit no reply; ping returns {}",
        rep_ping.get("result") == {} and "error" not in rep_ping,
        rep_ping,
    )

    rep_res = cli.request("resources/list")
    rep_tpl = cli.request("resources/templates/list")
    rep_prm = cli.request("prompts/list")
    check(
        "resources/list, resources/templates/list, prompts/list return empty lists",
        rep_res.get("result") == {"resources": []}
        and rep_tpl.get("result") == {"resourceTemplates": []}
        and rep_prm.get("result") == {"prompts": []},
        (rep_res, rep_tpl, rep_prm),
    )

    rep_unk_m = cli.request("nope/method")
    check("unknown method returns -32601", (rep_unk_m.get("error") or {}).get("code") == -32601, rep_unk_m)

    cli.send_raw(b"{broken json\n")
    rep_parse = cli.read_msg()
    check(
        "invalid JSON returns -32700 with id null",
        rep_parse.get("id") is None and (rep_parse.get("error") or {}).get("code") == -32700,
        rep_parse,
    )

    cli.send_json({"jsonrpc": "1.0", "id": 99, "method": "ping"})
    rep_bad_req = cli.read_msg()
    check(
        "invalid JSON-RPC version returns -32600",
        rep_bad_req.get("id") == 99 and (rep_bad_req.get("error") or {}).get("code") == -32600,
        rep_bad_req,
    )

    cli.send_json([])
    rep_empty_batch = cli.read_msg()
    check(
        "empty batch returns -32600",
        (rep_empty_batch.get("error") or {}).get("code") == -32600,
        rep_empty_batch,
    )

    rep_bad_params = cli.request("ping", params=[1, 2])
    check(
        "non-object params returns -32602",
        (rep_bad_params.get("error") or {}).get("code") == -32602,
        rep_bad_params,
    )

    rep_unk_tool = cli.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    rep_bad_args = cli.request("tools/call", {"name": "quota", "arguments": ["bad"]})
    check(
        "tools/call unknown tool and non-object arguments return -32602",
        (rep_unk_tool.get("error") or {}).get("code") == -32602
        and (rep_bad_args.get("error") or {}).get("code") == -32602,
        (rep_unk_tool, rep_bad_args),
    )

    # 3. tools/list schema sanity
    rep_tools = cli.request("tools/list")
    listed = (rep_tools.get("result") or {}).get("tools") or []
    schema_ok = len(listed) == len(EXPECTED_TOOLS) and [t.get("name") for t in listed] == EXPECTED_TOOLS
    for t in listed:
        sch = t.get("inputSchema") or {}
        props = sch.get("properties") or {}
        req = sch.get("required") or []
        ann = t.get("annotations") or {}
        schema_ok &= (
            bool(t.get("description"))
            and sch.get("type") == "object"
            and sch.get("additionalProperties") is False
            and isinstance(props, dict)
            and isinstance(req, list)
            and all(k in props for k in req)
            and ann.get("readOnlyHint") is True
            and ann.get("destructiveHint") is False
            and ann.get("idempotentHint") is True
            and ann.get("openWorldHint") is (t.get("name") == "git_state")
        )
    check("tools/list validates schema & annotations for all 10 tools", schema_ok, listed)
finally:
    rc_proto = cli.close()
check("server exits cleanly on EOF (protocol suite)", rc_proto == 0 and "Traceback" not in cli.stderr_text(), cli.stderr_text())


# ------------------------------------------------------------------------------------------------- 4. temp git workspace, memory, lsp_*, quota

PROJ = os.path.realpath(os.path.join(SP, "my proj ü"))
write(os.path.join(PROJ, "pyrightconfig.json"), "{}\n")
write(
    os.path.join(PROJ, "pkg", "service.py"),
    "class XpService:\n"
    "    def __init__(self, session):\n"
    "        self.session = session\n\n"
    "    def award(self, user, amount):\n"
    "        return amount\n\n\n"
    "def helper():\n"
    "    return XpService(None).award(1, 2)\n",
)
write(
    os.path.join(PROJ, "pkg", "comments.py"),
    "from pkg.service import XpService\n\n\n"
    "def create_comment(session):\n"
    "    # award is in this comment\n"
    '    title = "😀 привет"; return XpService(session).award(5, 1)\n\n\n'
    "def delete_comment(session):\n"
    "    return create_comment(session)\n",
)
write(
    os.path.join(PROJ, "pkg", "bad.py"),
    "def broken():\n"
    "    # TODO fix me\n"
    "    return oops\n",
)
write(os.path.join(PROJ, "unresolved", "run.py"), "import service\nservice.XpService(1).award(1, 1)\n")
git("init", "-q", "-b", "main", cwd=PROJ)
git("add", ".", cwd=PROJ)
git("commit", "-qm", "initial commit", cwd=PROJ)
write(os.path.join(PROJ, "notes.txt"), "untracked change\n")

NON_GIT = os.path.realpath(os.path.join(SP, "plain dir"))
os.makedirs(NON_GIT, exist_ok=True)

# Configure fake_lsp.py in temp HOME
os.makedirs(os.path.join(HOME, ".gemini", "antigravity-plus"), exist_ok=True)
with open(LSP_CFG, "w", encoding="utf-8") as f:
    json.dump({"python": {"name": "fakepy", "command": [sys.executable, FAKE_LSP], "ready": "pyright", "startup": 30}}, f)

# Save a project memory and a personal memory in temp HOME
r_mem_paths = subprocess.run(
    [sys.executable, MEMORY_PY, "paths", "--workspace", PROJ],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=15,
)
mem_paths = json.loads(r_mem_paths.stdout)
write(
    os.path.join(mem_paths["project"], "deploy-notes.md"),
    "---\nname: deploy-notes\ndescription: How staging builds run\nmetadata:\n  type: project\n---\n"
    "Run -alpha check before deploying to staging.example.\n",
)
write(
    os.path.join(mem_paths["personal"], "style.md"),
    "---\nname: style\ndescription: Preferred test output\nmetadata:\n  type: feedback\n---\n"
    "Keep MCP responses concise.\n",
)

# In-process fake Language Server HTTP server for quota tests
QUOTA_STATE: Dict[str, Any] = {"mode": "normal"}
ISO_FUTURE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 3600))
ISO_WEEK = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400))


class FakeLsHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

    def do_GET(self) -> None:
        self._send(200, {"status": "ok"} if self.path == "/healthz" else {})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-codeium-csrf-token") != "tok-mcp":
            self._send(403, {"code": "permission_denied", "message": "bad token"})
            return
        mode = QUOTA_STATE["mode"]
        if mode == "empty":
            self._send(200, {"response": {"groups": []}})
            return
        if mode == "exhausted":
            self._send(
                200,
                {
                    "response": {
                        "groups": [
                            {
                                "displayName": "Gemini Models",
                                "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen",
                                "buckets": [
                                    {"window": "weekly", "resetTime": ISO_WEEK},  # omitted remainingFraction => 0
                                    {"window": "5h", "resetTime": ISO_FUTURE, "disabled": True},
                                ],
                            }
                        ]
                    }
                },
            )
            return
        self._send(
            200,
            {
                "response": {
                    "groups": [
                        {
                            "displayName": "Gemini Models",
                            "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen",
                            "buckets": [
                                {"window": "weekly", "remainingFraction": 0.8, "resetTime": ISO_WEEK},
                                {"window": "5h", "remainingFraction": 0.45, "resetTime": ISO_FUTURE},
                            ],
                        },
                        {
                            "displayName": "Claude and GPT models",
                            "description": "Models within this group: Claude Sonnet 5.5",
                            "buckets": [
                                {"window": "weekly", "remainingFraction": 0.004, "resetTime": ISO_WEEK},
                            ],
                        },
                    ]
                }
            },
        )


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLsHandler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
LS_ADDR = f"127.0.0.1:{srv.server_address[1]}"

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
CLOSED_ADDR = f"127.0.0.1:{sock.getsockname()[1]}"
sock.close()

# Set a dead HTTP_PROXY / ALL_PROXY in the server env to verify OPENER bypasses proxies cleanly
tool_env = dict(
    os.environ,
    AGP_LSP_IDLE="60",
    ANTIGRAVITY_LS_ADDRESS=LS_ADDR,
    ANTIGRAVITY_CSRF_TOKEN="tok-mcp",
    HTTP_PROXY="http://127.0.0.1:9",
    http_proxy="http://127.0.0.1:9",
    ALL_PROXY="http://127.0.0.1:9",
)

cli2 = McpClient(SERVER, env=tool_env)
try:
    cli2.initialize()

    # -- git_state
    err, txt, _ = cli2.call_tool("git_state", {"workspace": PROJ})
    check(
        "git_state on temp git repo shows branch, untracked change, and commit",
        not err and "Branch: main" in txt and "notes.txt" in txt and "initial commit" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("git_state", {"workspace": NON_GIT})
    check("git_state on non-git folder returns isError=True", err and "Not a git repository" in txt, txt)

    # -- memory_search
    err, txt, _ = cli2.call_tool("memory_search", {"workspace": PROJ, "query": "staging.example"})
    check(
        "memory_search finds saved project memory",
        not err and "deploy-notes" in txt and "staging.example" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("memory_search", {"workspace": PROJ, "query": "concise"})
    check("memory_search finds saved personal memory", not err and "style" in txt and "concise" in txt, txt)
    err, txt, _ = cli2.call_tool("memory_search", {"workspace": PROJ, "query": "-alpha"})
    check("memory_search handles query starting with '-'", not err and "deploy-notes" in txt, txt)
    err, txt, _ = cli2.call_tool("memory_search", {"workspace": PROJ, "query": "no-such-needle-xyz"})
    check("memory_search returns non-error when nothing matches", not err and "Ничего не найдено" in txt, txt)

    # -- quota (normal, empty, exhausted)
    QUOTA_STATE["mode"] = "normal"
    err, txt, _ = cli2.call_tool("quota", {})
    check(
        "quota returns formatted limits and bypasses proxy env vars",
        not err
        and "Gemini Models" in txt
        and "week 80% left" in txt
        and "5 h 45% left" in txt
        and "0.4% left" in txt
        and "Gemini 4 Carbon" in txt,
        txt,
    )

    QUOTA_STATE["mode"] = "exhausted"
    err, txt, _ = cli2.call_tool("quota", {"workspace": PROJ})
    check(
        "quota reports USED UP and disabled buckets",
        not err and "week 0% left" in txt and "USED UP" in txt and "5 h: not applied now" in txt,
        txt,
    )

    QUOTA_STATE["mode"] = "empty"
    err, txt, _ = cli2.call_tool("quota", {})
    check("quota handles empty groups cleanly", not err and "no quota groups" in txt, txt)

    # -- all 7 lsp_* tools via fake_lsp.py
    err, txt, _ = cli2.call_tool("lsp_def", {"workspace": PROJ, "file": "pkg/comments.py", "line": 6, "name": "award"})
    check(
        "lsp_def resolves method definition from relative path + workspace",
        not err and re.search(r"pkg[/\\]service\.py:5:9\s+def award", txt),
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_refs",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "limit": 0, "include_declaration": True},
    )
    check(
        "lsp_refs returns definition, callers with enclosing functions, and unlinked notice",
        not err
        and "references to award: 3 in 2 files" in txt
        and "[definition]" in txt
        and "— in helper" in txt
        and "— in create_comment" in txt
        and "Not linked by the server" in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_refs",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "include_declaration": False},
    )
    check(
        "lsp_refs with include_declaration=False passes --no-decl",
        not err and "references to award: 2 in 2 files" in txt and "[definition]" not in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_calls",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "depth": 2},
    )
    check(
        "lsp_calls depth=2 shows incoming callers over 2 levels",
        not err and "← helper" in txt and "← create_comment" in txt and "← delete_comment" in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_calls",
        {"workspace": PROJ, "file": "pkg/comments.py", "line": 4, "name": "create_comment", "out": True},
    )
    check(
        "lsp_calls out=True shows outgoing calls",
        not err and ("→ XpService" in txt or "→ award" in txt),
        txt,
    )

    err, txt, _ = cli2.call_tool("lsp_hover", {"file": os.path.join(PROJ, "pkg", "service.py"), "line": 9, "name": "helper"})
    check(
        "lsp_hover returns signature with absolute file and no workspace",
        not err and "def helper():" in txt and "```" not in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool("lsp_symbols", {"workspace": PROJ, "file": "pkg/service.py", "depth": 2, "all": False})
    check(
        "lsp_symbols returns file outline",
        not err and "class XpService  1-6" in txt and "method award  5-6" in txt and "function helper  9-10" in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool("lsp_find", {"workspace": PROJ, "name": "award", "kind": "method", "lang": "python"})
    check(
        "lsp_find finds symbol by name, kind, and lang",
        not err and re.search(r"method XpService\.award\s+pkg[/\\]service\.py:5", txt),
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_check",
        {"workspace": PROJ, "files": ["pkg/bad.py", "pkg/service.py"], "errors_only": False},
    )
    check(
        "lsp_check reports errors and warnings when errors_only=False",
        not err and "1 error, 1 warning" in txt and "a TODO is left" in txt and "oops" in txt and "no problems" in txt,
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_check",
        {"workspace": PROJ, "files": ["pkg/bad.py"], "errors_only": True},
    )
    check(
        "lsp_check omits warnings when errors_only=True",
        not err and "1 errors" in txt and "oops" in txt and "a TODO is left" not in txt,
        txt,
    )

    # -- bad arguments -> isError=True without crashing the server
    bad_cases = [
        ("missing file", "lsp_def", {"line": 1, "name": "x"}, "required"),
        ("relative workspace", "git_state", {"workspace": "rel/dir"}, "absolute"),
        ("missing workspace dir", "git_state", {"workspace": os.path.join(SP, "missing-dir")}, "not a folder"),
        ("relative file without workspace", "lsp_symbols", {"file": "pkg/service.py"}, "absolute"),
        ("missing file on disk", "lsp_symbols", {"workspace": PROJ, "file": "pkg/nope.py"}, "no such file"),
        ("line 0", "lsp_def", {"workspace": PROJ, "file": "pkg/service.py", "line": 0, "name": "helper"}, "between"),
        ("line non-int", "lsp_def", {"workspace": PROJ, "file": "pkg/service.py", "line": "abc", "name": "helper"}, "integer"),
        ("line float", "lsp_def", {"workspace": PROJ, "file": "pkg/service.py", "line": 1.5, "name": "helper"}, "integer"),
        ("line bool", "lsp_def", {"workspace": PROJ, "file": "pkg/service.py", "line": True, "name": "helper"}, "integer"),
        ("name with space", "lsp_hover", {"workspace": PROJ, "file": "pkg/service.py", "line": 9, "name": "a b"}, "without spaces"),
        ("calls depth out of range", "lsp_calls", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "depth": 9}, "between 1 and 5"),
        ("empty files list", "lsp_check", {"workspace": PROJ, "files": []}, "non-empty list"),
        ("non-string item in files", "lsp_check", {"workspace": PROJ, "files": [123]}, "string"),
    ]
    bad_ok = True
    bad_fail: Any = None
    for label, tname, targs, needle in bad_cases:
        err, txt, _ = cli2.call_tool(tname, targs)
        if not err or needle not in txt:
            bad_ok = False
            bad_fail = (label, err, txt)
            break
    check("bad tool arguments return isError=True with clear messages", bad_ok, bad_fail)

    rep_after = cli2.request("ping")
    check("server remains responsive after bad tool calls", rep_after.get("result") == {}, rep_after)
finally:
    rc_tools = cli2.close()
    subprocess.run(
        [sys.executable, LSP, "stop", "--all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    srv.shutdown()
    srv.server_close()

check("server exits cleanly on EOF (tools suite)", rc_tools == 0 and "Traceback" not in cli2.stderr_text(), cli2.stderr_text())
check("no fake_lsp daemon left running after stop --all", not lsp_states(), lsp_states())


# ------------------------------------------------------------------------------------------------- 5. quota unreachable / HTTP 403 + missing optional scripts

# Unreachable LS
cli_down = McpClient(SERVER, env=dict(os.environ, ANTIGRAVITY_LS_ADDRESS=CLOSED_ADDR, ANTIGRAVITY_CSRF_TOKEN="tok-mcp"))
try:
    cli_down.initialize()
    err, txt, _ = cli_down.call_tool("quota", {})
    check("quota reports error cleanly when LS is unreachable", err and "couldn't ask Antigravity for the quota" in txt, txt)
finally:
    cli_down.close()

# HTTP 403 with http:// prefix in ANTIGRAVITY_LS_ADDRESS
srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLsHandler)
srv2.daemon_threads = True
threading.Thread(target=srv2.serve_forever, daemon=True).start()
cli_403 = McpClient(
    SERVER,
    env=dict(os.environ, ANTIGRAVITY_LS_ADDRESS=f"http://127.0.0.1:{srv2.server_address[1]}", ANTIGRAVITY_CSRF_TOKEN="wrong"),
)
try:
    cli_403.initialize()
    err, txt, _ = cli_403.call_tool("quota", {})
    check("quota reports HTTP 403 cleanly and accepts http:// in ANTIGRAVITY_LS_ADDRESS", err and "HTTP 403" in txt, txt)
finally:
    cli_403.close()
    srv2.shutdown()
    srv2.server_close()

# Missing optional scripts (temp plugin copy without skills/lsp and skills/git)
partial = os.path.join(SP, "partial-plugin")
for rel in ("plugin.json", "mcp/server.py", "hooks/compat.py"):
    src = os.path.join(PLUGIN, *rel.split("/"))
    dst = os.path.join(partial, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

cli_partial = McpClient(os.path.join(partial, "mcp", "server.py"))
try:
    cli_partial.initialize()
    err_lsp, txt_lsp, _ = cli_partial.call_tool(
        "lsp_def",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"},
    )
    err_git, txt_git, _ = cli_partial.call_tool("git_state", {"workspace": PROJ})
    rep_p = cli_partial.request("ping")
    check(
        "missing optional scripts return graceful isError=True without crashing server",
        err_lsp
        and "isn't installed" in txt_lsp
        and err_git
        and "isn't installed" in txt_git
        and rep_p.get("result") == {},
        (txt_lsp, txt_git, rep_p),
    )
finally:
    rc_partial = cli_partial.close()
check("partial plugin server exits cleanly on EOF", rc_partial == 0, rc_partial)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
