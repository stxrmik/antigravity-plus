#!/usr/bin/env python3
"""Antigravity Plus — /settings: what is set where, and the settings an agent may change for the user.

  settings.py show [--workspace DIR]           Antigravity's permissions, this project in Antigravity, MCP servers,
                                               Antigravity Plus settings, project rules and hooks, git
  settings.py mcp list                         MCP servers in ~/.gemini/config/mcp_config.json (no secret values)
  settings.py mcp add NAME --command CMD [--env KEY[=VALUE]]… [-- ARG …]   add or replace a local (stdio) server
  settings.py mcp add NAME --url URL           a remote (Streamable HTTP) server
  settings.py mcp remove NAME
  settings.py plugin [KEY on|off …]            Antigravity Plus settings (same as memory.py config)

`--env KEY` without a value takes it from the environment or the workspace's .env, so the secret never appears in
the conversation. MCP changes are applied right away (the language server reloads its servers). Antigravity's own
permission settings (preset, sandbox, auto-run, file access) are only shown: the user changes them in Settings.
Stdlib only, Python 3.8+.
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
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
import checks  # noqa: E402
import compat  # noqa: E402
import conversation as conv  # noqa: E402
import memory as memstore  # noqa: E402

CONFIG = os.path.expanduser("~/.gemini/config")
MCP_FILE = os.path.join(CONFIG, "mcp_config.json")
MEMORY_PY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PLUGIN_KEYS = [("auto-memory", "autoMemory"), ("share-with-claude-code", "shareWithClaudeCode"),
               ("recent-conversations", "recentConversations"), ("finish-tasks", "finishTasks"),
               ("check-edits", "checkEdits"), ("auto-review", "autoReview"), ("auto-verify", "autoVerify"),
               ("protect-secrets", "protectSecrets"), ("auto-setup", "autoSetup")]


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if text.strip() else {}


def short(enum: object) -> str:
    """AGENT_PERMISSION_PRESET_TURBO → turbo."""
    return str(enum).rsplit("_", 1)[-1].lower() if isinstance(enum, str) else json.dumps(enum)


def project_of(ws: str) -> Optional[dict]:
    """Antigravity's project whose folder holds `ws`."""
    for f in glob.glob(os.path.join(CONFIG, "projects", "*.json")):
        try:
            p = load_json(f)
        except (OSError, ValueError):
            continue
        for r in (p.get("projectResources") or {}).get("resources") or []:
            uri = r.get("folderUri") or (r.get("gitFolder") or {}).get("folderUri") or ""
            if uri.startswith("file://"):
                folder = os.path.realpath(urllib.parse.unquote(uri[len("file://"):]))
                if ws == folder or ws.startswith(folder + os.sep):
                    return p
    return None


def git(args: List[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def mcp_servers() -> Dict[str, dict]:
    data = load_json(MCP_FILE) if os.path.exists(MCP_FILE) else {}
    servers = data.get("mcpServers") or {}
    return servers if isinstance(servers, dict) else {}


def describe_server(name: str, s: dict) -> str:
    if s.get("url") or s.get("serverUrl"):
        line = f"{name}: remote {s.get('url') or s.get('serverUrl')}"
    else:
        args = " ".join(str(a) for a in s.get("args") or [])
        line = f"{name}: {s.get('command', '?')} {checks.redact(args)}".rstrip()
    if s.get("env"):
        line += " (env: " + ", ".join(sorted(s["env"])) + ")"
    return line + (" — disabled" if s.get("disabled") else "")


def show(ws: str) -> str:
    out = [f"# Settings — {ws}"]
    try:
        us = load_json(os.path.join(CONFIG, "config.json")).get("userSettings") or {}
    except (OSError, ValueError):
        us = {}
    out.append("\n## Antigravity — the user changes these in Settings (they decide what you may do without asking)")
    if us:
        out.append(f"- Permission preset: {short(us.get('permissionPreset', '?'))} · terminal commands: "
                   f"{short(us.get('autoExecutionPolicy', '?'))} · terminal sandbox: "
                   f"{'on' if us.get('enableTerminalSandbox') else 'off'} · files outside the workspace: "
                   f"{short(us.get('nonWorkspaceFileAccessPolicy', '?'))}")
    else:
        out.append("- (not readable)")
    p = project_of(ws)
    if p:
        grants = (p.get("permissionGrants") or {}).get("permissionGrants") or {}
        out.append(f"\n## This project in Antigravity: «{p.get('name', '?')}»")
        out.append("- Always allowed: " + (", ".join(grants.get("allow") or []) or "nothing extra")
                   + (" · always denied: " + ", ".join(grants["deny"]) if grants.get("deny") else ""))
        sets = p.get("settings") or {}
        out.append("- Project overrides: " + (", ".join(f"{k}={short(v)}" for k, v in sets.items()) or "none"))
    out.append(f"\n## MCP servers — {MCP_FILE.replace(os.path.expanduser('~'), '~')} (`settings.py mcp add|remove`)")
    try:
        servers = mcp_servers()
        out += [f"- {describe_server(n, s)}" for n, s in servers.items()] or ["- none"]
    except (OSError, ValueError) as e:
        out.append(f"- the file isn't valid JSON: {e}")
    s = memstore.settings()
    out.append("\n## Antigravity Plus — `settings.py plugin <key> on|off`")
    out.append("- " + " · ".join(f"{k} {('on' if s[v] else 'off') if isinstance(s[v], bool) else s[v]}"
                                 for k, v in PLUGIN_KEYS if v in s))
    since = conv._project_grant(ws, None)
    out.append(f"- Full access for this project: {'on since ' + since if since else 'off (per conversation only)'}")
    out.append("\n## Project rules and automation")
    rules = [n for n in ("AGENTS.md", "GEMINI.md", "CLAUDE.md") if os.path.isfile(os.path.join(ws, n))]
    out.append("- Rules files: " + (", ".join(rules) if rules else "none — /init creates AGENTS.md"))
    topic = sorted(os.path.basename(f) for f in glob.glob(os.path.join(ws, ".agents", "rules", "*.md")))
    out.append("- .agents/rules: " + (", ".join(topic) if topic else "none"))
    try:
        hooks = list(load_json(os.path.join(ws, ".agents", "hooks.json")))
    except (OSError, ValueError):
        hooks = []
    out.append("- .agents/hooks.json: " + (", ".join(hooks) if hooks else "none — /hooks sets them up"))
    out.append("- Rules for every project: " + ", ".join(
        f"{f.replace(os.path.expanduser('~'), '~')} ({'yes' if os.path.isfile(f) else 'no'})"
        for f in (os.path.join(CONFIG, "AGENTS.md"), os.path.expanduser("~/.gemini/GEMINI.md"))))
    out.append("\n## Git")
    top = git(["rev-parse", "--show-toplevel"], ws) if os.path.isdir(ws) else None
    out.append(f"- Repository: {top} (branch {git(['branch', '--show-current'], top) or '?'})" if top
               else "- Repository: none — /git-init creates one")
    cwd = ws if os.path.isdir(ws) else "/"
    name, email = git(["config", "user.name"], cwd), git(["config", "user.email"], cwd)
    out.append(f"- Identity for commits: {name} (email set)" if name and email
               else "- Identity for commits: not set — ask the user for a name and email (`git config --global …`)")
    return "\n".join(out)


def env_value(key: str, ws: str) -> Optional[str]:
    if os.environ.get(key):
        return os.environ[key]
    try:
        with open(os.path.join(ws, ".env"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?" + re.escape(key) + r"\s*=\s*(.*?)\s*$", line)
                if m:
                    return m.group(1).strip("'\"")
    except OSError:
        pass
    return None


def save_mcp(servers: Dict[str, dict]) -> None:
    path = os.path.realpath(MCP_FILE)
    data = load_json(path) if os.path.exists(path) else {}
    data["mcpServers"] = servers
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    compat.replace(tmp, path)


def reload_mcp(name: Optional[str]) -> str:
    """Ask the running language server to re-read MCP servers; report the server's state if it says."""
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "doctor", "scripts"))
        import doctor
        ls = doctor.discover_ls()
        if not ls:
            return "Antigravity isn't running: the change applies when it starts."
        doctor.rpc(ls, "RefreshMcpServers", {})
        if not name:
            return "Antigravity reloaded its MCP servers."
        for _ in range(12):  # report status and error only: the spec in this answer carries env values (secrets)
            for s in doctor.rpc(ls, "GetMcpServerStates", {}).get("states") or []:
                status = short(s.get("status", ""))
                if (s.get("spec") or {}).get("serverName") == name and status not in ("starting", "pending", "connecting"):
                    tools = f", {len(s['tools'])} tools" if isinstance(s.get("tools"), list) else ""
                    return (f"Antigravity reloaded its MCP servers; {name}: {status}{tools}"
                            + (f" — {checks.redact(str(s['error']))[:300]}" if s.get("error") else ""))
            time.sleep(0.5)
        return "Antigravity reloaded its MCP servers; the new tools show up in new conversations."
    except Exception as e:  # the file is saved either way
        return f"Saved; Antigravity will pick it up on restart (reload failed: {e})."


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("show")
    sp.add_argument("--workspace", default=os.getcwd())
    mcp = sub.add_parser("mcp").add_subparsers(dest="action")
    mcp.add_parser("list")
    add = mcp.add_parser("add")
    add.add_argument("name")
    add.add_argument("--command", help="the server program; its arguments go after --")
    add.add_argument("--url")
    add.add_argument("--env", action="append", default=[], help="KEY=VALUE, or KEY to take it from the environment/.env")
    add.add_argument("--workspace", default=os.getcwd())
    rm = mcp.add_parser("remove")
    rm.add_argument("name")
    sp = sub.add_parser("plugin")
    sp.add_argument("pairs", nargs="*")
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: List[str] = []
    if "--" in argv:  # the MCP server's own arguments (they often start with "-")
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)

    if a.cmd in (None, "show"):
        print(show(os.path.realpath(os.path.expanduser(getattr(a, "workspace", os.getcwd())))))
        return 0
    if a.cmd == "plugin":
        return subprocess.run([sys.executable, MEMORY_PY, "config"] + a.pairs).returncode
    try:
        servers = mcp_servers()
    except (OSError, ValueError) as e:
        print(f"{MCP_FILE} isn't valid JSON ({e}); fix it before changing servers.")
        return 2
    if a.action in (None, "list"):
        print("\n".join(describe_server(n, s) for n, s in servers.items()) or "No MCP servers configured.")
        return 0
    if a.action == "remove":
        if a.name not in servers:
            print(f"No MCP server named {a.name}.")
            return 2
        del servers[a.name]
        save_mcp(servers)
        print(f"Removed MCP server {a.name}. " + reload_mcp(None))
        return 0
    if not NAME.match(a.name) or bool(a.command) == bool(a.url):
        print("Give a name (letters, digits, _ . -) and exactly one of --command or --url.")
        return 2
    entry: dict = {"url": a.url} if a.url else {"command": a.command, "args": extra}
    env = {}
    for item in a.env:
        key, eq, value = item.partition("=")
        if not eq:
            value = env_value(key, os.path.realpath(a.workspace)) or ""
            if not value:
                print(f"{key} isn't set in the environment or in .env; ask the user to put it in .env, then retry.")
                return 2
        env[key] = value
    if env:
        entry["env"] = env
    replaced = a.name in servers
    servers[a.name] = entry
    save_mcp(servers)
    print(f"{'Replaced' if replaced else 'Added'} MCP server {describe_server(a.name, entry)}. " + reload_mcp(a.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
