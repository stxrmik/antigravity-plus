"""Finding Antigravity's language server (compat.language_server) and every caller of it.

Antigravity 2.34 no longer starts its language server with --csrf_token on the command line: the token goes into
the server's environment, and the server describes itself in ~/.gemini/antigravity/daemon/hub_<httpPort>.json
({pid, httpsPort, httpPort, lsVersion, csrfToken}). Hooks and the plugin's MCP server get no ANTIGRAVITY_LS_ADDRESS /
ANTIGRAVITY_CSRF_TOKEN, and Claude Code's delegate.py runs outside Antigravity — all of them must find it by that file.
"""
import json, os, socket, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOME = os.path.join(SP, "home-ls")  # never the real home: this test writes a daemon folder
os.makedirs(os.path.join(HOME, ".gemini", "antigravity-plus", "state"), exist_ok=True)
os.environ.update(HOME=HOME, USERPROFILE=HOME, AGP_DELEGATE_NO_LAUNCH="1")
for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_VERSION", "ANTIGRAVITY_AGENTAPI_EXE"):
    os.environ.pop(var, None)
REPO = os.path.dirname(os.path.abspath(PLUGIN))
for d in (os.path.join(PLUGIN, "hooks"), os.path.join(PLUGIN, "mcp"), os.path.join(PLUGIN, "skills", "doctor", "scripts"),
          os.path.join(PLUGIN, "skills", "schedule-task", "scripts"),
          os.path.join(REPO, "claude-code", "skills", "antigravity", "scripts")):
    sys.path.insert(0, d)
import compat

ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {got}"))

TOKEN = "tok-daemon"
QUOTA = {"response": {"groups": [{"displayName": "Gemini Models", "description": "Gemini 4 Hydrogen",
                                  "buckets": [{"window": "weekly", "remainingFraction": 0.5,
                                               "resetTime": "2030-01-01T00:00:00Z"}]}]}}

class LS(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        self.reply(200, {"status": "ok"}) if self.path == "/healthz" else self.reply(404, {})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("x-codeium-csrf-token") != TOKEN:
            return self.reply(403, {"code": "permission_denied", "message": "bad csrf token"})
        self.reply(200, QUOTA if self.path.endswith("/RetrieveUserQuotaSummary") else {})

server = HTTPServer(("127.0.0.1", 0), LS)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]
ADDR = f"127.0.0.1:{PORT}"
closed = socket.socket(); closed.bind(("127.0.0.1", 0)); CLOSED = closed.getsockname()[1]; closed.close()

DAEMON = compat.daemon_dir()
check("daemon_dir() lies in this test's home", DAEMON.startswith(HOME) and DAEMON.endswith(os.path.join("antigravity", "daemon")), DAEMON)

real_processes, real_ports = compat.processes, compat.listening_ports
compat.processes = lambda name: []  # never the real Antigravity on this machine
check("nothing running → None", compat.language_server(probe=0.5) is None)

def hub(port, token, pid, version="2.34.0", when=None, name=None):
    os.makedirs(DAEMON, exist_ok=True)
    path = os.path.join(DAEMON, name or f"hub_{port}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "httpsPort": port - 1, "httpPort": port, "lsVersion": version, "csrfToken": token}, f)
    if when is not None:
        os.utime(path, (when, when))
    return path

import time
now = time.time()
hub(PORT, TOKEN, os.getpid(), when=now - 60)
found = compat.language_server(probe=0.5)
check("Antigravity 2.34: found by daemon/hub_<port>.json",
      found is not None and (found.addr, found.token, found.source, found.pid, found.version)
      == (ADDR, TOKEN, "daemon", os.getpid(), "2.34.0"), found)

hub(CLOSED, "tok-stale", os.getpid(), when=now - 10)  # newer, but nothing answers on its port any more
with open(os.path.join(DAEMON, "hub_1.json"), "w") as f:
    f.write("{not json")
os.utime(os.path.join(DAEMON, "hub_1.json"), (now, now))
with open(os.path.join(DAEMON, "other.json"), "w") as f:
    json.dump({"pid": os.getpid(), "httpPort": PORT, "csrfToken": "tok-other"}, f)
found = compat.language_server(probe=0.5)
check("a newer file whose port doesn't answer, a broken file and other names are skipped",
      found is not None and found.token == TOKEN, found)
check("daemon_servers(): newest first, broken and foreign files left out",
      [s.token for s in compat.daemon_servers()] == ["tok-stale", TOKEN], compat.daemon_servers())

if not compat.WINDOWS:  # on Windows the pid isn't probed (os.kill(pid, 0) would end the process)
    gone = subprocess.Popen([sys.executable, "-c", "pass"]); gone.wait()
    hub(PORT, "tok-dead-pid", gone.pid, when=now + 5, name="hub_2.json")  # newest, healthy port, its process gone
    found = compat.language_server(probe=0.5)
    check("a file of a process that is gone is skipped even when its port answers",
          found is not None and found.token == TOKEN, found)
    os.remove(os.path.join(DAEMON, "hub_2.json"))

os.environ.update(ANTIGRAVITY_LS_ADDRESS="localhost:1234", ANTIGRAVITY_CSRF_TOKEN="tok-env", ANTIGRAVITY_LS_VERSION="2.34.0")
found = compat.language_server(probe=0.5)
check("the environment wins (agent terminals, sidecars)",
      found is not None and (found.addr, found.token, found.source, found.version) == ("localhost:1234", "tok-env", "env", "2.34.0"), found)
for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_VERSION"):
    os.environ.pop(var)

# every caller, with the environment hooks and the MCP server really get (no ANTIGRAVITY_LS_ADDRESS / _CSRF_TOKEN)
import delegate, doctor, quota, schedule_task
import importlib.util
spec = importlib.util.spec_from_file_location("agp_mcp_server", os.path.join(PLUGIN, "mcp", "server.py"))
mcp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mcp)
ls = delegate.discover()
check("delegate.discover() (Claude Code → Antigravity)", ls is not None and (ls.addr, ls.token) == (ADDR, TOKEN), ls)
check("doctor.discover_ls() also tells the version", doctor.discover_ls() == (ADDR, TOKEN, "2.34.0"), doctor.discover_ls())
check("hooks/quota.py discover()", quota.discover() == (ADDR, TOKEN), quota.discover())
groups = quota.groups_now(fresh=True)
check("…and the hook reads the limits with that token", bool(groups) and groups[0].get("name") == "Gemini Models", groups)
check("mcp/server.py language_server()", mcp.language_server() == (ADDR, TOKEN), mcp.language_server())
check("schedule_task._discover_ls()", schedule_task._discover_ls() == (ADDR, TOKEN), schedule_task._discover_ls())

# Antigravity ≤ 2.33: no daemon file — the --csrf_token on the command line, the port that answers /healthz
for n in os.listdir(DAEMON):
    os.remove(os.path.join(DAEMON, n))
compat.processes = lambda name: ([(4242, '"/Apps/Antigravity/language_server" --csrf_token "tok-argv" --enable_sidecars')]
                                 if name == "language_server" else [])
compat.listening_ports = lambda pid: [CLOSED, PORT] if pid == 4242 else []
found = compat.language_server(probe=0.5)
check("Antigravity ≤ 2.33: found by --csrf_token on its command line",
      found is not None and (found.addr, found.token, found.source, found.pid) == (ADDR, "tok-argv", "process", 4242), found)
check("ls_executable() reads the executable from the command line", compat.ls_executable(4242) == "/Apps/Antigravity/language_server",
      compat.ls_executable(4242))
ls = delegate.discover()
check("delegate.discover() keeps the executable for agentapi", ls is not None and ls.exe == "/Apps/Antigravity/language_server", ls)
compat.processes = lambda name: [(4243, "/opt/other/language_server --csrf_token=x")]  # not Antigravity's
check("another product's language server isn't taken", compat.language_server(probe=0.5) is None)

compat.processes, compat.listening_ports = real_processes, real_ports
server.shutdown()
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
