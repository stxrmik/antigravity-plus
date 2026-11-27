#!/usr/bin/env python3
"""Antigravity Plus installer for macOS, Linux and Windows.

  python3 install.py                       install or update into ~/.gemini/config/plugins/antigravity-plus
  python3 install.py --update              the newest release: `git pull --ff-only` here (or a download), then install
  python3 install.py --choose              pick the modules again (a menu with a detailed description of each)
  python3 install.py --list                describe every module and exit
  python3 install.py --modules core,git    exactly these modules (core is always in)
  python3 install.py --without mobile,web  the current choice minus these
  python3 install.py --all                 every module
  python3 install.py --no-claude           … without Claude Code's `antigravity` skill (= --without claude)
  python3 install.py --uninstall          remove the plugin (memory and settings are kept)
  python3 install.py --uninstall --purge  also remove its memory and settings (~/.claude memory is never touched)
  python3 install.py --plain               line-by-line output (what scripts, agents and CI get anyway)
  python3 install.py --demo                play the whole install with made-up data — changes nothing

A person at a full terminal gets the rich interface (installer_ui.py): the logo, what was found (Antigravity
Desktop, Antigravity CLI — the same plugin folder, no side panels there —, Claude Code), a module picker driven by
keys, live steps and a closing frame. Pipes, CI, agents' terminals, TERM=dumb and --plain get the plain lines
below, unchanged for update.py and scripts; NO_COLOR keeps the rich layout without colour.

Modules (modules.py): the first install in a terminal shows a menu; the choice is saved in
~/.gemini/antigravity-plus/modules.json and reused by every update, so `git pull && ./install.sh` asks nothing;
`--update` does that pull itself (only a fast-forward of a clean clone) and says what's new — the same as /update
in Antigravity (skills/update/scripts/update.py).
What isn't chosen isn't copied — its skills, subagents, rules and side panel never reach Antigravity's context —
and the hook features it owns stay off (settings forced in modules.json). No menu where nobody can answer: an agent's
terminal (TERM=dumb in Antigravity, CLAUDECODE in Claude Code), CI, or no terminal at all.

When Claude Code is on this computer (~/.claude exists), the module `claude` installs its side of the bridge: the
skill `antigravity` in ~/.claude/skills/antigravity, with which Claude Code hands sub-tasks to Antigravity agents.

On Windows run `install.cmd` (or `py -3 install.py`). Antigravity runs hook commands with `cmd /c` there, where
`python3` often doesn't exist, so the installer writes the Python command that works on this computer into the
installed hooks.json (and into the Claude Code skill). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from typing import Callable, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from modules import BY_ID, GROUP_OF, IDS, MODULES  # noqa: E402
import installer_ui as tui  # noqa: E402

SRC = os.path.join(HERE, "antigravity-plus")
DEST = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus")
DATA = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-plus")  # memory, settings, state, logs
CHOICE = os.path.join(DATA, "modules.json")                                   # the modules chosen (read by the hooks)
CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")                    # Claude Code's own folder
CLAUDE_SRC = os.path.join(HERE, "claude-code", "skills", "antigravity")
CLAUDE_DEST = os.path.join(CLAUDE, "skills", "antigravity")
SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}
WINDOWS = os.name == "nt"
WIDTH = 100

TEXT = {
    "ru": {
        "head": "Antigravity Plus — какие модули установить",
        "intro": "Модули добавляют агенту команды, субагентов и правила. Невыбранное не ставится и не занимает "
                 "контекст. Состав можно поменять когда угодно: ./install.sh --choose",
        "required": "обязательно",
        "tokens": "≈{n} токенов контекста в каждом запросе",
        "outside": "вне контекста Antigravity",
        "hints": "постоянного текста нет — только подсказки, когда нужны",
        "missing": "не найдено: {what}",
        "prompt": "Номера через пробел переключают модули (например «7 10»), «все» — выбрать всё, «минимум» — только "
                  "основу, Enter — установить отмеченные, «q» — выйти без установки.",
        "chosen": "Выбрано {n} из {total}.",
        "bad": "Не понял «{x}»: нужны номера от 1 до {total}, «все», «минимум», Enter или «q».",
        "cancel": "Ничего не установлено.",
        "core_fixed": "Основа обязательна — без неё остальное не работает.",
        "no_claude": "Claude Code на этом компьютере не найден (нет ~/.claude) — модуль пропущен.",
        "new": "Добавлены модули (новые в этой версии или появился нужный инструмент): {names}. Убрать: ./install.sh --choose",
    },
    "en": {
        "head": "Antigravity Plus — which modules to install",
        "intro": "Modules give the agent commands, subagents and rules. What you don't choose isn't installed and takes "
                 "no context. Change it any time: ./install.sh --choose",
        "required": "required",
        "tokens": "≈{n} tokens of context in every request",
        "outside": "outside Antigravity's context",
        "hints": "no standing text — only hints when they apply",
        "missing": "not found: {what}",
        "prompt": "Numbers separated by spaces toggle modules (e.g. «7 10»), «all» picks everything, «min» only the "
                  "core, Enter installs the checked ones, «q» quits without installing.",
        "chosen": "{n} of {total} chosen.",
        "bad": "Didn't get «{x}»: numbers 1 to {total}, «all», «min», Enter or «q».",
        "cancel": "Nothing installed.",
        "core_fixed": "The core is required — everything else builds on it.",
        "no_claude": "Claude Code isn't on this computer (no ~/.claude) — module skipped.",
        "new": "Modules added (new in this version, or the tool they need has appeared): {names}. Remove them with ./install.sh --choose",
    },
}

# The rich interface's words (installer_ui.py draws them; the plain output above stays as it was for scripts).
UI_TEXT = {
    "ru": {
        "tagline": "инженерная дисциплина в стиле Claude Code для Google Antigravity",
        "env": "Окружение", "modules": "Модули", "steps": "Установка", "steps_update": "Обновление",
        "steps_remove": "Удаление", "pick": "Какие модули установить",
        "desktop": "Antigravity Desktop", "cli": "Antigravity CLI", "claude": "Claude Code",
        "not_found": "не найден", "app_missing": "не найден — плагин всё равно ставится, Antigravity подхватит его",
        "cli_later": "найден — установка в CLI появится в следующей версии",
        "claude_missing": "не найден — мост Claude Code не ставится",
        "claude_dest": "→ {path} (модуль «Мост»)",
        "saved": "сохранённый выбор", "defaults": "по умолчанию — всё, для чего есть инструменты",
        "s_python": "Python для хуков", "s_stage": "Сборка выбранных модулей", "s_rules": "Правила и инструкции субагентов",
        "s_hooks": "Хуки и MCP-сервер", "s_copy": "Установка в {path}", "s_save": "Сохранение выбора",
        "s_panel": "Панель Control Center", "s_bridge": "Мост Claude Code", "s_doctor": "Проверка Antigravity (doctor)",
        "files": "файлов: {n}", "updated": "обновлено файлов: {n}", "trimmed": "очищено от лишних ссылок: {n}",
        "untouched": "без изменений", "hooks_detail": "событий хуков: {events} · MCP antigravity-plus_tools · `{py}`",
        "not_chosen": "модуль не выбран", "no_claude": "Claude Code не найден",
        "other_skill": "в {path} другой навык «antigravity» — не трогаю",
        "removed_skill": "навык убран (модуль не выбран)",
        "checks_ok": "проверок пройдено: {n}", "checks_bad": "замечаний: {n}",
        "doctor_hint": "Подробности — /doctor в Antigravity после перезапуска.",
        "no_python": "Python 3.8+ не найден как команда терминала",
        "done_first": "Antigravity Plus {v} установлен", "done_update": "Antigravity Plus обновлён: {old} → {v}",
        "done_same": "Antigravity Plus {v} переустановлен", "done_warn": "Antigravity Plus {v} установлен с замечаниями",
        "where": "Куда", "chosen": "Модули", "chosen_v": "{n} из {m}", "left_out": "не установлены: {names}",
        "added": "Добавлены", "bridge": "Claude Code",
        "next": "Что дальше",
        "next_restart": "Перезапустите Antigravity, чтобы он загрузил плагин.",
        "next_new": "Изменения действуют в новых разговорах (новый навык не виден — перезапустите Antigravity).",
        "next_doctor": "В Antigravity: /doctor — проверка установки.",
        "next_choose": "Изменить выбор модулей: {cmd} --choose",
        "cancel": "Отменено — ничего не изменено.", "cancel_late": "Прервано во время копирования — запустите {cmd} ещё раз.",
        "failed": "Установка не удалась", "failed_hint": "Установленная копия не тронута, если сбой случился до шага «Установка».",
        "un_q": "Удалить Antigravity Plus?", "purge_q": "Удалить и память, и настройки (~/.gemini/antigravity-plus)?",
        "un_plugin": "Плагин", "un_state": "Состояние и журналы", "un_data": "Память и настройки", "un_skill": "Навык Claude Code",
        "un_done": "Antigravity Plus удалён", "un_kept": "Память сохранена в {path} (удалить: --uninstall --purge).",
        "un_none": "не был установлен", "removed": "удалено", "kept": "оставлено",
        "un_next": "Перезапустите Antigravity.",
        "up_check": "Проверка новой версии", "up_same": "Уже последняя версия: {v}",
        "up_done": "Обновлено: {old} → {v}", "up_failed": "Обновление не удалось",
        "list_head": "Модули Antigravity Plus", "on": "установлен", "off": "не установлен",
        "demo": "Демо: ничего не устанавливается и не меняется", "sample": "образец", "demo_fail": "Демо: сбой шага",
        "demo_fail_hint": "Так выглядит сбой: текст ошибки раскрыт, ниже — подсказка, что делать.",
    },
    "en": {
        "tagline": "Claude Code-style engineering discipline for Google Antigravity",
        "env": "Environment", "modules": "Modules", "steps": "Installing", "steps_update": "Updating",
        "steps_remove": "Removing", "pick": "Which modules to install",
        "desktop": "Antigravity Desktop", "cli": "Antigravity CLI", "claude": "Claude Code",
        "not_found": "not found", "app_missing": "not found — the plugin installs anyway, Antigravity picks it up",
        "cli_later": "found — installing into the CLI comes in the next version",
        "claude_missing": "not found — the Claude Code bridge is skipped",
        "claude_dest": "→ {path} (the bridge module)",
        "saved": "saved choice", "defaults": "default — everything whose tools are here",
        "s_python": "Python for the hooks", "s_stage": "Staging the chosen modules", "s_rules": "Rules and subagent instructions",
        "s_hooks": "Hooks and the MCP server", "s_copy": "Installing into {path}", "s_save": "Saving the choice",
        "s_panel": "Control Center panel", "s_bridge": "Claude Code bridge", "s_doctor": "Antigravity check (doctor)",
        "files": "{n} files", "updated": "{n} files updated", "trimmed": "{n} files trimmed of missing commands",
        "untouched": "unchanged", "hooks_detail": "{events} hook events · MCP antigravity-plus_tools · `{py}`",
        "not_chosen": "module not chosen", "no_claude": "Claude Code not found",
        "other_skill": "{path} is another «antigravity» skill — left alone",
        "removed_skill": "skill removed (module not chosen)",
        "checks_ok": "{n} checks passed", "checks_bad": "{n} problems",
        "doctor_hint": "Details: /doctor in Antigravity after a restart.",
        "no_python": "Python 3.8+ isn't reachable as a terminal command",
        "done_first": "Antigravity Plus {v} installed", "done_update": "Antigravity Plus updated: {old} → {v}",
        "done_same": "Antigravity Plus {v} reinstalled", "done_warn": "Antigravity Plus {v} installed with problems",
        "where": "Where", "chosen": "Modules", "chosen_v": "{n} of {m}", "left_out": "not installed: {names}",
        "added": "Added", "bridge": "Claude Code",
        "next": "What's next",
        "next_restart": "Restart Antigravity so it loads the plugin.",
        "next_new": "Changes apply to new conversations (restart Antigravity if a new skill doesn't show up).",
        "next_doctor": "In Antigravity: /doctor checks the install.",
        "next_choose": "Change the modules: {cmd} --choose",
        "cancel": "Cancelled — nothing changed.", "cancel_late": "Interrupted while copying — run {cmd} again.",
        "failed": "The install failed", "failed_hint": "The installed copy is untouched if it failed before the «Installing» step.",
        "un_q": "Remove Antigravity Plus?", "purge_q": "Also remove its memory and settings (~/.gemini/antigravity-plus)?",
        "un_plugin": "Plugin", "un_state": "State and logs", "un_data": "Memory and settings", "un_skill": "Claude Code skill",
        "un_done": "Antigravity Plus removed", "un_kept": "Memory kept in {path} (delete it with --uninstall --purge).",
        "un_none": "wasn't installed", "removed": "removed", "kept": "kept",
        "un_next": "Restart Antigravity.",
        "up_check": "Looking for a new release", "up_same": "Already the newest: {v}",
        "up_done": "Updated: {old} → {v}", "up_failed": "The update failed",
        "list_head": "Antigravity Plus modules", "on": "installed", "off": "not installed",
        "demo": "Demo: nothing is installed or changed", "sample": "sample", "demo_fail": "Demo: a failing step",
        "demo_fail_hint": "This is what a failure looks like: the error is expanded, with a hint below.",
    },
}


def skipped(name: str) -> bool:
    return name in SKIP or name.endswith((".pyc", ".pyo"))


def sync(src: str, dest: str, extra: Optional[Dict[str, str]] = None) -> int:
    """Make `dest` a copy of `src` (like rsync --delete) without removing the folder itself, so a running
    Antigravity doesn't lose it mid-way. `extra` maps more files into it ({path in dest: source file}).
    Returns how many files changed."""
    changed = 0
    extra = extra or {}
    os.makedirs(dest, exist_ok=True)
    pairs = []
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if not skipped(d)]
        target = os.path.join(dest, os.path.relpath(dp, src))
        os.makedirs(target, exist_ok=True)
        pairs += [(os.path.join(dp, fn), os.path.join(target, fn)) for fn in fns if not skipped(fn)]
    pairs += [(s, os.path.join(dest, rel)) for rel, s in extra.items()]
    for s, d in pairs:
        if not os.path.exists(d) or not filecmp.cmp(s, d, shallow=False):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            changed += 1
    kept = {os.path.normcase(os.path.join(dest, rel)) for rel in extra}
    for dp, dns, fns in os.walk(dest, topdown=False):  # drop what the source no longer has
        rel = os.path.relpath(dp, dest)
        source_dir = os.path.join(src, rel)
        for fn in fns:
            if os.path.normcase(os.path.join(dp, fn)) in kept:
                continue
            if skipped(fn) or not os.path.exists(os.path.join(source_dir, fn)):
                os.remove(os.path.join(dp, fn))
                changed += 1
        if dp != dest and (skipped(os.path.basename(dp)) or not os.path.isdir(source_dir)) and not os.listdir(dp):
            os.rmdir(dp)
    return changed


def python_command() -> Optional[str]:
    """A Python 3.8+ command that works from `cmd /c` / `sh -c` without quoting: no spaces, no path. On Windows it
    runs in UTF-8 mode (-X utf8): Antigravity reads the hooks' answers as UTF-8, and files and pipes default to
    the ANSI code page there."""
    candidates = ["py -3 -X utf8", "python -X utf8", "python3 -X utf8"] if WINDOWS else ["python3", "python"]
    for cand in candidates:
        try:
            r = subprocess.run(cand.split() + ["-c", "import sys; print(sys.version_info >= (3, 8))"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip() == "True":
            return cand
    return None


def write_hooks(dest: str, python: str) -> None:
    path = os.path.join(dest, "hooks.json")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'"python3 (hooks/\w+\.py)"', lambda m: json.dumps(f"{python} {m.group(1)}"), text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_mcp(dest: str, python: str) -> None:
    """Rewrite mcp_config.json so the MCP server uses this computer's Python command (e.g. `py -3 -X utf8` on Windows)."""
    path = os.path.join(dest, "mcp_config.json")
    if not os.path.isfile(path):
        return
    parts = python.split()
    if not parts:
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return
    cmd, extra = parts[0], parts[1:]
    for srv in servers.values():
        if isinstance(srv, dict) and srv.get("command") == "python3":
            args = list(srv.get("args") or [])
            if extra[-2:] == ["-X", "utf8"] and args[:2] == ["-X", "utf8"]:
                args = args[2:]
            srv["command"] = cmd
            srv["args"] = extra + args
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ─────────────────────────────── modules ───────────────────────────────

def language(arg: Optional[str]) -> str:
    """--lang, else the language explicitly chosen before (modules.json), else the system's (lang_of_system)."""
    prev = saved()
    return pick_language(arg, prev.get("lang") if prev else None)


def lang_code(name: str) -> str:
    """"ru" for a Russian language or locale name (ru, ru-RU, ru_RU.UTF-8, Russian_Russia.1251), else ""."""
    n = (name or "").strip().lower()
    if not n or n in ("c", "posix") or n.startswith(("c.", "posix.")):
        return ""
    return "ru" if n.startswith(("ru", "russian")) else "en"


def apple_languages(plist_path: Optional[str] = None) -> List[str]:
    """macOS: the interface languages, first = the system's (AppleLanguages); [] when unreadable."""
    import plistlib
    path = plist_path or os.path.join(os.path.expanduser("~"), "Library", "Preferences", ".GlobalPreferences.plist")
    try:
        with open(path, "rb") as f:
            langs = plistlib.load(f).get("AppleLanguages")
        if isinstance(langs, list) and langs:
            return [str(x) for x in langs]
    except Exception:
        pass
    if plist_path is not None:  # a given file only (tests)
        return []
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=2, stdin=subprocess.DEVNULL)
        return re.findall(r'"?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)"?\s*,?\s*$', r.stdout or "", re.M) if r.returncode == 0 else []
    except Exception:
        return []


def windows_ui_language() -> Optional[int]:
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except Exception:
        return None


def env_locale(env: Dict[str, str]) -> str:
    """The locale the environment asks for, by precedence: LC_ALL → LC_MESSAGES → LANGUAGE (first of the list)
    → LANG; C, POSIX and empty values don't count."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = (env.get(var) or "").split(":")[0].strip()
        if lang_code(value):
            return value
    return ""


def lang_of_system(platform: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                   apple: Optional[Callable[[], List[str]]] = None, win: Optional[Callable[[], Optional[int]]] = None,
                   getlocale: Optional[Callable[[], str]] = None) -> str:
    """The system's language, "ru" or "en": the OS interface language (macOS AppleLanguages, Windows
    GetUserDefaultUILanguage), else the environment's locale, else locale.getlocale(), else English. Every source
    can be swapped (tests); none of them may fail the install."""
    platform = sys.platform if platform is None else platform
    env = dict(os.environ) if env is None else env
    try:
        if platform == "darwin":
            langs = (apple or apple_languages)()
            if langs and lang_code(langs[0]):
                return lang_code(langs[0])
        elif platform.startswith("win"):
            ui_lang = (win or windows_ui_language)()
            if ui_lang:
                return "ru" if ui_lang & 0x3FF == 0x19 else "en"
    except Exception:
        pass
    code = lang_code(env_locale(env))
    if code:
        return code
    try:
        name = getlocale() if getlocale else (locale.getlocale()[0] or "")
    except Exception:
        name = ""
    return lang_code(name) or "en"


def pick_language(arg: Optional[str], saved_lang: Optional[str], **sources) -> str:
    """--lang → the explicit choice saved before → the system's language."""
    if arg in ("ru", "en"):
        return arg
    if saved_lang in ("ru", "en"):
        return saved_lang
    return lang_of_system(**sources)


def missing(m: dict) -> List[str]:
    """What a module needs and this computer lacks (hard needs; "gh?" is only a note)."""
    out = []
    for need in m.get("needs") or []:
        if need == "git" and not shutil.which("git"):
            out.append("git")
        elif need == "browser" and not any(os.path.isfile(p) or shutil.which(p) for p in browsers()):
            out.append("Chrome / Chromium / Edge / Brave")
        elif need == "mobile" and not (shutil.which("xcrun") or shutil.which("adb") or android_sdk()):
            out.append("Xcode / Android SDK")
        elif need == "claude" and not os.path.isdir(CLAUDE):
            out.append("Claude Code (~/.claude)")
        elif need == "lsp" and not language_servers():
            out.append("a language server (pyright, sourcekit-lsp, kotlin-lsp, typescript-language-server, gopls…)")
    return out


def language_servers() -> List[str]:
    """The language servers on this computer (the lsp module's own list, skills/lsp/scripts/lsp.py)."""
    try:
        sys.path.insert(0, os.path.join(SRC, "skills", "lsp", "scripts"))
        import lsp
        return [sid for sid in lsp.SERVERS if lsp.server_command(sid)]
    except Exception:  # the module still installs; /lsp servers says what's missing
        return []


def browsers() -> List[str]:
    try:
        sys.path.insert(0, os.path.join(SRC, "hooks"))
        import compat
        return compat.browsers()
    except Exception:
        return []


def android_sdk() -> bool:
    roots = [os.environ.get("ANDROID_HOME", ""), os.environ.get("ANDROID_SDK_ROOT", ""),
             os.path.join(os.path.expanduser("~"), "Library", "Android", "sdk"),
             os.path.join(os.path.expanduser("~"), "Android", "Sdk"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")]
    return any(r and os.path.isdir(os.path.join(r, "platform-tools")) for r in roots)


def description_of(path: str) -> str:
    """The `description` of a SKILL.md or an agent file (plain or folded YAML)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(20000)
    except OSError:
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    lines, out, grab = m.group(1).splitlines(), [], False
    for line in lines:
        if grab:
            if line.startswith((" ", "\t")):
                out.append(line.strip())
                continue
            break
        d = re.match(r"^description:\s*(.*)$", line)
        if d:
            value = d.group(1).strip()
            if value in (">", ">-", "|", "|-", ""):
                grab = True
            else:
                out.append(value)
                break
    return " ".join(out)


def tokens(m: dict) -> int:
    """About how many tokens the module puts into every request: its skills' and agents' descriptions (the agent
    sees that list each turn) and its always-on rules."""
    chars = 0
    for s in m.get("skills") or []:
        chars += len(description_of(os.path.join(SRC, "skills", s, "SKILL.md"))) + 120  # name and path in the list
    for a in m.get("agents") or []:
        chars += len(description_of(os.path.join(SRC, "agents", a + ".md"))) + 60
    for r in m.get("rules") or []:
        try:
            chars += os.path.getsize(os.path.join(SRC, "rules", r))
        except OSError:
            pass
    return int(round(chars / 4 / 50.0)) * 50


def default_choice() -> Set[str]:
    return {m["id"] for m in MODULES if m.get("required") or not missing(m)}


def saved() -> Optional[dict]:
    try:
        with open(CHOICE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("installed"), list) else None
    except (OSError, ValueError):
        return None


def parse_ids(value: str) -> List[str]:
    ids = [x.strip().lower() for x in re.split(r"[\s,]+", value or "") if x.strip()]
    bad = [x for x in ids if x not in BY_ID]
    if bad:
        raise SystemExit(f"Unknown module(s): {', '.join(bad)}. Modules: {', '.join(IDS)} (install.py --list).")
    return ids


def interactive_terminal() -> bool:
    """A person at a terminal: not an agent's terminal (Antigravity sets TERM=dumb, the Antigravity CLI
    ANTIGRAVITY_AGENT=1, Claude Code CLAUDECODE), not CI."""
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return (tty and os.environ.get("TERM", "") != "dumb" and not os.environ.get("CLAUDECODE")
            and not os.environ.get("CI") and not os.environ.get("ANTIGRAVITY_AGENT"))


def wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=WIDTH, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False)


def title_line(i: int, m: dict, on: bool, lang: str) -> str:
    t = TEXT[lang]
    bits = []
    if m.get("required"):
        bits.append(t["required"])
    cost = tokens(m)
    bits.append(t["outside"] if m.get("claude") else t["hints"] if not cost else
                t["tokens"].format(n=f"{cost:,}".replace(",", " ")))
    lack = missing(m)
    if lack:
        bits.append("⚠ " + t["missing"].format(what=", ".join(lack)))
    return f"{i:>2}. [{'x' if on else ' '}] {m['title'][lang]} — {m['summary'][lang]}\n      " + " · ".join(bits)


def describe_all(selected: Set[str], lang: str) -> str:
    t = TEXT[lang]
    out = [t["head"], "", wrap(t["intro"], ""), ""]
    for i, m in enumerate(MODULES, 1):
        out.append(title_line(i, m, m["id"] in selected, lang))
        out.append(wrap(m["details"][lang], "      "))
        out.append("")
    return "\n".join(out)


def menu(selected: Set[str], lang: str, ask=input) -> Optional[Tuple[Set[str], Set[str]]]:
    """The interactive choice: toggles by number until Enter. (chosen, touched by the user), or None = quit."""
    t = TEXT[lang]
    chosen = set(selected)
    touched: Set[str] = set()
    print(describe_all(chosen, lang))
    while True:
        print(wrap(t["prompt"], ""))
        try:
            answer = ask("> ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("q", "quit", "exit", "выход", "й"):
            return None
        if answer == "":
            return chosen, touched
        if answer in ("all", "все", "всё"):
            chosen = {m["id"] for m in MODULES if not (m.get("claude") and missing(m))}
            touched = set(IDS)
        elif answer in ("min", "минимум", "core", "основа"):
            chosen, touched = {"core"}, set(IDS)
        else:
            for x in answer.replace(",", " ").split():
                if not x.isdigit() or not 1 <= int(x) <= len(MODULES):
                    print(t["bad"].format(x=x, total=len(MODULES)))
                    continue
                m = MODULES[int(x) - 1]
                if m.get("required"):
                    print(t["core_fixed"])
                elif m.get("claude") and missing(m):
                    print(t["no_claude"])
                else:
                    chosen ^= {m["id"]}
                    touched.add(m["id"])
        print()
        for i, m in enumerate(MODULES, 1):
            print(f"{i:>2}. [{'x' if m['id'] in chosen else ' '}] {m['title'][lang]}")
        print(t["chosen"].format(n=len(chosen), total=len(MODULES)) + "\n")


def choose(a, lang: str, ui=None) -> Optional[Tuple[Set[str], List[str], Set[str]]]:
    """The modules to install, the ids added to a saved choice (new in this version, or their tool has appeared),
    and the modules left out only because a tool they need is missing (added later, once it is there)."""
    prev = saved()
    lacking = {m["id"] for m in MODULES if missing(m)}
    if a.modules:
        return finish_choice(set(parse_ids(a.modules)), a), [], set()
    if a.all:
        return finish_choice(set(IDS), a), [], set()
    if prev and not a.choose:
        known = set(prev.get("known") or prev["installed"])
        waiting = {x for x in prev.get("unavailable") or [] if x in BY_ID}
        chosen = {x for x in prev["installed"] if x in BY_ID}
        new = [m["id"] for m in MODULES if (m["id"] not in known or m["id"] in waiting) and m["id"] not in lacking]
        chosen |= set(new)
        later = {x for x in IDS if (x not in known or x in waiting) and x in lacking}
        return finish_choice(chosen, a), new, later
    if os.path.isdir(DEST) and not a.choose:
        return finish_choice(set(IDS), a), [], set()  # installed before modules existed: keep everything it had
    chosen = {x for x in prev["installed"] if x in BY_ID} if prev else default_choice()
    touched: Set[str] = set()
    if a.choose or interactive_terminal():
        PROGRESS["menu"] = True
        picked = None
        try:  # the rich multi-select where a person can press keys, else the line menu
            if ui is None or not ui.rich:
                raise NotImplementedError
            picked = ui.select(module_items(lang), chosen, UI_TEXT[lang]["pick"])
            if picked is None:
                return None
        except NotImplementedError:
            picked = menu(chosen, lang)
        if picked is None:
            return None
        chosen, touched = picked
    return finish_choice(chosen, a), [], {x for x in lacking - chosen - touched}


def finish_choice(chosen: Set[str], a) -> Set[str]:
    chosen |= {m["id"] for m in MODULES if m.get("required")}
    for x in parse_ids(a.without or ""):
        if not BY_ID[x].get("required"):
            chosen.discard(x)
    if a.no_claude:
        chosen.discard("claude")
    return chosen


def excluded(chosen: Set[str]) -> Set[str]:
    """Paths (relative to the plugin, forward slashes) of what the modules left out own."""
    out = set()
    for m in MODULES:
        if m["id"] in chosen:
            continue
        keep = m.get("keep") or []
        for s in m.get("skills") or []:
            # a skill whose scripts the hooks need loses only its SKILL.md: Antigravity no longer lists it
            out.add(f"skills/{s}/SKILL.md" if any(k.startswith(f"skills/{s}/") for k in keep) else f"skills/{s}")
        out |= {f"agents/{x}.md" for x in m.get("agents") or []}
        out |= {f"rules/{r}" for r in m.get("rules") or []}
        out |= {f"sidecars/{s}" for s in m.get("sidecars") or []}
    return out


def filter_rules(text: str, skills: Set[str], sidecars: Set[str]) -> str:
    """A rule file without what points at commands that aren't installed: rows of the commands table, links to a
    side panel that isn't there, lines marked `<!-- needs:<skill> -->`, and such commands in comma lists."""
    out = []
    for line in text.splitlines():
        need = re.search(r"\s*<!--\s*needs:([\w-]+)\s*-->", line)
        if need:
            if need.group(1) not in skills:
                continue
            line = line[:need.start()] + line[need.end():]
        row = re.match(r"^\|\s*`/([\w-]+)`", line)
        if row and row.group(1) not in skills:
            continue
        panel = re.search(r"sidecar://antigravity-plus/([\w-]+)/", line)
        if panel and panel.group(1) not in sidecars:
            continue
        if len(re.findall(r"`/[\w-]+`", line)) >= 3:  # a list of commands: drop the missing ones
            line = re.sub(r"`/([\w-]+)`(, )?", lambda m: m.group(0) if m.group(1) in skills else "", line)
            line = re.sub(r",\s*(,|\s(or|and)\b)", r" \2", line).replace("  ", " ").replace(" ,", ",")
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def stage(chosen: Set[str], python: str) -> str:
    """A temporary copy of the plugin with just the chosen modules, ready to sync into place."""
    root = stage_copy(chosen)
    try:
        stage_rules(root)
        stage_python(root, python)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def stage_copy(chosen: Set[str]) -> str:
    """Step 1 of stage(): copy what the chosen modules own into a new temporary folder (removed on failure)."""
    root = tempfile.mkdtemp(prefix="agp-install-")
    dest = os.path.join(root, "antigravity-plus")
    gone = excluded(chosen)

    def ignore(folder: str, names: List[str]) -> List[str]:
        rel = os.path.relpath(folder, SRC).replace(os.sep, "/")
        rel = "" if rel == "." else rel + "/"
        return [n for n in names if skipped(n) or rel + n in gone]

    try:
        shutil.copytree(SRC, dest, ignore=ignore)
    except BaseException:  # Ctrl-C or a full disk: leave nothing behind
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def stage_rules(root: str) -> int:
    """Step 2: the rules and subagents lose what points at commands left out. Returns how many files changed."""
    dest = os.path.join(root, "antigravity-plus")
    skills = set(os.listdir(os.path.join(dest, "skills")))
    sidecars = set(os.listdir(os.path.join(dest, "sidecars"))) if os.path.isdir(os.path.join(dest, "sidecars")) else set()
    changed = 0
    for folder in ("rules", "agents"):  # subagents' instructions point at skills too (<!-- needs:lsp -->)
        folder = os.path.join(dest, folder)
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            new = filter_rules(text, skills, sidecars)
            if new != text:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
                changed += 1
    return changed


def stage_python(root: str, python: str) -> None:
    """Step 3: hooks.json and mcp_config.json start this computer's Python."""
    dest = os.path.join(root, "antigravity-plus")
    if python != "python3":
        write_hooks(dest, python)
        write_mcp(dest, python)


def count_files(folder: str) -> int:
    return sum(len(fns) for _dp, _dns, fns in os.walk(folder))


def make_executable() -> None:
    if WINDOWS:
        return
    for sub in ("hooks", "skills", "mcp"):
        for dp, _dns, fns in os.walk(os.path.join(DEST, sub)):
            for fn in fns:
                if fn.endswith((".py", ".sh")):
                    p = os.path.join(dp, fn)
                    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def version_of(plugin: str) -> Optional[str]:
    try:
        with open(os.path.join(plugin, "plugin.json"), encoding="utf-8") as f:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def save_choice(chosen: Set[str], version: str, later: Set[str], lang: Optional[str] = None) -> None:
    off: Dict[str, object] = {}
    for m in MODULES:
        if m["id"] not in chosen:
            off.update(m.get("off") or {})
    record = {"version": version, "at": dt.datetime.now().isoformat(timespec="seconds"),
              "installed": [m["id"] for m in MODULES if m["id"] in chosen], "known": IDS,
              "titles": {m["id"]: m["title"] for m in MODULES}, "off": off, "source": HERE,
              "unavailable": [x for x in IDS if x in later and x not in chosen]}
    if lang in ("ru", "en"):
        record["lang"] = lang
    os.makedirs(DATA, exist_ok=True)
    tmp = CHOICE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHOICE)


# ─────────────────────────────── Claude Code ───────────────────────────────

BRIDGE_SHARED = ("compat.py", "client.py", "client-snapshot.json")  # hooks/ files delegate.py imports or reads


def bridge_files() -> Dict[str, str]:
    """The plugin's hooks/ files that go next to delegate.py ({path in the skill: source}); a file the source
    doesn't have (yet) is skipped."""
    return {os.path.join("scripts", n): os.path.join(SRC, "hooks", n) for n in BRIDGE_SHARED
            if os.path.isfile(os.path.join(SRC, "hooks", n))}


def claude_skill_ours() -> bool:
    """~/.claude/skills/antigravity is free, or it is the one this installer put there (never touch another
    skill of the same name)."""
    return not os.path.exists(CLAUDE_DEST) or os.path.isfile(os.path.join(CLAUDE_DEST, "scripts", "delegate.py"))


def install_claude_skill(python: str) -> Optional[str]:
    """Claude Code's side of the bridge: the skill `antigravity`, with compat.py next to its script. Returns a line
    for the summary, or None when Claude Code isn't on this computer."""
    if not os.path.isdir(CLAUDE) or not os.path.isfile(os.path.join(CLAUDE_SRC, "SKILL.md")):
        return None
    if not claude_skill_ours():
        return f"  claude : {CLAUDE_DEST} is another skill named «antigravity» — left as it is"
    changed = sync(CLAUDE_SRC, CLAUDE_DEST, bridge_files())
    skill = os.path.join(CLAUDE_DEST, "SKILL.md")
    if python != "python3":  # Windows: the skill's commands start Python the way this computer can
        with open(skill, encoding="utf-8") as f:
            text = f.read()
        with open(skill, "w", encoding="utf-8") as f:
            f.write(text.replace("python3 ${CLAUDE_SKILL_DIR}", python + " ${CLAUDE_SKILL_DIR}"))
    if not WINDOWS:
        script = os.path.join(CLAUDE_DEST, "scripts", "delegate.py")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return (f"  claude : {CLAUDE_DEST} ({changed} files updated) — Claude Code hands sub-tasks to Antigravity "
            "on its own (skill «antigravity», new Claude Code sessions)")


def remove_claude_skill() -> Optional[str]:
    if os.path.isdir(CLAUDE_DEST) and claude_skill_ours():
        shutil.rmtree(CLAUDE_DEST, ignore_errors=True)
        return f"  claude : removed {CLAUDE_DEST} (module not chosen)"
    return None


STATE_FILES = ("state", "probe", "coord", "guard.log", "usage-cache.json", "delegate")


def remove_state() -> int:
    """The plugin's runtime state (memory and settings stay). Returns how many entries were removed."""
    n = 0
    for name in STATE_FILES:
        p = os.path.join(DATA, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            n += 1
        elif os.path.exists(p):
            os.remove(p)
            n += 1
    return n


def uninstall(purge: bool) -> int:
    shutil.rmtree(DEST, ignore_errors=True)
    if os.path.isdir(CLAUDE_DEST) and claude_skill_ours():
        shutil.rmtree(CLAUDE_DEST, ignore_errors=True)
        print(f"Removed Claude Code's skill {CLAUDE_DEST}.")
    remove_state()
    if purge:
        shutil.rmtree(DATA, ignore_errors=True)
        print(f"Removed {DEST} and {DATA}. Restart Antigravity.")
    else:
        print(f"Removed {DEST}. Restart Antigravity.")
        if os.path.isdir(os.path.join(DATA, "memory")):
            print(f"Memory kept in {os.path.join(DATA, 'memory')} (delete it with --uninstall --purge).")
    return 0


# ─────────────────────────────── the rich interface (installer_ui.py) ───────────────────────────────

PROGRESS = {"copying": False, "menu": False}  # copying: the installed copy is being replaced; menu: a menu was shown


def short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def script_cmd() -> str:
    return "install.cmd" if WINDOWS else "./install.sh"


def hooks_compat():
    """The plugin's hooks/compat.py (its CLI helpers arrive with the CLI support: called through getattr)."""
    try:
        sys.path.insert(0, os.path.join(SRC, "hooks"))
        import compat
        return compat
    except Exception:
        return None


def desktop_app() -> Tuple[bool, str]:
    """Antigravity's desktop app: (found, version). macOS reads the app bundle; elsewhere the program on PATH or in
    its usual folder; the running language server's version fills a gap."""
    found, version = False, ""
    if sys.platform == "darwin":
        for base in ("/Applications", os.path.join(os.path.expanduser("~"), "Applications")):
            plist = os.path.join(base, "Antigravity.app", "Contents", "Info.plist")
            if os.path.isfile(plist):
                found = True
                try:
                    import plistlib
                    with open(plist, "rb") as f:
                        version = str(plistlib.load(f).get("CFBundleShortVersionString") or "")
                except Exception:
                    pass
                break
    elif WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        found = bool(local) and os.path.isfile(os.path.join(local, "Programs", "Antigravity", "Antigravity.exe"))
    else:
        found = bool(shutil.which("antigravity"))
    if not version:
        compat = hooks_compat()
        try:
            servers = compat.daemon_servers() if compat else []
            version = next((ls.version for ls in servers if getattr(ls, "version", "")), "")
        except Exception:
            version = ""
    return found or bool(version), version


def cli_app() -> Tuple[bool, str]:
    """Antigravity CLI (`agy`): (found, version) — compat.agy()/agy_version() when the plugin has them."""
    compat = hooks_compat()
    path = ""
    try:
        fn = getattr(compat, "agy", None)
        path = fn() if fn else ""
    except Exception:
        path = ""
    if not path:
        exe = "agy.exe" if WINDOWS else "agy"
        local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", exe) if WINDOWS else \
            os.path.join(os.path.expanduser("~"), ".local", "bin", exe)
        path = shutil.which("agy") or (local if os.path.isfile(local) else "")
    if not path:
        return False, ""
    version = ""
    try:
        fn = getattr(compat, "agy_version", None)
        if fn:
            version = fn() or ""
        else:
            r = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=2, stdin=subprocess.DEVNULL)
            m = re.search(r"\d+\.\d+(?:\.\d+)?", r.stdout or "")
            version = m.group(0) if m and r.returncode == 0 else ""
    except Exception:
        version = ""
    return True, version


def claude_app() -> Tuple[bool, str]:
    if not os.path.isdir(CLAUDE):
        return False, ""
    exe = shutil.which("claude")
    if not exe:
        return True, ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=5, stdin=subprocess.DEVNULL)
        m = re.search(r"\d+\.\d+(?:\.\d+)?", r.stdout or "")
        return True, m.group(0) if m and r.returncode == 0 else ""
    except Exception:
        return True, ""


def targets() -> List[dict]:
    """Where the plugin goes: one entry per client found or looked for — {id, name, found, version, dest, note}.
    Desktop and CLI read the same plugin folder (the CLI has no side panels); Claude Code gets the bridge skill."""
    desktop, desktop_v = desktop_app()
    cli, cli_v = cli_app()
    claude, claude_v = claude_app()
    return [
        {"id": "desktop", "name": "Antigravity Desktop", "found": desktop, "version": desktop_v, "dest": DEST},
        {"id": "cli", "name": "Antigravity CLI", "found": cli, "version": cli_v, "dest": DEST},
        {"id": "claude", "name": "Claude Code", "found": claude, "version": claude_v, "dest": CLAUDE_DEST},
    ]


TARGET_NOTES = {
    "ru": {"desktop": "→ {dest}", "cli": "→ та же папка плагина; панели Control Center в CLI нет",
           "claude": "→ {dest} (модуль «Мост»)",
           "desktop_none": "не найден — плагин всё равно ставится", "cli_none": "не найден",
           "claude_none": "не найден — мост Claude Code не ставится"},
    "en": {"desktop": "→ {dest}", "cli": "→ the same plugin folder; no Control Center panel in the CLI",
           "claude": "→ {dest} (the bridge module)",
           "desktop_none": "not found — the plugin installs anyway", "cli_none": "not found",
           "claude_none": "not found — the Claude Code bridge is skipped"},
}


def environment_rows(found: List[dict], lang: str) -> List[Tuple[str, str, str]]:
    notes = TARGET_NOTES[lang]
    rows = []
    for t in found:
        name = t["name"] + (" " + t["version"] if t.get("version") else "")
        if t["found"]:
            rows.append(("found", name, notes[t["id"]].format(dest=short(t["dest"]))))
        else:
            rows.append(("none", t["name"], notes[t["id"] + "_none"]))
    return rows


def module_items(lang: str) -> List[tui.Item]:
    from modules import GROUPS
    titles = {g["id"]: g["title"][lang] for g in GROUPS}
    t = TEXT[lang]
    items = []
    for m in MODULES:
        cost = tokens(m)
        meta = (t["outside"] if m.get("claude") else t["hints"] if not cost else
                t["tokens"].format(n=f"{cost:,}".replace(",", " ")))
        lack = missing(m)
        items.append(tui.Item(m["id"], m["title"][lang], m["summary"][lang], m["details"][lang], meta,
                              locked=bool(m.get("required")),
                              unavailable=t["missing"].format(what=", ".join(lack)) if lack else "",
                              group=titles.get(GROUP_OF.get(m["id"], {}).get("id", ""), ""),
                              blocked=bool(m.get("claude") and lack)))
    return items


def run_doctor(capture: bool) -> Tuple[int, str]:
    # UTF-8 mode: on Windows the console's code page (cp1251, cp1252…) would mangle what the checks read and print
    cmd = [sys.executable, "-X", "utf8", os.path.join(DEST, "skills", "doctor", "scripts", "doctor.py"), "--install-check"]
    if not capture:
        return subprocess.run(cmd).returncode, ""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                       stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUTF8="1"))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def install_plain(a, lang: str) -> int:
    """The line-by-line install: what update.py, scripts, agents and CI read (keep its wording)."""
    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is required.", file=sys.stderr)
        return 1
    if not os.path.isfile(os.path.join(SRC, "plugin.json")):
        print(f"plugin.json not found in {SRC}", file=sys.stderr)
        return 1
    python = python_command()
    if not python:
        print(no_python_text(), file=sys.stderr)
        return 1
    picked = choose(a, lang)
    if picked is None:
        print(TEXT[lang]["cancel"])
        return 0
    chosen, new, later = picked

    first = not os.path.isdir(DEST)
    staged = stage(chosen, python)
    try:
        PROGRESS["copying"] = True
        changed = sync(os.path.join(staged, "antigravity-plus"), DEST)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    make_executable()
    PROGRESS["copying"] = False

    version_text = version_of(DEST) or "?"
    save_choice(chosen, version_text, later, a.lang or (saved() or {}).get("lang"))
    left_out = [m["title"][lang] for m in MODULES if m["id"] not in chosen]
    print(f"Installed Antigravity Plus {version_text} → {DEST} ({changed} files updated)")
    print(f"  modules: {len(chosen)} of {len(MODULES)} — " + ", ".join(m["title"][lang] for m in MODULES if m["id"] in chosen)
          + (f"; not installed: {', '.join(left_out)}" if left_out else "") + " · change: ./install.sh --choose")
    if new:
        print("  " + TEXT[lang]["new"].format(names=", ".join(BY_ID[x]["title"][lang] for x in new)))
    print("  skills : " + " ".join(sorted(os.listdir(os.path.join(DEST, "skills")))))
    agents = sorted(n[:-3] for n in os.listdir(os.path.join(DEST, "agents")) if n.endswith(".md"))
    print("  agents : " + (" ".join(agents) if agents else "—"))
    print(f"  hooks  : run with `{python}` — session context, instructions, guard, edit checks, coordination, Stop")
    if "usage" in chosen:
        print("  panel  : sidecars/usage — side pane «Расход токенов» (Antigravity Desktop)")
    print(f"  data   : {DATA} (memory, settings, modules.json) + ~/.claude/projects/<project>/memory (shared with Claude Code)")
    claude = install_claude_skill(python) if "claude" in chosen else remove_claude_skill()
    if claude:
        print(claude)
    print("\nAntigravity check:", flush=True)
    run_doctor(capture=False)
    print("\n" + ("First install: restart Antigravity to load the plugin." if first else
                  "Updated: changes apply to new conversations (restart Antigravity if a new skill doesn't show up)."))
    return 0


def no_python_text() -> str:
    return ("Python 3.8+ isn't reachable as " + ("`py -3`, `python` or `python3`" if WINDOWS else "`python3`")
            + " from a terminal; Antigravity's hooks need it. Install it from python.org"
            + (" (keep «Add python.exe to PATH» and the py launcher)" if WINDOWS else "") + ".")


def hook_events() -> int:
    try:
        with open(os.path.join(SRC, "hooks.json"), encoding="utf-8") as f:
            data = json.load(f)
        hooks = data.get("hooks", data) if isinstance(data, dict) else {}
        return len(hooks) if isinstance(hooks, dict) else 0
    except (OSError, ValueError):
        return 0


def doctor_verdict(out: str) -> Tuple[int, List[str]]:
    """(checks passed, problem lines) from doctor --install-check."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    good = sum(1 for ln in lines if ln.startswith("✅"))
    bad = [ln for ln in lines if ln.startswith(("❌", "⚠️", "⚠"))]
    return good, bad


def install_rich(a, lang: str, ui) -> int:
    t = UI_TEXT[lang]
    old = version_of(DEST)
    first = not os.path.isdir(DEST)
    ui.banner(version_of(SRC) or "?", t["tagline"])
    if sys.version_info < (3, 8) or not os.path.isfile(os.path.join(SRC, "plugin.json")):
        ui.box(t["failed"], ["Python 3.8+" if sys.version_info < (3, 8) else f"plugin.json not found in {SRC}"], "fail")
        return 1
    ui.section(t["env"])
    ui.rows(environment_rows(targets(), lang))
    ui.line()
    with ui.step(t["s_python"]) as s:
        python = python_command()
        if python:
            s.ok(python)
        else:
            s.fail(t["no_python"], hint=no_python_text())
    if not python:
        return 1
    picked = choose(a, lang, ui)
    if picked is None:
        ui.note(t["cancel"], "skip")
        return 0
    chosen, new, later = picked
    if not PROGRESS.get("menu"):
        names = ", ".join(m["title"][lang] for m in MODULES if m["id"] in chosen)
        how = t["saved"] if saved() and not (a.modules or a.all) else ""
        ui.rows([("ok", t["modules"], t["chosen_v"].format(n=len(chosen), m=len(MODULES))
                  + (" · " + how if how else "") + " — " + names)], indent=3)
    ui.line()
    ui.section(t["steps"] if first else t["steps_update"])
    staged = None
    try:
        with ui.step(t["s_stage"]) as s:
            staged = stage_copy(chosen)
            s.ok(t["files"].format(n=count_files(staged)))
        with ui.step(t["s_rules"]) as s:
            n = stage_rules(staged)
            s.ok(t["trimmed"].format(n=n) if n else t["untouched"])
        with ui.step(t["s_hooks"]) as s:
            stage_python(staged, python)
            s.ok(t["hooks_detail"].format(events=hook_events(), py=python))
        with ui.step(t["s_copy"].format(path=short(DEST))) as s:
            PROGRESS["copying"] = True
            changed = sync(os.path.join(staged, "antigravity-plus"), DEST)
            make_executable()
            PROGRESS["copying"] = False
            s.ok(t["updated"].format(n=changed))
    finally:
        if staged:
            shutil.rmtree(staged, ignore_errors=True)
    version_text = version_of(DEST) or "?"
    with ui.step(t["s_save"]) as s:
        save_choice(chosen, version_text, later, a.lang or (saved() or {}).get("lang"))
        s.ok(short(CHOICE))
    with ui.step(t["s_panel"]) as s:
        if "usage" in chosen:
            s.ok("sidecars/usage · Antigravity Desktop")
        else:
            s.skip(t["not_chosen"])
    with ui.step(t["s_bridge"]) as s:
        if "claude" in chosen:
            said = install_claude_skill(python)
            if said is None:
                s.skip(t["no_claude"])
            elif "left as it is" in said:
                s.warn(t["other_skill"].format(path=short(CLAUDE_DEST)))
            else:
                m = re.search(r"\((\d+) files updated\)", said)
                s.ok(short(CLAUDE_DEST) + (" · " + t["updated"].format(n=m.group(1)) if m else ""))
        else:
            said = remove_claude_skill()
            s.skip(t["removed_skill"] if said else t["not_chosen"] if os.path.isdir(CLAUDE) else t["no_claude"])
    problems: List[str] = []
    with ui.step(t["s_doctor"]) as s:
        code, out = run_doctor(capture=True)
        good, problems = doctor_verdict(out)
        if code != 0 and not problems:
            problems = [ln for ln in out.strip().splitlines()[-12:]]
        if problems:
            s.warn(t["checks_bad"].format(n=len(problems)), body=problems, hint=t["doctor_hint"])
        else:
            s.ok(t["checks_ok"].format(n=good))
    left_out = [m["title"][lang] for m in MODULES if m["id"] not in chosen]
    title = (t["done_warn"].format(v=version_text) if problems else t["done_first"].format(v=version_text) if first or not old
             else t["done_update"].format(old=old, v=version_text) if old != version_text
             else t["done_same"].format(v=version_text))
    lines: List = [(t["where"], short(DEST)),
                   (t["chosen"], t["chosen_v"].format(n=len(chosen), m=len(MODULES))
                    + (" · " + t["left_out"].format(names=", ".join(left_out)) if left_out else ""))]
    if new:
        lines.append((t["added"], ", ".join(BY_ID[x]["title"][lang] for x in new)))
    if "claude" in chosen and os.path.isdir(CLAUDE):
        lines.append((t["bridge"], short(CLAUDE_DEST)))
    lines += ["", ui.style.bold(t["next"]),
              "1. " + (t["next_restart"] if first else t["next_new"]),
              "2. " + t["next_doctor"],
              "3. " + t["next_choose"].format(cmd=script_cmd())]
    ui.box(title, lines, "warn" if problems else "ok")
    return 0


def uninstall_rich(purge: bool, lang: str, ui) -> int:
    t = UI_TEXT[lang]
    ui.banner(version_of(DEST) or version_of(SRC) or "?", t["tagline"])
    if ui.caps.interactive:
        ans = ui.confirm(t["un_q"], default=True)
        if not ans:
            ui.note(t["cancel"], "skip")
            return 0
        if purge:
            ans = ui.confirm(t["purge_q"], default=False)
            if ans is None:
                ui.note(t["cancel"], "skip")
                return 0
            purge = ans
    ui.line()
    ui.section(t["steps_remove"])
    with ui.step(t["un_plugin"]) as s:
        if os.path.isdir(DEST):
            shutil.rmtree(DEST, ignore_errors=True)
            s.ok(t["removed"] + " · " + short(DEST))
        else:
            s.skip(t["un_none"])
    with ui.step(t["un_skill"]) as s:
        if os.path.isdir(CLAUDE_DEST) and claude_skill_ours():
            shutil.rmtree(CLAUDE_DEST, ignore_errors=True)
            s.ok(t["removed"] + " · " + short(CLAUDE_DEST))
        elif os.path.isdir(CLAUDE_DEST):
            s.skip(t["other_skill"].format(path=short(CLAUDE_DEST)))
        else:
            s.skip(t["un_none"])
    with ui.step(t["un_state"]) as s:
        n = remove_state()
        s.ok(t["files"].format(n=n)) if n else s.skip(t["un_none"])
    memory = os.path.isdir(os.path.join(DATA, "memory"))
    with ui.step(t["un_data"]) as s:
        if purge:
            shutil.rmtree(DATA, ignore_errors=True)
            s.ok(t["removed"] + " · " + short(DATA))
        else:
            s.skip(t["kept"] + " · " + short(DATA))
    lines = [t["un_next"]]
    if not purge and memory:
        lines.append(t["un_kept"].format(path=short(os.path.join(DATA, "memory"))))
    ui.box(t["un_done"], lines)
    return 0


def update_rich(lang: str, ui) -> int:
    t = UI_TEXT[lang]
    old = version_of(DEST)
    ui.banner(version_of(SRC) or "?", t["tagline"])
    ui.section(t["steps_update"])
    with ui.step(t["up_check"]) as s:
        r = subprocess.run([sys.executable, os.path.join(SRC, "skills", "update", "scripts", "update.py"), "run",
                            "--source", HERE], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUTF8="1"))
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        new = version_of(DEST)
        if r.returncode != 0:
            s.fail(t["up_failed"], body=out.splitlines()[-15:])
        elif new and new != old:
            s.ok(t["up_done"].format(old=old or "—", v=new))
        else:
            s.ok(t["up_same"].format(v=new or old or "?"))
    if r.returncode == 0:
        title = t["up_done"].format(old=old or "—", v=new) if new and new != old else t["up_same"].format(v=new or old or "?")
        ui.box(title, out.splitlines()[:40] or [""], "warn" if any(ln.strip().startswith(("❌", "⚠")) for ln in out.splitlines())
               else "ok")
    return r.returncode


def list_rich(lang: str, ui) -> int:
    from modules import GROUPS
    t, tt = UI_TEXT[lang], TEXT[lang]
    prev = saved()
    selected = set(prev["installed"]) if prev else default_choice()
    ui.banner(version_of(SRC) or "?", t["tagline"])
    for g in GROUPS:
        ui.section(g["title"][lang])
        for mid in g["modules"]:
            m = BY_ID[mid]
            lack = missing(m)
            cost = tokens(m)
            meta = [t["on"] if mid in selected else t["off"]]
            if m.get("required"):
                meta.insert(0, tt["required"])
            meta.append(tt["outside"] if m.get("claude") else tt["hints"] if not cost else
                        tt["tokens"].format(n=f"{cost:,}".replace(",", " ")))
            if lack:
                meta.append(tt["missing"].format(what=", ".join(lack)))
            ui.rows([("ok" if mid in selected else "none", m["title"][lang], " · ".join(meta))])
            ui.paragraph(m["details"][lang], indent=6)
        ui.line()
    ui.note(tt["intro"])
    return 0


def demo(lang: str, ui) -> int:
    """`install.py --demo`: the whole scenario with made-up data and delays — nothing on this computer changes."""
    import time as _t
    t = UI_TEXT[lang]
    pause = 0.0 if os.environ.get("AGP_DEMO_FAST") else 1.0
    ui.banner(version_of(SRC) or "?", t["tagline"], t["demo"])
    ui.section(t["env"])
    found = targets()  # the real environment (read only); the steps below are made up
    if any(x["found"] for x in found):
        ui.rows(environment_rows(found, lang))
    else:
        ui.rows([(k, n + " · " + t["sample"], d) for k, n, d in environment_rows(
            [dict(x, found=True, version="") for x in found], lang)])
    ui.line()
    with ui.step(t["s_python"]) as s:
        _t.sleep(0.4 * pause)
        s.ok("py -3 -X utf8" if WINDOWS else "python3")
    chosen = default_choice() | {"claude"}
    try:
        picked = ui.select(module_items(lang), chosen, t["pick"])
        if picked is None:
            ui.note(t["cancel"], "skip")
            return 0
        chosen = picked[0]
    except NotImplementedError:
        ui.rows([("ok", t["modules"], t["chosen_v"].format(n=len(chosen), m=len(MODULES)) + " · " + t["defaults"])])
    ui.line()
    ui.section(t["steps"])
    plan = [(t["s_stage"], t["files"].format(n=412), 0.9), (t["s_rules"], t["trimmed"].format(n=3), 0.5),
            (t["s_hooks"], t["hooks_detail"].format(events=5, py="python3"), 0.5),
            (t["s_copy"].format(path=short(DEST)), t["updated"].format(n=37), 1.2),
            (t["s_save"], short(CHOICE), 0.3), (t["s_panel"], "sidecars/usage · Antigravity Desktop", 0.3),
            (t["s_bridge"], short(CLAUDE_DEST), 0.6), (t["s_doctor"], t["checks_ok"].format(n=31), 2.4)]
    for label, detail, took in plan:
        with ui.step(label) as s:
            end = _t.time() + took * pause
            while _t.time() < end:
                _t.sleep(0.05)
            s.ok(detail)
    left_out = [m["title"][lang] for m in MODULES if m["id"] not in chosen]
    ui.box(t["done_first"].format(v=version_of(SRC) or "?"), [
        (t["where"], short(DEST)),
        (t["chosen"], t["chosen_v"].format(n=len(chosen), m=len(MODULES))
         + (" · " + t["left_out"].format(names=", ".join(left_out)) if left_out else "")),
        (t["bridge"], short(CLAUDE_DEST)),
        "", ui.style.bold(t["next"]), "1. " + t["next_restart"], "2. " + t["next_doctor"],
        "3. " + t["next_choose"].format(cmd=script_cmd())])
    ui.line()
    ui.section(t["demo_fail"])
    with ui.step(t["s_copy"].format(path=short(DEST))) as s:
        _t.sleep(0.8 * pause)
        s.ok(t["updated"].format(n=37))
    with ui.step(t["s_doctor"]) as s:
        _t.sleep(1.5 * pause)
        s.warn(t["checks_bad"].format(n=2), body=["❌ hooks: hooks.json не загружен — Antigravity ещё не перезапущен"
                                                  if lang == "ru" else "❌ hooks: hooks.json isn't loaded — Antigravity wasn't restarted",
                                                  "⚠️ MCP: antigravity-plus_tools (starting)"], hint=t["doctor_hint"])
    with ui.step(t["s_bridge"]) as s:
        _t.sleep(0.6 * pause)
        s.fail("PermissionError: [Errno 13] " + short(CLAUDE_DEST),
               body=["shutil.copy2: " + short(os.path.join(CLAUDE_DEST, "scripts", "delegate.py")),
                     "PermissionError: [Errno 13] Permission denied: '" + short(CLAUDE_DEST) + "'"],
               hint=t["demo_fail_hint"])
    ui.box(t["done_warn"].format(v=version_of(SRC) or "?"), [t["doctor_hint"]], "warn")
    return 0


def dispatch(a, lang: str, ui) -> int:
    """The branch the arguments ask for."""
    if a.demo:
        return demo(lang, ui)
    if a.uninstall:
        return uninstall_rich(a.purge, lang, ui) if ui.rich else uninstall(a.purge)
    if a.update:  # the same as /update in Antigravity: pull (fast-forward only), then the new install.py installs
        if ui.rich:
            return update_rich(lang, ui)
        return subprocess.run([sys.executable, os.path.join(SRC, "skills", "update", "scripts", "update.py"),
                               "run", "--source", HERE]).returncode
    if a.list:
        if ui.rich:
            return list_rich(lang, ui)
        prev = saved()
        print(describe_all(set(prev["installed"]) if prev else default_choice(), lang))
        return 0
    if ui.rich:
        return install_rich(a, lang, ui)
    return install_plain(a, lang)


def main(argv: Optional[List[str]] = None) -> int:
    encoding = getattr(sys.stdout, "encoding", "") or ""  # the console's own, before it is switched to UTF-8
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true", help="remove the plugin")
    ap.add_argument("--update", action="store_true",
                    help="update to the newest release (fast-forward pull of this clone, or a download) and install it")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also remove memory and settings")
    ap.add_argument("--choose", action="store_true", help="pick the modules again in a menu")
    ap.add_argument("--list", action="store_true", help="describe every module and exit")
    ap.add_argument("--modules", help="exactly these modules, comma-separated (core is always in)")
    ap.add_argument("--without", help="the current choice minus these modules")
    ap.add_argument("--all", action="store_true", help="every module")
    ap.add_argument("--no-claude", action="store_true", help="don't install Claude Code's skill «antigravity»")
    ap.add_argument("--lang", choices=("ru", "en"), help="language of the menu and the module descriptions")
    ap.add_argument("--plain", action="store_true", help="line-by-line output: no colours, frames or key menus")
    ap.add_argument("--demo", action="store_true", help="play the whole install with made-up data; changes nothing")
    a = ap.parse_args(argv)
    lang = language(a.lang)
    caps = tui.detect(plain=a.plain, encoding=encoding)
    ui = tui.UI(caps, lang=lang)
    try:
        if ui.rich:  # one frame for every failure of the rich branches (plain keeps its tracebacks for scripts)
            try:
                return dispatch(a, lang, ui)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                ui.box(UI_TEXT[lang]["failed"], [f"{type(e).__name__}: {e}", UI_TEXT[lang]["failed_hint"]], "fail")
                return 1
        return dispatch(a, lang, ui)
    except KeyboardInterrupt:
        ui.close()
        t = UI_TEXT[lang]
        msg = t["cancel_late"].format(cmd=script_cmd()) if PROGRESS["copying"] else t["cancel"]
        if ui.rich:
            ui.line()
            ui.note(msg, "warn" if PROGRESS["copying"] else "skip")
        else:
            print("\n" + msg, file=sys.stderr)
        return 130
    finally:
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
