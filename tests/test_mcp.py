"""MCP server (mcp/server.py + mcp_config.json): JSON-RPC 2.0 protocol (lone surrogates included), all 19 tool
schemas & annotations, git_state, memory_search, quota (HTTP + proxy bypass + errors), all 10 lsp_* tools (including
impl, type, rename preview vs apply), ci_status, ci_failure_logs, deps_audit, ui_check, mobile_ui, mobile_act, bad
arguments, missing scripts (left out of tools/list, still answered), PNG-only screenshot paths, build paths outside
the workspace, time limits sized from each script's worst case, a timeout that keeps what was printed, head + tail
cuts that keep stderr's end, json=true results without stderr, and clean EOF shutdown."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import queue
import re
import shutil
import signal
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
WIN = os.name == "nt"

EXPECTED_TOOLS = [
    "lsp_def",
    "lsp_refs",
    "lsp_calls",
    "lsp_hover",
    "lsp_impl",
    "lsp_type",
    "lsp_symbols",
    "lsp_find",
    "lsp_check",
    "lsp_rename",
    "memory_search",
    "git_state",
    "quota",
    "ci_status",
    "ci_failure_logs",
    "deps_audit",
    "ui_check",
    "mobile_ui",
    "mobile_act",
]
MUTATING_TOOLS = {"lsp_rename", "mobile_act"}
DESTRUCTIVE_TOOLS = {"lsp_rename"}  # apply=true rewrites files
OPEN_WORLD_TOOLS = {"git_state", "ci_status", "ci_failure_logs", "deps_audit", "ui_check", "mobile_act"}
MAX_OUT = 60000  # mcp/server.py's limit on one tool result

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
    "server.py --list prints all 19 tool names",
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

    # 3. tools/list schema & annotation sanity
    rep_tools = cli.request("tools/list")
    listed = (rep_tools.get("result") or {}).get("tools") or []
    schema_ok = len(listed) == len(EXPECTED_TOOLS) and [t.get("name") for t in listed] == EXPECTED_TOOLS
    by_name = {}
    for t in listed:
        tname = t.get("name")
        by_name[tname] = t
        sch = t.get("inputSchema") or {}
        props = sch.get("properties") or {}
        req = sch.get("required") or []
        ann = t.get("annotations") or {}
        is_mut = tname in MUTATING_TOOLS
        schema_ok &= (
            bool(t.get("description"))
            and sch.get("type") == "object"
            and sch.get("additionalProperties") is False
            and isinstance(props, dict)
            and isinstance(req, list)
            and all(k in props for k in req)
            and ann.get("readOnlyHint") is (not is_mut)
            and ann.get("destructiveHint") is (tname in DESTRUCTIVE_TOOLS)
            and ann.get("idempotentHint") is (not is_mut)
            and ann.get("openWorldHint") is (tname in OPEN_WORLD_TOOLS)
        )
    mob_ui_acts = (((by_name.get("mobile_ui") or {}).get("inputSchema") or {}).get("properties") or {}).get("action", {}).get("enum")
    mob_act_acts = (((by_name.get("mobile_act") or {}).get("inputSchema") or {}).get("properties") or {}).get("action", {}).get("enum")
    check_files = (((by_name.get("lsp_check") or {}).get("inputSchema") or {}).get("properties") or {}).get("files") or {}
    schema_ok &= (
        mob_ui_acts == ["devices", "project", "ui", "shot", "logs"]
        and mob_act_acts == ["boot", "run", "tap", "swipe", "type", "key", "open", "wait"]
        and check_files.get("maxItems") == 50
        and "tools (19)" in str(res_24.get("instructions") or "")
        and "BypassSandbox" in str((by_name.get("mobile_act") or {}).get("description"))
    )
    check("tools/list validates schema & annotations for all 19 tools", schema_ok, listed)

    # a lone UTF-16 surrogate (what JavaScript's JSON.stringify sends for one) still gets an answer, as an escape
    cli.send_raw(b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n')
    rep_sur = cli.read_msg()
    cli.send_raw(b'{"jsonrpc":"2.0","id":"s2","method":"tools/call","params":{"name":"git_state",'
                 b'"arguments":{"workspace":"/no/such\\ud800dir"}}}\n')
    rep_sur2 = cli.read_msg()
    check(
        "a lone surrogate in an id or an argument still gets a reply (ASCII-escaped JSON)",
        rep_sur.get("id") == "\ud800" and rep_sur.get("result") == {}
        and rep_sur2.get("id") == "s2" and (rep_sur2.get("result") or {}).get("isError") is True,
        (rep_sur, rep_sur2),
    )
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
write(os.path.join(PROJ, "web", "index.html"), "<!doctype html><html><body><h1>Hi</h1></body></html>\n")

# Fake Go LSP for lsp_impl and lsp_type end-to-end tests
GO_PROJ = os.path.realpath(os.path.join(SP, "go proj"))
write(os.path.join(GO_PROJ, "go.mod"), "module example.com/demo\n\ngo 1.22\n")
write(
    os.path.join(GO_PROJ, "main.go"),
    "package main\n\n"
    "type Speaker interface {\n"
    "    Speak() string\n"
    "}\n\n"
    "type Dog struct{}\n\n"
    "func (Dog) Speak() string { return \"woof\" }\n",
)
FAKE_GO_LSP = os.path.join(SP, "fake_go_lsp.py")
write(
    FAKE_GO_LSP,
    r'''#!/usr/bin/env python3
import json, os, sys, urllib.parse
from pathlib import Path

ROOT = ""

def send(msg):
    data = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()

def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                break
            continue
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            length = int(v)
    return json.loads(sys.stdin.buffer.read(length))

while True:
    msg = read()
    if msg is None:
        break
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method == "initialize":
        p = urllib.parse.unquote(urllib.parse.urlparse(params["rootUri"]).path)
        if os.name == "nt" and len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        ROOT = os.path.realpath(os.path.normpath(p))
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {
            "definitionProvider": True, "implementationProvider": True, "typeDefinitionProvider": True,
            "documentSymbolProvider": True, "textDocumentSync": 1}}})
    elif method == "initialized":
        send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "Found 1 source files"}})
    elif method == "exit":
        break
    elif rid is not None:
        uri = Path(os.path.join(ROOT, "main.go")).as_uri()
        if method == "textDocument/implementation":
            res = [{"uri": uri, "range": {"start": {"line": 8, "character": 11}, "end": {"line": 8, "character": 16}}}]
        elif method == "textDocument/typeDefinition":
            res = [{"uri": uri, "range": {"start": {"line": 2, "character": 5}, "end": {"line": 2, "character": 12}}}]
        elif method == "textDocument/documentSymbol":
            res = []
        else:
            res = None
        send({"jsonrpc": "2.0", "id": rid, "result": res})
''',
)

# Configure fake_lsp.py and fake_go_lsp.py in temp HOME
os.makedirs(os.path.join(HOME, ".gemini", "antigravity-plus"), exist_ok=True)
with open(LSP_CFG, "w", encoding="utf-8") as f:
    json.dump(
        {
            "python": {"name": "fakepy", "command": [sys.executable, FAKE_LSP], "ready": "pyright", "startup": 30},
            "go": {"name": "fakego", "command": [sys.executable, FAKE_GO_LSP], "ready": "pyright", "startup": 30},
        },
        f,
    )

# Fake CLI shims (gh, pip-audit) + temp CI / deps / mobile projects
FAKE_BIN = os.path.join(SP, "fake_bin")
os.makedirs(FAKE_BIN, exist_ok=True)


def make_exec(name: str, target_script: str) -> str:
    path = os.path.join(FAKE_BIN, name)
    if WIN:
        cmd_path = path + ".cmd"
        write(cmd_path, f'@echo off\r\n"{sys.executable}" "{target_script}" {name} %*\r\n')
        return cmd_path
    write(
        path,
        f"#!{sys.executable}\nimport runpy, sys\nsys.argv = [sys.argv[0], {name!r}, *sys.argv[1:]]\nrunpy.run_path({target_script!r}, run_name='__main__')\n",
    )
    os.chmod(path, 0o755)
    return path


FAKE_TOOLS_PY = os.path.join(SP, "fake_tools.py")
write(
    FAKE_TOOLS_PY,
    r'''import json, os, sys
tool, argv = sys.argv[1], sys.argv[2:]
if tool == "gh":
    if argv[:2] == ["auth", "status"]:
        sys.stdout.write("Logged in to github.com\n")
        sys.exit(0)
    if argv[:2] == ["run", "list"]:
        sys.stdout.write(json.dumps([{
            "databaseId": 42, "workflowName": "CI", "name": "CI", "displayTitle": "ci test",
            "event": "push", "headBranch": "main", "headSha": "deadbeef0000", "status": "completed",
            "conclusion": "failure", "attempt": 1, "createdAt": "2026-11-14T14:00:00Z",
            "startedAt": "2026-11-14T14:00:02Z", "updatedAt": "2026-11-14T14:01:12Z",
            "url": "https://github.com/example/demo/actions/runs/42",
        }]))
        sys.exit(0)
    if argv[:2] == ["run", "view"] and "--log-failed" in argv:
        sys.stdout.write(
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01Z ##[group]Run pytest\n"
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01Z   INTERNAL_CI_VAR: secret\n"
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01Z ##[endgroup]\n"
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:02Z PASS tests/test_ok.py\n"
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03Z FAIL tests/test_bad.py\n"
            "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03Z AssertionError: expected 200 got 500\n"
        )
        sys.exit(0)
    if argv[:2] == ["run", "view"]:
        sys.stdout.write(json.dumps({
            "workflowName": "CI", "status": "completed", "conclusion": "failure",
            "url": "https://github.com/example/demo/actions/runs/42",
            "jobs": [{"databaseId": 7, "name": "test (ubuntu-latest)", "status": "completed",
                      "conclusion": "failure", "steps": [{"name": "Run pytest", "conclusion": "failure"}]}],
        }))
        sys.exit(0)
elif tool == "pip-audit":
    if os.environ.get("AGP_TEST_NO_AUDIT") == "1":
        sys.exit(2)
    sys.stdout.write(json.dumps({"dependencies": [{
        "name": "requests", "version": "2.31.0",
        "vulns": [{"id": "PYSEC-2023-74", "fix_versions": ["2.32.0"],
                   "aliases": ["CVE-2023-32681"], "description": "Header leak on redirect."}],
    }]}))
    sys.exit(1)
''',
)
make_exec("gh", FAKE_TOOLS_PY)
make_exec("pip-audit", FAKE_TOOLS_PY)

CI_PROJ = os.path.realpath(os.path.join(SP, "ci repo"))
write(os.path.join(CI_PROJ, ".github", "workflows", "ci.yml"), "name: CI\non: [push, pull_request]\njobs:\n  t:\n    runs-on: ubuntu-latest\n")
write(os.path.join(HOME, ".config", "gh", "hosts.yml"), "github.com:\n  user: test-user\n")
git("init", "-q", "-b", "main", cwd=CI_PROJ)
git("remote", "add", "origin", "https://github.com/example/demo.git", cwd=CI_PROJ)
git("add", ".", cwd=CI_PROJ)
git("commit", "-qm", "init ci", cwd=CI_PROJ)

DEPS_PROJ = os.path.realpath(os.path.join(SP, "deps proj"))
write(os.path.join(DEPS_PROJ, "requirements.txt"), "requests==2.31.0\n")

MOB_PROJ = os.path.realpath(os.path.join(SP, "mob proj"))
write(os.path.join(MOB_PROJ, "android", "settings.gradle.kts"), 'rootProject.name = "Demo"\ninclude(":app")\n')
write(os.path.join(MOB_PROJ, "android", "app", "build.gradle.kts"), 'plugins { id("com.android.application") }\nandroid { defaultConfig { applicationId = "com.example.demo" } }\n')
write(os.path.join(MOB_PROJ, "android", "app", "src", "main", "AndroidManifest.xml"), '<manifest><application><activity><intent-filter><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity></application></manifest>\n')

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
    PATH=FAKE_BIN + os.pathsep + os.environ.get("PATH", ""),
    APPDATA=os.path.join(SP, "appdata"),
    XDG_CONFIG_HOME=os.path.join(SP, "xdg"),
    AGP_LSP_IDLE="60",
    ANTIGRAVITY_LS_ADDRESS=LS_ADDR,
    ANTIGRAVITY_CSRF_TOKEN="tok-mcp",
    HTTP_PROXY="http://127.0.0.1:9",
    http_proxy="http://127.0.0.1:9",
    ALL_PROXY="http://127.0.0.1:9",
)
for _v in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GITLAB_TOKEN", "GLAB_TOKEN", "GLAB_CONFIG_DIR"):
    tool_env.pop(_v, None)
os.makedirs(tool_env["APPDATA"], exist_ok=True)
os.makedirs(tool_env["XDG_CONFIG_HOME"], exist_ok=True)

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

    # -- lsp_impl, lsp_type (via fake_go_lsp + fake_lsp capability fallback), lsp_rename (preview vs apply)
    err, txt, _ = cli2.call_tool("lsp_impl", {"workspace": GO_PROJ, "file": "main.go", "line": 4, "name": "Speak"})
    check(
        "lsp_impl returns implementations from capable language server",
        not err and "implementations of Speak" in txt and "main.go:9" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("lsp_type", {"workspace": GO_PROJ, "file": "main.go", "line": 9, "name": "Dog"})
    check(
        "lsp_type returns type definition from capable language server",
        not err and "type definition of Dog" in txt and "main.go:3" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("lsp_impl", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"})
    check("lsp_impl reports unsupported server capability cleanly", err and "can't answer" in txt, txt)

    err, txt, _ = cli2.call_tool(
        "lsp_rename",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "new_name": "grant_xp"},
    )
    with open(os.path.join(PROJ, "pkg", "comments.py"), encoding="utf-8") as f:
        untouched_comments = f.read()
    check(
        "lsp_rename without apply previews edits and leaves files untouched",
        not err and "rename award → grant_xp" in txt and "Preview only" in txt and "award(5, 1)" in untouched_comments,
        txt,
    )

    err, txt, _ = cli2.call_tool(
        "lsp_rename",
        {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "new_name": "grant_xp", "apply": True},
    )
    with open(os.path.join(PROJ, "pkg", "service.py"), encoding="utf-8") as f:
        svc_after = f.read()
    with open(os.path.join(PROJ, "pkg", "comments.py"), encoding="utf-8") as f:
        cmt_after = f.read()
    check(
        "lsp_rename with apply=True rewrites all linked files cleanly",
        not err and "Applied:" in txt and "def grant_xp(" in svc_after and "grant_xp(5, 1)" in cmt_after,
        (txt, svc_after, cmt_after),
    )
    git("checkout", "-q", "--", ".", cwd=PROJ)

    # -- ci_status & ci_failure_logs (real ci.py + fake gh)
    err, txt, _ = cli2.call_tool("ci_status", {"workspace": CI_PROJ, "branch": "main", "limit": 5})
    check(
        "ci_status reports runs and failed job from ci.py",
        not err and "CI failed" in txt and "#42" in txt and "test (ubuntu-latest)" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("ci_status", {"workspace": CI_PROJ, "json": True})
    ci_json_ok = not err
    try:
        ci_parsed = json.loads(txt)
        runs = ci_parsed.get("runs")
        ci_json_ok &= isinstance(runs, list) and bool(runs) and runs[0].get("id") == "42"
    except ValueError:
        ci_json_ok = False
    check("ci_status json=True returns structured run JSON", ci_json_ok, txt)

    err, txt, _ = cli2.call_tool(
        "ci_failure_logs",
        {"workspace": CI_PROJ, "run": "42", "job": "ubuntu", "lines": 50, "full": False},
    )
    check(
        "ci_failure_logs fetches and cleans failed logs",
        not err and "Failed logs" in txt and "AssertionError: expected 200 got 500" in txt and "INTERNAL_CI_VAR" not in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("ci_failure_logs", {"workspace": NON_GIT})
    check("ci_failure_logs on non-CI folder returns isError=True", err and "Cannot fetch CI logs" in txt, txt)

    # -- deps_audit (real deps.py + fake pip-audit)
    err, txt, _ = cli2.call_tool("deps_audit", {"workspace": DEPS_PROJ, "mode": "scan"})
    check("deps_audit mode=scan inspects manifests and tools offline", not err and "Python" in txt and "pip-audit ✓" in txt, txt)

    err, txt, _ = cli2.call_tool("deps_audit", {"workspace": DEPS_PROJ, "mode": "audit"})
    check(
        "deps_audit mode=audit returns findings with isError=False",
        not err and "PYSEC-2023-74" in txt and "requests" in txt,
        txt,
    )
    err, txt, _ = cli2.call_tool("deps_audit", {"workspace": DEPS_PROJ, "mode": "audit", "json": True})
    deps_json_ok = not err
    try:
        deps_parsed = json.loads(txt)
        deps_json_ok &= (deps_parsed.get("summary") or {}).get("vulnerabilities") == 1
    except ValueError:
        deps_json_ok = False
    check("deps_audit json=True returns structured report", deps_json_ok, txt)

    # -- mobile_ui action=project (real mobile.py, pure local read)
    err, txt, _ = cli2.call_tool("mobile_ui", {"action": "project", "workspace": MOB_PROJ})
    check(
        "mobile_ui action=project inspects Android project with real mobile.py",
        not err and ":app:assembleDebug" in txt and "com.example.demo" in txt,
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
        ("name leading dash", "lsp_rename", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "--apply", "new_name": "foo", "apply": False}, "must not start with '-'"),
        ("file outside workspace", "lsp_def", {"workspace": PROJ, "file": "../outside.py", "line": 1, "name": "x"}, "stay inside `workspace`"),
        ("rename apply without workspace", "lsp_rename", {"file": os.path.join(PROJ, "pkg", "service.py"), "line": 5, "name": "award", "new_name": "grant_xp", "apply": True}, "`workspace` is required"),
        ("ui_check non-html local file", "ui_check", {"workspace": PROJ, "target": ".env"}, "HTML/SVG"),
        ("ui_check too many widths", "ui_check", {"workspace": PROJ, "target": "web/index.html", "widths": "1,2,3,4,5,6,7,8,9"}, "at most 8"),
        ("calls depth out of range", "lsp_calls", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "depth": 9}, "between 1 and 5"),
        ("empty files list", "lsp_check", {"workspace": PROJ, "files": []}, "non-empty list"),
        ("non-string item in files", "lsp_check", {"workspace": PROJ, "files": [123]}, "string"),
        ("impl missing name", "lsp_impl", {"workspace": PROJ, "file": "pkg/service.py", "line": 5}, "required"),
        ("type space in name", "lsp_type", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "a b"}, "without spaces"),
        ("rename missing new_name", "lsp_rename", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"}, "required"),
        ("rename space in new_name", "lsp_rename", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "new_name": "a b"}, "without spaces"),
        ("rename dash new_name", "lsp_rename", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "new_name": "-bad"}, "without spaces"),
        ("ci_status limit out of range", "ci_status", {"workspace": PROJ, "limit": 99}, "between 1 and 50"),
        ("ci_status dash branch", "ci_status", {"workspace": PROJ, "branch": "-bad"}, "must not start"),
        ("ci_failure_logs lines out of range", "ci_failure_logs", {"workspace": PROJ, "lines": 5}, "between 10 and 2000"),
        ("ci_failure_logs space in run", "ci_failure_logs", {"workspace": PROJ, "run": "1 2"}, "without spaces"),
        ("deps_audit bad mode", "deps_audit", {"workspace": PROJ, "mode": "bogus"}, "must be one of"),
        ("deps_audit bad timeout", "deps_audit", {"workspace": PROJ, "timeout": 1}, "between 5 and 600"),
        ("ui_check bad scheme", "ui_check", {"target": "file:///etc/passwd"}, "http:// or https://"),
        ("ui_check empty host", "ui_check", {"target": "http://"}, "http:// or https://"),
        ("ui_check missing html", "ui_check", {"workspace": PROJ, "target": "web/missing.html"}, "no such file"),
        ("ui_check rel without ws", "ui_check", {"target": "web/index.html"}, "absolute"),
        ("ui_check bad widths", "ui_check", {"workspace": PROJ, "target": "web/index.html", "widths": "390,abc"}, "positive integers"),
        ("ui_check bad scan dir", "ui_check", {"workspace": PROJ, "target": "web/index.html", "scan": "nope-dir"}, "not a folder"),
        ("mobile_ui disallowed shutdown", "mobile_ui", {"action": "shutdown", "workspace": PROJ}, "must be one of"),
        ("mobile_ui bad platform", "mobile_ui", {"action": "ui", "workspace": PROJ, "platform": "win"}, "'ios' or 'android'"),
        ("mobile_ui bad last", "mobile_ui", {"action": "logs", "workspace": PROJ, "last": "forever"}, "duration"),
        ("mobile_act disallowed grant", "mobile_act", {"action": "grant", "workspace": PROJ}, "must be one of"),
        ("mobile_act tap no target", "mobile_act", {"action": "tap", "workspace": PROJ}, "requires"),
        ("mobile_act tap partial xy", "mobile_act", {"action": "tap", "workspace": PROJ, "x": 10}, "both `x` and `y`"),
        ("mobile_act tap neg x", "mobile_act", {"action": "tap", "workspace": PROJ, "x": -5, "y": 10}, "between 0 and 20000"),
        ("mobile_act swipe partial xy", "mobile_act", {"action": "swipe", "workspace": PROJ, "x": 1, "y": 2}, "all four"),
        ("mobile_act swipe bad dir", "mobile_act", {"action": "swipe", "workspace": PROJ, "direction": "diagonal"}, "up, down, left, or right"),
        ("mobile_act type empty", "mobile_act", {"action": "type", "workspace": PROJ, "text": ""}, "required"),
        ("mobile_act key spaces", "mobile_act", {"action": "key", "workspace": PROJ, "text": "a b"}, "without spaces"),
        ("mobile_act bad url", "mobile_act", {"action": "open", "workspace": PROJ, "url": "not-a-url"}, "valid URL"),
        ("mobile_act bad wait timeout", "mobile_act", {"action": "wait", "workspace": PROJ, "text": "ok", "timeout": 0.1}, "between 0.5 and 300"),
        ("lsp_check over 50 files", "lsp_check", {"workspace": PROJ, "files": ["pkg/service.py"] * 51}, "at most 50"),
        ("mobile_ui shot onto a source file", "mobile_ui", {"action": "shot", "workspace": PROJ, "out": "pkg/service.py"}, "must be a .png"),
        ("mobile_act run path that isn't a build", "mobile_act", {"action": "run", "workspace": PROJ, "path": "notes.txt"}, "must be an .app bundle"),
        ("mobile_act run missing build", "mobile_act", {"action": "run", "workspace": PROJ, "path": os.path.join(SP, "no-such", "Demo.app")}, "no such build path"),
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

# Missing optional scripts (temp plugin copy without any skills/)
partial = os.path.join(SP, "partial-plugin")
for rel in ("plugin.json", "mcp/server.py", "hooks/compat.py"):
    src = os.path.join(PLUGIN, *rel.split("/"))
    dst = os.path.join(partial, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

cli_partial = McpClient(os.path.join(partial, "mcp", "server.py"))
try:
    init_partial = cli_partial.initialize()
    rep_pl = cli_partial.request("tools/list")
    partial_names = [t.get("name") for t in ((rep_pl.get("result") or {}).get("tools") or [])]
    partial_instr = str((init_partial.get("result") or {}).get("instructions") or "")
    check(
        "tools/list and the instructions leave out tools whose script isn't installed",
        partial_names == ["quota"] and "tools (1)" in partial_instr and "lsp_def" not in partial_instr
        and "mobile_act" not in partial_instr,
        (partial_names, partial_instr),
    )
    missing_calls = [
        ("lsp_def", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"}),
        ("lsp_impl", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"}),
        ("lsp_type", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award"}),
        ("lsp_rename", {"workspace": PROJ, "file": "pkg/service.py", "line": 5, "name": "award", "new_name": "g"}),
        ("git_state", {"workspace": PROJ}),
        ("ci_status", {"workspace": PROJ}),
        ("ci_failure_logs", {"workspace": PROJ}),
        ("deps_audit", {"workspace": PROJ}),
        ("ui_check", {"target": "http://127.0.0.1:8080"}),
        ("mobile_ui", {"action": "devices", "workspace": PROJ}),
        ("mobile_act", {"action": "tap", "workspace": PROJ, "text": "OK"}),
    ]
    missing_ok = True
    missing_fail: Any = None
    for tname, targs in missing_calls:
        e, t, _ = cli_partial.call_tool(tname, targs)
        if not e or "isn't installed" not in t:
            missing_ok = False
            missing_fail = (tname, e, t)
            break
    rep_p = cli_partial.request("ping")
    check(
        "missing optional scripts return graceful isError=True for all script tools without crashing server",
        missing_ok and rep_p.get("result") == {},
        (missing_fail, rep_p),
    )
finally:
    rc_partial = cli_partial.close()
check("partial plugin server exits cleanly on EOF", rc_partial == 0, rc_partial)


# ------------------------------------------------------------------------------------------------- 6. mock-plugin argv tests for ui_check, mobile_ui, mobile_act
mock_plug = os.path.join(SP, "mock-plugin")
for rel in ("plugin.json", "mcp/server.py", "hooks/compat.py"):
    src = os.path.join(PLUGIN, *rel.split("/"))
    dst = os.path.join(mock_plug, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

SHIM_SRC = r'''import json, os, sys
print(json.dumps({"script": os.path.basename(__file__), "argv": sys.argv[1:], "cwd": os.getcwd()}))
'''
write(os.path.join(mock_plug, "skills", "ui-check", "scripts", "ui_check.py"), SHIM_SRC)
write(os.path.join(mock_plug, "skills", "mobile", "scripts", "mobile.py"), SHIM_SRC)

cli_mock = McpClient(os.path.join(mock_plug, "mcp", "server.py"))
try:
    init_mock = cli_mock.initialize()
    rep_ml = cli_mock.request("tools/list")
    mock_names = [t.get("name") for t in ((rep_ml.get("result") or {}).get("tools") or [])]
    check(
        "tools/list offers just the tools whose scripts are there (ui_check, mobile_*) plus quota",
        mock_names == ["quota", "ui_check", "mobile_ui", "mobile_act"]
        and "tools (4)" in str((init_mock.get("result") or {}).get("instructions") or ""),
        (mock_names, init_mock),
    )

    def call_mock(name: str, arguments: dict) -> dict:
        err, txt, _ = cli_mock.call_tool(name, arguments)
        if err:
            raise AssertionError(f"{name} returned error: {txt}")
        return json.loads(txt)

    res_up = call_mock("ui_check", {"target": "HTTPS://Example.com/App?q=1"})
    check("ui_check lowercases the URL scheme (ui_check.py knows URLs by a lowercase one)",
          res_up["argv"][0] == "https://Example.com/App?q=1", res_up)

    # ui_check: relative HTML file, default widths
    res_u1 = call_mock("ui_check", {"workspace": PROJ, "target": "web/index.html"})
    check(
        "ui_check forwards resolved file path and default widths",
        res_u1["argv"] == [os.path.join(PROJ, "web", "index.html"), "--widths", "390,1440"] and os.path.realpath(res_u1["cwd"]) == PROJ,
        res_u1,
    )
    # ui_check: URL + widths + height + scan + out
    res_u2 = call_mock(
        "ui_check",
        {"workspace": PROJ, "target": "https://example.com/app", "widths": "390, 768, 1440", "height": 1200, "scan": "web", "out": "ui-out"},
    )
    check(
        "ui_check forwards URL, widths, height, out, and scan",
        res_u2["argv"] == [
            "https://example.com/app",
            "--widths",
            "390,768,1440",
            "--height",
            "1200",
            f"--out={os.path.join(PROJ, 'ui-out')}",
            f"--scan={os.path.join(PROJ, 'web')}",
        ],
        res_u2,
    )

    # mobile_ui: all 5 actions
    m_dev = call_mock("mobile_ui", {"action": "devices", "workspace": MOB_PROJ, "platform": "ios"})
    m_prj = call_mock("mobile_ui", {"action": "project", "workspace": MOB_PROJ})
    m_ui = call_mock("mobile_ui", {"action": "ui", "workspace": MOB_PROJ, "platform": "android", "device": "emu-1", "find": "Sign In", "all": True, "json": True})
    m_shot = call_mock("mobile_ui", {"action": "shot", "workspace": MOB_PROJ, "platform": "ios", "out": "shots/s.png"})
    m_logs = call_mock("mobile_ui", {"action": "logs", "workspace": MOB_PROJ, "platform": "ios", "app": "com.example.demo", "last": "5m", "all": True})
    check(
        "mobile_ui forwards exact flags for all 5 read-only actions",
        m_dev["argv"] == ["devices"]
        and m_prj["argv"] == ["project", MOB_PROJ]
        and m_ui["argv"] == ["ui", "--android", "--device=emu-1", "--find=Sign In", "--all", "--json"]
        and m_shot["argv"] == ["shot", "--ios", f"--out={os.path.join(MOB_PROJ, 'shots', 's.png')}"]
        and m_logs["argv"] == ["logs", "com.example.demo", "--ios", "--last=5m", "--all"],
        (m_dev, m_prj, m_ui, m_shot, m_logs),
    )

    # mobile_act: all 8 actions
    a_boot = call_mock("mobile_act", {"action": "boot", "workspace": MOB_PROJ, "platform": "ios", "device": "iPhone 17"})
    a_run = call_mock("mobile_act", {"action": "run", "workspace": MOB_PROJ, "platform": "android"})
    a_tap_txt = call_mock("mobile_act", {"action": "tap", "workspace": MOB_PROJ, "text": "Continue", "long": True, "nth": 2, "shot": True})
    a_tap_xy = call_mock("mobile_act", {"action": "tap", "workspace": MOB_PROJ, "x": 0, "y": 0})
    a_sw_dir = call_mock("mobile_act", {"action": "swipe", "workspace": MOB_PROJ, "direction": "up"})
    a_sw_xy = call_mock("mobile_act", {"action": "swipe", "workspace": MOB_PROJ, "x": 10, "y": 200, "x2": 10, "y2": 50, "shot": True})
    a_type = call_mock("mobile_act", {"action": "type", "workspace": MOB_PROJ, "text": "-50%", "into": "Amount", "id": "amtField"})
    a_key = call_mock("mobile_act", {"action": "key", "workspace": MOB_PROJ, "text": "BACK", "platform": "android"})
    a_open = call_mock("mobile_act", {"action": "open", "workspace": MOB_PROJ, "url": "demo://home", "shot": True})
    a_wait = call_mock("mobile_act", {"action": "wait", "workspace": MOB_PROJ, "text": "-loading", "gone": True, "timeout": 5.5, "shot": True})
    check(
        "mobile_act forwards exact flags for all 8 interaction actions",
        a_boot["argv"] == ["boot", "iPhone 17", "--ios", "--project", MOB_PROJ]
        and a_run["argv"] == ["run", "--project", MOB_PROJ, "--android"]
        and a_tap_txt["argv"] == ["tap", "--shot", "--text=Continue", "--long", "--nth", "2"]
        and a_tap_xy["argv"] == ["tap", "0", "0"]
        and a_sw_dir["argv"] == ["swipe", "up"]
        and a_sw_xy["argv"] == ["swipe", "--shot", "10", "200", "10", "50"]
        and a_type["argv"] == ["type", "--into=Amount", "--id=amtField", "--", "-50%"]
        and a_key["argv"] == ["key", "back", "--android"]
        and a_open["argv"] == ["open", "demo://home", "--shot"]
        and a_wait["argv"] == ["wait", "--shot", "--gone", "--timeout", "5.5", "--", "-loading"],
        (a_boot, a_run, a_tap_txt, a_tap_xy, a_sw_dir, a_sw_xy, a_type, a_key, a_open, a_wait),
    )

    # mobile_ui shot: mobile.py deletes or overwrites whatever is at --out, so only a new .png or an existing PNG
    write(os.path.join(MOB_PROJ, "src", "App.kt"), "fun main() = Unit\n")
    write(os.path.join(MOB_PROJ, "shots", "fake.png"), "not an image\n")
    with open(os.path.join(MOB_PROJ, "shots", "old.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    shot_new = call_mock("mobile_ui", {"action": "shot", "workspace": MOB_PROJ, "out": "shots/new/s.png"})
    shot_old = call_mock("mobile_ui", {"action": "shot", "workspace": MOB_PROJ, "out": "shots/old.png"})
    refused = [cli_mock.call_tool("mobile_ui", {"action": "shot", "workspace": MOB_PROJ, "out": o})
               for o in ("src/App.kt", "shots/fake.png", "shots", "../outside.png")]
    link_refused = True
    if not WIN:  # a .png name that is a link to a source file
        os.symlink(os.path.join(MOB_PROJ, "src", "App.kt"), os.path.join(MOB_PROJ, "shots", "link.png"))
        e_l, t_l, _ = cli_mock.call_tool("mobile_ui", {"action": "shot", "workspace": MOB_PROJ, "out": "shots/link.png"})
        link_refused = e_l and "isn't a PNG" in t_l
    with open(os.path.join(MOB_PROJ, "src", "App.kt"), encoding="utf-8") as f:
        source_kept = f.read() == "fun main() = Unit\n"
    check(
        "mobile_ui shot saves only to a new .png or an existing PNG inside the workspace",
        shot_new["argv"] == ["shot", f"--out={os.path.join(MOB_PROJ, 'shots', 'new', 's.png')}"]
        and shot_old["argv"] == ["shot", f"--out={os.path.join(MOB_PROJ, 'shots', 'old.png')}"]
        and all(e for e, _t, _r in refused) and "must be a .png" in refused[0][1] and "isn't a PNG" in refused[1][1]
        and "stay inside `workspace`" in refused[3][1] and link_refused and source_kept,
        (shot_new, shot_old, refused),
    )

    # mobile_act run: a build is only installed, so it may be anywhere (Xcode builds land in DerivedData)
    derived = os.path.join(SP, "DerivedData", "Demo-abc", "Build", "Products", "Debug-iphonesimulator")
    write(os.path.join(derived, "Demo.app", "Info.plist"), "<plist/>\n")
    apk = os.path.join(SP, "outside build", "app-debug.apk")
    write(apk, "PK\n")
    run_dir = call_mock("mobile_act", {"action": "run", "workspace": MOB_PROJ, "platform": "ios", "path": derived})
    run_app = call_mock("mobile_act", {"action": "run", "workspace": MOB_PROJ, "path": os.path.join(derived, "Demo.app")})
    run_apk = call_mock("mobile_act", {"action": "run", "workspace": MOB_PROJ, "path": apk})
    e_nb, t_nb, _ = cli_mock.call_tool("mobile_act", {"action": "run", "workspace": MOB_PROJ, "path": os.path.join(SP, "plain dir")})
    check(
        "mobile_act run takes a build outside the workspace (.app, .apk, a folder with one) and nothing else",
        run_dir["argv"] == ["run", derived, "--project", MOB_PROJ, "--ios"]
        and run_app["argv"][1] == os.path.join(derived, "Demo.app") and run_apk["argv"][1] == apk
        and e_nb and "must be an .app bundle" in t_nb,
        (run_dir, run_app, run_apk, t_nb),
    )
finally:
    rc_mock = cli_mock.close()
check("mock plugin server exits cleanly on EOF", rc_mock == 0 and "Traceback" not in cli_mock.stderr_text(), cli_mock.stderr_text())


# ------------------------------------------------------------------------------------------------- 7. time limits, timeouts, cuts, json=true

# Each tool's time limit covers its script's own worst case: run_script swapped for a recorder, in this process
spec = importlib.util.spec_from_file_location("agp_mcp_server", SERVER)
srv_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv_mod)  # type: ignore[union-attr]
asked: List[Tuple[str, str, float]] = []
srv_mod.run_script = lambda script, argv, cwd, timeout, **kw: asked.append((script, argv[0], timeout)) or (0, "ok")


def limit_of(fn: str, arguments: dict) -> float:
    asked.clear()
    getattr(srv_mod, fn)(arguments)
    return asked[-1][2]


lims = {
    "deps all 100": limit_of("t_deps_audit", {"workspace": PROJ, "mode": "all", "timeout": 100}),
    "deps audit 100": limit_of("t_deps_audit", {"workspace": PROJ, "mode": "audit", "timeout": 100}),
    "deps outdated 100": limit_of("t_deps_audit", {"workspace": PROJ, "mode": "outdated", "timeout": 100}),
    "deps all default": limit_of("t_deps_audit", {"workspace": PROJ, "mode": "all"}),
    "ui 8 widths": limit_of("t_ui_check", {"workspace": PROJ, "target": "web/index.html", "widths": "1,2,3,4,5,6,7,8"}),
    "ui default": limit_of("t_ui_check", {"workspace": PROJ, "target": "web/index.html"}),
    "ci logs": limit_of("t_ci_failure_logs", {"workspace": PROJ}),
    "ci status": limit_of("t_ci_status", {"workspace": PROJ}),
    "mobile run": limit_of("t_mobile_act", {"action": "run", "workspace": MOB_PROJ}),
}
check(
    "time limits cover each script's worst case (deps 3×T+60 / 2×T+60, ui_check 120 s a width, ci logs ≥ 180 s)",
    lims["deps all 100"] >= 360 and lims["deps audit 100"] >= 260 and lims["deps outdated 100"] >= 160
    and lims["deps all default"] >= 420 and lims["ui 8 widths"] >= 960 and lims["ui default"] >= 240
    and lims["ci logs"] >= 180 and lims["ci status"] >= 180 and lims["mobile run"] >= 300,
    lims,
)
os.environ["AGP_MCP_TIMEOUT"] = "3"
capped = srv_mod.time_limit(600.0)
del os.environ["AGP_MCP_TIMEOUT"]
check("AGP_MCP_TIMEOUT caps every limit", capped == 3.0 and srv_mod.time_limit(600.0) == 600.0, capped)

# A plugin copy whose memory.py and deps.py are fakes that misbehave on cue
robust = os.path.join(SP, "robust-plugin")
for rel in ("plugin.json", "mcp/server.py", "hooks/compat.py"):
    dst = os.path.join(robust, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(os.path.join(PLUGIN, *rel.split("/")), dst)
write(
    os.path.join(robust, "skills", "memory", "scripts", "memory.py"),
    r'''import os, subprocess, sys, time
query, here = sys.argv[-1], os.environ["MCP_FAKE_DIR"]
if query == "partial":  # prints, then hangs
    with open(os.path.join(here, "partial.pid"), "w") as f:
        f.write(str(os.getpid()))
    print("first part done", flush=True)
    print("warning: still working", file=sys.stderr, flush=True)
    time.sleep(60)
elif query == "huge":  # longer than a result carries, an error at the end of stderr
    sys.stdout.write("HEAD-MARK\n" + ("x" * 99 + "\n") * 3000 + "TAIL-MARK\n")
    sys.stderr.write("Traceback (most recent call last):\n" + "e" * 20000 + "\nREAL ERROR AT END\n")
elif query == "giant":  # longer than what is kept while reading
    sys.stdout.write("HEAD-MARK\n" + ("y" * 99 + "\n") * 12000 + "TAIL-MARK\n")
elif query == "helper":  # leaves a helper running that keeps our stdout open
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         **({} if os.name == "nt" else {"start_new_session": True}))
    with open(os.path.join(here, "helper.pid"), "w") as f:
        f.write(str(p.pid))
    print("parent done")
''',
)
write(
    os.path.join(robust, "skills", "deps", "scripts", "deps.py"),
    r'''import json, sys
argv = sys.argv[1:]
t = argv[argv.index("--timeout") + 1] if "--timeout" in argv else ""
warn = "deps.py:1: DeprecationWarning: 'maxsplit' is passed as positional argument"
if t == "5":  # a good result with a warning beside it
    print(json.dumps({"summary": {"vulnerabilities": 0}}))
    print(warn, file=sys.stderr)
    sys.exit(0)
if t == "6":  # findings: exit 1 is a result
    print(json.dumps({"summary": {"vulnerabilities": 2}}))
    print(warn, file=sys.stderr)
    sys.exit(1)
if t == "7":  # checks couldn't run: exit 2, the reason on stderr
    print(json.dumps({"summary": {"not_checked": 1}}))
    print("pip-audit: network unreachable", file=sys.stderr)
    sys.exit(2)
if t == "8":  # more JSON than one result carries
    print(json.dumps({"packages": ["p" * 50] * 3000}))
    sys.exit(0)
raise RuntimeError("boom inside deps.py")  # a crash: a traceback and exit 1 — not a result
''',
)


def parses(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


cli_rob = McpClient(os.path.join(robust, "mcp", "server.py"), env=dict(os.environ, AGP_MCP_TIMEOUT="3", MCP_FAKE_DIR=SP))
try:
    cli_rob.initialize()
    rep_rl = cli_rob.request("tools/list")
    rob_names = [t.get("name") for t in ((rep_rl.get("result") or {}).get("tools") or [])]
    check("tools/list follows the installed scripts (memory, deps)", rob_names == ["memory_search", "quota", "deps_audit"], rob_names)

    t0 = time.time()
    err, txt, _ = cli_rob.call_tool("memory_search", {"workspace": PROJ, "query": "partial"}, timeout=30)
    took = time.time() - t0
    stopped = True
    pid_file = os.path.join(SP, "partial.pid")
    if not WIN and os.path.isfile(pid_file):
        with open(pid_file, encoding="utf-8") as f:
            try:
                os.kill(int(f.read()), 0)
                stopped = False
            except OSError:
                pass
    check(
        "past its time limit a script is stopped, and what it printed (stdout and stderr) comes back with a note",
        err and "first part done" in txt and "warning: still working" in txt
        and "took longer than 3 s and was stopped" in txt and took < 15 and stopped,
        (round(took, 1), txt),
    )

    err, txt, _ = cli_rob.call_tool("memory_search", {"workspace": PROJ, "query": "huge"})
    check(
        "a long output keeps its start, its end and the end of stderr, cut in the middle with a marker",
        not err and txt.startswith("HEAD-MARK") and "TAIL-MARK" in txt and txt.endswith("REAL ERROR AT END")
        and "characters cut here" in txt and len(txt) <= MAX_OUT + 500,
        (len(txt), txt[:120], txt[-300:]),
    )
    err, txt, _ = cli_rob.call_tool("memory_search", {"workspace": PROJ, "query": "giant"})
    check(
        "an output longer than what is kept while reading is cut the same way",
        not err and txt.startswith("HEAD-MARK") and txt.endswith("TAIL-MARK") and "about " in txt
        and len(txt) <= MAX_OUT + 500,
        (len(txt), txt[:120], txt[-200:]),
    )

    def call_items(arguments: dict) -> Tuple[bool, List[str]]:
        rep = cli_rob.request("tools/call", {"name": "deps_audit", "arguments": dict(arguments, workspace=PROJ)})
        res = rep.get("result") or {}
        return bool(res.get("isError")), [c.get("text", "") for c in res.get("content") or []]

    e5, c5 = call_items({"json": True, "timeout": 5})
    e5t, c5t = call_items({"timeout": 5})
    e6, c6 = call_items({"json": True, "timeout": 6})
    e7, c7 = call_items({"json": True, "timeout": 7})
    e8, c8 = call_items({"json": True, "timeout": 8})
    e9, c9 = call_items({"json": True, "timeout": 9})
    check(
        "json=true returns the JSON alone: a warning on stderr beside a good result is dropped (text mode keeps it)",
        not e5 and len(c5) == 1 and parses(c5[0]) and "DeprecationWarning" not in c5[0]
        and not e5t and "DeprecationWarning" in "\n".join(c5t),
        (c5, c5t),
    )
    check(
        "json=true: findings (exit 1) are a result; a failure keeps the JSON and stderr in separate items",
        not e6 and len(c6) == 1 and parses(c6[0])
        and e7 and len(c7) == 2 and parses(c7[0]) and c7[1].startswith("stderr:") and "network unreachable" in c7[1],
        (c6, c7),
    )
    check(
        "json=true too big for one result is an error rather than cut JSON; a crash (exit 1) stays an error",
        e8 and not parses(c8[0]) and "wouldn't parse" in c8[0] and e9 and "boom inside deps.py" in "\n".join(c9),
        (c8, c9),
    )
finally:
    rc_rob = cli_rob.close()
check("robust plugin server exits cleanly on EOF", rc_rob == 0 and "Traceback" not in cli_rob.stderr_text(), cli_rob.stderr_text())

# Without a cap: a helper the script leaves running (holding its stdout) doesn't hold the answer up until the limit
cli_free = McpClient(os.path.join(robust, "mcp", "server.py"), env=dict(os.environ, MCP_FAKE_DIR=SP))
try:
    cli_free.initialize()
    t0 = time.time()
    err, txt, _ = cli_free.call_tool("memory_search", {"workspace": PROJ, "query": "helper"}, timeout=40)
    took = time.time() - t0
finally:
    cli_free.close()
    helper_pid = os.path.join(SP, "helper.pid")
    if os.path.isfile(helper_pid):
        with open(helper_pid, encoding="utf-8") as f:
            try:
                os.kill(int(f.read()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
check("a helper left running by the script doesn't delay the result", not err and txt == "parent done" and took < 10,
      (round(took, 1), txt))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
