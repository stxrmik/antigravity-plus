#!/usr/bin/env python3
"""Antigravity Plus — PreToolUse guard.

Stops and asks the user (decision "force_ask", which ignores cached "always allow" grants) before:
  * run_command (sh/bash/zsh on macOS and Linux, PowerShell or cmd on Windows): irreversible or outward-facing
    commands — rm -rf / Remove-Item -Recurse -Force on /, ~, C:\, . or *, git reset --hard,
    git clean -f, force-push, --no-verify, DROP/TRUNCATE via a DB client, mkfs/dd to a device,
    curl | sh, sudo, terraform destroy, kubectl delete, npm publish, gh pr merge, keychain/SSH-key reads…
  * write_file / edit_file: writes into .git/ internals, shell startup files, SSH keys, LaunchAgents, /etc, and
    into what decides what the agent may do: Antigravity's settings and projects, MCP servers, installed plugins
    (this guard among them), Antigravity Plus's own settings; also connecting an MCP server (/settings) and turning
    secret protection off — the agent changes these only on the user's explicit request, and the user confirms;
  * write_file / edit_file over a file that changed since this agent last read or wrote it (hooks/coord.py) —
    refused ("deny") with who changed it, so the agent re-reads and retries without bothering the user;
  * push / deploy / upload to a server, or git commit — only when the user said not to in this conversation
    ("ничего не пушь", "don't commit") and hasn't lifted it since ("пушь"); see hooks/conversation.py.
Writes of memory files (skills/memory/scripts/memory.py) get "auto_approve", so auto-memory never stops for a
permission prompt. Under full access (one grant from the user, hooks/conversation.py) every command and edit not
listed above gets "auto_approve" too; the destructive ones and the user's explicit bans still stop.

Everything else gets "allow", which in Antigravity means "no objection — delegate to the next hook or the
default policy" (it is NOT auto-approval; that is "auto_approve"), so your permission settings apply unchanged.
An empty decision would be treated as deny, so every path below returns an explicit decision.
Matches are appended to ~/.gemini/antigravity-plus/guard.log (shown by /doctor).

    guard.py --test "<command>" [--said "<user message>" …]   # print the verdict for a command (no stdin)

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import sys
from typing import List, Optional, Set, Tuple

import checks
import compat
import conversation as conv
import coord

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

# Rules for the whole command line (they span pipes).
PIPE_RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("curl-pipe-shell", re.compile(r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da|fi|k)?sh\b|\b(ba|z)?sh\s+(-c\s+)?[\"']?\$\((curl|wget)\b|\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(python3?|node|ruby|perl)\b"),
     "скрипт из интернета выполняется без проверки (`curl | sh`)."),
    ("fork-bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "fork-бомба положит систему."),
    ("download-run", re.compile(  # PowerShell's `curl | sh`
        r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^;&]*\|\s*(iex|invoke-expression)\b"
        r"|\b(iex|invoke-expression)\b[\s(]*\(?\s*(new-object\s+(system\.)?net\.webclient|iwr|irm|invoke-webrequest|"
        r"invoke-restmethod)\b", re.I),
     "команда скачивает код из интернета и сразу его выполняет."),
]

# Programs whose arguments are just text (a commit message or a search pattern can mention `rm -rf /`).
TEXT_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "git-grep", ":", "true", "test", "["}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh", "eval", "xargs", "su", "sudo", "doas",
          "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "iex", "invoke-expression"}
# When the command hands text to another interpreter, quoted text is code, not data.
INTERPRETER = re.compile(r"\b((ba|z|da|k)?sh|fish)\s+(-\w+\s+)*-c\b|\beval\b|\bxargs\b|\bssh\b|\bwatch\b|\bdocker\s+(exec|run)\b|\bkubectl\s+exec\b"
                         r"|\b(powershell|pwsh)(\.exe)?\s+(-\w+\s+)*-c(ommand)?\b|\bcmd(\.exe)?\s+/[ck]\b|\b(iex|invoke-expression)\b", I)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)

DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", ".", "./", "./*", "..", "../", "../*", "*", ".*",
                        "$HOME", "${HOME}", "$HOME/", "${HOME}/", "$HOME/*", "${HOME}/*", "/Users", "/home"}
TOP_LEVEL = re.compile(r"^/(Applications|Library|System|Users|Volumes|bin|cores|dev|etc|home|opt|private|sbin|tmp|usr|var)/?\*?$")
VAR_ROOT = re.compile(r"^\$\{?\w+\}?/")  # "$DIR/…" — deletes from / when the variable is empty

PROTECTED_FILE = re.compile(
    r"(^|/)\.git/(?!COMMIT_EDITMSG$)|(^|/)\.(zshrc|zprofile|zshenv|bashrc|bash_profile|profile)$"
    r"|/\.ssh/(?!(config|known_hosts)$)|/Library/LaunchAgents/|/Library/LaunchDaemons/|^/etc/|^/private/etc/"
    r"|/\.config/(autostart|systemd/user)/|(^|/)(Microsoft\.PowerShell_|Microsoft\.VSCode_)?profile\.ps1$"
    r"|/Start Menu/Programs/Startup/|^[A-Za-z]:/Windows/", re.I)
# What decides what the agent may do: Antigravity's settings and projects (permission grants), MCP servers,
# installed plugins (this guard among them) and Antigravity Plus's settings (full access, secret protection).
AGENT_CONTROL = re.compile(r"/\.gemini/config/(config\.json|mcp_config\.json|projects/|plugins/)"
                           r"|/\.gemini/antigravity/mcp_config\.json$|/\.gemini/antigravity-plus/settings\.json$")


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


def check_rm(segment: str) -> Optional[str]:
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
        return "`rm --no-preserve-root` может удалить всю систему."
    if not re.search(r"[rR]", flags):
        return None
    for t in targets:  # shlex keeps ~ and $VAR literal, which is what we want to inspect
        if t in DANGEROUS_RM_TARGETS or t.rstrip("/") in DANGEROUS_RM_TARGETS or TOP_LEVEL.match(t):
            return f"`rm -r {t}` удалит всё содержимое `{t}` без корзины."
        if VAR_ROOT.match(t) and not re.match(r"^\$\{?HOME\}?/", t):
            return f"`rm -r {t}`: если переменная пустая, удаление пойдёт от корня `/`."
    return None


WIN_RM = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "rmdir.exe"}
WIN_BROAD = re.compile(r"^([a-z]:)?[\\/]?\*?$|^[a-z]:[\\/](users|windows|program files( \(x86\))?)[\\/]?\*?$"
                       r"|^(~|\$home|\$env:(userprofile|homepath|systemdrive|systemroot)|%userprofile%|%systemdrive%)[\\/]?\*?$"
                       r"|^\.{1,2}[\\/]?\*?$", re.I)


def check_win_rm(segment: str) -> Optional[str]:
    """PowerShell `Remove-Item -Recurse -Force …` / `rm -r -fo …` and cmd `rd /s /q …` / `del /s /q …` on a whole
    drive, the user folder or the current folder."""
    tokens = tokenize(segment)
    if not tokens or os.path.basename(tokens[0]).lower() not in WIN_RM:
        return None
    flags = [t.lower() for t in tokens[1:] if t.startswith(("-", "/")) and not WIN_BROAD.match(t)]
    recursive = any(f in ("-r", "-recurse", "/s") or f.startswith("-rec") for f in flags)
    if not recursive:
        return None
    for t in tokens[1:]:
        if t.lower() in ("-path", "-literalpath"):
            continue
        if (not t.startswith(("-", "/")) or WIN_BROAD.match(t)) and WIN_BROAD.match(t.strip("'\"")):
            return f"`{tokens[0]} {t}` рекурсивно удалит всё содержимое `{t}` без корзины."
    return None


SQL_REASON = "SQL-команда удаляет таблицы или данные (DROP/TRUNCATE/DELETE без WHERE)."
SQL_IN_EXEC = re.compile(r"\b(psql|mysql|mariadb|sqlite3|mongosh?|clickhouse-client)\b")


def analyze_command(cmd: str, depth: int = 0) -> Optional[Tuple[str, str]]:
    if not cmd or not cmd.strip() or depth > 2:
        return None
    if compat.WINDOWS:  # PowerShell/cmd: backslashes are path separators, also inside quotes
        cmd = cmd.replace("\\", "/")
    cmd = re.sub(r"\\\r?\n", " ", cmd)
    cmd, heredocs = split_heredocs(cmd)
    # Quoted text is data (commit messages, search patterns) unless it is handed to an interpreter.
    text = cmd if INTERPRETER.search(cmd) else QUOTED.sub("''", cmd)
    for rid, rx, reason in PIPE_RULES:
        if rx.search(text):
            return rid, reason
    for seg in split_segments(cmd):
        prog = program(seg)
        if prog in TEXT_PROGRAMS:
            continue
        if (prog in SQL_PROGRAMS or prog in ("docker", "kubectl", "ssh") and SQL_IN_EXEC.search(seg)) \
                and SQL_DESTRUCTIVE.search(seg):
            return "sql-destructive", SQL_REASON
        r = check_rm(seg) or check_win_rm(seg)
        if r:
            return "rm-dangerous", r
        target = seg if INTERPRETER.search(seg) else QUOTED.sub("''", seg)
        for rid, rx, reason in SEGMENT_RULES:
            if rx.search(target):
                return rid, reason
        if prog in SHELLS:  # bash -c '…', ssh host '…', eval "…": the quoted text is a command
            for q in QUOTED.findall(seg):
                hit = analyze_command(q[1:-1], depth + 1)
                if hit:
                    return hit
    for owner, body in heredocs:  # bodies fed to a shell or a DB client are code
        prog = program(owner)
        if prog in SQL_PROGRAMS and SQL_DESTRUCTIVE.search(body):
            return "sql-destructive", SQL_REASON
        if prog in SHELLS:
            hit = analyze_command(body, depth + 1)
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
    p = compat.slashes(os.path.expanduser(path or ""))
    if p and PROTECTED_FILE.search(p):
        return "protected-file", f"запись в служебный/системный файл `{path}` (git-внутренности, автозапуск, SSH, /etc)."
    if p and AGENT_CONTROL.search(p):
        return "agent-settings", (f"запись в `{path}`: эти настройки решают, что агенту разрешено (разрешения "
                                  "Antigravity, MCP-серверы, плагины, настройки Antigravity Plus).")
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
    full = lambda: conv.full_access(payload, st) is not None  # noqa: E731 — only computed when nothing else decided
    if tool == "read_file":
        subject = unwrap(args.get("Path") or args.get("path") or "")
        if protect_secrets() and SECRET_FILE.search(compat.slashes(subject)) and not subject.endswith(".pub"):
            log(payload, tool, "secret-read", subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: в {os.path.basename(subject)} секреты. "
                                                  + SECRET_HINT.format(f=subject)}
        return PASS
    if tool == "run_command":
        subject = unwrap(args.get("CommandLine") or args.get("command") or "")
        hit = analyze_command(subject)
        secret = secret_print(subject) if not hit and protect_secrets() else None
        if secret:  # refuse rather than ask: the agent can do the same without printing the value
            log(payload, tool, secret[0], subject)
            return {"decision": "deny", "reason": f"🛡️ Antigravity Plus: {secret[1]}"}
        if not hit and outward_kinds(subject):
            hit = restriction(conv.with_current(st, payload), subject)
    elif tool in ("write_file", "edit_file"):
        subject = unwrap(args.get("Path") or args.get("TargetFile") or args.get("path") or "")
        if memstore and memstore.memory_dir_of(subject):
            return AUTO
        stale = coord.check_write(payload, subject, unwrap(args.get("Content")) if tool == "write_file" and "Content" in args else None)
        if stale:  # refuse rather than ask: the agent re-reads the file and retries on its own
            log(payload, tool, "stale-write", subject)
            return {"decision": "deny", "reason": stale}
        hit = analyze_path(subject)
    if not hit:
        return AUTO if tool in ("run_command", "write_file", "edit_file") and full() else PASS
    rid, reason = hit
    log(payload, tool, rid, subject)
    shown = subject if len(subject) <= 160 else subject[:159] + "…"
    if not rid.startswith("user-said"):
        reason += " Подтвердите, если это действительно нужно."
    return {"decision": "force_ask", "reason": f"🛡️ Antigravity Plus: {reason} → {shown}"}


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        rest, said = sys.argv[2:], []
        while "--said" in rest:
            i = rest.index("--said")
            said.append(rest[i + 1] if i + 1 < len(rest) else "")
            rest = rest[:i] + rest[i + 2:]
        cmd = " ".join(rest)
        users = [{"text": s} for s in said]
        verdict = analyze_command(cmd) or analyze_path(cmd) or restriction(users, cmd)
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
