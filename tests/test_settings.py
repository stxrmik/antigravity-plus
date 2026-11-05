"""/settings: show works on a fresh setup; MCP servers are added, listed and removed, with secrets taken from .env
and never printed (the language server reload is stubbed out)."""
import contextlib, io, json, os, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(PLUGIN, "skills", "settings", "scripts"))
import settings
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:300]}"))
def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = settings.main(list(argv))
    return code, out.getvalue()

settings.MCP_FILE = os.path.join(SP, "mcp_config.json")
settings.reload_mcp = lambda name: "(reload skipped in tests)"
ws = os.path.join(SP, "ws"); os.makedirs(ws)
open(os.path.join(ws, ".env"), "w").write("GITHUB_TOKEN=secret-value-123\n")

code, out = run("show", "--workspace", ws)
check("show works on a fresh setup", code == 0 and "## MCP servers" in out and "## Git" in out, out)
code, out = run("mcp", "add", "github", "--command", "npx", "--env", "GITHUB_TOKEN", "--workspace", ws, "--",
                "-y", "@modelcontextprotocol/server-github")
data = json.load(open(settings.MCP_FILE))
srv = data["mcpServers"].get("github", {})
check("mcp add: command and arguments after --", srv.get("command") == "npx" and srv.get("args") == ["-y", "@modelcontextprotocol/server-github"], srv)
check("mcp add: the key comes from .env", srv.get("env", {}).get("GITHUB_TOKEN") == "secret-value-123")
check("…and is never printed", "secret-value-123" not in out, out)
code, out = run("mcp", "add", "docs", "--url", "https://example.com/mcp")
check("mcp add --url: a remote server", json.load(open(settings.MCP_FILE))["mcpServers"]["docs"] == {"url": "https://example.com/mcp"})
code, out = run("mcp", "add", "broken", "--command", "x", "--env", "MISSING_KEY", "--workspace", ws)
check("a key that isn't set is refused", code == 2 and "broken" not in json.load(open(settings.MCP_FILE))["mcpServers"], out)
code, out = run("mcp", "list")
check("mcp list hides values", "github" in out and "docs" in out and "secret-value-123" not in out, out)
code, out = run("mcp", "remove", "github")
check("mcp remove", code == 0 and "github" not in json.load(open(settings.MCP_FILE))["mcpServers"])
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
