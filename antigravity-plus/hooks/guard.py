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

import datetime as dt
import json
import os
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
]

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
                "protection-off", "rm-dangerous", "system-files", "protected-file", "agent-settings"}

# Programs whose arguments are just text (a commit message or a search pattern can mention `rm -rf /`).
TEXT_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "git-grep", ":", "true", "test", "["}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh", "eval", "xargs", "su", "sudo", "doas",
          "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "iex", "invoke-expression"}
# When the command hands text to another interpreter, quoted text is code, not data.
INTERPRETER = re.compile(r"\b((ba|z|da|k)?sh|fish)\s+(-\w+\s+)*-c\b|\beval\b|\bxargs\b|\bssh\b|\bwatch\b|\bdocker\s+(exec|run)\b|\bkubectl\s+exec\b"
                         r"|\b(powershell|pwsh)(\.exe)?\s+(-\w+\s+)*-c(ommand)?\b|\bcmd(\.exe)?\s+/[ck]\b|\b(iex|invoke-expression)\b", I)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)

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
        docs.append((cmd[start:m.start()], m.group(3)))
        return "<<" + m.group(2) + "\n"

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


def strip_prefixes(tokens: List[str]) -> List[str]:
    while tokens:
        t = tokens[0]
        if re.match(r"^\w+=", t) or t in ("command", "exec", "time", "nohup", "env", "builtin", "noglob"):
            tokens = tokens[1:]
        elif t in ("sudo", "doas"):
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        else:
            break
    return tokens


def note_assignments(segment: str, known: Dict[str, str]) -> None:
    """A segment that only sets variables (`S=/path`, `export APP="…"`): remember the ones that can't be empty, so
    `S=/path && rm -rf $S/x` isn't taken for `rm -rf /x`. A value from `$(…)` or another variable is unknown."""
    tokens = tokenize(segment)
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
        if t in FOLDER_RM_TARGETS or t.rstrip("/") in FOLDER_RM_TARGETS:
            folder = folder or ("rm-folder", f"`rm -r {t}` удалит всё содержимое текущей папки без корзины.")
        elif t in DANGEROUS_RM_TARGETS or t.rstrip("/") in DANGEROUS_RM_TARGETS or TOP_LEVEL.match(t):
            return "rm-dangerous", f"`rm -r {t}` удалит всё содержимое `{t}` без корзины."
        elif VAR_ROOT.match(t) and not re.match(r"^\$\{?HOME\}?/", t):
            return "rm-dangerous", f"`rm -r {t}`: если переменная пустая, удаление пойдёт от корня `/`."
    return folder


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


def expand_path(p: str, known: Optional[Dict[str, str]] = None) -> str:
    """A path as a command writes it → absolute with forward slashes where it can be told: ~, $HOME, %USERPROFILE%,
    %WINDIR%, $env:ProgramFiles and variables set earlier in the command; anything else stays as it is."""
    p = with_known(p.strip().strip("'\"").replace("\\", "/"), known).strip("'\"")
    for rx, repl in WIN_ENV:
        p = rx.sub(repl, p, count=1)
    p = HOME_REF.sub(lambda _m: HOME, p, count=1)
    return re.sub(r"/{2,}", "/", p)


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
        elif prog in ("sed", "gsed", "perl") and any(a == "--in-place" or re.match(r"^-[a-zA-Z]*i", a) for a in args):
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
SCRIPT_RUNNERS = {"sh", "bash", "zsh", "dash", "python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx", "ts-node"}
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


def remote_writes(remote: str) -> bool:
    """Could this command, run on the server, change it? Only clearly read-only commands say no."""
    if not remote.strip():
        return True  # interactive shell on the server
    remote, docs = split_heredocs(re.sub(r"\\\r?\n", " ", remote))
    if docs:
        return True
    for seg in split_unquoted(remote):
        if REDIRECT.search(QUOTED.sub("''", seg)):
            return True
        tokens = strip_prefixes(tokenize(seg))
        if not tokens:
            continue
        prog, args = os.path.basename(tokens[0]), tokens[1:]
        if prog in READ_ONLY:
            if prog == "sed" and any(a.startswith("-i") for a in args):
                return True
            if prog in ("curl", "wget") and any(a.startswith(("-X", "--request", "-d", "--data", "-F", "--form", "-T",
                                                              "--upload", "-o", "-O", "--output", "--post")) for a in args):
                return True
            continue
        sub = next((a for a in args if not a.startswith("-") or prog == "crontab"), "")
        if prog == "docker" and sub == "compose":
            if set(args) & COMPOSE_READ and not set(args) & {"up", "down", "restart", "build", "pull", "rm", "stop",
                                                             "start", "exec", "run", "kill", "create", "push"}:
                continue
            return True
        if prog == "docker" and sub == "exec":
            rest = args[args.index("exec") + 1:]
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in ("-e", "--env", "-u", "--user", "-w", "--workdir") else rest[1:]
            if len(rest) >= 2 and os.path.basename(rest[1]) in READ_ONLY:
                continue
            return True
        if prog == "service" and "status" in args:
            continue
        if sub in READ_ONLY_SUB.get(prog, ()):
            continue
        return True
    return False


def script_of(tokens: List[str]) -> str:
    """Path of the script a command runs (./deploy/update.sh, bash deploy.sh, python3 release.py), or ""."""
    if not tokens:
        return ""
    if "/" in tokens[0] or tokens[0].endswith((".sh", ".py")):
        return tokens[0]
    if os.path.basename(tokens[0]) in SCRIPT_RUNNERS and len(tokens) > 1 and not tokens[1].startswith("-"):
        return tokens[1]
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
            if "<<" not in seg and remote_writes(remote):  # ssh host <<EOF … is checked with the heredocs below
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
        otok = strip_prefixes(tokenize(owner.split("<<")[0]))
        if ssh_remote(otok) is not None:
            if remote_writes(body):
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
    p = compat.slashes(os.path.expanduser(path or ""))
    if not p:
        return None
    if PROTECTED_FILE.search(p):
        return "protected-file", f"запись в служебный/системный файл `{path}` (автозапуск, SSH-ключи, профиль оболочки, /etc)."
    if AGENT_CONTROL.search(p):
        return "agent-settings", (f"запись в `{path}`: эти настройки решают, что агенту разрешено (разрешения "
                                  "Antigravity, MCP-серверы, плагины, настройки Antigravity Plus).")
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
    st = conv.load(payload)
    bypass = bypass_on(payload, st)
    full = lambda: conv.full_access(payload, st) is not None  # noqa: E731 — only computed when nothing else decided
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
        stall = gitcheck.interactive(subject) if gitcheck and git_checks() else None
        if stall:  # it would wait at the keyboard (or fail on GIT_EDITOR=false): refuse with the form that works
            log(payload, tool, stall[0], subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: {stall[1]}"}
        hit = analyze_command(subject, system_only=bypass)
        if bypass and not hit:  # what Bypass Mode lets through still goes to the log
            skipped = analyze_command(subject)
            if skipped:
                log(payload, tool, "bypass:" + skipped[0], subject)
        secret = secret_print(subject) if not hit and protect_secrets() else None
        if secret:  # refuse rather than ask: the agent can do the same without printing the value
            log(payload, tool, secret[0], subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: {secret[1]}"}
        fake = facts.fake_check(subject) if not hit and facts else None
        if fake:  # `echo "tests passed"` checks nothing: refuse, the agent runs the real check
            log(payload, tool, "fake-check", subject)
            return {"decision": "deny", "reason": fake}
        if not hit and outward_kinds(subject):
            hit = restriction(conv.with_current(st, payload), subject)
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
        stale = coord.check_write(payload, subject, unwrap(args.get("Content")) if tool == "write_file" and "Content" in args else None)
        if stale:  # refuse rather than ask: the agent re-reads the file and retries on its own
            log(payload, tool, "stale-write", subject)
            return {"decision": "deny", "reason": stale}
        hit = analyze_path(subject)
        if hit and bypass and hit[0] not in SYSTEM_RULES:
            log(payload, tool, "bypass:" + hit[0], subject)
            hit = None
    if not hit:
        return AUTO if tool in ("run_command", "write_file", "edit_file") and (bypass or full()) else PASS
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
