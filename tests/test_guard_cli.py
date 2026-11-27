"""Guard: the Antigravity CLI (`agy`, 0.31.0) — the agent doesn't take its own protection off through the CLI: no
permission prompts (--dangerously-skip-permissions, --mode auto|accept-edits, --settings with permissions), plugins and
MCP servers (`agy plugin|mcp …`), a message that switches Bypass Mode on (`agy -p|-c|-r`, stdin, heredoc), the CLI's
settings and hooks files; the tools' older names; the 2.35 RPCs and REST route. Synthetic examples only."""
import os, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat, guard
compat.WINDOWS = False  # POSIX shell syntax
ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<76} → {got}" + ("" if good else f" (want {want})"))


def both(cmd):
    hits = [guard.analyze_command(cmd, system_only=b) for b in (False, True)]
    return tuple(h[0] if h else None for h in hits)


PO, AT, AS = ("protection-off",) * 2, ("agent-tools",) * 2, ("agent-settings",) * 2  # ask even in Bypass Mode
CD = ("conversation-delete", None)  # asks, Bypass Mode approves
FREE = (None, None)
HOME = os.path.expanduser("~")

cases = [
    # no permission prompts
    ("agy --dangerously-skip-permissions", PO),
    ("~/.local/bin/agy -p 'fix it' --dangerously-skip-permissions", PO),
    (f"{HOME}/.local/bin/agy -p x -dangerously-skip-permissions", PO),
    ("agy --dangerously-skip-permissions=false -p x", FREE),
    ("agy --mode=auto", PO), ("agy --mode auto -p x", PO), ("agy -mode accept-edits", PO),
    ("agy --mode=plan", FREE), ("agy --mode default -p x", FREE),
    ("agy -p 'hi' -- --dangerously-skip-permissions", FREE),  # after --: the prompt's text
    ("agy --settings '{\"toolPermission\":\"always-proceed\"}' -p x", AS),
    ("agy --settings ./session.json -p x", AS),
    ("agy --settings '{\"colorScheme\":\"dark\"}' -p x", FREE),
    # plugins and MCP servers
    ("agy plugin disable antigravity-plus", AT), ("agy plugins uninstall antigravity-plus", AT),
    ("agy plugin install ./some-plugin", AT), ("agy plugin enable other", AT), ("agy plugin import claude", AT),
    ("agy mcp add srv -- npx srv", AT), ("agy mcp remove srv", AT),
    ("agy plugin list", FREE), ("agy plugin list --json", FREE), ("agy plugin validate .", FREE),
    ("agy install", AT), ("agy install --dir ~/bin", AT), ("agy remote-control start", AT),
    ("agy update", AT), ("agy -p x --remote-control", AT), ("agy --remote-control=true", AT), ("agy mic-serve", AT),
    ("agy mic-serve --help", FREE), ("agy --remote-control=false -p x", FREE),
    ("agy -p x --sandbox=false", PO), ("agy -sandbox=0 -p x", PO), ("agy --sandbox -p x", FREE),
    ("agy --sandbox=true -p x", FREE),
    ("agy install --help", FREE), ("agy remote-control status", FREE), ("agy mcp add --help", FREE),
    ("agy mcp list", FREE), ("agy --help", FREE), ("agy --version", FREE), ("agy models", FREE),
    # a message that switches Bypass Mode / full access on
    ("agy -p '/bypass and fix it'", AS), ("agy -c 'turn on full access'", AS),
    ("agy -r 0123abcd 'bypass mode please'", AS), ("agy --conversation=0123abcd /bypass", AS),
    ("AGY_SOME_FLAG=1 agy -p '/bypass'", AS), ("echo '/bypass' | agy -p", AS),
    ("agy -p <<'EOF'\nplease /bypass now\nEOF", AS), ("cat <<EOF | agy -p\nfull access please\nEOF", AS),
    ("agy -p 'fix the tests'", FREE), ("agy -p <<EOF\nfix the tests\nEOF", FREE),
    ("echo '/bypass' | agy plugin list", FREE),
    # the shell's spellings and wrappers
    ("xargs agy --dangerously-skip-permissions", PO), ("echo x | xargs -I{} agy plugin disable {}", AT),
    ("sh -c 'agy --mode=auto'", PO), ("env FOO=1 agy --mode=auto", PO), ("command agy plugin uninstall x", AT),
    ("exec agy --dangerously-skip-permissions", PO), ("timeout 30 agy -p x --mode auto", PO),
    ("A=agy; $A --mode auto", PO), ("$'\\x61gy' --mode auto", PO), ("ag[y] --mode auto", PO),
    # text that only mentions it, other words
    ("cat ./agy_notes.txt", FREE), ("magy --dangerously-skip-permissions", FREE),
    ("grep -r 'agy --dangerously-skip-permissions' .", FREE), ("echo agy plugin disable x", FREE),
    ("gh pr create --title t --body 'agy --mode auto'", FREE), ("git commit -m 'agy plugin disable x'", FREE),
    # another machine or a container: the rules on this computer drop
    ("ssh host agy --dangerously-skip-permissions", FREE), ("ssh host 'agy plugin disable x'", FREE),
    ("docker exec app agy --mode auto", FREE), ("./deploy/ssh.sh 'agy -p /bypass'", FREE),
    ("ssh h <<'EOF'\nagy -p /bypass\nEOF", FREE),
    # the CLI's settings and the global hooks file
    ("echo '{}' > ~/.gemini/antigravity-cli/settings.json", AS), ("cp x.json ~/.gemini/config/hooks.json", AS),
    # 2.35 RPCs outside the verbs, and the Conversation Management API's REST route
    ("curl -X POST http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService/SendTerminalInput -d x", AS),
    ("curl -X POST http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService/SaveAgentScriptCommandSpec -d x", AS),
    ("curl -d '{\"text\":\"/bypass\"}' http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService/SendAgentMessage", AS),
    ("curl -d '{\"text\":\"hello\"}' http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService/SendAgentMessage", FREE),
    ("curl http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService/GetUserStatus", FREE),
    ("curl -X DELETE http://127.0.0.1:41234/v1/conversations/abc-123", CD),
    ("curl http://127.0.0.1:41234/v1/conversations/abc-123", FREE),
    ("curl -X delete http://127.0.0.1:41234/v1/conversations/abc-123", CD),
    ("python3 -c 'import requests; requests.delete(\"http://127.0.0.1:41234/v1/conversations/abc-123\")'", CD),
    ("node -e 'fetch(\"http://127.0.0.1:41234/v1/conversations/abc-123\", {method: \"delete\"})'", CD),
    ("curl -X GET http://127.0.0.1:41234/v1/conversations/abc-123", FREE),
    ("echo 'curl -X DELETE http://127.0.0.1:41234/v1/conversations/abc-123'", FREE),
    ("grep -rn 'delete /v1/conversations/x' docs", FREE),
]
for cmd, want in cases:
    check(cmd.replace("\n", "⏎")[:76], both(cmd), want)

# file writes by path
for path, want in [("~/.gemini/antigravity-cli/settings.json", "agent-settings"),
                   ("~/.gemini/config/hooks.json", "agent-settings"), ("./.agents/notes.md", None)]:
    hit = guard.analyze_path(path)
    check(f"write {path}", hit[0] if hit else None, want)

# the tools' older names are the same tools
for name, want in [("write_to_file", "write_file"), ("view_file", "read_file"), ("replace_file_content", "edit_file"),
                   ("run_command", "run_command"), ("something_new", "something_new")]:
    check(f"canonical_tool({name})", guard.canonical_tool(name), want)
saved = getattr(compat, "tool", None)
try:
    if saved:
        del compat.tool  # an older compat.py: the guard's own table
    check("canonical_tool without compat.tool", guard.canonical_tool("write_to_file"), "write_file")
finally:
    if saved:
        compat.tool = saved
out = guard.decide({"toolCall": {"name": "write_to_file",
                                 "args": {"TargetFile": os.path.join(HOME, ".gemini", "config", "hooks.json"),
                                          "CodeContent": "{}"}}})
check("decide(write_to_file ~/.gemini/config/hooks.json)", out.get("decision"), "force_ask")
out = guard.decide({"toolCall": {"name": "run_command", "args": {"CommandLine": "agy plugin disable antigravity-plus"}}})
check("decide(run_command agy plugin disable)", out.get("decision"), "force_ask")
# the LS of the CLI itself: a random port from $ANTIGRAVITY_LS_ADDRESS, no hub file
for cmd, want in [
    ('curl -X POST "$ANTIGRAVITY_LS_ADDRESS/exa.language_server_pb.LanguageServerService/SetUserSettings" -d "{}"', AS),
    ('curl -X POST http://$ANTIGRAVITY_LS_ADDRESS/product.v1.ConversationService/DeleteConversation -d "{}"', CD),
    ('curl -X POST "${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage" '
     '-d "{\\"text\\":\\"/bypass\\"}"', AS),
    ('curl "$ANTIGRAVITY_LS_ADDRESS/exa.language_server_pb.LanguageServerService/GetUserStatus"', FREE),
]:
    check(cmd[:76], both(cmd), want)
# an ask in a CLI conversation tells the agent itself what to do
cli = {"transcriptPath": os.path.join(HOME, ".gemini", "antigravity-cli", "brain", "c1", ".system_generated", "logs",
                                      "transcript_full.jsonl"),
       "toolCall": {"name": "run_command", "args": {"CommandLine": "agy plugin disable antigravity-plus"}}}
check("CLI ask explains itself", guard.HEADLESS_NOTE in guard.decide(cli).get("reason", ""), True)
check("App ask has no CLI note", guard.HEADLESS_NOTE in guard.decide({"toolCall": cli["toolCall"]}).get("reason", ""), False)
sys.exit(0 if ok else 1)
