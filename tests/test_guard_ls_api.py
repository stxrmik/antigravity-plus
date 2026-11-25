"""Guard: Antigravity's own control routes (0.28.0): the language server's control plane protected by verb (not by a list
of names), the usage panel's API and the removal of a conversation read through the shell's and curl's own spelling
tricks, a message that switches Bypass Mode on, and plain text that only mentions all of it. Synthetic examples only."""
import os, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat, guard
compat.WINDOWS = False  # POSIX shell syntax
ok = True
LS = "http://127.0.0.1:41234/exa.language_server_pb.LanguageServerService"
CS = "http://127.0.0.1:41234/product.v1.ConversationService"
PANEL = "http://127.0.0.1:8765"


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<76} → {got}" + ("" if good else f" (want {want})"))


def rule(cmd, bypass=False):
    hit = guard.analyze_command(cmd, system_only=bypass)
    return hit[0] if hit else None


def both(cmd):
    return rule(cmd), rule(cmd, True)


AS = ("agent-settings", "agent-settings")  # asks even in Bypass Mode
CD = ("conversation-delete", None)         # asks, but Bypass Mode approves
FREE = (None, None)
post = lambda url, body="{}": f"curl -s -X POST {url} -d '{body}'"  # noqa: E731
UPD = "UpdatePermissionSettings"

# 1. the control plane through spelling tricks (all went through unseen in the first 0.28.0)
for label, cmd in [
    ("plain", post(f"{LS}/{UPD}")),
    ("/./ segment", post(f"{LS}/./{UPD}")), ("x/../ segment", post(f"{LS}/Foo/../{UPD}")),
    ("// slashes", post(f"{LS}//{UPD}")), ("percent-encoded %55", post(f"{LS}/%55pdatePermissionSettings")),
    ("percent-encoded dot segment", post(f"{LS}/%2e/{UPD}")),
    ("ANSI-C hex $'\\x55'", post(f"{LS}/$'\\x55'pdatePermissionSettings")),
    ("ANSI-C octal $'\\125'", post(f"{LS}/$'\\125'pdatePermissionSettings")),
    ("ANSI-C unicode $'\\u0055'", post(f"{LS}/$'\\u0055'pdatePermissionSettings")),
    ("variable $M", f"M={UPD}; curl -s -X POST {LS}/$M"), ("variable ${M}", f"M={UPD}; curl -s -X POST {LS}/${{M}}"),
    ("variable in double quotes", f"M={UPD} && curl -s -X POST \"{LS}/$M\""),
    ("export on an earlier line", f"export M={UPD}\ncurl -s -X POST {LS}/$M"),
    ("variable chain", f"A=Update; B=${{A}}PermissionSettings; curl -s -X POST {LS}/$B"),
    ("variable default ${X:-…}", f"curl -s -X POST {LS}/${{X:-{UPD}}}"),
    ("variable inside bash -c", f"bash -c 'M={UPD}; curl -s -X POST {LS}/$M'"),
    ("variable into a heredoc", f"M=Update; python3 - <<EOF\nimport urllib.request\nurllib.request.urlopen('{LS}/${{M}}PermissionSettings')\nEOF"),
    ("curl glob {…}", f"curl -s -X POST {LS}/{{{UPD}}}"), ("curl glob [U]", f"curl -s -X POST '{LS}/[U]pdatePermissionSettings'"),
    ("curl glob {a,b}", f"curl -s -X POST {LS}/{{GetStatus,{UPD}}}"), ("curl glob [A-Z]", f"curl -s -X POST {LS}/[A-Z]pdatePermissionSettings"),
    ("quote split", post(f"{LS}/Upd''atePermissionSettings")), ("backslash", post(f"{LS}/Update\\PermissionSettings")),
    ("python string join", f"python3 -c \"import urllib.request as u; u.urlopen('{LS}/' + 'Update' + 'PermissionSettings')\""),
    ("node -e", f"node -e \"fetch('{LS}/{UPD}', {{method:'POST'}})\""),
    ("gh api", f"gh api -X POST {LS}/{UPD}"),
    ("the panel's route through /./", post(f"{PANEL}/api/./toggle")), ("the panel's route percent-encoded", post(f"{PANEL}/api/%74oggle")),
    ("the panel's route through a variable", f"P=toggle; curl -s -X POST {PANEL}/api/$P"),
]:
    check(f"spelling: {label}", both(cmd), AS)

# 2. by verb
for method in ["SetUserSettings", "SetMendelFlagOverrides", "SetWorkspaceTrust", "UpdatePermissionSettings", "UpdateCustomization",
               "UpdateTrigger", "UpdateProject", "InstallCascadePlugin", "InstallCustomization", "UninstallPlugin", "ToggleMcpServer",
               "RevokePluginPermissionGrant", "GrantPluginPermission", "DisconnectMcpOAuth", "DisconnectRemotePlugin", "ResetSomething",
               "HandleCascadeUserInteraction", "CreateCustomizationFile", "DeletePlugin", "DeleteAllUserData", "DeleteSkill", "DeleteTrigger",
               "DeleteWorktree", "DeleteProject", "DeleteCascadeMemory"]:
    check(f"verb: {method} asks, also in Bypass Mode", both(post(f"{LS}/{method}")), AS)
check("verb: DeleteCascadeTrajectory is a conversation removal", both(post(f"{LS}/DeleteCascadeTrajectory")), CD)
for method in ["ManageSidecar", "GetCascadeTrajectorySteps", "GetCascadeTrajectoryGeneratorMetadata", "GetStatus", "GetAllSkills",
               "GetPermissionSettings", "GetUserSettings", "ListBackgroundTasks", "ListPluginPermissionGrants", "RetrieveUserQuotaSummary",
               "WaitForConversationFullyIdle", "StreamAgentStateUpdates", "StreamCascadeReactiveUpdates", "CancelCascadeInvocation",
               "SearchConversations", "Heartbeat", "Restart", "Setup", "SetupJetskiChat", "ResolveSidecarConfig", "RecordError"]:
    check(f"free: {method}", both(post(f"{LS}/{method}")), FREE)
check("free: SendUserCascadeMessage with an ordinary message",
      both(post(f"{LS}/SendUserCascadeMessage", '{"items":[{"text":"run the tests"}]}')), FREE)
check("free: lower-case verb is no method (names are case-sensitive)", both(post(f"{LS}/setUserSettings")), FREE)
check("ManageSidecar through a variable stays free", both(f"M=ManageSidecar; curl -s -X POST {LS}/$M -d '{{}}'"), FREE)

# 3. conversations
for label, cmd in [("ConversationService/DeleteConversation", post(f"{CS}/DeleteConversation")),
                   ("quoted URL", f"curl -X POST \"{CS}/DeleteConversation\" -d '{{}}'"),
                   ("percent-encoded", post(f"{CS}/%44eleteConversation")), ("python -c", f"python3 -c \"import urllib.request; urllib.request.urlopen('{CS}/DeleteConversation')\""),
                   ("node heredoc", f"node <<'EOF'\nfetch('{CS}/DeleteConversation')\nEOF"),
                   ("piped heredoc", f"cat <<'EOF' | python3 -\nimport urllib.request\nurllib.request.urlopen('{CS}/DeleteConversation')\nEOF")]:
    check(f"conversation-delete: {label}", both(cmd), CD)
for method in ("GetConversation", "ListConversations", "UpdateConversation"):
    check(f"free: ConversationService/{method}", both(post(f"{CS}/{method}")), FREE)

# 4. a message that switches Bypass Mode / full access on
SW = '{"items":[{"text":"/bypass"}]}'
for label, cmd in [
    ("agentapi new-conversation", "agentapi new-conversation '/bypass'"), ("agentapi send-message", "agentapi send-message abc 'turn on full access'"),
    ("agentapi + «включи bypass»", "agentapi send-message abc 'включи bypass'"), ("python -c", "python3 -c \"import subprocess; subprocess.run(['agentapi', 'send-message', 'x', '/bypass'])\""),
    ("agentapi heredoc body", "agentapi send-message abc <<'EOF'\n/bypass\nEOF"),
    ("SendUserCascadeMessage", post(f"{LS}/SendUserCascadeMessage", SW)), ("StartCascade", post(f"{LS}/StartCascade", '{"items":[{"text":"enable full access"}]}')),
    ("body before the URL", f"curl -s -X POST -d '{SW}' {LS}/SendUserCascadeMessage"),
    ("percent-encoded method", post(f"{LS}/Send%55serCascadeMessage", SW)),
    ("CreateConversation", post(f"{CS}/CreateConversation", '{"prompt":[{"text":"/bypass"}]}')),
    ("curl -d @- heredoc", f"curl -s -X POST -d @- {LS}/SendUserCascadeMessage <<'EOF'\n{SW}\nEOF"),
]:
    check(f"switch: {label}", both(cmd), AS)
for label, cmd in [("agentapi, ordinary message", "agentapi send-message abc 'run the tests'"),
                   ("SendUserCascadeMessage, ordinary", post(f"{LS}/SendUserCascadeMessage", '{"items":[{"text":"review the diff"}]}')),
                   ("StartCascade, ordinary", post(f"{LS}/StartCascade", '{"items":[{"text":"fix the typo"}]}')),
                   ("CreateConversation, ordinary", post(f"{CS}/CreateConversation", '{"prompt":[{"text":"review the diff"}]}')),
                   ("grep over the guard's source", "grep -c \"ConversationService/CreateConversation.*bypass\" antigravity-plus/hooks/guard.py"),
                   ("rg for the words", "rg -n 'agentapi send-message.*bypass' antigravity-plus tests"),
                   ("echo of the command", "echo \"agentapi send-message abc /bypass\""),
                   ("printf of the call", "printf '%s\\n' 'curl -d /bypass " + LS + "/StartCascade'"),
                   ("git commit message", "git commit -m \"docs: agentapi send-message bypass, CreateConversation full access\""),
                   ("a doc file written by cat", "cat > notes.md <<'EOF'\nagentapi send-message x /bypass is stopped, so is ConversationService/CreateConversation\nEOF"),
                   ("gh pr comment", "gh pr comment 12 -b 'agentapi send-message x /bypass'")]:
    check(f"switch exempt: {label}", both(cmd), FREE)

# 5. plain text that mentions the routes
for label, cmd in [
    ("grep for a method", f"grep -rn 'LanguageServerService/{UPD}' antigravity-plus"), ("rg for a method", "rg -n 'LanguageServerService/DeletePlugin' tests"),
    ("echo with a URL", f"echo 'curl {PANEL}/api/toggle'"), ("printf with a URL", f"printf '%s' '{CS}/DeleteConversation'"),
    ("git commit -m", f"git commit -m 'guard: protects LanguageServerService/{UPD} and {PANEL}/api/toggle'"),
    ("git log --grep", "git log --grep='LanguageServerService/DeletePlugin' --oneline"),
    ("gh pr create body", f"gh pr create --title x --body \"protects LanguageServerService/{UPD}\""),
    ("gh pr create --body=", f"gh pr create --title=x --body='protects LanguageServerService/{UPD}'"),
    ("gh pr create title", "gh pr create --title 'LanguageServerService/DeletePlugin is asked about' --body-file /tmp/body.md"),
    ("gh release notes", "gh release create v1 --notes '… DeletePlugin LanguageServerService/DeletePlugin'"),
    ("gh release -n", f"gh release create v1 -n 'LanguageServerService/{UPD}'"), ("gh issue create -t -b", f"gh issue create -t 'a {CS}/DeleteConversation' -b 'b'"),
    ("gh pr edit --body", f"gh pr edit 3 --body 'LanguageServerService/{UPD}'"), ("gh pr comment -m", f"gh pr comment 3 -m 'LanguageServerService/{UPD}'"),
    ("glab mr create", f"glab mr create --title t --description 'LanguageServerService/{UPD}'"),
]:
    check(f"text: {label}", both(cmd), FREE)
for label, cmd, want in [
    ("gh api calls a URL", f"gh api -X POST {LS}/{UPD}", AS), ("gh -R … api", f"gh -R o/r api {LS}/{UPD}", AS),
    ("gh api with a body option", f"gh api -X POST {LS}/{UPD} -f body=x", AS),
    ("a command substitution in the gh body still runs", f"gh pr create --title x --body \"$(curl -s -X POST {LS}/{UPD})\"", AS),
    ("a gh option that is no text option", f"gh pr create --title x --body x --repo 'LanguageServerService/{UPD}'", AS),
    ("python3 -c", f"python3 -c \"import urllib.request; urllib.request.urlopen('{LS}/{UPD}')\"", AS),
    ("node -e", f"node -e \"fetch('{LS}/{UPD}')\"", AS),
    ("python3 heredoc", f"python3 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('{LS}/{UPD}')\nEOF", AS),
    ("bash heredoc", f"bash <<'EOF'\ncurl -X POST {LS}/{UPD}\nEOF", AS),
    ("a heredoc that only writes a doc", f"cat > notes.md <<'EOF'\nthe guard protects LanguageServerService/{UPD}\nEOF", FREE),
    ("a python heredoc that reads a method that is free", f"python3 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('{LS}/GetStatus')\nEOF", FREE),
]:
    check(f"code and URLs still checked: {label}", both(cmd), want)

# 6. git and code runners are no loophole
for label, cmd in [
    ("git -c alias", f"git -c alias.p='!curl -X POST {LS}/{UPD}' p"),
    ("GIT_CONFIG_COUNT/KEY_0/VALUE_0", f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0='!curl -X POST {LS}/{UPD}' git p"),
    ("GIT_SSH_COMMAND", f"GIT_SSH_COMMAND='curl -X POST {LS}/{UPD}' git fetch origin"),
    ("env GIT_PAGER", f"env GIT_PAGER='curl -X POST {LS}/{UPD}' git log"),
    ("--exec-path", f"git --exec-path=/tmp/x commit -m 'note {LS}/{UPD}'"),
    ("config alias", f"git config alias.p '!curl -X POST {LS}/{UPD}'"), ("config core.sshCommand", f"git config core.sshCommand 'curl {LS}/{UPD}'"),
    ("rebase -x", f"git rebase -x 'curl -X POST {LS}/{UPD}' HEAD~2"), ("rebase --exec", f"git rebase --exec 'curl -X POST {LS}/{UPD}' HEAD~2"),
    ("bisect run", f"git bisect run sh -c 'curl -X POST {LS}/{UPD}'"),
    ("clone --upload-pack", f"git clone --upload-pack 'curl {LS}/{UPD}' u d"),
    ("node heredoc", f"node <<'EOF'\nfetch('{LS}/{UPD}', {{method: 'POST'}})\nEOF"),
    ("ruby heredoc", f"ruby <<'EOF'\nrequire 'net/http'\nNet::HTTP.post(URI('{LS}/{UPD}'), '{{}}')\nEOF"),
    ("perl heredoc", f"perl <<'EOF'\nsystem('curl', '{LS}/{UPD}');\nEOF"), ("php heredoc", f"php <<'EOF'\n<?php file_get_contents('{LS}/{UPD}');\nEOF"),
    ("python3.13 - heredoc", f"python3.13 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('{LS}/{UPD}')\nEOF"),
    ("env node heredoc", f"env node <<'EOF'\nfetch('{LS}/{UPD}')\nEOF"),
    ("cat | python3 -", f"cat <<'EOF' | python3 -\nimport urllib.request\nurllib.request.urlopen('{LS}/{UPD}')\nEOF"),
    ("cat | node -", f"cat <<'EOF' | node -\nfetch('{LS}/{UPD}')\nEOF"),
    ("cat | bash", f"cat <<'EOF' | bash\ncurl -X POST {LS}/{UPD}\nEOF"),
]:
    check(f"no loophole: {label}", both(cmd), AS)
check("no loophole: filter-branch (rewriting history asks first; in Bypass Mode the call itself)",
      both(f"git filter-branch --tree-filter 'curl {LS}/{UPD}' HEAD"), ("git-history-rewrite", "agent-settings"))
for label, cmd in [("git status", "git status -sb"), ("git push with a prompt variable", "GIT_TERMINAL_PROMPT=0 git push -q origin main"),
                   ("git rebase --continue", "GIT_EDITOR=true git rebase --continue"), ("git commit", "git add -A && git commit -m 'wip'"),
                   ("git log --format", "git log --format='%h %s' -n 5"), ("python3 heredoc, plain", "python3 - <<'EOF'\nprint('hi')\nEOF"),
                   ("node script", "node scripts/build.js"), ("curl to a project API", "curl -s http://localhost:3000/api/settings")]:
    check(f"stays free: {label}", both(cmd), FREE)

# 6b. text that is handed to a socket is a request, not text
for label, cmd, want in [
    ("printf | nc", f"printf 'POST /exa.language_server_pb.LanguageServerService/SetUserSettings HTTP/1.1\\r\\n\\r\\n' | nc 127.0.0.1 41234", AS),
    ("echo | curl -d @-", f"echo '{SW}' | curl -s -X POST -d @- {LS}/SendUserCascadeMessage", AS),
    ("echo | curl -d @- (the call)", f"echo '{{}}' | curl -s -X POST -d @- {LS}/{UPD}", AS),
    ("/dev/tcp and printf", f"exec 3<>/dev/tcp/127.0.0.1/41234; printf 'POST /exa.language_server_pb.LanguageServerService/{UPD} HTTP/1.1\\r\\n' >&3", AS),
    ("echo | curl, ordinary", "echo 'hello' | curl -s -X POST -d @- https://example.com/api", FREE),
    ("echo | nc -l", "echo ok | nc -l 9000", FREE),
    ("printf, no socket", f"printf '%s\\n' 'POST /exa.language_server_pb.LanguageServerService/{UPD}'", FREE),
]:
    check(f"socket: {label}", both(cmd), want)

# 7. what the rules only ask about, with the reasons in Russian
for cmd, word in [(post(f"{LS}/SetUserSettings"), "языкового сервера"), (post(f"{CS}/DeleteConversation"), "DeleteConversation"),
                  (post(f"{LS}/DeleteCascadeTrajectory"), "DeleteCascadeTrajectory"), ("agentapi send-message x '/bypass'", "Bypass Mode")]:
    hit = guard.analyze_command(cmd)
    check(f"reason names it: {cmd[:50]}", bool(hit) and word in hit[1], True)

# 8. bounded and linear: a long command or a heap of globs must not hang the guard, nor hide a call behind them
junk = "x " * 15000
for label, cmd in [("30 KB of words", f"echo {junk}; curl -s -X POST {LS}/{UPD}"), ("30 KB of $M", f"M=a; curl -s {'$M ' * 10000}"),
                   ("30 KB of braces", "curl -s " + "{a,b} " * 5000), ("one 40 KB word", "curl -s " + "x" * 40000 + f" {LS}/{UPD}"), ("100 KB of spaces", "curl -s " + " " * 100000 + f"{LS}/{UPD}"),
                   ("30 KB of quotes and +", "curl -s " + "'a' + " * 5000), ("30 KB of ANSI-C", "curl -s " + "$'\\x41'" * 5000),
                   ("deep nesting of globs", "curl -s http://h/" + "[Uu]" * 40),
                   ("many assignments", "; ".join(f"V{i}=x" for i in range(2000)) + f"; curl -s {LS}/{UPD}")]:
    t0 = time.time()
    got = both(cmd)
    took = time.time() - t0
    check(f"bounded: {label} ({took:.2f}s)", took < 30, True)
    if label in ("30 KB of words", "100 KB of spaces", "many assignments"):
        check(f"bounded: {label} still sees the call", got, AS)
check("too many globs in one address: asked about, not guessed", both("curl -s http://h/" + "[Uu]" * 40 + "/x"), AS)
check("a sed regex full of [a-z] classes is no address (seen live: masking emails)",
      both("tool --help 2>&1 | sed -E 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/<email>/g' | head -20"), FREE)
check("…while the same classes in a local address still ask",
      both("curl -s 127.0.0.1:41234/[A-Za-z0-9._%+-][A-Za-z0-9.-]x"), AS)
check("a JSON body full of commas is no glob",both(post(f"{LS}/GetCascadeTrajectorySteps", '{"a":[1,2,3],"b":{"c":1,"d":2},"e":[{"f":1,"g":2},{"h":3,"i":4}]}')), FREE)

# 9. the rule ids: agent-settings is a system rule (asks in Bypass Mode), conversation-delete is not
check("agent-settings is a system rule, conversation-delete is not", ("agent-settings" in guard.SYSTEM_RULES, "conversation-delete" in guard.SYSTEM_RULES), (True, False))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
