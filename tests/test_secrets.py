"""Secret protection: secrets in files and commands that would print them."""
import json, os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import checks, guard as guard_mod
ok = True
def check(name, got, want):
    global ok
    good = got == want; ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {got}")
tg = "123456789:AA" + "h" * 10 + "Qz7xK2pL9mN4vB8cR1tY5wE3s"[:23]
samples = {
 "telegram bot token": (f'BOT_TOKEN = "{tg}"', True),
 "aws key": ('aws = "AKIAQWERTYUIOPASDFGH"', True),
 "aws docs example": ('aws = "AKIAIOSFODNN7EXAMPLE"', False),
 "github token": ('t = "ghp_' + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + '"', True),
 "hard-coded password": ('password = "S3cr3tPassw0rd!"', True),
 "password from env": ('password = os.getenv("DB_PASSWORD")', False),
 "placeholder key": ('api_key = "your_api_key_here"', False),
 "db url with password": ('DB = "postgres://admin:hunter22@db:5432/app"', True),
 "db url with ${}": ('DB = "postgres://user:${DB_PASS}@db/app"', False),
 "private key block": ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", True),
}
for name, (text, want) in samples.items():
    check(f"find_secrets: {name}", bool(checks.find_secrets(text)), want)
check("redact hides the value", "[скрыто]" in checks.redact('password = "S3cr3tPassw0rd!"') and "S3cr3t" not in checks.redact('password = "S3cr3tPassw0rd!"'), True)
# edit check in a real git repo
repo = os.path.join(SP, "sec_repo"); shutil.rmtree(repo, ignore_errors=True); os.makedirs(repo)
subprocess.run(["git", "init", "-q"], cwd=repo)
open(os.path.join(repo, ".gitignore"), "w").write(".env\n")
cfg = os.path.join(repo, "config.py"); open(cfg, "w").write(f'BOT_TOKEN = "{tg}"\n')
env = os.path.join(repo, ".env"); open(env, "w").write(f'BOT_TOKEN={tg}\n')
check("secret in a tracked file → warning", [p["kind"] for p in checks.secret_problems(cfg, repo)], ["secret"])
check("secret in .env (ignored) → nothing", checks.secret_problems(env, repo), [])
nogit = os.path.join(SP, "sec_nogit"); shutil.rmtree(nogit, ignore_errors=True); os.makedirs(nogit)
f2 = os.path.join(nogit, "config.py"); open(f2, "w").write(f'BOT_TOKEN = "{tg}"\n')
check("not a git repo → nothing", checks.secret_problems(f2, nogit), [])
# guard
def guard(tool, arg):
    key = "CommandLine" if tool == "run_command" else "Path"
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")], input=json.dumps({"conversationId": "agp-sec-test-01", "workspacePaths": [repo], "toolCall": {"name": tool, "args": {key: arg}}}), capture_output=True, text=True)
    return json.loads(r.stdout or "{}").get("decision")
for cmd, want in [("cat .env", "deny"), ("cat .env.example", "allow"), ("cat deploy/server.env", "deny"), ("env", "deny"),
                  ("printenv PATH", "allow"), ("echo $TELEGRAM_TOKEN", "deny"), ("echo $HOME", "allow"),
                  ("cat ~/.ssh/id_rsa", "force_ask"), ("grep -c '^BOT_TOKEN=' .env", "allow"), ("sed 's/=.*/=***/' .env", "allow"),
                  # ${VAR:+word} / ${VAR+word} print word only when VAR is set, ${#VAR} its length: not the value
                  ('echo "addr=${ADDR:-none} tok=${API_TOKEN:+set}"', "allow"), ("echo ${API_TOKEN+set}", "allow"),
                  ("echo ${#API_TOKEN}", "allow"), ("echo ${API_TOKEN:+${OTHER}}", "allow"),
                  ('echo "${API_TOKEN}"', "deny"), ("echo ${API_TOKEN:-x}", "deny"), ("echo ${API_TOKEN:0:4}", "deny"),
                  ("echo ${HOST:+$API_TOKEN}", "deny"), ('echo "${API_TOKEN:+set} ${DB_PASSWORD}"', "deny"),
                  # names only: the environment piped straight into a filter that drops the values
                  ('printenv | cut -d= -f1 | grep -i -E "APP|TOKEN" | sort', "allow"), ("env | cut -d '=' -f 1", "allow"),
                  ("env | awk -F= '{print $1}'", "allow"), ("export -p | sed 's/=.*//'", "allow"), ("compgen -e", "allow"),
                  ("env | grep NAME", "deny"), ("printenv", "deny"), ("env | sort", "deny"), ("env | sort | cut -d= -f1", "deny"),
                  ("env | cut -d= -f2", "deny"), ("env | cut -d= -f1-", "deny"), ("env | awk -F= '{print $2}'", "deny"),
                  ("env | sed 's/=.*/=&/'", "deny")]:
    check(f"guard: {cmd}", guard("run_command", cmd), want)
for path, want in [(env, "deny"), (os.path.expanduser("~/.ssh/id_ed25519.pub"), "allow"), (cfg, "allow")]:
    check(f"guard read_file: {os.path.basename(path)}", guard("read_file", path), want)

# 2.35.1 updates: env-prefixed env/printenv, ConversationService / LanguageServerService RPCs, settings/lsp.json writes
for cmd, want_dec, want_rule in [
    ("AGY_ACCOUNT=work env", "deny", "secret-print"),
    ("AGY_ACCOUNT=work printenv", "deny", "secret-print"),
    ("env FOO=bar printenv", "deny", "secret-print"),
    ("AGY_ACCOUNT=work /usr/bin/env", "deny", "secret-print"),
    ("env -i printenv", "deny", "secret-print"),
    ("AGY_ACCOUNT=work printenv | cut -d= -f1", "allow", None),
]:
    hit = guard_mod.secret_print(cmd)
    check(f"guard env-prefix: {cmd}", (guard("run_command", cmd), hit[0] if hit else None), (want_dec, want_rule))

create_bypass = 'curl -X POST http://127.0.0.1:9999/product.v1.ConversationService/CreateConversation -d \'{"prompt":[{"text":"/bypass"}]}\''
create_split = 'curl -X POST http://127.0.0.1:9999/product.v1.ConversationService/Create""Conversation -d \'{"prompt":[{"text":"/bypass"}]}\''
del_conv = 'curl -X POST http://127.0.0.1:9999/product.v1.ConversationService/DeleteConversation -d \'{"conversationId":"abc"}\''
del_conv_q = 'curl -X POST "http://127.0.0.1:9999/product.v1.ConversationService/DeleteConversation" -d "{}"'
del_conv_py = 'python3 -c "import urllib.request; urllib.request.urlopen(\\"http://127.0.0.1:9999/product.v1.ConversationService/DeleteConversation\\")"'
upd_perm = "curl -X POST http://127.0.0.1:9999/exa.language_server_pb.LanguageServerService/UpdatePermissionSettings -d '{}'"
upd_perm_esc = "curl -X POST http://127.0.0.1:9999/exa.language_server_pb.LanguageServerService/Update\\PermissionSettings -d '{}'"
upd_perm_gitc = 'git -c alias.p="!curl -X POST http://127.0.0.1:9999/exa.language_server_pb.LanguageServerService/UpdatePermissionSettings -d {}" p'
upd_perm_heredoc = "python3 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('http://127.0.0.1:9999/exa.language_server_pb.LanguageServerService/UpdatePermissionSettings')\nEOF"
mgr_side = "curl -X POST http://127.0.0.1:9999/exa.language_server_pb.LanguageServerService/ManageSidecar -d '{}'"

for label, cmd, want_dec, want_rule, want_bypass_rule in [
    ("ConversationService/CreateConversation /bypass", create_bypass, "force_ask", "agent-settings", "agent-settings"),
    ("ConversationService/CreateConversation quote-split", create_split, "force_ask", "agent-settings", "agent-settings"),
    ("ConversationService/DeleteConversation", del_conv, "force_ask", "conversation-delete", None),
    ("ConversationService/DeleteConversation quoted", del_conv_q, "force_ask", "conversation-delete", None),
    ("ConversationService/DeleteConversation python3 -c", del_conv_py, "force_ask", "conversation-delete", None),
    ("LanguageServerService/UpdatePermissionSettings", upd_perm, "force_ask", "agent-settings", "agent-settings"),
    ("LanguageServerService/UpdatePermissionSettings escaped", upd_perm_esc, "force_ask", "agent-settings", "agent-settings"),
    ("LanguageServerService/UpdatePermissionSettings git -c", upd_perm_gitc, "force_ask", "agent-settings", "agent-settings"),
    ("LanguageServerService/UpdatePermissionSettings python heredoc", upd_perm_heredoc, "force_ask", "agent-settings", "agent-settings"),
    ("LanguageServerService/ManageSidecar", mgr_side, "allow", None, None),
]:
    h_norm = guard_mod.analyze_command(cmd)
    h_byp = guard_mod.analyze_command(cmd, system_only=True)
    check(f"guard RPC: {label}",
          (guard("run_command", cmd), h_norm[0] if h_norm else None, h_byp[0] if h_byp else None),
          (want_dec, want_rule, want_bypass_rule))

# End-to-end Bypass Mode subprocess check for DeleteConversation vs UpdatePermissionSettings vs ManageSidecar
bp_tp = os.path.join(SP, "sec_bypass_tp.jsonl")
with open(bp_tp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-24T10:00:00Z",
                        "content": "<USER_REQUEST>/bypass</USER_REQUEST>"}) + "\n")
def guard_bypass(cmd):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")],
                       input=json.dumps({"conversationId": "agp-sec-bypass-01", "transcriptPath": bp_tp,
                                         "workspacePaths": [repo],
                                         "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}),
                       capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout or "{}").get("decision")
check("guard bypass: DeleteConversation auto-approved", guard_bypass(del_conv), "auto_approve")
check("guard bypass: UpdatePermissionSettings still force_ask", guard_bypass(upd_perm), "force_ask")
check("guard bypass: ManageSidecar auto-approved", guard_bypass(mgr_side), "auto_approve")
for p in (bp_tp, os.path.expanduser("~/.gemini/antigravity-plus/settings.json")):
    if os.path.exists(p):
        os.remove(p)

for target in ("~/.gemini/antigravity/settings.json", "~/.gemini/antigravity-cli/mcp_config.json", "~/.gemini/antigravity-plus/lsp.json"):
    exp = os.path.expanduser(target)
    phit = guard_mod.analyze_path(exp)
    check(f"guard write/edit {target}",
          (phit[0] if phit else None, guard("write_file", exp), guard("edit_file", exp)),
          ("agent-settings", "force_ask", "force_ask"))

dir_cp = guard_mod.analyze_command("cp /tmp/settings.json ~/.gemini/antigravity/", system_only=True)
check("guard cp into ~/.gemini/antigravity/", dir_cp[0] if dir_cp else None, "agent-settings")

# secret_print(): a program that prints the environment is stopped through every wrapper that only starts it (0.27.1's
# reading — only `sudo`, `command` and `builtin` words dropped — must never lose a stop), and a command that merely
# looks like one is not (`command -v env`, `env --version`, `printenv | cut -d= -f1`)
STOPS = ["env", "printenv", "set", "export", "export -p", "declare -x", "env FOO=bar", "sudo env", "sudo -E env", "sudo -u x printenv",
         "sudo -u root -- env", "sudo --user=x env", "sudo -n -k printenv", "sudo sudo env", "doas env", "doas -u x env", "nohup env",
         "nohup printenv", "time env", "time -p env", "time -p printenv", "exec env", "exec -a x env", "exec -c printenv", "env -i",
         "env -i FOO=1", "env -u X", "env -u X FOO=1", "env -S 'printenv'", "env -S printenv", "env -i -S 'env'", "command env",
         "command printenv", "command -p env", "builtin printenv", "builtin export -p", "AGY_ACCOUNT=work env",
         "AGY_ACCOUNT=work printenv", "AGY_ACCOUNT=work /usr/bin/env", "FOO=1 BAR=2 printenv", "/usr/bin/env", "/usr/bin/printenv",
         "/usr/bin/env FOO=1 printenv", "/usr/bin/env -i printenv", "env sudo", "env sudo printenv", "nice -n 5 env", "timeout 5 env",
         "timeout -k 1 5 printenv", "cat .env", "sudo cat .env", "AGY=1 cat .env", "less .env", "head -3 .env.local",
         "printenv | cut -d= -f2", "printenv | sort", "env | head", "env | grep PATH", "printenv | tee out.txt", "cd /tmp; env",
         "ls && printenv", "ls; sudo -E printenv", "true || env", "echo hi | env", "echo $TELEGRAM_TOKEN", "sudo echo $GITHUB_TOKEN",
         "FOO=1 echo $API_KEY", "printf '%s' \"$DB_PASSWORD\"", "gci env:", "Get-ChildItem env:"]
FREE_CMDS = ["printenv PATH", "env FOO=bar ls", "env -i bash -c true", "command -v env", "command -V printenv", "which env", "type env",
             "env --help", "env --version", "env --version | head -1", "export FOO=bar", "export -n FOO", "set -e", "set -euo pipefail",
             "FOO=1", "sudo", "sudo -l", "time", "exec", "nohup", "printenv | cut -d= -f1", "env | cut -d= -f1", "sudo env | cut -d= -f1",
             "AGY_ACCOUNT=work printenv | cut -d= -f1", "/usr/bin/env | cut -d= -f1", "env | awk -F= '{print $1}'",
             "env | sed 's/=.*/=***/'", "cat .env.example", "cat ~/.ssh/id_rsa.pub", "grep -c TOKEN .env", "sed 's/=.*/=***/' .env",
             "echo ${API_TOKEN:+set}", "echo $HOME", "ls -la", "docker exec app env", "ssh h env", "xargs env"]
check(f"secret_print stops these {len(STOPS)} environment/secret printers", [c for c in STOPS if not guard_mod.secret_print(c)], [])
check(f"secret_print lets these {len(FREE_CMDS)} commands through", [c for c in FREE_CMDS if guard_mod.secret_print(c)], [])

shutil.rmtree(repo); shutil.rmtree(nogit)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
