#!/usr/bin/env python3
r"""Antigravity Plus — PreToolUse guard.

Stops and asks the user (decision "force_ask", which ignores cached "always allow" grants) before:
  * run_command (sh/bash/zsh on macOS and Linux, PowerShell or cmd on Windows): irreversible or outward-facing
    commands — rm -rf / Remove-Item -Recurse -Force on /, ~, C:\, . or *, git reset --hard,
    git clean -f, force-push, --no-verify, DROP/TRUNCATE via a DB client, mkfs/dd to a device,
    curl | sh, sudo, terraform destroy, kubectl delete, npm publish, gh pr merge, keychain/SSH-key reads,
    changes to system folders (/System, /usr, /etc, /Library, C:\Windows…) by rm/mv/cp/chmod/tee/sed -i/redirection…
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
import sys
from typing import Dict, List, Optional, Set, Tuple

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

# (rule id, regex, reason). Regexes run on each shell segment (split on ; && || | newlines),
# except PIPE_RULES which run on the whole command line.
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

# Programs whose arguments are just text (a commit message or a search pattern can mention `rm -rf /`).
TEXT_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "git-grep", ":", "true", "test", "["}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh", "eval", "xargs", "su", "sudo", "doas",
          "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "iex", "invoke-expression"}
# When the command hands text to another interpreter, quoted text is code, not data.
INTERPRETER = re.compile(r"\b((ba|z|da|k)?sh|fish)\s+(-\w+\s+)*-c\b|\beval\b|\bxargs\b|\bssh\b|\bwatch\b|\bdocker\s+(exec|run)\b|\bkubectl\s+exec\b"
                         r"|\b(powershell|pwsh)(\.exe)?\s+(-\w+\s+)*-c(ommand)?\b|\bcmd(\.exe)?\s+/[ck]\b|\b(iex|invoke-expression)\b", I)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)

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


def split_segments(cmd: str) -> List[str]:
    parts = re.split(r"\|\||&&|;|\||\r?\n|(?<![<>&])&(?![>&])", cmd)
    return [p.strip() for p in parts if p.strip()]


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


def split_heredocs(cmd: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Remove heredoc bodies from the command; return (command, [(owner line, body), …])."""
    docs: List[Tuple[str, str]] = []

    def repl(m: "re.Match[str]") -> str:
        start = cmd.rfind("\n", 0, m.start()) + 1
        rest = m.group(3)
        docs.append((cmd[start:m.start()] + rest, m.group(4)))
        return "<<" + m.group(2) + rest + "\n"

    return HEREDOC.sub(repl, cmd), docs


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


ENV_VAL_FLAGS = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}


def strip_prefixes(tokens: List[str]) -> List[str]:
    while tokens:
        t = tokens[0]
        if re.match(r"^\w+=", t) or t in ("builtin", "noglob"):
            tokens = tokens[1:]
        elif t == "env":
            tokens = tokens[1:]
            while tokens:
                if tokens[0] in ENV_VAL_FLAGS and len(tokens) > 1:
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
    folder = None
    for t in targets:  # shlex keeps ~ and $VAR literal, which is what we want to inspect
        t = with_known(t, known)
        if t in DANGEROUS_RM_TARGETS or t.rstrip("/") in DANGEROUS_RM_TARGETS or TOP_LEVEL.match(t) or rm_reaches_up(t, known):
            return "rm-dangerous", f"`rm -r {t}` удалит всё содержимое `{t}` без корзины."
        if t in FOLDER_RM_TARGETS or t.rstrip("/") in FOLDER_RM_TARGETS:
            folder = folder or ("rm-folder", f"`rm -r {t}` удалит всё содержимое текущей папки без корзины.")
        elif VAR_ROOT.match(t) and not re.match(r"^\$\{?HOME\}?/", t):
            return "rm-dangerous", f"`rm -r {t}`: если переменная пустая, удаление пойдёт от корня `/`."
    return folder


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
    return ([expand_path(p, known) for p in paths if p and p != "-"], [expand_path(p, known) for p in deep])


def check_writes(segment: str, known: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """A command that changes the agent's own permissions ("agent-settings"), files that start programs or hold keys —
    shell startup files, SSH keys, LaunchAgents ("protected-file") — or the system's own folders ("system-files")."""
    paths, deep = changed_paths(segment, known)
    perms_only = program(segment).lower() in RECURSIVE_OWNERS | {"chattr"}  # chmod 600 ~/.ssh/id_x is a fix, not a risk
    for p in paths:
        if AGENT_CONTROL.search(p) or AGENT_CONTROL_DIR.search(p):
            return "agent-settings", (f"команда меняет `{p}`: эти файлы решают, что агенту разрешено (разрешения "
                                      "Antigravity, MCP-серверы, плагины, настройки Antigravity Plus).")
        if SCHEDULED_SIDECAR.search(p) and not perms_only:
            return "scheduled-sidecar", f"команда меняет `{p}`: sidecar-процессы и расписания Antigravity запускаются сами."
        if PROTECTED_FILE.search(p) and not perms_only:
            return "protected-file", f"команда меняет служебный файл `{p}` (автозапуск, SSH-ключи, профиль оболочки, /etc)."
        if SYSTEM_DIR.search(p):
            return "system-files", f"команда меняет системные файлы: `{p}`."
    for p in deep:
        if p in ("/", "/*") or TOP_LEVEL.match(p) or p.rstrip("/*") == HOME.rstrip("/"):
            return "system-files", f"команда рекурсивно меняет права или владельца всего `{p}`."
    return None


def pipelines(cmd: str) -> List[List[str]]:
    """The command line's pipelines, each a list of its stages (`a | b`); separators inside quotes don't count."""
    out: List[List[str]] = [[]]
    buf: List[str] = []
    q, i, n = "", 0, len(cmd)

    def cut(new_pipeline: bool) -> None:
        out[-1].append("".join(buf).strip())
        buf.clear()
        if new_pipeline:
            out.append([])

    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = ""
        elif c in "'\"":
            q = c
            buf.append(c)
        elif c == "\\" and i + 1 < n:
            buf += [c, cmd[i + 1]]
            i += 1
        elif c in "|&" and i + 1 < n and cmd[i + 1] == c:  # || and &&
            cut(True)
            i += 1
        elif c == "|":
            cut(False)
        elif c in ";\n" or (c == "&" and (i == 0 or cmd[i - 1] not in "<>") and (i + 1 == n or cmd[i + 1] != ">")):
            cut(True)
        else:
            buf.append(c)
        i += 1
    cut(False)
    return [[s for s in p if s] for p in out if any(p)]


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
    """Extract shell substitution bodies (`$(...)`, `<(...)`, `>(...)`, backticks) outside single quotes."""
    out: List[str] = []
    q, i, n = "", 0, len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                q = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if q == '"':
            if c == '"':
                q = ""
                i += 1
                continue
        elif c in "'\"":
            q = c
            i += 1
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                elif s[j] == "`":
                    break
                else:
                    j += 1
            if j < n and j > i + 1:
                out.append(s[i + 1:j])
            i = j + 1 if j < n else n
            continue
        if s[i:i + 2] in ("$(", "<(", ">(") and s[i:i + 3] != "$((" :
            j, depth, sub_q = i + 2, 1, ""
            while j < n and depth > 0:
                sc = s[j]
                if sub_q == "'":
                    if sc == "'":
                        sub_q = ""
                elif sc == "\\" and j + 1 < n:
                    j += 1
                elif sub_q == '"':
                    if sc == '"':
                        sub_q = ""
                elif sc in "'\"":
                    sub_q = sc
                elif sc == "(":
                    depth += 1
                elif sc == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0 and j > i + 2:
                out.append(s[i + 2:j])
            i = j + 1 if depth == 0 else n
            continue
        i += 1
    return out


def analyze_command(cmd: str, depth: int = 0, system_only: bool = False) -> Optional[Tuple[str, str]]:
    """The command's first stop as (rule id, reason), or None. system_only (Bypass Mode): only the stops that hold
    there (SYSTEM_RULES), looked for in the whole command — past any project-level match."""
    if not cmd or not cmd.strip() or depth > 2:
        return None
    keep = (lambda rid: rid in SYSTEM_RULES) if system_only else (lambda rid: True)  # noqa: E731
    if compat.WINDOWS:  # PowerShell/cmd: backslashes are path separators, also inside quotes
        cmd = cmd.replace("\\", "/")
    cmd = re.sub(r"\\\r?\n", " ", cmd)
    if SELF_SWITCH.search(cmd):
        return "agent-settings", SELF_SWITCH_REASON
    cmd, heredocs = split_heredocs(cmd)
    # Quoted text is data (commit messages, search patterns) unless it is handed to an interpreter.
    text = cmd if INTERPRETER.search(cmd) else QUOTED.sub("''", cmd)
    for rid, rx, reason in PIPE_RULES:
        if keep(rid) and rx.search(text):
            return rid, reason
    if keep("curl-pipe-shell") and pipe_runs_download(cmd):
        return "curl-pipe-shell", CURL_REASON
    known: Dict[str, str] = {}
    for seg in split_segments(cmd):
        for sub in _substitutions(seg):
            hit = analyze_command(sub, depth + 1, system_only)
            if hit:
                return hit
        note_assignments(seg, known)
        prog = program(seg)
        if prog in TEXT_PROGRAMS:  # its arguments are text, but `echo … >> ~/.zshrc` still writes a file
            w = check_writes(seg, known)
            if w:
                return w
            continue
        if keep("sql-destructive") and (prog in SQL_PROGRAMS or prog in ("docker", "kubectl", "ssh")
                                         and SQL_IN_EXEC.search(seg)) and SQL_DESTRUCTIVE.search(seg):
            return "sql-destructive", SQL_REASON
        r = check_rm(seg, known) or check_win_rm(seg)
        if r and keep(r[0]):
            return r
        target = seg if INTERPRETER.search(seg) else QUOTED.sub("''", seg)
        for rid, rx, reason in SEGMENT_RULES:
            if keep(rid) and rx.search(target):
                return rid, reason
        if keep("agent-settings") and prog.lower() not in ("git", "git.exe") and _sidecar_api_hit(seg):
            return "agent-settings", SIDECAR_API_REASON
        if keep("scheduled-sidecar") and schedule_change(seg):
            return "scheduled-sidecar", SCHEDULE_REASON
        w = check_writes(seg, known)
        if w:
            return w
        if prog in SHELLS:  # bash -c '…', ssh host '…', eval "…": the quoted text is a command
            for q in QUOTED.findall(seg):
                hit = analyze_command(q[1:-1], depth + 1, system_only)
                if hit:
                    return hit
    for owner, body in heredocs:  # bodies fed to a shell or a DB client are code
        prog = program(owner)
        if keep("sql-destructive") and prog in SQL_PROGRAMS and SQL_DESTRUCTIVE.search(body):
            return "sql-destructive", SQL_REASON
        if prog in SHELLS:
            hit = analyze_command(body, depth + 1, system_only)
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


def split_unquoted(cmd: str) -> List[str]:
    """Like split_segments, but separators inside quotes don't count (ssh host 'a && b' stays one segment)."""
    parts: List[str] = []
    buf: List[str] = []
    q, i, n = "", 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = ""
        elif c in "'\"":
            q = c
            buf.append(c)
        elif c == "\\" and i + 1 < n:
            buf += [c, cmd[i + 1]]
            i += 1
        elif c in ";|\n" or (c == "&" and (i == 0 or cmd[i - 1] not in "<>") and (i + 1 == n or cmd[i + 1] != ">")):
            if c in "|&" and i + 1 < n and cmd[i + 1] == c:
                i += 1
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


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


def secret_print(cmd: str) -> Optional[Tuple[str, str]]:
    """A command that would print secrets: cat .env, a private key, the whole environment, echo $TOKEN."""
    for seg in split_unquoted(re.sub(r"\\\r?\n", " ", cmd.replace("\\", "/") if compat.WINDOWS else cmd)):
        raw = [t for t in tokenize(seg) if t not in ("sudo", "command", "builtin")]
        low = [t.lower() for t in raw]
        if raw in (["env"], ["printenv"], ["set"], ["export"], ["export", "-p"], ["declare", "-x"]) \
                or (len(low) == 2 and low[0] in ENV_LISTERS and low[1].rstrip("/*") in ("env:", "env:\\")) \
                or "[environment]::getenvironmentvariables" in seg.lower():
            return "secret-print", ("команда выведет все переменные окружения, а среди них бывают токены и пароли. "
                                    "Нужна одна переменная — `printenv NAME`; проверить, что она задана — `test -n \"$NAME\"`.")
        tokens = strip_prefixes(tokenize(seg))
        if not tokens:
            continue
        prog = os.path.basename(tokens[0]).lower()
        files = [t for t in tokens[1:] if not t.startswith("-") and SECRET_FILE.search(t)]
        if prog in PRINTERS and files:
            return "secret-print", f"команда выведет секреты из {files[0]}. " + SECRET_HINT.format(f=files[0])
        if (prog in ("echo", "printf", "write-output", "write-host") and SECRET_VAR.search(seg)) \
                or re.fullmatch(r"\s*\$env:\w*(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY)\w*\s*", seg, I):
            return "secret-print", ("команда выведет значение секретной переменной. Проверить, что оно задано: "
                                    "`test -n \"$NAME\" && echo задано`.")
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
