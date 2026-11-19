#!/usr/bin/env python3
r"""Antigravity Plus — PreToolUse guard.

Stops and asks the user (decision "force_ask", which ignores cached "always allow" grants) before:
  * run_command (sh/bash/zsh on macOS and Linux, PowerShell or cmd on Windows): irreversible or outward-facing
    commands — rm -rf / find -delete / Remove-Item -Recurse -Force on /, ~, C:\, . or * (also inside `sh -c '…'`,
    `$(…)` or `env -S '…'`), git reset --hard,
    git clean -f, force-push, --no-verify, DROP/TRUNCATE via a DB client, mkfs/dd to a device,
    curl | sh, sudo, terraform destroy, kubectl delete, npm publish, gh pr merge, keychain/SSH-key reads,
    changes to system folders (/System, /usr, /etc, /Library, C:\Windows…) by rm/mv/cp/chmod/tee/sed -i/redirection…
    — what runs on a server or in a container (`ssh host "sudo cp -a /etc/nginx /root/backup/"`, `docker exec …`)
    is checked without the stops that only protect this computer (LOCAL_RULES: its system folders, admin rights…);
  * write_file / edit_file: writes into .git/ internals, shell startup files, SSH keys, LaunchAgents, /etc and other
    system folders, and into what decides what the agent may do: Antigravity's settings and projects, MCP servers,
    installed plugins (this guard among them), Antigravity Plus's own settings — by file tools or by commands; also
    connecting an MCP server (/settings), turning secret protection off, and starting a conversation (agentapi) with
    a message that would switch Bypass Mode or full access on — the agent changes these only on the user's explicit
    request, and the user confirms;
  * git and gh commands that would wait at the keyboard in Antigravity's terminal (`git add -p`, `gh pr create`
    without a title and body, `gh auth login`…) or fail on its GIT_EDITOR=false (`git rebase --continue`) — refused
    ("deny") with the form that works (hooks/gitcheck.py, setting gitChecks);
  * run_command that only prints success and checks nothing (`echo "tests passed"`, hooks/facts.py) — refused
    ("deny"), so the agent runs the real check or says it couldn't;
  * write_file / edit_file over a file that changed since this agent last read or wrote it (hooks/coord.py) —
    refused ("deny") with who changed it, so the agent re-reads and retries without bothering the user;
  * push / deploy / upload to a server, or git commit — only when the user said not to in this conversation
    ("ничего не пушь", "don't commit") and hasn't lifted it since ("пушь"); see hooks/conversation.py.
Writes of memory files (skills/memory/scripts/memory.py) get "auto_approve", so auto-memory never stops for a
permission prompt — unless the text looks like a secret (a token, a private key, a password in a URL): memory goes
into every later conversation, so that write is refused and the agent writes where the secret is kept instead
(protectSecrets). A new file in the memory format written anywhere else (weak models shorten the long path to
`<project>/memory/…`) is refused with the exact folder it belongs in. Under full access (one grant from the user, hooks/conversation.py) every command and edit not
listed above gets "auto_approve" too; the destructive ones and the user's explicit bans still stop.
Bypass Mode (the user's «/bypass», kept in settings for every conversation until «/bypass off»; hooks/conversation.py)
goes further: every command, edit and read gets "auto_approve", and of the stops above only the system-level ones
remain (SYSTEM_RULES: admin rights, system folders and OS settings, disks, power, autostart, the Keychain and private
SSH keys, wiping the home folder or a disk, the agent's own permissions) together with the user's explicit bans;
refusals that aren't questions (secrets printed, fake checks, stale files, git commands that would hang) stay.
What it lets through is still logged, as "bypass:<rule>".
A delegated agent's `delegate.py ask|tell` (its questions and messages for Claude Code, which only print a line) gets
"auto_approve" — only the installed script in ~/.claude/skills/antigravity, and only as one plain command.

Everything else gets "allow", which in Antigravity means "no objection — delegate to the next hook or the
default policy" (it is NOT auto-approval; that is "auto_approve"), so your permission settings apply unchanged.
An empty decision would be treated as deny, so every path below returns an explicit decision.
Matches are appended to ~/.gemini/antigravity-plus/guard.log (shown by /doctor).

    guard.py --test "<command>" [--said "<user message>" …] [--bypass]   # the verdict for a command (no stdin)

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import posixpath
import re
import shlex
import socket
import sys
from typing import AbstractSet, Dict, List, Optional, Set, Tuple

import checks
import compat
import conversation as conv
import coord

try:
    import facts
except Exception:  # the fake-check rule is optional; the guard must work without it
    facts = None
try:
    import gitcheck
except Exception:  # optional like facts: git commands that would hang the agent
    gitcheck = None
try:
    import speed
except Exception:  # optional: sandbox bypass overwrite
    speed = None
try:
    import orchestrator
except Exception:  # optional: exit_plan_mode checks
    orchestrator = None

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
try:
    import memory as memstore
except Exception:  # memory is optional; the guard must work without it
    memstore = None

LOG = os.path.expanduser("~/.gemini/antigravity-plus/guard.log")
PASS = {"decision": "allow"}  # = delegate to Antigravity's default permission policy
AUTO = {"decision": "auto_approve"}
I = re.I

# (rule id, regex, reason). Regexes run on each shell segment (split on ; & && || | newlines outside quotes and
# $(…): split_unquoted), except PIPE_RULES which run on the whole command line.
SEGMENT_RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("git-reset-hard", re.compile(r"\bgit\b.*\breset\b.*--hard\b"),
     "`git reset --hard` безвозвратно удалит незакоммиченные изменения."),
    ("git-clean", re.compile(r"\bgit\b.*\bclean\b.*(\s-[a-zA-Z]*f|\s--force\b)"),
     "`git clean -f` удалит неотслеживаемые файлы без возможности восстановления."),
    ("git-checkout-all", re.compile(r"\bgit\b.*\bcheckout\b(\s+-f\b|\s+--force\b|(\s+--)?\s+\.(\s|$))"),
     "`git checkout .` / `-f` отбросит все незакоммиченные изменения."),
    ("git-restore-all", re.compile(r"\bgit\b.*\brestore\b(?!.*--staged\b(?!.*--worktree)).*\s(\.|:/)(\s|$)"),
     "`git restore .` отбросит все незакоммиченные изменения в рабочей копии."),
    ("git-force-push", re.compile(r"\bgit\b.*\bpush\b.*(\s--force(-with-lease)?\b|\s-[a-zA-Z]*f\b|\s--mirror\b|\s--delete\b|\s-d\b|\s:[\w./-]+|\s\+[\w./-]+)"),
     "force-push / удаление удалённой ветки перепишет или удалит историю на сервере."),
    ("git-branch-D", re.compile(r"\bgit\b.*\bbranch\b.*(\s-D\b|\s--delete\b.*\s--force\b|\s-d\b.*\s-f\b)"),
     "`git branch -D` удалит ветку даже с неслитыми коммитами."),
    ("git-stash-drop", re.compile(r"\bgit\b.*\bstash\s+(drop|clear)\b"),
     "`git stash drop/clear` безвозвратно удалит отложенные изменения."),
    ("git-history-rewrite", re.compile(r"\bgit\b.*\b(filter-branch|filter-repo|reflog\s+expire|update-ref\s+-d)\b|\bgit\b.*\bgc\b.*--prune=now"),
     "команда переписывает или чистит историю git."),
    ("git-no-verify", re.compile(r"\bgit\b.*\b(commit|push|merge|rebase|am)\b.*\s--no-verify\b"),
     "`--no-verify` пропускает git-хуки (pre-commit/pre-push) — обычно так делать нельзя без явной просьбы."),
    ("disk-format", re.compile(r"\bmkfs(\.\w+)?\b|\bdd\b.*\bof=/dev/|>\s*/dev/(r?disk|sd|nvme|hd)\w*|\bdiskutil\s+(erase\w*|partitionDisk|zeroDisk|secureErase|reformat|apfs\s+delete\w*)\b"),
     "команда пишет напрямую на диск / форматирует раздел."),
    ("system-power", re.compile(r"^\s*(sudo\s+)?(shutdown|reboot|halt|poweroff)\b"),
     "команда выключит или перезагрузит компьютер."),
    ("chmod-777", re.compile(r"\bchmod\s+(-\w+\s+)*-R\s+(0?777|a\+rwx)\b|\bchmod\s+(0?777|a\+rwx)\s+-R\b"),
     "рекурсивный `chmod 777` открывает файлы всем на запись."),
    ("security-off", re.compile(r"\bcsrutil\s+disable\b|\bspctl\s+--master-disable\b|\bnvram\s+\S+=|\bxattr\s+(-r\s+)?-d\s+com\.apple\.quarantine\b.*\s/Applications"),
     "команда отключает защитные механизмы macOS."),
    ("sudo", re.compile(r"^\s*(sudo|doas)\b|^\s*su(\s+-|\s+root)?\s*$"),
     "команда запускается с правами администратора."),
    ("infra-destroy", re.compile(
        r"\bterraform\s+(destroy\b|apply\b.*-auto-approve)|\bpulumi\s+destroy\b|\bkubectl\b.*\bdelete\b|\bhelm\s+(uninstall|delete)\b"
        r"|\bdocker\s+(system|volume|image|container|network|builder)\s+prune\b|\baws\s+s3\s+(rm|rb)\b.*(--recursive|--force)"
        r"|\baws\s+\S+\s+delete-\S+|\bgcloud\b.*\s(delete|destroy)\b|\baz\b.*\sdelete\b|\bfirebase\b.*:delete\b"
        r"|\bheroku\s+(apps:destroy|pg:reset)\b|\bvercel\s+(rm|remove)\b|\bfly(ctl)?\s+(apps\s+)?destroy\b|\bsupabase\s+db\s+reset\b"),
     "команда удаляет облачные / инфраструктурные ресурсы."),
    ("db-destructive", re.compile(
        r"\bprisma\s+(migrate\s+reset|db\s+push\b.*--force-reset)|\b(rails|rake)\s+db:(drop|reset|purge)\b"
        r"|\bmanage\.py\s+(flush|reset_db)\b|\bredis-cli\b.*\b(flushall|flushdb)\b|\bdropdb\b", I),
     "команда удаляет данные в базе."),
    ("publish", re.compile(
        r"\b(npm|pnpm|yarn|bun)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bpoetry\s+publish\b|\bgem\s+push\b"
        r"|\bpod\s+trunk\s+push\b|\bgh\s+(pr\s+merge|repo\s+(delete|archive)|release\s+delete|secret\s+(set|delete))\b|\bgh\s+api\b.*-X\s*DELETE\b"),
     "команда публикует что-то наружу или меняет удалённый репозиторий."),
    ("secrets-read", re.compile(
        r"\bsecurity\s+(find-(generic|internet)-password\b.*\s-[a-zA-Z]*w|dump-keychain)\b"
        r"|\b(cat|less|more|head|tail|cp|scp|base64|xxd|pbcopy)\b[^|;&]*\.ssh/id_[a-z0-9_]+(?!\.pub)(\s|$|['\"])"),
     "команда читает пароли из Keychain или приватный SSH-ключ."),
    ("simulator-wipe", re.compile(r"\bsimctl\s+(erase|delete)\s+(?!unavailable\b)\S|\bemulator(\.exe)?\b.*\s-wipe-data\b"
                                  r"|\bavdmanager(\.bat)?\s+delete\b", I),
     "команда сотрёт симулятор или эмулятор вместе с установленными приложениями и их данными."),
    ("real-device", re.compile(
        r"\badb(\.exe)?\s+(-d\b|-s\s+(?!emulator-|localhost:|127\.0\.0\.1:)\S+)\s.*\b(install(-multiple)?|uninstall|push|reboot|root|"
        r"remount|sideload|shell\s+.*\b(rm|pm\s+(clear|uninstall)|wipe|settings\s+put|reboot|svc)\b)", I),
     "команда меняет настоящий телефон, подключённый к компьютеру, а не эмулятор."),
    ("kill-all", re.compile(r"\bkill\s+-9\s+-1\b"),
     "`kill -9 -1` завершит все ваши процессы."),
    # Windows (PowerShell / cmd) and Linux
    ("disk-format-win", re.compile(r"\bformat(\.com)?\s+[a-z]:|\bdiskpart\b|\b(clear-disk|format-volume|remove-partition|"
                                   r"initialize-disk)\b|\bcipher(\.exe)?\s+/w\b", I),
     "команда форматирует или стирает диск."),
    ("system-power-win", re.compile(r"\b(stop-computer|restart-computer)\b|\bshutdown(\.exe)?\s+/[srp]\b", I),
     "команда выключит или перезагрузит компьютер."),
    ("security-off-win", re.compile(
        r"\bset-executionpolicy\b(?!.*-scope\s+process\b).*\b(unrestricted|bypass)\b|\bset-mppreference\b.*-disable\w*"
        r"|\bnetsh\s+(advfirewall|firewall)\s+set\b.*\b(state\s+off|disable)\b|\bbcdedit\b"
        r"|\breg(\.exe)?\s+delete\s+(hklm|hkey_local_machine)\b|\bremove-item\b.*\bhklm:", I),
     "команда отключает защиту Windows или меняет системные настройки."),
    ("autostart", re.compile(
        r"\breg(\.exe)?\s+add\s+\S*[\\/]currentversion[\\/]run(once)?\b|\bschtasks(\.exe)?\s+/create\b"
        r"|\bregister-scheduledtask\b|\b(new|set)-itemproperty\b.*[\\/]currentversion[\\/]run|\bcrontab\s+(?!-l\b)\S", I),
     "команда добавляет программу в автозапуск или расписание (cron, Task Scheduler, реестр)."),
    ("run-as-admin", re.compile(r"\bstart-process\b.*-verb\s+runas\b|\brunas(\.exe)?\s+/user", I),
     "команда запускается с правами администратора."),
    ("agent-tools", re.compile(r"\bsettings\.py\b.*\bmcp\s+add\b"),
     "подключение MCP-сервера: у агента появятся новые инструменты, а программа сервера будет запускаться на вашем "
     "компьютере."),
    ("protection-off", re.compile(r"\b(settings|memory)\.py\b.*\b(plugin|config)\b.*\bprotect-secrets\s+off\b"),
     "выключение защиты секретов: агент сможет выводить содержимое .env и ключей в разговор."),
    ("claude-permissions", re.compile(
        r"\b(claude_task\.py|claude(\.exe|\.cmd)?)\b.*(--dangerously-skip-permissions|"
        r"--permission-mode[=\s]+bypassPermissions|--allow(?:edTools)?[=\s]+[\"']?(?:Bash(?:\(\s*\*\s*\))?[\"']?(?:\s|$)|"
        r"[^\s\"']*\b(?:sudo|rm|ssh|scp|rsync|curl|wget|git\s+push)\b))", I),
     "запуск Claude Code с обходом ограничений прав (bypassPermissions / широкий Bash)."),
]
# The usage panel's routes that change something (sidecars/usage/server.mjs): a plugin setting, a running LSP daemon.
# Only these two, so a project's own `localhost:3000/api/settings` is not taken for the panel.
SIDECAR_API = re.compile(r"(?:localhost\.?|0\.0\.0\.0|127(?:\.\d+){1,3}|\[::1?\]|::1):\d+/+api/+(?:toggle|lsp/+stop)\b", I)
SIDECAR_API_REASON = "команда обращается к управляющему API боковой панели Antigravity Plus (настройки или LSP)."
SCHEDULE_REASON = "команда создаёт или удаляет расписание Antigravity: по нему агент будет сам запускаться без вас."


def schedule_change(seg: str) -> bool:
    """`[python3 …] …/schedule_task.py create|remove …` (/schedule-task): it writes an Antigravity automation."""
    tokens = strip_prefixes(tokenize(seg))
    for i, t in enumerate(tokens[:6]):
        if os.path.basename(t.replace("\\", "/")).lower() == "schedule_task.py":
            sub = next((a for a in tokens[i + 1:] if not a.startswith("-")), "")
            return sub in ("create", "remove")
    return False


def _sidecar_api_hit(seg: str) -> bool:
    if SIDECAR_API.search(seg):
        return True
    unquoted = seg.replace('"', "").replace("'", "")
    if SIDECAR_API.search(unquoted):
        return True
    if ":" in seg:
        collapsed = re.sub(r"['\"\s]*\+['\"\s]*", "", unquoted)
        if SIDECAR_API.search(collapsed):
            return True
    return False

# SQL is checked only when a DB client is the program being run (so `rg "DROP TABLE"` stays quiet).
SQL_PROGRAMS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse", "clickhouse-client", "cockroach",
                "mongo", "mongosh", "duckdb", "bq", "turso", "wrangler", "prisma", "supabase"}
SQL_DESTRUCTIVE = re.compile(r"\b(drop\s+(table|database|schema|view|index)|truncate\s+(table\s+)?[\w.\"`]+)|\bdelete\s+from\s+[\w.\"`]+\s*(;|$|\"|')|\.dropDatabase\(|\bdb\.\w+\.drop\(", I)

# Rules for the whole command line (they span pipes). `curl … | sh` itself is pipe_runs_download(): only a program
# that runs what it reads (`sh`, `bash -s`, `python3 -`) counts — `| python3 -c '…'` or `| jq` just reads data.
CURL_REASON = "скрипт из интернета выполняется без проверки (`curl | sh`)."
PIPE_RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("curl-pipe-shell", re.compile(r"\b(ba|z)?sh\s+(-\w+\s+)*[\"']?\$\((curl|wget)\b"
                                   r"|(\bsource|(^|[;&|]\s*)\.|\b(ba|z|da|k)?sh|\bpython3?|\bnode|\bruby|\bperl)\s+<\(\s*(curl|wget)\b"),
     CURL_REASON),
    ("fork-bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "fork-бомба положит систему."),
    ("download-run", re.compile(  # PowerShell's `curl | sh`
        r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^;&]*\|\s*(iex|invoke-expression)\b"
        r"|\b(iex|invoke-expression)\b[\s(]*\(?\s*(new-object\s+(system\.)?net\.webclient|iwr|irm|invoke-webrequest|"
        r"invoke-restmethod)\b", re.I),
     "команда скачивает код из интернета и сразу его выполняет."),
]

# The stops that hold in Bypass Mode too: what can break the computer or hand the agent more power than the user gave —
# admin rights, system folders and OS settings, disks, power, autostart, the Keychain and private SSH keys, wiping
# the home folder or a disk, the agent's own permissions. Everything else (git reset --hard, force-push, curl | sh,
# DROP TABLE, cloud deletions, publishing, rm -rf of the current folder…) Bypass Mode lets through, and logs.
SYSTEM_RULES = {"disk-format", "disk-format-win", "system-power", "system-power-win", "security-off", "security-off-win",
                "sudo", "run-as-admin", "autostart", "kill-all", "fork-bomb", "secrets-read", "agent-tools",
                "protection-off", "claude-permissions", "rm-dangerous", "system-files", "protected-file",
                "agent-settings", "scheduled-sidecar"}
# The stops that protect only this computer — its system folders and startup files, admin rights, OS settings, the
# agent's own permissions. Text that runs on another machine or inside a container (runs_elsewhere: `ssh host "sudo
# mkdir -p /root/backup"`, `docker exec app sh -c '…'`, the unquoted words of `ssh host crontab -e` or `kubectl exec
# pod -- rm -rf /`, a heredoc fed to `./deploy/ssh.sh 'bash -s'`) is checked without them; wiping / or ~, disks, power,
# kill -9 -1, fork bombs, reading private keys and every project-level rule still apply to it. Of check_writes' stops
# only the recursive chmod/chown of / or a top-level folder stays there.
LOCAL_RULES = {"system-files", "protected-file", "sudo", "run-as-admin", "autostart", "security-off",
               "security-off-win", "agent-settings", "agent-tools", "protection-off", "claude-permissions",
               "scheduled-sidecar"}

# Programs whose arguments are just text (a commit message or a search pattern can mention `rm -rf /`).
TEXT_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "git-grep", ":", "true", "test", "["}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh", "eval", "xargs", "su", "sudo", "doas",
          "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "iex", "invoke-expression"}
# When the command hands text to another interpreter, quoted text is code, not data (`env -S '…'` splits its string
# into a command line of its own).
INTERPRETER = re.compile(r"\b((ba|z|da|k)?sh|fish)\s+(-\w+\s+)*-c\b|\beval\b|\bxargs\b|\bssh\b|\bwatch\b|\bdocker\s+(exec|run)\b|\bkubectl\s+exec\b"
                         r"|\b(powershell|pwsh)(\.exe)?\s+(-\w+\s+)*-c(ommand)?\b|\bcmd(\.exe)?\s+/[ck]\b|\b(iex|invoke-expression)\b"
                         r"|\benv(\s+(-\w+|\w+=\S*))*\s+(-[iv0]*S|--split-string)", I)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
HEREDOC = re.compile(r"<<-?\s*\\?(['\"]?)([A-Za-z_][\w-]*)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)  # <<EOF <<'EOF' <<\EOF

DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "..", "../", "../*",
                        "$HOME", "${HOME}", "$HOME/", "${HOME}/", "$HOME/*", "${HOME}/*", "/Users", "/home"}
FOLDER_RM_TARGETS = {".", "./", "./*", "*", ".*"}  # the current folder — the project, not the system
TOP_LEVEL = re.compile(r"^/(Applications|Library|System|Users|Volumes|bin|cores|dev|etc|home|opt|private|sbin|tmp|usr|var"
                       r"|usr/local|opt/homebrew)/?\*?$")
VAR_ROOT = re.compile(r"^\$\{?\w+\}?/")  # "$DIR/…" — deletes from / when the variable is empty
VAR_REF = re.compile(r"^\$\{?([A-Za-z_]\w*)\}?(?=/|$)")

GIT_INTERNALS = re.compile(r"(^|/)\.git/(?!COMMIT_EDITMSG$)")
PROTECTED_FILE = re.compile(
    r"(^|/)\.(zshrc|zprofile|zshenv|bashrc|bash_profile|profile)$"
    r"|/\.ssh/(?!(config|known_hosts)$)|/Library/LaunchAgents/|/Library/LaunchDaemons/|^/etc/|^/private/etc/"
    r"|/\.config/(autostart|systemd/user)/|(^|/)(Microsoft\.PowerShell_|Microsoft\.VSCode_)?profile\.ps1$"
    r"|/Start Menu/Programs/Startup/|^[A-Za-z]:/Windows/", re.I)
# What decides what the agent may do: Antigravity's settings and projects (permission grants), MCP servers,
# installed plugins (this guard among them), Antigravity Plus's settings (full access, Bypass Mode, secret protection)
# and the script whose «ask|tell» the guard approves for Claude Code's delegated agents.
AGENT_CONTROL = re.compile(r"/\.gemini/config/(config\.json|mcp_config\.json|projects/|plugins/)"
                           r"|/\.gemini/antigravity/mcp_config\.json$|/\.gemini/antigravity-plus/settings\.json$"
                           r"|/\.claude/skills/antigravity/")
AGENT_CONTROL_DIR = re.compile(r"/\.gemini(/config(/(projects|plugins)(/antigravity-plus)?)?|/antigravity-plus)?/?$"
                               r"|/\.claude/skills/antigravity/?$")  # a whole folder of them (rm -rf, mv, chmod -R)
# Antigravity's own sidecars (`<configDir>/sidecars/<id>/sidecar.json`; jetski = an older path in its docs): a command it
# starts by itself, or a `"builtin": "schedule"` automation that starts agent conversations on a cron schedule.
SCHEDULED_SIDECAR = re.compile(r"/\.gemini/(?:config|jetski)/sidecars(?:/|$)", I)
# Folders of the operating system itself. Not the user's ~/Library, not /usr/local or /opt (Homebrew), not the temp
# folders (/tmp, /var/folders, /var/tmp) or /dev/null.
SYSTEM_DIR = re.compile(
    r"^/(System|bin|sbin|etc|Library|boot|proc|sys|lib|lib32|lib64|libx32|root|cores|usr(?!/local(/|$))|private/etc"
    r"|var(?!/(folders|tmp)(/|$))|private/var(?!/(folders|tmp)(/|$))"
    r"|dev(?!/(null|zero|random|urandom|stdin|stdout|stderr|tty)$|/fd/))(/|$)"
    r"|^[a-z]:/(windows|program files|program files \(x86\)|programdata|recovery|system volume information)(/|$)", re.I)
WIN_ENV = [(re.compile(r"^(\$env:(windir|systemroot)|%(windir|systemroot)%)", re.I), "C:/Windows"),
           (re.compile(r"^(\$\{env:programfiles\(x86\)\}|%programfiles\(x86\)%)", re.I), "C:/Program Files (x86)"),
           (re.compile(r"^(\$env:programfiles|%programfiles%)", re.I), "C:/Program Files"),
           (re.compile(r"^(\$env:programdata|%programdata%)", re.I), "C:/ProgramData")]
HOME_REF = re.compile(r"^(~|\$\{?HOME\}?|\$env:userprofile|%userprofile%|\$home)(?=/|$)", re.I)
HOME = compat.slashes(os.path.expanduser("~"))
# Programs and the arguments they change: every path (rm, mv, chmod, tee…), only the destination (cp, ln, install,
# rsync), or the files they edit in place (sed -i, perl -i).
CHANGES_ALL = {"rm", "grm", "rmdir", "unlink", "srm", "shred", "truncate", "touch", "mkdir", "chmod", "chown", "chgrp",
               "chflags", "chattr", "setfacl", "tee", "mv", "gmv", "remove-item", "ri", "del", "erase", "rd",
               "move-item", "mi", "move", "new-item", "ni", "set-content", "add-content", "ac", "out-file",
               "clear-content", "clc", "set-acl", "icacls", "icacls.exe", "takeown", "takeown.exe", "attrib",
               "attrib.exe", "mklink"}
CHANGES_DEST = {"cp", "gcp", "install", "ginstall", "ln", "gln", "rsync", "ditto", "copy-item", "cpi", "copy", "xcopy",
                "xcopy.exe", "robocopy", "robocopy.exe"}
RECURSIVE_OWNERS = {"chmod", "chown", "chgrp", "chflags", "setfacl"}  # -R on / or ~ changes the whole system or home
REDIRECT_TARGET = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|(?<![<>&\d])(?:\d|&)?>{1,2}\|?\s*('[^']*'|"(?:\\.|[^"\\])*"|[^\s;|&<>()'"]+)""")
# An Antigravity conversation started or messaged from the terminal with words that switch Bypass Mode or full access
# on: only the user's own message may do that (hooks/conversation.py).
SELF_SWITCH = re.compile(r"\bagentapi\b[^\n]*?\b(new-conversation|send-message)\b.*?(/bypass\b|\bbypass\b|байпас"
                         r"|полн\w*\s+доступ|full[\s-]*access)", re.I | re.S)
SELF_SWITCH_REASON = ("команда пишет в разговор Antigravity сообщение, которое включает Bypass Mode или полный доступ: "
                      "их включает только сам пользователь своим сообщением.")


def _opener(s: str, i: int) -> Tuple[str, str]:
    """(opening, what closes it) when a quoted string or a command substitution starts at s[i] — `'`, `"`, `$'` (an
    ANSI-C string, where a backslash escapes even the quote), `$(` `<(` `>(` and a backtick — else ("", ""). In
    PowerShell (Windows) a backtick escapes and `$'` is no string."""
    c, posix = s[i], not compat.WINDOWS
    if c in "'\"":
        return c, c
    if posix and s.startswith("$'", i):
        return "$'", "$'"
    if s.startswith(("$(", "<(", ">("), i):
        return s[i:i + 2], ")"
    if posix and c == "`":
        return c, c
    return "", ""


def _skip(s: str, i: int, close: str) -> int:
    """s[i:] lies inside a string or substitution that `close` ends (see _opener; ")" also for the parentheses nested
    in a substitution): the index just past its end, -1 when it never ends. Quotes, $(…) and backticks nest inside it
    as the shell reads them; in a `#` comment inside $(…) quotes count for nothing."""
    n, esc = len(s), ("`" if compat.WINDOWS else "\\")
    stack = [close]
    while i < n:
        top, c = stack[-1], s[i]
        if top == "'":  # nothing is special inside '…' but its end
            if c == "'":
                stack.pop()
            i += 1
        elif top == "$'":
            if c == "'":
                stack.pop()
            i += 2 if c == "\\" else 1
        elif c == esc:
            i += 2
        elif c == top:
            stack.pop()
            i += 1
        elif top == "`":
            i += 1
        elif c == "$" and s.startswith("(", i + 1):
            stack.append(")")
            i += 2
        elif c == "`" and esc == "\\":
            stack.append("`")
            i += 1
        elif top == '"':
            i += 1
        elif c == "#" and (i == 0 or s[i - 1] in " \t\n;&|("):  # its quotes count for nothing; `)` still ends $(…) (zsh)
            while i < n and s[i] not in "\n)":
                i += 1
        elif c in "'\"":
            stack.append(c)
            i += 1
        elif c == "$" and s.startswith("'", i + 1) and esc == "\\":
            stack.append("$'")
            i += 2
        elif c == "(" or c in "<>" and s.startswith("(", i + 1):
            stack.append(")")
            i += 1 if c == "(" else 2
        else:
            i += 1
        if not stack:
            return i
    return -1


def _split(cmd: str) -> List[Tuple[str, str]]:
    """The command's simple commands, each with the operator after it ("" after the last): split at ; & && || | |&
    and newlines — not inside quotes, $(…), <(…), >(…) or backticks (`sh -c 'cd / && rm -rf *'` and `x=$(a; b)` stay
    whole), nor at the & or | of a redirection (2>&1, &>, >|). A `#` that starts a word comments out the rest of the
    line in bash but not in an interactive zsh, so quotes in it count for nothing while separators still split; a
    quote that never closes is just a character: nothing after either can hide inside a string."""
    out: List[Tuple[str, str]] = []
    esc = "`" if compat.WINDOWS else "\\"
    start, i, n, comment, broken = 0, 0, len(cmd), False, False
    while i < n:
        c = cmd[i]
        if c == "\n":
            comment = False
        elif comment or c in ";|&":
            pass
        elif c == esc:
            i += 2
            continue
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            comment = True
        else:  # a string that never ends is a syntax error the shell runs nothing after — or a heredoc's apostrophe:
            opening, close = _opener(cmd, i) if not broken else ("", "")  # from there on, split as plain text
            end = _skip(cmd, i + len(opening), close) if opening else -1
            broken = broken or bool(opening) and end < 0
            i = i + 1 if end < 0 else end
            continue
        op = ""
        if c in ";\n":
            op = c
        elif c == "|" and not (i and cmd[i - 1] == ">"):
            op = cmd[i:i + 2] if cmd[i:i + 2] in ("||", "|&") else c
        elif c == "&" and not (i and cmd[i - 1] in "<>") and not cmd.startswith("&>", i):
            op = "&&" if cmd.startswith("&&", i) else c
        if op:
            out.append((cmd[start:i], op))
            i += len(op)
            start = i
        else:
            i += 1
    out.append((cmd[start:], ""))
    return out


def split_unquoted(cmd: str) -> List[str]:
    """The command's simple commands (_split): `ssh host 'a && b'` and `x=$(a; b)` stay one segment each."""
    return [s.strip() for s, _op in _split(cmd) if s.strip()]


def _bare(seg: str) -> str:
    """The segment as the rules read it: quoted strings emptied to '' and $(…) / backtick bodies to $() / `` — what
    the command itself says, not the data it carries or the commands it runs inside (those are analysed on their
    own, split at their own separators). A comment stays as it is."""
    out: List[str] = []
    esc = "`" if compat.WINDOWS else "\\"
    i, n, broken = 0, len(seg), False
    while i < n:
        c = seg[i]
        if c == esc:
            out.append(seg[i:i + 2])
            i += 2
            continue
        if c == "#" and (i == 0 or seg[i - 1] in " \t\n;&|()"):
            out.append(seg[i:])
            break
        opening, close = _opener(seg, i) if not broken else ("", "")
        end = _skip(seg, i + len(opening), close) if opening else -1
        broken = broken or bool(opening) and end < 0
        if end < 0:  # no string here, or one that never ends (_split): the character as it is
            out.append(c)
            i += 1
            continue
        out.append("''" if close in ("'", '"', "$'") else opening + close)
        i = end
    return "".join(out)


def tokenize(segment: str) -> List[str]:
    if compat.WINDOWS:  # a backslash is a path separator there (PowerShell escapes with `), not an escape
        segment = segment.replace("\\", "/")
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def program(segment: str) -> str:
    tokens = strip_prefixes(tokenize(segment))
    return os.path.basename(tokens[0]) if tokens else ""


def _heredocs(cmd: str) -> Tuple[str, List[Tuple[str, str, str]]]:
    """The command without its heredoc bodies, and per heredoc (its line up to the `<<`, the rest of that line, body)."""
    docs: List[Tuple[str, str, str]] = []

    def repl(m: "re.Match[str]") -> str:
        start = cmd.rfind("\n", 0, m.start()) + 1
        docs.append((cmd[start:m.start()], m.group(3), m.group(4)))
        return "<<" + m.group(2) + m.group(3) + "\n"

    return HEREDOC.sub(repl, cmd), docs


def split_heredocs(cmd: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Remove heredoc bodies from the command; return (command, [(owner line, body), …])."""
    cmd, docs = _heredocs(cmd)
    return cmd, [(before + rest, body) for before, rest, body in docs]


def heredoc_owner(before: str, rest: str) -> str:
    """The simple command a heredoc is fed to: the words around its `<<` up to the separators on either side
    (`cd /srv/app && ./deploy/ssh.sh 'bash -s' <<'EOF' | tee log` → `./deploy/ssh.sh 'bash -s'`)."""
    return (_split(before)[-1][0] + " " + _split(rest)[0][0]).strip()


# A delegated agent talking to Claude Code (claude-code/skills/antigravity/scripts/delegate.py ask|tell, the commands
# the brief gives it): they only print a line for Claude Code, so they never need the user's approval.
CLAUDE_TALK = os.path.join(os.path.expanduser("~"), ".claude", "skills", "antigravity", "scripts", "delegate.py")
PYTHON_EXE = re.compile(r"^(?:.*/)?(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?$", re.I)
PYTHON_FLAGS = {"-3", "-I", "-u", "-B", "-E", "-s", "-X", "utf8"}


def talks_to_claude(cmd: str) -> bool:
    """`python3 "~/.claude/skills/antigravity/scripts/delegate.py" ask|tell "<text>"` and nothing else: only that
    installed script (not a delegate.py from the project), no second command, redirection or substitution."""
    text = (cmd or "").strip()
    if not text or "$(" in text or "${" in text or "`" in text:
        return False
    if text.startswith("& "):  # PowerShell runs a quoted program with &
        text = text[2:]
    if compat.WINDOWS:  # a backslash is a path separator there, not an escape
        text = text.replace("\\", "/")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""  # «#» is no comment in every shell
        tokens = list(lex)
    except ValueError:
        return False
    if len(tokens) < 3 or not PYTHON_EXE.match(tokens[0]) or any(t and not t.strip("();<>|&\n") for t in tokens):
        return False
    rest = tokens[1:]
    while rest and rest[0] in PYTHON_FLAGS:
        rest = rest[1:]
    if len(rest) < 2 or rest[1] not in ("ask", "tell"):
        return False
    norm = lambda p: os.path.normcase(os.path.realpath(os.path.expanduser(p)))  # noqa: E731
    return norm(rest[0]) == norm(CLAUDE_TALK)


ENV_VAL_FLAGS = {"-u", "--unset", "-C", "--chdir"}
ENV_SPLIT = re.compile(r"^(?:-[iv0]*S|--split-string(?==|$))=?(.*)$", re.S)  # env -S 'cmd args', -S'…', --split-string=…


def strip_prefixes(tokens: List[str]) -> List[str]:
    while tokens:
        t = tokens[0]
        if re.match(r"^\w+=", t) or t in ("builtin", "noglob"):
            tokens = tokens[1:]
        elif t == "env":
            tokens = tokens[1:]
            while tokens:
                split = ENV_SPLIT.match(tokens[0])
                if split:  # the string is a command line of its own: env splits it into the words it runs
                    line, rest = (split.group(1), tokens[1:]) if split.group(1) else (" ".join(tokens[1:2]), tokens[2:])
                    try:
                        tokens = shlex.split(line, posix=True) + rest
                    except ValueError:
                        tokens = line.split() + rest
                elif tokens[0] in ENV_VAL_FLAGS and len(tokens) > 1:
                    tokens = tokens[2:]
                elif tokens[0] == "--":
                    tokens = tokens[1:]
                    break
                elif tokens[0].startswith("-") or re.match(r"^\w+=", tokens[0]):
                    tokens = tokens[1:]
                else:
                    break
        elif t == "command":
            tokens = tokens[1:]
            if tokens and tokens[0] in ("-v", "-V"):
                return tokens  # `command -v rm` looks up rm without executing it
            while tokens and tokens[0] in ("-p", "--"):
                if tokens[0] == "--":
                    tokens = tokens[1:]
                    break
                tokens = tokens[1:]
        elif t in ("sudo", "doas", "exec", "time", "nohup"):
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                if t == "exec" and tokens[0] == "-a" and len(tokens) > 1:
                    tokens = tokens[2:]
                    continue
                if tokens[0] == "--":
                    tokens = tokens[1:]
                    break
                tokens = tokens[1:]
        else:
            break
    return tokens


def note_assignments(segment: str, known: Dict[str, str]) -> None:
    """A segment that only sets variables (`S=/path`, `export APP="…"`): remember the ones that can't be empty, so
    `S=/path && rm -rf $S/x` isn't taken for `rm -rf /x`. A value from `$(…)` or another variable is unknown."""
    tokens = tokenize(segment)
    if tokens[:1] == ["cd"]:  # where relative paths of the next segments point (`cd / && rm -rf *`)
        dest = HOME if len(tokens) == 1 else ""
        if len(tokens) == 2 and not tokens[1].startswith("-") and "`" not in tokens[1] and "$(" not in tokens[1]:
            dest = expand_path(tokens[1], known)
        if dest:
            known["__cwd__"] = dest
        else:  # `cd -`, `cd -P …`: the folder is unknown from here on
            known.pop("__cwd__", None)
        return
    if tokens and tokens[0] in ("export", "local", "declare", "typeset", "readonly"):
        tokens = [t for t in tokens[1:] if not t.startswith("-")]
    pairs = [re.match(r"^([A-Za-z_]\w*)=(.*)$", t, re.S) for t in tokens]
    if not pairs or not all(pairs):
        return
    for m in pairs:
        name, value = m.group(1), m.group(2)
        if value and "`" not in value and ("$" not in value or re.match(r"^\$\{?HOME\}?(/|$)", value)):
            known[name] = value
        else:
            known.pop(name, None)


def with_known(token: str, known: Optional[Dict[str, str]]) -> str:
    """`$S/x` → `/path/x` when S was set earlier in the same command (note_assignments)."""
    m = VAR_REF.match(token)
    if m and known and m.group(1) in known:
        return re.sub(r"/{2,}", "/", known[m.group(1)] + token[m.end():])
    return token


def check_rm(segment: str, known: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """`rm -r` on / or a top-level folder, the home folder, the parent folder or `$VAR/…` with VAR unknown
    ("rm-dangerous"), or on everything in the current folder ("rm-folder": inside the project)."""
    tokens = strip_prefixes(tokenize(segment))
    if not tokens or os.path.basename(tokens[0]) not in ("rm", "grm"):
        return None
    flags, targets, end_of_opts = "", [], False
    for t in tokens[1:]:
        if not end_of_opts and t == "--":
            end_of_opts = True
        elif not end_of_opts and t.startswith("--"):
            flags += {"--recursive": "r", "--force": "f", "--no-preserve-root": "!"}.get(t, "")
        elif not end_of_opts and t.startswith("-") and len(t) > 1:
            flags += t[1:]
        else:
            targets.append(t)
    if "!" in flags:
        return "rm-dangerous", "`rm --no-preserve-root` может удалить всю систему."
    if not re.search(r"[rR]", flags):
        return None
    return wipe_verdict("rm -r {t}", targets, known)


def rm_scope(t: str, known: Optional[Dict[str, str]]) -> Tuple[str, str]:
    """What `rm -r <t>` wipes: "system" (/, the home folder, a top-level folder, above the current folder —
    rm_reaches_up), "var" (`$DIR/…` with DIR unknown: from / when it is empty), "folder" (everything in the current
    folder) or "" — with t as the command means it (variables set earlier in the command filled in)."""
    t = with_known(t, known)  # shlex keeps ~ and $VAR literal, which is what we want to inspect
    if t in DANGEROUS_RM_TARGETS or t.rstrip("/") in DANGEROUS_RM_TARGETS or TOP_LEVEL.match(t) or rm_reaches_up(t, known):
        return "system", t
    if t in FOLDER_RM_TARGETS or t.rstrip("/") in FOLDER_RM_TARGETS:
        return "folder", t
    if VAR_ROOT.match(t) and not re.match(r"^\$\{?HOME\}?/", t):
        return "var", t
    return "", t


def wipe_verdict(form: str, targets: List[str], known: Optional[Dict[str, str]]) -> Optional[Tuple[str, str]]:
    """The stop for wiping these targets the way `form` says (`rm -r {t}`, `find {t} -delete`): "rm-dangerous" for the
    system, the home folder or an unknown variable's path, else "rm-folder" for the current folder (the project)."""
    folder = None
    for t in targets:
        kind, t = rm_scope(t, known)
        what = form.replace("{t}", t)
        if kind == "system":
            return "rm-dangerous", f"`{what}` удалит всё содержимое `{t}` без корзины."
        if kind == "var":
            return "rm-dangerous", f"`{what}`: если переменная пустая, удаление пойдёт от корня `/`."
        if kind == "folder":
            folder = folder or ("rm-folder", f"`{what}` удалит всё содержимое текущей папки без корзины.")
    return folder


FIND_OPTIONS = {"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"}  # before the starting points (GNU, BSD)
# Tests that pick files by name (-empty: nothing in what it picks to lose); a name of only `*` picks everything.
FIND_NARROW = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname",
               "-ilname", "-inum", "-samefile"}
FIND_VALUE = FIND_NARROW | {"-type", "-xtype", "-maxdepth", "-mindepth", "-mtime", "-mmin", "-atime", "-amin", "-ctime",
                            "-cmin", "-Btime", "-Bmin", "-newer", "-anewer", "-cnewer", "-Bnewer", "-size", "-perm",
                            "-user", "-group", "-uid", "-gid", "-links", "-fstype", "-used", "-context", "-printf",
                            "-fprint", "-fprint0", "-fls", "-regextype", "-files0-from"}
FIND_REMOVERS = {"rm", "grm", "unlink", "shred", "srm"}


def find_deletion(tokens: List[str]) -> Optional[Tuple[List[str], str, bool]]:
    """A `find` that deletes (`-delete`, `-exec rm … {}`): (its starting points, the action, whether the action runs
    for everything it walks — no name test before it in its branch: `find ~ -type f -delete`, not `find . -name
    '*.pyc' -delete` or `find . \\( -name a -o -name b \\) -delete`); None for any other command."""
    if not tokens or os.path.basename(tokens[0]) not in ("find", "gfind"):
        return None
    args, roots, i = tokens[1:], [], 0
    while i < len(args):  # options before the starting points
        if args[i] in FIND_OPTIONS or re.match(r"^-O\d$", args[i]):
            i += 1
        elif args[i] in ("-D", "-f") and i + 1 < len(args):  # GNU -D <debug opts>; BSD -f <starting point>
            roots += args[i + 1:i + 2] if args[i] == "-f" else []
            i += 2
        else:
            i += args[i] == "--"
            break
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", ")", "!", ","):
        roots.append(args[i])
        i += 1
    expr = args[i:]
    # An action runs for what the AND-chain before it lets through: in its own -o branch, inside every group around
    # it. Per open group: has the chain outside it named files ("outer"), has the chain so far ("chain"), has this
    # branch itself ("own"), had every earlier branch ("all"), is the group negated ("neg").
    groups: List[Dict[str, bool]] = [{"outer": False, "chain": False, "own": False, "all": True, "neg": False}]
    action, everything, neg, i = "", False, False, 0
    while i < len(expr):
        t, g = expr[i], groups[-1]
        i += 1
        if t in ("!", "-not"):
            neg = not neg
            continue
        if t == "(":
            groups.append({"outer": g["chain"], "chain": g["chain"], "own": False, "all": True, "neg": neg})
        elif t == ")" and len(groups) > 1:
            groups.pop()
            if g["own"] and g["all"] and not g["neg"]:  # every branch picks files by name: so does the group
                groups[-1]["chain"] = groups[-1]["own"] = True
        elif t in ("-o", "-or", ","):
            g["all"], g["chain"], g["own"] = g["all"] and g["own"], g["outer"], False
        elif t in FIND_NARROW or t == "-empty":
            value = expr[i] if t != "-empty" and i < len(expr) else "x"
            i += t != "-empty"
            if not neg and not re.fullmatch(r"\**" if "regex" not in t else r"\^?(\.\*)*\$?", value):
                g["chain"] = g["own"] = True
        elif t in ("-delete", "-exec", "-execdir", "-ok", "-okdir"):
            if t != "-delete":
                end = i
                while end < len(expr) and expr[end] != ";" and not (expr[end] == "+" and expr[end - 1] == "{}"):
                    end += 1
                runs = strip_prefixes(expr[i:end])  # -exec sudo rm …
                runs, i = os.path.basename(runs[0]) if runs else "", end + 1
                if runs not in FIND_REMOVERS:
                    neg = False
                    continue
                t += " " + runs
            action = action or t
            everything = everything or not g["chain"]
        elif t in FIND_VALUE or re.match(r"^-newer[a-zA-Z]{2}$", t):
            i += 1
        elif t == "-fprintf":
            i += 2
        neg = False
    return (roots or ["."], action, everything) if action else None


def check_find(segment: str, known: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """`find <root> -delete` / `-exec rm … {}` that deletes everything it walks: the same stop as `rm -r <root>`
    (check_rm) — / or the home folder, an unknown variable's path, the current folder."""
    found = find_deletion(strip_prefixes(tokenize(segment))) if "find" in segment else None
    if not found or not found[2]:
        return None
    return wipe_verdict("find {t} " + found[1], found[0], known)


UP_ONLY = re.compile(r"^\.\.(/\.\.)*(/\*?)?$")  # `../..`, `../../*`: above the parent folder too


def rm_reaches_up(t: str, known: Optional[Dict[str, str]]) -> bool:
    """`rm -r` target that is / , the home folder or a top-level folder once `..` is resolved and an earlier `cd`
    applied (`/usr/../`, `$HOME/x/..`, `cd / && rm -rf *`), or a relative path that climbs above the parent folder."""
    p = expand_path(t, known)
    if not p:
        return False
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        bare = p[:-1] if p.endswith("/*") else p
        bare = bare.rstrip("/") or "/"
        same = (lambda a, b: a.lower() == b.lower()) if compat.WINDOWS else (lambda a, b: a == b)  # noqa: E731
        return bare == "/" or bool(re.match(r"^[A-Za-z]:$", bare)) or same(bare, HOME.rstrip("/")) \
            or bool(TOP_LEVEL.match(bare))
    return bool(UP_ONLY.match(posixpath.normpath(p)))


WIN_RM = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "rmdir.exe"}
WIN_BROAD = re.compile(r"^([a-z]:)?[\\/]?\*?$|^[a-z]:[\\/](users|windows|program files( \(x86\))?)[\\/]?\*?$"
                       r"|^(~|\$home|\$env:(userprofile|homepath|systemdrive|systemroot)|%userprofile%|%systemdrive%)[\\/]?\*?$"
                       r"|^\.{1,2}[\\/]?\*?$", re.I)
WIN_FOLDER = re.compile(r"^(\.[\\/]?\*?|\*)$")  # the current folder


def check_win_rm(segment: str) -> Optional[Tuple[str, str]]:
    """PowerShell `Remove-Item -Recurse -Force …` / `rm -r -fo …` and cmd `rd /s /q …` / `del /s /q …` on a whole
    drive, the user folder or the parent folder ("rm-dangerous"), or on the current folder ("rm-folder")."""
    tokens = tokenize(segment)
    if not tokens or os.path.basename(tokens[0]).lower() not in WIN_RM:
        return None
    flags = [t.lower() for t in tokens[1:] if t.startswith(("-", "/")) and not WIN_BROAD.match(t)]
    recursive = any(f in ("-r", "-recurse", "/s") or f.startswith("-rec") for f in flags)
    if not recursive:
        return None
    folder = None
    for t in tokens[1:]:
        if t.lower() in ("-path", "-literalpath"):
            continue
        bare = t.strip("'\"")
        if (not t.startswith(("-", "/")) or WIN_BROAD.match(t)) and WIN_BROAD.match(bare):
            if WIN_FOLDER.match(bare):
                folder = folder or ("rm-folder", f"`{tokens[0]} {t}` рекурсивно удалит всё содержимое текущей папки без корзины.")
            else:
                return "rm-dangerous", f"`{tokens[0]} {t}` рекурсивно удалит всё содержимое `{t}` без корзины."
    return folder


def _norm_guard_path(p: str) -> str:
    if not p:
        return ""
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        if p.endswith("/*"):
            base = posixpath.normpath(p[:-2] or "/")
            return "/*" if base == "/" else base + "/*"
        had_slash = len(p) > 1 and p.endswith("/")
        norm = posixpath.normpath(p)
        return norm + ("/" if had_slash and not norm.endswith("/") else "")
    return p


def expand_path(p: str, known: Optional[Dict[str, str]] = None) -> str:
    """A path as a command writes it → absolute with forward slashes where it can be told: ~, $HOME, %USERPROFILE%,
    %WINDIR%, $env:ProgramFiles and variables set earlier in the command; anything else stays as it is."""
    p = with_known(p.strip().strip("'\"").replace("\\", "/"), known).strip("'\"")
    for rx, repl in WIN_ENV:
        p = rx.sub(repl, p, count=1)
    p = HOME_REF.sub(lambda _m: HOME, p, count=1)
    p = re.sub(r"/{2,}", "/", p)
    if known and "__cwd__" in known and p and not (p.startswith("/") or re.match(r"^[A-Za-z]:/", p) or p.startswith("$")):
        cwd = known["__cwd__"].rstrip("/")
        p = f"{cwd}/{p}" if cwd else f"/{p}"
    return _norm_guard_path(p)


def changed_paths(segment: str, known: Optional[Dict[str, str]] = None) -> Tuple[List[str], List[str]]:
    """(paths a segment changes, paths it changes recursively with chmod/chown -R): the arguments of rm/mv/chmod/
    touch/tee…, the destination of cp/ln/install/rsync, files edited in place (sed -i, perl -i), dd of=…, and the
    targets of > / >> outside quotes."""
    paths = [m.group(1) for m in REDIRECT_TARGET.finditer(segment) if m.group(1)]
    deep: List[str] = []
    tokens = strip_prefixes(tokenize(segment))
    if tokens:
        prog, args = os.path.basename(tokens[0]).lower(), tokens[1:]
        pos = [a for a in args if a and not a.startswith("-")]
        if prog in CHANGES_ALL:
            paths += pos
            if prog in RECURSIVE_OWNERS and any(re.match(r"^-[a-zA-Z]*R", a) for a in args):
                deep = pos
        elif prog in CHANGES_DEST:
            low = [a.lower() for a in args]
            dest = next((args[i + 1] for i, a in enumerate(low[:-1]) if a in ("-t", "--target-directory", "-destination")),
                        next((a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")), None))
            if dest is None and len(pos) >= 2:
                dest = pos[-1]
            paths += [dest] if dest else []
        elif prog in ("sed", "gsed", "perl") and any(
                a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[a-zA-Z]*i", a) for a in args):
            paths += pos
        elif prog == "dd":
            paths += [a[3:] for a in args if a.startswith("of=")]
        else:  # find … -delete: each starting point when it deletes all it walks (like rm -r), else what's inside it
            found = find_deletion(tokens)
            paths += ([r if found[2] else r.rstrip("/") + "/*" for r in found[0]]) if found else []
    return ([expand_path(p, known) for p in paths if p and p != "-"], [expand_path(p, known) for p in deep])


def check_writes(segment: str, known: Optional[Dict[str, str]] = None,
                 remote: bool = False) -> Optional[Tuple[str, str]]:
    """A command that changes the agent's own permissions ("agent-settings"), files that start programs or hold keys —
    shell startup files, SSH keys, LaunchAgents ("protected-file") — or the system's own folders ("system-files").
    remote (run on another machine or in a container): those are not this computer's files, so only a recursive
    chmod/chown of / or a top-level folder stops — it breaks any machine."""
    paths, deep = changed_paths(segment, known)
    perms_only = program(segment).lower() in RECURSIVE_OWNERS | {"chattr"}  # chmod 600 ~/.ssh/id_x is a fix, not a risk
    for p in [] if remote else paths:
        if AGENT_CONTROL.search(p) or AGENT_CONTROL_DIR.search(p):
            return "agent-settings", (f"команда меняет `{p}`: эти файлы решают, что агенту разрешено (разрешения "
                                      "Antigravity, MCP-серверы, плагины, настройки Antigravity Plus).")
        if SCHEDULED_SIDECAR.search(p) and not perms_only:
            return "scheduled-sidecar", f"команда меняет `{p}`: sidecar-процессы и расписания Antigravity запускаются сами."
        if PROTECTED_FILE.search(p) and not perms_only:
            return "protected-file", f"команда меняет служебный файл `{p}` (автозапуск, SSH-ключи, профиль оболочки, /etc)."
        if SYSTEM_DIR.search(p):
            return "system-files", f"команда меняет системные файлы: `{p}`."
    for p in deep:  # ~ became this computer's home folder (expand_path): remote, it is the server's, not this one
        if p in ("/", "/*") or TOP_LEVEL.match(p) or not remote and p.rstrip("/*") == HOME.rstrip("/"):
            return "system-files", f"команда рекурсивно меняет права или владельца всего `{p}`."
    return None


def pipelines(cmd: str) -> List[List[str]]:
    """The command line's pipelines, each a list of its stages (`a | b`, `a |& b`); separators inside quotes and
    $(…) don't count (_split)."""
    out: List[List[str]] = [[]]
    for seg, op in _split(cmd):
        if seg.strip():
            out[-1].append(seg.strip())
        if op not in ("|", "|&"):
            out.append([])
    return [p for p in out if p]


DOWNLOADER = re.compile(r"^(curl|wget|fetch|aria2c|http|https|xh)(\.exe)?$", re.I)  # http/https: HTTPie
SH_RUNNERS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
CODE_RUNNERS = {"python", "node", "ruby", "perl", "php"}


def runs_stdin(prog: str, args: List[str]) -> bool:
    """Does `prog args` run the program it reads on stdin (`sh`, `bash -s`, `python3 -`) — rather than code or a
    script named in its arguments (`python3 -c '…'`, `python3 -m json.tool`, `node script.js`, `bash -c '…'`)?"""
    base = re.sub(r"(\d+(\.\d+)*)?(\.exe)?$", "", prog.lower())
    if base in SH_RUNNERS:
        if any(re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a) for a in args):
            return False
        if any(re.match(r"^-[a-zA-Z]*s", a) for a in args):
            return True
    elif base in CODE_RUNNERS:
        if any(a in ("-c", "-m", "-e", "-E", "-r", "-p", "--eval", "--print") for a in args):
            return False
    else:
        return False
    pos = [a for a in args if not a.startswith("-") or a == "-"]
    return not pos or pos[0] == "-"


def pipe_runs_download(cmd: str) -> bool:
    """`curl … | sh`, `wget -qO- … | sudo bash -s`, `curl … | python3`: what was just downloaded runs as code."""
    for stages in pipelines(cmd):
        fetched = False
        for stage in stages:
            tokens = strip_prefixes(tokenize(stage))
            if not tokens:
                continue
            prog = os.path.basename(tokens[0])
            if DOWNLOADER.match(prog):
                fetched = True
            elif fetched and runs_stdin(prog, tokens[1:]):
                return True
    return False


SQL_REASON = "SQL-команда удаляет таблицы или данные (DROP/TRUNCATE/DELETE без WHERE)."
SQL_IN_EXEC = re.compile(r"\b(psql|mysql|mariadb|sqlite3|mongosh?|clickhouse-client)\b")


def _substitutions(s: str) -> List[str]:
    """The bodies of the command substitutions in s — `$(…)`, `<(…)`, `>(…)`, backticks — outside single quotes (not
    of `$((…))` arithmetic, whose own substitutions do count)."""
    out: List[str] = []
    esc = "`" if compat.WINDOWS else "\\"
    i, n, dq = 0, len(s), False
    while i < n:
        c = s[i]
        if c == esc:
            i += 2
            continue
        if c == '"':
            dq = not dq
            i += 1
            continue
        if s.startswith("$((", i):
            i += 3
            continue
        opening, close = _opener(s, i)
        if not opening or dq and opening not in ("$(", "`"):  # inside "…" only $(…) and backticks run
            i += 1
            continue
        end = _skip(s, i + len(opening), close)
        if close in (")", "`") and end > i + len(opening) + 1:
            out.append(s[i + len(opening):end - 1])
        i = n if end < 0 else end
    return out


def command_strings(seg: str) -> List[str]:
    """The quoted strings of a segment that hands them to an interpreter (`bash -c '…'`, `ssh host "…"`, `eval "…"`),
    also as the interpreter gets them, quotes resolved: `bash -c 'echo '\\''hi'\\''; rm -rf ~'` runs one string,
    `echo 'hi'; rm -rf ~`."""
    found = [q[1:-1] for q in QUOTED.findall(seg)]
    try:
        found += [w for w in shlex.split(seg, posix=True) if re.search(r"\s", w)]
    except ValueError:
        pass
    return [s for s in dict.fromkeys(found) if s.strip()]


# Programs with "ssh" in the name that run what they are given here, not on a server.
SSH_LOCAL_TOOLS = {"ssh-agent", "ssh-add", "ssh-keygen", "ssh-keyscan", "ssh-copy-id", "sshd", "sshfs"}
CONTAINER_CLIS = {"docker", "podman", "kubectl", "docker-compose"}
# Their options before the subcommand that take a value (`kubectl -n prod exec …`, `docker compose -f x.yml exec`),
# and the ones that take none: any other option right before `exec` may have taken it as its value (`docker
# --tlscacert exec run -v /:/host …` is `docker run`), so such a command counts as one that runs here.
CONTAINER_VALUE_OPTS = {"-c", "--context", "-H", "--host", "-l", "--log-level", "--config", "--connection", "--url",
                        "--identity", "-n", "--namespace", "--kubeconfig", "--cluster", "--user", "-s", "--server",
                        "--token", "--as", "-v", "-f", "--file", "-p", "--project-name", "--project-directory",
                        "--env-file", "--profile", "--tlscacert", "--tlscert", "--tlskey", "--as-group", "--as-uid",
                        "--cache-dir", "--certificate-authority", "--client-certificate", "--client-key", "--password",
                        "--profile-output", "--request-timeout", "--tls-server-name", "--username", "--v", "--vmodule",
                        "--log-dir", "--log-file", "--log-flush-frequency", "--ansi", "--parallel", "--progress",
                        "--root", "--runroot", "--runtime", "--storage-driver", "--storage-opt", "--tmpdir", "--ssh",
                        "--cgroup-manager", "--events-backend", "--hooks-dir", "--network-config-dir", "--module"}
CONTAINER_FLAGS = {"-D", "--debug", "--tls", "--tlsverify", "--insecure-skip-tls-verify", "--match-server-version",
                   "--disable-compression", "--warnings-as-errors", "--dry-run", "--compatibility", "--all-resources",
                   "-r", "--remote", "--syslog", "--transient-store", "--verbose", "--no-ansi", "--skip-hostname-check"}
# ssh destinations that are this computer itself: what ssh runs there runs here.
try:
    _NAME = socket.gethostname().lower().rstrip(".")
except Exception:  # no name: only the loopback ones
    _NAME = ""
THIS_COMPUTER = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "0.0.0.0", "::1",
                 "0:0:0:0:0:0:0:1"} | ({_NAME, _NAME.split(".")[0], _NAME.split(".")[0] + ".local"} if _NAME else set())


def _this_computer(host: str) -> bool:
    """Is this ssh destination (`user@host`, `ssh://user@host:2222`, `[::1]`) this computer — localhost, 127.x, ::1 or
    its own name?"""
    h = host.lower()
    uri = h.startswith("ssh://")
    h = (h[6:] if uri else h).rsplit("@", 1)[-1].rstrip("/")
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif uri:
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    return h in THIS_COMPUTER or bool(re.fullmatch(r"127(?:\.\d{1,3}){1,3}", h))


def _exec_at(words: List[str], i: int, prog: str) -> Optional[int]:
    """Where `exec`'s own words start in `docker [options] exec …` (also `docker compose exec`, `docker container
    exec`, podman, `kubectl [options] exec …`), words[i] being the CLI; None for another subcommand, or when an option
    it doesn't know stands right before `exec` (it may have taken `exec` as its value)."""
    seen, j = [], i + 1
    while j < len(words) and len(seen) < 2 and seen[-1:] != ["exec"]:
        w, before = words[j], words[j - 1] if j > i + 1 else ""
        if w == "exec" and before.startswith("-") and "=" not in before and before not in CONTAINER_FLAGS:
            return None
        j += 2 if w in CONTAINER_VALUE_OPTS else 1
        if not w.startswith("-"):
            seen.append(w)
    if seen != ["exec"] and not (prog in ("docker", "podman") and seen in (["compose", "exec"], ["container", "exec"])):
        return None
    return j


def runs_elsewhere(seg: str) -> bool:
    """Does this command run its command text on another machine — ssh, sshpass, a wrapper script named after ssh
    (`./deploy/ssh.sh "…"`), also under `timeout 30 …` — or inside a container (`docker exec`, `docker compose
    exec`, `podman exec`, `kubectl exec`)? Not `docker run`: its volumes can reach this computer; not `ssh localhost`."""
    tokens = strip_prefixes(tokenize(seg))
    if tokens and os.path.basename(tokens[0]) in ("timeout", "gtimeout"):  # timeout [-s SIG] [-k T] 30 ssh …
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in ("-s", "--signal", "-k", "--kill-after") else 1
        tokens = tokens[i + 1:]  # `timeout 30 sudo ssh …` stays local: its sudo runs here
    prog = re.sub(r"\.exe$", "", os.path.basename(tokens[0]).lower()) if tokens else ""
    if "ssh" in prog:
        if prog in SSH_LOCAL_TOOLS:
            return False
        parts = _ssh_parts(tokens, 0, prog) if prog in ("ssh", "sshpass") else None
        return not (parts and _this_computer(tokens[parts[0]]))
    return prog in CONTAINER_CLIS and _exec_at(tokens, 0, prog) is not None


ELSEWHERE_HINT = re.compile(r"ssh|docker|podman|kubectl", I)  # every program runs_elsewhere knows has one in its name
# Options of `exec` that take a value: docker/podman/compose (`-e`, `-u`, `-w`), kubectl (`-c`, `-f`, `-n`, `-s`, `-v`).
EXEC_VALUE_OPTS = {"--env", "--env-file", "--user", "--workdir", "--detach-keys", "--index", "--preserve-fd",
                   "--preserve-fds", "--cidfile", "--container", "--filename", "--pod-running-timeout", "--namespace",
                   "--context", "--cluster", "--kubeconfig", "--server", "--token", "--as", "--as-group", "--as-uid",
                   "--request-timeout", "--v", "--log-file", "--username", "--password"}


def _options_end(words: List[str], i: int, letters: str, longs: AbstractSet[str] = frozenset()) -> Tuple[int, bool]:
    """Where a getopt-style command's options from words[i] on end: (the index of its first other word, whether `--`
    ended them). A long option in `longs` takes the next word (`--user root`; `--user=root` is one word); in a cluster
    of short ones (`-it`, `-itu root`) a letter in `letters` takes the rest of the word (`-p2222`) or the next one."""
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1, True
        if len(w) < 2 or not w.startswith("-"):
            return i, False
        i += 1
        if w.startswith("--"):
            i += 1 if w in longs else 0
            continue
        for k in range(1, len(w)):
            if w[k] in letters:
                i += 1 if k == len(w) - 1 else 0
                break
    return i, False


def _ssh_parts(words: List[str], i: int, prog: str) -> Optional[Tuple[int, int]]:
    """(where the host is, where the remote command starts) in `[sshpass …] ssh [options] host [options] [--]
    command…`, words[i] being `prog`, sshpass or ssh (OpenSSH reads options after the host too, up to its first other
    word); None without a host or when sshpass runs something else."""
    if prog == "sshpass":
        i = _options_end(words, i + 1, "pfdP")[0]
        if i >= len(words) or re.sub(r"\.exe$", "", os.path.basename(words[i]).lower()) != "ssh":
            return None
    letters = "".join(SSH_ARG_OPTS)
    host, ended = _options_end(words, i + 1, letters)
    if host >= len(words):
        return None
    return host, host + 1 if ended else _options_end(words, host + 1, letters)[0]


def _exec_command_at(words: List[str], i: int, prog: str) -> Optional[int]:
    """Where the command starts in `docker [options] exec [options] container command…` (`docker compose exec`,
    `docker container exec`, podman, `kubectl exec pod [options] -- command…`), words[i] being the CLI; None for
    another subcommand."""
    j = _exec_at(words, i, prog)
    if j is None:
        return None
    if prog == "kubectl":  # kubectl exec pod -- command…; the older form without `--`: the words after the pod
        if "--" in words[j:]:
            return words.index("--", j) + 1
        k = _options_end(words, j, "cfnsv", EXEC_VALUE_OPTS)[0]
        return _options_end(words, k + 1, "cfnsv", EXEC_VALUE_OPTS)[0] if k < len(words) else None
    k = _options_end(words, j, "euw", EXEC_VALUE_OPTS)[0]
    if prog == "podman" and any(w == "--latest" or re.fullmatch(r"-[dit]*l[dilt]*", w) for w in words[j:k]):
        return k  # podman exec -l: the latest container, none named
    if k >= len(words):
        return None
    return k + 2 if words[k + 1:k + 2] == ["--"] else k + 1  # `--` as a command fails; read on, to be safe


def elsewhere_words(seg: str) -> Optional[Tuple[str, str]]:
    """A command that runs its own words on another machine or in a container (runs_elsewhere) → (what runs here — the
    program, its options, the host or container —, what runs there, as a command line): `ssh h rm -rf /` → ("ssh h",
    "rm -rf /"). ssh joins its words with spaces for the remote shell and a wrapper script named after ssh passes them
    on (`./deploy/ssh.sh uptime`); `docker exec app rm -rf /` and `kubectl exec pod -- rm -rf /` run them as they are.
    None for anything else, and with nothing to run there (`ssh h` opens a session; its heredoc is read on its own)."""
    if not ELSEWHERE_HINT.search(seg):  # no such program named: the long `git commit -m "…"` is not split again
        return None
    words = tokenize(seg)
    tokens = strip_prefixes(words)
    if not tokens or words[len(words) - len(tokens):] != tokens:  # env -S rebuilt them: its string is read on its own
        return None
    i = 0
    if os.path.basename(tokens[0]) in ("timeout", "gtimeout"):  # timeout [-s SIG] [-k T] 30 ssh … (runs_elsewhere)
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in ("-s", "--signal", "-k", "--kill-after") else 1
        i += 1
    prog = re.sub(r"\.exe$", "", os.path.basename(tokens[i]).lower()) if i < len(tokens) else ""
    if "ssh" in prog and prog not in SSH_LOCAL_TOOLS:
        parts = _ssh_parts(tokens, i, prog) if prog in ("ssh", "sshpass") else (i, i + 1)
        if prog in ("ssh", "sshpass") and parts and _this_computer(tokens[parts[0]]):
            return None  # `ssh localhost crontab -e` runs here
        at, join = parts[1] if parts else None, " ".join
    elif prog in CONTAINER_CLIS:
        at, join = _exec_command_at(tokens, i, prog), shlex.join
    else:
        return None
    if at is None or at >= len(tokens):
        return None
    return shlex.join(words[:len(words) - len(tokens) + at]), join(tokens[at:])


def runs_there(seg: str) -> str:
    """The program that ends up running in `ssh h psql`, `./deploy/ssh.sh 'docker exec -i db psql'` (through each
    machine and container on the way: elsewhere_words), or of the command itself."""
    for _ in range(3):
        there = elsewhere_words(seg)
        if not there:
            break
        seg = there[1]
    return program(seg)


def analyze_command(cmd: str, depth: int = 0, system_only: bool = False,
                    known: Optional[Dict[str, str]] = None, remote: bool = False) -> Optional[Tuple[str, str]]:
    """The command's first stop as (rule id, reason), or None. system_only (Bypass Mode): only the stops that hold
    there (SYSTEM_RULES), looked for in the whole command — past any project-level match. known: what the command
    around this one set (its variables and current folder: `cd / && bash -c 'rm -rf *'`, `x=$(rm -rf $S/y)`).
    remote: the text runs on another machine or in a container (runs_elsewhere) — without the stops that only protect
    this computer (LOCAL_RULES), down into its own quoted strings, $(…) and heredocs. What a command hands over to run
    there — its unquoted words too (`ssh h crontab -e`, `docker exec app rm -rf /`: elsewhere_words), a heredoc fed to
    it (`./deploy/ssh.sh 'bash -s' <<'EOF'`) — is analysed so."""
    if not cmd or not cmd.strip() or depth > 2:
        return None
    keep = lambda rid: (not system_only or rid in SYSTEM_RULES) and not (remote and rid in LOCAL_RULES)  # noqa: E731
    if compat.WINDOWS:  # PowerShell/cmd: backslashes are path separators, also inside quotes
        cmd = cmd.replace("\\", "/")
    cmd = re.sub(r"\\\r?\n", " ", cmd)
    if keep("agent-settings") and SELF_SWITCH.search(cmd):
        return "agent-settings", SELF_SWITCH_REASON
    cmd, heredocs = _heredocs(cmd)
    # Quoted text is data (commit messages, search patterns) unless it is handed to an interpreter.
    text = cmd if INTERPRETER.search(cmd) else QUOTED.sub("''", cmd)
    for rid, rx, reason in PIPE_RULES:
        if keep(rid) and rx.search(text):
            return rid, reason
    if keep("curl-pipe-shell") and pipe_runs_download(cmd):
        return "curl-pipe-shell", CURL_REASON
    known = dict(known or {})
    deeper = depth < 2  # what a segment runs inside — $(…), a quoted command string — is analysed on its own
    for seg in split_unquoted(cmd):
        for sub in _substitutions(seg):  # a subshell of this shell: the same variables and folder
            hit = analyze_command(sub, depth + 1, system_only, known, remote)
            if hit:
                return hit
        note_assignments(seg, known)
        prog = program(seg)
        if prog in TEXT_PROGRAMS:  # its arguments are text, but `echo … >> ~/.zshrc` still writes a file
            w = check_writes(seg, known, remote)
            if w:
                return w
            continue
        if keep("sql-destructive") and (prog in SQL_PROGRAMS or prog in ("docker", "kubectl", "ssh")
                                         and SQL_IN_EXEC.search(seg)) and SQL_DESTRUCTIVE.search(seg):
            return "sql-destructive", SQL_REASON
        r = check_rm(seg, known) or check_find(seg, known) or check_win_rm(seg)
        if r and keep(r[0]):
            return r
        bare = _bare(seg)
        code = prog in SHELLS or bool(INTERPRETER.search(bare))  # bash -c '…', ssh host '…', env -S '…'
        # The rules read the command's own words; a quoted command string is split at its own separators below, so
        # `ssh h 'git pull && make clean && rm -f x'` is no `git clean -f`. Too deep for that: the whole text.
        target = bare if deeper else seg if INTERPRETER.search(seg) else QUOTED.sub("''", seg)
        # `ssh h crontab -e`: only the program, its options and the host or container run here, so the rules that
        # protect only this computer read just those; the words that run there are analysed as remote below. The
        # other rules read all the words, as before (`docker exec app cat ~/.ssh/id_rsa`).
        there = elsewhere_words(seg)
        here = _bare(there[0]) if there else target
        for rid, rx, reason in SEGMENT_RULES:
            if keep(rid) and rx.search(here if rid in LOCAL_RULES else target):
                return rid, reason
        # the panel's routes in all the words: `ssh -R 9999:localhost:…` brings a server's request to this computer
        if keep("agent-settings") and prog.lower() not in ("git", "git.exe") and _sidecar_api_hit(seg):
            return "agent-settings", SIDECAR_API_REASON
        if keep("scheduled-sidecar") and schedule_change(there[0] if there else seg):
            return "scheduled-sidecar", SCHEDULE_REASON
        w = check_writes(seg, known, remote)  # a redirection outside quotes writes here: `ssh h cat x > /etc/y`
        if w:
            return w
        analysed: List[str] = []  # command strings analysed as remote with no folder: `ssh h '…'` needs no second
        if code and deeper:  # the quoted text is a command, run from this folder (its variables are not passed on)
            cwd = {"__cwd__": known["__cwd__"]} if "__cwd__" in known else None
            elsewhere = remote or runs_elsewhere(seg)  # `ssh host "…"`, `docker exec app sh -c '…'`
            for line in command_strings(seg):
                hit = analyze_command(line, depth + 1, system_only, cwd, elsewhere)
                if hit:
                    return hit
                analysed += [line] if elsewhere and not cwd else []
        if there and deeper and there[1] not in analysed:  # there, neither this shell's variables nor its folder apply
            hit = analyze_command(there[1], depth + 1, system_only, remote=True)
            if hit:
                return hit
    for before, rest, body in heredocs:  # bodies fed to a shell or a DB client are code
        owner = heredoc_owner(before, rest)  # `cd /srv/app && ssh h <<'EOF'`: the body goes to ssh, not to cd
        prog = program(owner)
        if keep("sql-destructive") and runs_there(owner) in SQL_PROGRAMS and SQL_DESTRUCTIVE.search(body):
            return "sql-destructive", SQL_REASON  # `psql <<EOF`, `docker exec -i db psql <<EOF`, `ssh h psql <<EOF`
        far = runs_elsewhere(owner)  # `ssh host <<'EOF'`, `./deploy/ssh.sh 'bash -s' <<'EOF'`, `docker exec -i app sh`
        if prog in SHELLS or far:  # what runs on the server or in the container reads it there
            hit = analyze_command(body, depth + 1, system_only, remote=remote or far)
            if hit:
                return hit
    return None


# ── restrictions the user set in this conversation ("не пушь", "don't commit") ──
PUSH_RULE = re.compile(
    r"\bgit\b.*\bpush\b|\b(docker|podman|buildah)\s+push\b|\b(scp|sftp)\s|\brsync\b.*\s[\w.@-]+:\S*"
    r"|\b(fly(ctl)?|netlify|firebase|railway|wrangler|serverless|sls|eb|amplify|vercel|surge)\s+(deploy|publish|up)\b"
    r"|\bvercel\b.*\s--prod\b|\bkubectl\s+(apply|create|replace|patch|rollout|set|scale|edit|delete)\b"
    r"|\bhelm\s+(install|upgrade|rollback|uninstall)\b|\b(terraform|tofu)\s+(apply|destroy)\b|\bpulumi\s+(up|destroy)\b"
    r"|\bansible(-playbook)?\s|\bgcloud\b.*\bdeploy\b|\baws\s+s3\s+(cp|sync|mv)\b.*\ss3://"
    r"|\bgh\s+(pr\s+(create|merge)|release\s+create|workflow\s+run)\b|\b(npm|pnpm|yarn|bun|cargo|poetry|gem|twine)\s+publish\b"
    r"|\b(make|just|task|npm\s+run|pnpm(\s+run)?|yarn(\s+run)?|bun\s+run)\s+[\w:.-]*(deploy|release|publish|ship)")
COMMIT_RULE = re.compile(r"\bgit\b.*\bcommit\b")
DEPLOY_SCRIPT = re.compile(r"deploy|release|publish|rollout|provision", I)
SCRIPT_RUNNERS = {"sh", "bash", "zsh", "dash", "py", "python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx", "ts-node"}
SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
# Remote commands that only look around (anything else over ssh may change the server).
READ_ONLY = {"cat", "ls", "ll", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "zgrep", "rg", "ag", "find",
             "stat", "file", "wc", "df", "du", "free", "uptime", "whoami", "id", "groups", "hostname", "uname", "ps",
             "pgrep", "top", "htop", "vmstat", "iostat", "journalctl", "dmesg", "echo", "printf", "pwd", "date", "env",
             "printenv", "which", "whereis", "type", "command", "ss", "netstat", "lsof", "ip", "ifconfig", "ping",
             "dig", "nslookup", "host", "traceroute", "curl", "wget", "sort", "uniq", "cut", "tr", "jq", "yq", "awk",
             "sed", "sha256sum", "sha1sum", "md5sum", "test", "true", "false", "nproc", "lscpu", "lsblk", "last",
             "w", "who", "getent", "tree", "realpath", "readlink", "basename", "dirname", "xxd", "hexdump", "od",
             "strings", "diff", "cmp", "sleep", "cd", "export", "column", "nl", "openssl"}
READ_ONLY_SUB = {
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "show", "cat",
                  "list-timers"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "version", "info", "top", "port", "diff", "history"},
    "git": {"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "blame"},
    "crontab": {"-l"}, "nginx": {"-t", "-T", "-v", "-V"}, "service": {"status"}, "ufw": {"status"},
}
COMPOSE_READ = {"ps", "logs", "config", "images", "top", "version", "ls"}
REDIRECT = re.compile(r"(?<![0-9&<>])>{1,2}(?!\s*&)(?!\s*/dev/null)")


def ssh_remote(tokens: List[str]) -> Optional[str]:
    """The remote command of an ssh call ("" for an interactive session); None when this isn't ssh."""
    if tokens and os.path.basename(tokens[0]) == "sshpass":
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in ("-p", "-f", "-d") else 1
        tokens = tokens[i:]
    if not tokens or os.path.basename(tokens[0]) != "ssh":
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "--":
        i += 2 if len(tokens[i]) == 2 and tokens[i][1] in SSH_ARG_OPTS else 1
    if i < len(tokens) and tokens[i] == "--":
        i += 1
    return " ".join(tokens[i + 1:])


SAFE_PY_MODULES = {
    "os", "os.path", "sys", "json", "re", "time", "datetime", "math", "collections", "itertools", "functools",
    "pathlib", "urllib.request", "urllib.parse", "http.client", "socket", "ssl", "sqlite3", "psycopg", "psycopg2",
    "asyncpg", "redis", "hashlib", "base64", "csv", "io", "stat", "glob", "gzip",
}
FORBIDDEN_PY_NAMES = {
    "exec", "eval", "compile", "__import__", "__builtins__", "builtins", "importlib",
    "setattr", "delattr", "breakpoint", "vars", "globals", "locals", "dir",
    "__dict__", "__class__", "__subclasses__", "__mro__", "__bases__", "__globals__",
    "__code__", "__closure__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
    "input", "FileIO", "RawIOBase", "BufferedWriter", "BufferedRandom", "TextIOWrapper",
}
FORBIDDEN_PY_CALLS = {
    "system", "popen", "spawn", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "fork", "forkpty", "kill", "killpg",
    "remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace",
    "makedirs", "mkdir", "chmod", "fchmod", "lchmod", "chown", "fchown", "lchown",
    "truncate", "ftruncate", "write", "writelines", "write_text", "write_bytes",
    "pwrite", "pwritev", "writev", "sendfile", "copy_file_range",
    "touch", "symlink_to", "hardlink_to", "link", "symlink", "mkfifo", "mknod",
    "putenv", "unsetenv", "commit", "executemany", "executescript", "execute_command", "callproc",
    "post", "put", "delete", "patch", "putrequest", "putheader", "endheaders", "makefile",
    "load_extension", "backup",
    "set", "setex", "psetex", "setnx", "mset", "msetnx", "hset", "hsetnx", "hmset",
    "hdel", "hincrby", "hincrbyfloat", "lpush", "rpush", "lpop", "rpop", "lrem", "ltrim",
    "linsert", "lset", "lmove", "blpop", "brpop", "brpoplpush", "rpoplpush",
    "sadd", "srem", "spop", "smove", "zadd", "zrem", "zpopmin", "zpopmax", "bzpopmin", "bzpopmax", "zincrby",
    "incr", "incrby", "incrbyfloat", "decr", "decrby", "append", "setrange", "getset", "getdel", "getex",
    "expire", "pexpire", "renamenx", "flushdb", "flushall", "publish", "restore", "migrate",
    "evalsha", "fcall", "fcall_ro", "config_set", "save", "bgsave", "bgrewriteaof", "shutdown", "slaveof", "replicaof",
    "sendall", "send", "sendto", "sendmsg",
}
SQL_PY_MUTATION = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+(?:TABLE|INDEX|DATABASE|SCHEMA|VIEW|TRIGGER|FUNCTION)"
    r"|ALTER\s+TABLE|CREATE\s+(?:TABLE|INDEX|TRIGGER|FUNCTION)|TRUNCATE|REPLACE\s+INTO|MERGE\s+INTO"
    r"|GRANT|REVOKE|COPY\s+\w+\s+FROM|CALL|VACUUM|ATTACH|DETACH)\b",
    I,
)
SAFE_DYNAMIC_SQL_START = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN|SHOW)\b", I)
PYTHON_CMD_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)?)?|py)$", re.I)
PY_VAL_FLAGS = {"-X", "-W", "-Q", "--check-hash-based-pycs"}
PY_BOOL_FLAGS = re.compile(r"^-[BbdEIiOPqRsSuvVx3]+$")


def _is_const_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _const_str_join(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue):
        inner = _const_str_join(node.value)
        return inner if inner is not None else " _VAR_ "
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _const_str_join(v) if isinstance(v, ast.FormattedValue)
            else (v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " _VAR_ ")
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_str_join(node.left)
        right = _const_str_join(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _safe_open_call(node: ast.Call, is_std_hint: Optional[bool] = None) -> bool:
    if any(isinstance(a, ast.Starred) for a in node.args) or any(kw.arg is None for kw in node.keywords):
        return False
    kw_map = {kw.arg: kw.value for kw in node.keywords}
    if "opener" in kw_map:
        return False
    mode_node = kw_map.get("mode")
    if mode_node is None:
        is_std = is_std_hint if is_std_hint is not None else (
            (isinstance(node.func, ast.Name) and node.func.id == "open") or (
                isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("io", "gzip", "builtins")
            )
        )
        pos_idx = 1 if is_std else 0
        if len(node.args) > pos_idx:
            mode_node = node.args[pos_idx]
    if mode_node is None:
        return True
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return bool(mode_node.value) and set(mode_node.value) <= {"r", "b", "t"}
    return False


def _safe_http_call(node: ast.Call, name: str, has_http_client: bool) -> bool:
    if any(isinstance(a, ast.Starred) for a in node.args) or any(kw.arg is None for kw in node.keywords):
        return False
    kw_map = {kw.arg: kw.value for kw in node.keywords}
    if name in ("Request", "urlopen"):
        if len(node.args) >= 2 and not _is_const_none(node.args[1]):
            return False
        if "data" in kw_map and not _is_const_none(kw_map["data"]):
            return False
        if name == "Request":
            method_node = kw_map.get("method")
            if method_node is None and len(node.args) >= 6:
                method_node = node.args[5]
            if method_node is not None and not _is_const_none(method_node):
                if not (isinstance(method_node, ast.Constant) and isinstance(method_node.value, str)
                        and method_node.value.upper() in ("GET", "HEAD")):
                    return False
        return True
    if name == "request" and has_http_client:
        if len(node.args) >= 3 and not _is_const_none(node.args[2]):
            return False
        if "body" in kw_map and not _is_const_none(kw_map["body"]):
            return False
        method_node = kw_map.get("method") or (node.args[0] if node.args else None)
        if not (isinstance(method_node, ast.Constant) and isinstance(method_node.value, str)
                and method_node.value.upper() in ("GET", "HEAD")):
            return False
    return True


def readonly_python_code(src: str) -> bool:
    """AST check: True only when Python source uses allowed read-only stdlib/DB modules and performs no mutations."""
    if not isinstance(src, str) or not src.strip() or len(src) > 20_000:
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    has_http_client = False
    open_aliases: Set[str] = set()
    http_aliases: Dict[str, str] = {}
    urllib_req_aliases: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in SAFE_PY_MODULES:
                    return False
                if alias.name in ("http.client", "http"):
                    has_http_client = True
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                return False
            if any(a.name == "*" or a.name in FORBIDDEN_PY_NAMES or a.name in FORBIDDEN_PY_CALLS for a in node.names):
                return False
            if node.module == "os" and any(a.name == "open" for a in node.names):
                return False
            if node.module in SAFE_PY_MODULES:
                if node.module == "http.client":
                    has_http_client = True
                for a in node.names:
                    local = a.asname or a.name
                    if a.name == "open" and node.module in ("io", "gzip", "builtins"):
                        open_aliases.add(local)
                    if node.module == "urllib.request" and a.name in ("Request", "urlopen"):
                        http_aliases[local] = a.name
            elif node.module == "urllib" and all(a.name in ("request", "parse") for a in node.names):
                for a in node.names:
                    if a.name == "request":
                        urllib_req_aliases.add(a.asname or a.name)
            elif node.module == "http" and all(a.name == "client" for a in node.names):
                has_http_client = True
            else:
                return False

    guarded_names = {"open", "getattr", "Request", "urlopen"} | open_aliases | set(http_aliases)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_PY_NAMES:
                return False
            if node.id in guarded_names and id(node) not in call_funcs:
                return False
            if node.id in FORBIDDEN_PY_CALLS and node.id != "set" and id(node) not in call_funcs:
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_PY_NAMES or node.attr in FORBIDDEN_PY_CALLS:
                return False
            if node.attr in ("autocommit", "isolation_level") and isinstance(node.ctx, ast.Store):
                return False
            if node.attr in ("open", "Request", "urlopen") and id(node) not in call_funcs:
                return False
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                if SQL_PY_MUTATION.search(node.value):
                    return False
            elif isinstance(node.value, bytes):
                if SQL_PY_MUTATION.search(node.value.decode("utf-8", errors="ignore")):
                    return False
        elif isinstance(node, (ast.JoinedStr, ast.BinOp)):
            joined = _const_str_join(node)
            if joined and SQL_PY_MUTATION.search(joined):
                return False
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id in FORBIDDEN_PY_NAMES or (fn.id in FORBIDDEN_PY_CALLS and fn.id != "set"):
                    return False
                if fn.id == "getattr":
                    if (len(node.args) not in (2, 3) or any(isinstance(a, ast.Starred) for a in node.args)
                            or node.keywords):
                        return False
                    attr = node.args[1]
                    if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                        return False
                    if attr.value in FORBIDDEN_PY_NAMES or attr.value in FORBIDDEN_PY_CALLS:
                        return False
                elif fn.id == "print":
                    for kw in node.keywords:
                        if kw.arg == "file" and not _is_const_none(kw.value) and not (
                            isinstance(kw.value, ast.Attribute)
                            and isinstance(kw.value.value, ast.Name)
                            and kw.value.value.id == "sys"
                            and kw.value.attr in ("stdout", "stderr")
                        ):
                            return False
                elif fn.id == "open" or fn.id in open_aliases:
                    if not _safe_open_call(node, is_std_hint=True):
                        return False
                elif fn.id in http_aliases and not _safe_http_call(node, http_aliases[fn.id], has_http_client):
                    return False
            elif isinstance(fn, ast.Attribute):
                if fn.attr in FORBIDDEN_PY_NAMES or fn.attr in FORBIDDEN_PY_CALLS:
                    return False
                if fn.attr == "open":
                    if isinstance(fn.value, ast.Name) and fn.value.id == "os":
                        return False
                    if not _safe_open_call(node):
                        return False
                if fn.attr in ("Request", "urlopen", "request") and not _safe_http_call(node, fn.attr, has_http_client):
                    return False
                if fn.attr == "connect":
                    kw_map = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                    if "isolation_level" in kw_map and _is_const_none(kw_map["isolation_level"]):
                        return False
                    if len(node.args) >= 4 and _is_const_none(node.args[3]):
                        return False
                    if "autocommit" in kw_map and isinstance(kw_map["autocommit"], ast.Constant) and kw_map["autocommit"].value is True:
                        return False
                if fn.attr == "execute" and node.args:
                    arg0 = node.args[0]
                    if not isinstance(arg0, (ast.Constant, ast.Name)):
                        joined = _const_str_join(arg0)
                        if not joined or ";" in joined or not SAFE_DYNAMIC_SQL_START.match(joined):
                            return False
            else:
                return False
    return True


def _python_inline(tokens: List[str]) -> Optional[str]:
    """Return Python `-c` code if `tokens` invokes `python[3[.x]] -c <code>`, else None."""
    if not tokens or not PYTHON_CMD_RE.match(os.path.basename(tokens[0])):
        return None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in PY_VAL_FLAGS:
            i += 2
            continue
        if t.startswith(("-X", "-W", "-Q")) and len(t) > 2:
            i += 1
            continue
        if t == "-c" or re.fullmatch(r"-[BbdEIiOPqRsSuvVx3]+c", t):
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if t.startswith("-c") and len(t) > 2:
            return t[2:]
        if PY_BOOL_FLAGS.match(t):
            i += 1
            continue
        return None
    return None


def _python_stdin(tokens: List[str]) -> bool:
    """True if `tokens` invokes `python[3[.x]] [-]` reading code from stdin."""
    if not tokens or not PYTHON_CMD_RE.match(os.path.basename(tokens[0])):
        return False
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in PY_VAL_FLAGS:
            i += 2
            continue
        if t.startswith(("-X", "-W", "-Q")) and len(t) > 2:
            i += 1
            continue
        if PY_BOOL_FLAGS.match(t):
            i += 1
            continue
        return t == "-"
    return True


FIND_MUTATORS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf"}


def _segment_writes(seg: str, heredoc_body: Optional[str] = None, depth: int = 0) -> bool:
    if depth > 2:
        return True
    if REDIRECT.search(QUOTED.sub("''", seg)):
        return True
    if any(remote_writes(sub, depth + 1) for sub in _substitutions(seg)):
        return True
    tokens = strip_prefixes(tokenize(seg))
    if not tokens:
        return False
    prog, args = os.path.basename(tokens[0]), tokens[1:]
    if heredoc_body is not None:
        if _python_stdin(tokens):
            return not readonly_python_code(heredoc_body)
        if prog == "docker" and args[:1] == ["exec"]:
            rest = args[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in ("-e", "--env", "-u", "--user", "-w", "--workdir") else rest[1:]
            if len(rest) >= 2 and _python_stdin(rest[1:]):
                return not readonly_python_code(heredoc_body)
        if prog in SH_RUNNERS:
            return remote_writes(heredoc_body, depth + 1)
        return True
    py_src = _python_inline(tokens)
    if py_src is not None:
        return not readonly_python_code(py_src)
    if prog in READ_ONLY:
        if prog == "sed" and any(a == "--in-place" or a.startswith(("-i", "--in-place=")) for a in args):
            return True
        if prog == "find" and any(a in FIND_MUTATORS for a in args):
            return True
        if prog == "sort" and any(a == "-o" or a.startswith(("-o", "--output")) for a in args):
            return True
        if prog in ("awk", "gawk", "mawk", "nawk") and (
            "system(" in seg or re.search(r"\b(?:print|printf)\b[^;\n]*>", seg)
            or re.search(r"\|\s*getline\b|getline\s*<", seg)
        ):
            return True
        if prog in ("curl", "wget") and any(a.startswith((
            "-X", "--request", "--method", "-d", "--data", "--json", "-F", "--form", "-T", "--upload",
            "-o", "-O", "--output", "--remote-name", "-J", "--remote-header-name", "--post", "-K", "--config",
        )) for a in args):
            return True
        return False
    sub = next((a for a in args if not a.startswith("-") or prog == "crontab"), "")
    if prog == "docker" and sub == "compose":
        if set(args) & COMPOSE_READ and not set(args) & {"up", "down", "restart", "build", "pull", "rm", "stop",
                                                         "start", "exec", "run", "kill", "create", "push"}:
            return False
        return True
    if prog == "docker" and sub == "exec":
        rest = args[args.index("exec") + 1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-e", "--env", "-u", "--user", "-w", "--workdir") else rest[1:]
        if len(rest) >= 2:
            if os.path.basename(rest[1]) in READ_ONLY:
                return False
            sub_py = _python_inline(rest[1:])
            if sub_py is not None:
                return not readonly_python_code(sub_py)
        return True
    if prog == "service" and "status" in args:
        return False
    if sub in READ_ONLY_SUB.get(prog, ()):
        return False
    return True


def _ssh_heredoc_writes(remote: str, body: str, depth: int = 0) -> bool:
    if depth > 2:
        return True
    if not remote.strip():
        return remote_writes(body, depth + 1)
    segs = split_unquoted(re.sub(r"\\\r?\n", " ", remote))
    consumed = False
    for seg in segs:
        tokens = strip_prefixes(tokenize(seg))
        if not tokens:
            continue
        if not consumed and _python_stdin(tokens):
            consumed = True
            if _segment_writes(seg, body, depth + 1):
                return True
        elif not consumed and os.path.basename(tokens[0]) == "docker" and "exec" in tokens[1:]:
            rest = tokens[tokens.index("exec") + 1:]
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in ("-e", "--env", "-u", "--user", "-w", "--workdir") else rest[1:]
            if len(rest) >= 2 and _python_stdin(rest[1:]):
                consumed = True
                if _segment_writes(seg, body, depth + 1):
                    return True
            elif _segment_writes(seg, depth=depth + 1):
                return True
        elif not consumed and os.path.basename(tokens[0]) in SH_RUNNERS and len(segs) == 1:
            consumed = True
            if _segment_writes(seg, body, depth + 1):
                return True
        elif _segment_writes(seg, depth=depth + 1):
            return True
    if not consumed:
        return remote_writes(body, depth + 1)
    return False


def remote_writes(remote: str, depth: int = 0) -> bool:
    """Could this command, run on the server, change it? Only clearly read-only commands say no."""
    if depth > 2 or not remote.strip():
        return True  # interactive shell on the server
    remote_cmd, docs = split_heredocs(remote)
    remote_cmd = re.sub(r"\\\r?\n", " ", remote_cmd)
    doc_idx = 0
    for seg in split_unquoted(remote_cmd):
        bare = QUOTED.sub("''", seg)
        body = None
        if "<<" in bare and doc_idx < len(docs):
            body = docs[doc_idx][1]
            doc_idx += 1
        elif "<<" in bare:
            return True
        if _segment_writes(seg, body, depth + 1):
            return True
    if doc_idx < len(docs):
        return True
    return False


def script_of(tokens: List[str]) -> str:
    """Path of the script a command runs (./deploy/update.sh, bash deploy.sh, python3 release.py), or ""."""
    if not tokens:
        return ""
    prog = os.path.splitext(os.path.basename(tokens[0].replace("\\", "/")))[0].lower()
    if prog in SCRIPT_RUNNERS or re.fullmatch(r"python\d+(?:\.\d+)?", prog):
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-c", "-m", "-e"):
                return ""
            if tok in ("-X", "-W", "-Q", "-r"):
                i += 2
                continue
            if tok == "--":
                return tokens[i + 1] if i + 1 < len(tokens) else ""
            if tok.startswith("-"):
                i += 1
                continue
            return tok
        return ""
    if "/" in tokens[0] or "\\" in tokens[0] or tokens[0].endswith((".sh", ".py")):
        return tokens[0]
    return ""


def outward_kinds(cmd: str, depth: int = 0) -> Set[str]:
    """{"push", "commit"} subsets: does the command push/deploy/upload, or create a commit?"""
    kinds: Set[str] = set()
    if not cmd or not cmd.strip() or depth > 2:
        return kinds
    cmd, heredocs = split_heredocs(re.sub(r"\\\r?\n", " ", cmd))
    for seg in split_unquoted(cmd):
        tokens = strip_prefixes(tokenize(seg))
        if not tokens or os.path.basename(tokens[0]) in TEXT_PROGRAMS:
            continue
        remote = ssh_remote(tokens)
        if remote is not None:
            if "<<" not in QUOTED.sub("''", seg) and remote_writes(remote):  # ssh host <<EOF … is checked with the heredocs below
                kinds.add("push")
            continue
        bare = seg if INTERPRETER.search(seg) else QUOTED.sub("''", seg)
        if PUSH_RULE.search(bare) or DEPLOY_SCRIPT.search(os.path.basename(script_of(tokens)) or "") \
                or DEPLOY_SCRIPT.search(os.path.dirname(script_of(tokens)) or ""):
            kinds.add("push")
        if COMMIT_RULE.search(bare):
            kinds.add("commit")
        if os.path.basename(tokens[0]) in SHELLS:  # bash -c '…', eval "…", xargs …
            for q in QUOTED.findall(seg):
                kinds |= outward_kinds(q[1:-1], depth + 1)
    for owner, body in heredocs:
        owner_segs = split_unquoted(owner.split("<<")[0])
        otok = strip_prefixes(tokenize(owner_segs[-1])) if owner_segs else []
        oremote = ssh_remote(otok)
        if oremote is not None:
            if _ssh_heredoc_writes(oremote, body):
                kinds.add("push")
        elif program(owner) in SHELLS:
            kinds |= outward_kinds(body, depth + 1)
    return kinds


def restriction(users: List[dict], cmd: str) -> Optional[Tuple[str, str]]:
    kinds = outward_kinds(cmd)
    if not kinds:
        return None
    bans = conv.active_bans(users)
    for kind, what, lift in (("push", "отправляет изменения наружу (push, деплой или загрузка на сервер)", "«пушь»"),
                             ("commit", "создаёт коммит", "«коммить»")):
        if kind in kinds and kind in bans:
            said = re.sub(r"\s+", " ", bans[kind]["text"]).strip()
            said = said if len(said) <= 160 else said[:159] + "…"
            return (f"user-said-no-{kind}",
                    f"в этом разговоре вы просили: «{said}» (сообщение {bans[kind]['n']}), а эта команда {what}. "
                    f"Подтвердите, если уже можно, — или напишите {lift}, и guard перестанет спрашивать.")
    return None


# Files and variables whose content must not be printed into the conversation.
SECRET_FILE = checks.SECRET_FILE
PRINTERS = {"cat", "less", "more", "head", "tail", "bat", "nl", "xxd", "strings", "base64", "od", "hexdump", "pbcopy",
            "tac", "sort", "uniq", "awk", "type", "gc", "get-content", "select-string", "sls", "format-hex", "clip",
            "set-clipboard"}
SECRET_VAR = re.compile(r"\$(\{|env:)?\w*(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY)\w*", I)
ENV_LISTERS = {"gci", "get-childitem", "dir", "ls", "get-item", "gi"}  # PowerShell: `gci env:` lists the environment
SECRET_HINT = ("Чтобы секрет не попал в разговор (его видят модель и логи), не выводите его. Имена переменных без значений: "
               "`sed 's/=.*/=***/' {f}`; проверить, что значение задано: `grep -c '^KEY=' {f}`; использовать в команде: "
               "`set -a; . {f}; set +a; <команда>`. Если пользователь сам хочет увидеть значение — предложите ему открыть "
               "файл у себя.")


ENV_DUMPS = (["env"], ["printenv"], ["set"], ["export"], ["export", "-p"], ["declare", "-x"])
# `${VAR:+word}` and `${VAR+word}` print word only when VAR is set (word may name a variable of its own: `${A:+$B}`),
# `${#VAR}` prints its length: none of them prints VAR's value.
VALUE_FREE = re.compile(r"\$\{(?:#\w+|\w+:?\+([^{}]*(?:\{[^{}]*\}[^{}]*)*))\}")


def _value_free(seg: str) -> str:
    """The segment with the expansions that never print a variable's value taken out (VALUE_FREE), innermost first."""
    while True:
        out = VALUE_FREE.sub(lambda m: m.group(1) or "", seg)
        if out == seg:
            return out
        seg = out


def names_only(stage: str) -> bool:
    """Does this pipeline stage keep only the names of the `NAME=value` lines it reads: `cut -d= -f1`, `awk -F=
    '{print $1}'`, `sed 's/=.*//'` (or `s/=.*/=***/`)? Any other option, field or file — it may print values."""
    tokens = tokenize(stage)
    prog, args = (os.path.basename(tokens[0]).lower(), tokens[1:]) if tokens else ("", [])
    opts: Dict[str, str] = {}
    script: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if prog in ("cut", "gcut") and a in ("-s", "--only-delimited"):
            i += 1
            continue
        if prog in ("cut", "gcut") and a in ("-d", "-f", "--delimiter", "--fields") and i + 1 < len(args):
            opts[a.lstrip("-")[0]], i = args[i + 1], i + 2
        elif prog in ("cut", "gcut") and a.startswith(("--delimiter=", "--fields=")):
            opts[a[2]], i = a.split("=", 1)[1], i + 1
        elif prog in ("cut", "gcut") and a.startswith(("-d", "-f")):  # -d= -f1: the value right after the letter
            opts[a[1]], i = a[2:], i + 1
        elif prog in ("awk", "gawk", "mawk", "nawk") and a == "-F" and i + 1 < len(args):
            opts["d"], i = args[i + 1], i + 2
        elif prog in ("awk", "gawk", "mawk", "nawk") and a.startswith("-F"):
            opts["d"], i = a[2:], i + 1
        elif prog in ("sed", "gsed") and a in ("-E", "-r", "--regexp-extended"):
            i += 1
        elif prog in ("sed", "gsed") and a in ("-e", "--expression") and i + 1 < len(args):
            script, i = script + [args[i + 1]], i + 2
        elif prog in ("awk", "gawk", "mawk", "nawk", "sed", "gsed") and not a.startswith("-") and not script:
            script, i = [a], i + 1
        else:
            return False
    if prog in ("cut", "gcut"):
        return opts.get("d") == "=" and opts.get("f") == "1"
    if prog in ("awk", "gawk", "mawk", "nawk"):
        return opts.get("d") == "=" and bool(re.fullmatch(r"\{\s*print\s+\$1\s*;?\s*\}", "".join(script)))
    return prog in ("sed", "gsed") and len(script) == 1 \
        and bool(re.fullmatch(r"s(.)=\.\*\$?\1(?:(?!\1)[^&\\])*\1g?", script[0]))  # no & or \1: no value comes back


def secret_print(cmd: str) -> Optional[Tuple[str, str]]:
    """A command that would print secrets: cat .env, a private key, the whole environment (not when piped straight
    into a filter that keeps only the names: names_only), echo $TOKEN (not `${TOKEN:+set}`: _value_free)."""
    text = re.sub(r"\\\r?\n", " ", cmd.replace("\\", "/") if compat.WINDOWS else cmd)
    parts = [(s.strip(), op) for s, op in _split(text)]  # each simple command with the operator after it
    for k, (seg, op) in enumerate(parts):
        if not seg:
            continue
        raw = [t for t in tokenize(seg) if t not in ("sudo", "command", "builtin")]
        low = [t.lower() for t in raw]
        names = op in ("|", "|&") and k + 1 < len(parts) and names_only(parts[k + 1][0])  # printenv | cut -d= -f1
        if raw in ENV_DUMPS and not names \
                or (len(low) == 2 and low[0] in ENV_LISTERS and low[1].rstrip("/*") in ("env:", "env:\\")) \
                or "[environment]::getenvironmentvariables" in seg.lower():
            return "secret-print", ("команда выведет все переменные окружения, а среди них бывают токены и пароли. "
                                    "Нужна одна переменная — `printenv NAME`; проверить, что она задана — "
                                    "`test -n \"$NAME\"`; только имена — `printenv | cut -d= -f1`.")
        tokens = strip_prefixes(tokenize(seg))
        if not tokens:
            continue
        prog = os.path.basename(tokens[0]).lower()
        files = [t for t in tokens[1:] if not t.startswith("-") and SECRET_FILE.search(t)]
        if prog in PRINTERS and files:
            return "secret-print", f"команда выведет секреты из {files[0]}. " + SECRET_HINT.format(f=files[0])
        if (prog in ("echo", "printf", "write-output", "write-host") and SECRET_VAR.search(_value_free(seg))) \
                or re.fullmatch(r"\s*\$env:\w*(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY)\w*\s*", seg, I):
            return "secret-print", ("команда выведет значение секретной переменной. Проверить, что оно задано: "
                                    "`test -n \"$NAME\" && echo задано` (или `${NAME:+задано}`).")
    return None


def analyze_path(path: str) -> Optional[Tuple[str, str]]:
    """A file write's stop: startup files, SSH keys and /etc ("protected-file"), the agent's own permissions
    ("agent-settings"), the system's folders ("system-files"), git's internals ("git-internals": the project's)."""
    raw = compat.slashes(os.path.expanduser(path or ""))
    if not raw:
        return None
    norm = _norm_guard_path(raw) if (raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw)) else posixpath.normpath(raw)
    cands = [norm]
    if (norm.startswith("/") and os.name != "nt") or (os.name == "nt" and re.match(r"^[A-Za-z]:/", norm)):
        try:
            rp = compat.slashes(os.path.realpath(norm))
            if rp.startswith("/System/Volumes/Data/"):
                rp = rp[len("/System/Volumes/Data"):]
            if rp and rp not in cands:
                cands.append(rp)
        except OSError:
            pass
    for p in cands:
        if PROTECTED_FILE.search(p):
            return "protected-file", f"запись в служебный/системный файл `{path}` (автозапуск, SSH-ключи, профиль оболочки, /etc)."
        if AGENT_CONTROL.search(p):
            return "agent-settings", (f"запись в `{path}`: эти настройки решают, что агенту разрешено (разрешения "
                                      "Antigravity, MCP-серверы, плагины, настройки Antigravity Plus).")
        if SCHEDULED_SIDECAR.search(p):
            return "scheduled-sidecar", f"запись в `{path}`: sidecar-процессы и расписания Antigravity запускаются сами."
        if SYSTEM_DIR.search(p):
            return "system-files", f"запись в системную папку: `{path}`."
        if GIT_INTERNALS.search(p):
            return "git-internals", f"запись во внутренние файлы git `{path}` — так можно испортить репозиторий."
    return None


def unwrap(v) -> str:
    """Tool args may arrive JSON-encoded twice ("\"ls -la\"")."""
    if isinstance(v, str):
        s = v.strip()
        if len(s) >= 2 and s[0] == s[-1] == '"':
            try:
                d = json.loads(s)
                if isinstance(d, str):
                    return d
            except ValueError:
                pass
        return v
    return "" if v is None else str(v)


def log(payload: dict, tool: str, rid: str, subject: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "conversation": payload.get("conversationId", ""),
               "tool": tool, "rule": rid, "subject": subject[:300]}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(LOG) > 256_000:  # keep it small
            with open(LOG, encoding="utf-8") as f:
                tail = f.readlines()[-300:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except OSError:
        pass


def protect_secrets() -> bool:
    try:
        return bool(memstore.settings().get("protectSecrets", True)) if memstore else True
    except Exception:
        return True


def git_checks() -> bool:
    try:
        return bool(memstore.settings().get("gitChecks", True)) if memstore else True
    except Exception:
        return True


def memory_elsewhere(payload: dict, path: str, content: str) -> Optional[str]:
    """A new file in the memory format (frontmatter with name, description and metadata.type) outside the memory
    folders: no later conversation would ever see it. The reason names the exact folder to write it to."""
    if not memstore or not content or not path or os.path.exists(path):
        return None
    meta, _body = memstore.parse_frontmatter(content[:4000])
    if meta.get("type") not in memstore.TYPES or not meta.get("name") or not meta.get("description"):
        return None
    ws = next((conv.to_path(w) for w in payload.get("workspacePaths") or [] if isinstance(w, str) and w), "")
    personal, project = memstore.PERSONAL, (memstore.project_dir(ws)[0] if ws else "")
    first, other = ((personal, project) if meta["type"] in ("user", "feedback") or not project else (project, personal))
    name = os.path.basename(path)
    alt = ""
    if other:
        when = "о пользователе и его работе" if other == personal else "только об этом проекте"
        alt = f" (или в \"{os.path.join(other, name)}\", если она {when})"
    return ("🛡️ Antigravity Plus: это запись памяти, но путь не в папке памяти — её не увидит ни один следующий "
            f"разговор. Запишите тот же текст сюда: write_file \"{os.path.join(first, name)}\"{alt}.")


def decide(payload: dict) -> dict:
    call = payload.get("toolCall") or {}
    tool = call.get("name") or ""
    args = call.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    hit, subject = None, ""
    rewritten = None
    st = conv.load(payload)
    bypass = bypass_on(payload, st)
    full = lambda: conv.full_access(payload, st) is not None  # noqa: E731 — only computed when nothing else decided
    if tool == "exit_plan_mode":
        plan = unwrap(args.get("plan") or args.get("Plan") or "")
        check = orchestrator.check_plan(payload, st, conv.with_current(st, payload), {**args, "plan": plan}) if orchestrator else None
        if check:
            rid, reason = check[0], check[1]
            log(payload, tool, rid, plan[:300])
            # A step the user banned is the user's call (they may have asked for it since): ask, never deny — a denied
            # plan can only be re-planned, again and again. A missing check is the planner's to fix (once: orchestrator).
            decision = "force_ask" if rid == "orchestrator-ban-in-plan" else "deny"
            return {"decision": decision, "reason": f"🛡️ Antigravity Plus: {reason}"}
        return PASS
    if tool == "read_file":
        subject = unwrap(args.get("Path") or args.get("path") or "")
        if protect_secrets() and SECRET_FILE.search(compat.slashes(subject)) and not subject.endswith(".pub"):
            log(payload, tool, "secret-read", subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: в {os.path.basename(subject)} секреты. "
                                                  + SECRET_HINT.format(f=subject)}
        return AUTO if bypass else PASS
    if tool == "run_command":
        subject = unwrap(args.get("CommandLine") or args.get("command") or "")
        if talks_to_claude(subject):
            return AUTO  # a delegated agent's question or message for Claude Code: it only prints a line
        rewritten = gitcheck.rewrite(subject) if gitcheck and git_checks() else None
        effective = rewritten[1] if rewritten else subject
        stall = gitcheck.interactive(effective) if gitcheck and git_checks() else None
        if stall:  # it would wait at the keyboard (or fail on GIT_EDITOR=false): refuse with the form that works
            log(payload, tool, stall[0], subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: {stall[1]}"}
        hit = analyze_command(effective, system_only=bypass)
        if bypass and not hit:  # what Bypass Mode lets through still goes to the log
            skipped = analyze_command(effective)
            if skipped:
                log(payload, tool, "bypass:" + skipped[0], subject)
        secret = secret_print(effective) if not hit and protect_secrets() else None
        if secret:  # refuse rather than ask: the agent can do the same without printing the value
            log(payload, tool, secret[0], subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: {secret[1]}"}
        fake = facts.fake_check(effective) if not hit and facts else None
        if fake:  # `echo "tests passed"` checks nothing: refuse, the agent runs the real check
            log(payload, tool, "fake-check", subject)
            return {"decision": "deny", "reason": fake}
        if not hit and outward_kinds(effective):
            hit = restriction(conv.with_current(st, payload), effective)
    elif tool in ("write_file", "edit_file"):
        subject = unwrap(args.get("Path") or args.get("TargetFile") or args.get("path") or "")
        if memstore and memstore.memory_dir_of(subject):
            body = unwrap(args.get("Content") if tool == "write_file" else args.get("Replacement"))
            found = checks.find_secrets(body) if body and protect_secrets() else []
            if found:  # memory goes into every later conversation: refuse, the agent rewrites it without the value
                log(payload, tool, "memory-secret", subject)
                return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: в записи памяти похоже на секрет ({found[0][1]}, "
                                                      f"строка {found[0][0]}). Память попадает в каждый следующий разговор — "
                                                      "секретам там не место. Запишите, где он хранится (например «пароль "
                                                      "панели — ADMIN_PASSWORD в /opt/app/.env»), без самого значения."}
            return AUTO
        misplaced = memory_elsewhere(payload, subject, unwrap(args.get("Content"))) if tool == "write_file" else None
        if misplaced:  # a memory written into the project (weak models shorten the long path): say where it goes
            log(payload, tool, "memory-elsewhere", subject)
            return {"decision": "deny", "reason": misplaced}
        target_text = (unwrap(args.get("Target") or args.get("TargetContent"))
                       if tool == "edit_file" and ("Target" in args or "TargetContent" in args) else None)
        stale = coord.check_write(
            payload, subject,
            unwrap(args.get("Content")) if tool == "write_file" and "Content" in args else None,
            target=target_text,
        )
        if stale:  # refuse rather than ask: the agent re-reads the file and retries on its own
            log(payload, tool, "stale-write", subject)
            return {"decision": "deny", "reason": stale}
        hit = analyze_path(subject)
        if hit and bypass and hit[0] not in SYSTEM_RULES:
            log(payload, tool, "bypass:" + hit[0], subject)
            hit = None
    if not hit:
        overwrite: Dict[str, object] = {}
        if tool == "run_command":
            effective = rewritten[1] if rewritten else subject
            if rewritten:
                key = "CommandLine" if ("CommandLine" in args or "command" not in args) else "command"
                overwrite[key] = effective
                log(payload, tool, rewritten[0], subject)
            if speed and speed.needs_bypass_sandbox(payload, effective, args):
                overwrite["BypassSandbox"] = True
                log(payload, tool, "overwrite:bypass-sandbox", subject)
        base = AUTO if tool in ("run_command", "write_file", "edit_file") and (bypass or full()) else PASS
        return {**base, "overwrite": overwrite} if overwrite else base
    rid, reason = hit
    log(payload, tool, rid, subject)
    shown = subject if len(subject) <= 160 else subject[:159] + "…"
    if not rid.startswith("user-said"):
        reason += " Подтвердите, если это действительно нужно."
    if bypass:
        reason += (" (Bypass Mode включён, но о системных действиях guard спрашивает всегда.)" if rid in SYSTEM_RULES
                   else " (Bypass Mode включён, но это ваш запрет из разговора.)")
    return {"decision": "force_ask", "reason": f"🛡️ Antigravity Plus: {reason} → {shown}"}


def bypass_on(payload: dict, st: Optional[dict]) -> bool:
    """Bypass Mode (hooks/conversation.py). A broken switch counts as off: it must never hide a stop."""
    try:
        return conv.bypass_mode(payload, st) is not None
    except Exception as e:
        print(f"guard: bypass: {e}", file=sys.stderr)
        return False


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        rest, said = sys.argv[2:], []
        while "--said" in rest:
            i = rest.index("--said")
            said.append(rest[i + 1] if i + 1 < len(rest) else "")
            rest = rest[:i] + rest[i + 2:]
        bypass = "--bypass" in rest
        rest = [r for r in rest if r != "--bypass"]
        cmd = " ".join(rest)
        users = [{"text": s} for s in said]
        path = analyze_path(cmd)
        verdict = (analyze_command(cmd, system_only=bypass)
                   or (path if path and (not bypass or path[0] in SYSTEM_RULES) else None) or restriction(users, cmd))
        if not verdict and memstore and memstore.memory_dir_of(cmd):
            verdict = ("memory-file", "auto_approve")
        print(json.dumps(verdict, ensure_ascii=False))
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = decide(payload if isinstance(payload, dict) else {})
    except Exception as e:  # a bug in the guard must not block the agent: fall back to the default policy
        print(f"guard: {e}", file=sys.stderr)
        out = PASS
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
