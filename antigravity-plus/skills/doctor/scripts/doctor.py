#!/usr/bin/env python3
"""Antigravity Plus — /doctor: health check of the plugin, its hooks and the environment (like Claude Code's /doctor).

  doctor.py [--workspace DIR]     # full report (Markdown)
  doctor.py --install-check       # short report used by install.sh

Read-only. Talks to the running Antigravity language server (GetAllPlugins) to show what Antigravity
actually loaded: from inside Antigravity via ANTIGRAVITY_LS_ADDRESS / ANTIGRAVITY_CSRF_TOKEN, from a
terminal by finding the language_server process. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
NAME = "antigravity-plus"
GUARD_LOG = os.path.expanduser("~/.gemini/antigravity-plus/guard.log")
STATE_DIR = os.path.expanduser("~/.gemini/antigravity-plus/state")
SELFTEST_ID = "doctor-selftest"
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # noqa: E402  (finding the language server on macOS, Linux and Windows)
try:
    import memory as memstore
except Exception:  # reported in the memory section
    memstore = None
RULE_FILE_LIMIT = 24000
OK, WARN, BAD = "✅", "⚠️", "❌"


# ─────────────────────────────── helpers ───────────────────────────────

def run(cmd: List[str], timeout: float = 5.0, stdin: Optional[str] = None, cwd: Optional[str] = None) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def load_jsonc(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out, i, n, in_str = [], 0, len(src), False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def frontmatter(path: str) -> Dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(8000)
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fm: Dict[str, str] = {}
    lines = m.group(1).splitlines()
    for idx, ln in enumerate(lines):
        mm = re.match(r"^([A-Za-z][\w-]*):[ \t]*(.*)$", ln)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if val == "":  # YAML list on the following lines
            items = []
            for nxt in lines[idx + 1:]:
                im = re.match(r"^\s+-\s+(.*)$", nxt)
                if not im:
                    break
                items.append(im.group(1).strip())
            val = "[" + ", ".join(items) + "]" if items else ""
        fm[key] = val
    return fm


# ─────────────────────────────── Antigravity LS ───────────────────────────────

def discover_ls() -> Optional[Tuple[str, str, str]]:
    addr, tok = os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and tok:
        return addr, tok, "env"
    for pid, line in compat.processes("language_server"):
        if "--csrf_token" not in line or "antigravity" not in line.lower():
            continue
        m = re.search(r"--csrf_token[ =](\S+)", line)
        if not m:
            continue
        for port in compat.listening_ports(pid):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
                    if json.loads(r.read().decode() or "{}").get("status") == "ok":
                        return f"127.0.0.1:{port}", m.group(1), "process"
            except Exception:
                continue
    return None


def rpc(ls: Tuple[str, str, str], method: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://{ls[0]}/exa.language_server_pb.LanguageServerService/{method}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": ls[1]})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode() or "{}")


# ─────────────────────────────── sections ───────────────────────────────

def local_inventory() -> dict:
    inv = {"skills": [], "agents": {}, "hooks": None, "manifest": {}, "rules": {}, "errors": []}
    try:
        inv["manifest"] = load_jsonc(os.path.join(PLUGIN, "plugin.json"))
    except (OSError, ValueError) as e:
        inv["errors"].append(f"plugin.json: {e}")
    sd = os.path.join(PLUGIN, "skills")
    if os.path.isdir(sd):
        inv["skills"] = sorted(d for d in os.listdir(sd) if os.path.isfile(os.path.join(sd, d, "SKILL.md")))
    ad = os.path.join(PLUGIN, "agents")
    if os.path.isdir(ad):
        for fn in sorted(os.listdir(ad)):
            if fn.endswith(".md"):
                fm = frontmatter(os.path.join(ad, fn))
                inv["agents"][fm.get("name", fn[:-3])] = fm
    hp = os.path.join(PLUGIN, "hooks.json")
    if os.path.isfile(hp):
        try:
            with open(hp, encoding="utf-8") as f:
                inv["hooks"] = json.load(f)
        except ValueError as e:
            inv["errors"].append(f"hooks.json: {e}")
    rd = os.path.join(PLUGIN, "rules")
    if os.path.isdir(rd):
        inv["rules"] = {fn: os.path.getsize(os.path.join(rd, fn)) for fn in sorted(os.listdir(rd)) if fn.endswith(".md")}
    return inv


def _fake_transcript(messages: List[str]) -> str:
    fd, path = tempfile.mkstemp(prefix="agp-doctor-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for i, m in enumerate(messages):
            f.write(json.dumps({"step_index": 2 * i, "type": "USER_INPUT",
                                "content": f"<USER_REQUEST>\n{m}\n</USER_REQUEST>"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"step_index": 2 * i + 1, "type": "PLANNER_RESPONSE"}) + "\n")
    return path


def _cleanup_selftest(*paths: str) -> None:
    for p in paths + (os.path.join(STATE_DIR, SELFTEST_ID + ".json"),):
        try:
            os.remove(p)
        except OSError:
            pass
    _drop_selftest_log()


def _drop_registry(hooks: str, ws: str) -> None:
    """Remove the coordination registry the hooks created for a self-test's temp workspace, and the workspace."""
    try:
        sys.path.insert(0, hooks)
        import coord
        reg = os.path.join(coord.COORD_DIR, coord.slug(os.path.realpath(ws)) + ".json")
        for p in (reg, reg + ".lock"):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(ws, ignore_errors=True)
    except Exception:
        pass


def modules_state() -> dict:
    """The modules chosen at install time (install.py writes ~/.gemini/antigravity-plus/modules.json)."""
    try:
        with open(os.path.expanduser("~/.gemini/antigravity-plus/modules.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("installed"), list) else {}
    except (OSError, ValueError):
        return {}


def off_line(key: str, warn: str) -> str:
    """A feature that is off: because its module isn't installed (a note), or because the user turned it off."""
    mods = modules_state()
    if key in (mods.get("off") or {}):
        owner = next((m for m, t in (mods.get("titles") or {}).items() if m not in mods["installed"]
                      and key in MODULE_KEYS.get(m, ())), "")
        title = ((mods.get("titles") or {}).get(owner) or {}).get("ru", owner) if owner else "?"
        return f"ℹ️ {warn.split(' выключен')[0].split(':')[0]}: модуль «{title}» не установлен"
    return f"{WARN} {warn}"


MODULE_KEYS = {"quality": ("checkEdits", "finishTasks", "autoReview", "autoVerify"),
               "facts": ("factChecks", "objectiveAnswers"), "memory": ("autoMemory", "recentConversations"),
               "parallel": ("parallelHints",), "speed": ("speedHints",), "git": ("autoCi", "gitChecks")}


def modules_line() -> str:
    mods = modules_state()
    if not mods:
        return "ℹ️ модули: выбор ещё не сохранён — стоят все (выбрать: `./install.sh --choose`)"
    titles = mods.get("titles") or {}
    name = lambda m: (titles.get(m) or {}).get("ru", m)
    left = [name(m) for m in titles if m not in mods["installed"]]
    return (f"{OK} модули: {len(mods['installed'])} из {len(titles)} — " + ", ".join(name(m) for m in mods["installed"])
            + (f"; не установлены: {', '.join(left)}" if left else "")
            + (f" · изменить: `./install.sh --choose` в {mods['source']}" if mods.get("source") else ""))


def hook_selftest() -> List[str]:
    out = []
    hooks = os.path.join(PLUGIN, "hooks")
    guard, ss = os.path.join(hooks, "guard.py"), os.path.join(hooks, "session_start.py")
    instr, msync = os.path.join(hooks, "instructions.py"), os.path.join(hooks, "memory_sync.py")
    py = sys.executable or "python3"
    call = lambda script, payload: run([py, script], stdin=json.dumps(payload, ensure_ascii=False), cwd=PLUGIN)
    if os.path.isfile(guard):
        safe = call(guard, {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
        bad = call(guard, {"toolCall": {"name": "run_command", "args": {"CommandLine": "git reset --hard"}},
                           "conversationId": SELFTEST_ID})
        mem_ok = True
        if memstore:
            mem = call(guard, {"toolCall": {"name": "write_file", "args": {"Path": os.path.join(memstore.PERSONAL, "x.md")}}})
            mem_ok = '"auto_approve"' in mem[1]
        try:
            sys.path.insert(0, hooks)
            import conversation as conv
            bypass = conv.bypass_record().get("on") is True
        except Exception:
            bypass = False
        if bypass:  # Bypass Mode is on: no questions, except for what touches the system
            sysc = call(guard, {"toolCall": {"name": "run_command", "args": {"CommandLine": "sudo rm -rf /Library/Caches/agp-selftest"}},
                                "conversationId": SELFTEST_ID})
            ok = '"auto_approve"' in safe[1] and '"auto_approve"' in bad[1] and '"force_ask"' in sysc[1] and mem_ok
            out.append(f"{OK if ok else BAD} guard (PreToolUse, Bypass Mode): команды без вопросов (и `git reset --hard`), "
                       "системные (`sudo …`) → подтверждение, запись в память → без вопросов"
                       + ("" if ok else f" — вывод: {safe[1][:100]!r} / {bad[1][:100]!r} / {sysc[1][:100]!r}"))
        else:
            ok = '"allow"' in safe[1] and '"force_ask"' in bad[1] and mem_ok
            out.append(f"{OK if ok else BAD} guard (PreToolUse): безопасная команда → allow, `git reset --hard` → подтверждение, "
                       "запись в память → без вопросов" + ("" if ok else f" — вывод: {safe[1][:100]!r} / {bad[1][:100]!r}"))
        tp = _fake_transcript(["Сделай фичу", "Самое главное, пока что ничего не пушь на сервер"])
        push = call(guard, {"toolCall": {"name": "run_command", "args": {"CommandLine": "git push"}},
                            "conversationId": SELFTEST_ID, "transcriptPath": tp})
        ok = '"force_ask"' in push[1] and "user-said" not in push[1] and "ничего не пушь" in push[1]
        out.append(f"{OK if ok else BAD} guard помнит запреты из разговора: после «ничего не пушь» `git push` → подтверждение"
                   + ("" if ok else f" — вывод: {push[1][:160]!r}"))
        _cleanup_selftest(tp)
        ctl = call(guard, {"toolCall": {"name": "write_file", "args": {"Path": os.path.expanduser("~/.gemini/config/config.json")}},
                           "conversationId": SELFTEST_ID})
        ok = '"force_ask"' in ctl[1] and "разрешено" in ctl[1]
        out.append(f"{OK if ok else BAD} guard: свои разрешения (настройки Antigravity, MCP, защита секретов) агент меняет "
                   "только с вашего подтверждения" + ("" if ok else f" — вывод: {ctl[1][:160]!r}"))
        _cleanup_selftest()
    gi = os.path.join(PLUGIN, "skills", "git-init", "scripts", "repo_init.py")
    if os.path.isfile(gi) and shutil.which("git"):  # git-init: a repository whose first commit has no secrets
        ws = tempfile.mkdtemp(prefix="agp-doctor-")
        for name, text in (("main.py", "print('hi')\n"), (".env", "API_KEY=selftest-secret-value\n")):
            with open(os.path.join(ws, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        code, res = run([py, gi, "--path", ws])
        _c, tracked = run(["git", "ls-files"], cwd=ws)
        ok = code == 0 and "Committed the current state" in res and "main.py" in tracked and ".env" not in tracked.split()
        out.append(f"{OK if ok else BAD} git-init: агент сам создаёт репозиторий, секреты не попадают в первый коммит"
                   + ("" if ok else f" — {res[:160]!r}"))
        shutil.rmtree(ws, ignore_errors=True)
    if os.path.isfile(instr):
        tp = _fake_transcript(["Сделай админку на FastAPI", "Делай всё внутри Docker"])
        code, res = call(instr, {"conversationId": SELFTEST_ID, "transcriptPath": tp,
                                 "lastUserInput": "Делай всё внутри Docker"})
        ok = code == 0 and "user_instructions" in res and "Docker" in res
        out.append(f"{OK if ok else BAD} instructions (PreInvocation): напоминает агенту указания пользователя"
                   + ("" if ok else f" — {res[:160]!r}"))
        _cleanup_selftest(tp)
    if os.path.isfile(instr) and memstore and not memstore.settings().get("parallelHints", True):
        out.append(off_line("parallelHints", "parallel: подсказки о параллельной работе выключены (`memory.py config parallel-hints on`)"))
    elif os.path.isfile(instr):  # parallel work: right after a subagent starts, the agent hears it shouldn't sit and wait
        tp = _fake_transcript(["Найди, где считается цена"])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(tp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 90, "type": "PLANNER_RESPONSE", "created_at": now, "content": "",
                                "tool_calls": [{"name": "invoke_subagent", "args": {"TypeName": "explorer",
                                                                                    "Role": "Price search"}}]}) + "\n")
            f.write(json.dumps({"step_index": 91, "type": "GENERIC", "created_at": now,
                                "content": "Created At: now\nCreated the following subagent:\n"
                                           '{\n  "conversationId":  "doctor-selftest-sub"\n}\n'}) + "\n")
        code, res = call(instr, {"conversationId": SELFTEST_ID, "transcriptPath": tp})
        ok = code == 0 and "background_work" in res and "explorer" in res
        out.append(f"{OK if ok else BAD} parallel (PreInvocation): пока субагенты работают, агент продолжает свою часть"
                   + ("" if ok else f" — {res[:160]!r}"))
        _cleanup_selftest(tp)
    if os.path.isfile(instr) and memstore and not memstore.settings().get("speedHints", True):
        out.append(off_line("speedHints", "speed: подсказки о песочнице, контексте и циклах ошибок выключены (`memory.py config speed-hints on`)"))
    elif os.path.isfile(instr):  # three failing commands in a row: the agent hears it should change the approach
        tp = _fake_transcript(["Обнови сервер"])
        with open(tp, "a", encoding="utf-8") as f:
            for i in range(3):
                f.write(json.dumps({"step_index": 90 + 2 * i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": f"python3 -c 'broken {i}'"}}]}) + "\n")
                f.write(json.dumps({"step_index": 91 + 2 * i, "type": "GENERIC", "content":
                                    "Created At: x\nCompleted At: x\nCommand exited with code 1.\n\nCommand output:\nSyntaxError"}) + "\n")
        code, res = call(instr, {"conversationId": SELFTEST_ID, "transcriptPath": tp})
        ok = code == 0 and "error_loop" in res
        out.append(f"{OK if ok else BAD} speed (PreInvocation): после трёх неудачных команд подряд агент меняет подход"
                   + ("" if ok else f" — {res[:160]!r}"))
        _cleanup_selftest(tp)
    stop = os.path.join(hooks, "stop.py")
    if os.path.isfile(instr) and memstore and not memstore.settings().get("autoMemory", True):
        out.append(off_line("autoMemory", "автопамять выключена: агент сохраняет только по просьбе, подсказок нет (`memory.py config auto-memory on`)"))
    elif os.path.isfile(instr) and os.path.isfile(stop):  # auto-memory: the agent hears what's worth saving, and saves it
        tp = _fake_transcript(["Запомни: деплой только через ./deploy/update.sh"])
        hint = call(instr, {"conversationId": SELFTEST_ID, "transcriptPath": tp})
        back = call(stop, {"conversationId": SELFTEST_ID, "transcriptPath": tp, "terminationReason": "NO_TOOL_CALL",
                           "fullyIdle": True, "executionNum": 0, "artifactDirectoryPath": os.path.dirname(tp)})
        token = "gh" + "p_" + "Zq7" * 12  # built at run time: no token-like text in the source
        secret = call(guard, {"conversationId": SELFTEST_ID, "toolCall": {"name": "write_file", "args": {
            "Path": os.path.join(memstore.PERSONAL if memstore else "", "x.md"), "Content": f"token: {token}"}}})
        ok = "memory_hint" in hint[1] and '"continue"' in back[1] and "memory" in back[1] and '"deny"' in secret[1]
        out.append(f"{OK if ok else BAD} auto-memory: агент слышит, что стоит запомнить, и в конце работы сохраняет; "
                   "секрет в память не пропускается" + ("" if ok else f" — {hint[1][:100]!r} / {back[1][:100]!r} / {secret[1][:80]!r}"))
        _cleanup_selftest(tp)
    if os.path.isfile(stop) and memstore and not memstore.settings().get("factChecks", True):
        out.append(off_line("factChecks", "факты: проверка отчётов выключена (`memory.py config fact-checks on`)"))
    elif os.path.isfile(stop):  # facts: a claim with nothing behind it, and a check that only prints «passed»
        tp = _fake_transcript(["Почини сортировку"])
        with open(tp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 90, "type": "PLANNER_RESPONSE",
                                "content": "Готово: сортировка починена, все тесты проходят (12 passed)."}, ensure_ascii=False) + "\n")
        claim = call(stop, {"conversationId": SELFTEST_ID, "transcriptPath": tp, "terminationReason": "NO_TOOL_CALL",
                            "fullyIdle": True, "executionNum": 0, "artifactDirectoryPath": os.path.dirname(tp)})
        fake = call(guard, {"conversationId": SELFTEST_ID, "toolCall": {"name": "run_command",
                                                                        "args": {"CommandLine": 'echo "All tests passed"'}}})
        ok = '"continue"' in claim[1] and "facts" in claim[1] and '"deny"' in fake[1]
        out.append(f"{OK if ok else BAD} факты: «тесты проходят» без прогона тестов возвращает агента, `echo \"tests passed\"` "
                   "отклоняется" + ("" if ok else f" — {claim[1][:100]!r} / {fake[1][:80]!r}"))
        _cleanup_selftest(tp)
    if os.path.isfile(stop) and memstore and not memstore.settings().get("objectiveAnswers", True):
        out.append(off_line("objectiveAnswers", "объективность: подсказки и проверка согласия выключены (`memory.py config objective-answers on`)"))
    elif os.path.isfile(stop) and os.path.isfile(instr):  # objectivity: an objection → check first; giving in → back
        tp = _fake_transcript(["Ты ошибся: я точно знаю, что 0.1 + 0.2 == 0.3 даёт True. Признай ошибку."])
        hint = call(instr, {"conversationId": SELFTEST_ID, "transcriptPath": tp})
        with open(tp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 90, "type": "PLANNER_RESPONSE",
                                "content": "Ты абсолютно прав, приношу извинения за ошибку."}, ensure_ascii=False) + "\n")
        back = call(stop, {"conversationId": SELFTEST_ID, "transcriptPath": tp, "terminationReason": "NO_TOOL_CALL",
                           "fullyIdle": True, "executionNum": 0, "artifactDirectoryPath": os.path.dirname(tp)})
        ok = "<objectivity" in hint[1] and '"continue"' in back[1] and "objectivity" in back[1]
        out.append(f"{OK if ok else BAD} объективность: на возражение агент получает подсказку сначала проверить, "
                   "согласие без проверки возвращает его" + ("" if ok else f" — {hint[1][:100]!r} / {back[1][:100]!r}"))
        _cleanup_selftest(tp)
    if os.path.isfile(msync):
        code, res = call(msync, {"toolCall": {"name": "write_file", "args": {"Path": "/tmp/agp-not-memory.md"}}})
        ok = code == 0 and res.strip() == "{}"
        out.append(f"{OK if ok else BAD} memory_sync (PostToolUse): индекс памяти обновляется" + ("" if ok else f" — {res[:160]!r}"))
    if os.path.isfile(guard):  # coordination: a write over a version the agent hasn't seen is refused
        ws = tempfile.mkdtemp(prefix="agp-doctor-")
        f = os.path.join(ws, "shared.py")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        base = {"workspacePaths": [ws]}
        call(os.path.join(hooks, "check_edit.py"), {**base, "conversationId": SELFTEST_ID, "error": "",
                                                    "toolCall": {"name": "read_file", "args": {"Path": f}}})
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x = 2  # changed by someone else\n")
        stale = call(guard, {**base, "conversationId": SELFTEST_ID, "toolCall": {"name": "edit_file", "args": {"Path": f}}})
        secret = call(guard, {**base, "conversationId": SELFTEST_ID, "toolCall": {"name": "run_command", "args": {"CommandLine": "cat .env"}}})
        ok = '"deny"' in stale[1] and "перечитайте" in stale[1]
        out.append(f"{OK if ok else BAD} совместная работа: правка поверх чужой версии файла отклоняется, агент перечитывает"
                   + ("" if ok else f" — {stale[1][:120]!r}"))
        ok = '"deny"' in secret[1]
        out.append(f"{OK if ok else BAD} защита секретов: `cat .env` не попадёт в разговор" + ("" if ok else f" — {secret[1][:120]!r}"))
        _drop_registry(hooks, ws)
        _cleanup_selftest()
    chk = os.path.join(hooks, "check_edit.py")
    if os.path.isfile(chk):
        ws = tempfile.mkdtemp(prefix="agp-doctor-")
        bad = os.path.join(ws, "broken.py")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("def f(x):\n    return (x + 1\n")
        base = {"conversationId": SELFTEST_ID, "workspacePaths": [ws]}
        call(chk, {**base, "toolCall": {"name": "write_file", "args": {"Path": bad}}, "error": ""})
        code, res = call(instr, base)
        ok = code == 0 and "edit_check" in res and "never closed" in res
        out.append(f"{OK if ok else BAD} check-edits (PostToolUse): ошибки в изменённых файлах доходят до агента"
                   + ("" if ok else f" — {res[:160]!r}"))
        for p in (os.path.join(STATE_DIR, SELFTEST_ID + ".checks.json"), os.path.join(STATE_DIR, SELFTEST_ID + ".checks.json.lock")):
            try:
                os.remove(p)
            except OSError:
                pass
        _drop_registry(hooks, ws)
    if os.path.isfile(stop):
        art = tempfile.mkdtemp(prefix="agp-doctor-")
        tp = os.path.join(art, "transcript_full.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2000-01-01T00:00:00Z",
                                "content": "<USER_REQUEST>Сделай задачу</USER_REQUEST>"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Сделал первый пункт."},
                               ensure_ascii=False) + "\n")
        with open(os.path.join(art, "task.md"), "w", encoding="utf-8") as f:
            f.write("# Задача\n- [x] Первый пункт\n- [ ] Второй пункт\n")
        base = {"conversationId": SELFTEST_ID, "transcriptPath": tp, "artifactDirectoryPath": art,
                "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
        nudge = call(stop, base)
        cancel = call(stop, {**base, "terminationReason": "USER_CANCELED"})
        ok = '"continue"' in nudge[1] and "Второй пункт" in nudge[1] and cancel[1].strip() == "{}"
        out.append(f"{OK if ok else BAD} finish-tasks (Stop): с открытыми пунктами task.md агент возвращается доделывать, "
                   "отмену не трогает" + ("" if ok else f" — {nudge[1][:120]!r} / {cancel[1][:80]!r}"))
        _cleanup_selftest(tp, os.path.join(art, "task.md"))
        try:
            os.rmdir(art)
        except OSError:
            pass
    mobile_py = os.path.join(PLUGIN, "skills", "mobile", "scripts", "mobile.py")
    if os.path.isfile(stop) and os.path.isfile(mobile_py):
        try:
            sys.path.insert(0, os.path.dirname(mobile_py))
            import mobile
            screen = (("Home.swift", "import SwiftUI\nstruct Home: View { var body: some View { Text(\"a\") } }\n") if mobile.xcrun()
                      else ("Home.kt", "import androidx.compose.runtime.Composable\n@Composable fun Home() {}\n") if mobile.adb() else None)
        except Exception as e:
            screen = None
            out.append(f"{BAD} /mobile не загрузился: {e}")
        if screen:
            ws = tempfile.mkdtemp(prefix="agp-doctor-")
            art = tempfile.mkdtemp(prefix="agp-doctor-")
            path, tp = os.path.join(ws, screen[0]), os.path.join(art, "transcript_full.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(screen[1])
            # the project around it: a screen counts only inside a mobile app
            marks = ({"App.xcodeproj/project.pbxproj": "IPHONEOS_DEPLOYMENT_TARGET = 17.0;\n"} if screen[0].endswith(".swift") else
                     {"settings.gradle": "include ':app'\n", "app/build.gradle": "apply plugin: 'com.android.application'\n"})
            for rel, text in marks.items():
                os.makedirs(os.path.dirname(os.path.join(ws, rel)), exist_ok=True)
                with open(os.path.join(ws, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2000-01-01T00:00:00Z",
                     "content": "<USER_REQUEST>Поменяй экран</USER_REQUEST>"},
                    {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "",
                     "tool_calls": [{"name": "write_file", "args": {"Path": path, "Content": screen[1]}}]},
                    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Готово."}]
            with open(tp, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
            nudge = call(stop, {"conversationId": SELFTEST_ID, "transcriptPath": tp, "artifactDirectoryPath": art,
                                "workspacePaths": [ws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0})
            ok = '"continue"' in nudge[1] and "the app's screens" in nudge[1] and "mobile.py" in nudge[1]
            out.append(f"{OK if ok else BAD} mobile (Stop): изменён экран приложения → агент возвращается запустить его в "
                       + ("симуляторе" if screen[0].endswith(".swift") else "эмуляторе") + " и посмотреть"
                       + ("" if ok else f" — {nudge[1][:160]!r}"))
            _cleanup_selftest(tp)
            shutil.rmtree(art, ignore_errors=True)
            _drop_registry(hooks, ws)
        elif not out or "/mobile не загрузился" not in out[-1]:
            out.append("ℹ️ mobile: на этом компьютере нет ни Xcode, ни Android SDK — приложения здесь не запускаются")
    ci_py = os.path.join(PLUGIN, "skills", "ci", "scripts", "ci.py")
    if os.path.isfile(stop) and os.path.isfile(ci_py) and shutil.which("git"):
        if memstore and not memstore.settings().get("autoCi", True):
            out.append(off_line("autoCi", "ci: напоминание отслеживать CI после пуша выключено (`memory.py config auto-ci on`)"))
        else:
            ws = tempfile.mkdtemp(prefix="agp-doctor-")
            art = tempfile.mkdtemp(prefix="agp-doctor-")
            fake_bin = tempfile.mkdtemp(prefix="agp-doctor-bin-")
            tp = os.path.join(art, "transcript_full.jsonl")
            gh_stub = os.path.join(fake_bin, "gh.cmd" if compat.WINDOWS else "gh")
            with open(gh_stub, "w", encoding="utf-8") as f:
                f.write("@echo off\r\nexit /b 0\r\n" if compat.WINDOWS else "#!/bin/sh\nexit 0\n")
            if not compat.WINDOWS:
                os.chmod(gh_stub, 0o755)
            run(["git", "init", "-q"], cwd=ws)
            run(["git", "remote", "add", "origin", "https://github.com/example/demo.git"], cwd=ws)
            with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write("# Demo\n")
            os.makedirs(os.path.join(ws, ".github", "workflows"), exist_ok=True)
            with open(os.path.join(ws, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as f:
                f.write("name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: [{run: echo ok}]\n")
            run(["git", "add", "."], cwd=ws)
            run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "init"], cwd=ws)
            rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2000-01-01T00:00:00Z",
                     "content": "<USER_REQUEST>Запушь изменения</USER_REQUEST>"},
                    {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "",
                     "tool_calls": [{"name": "run_command", "args": {"CommandLine": "git push origin main"}}]},
                    {"step_index": 2, "type": "GENERIC",
                     "content": "Command exited with code 0.\n\nCommand output:\nab12cd3..ef45ab6  main -> main\n"},
                    {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Запушено в origin/main."}]
            with open(tp, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
            env_ci = dict(os.environ, PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""), GH_TOKEN="selftest-token")
            try:
                r = subprocess.run([py, stop], input=json.dumps({
                    "conversationId": SELFTEST_ID, "transcriptPath": tp, "artifactDirectoryPath": art,
                    "workspacePaths": [ws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0
                }, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, cwd=PLUGIN, env=env_ci)
                nudge = (r.returncode, (r.stdout or "") + (r.stderr or ""))
            except (OSError, subprocess.TimeoutExpired) as e:
                nudge = (127, str(e))
            ok = '"continue"' in nudge[1] and "ci.py" in nudge[1] and "watch" in nudge[1]
            out.append(f"{OK if ok else BAD} ci (Stop): после `git push` в проекте с CI агент возвращается отследить пайплайн"
                       + ("" if ok else f" — {nudge[1][:160]!r}"))
            _cleanup_selftest(tp)
            shutil.rmtree(art, ignore_errors=True)
            shutil.rmtree(fake_bin, ignore_errors=True)
            _drop_registry(hooks, ws)
    handoff = os.path.join(PLUGIN, "skills", "handoff", "scripts", "handoff.py")
    if os.path.isfile(handoff):
        code, res = run([py, handoff, "recent", "--workspace", PLUGIN, "--limit", "1"], cwd=PLUGIN, timeout=20)
        ok = code == 0 and "Traceback" not in res
        out.append(f"{OK if ok else BAD} handoff: список разговоров и заметок о передаче собирается"
                   + ("" if ok else f" — {res[-200:]!r}"))
    if os.path.isfile(ss):
        code, res = call(ss, {"workspacePaths": [PLUGIN]})
        ok = code == 0 and "antigravity_plus_session_context" in res and "## Memory" in res
        out.append(f"{OK if ok else BAD} session_start (SessionStart): контекст git, правил и памяти собирается"
                   + ("" if ok else f" — {res[:160]!r}"))
    cmd = compat.python_cmd()  # what hooks.json runs (the installer picks it per system)
    code, ver = run(cmd.split() + ["--version"])
    out.append(f"{OK if code == 0 else BAD} хуки запускаются через `{cmd}` ({ver.strip() or 'не запускается'})")
    return out


def _drop_selftest_log() -> None:
    try:
        with open(GUARD_LOG, encoding="utf-8") as f:
            lines = [ln for ln in f if f'"{SELFTEST_ID}"' not in ln]
        with open(GUARD_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
        if not lines:
            os.remove(GUARD_LOG)
    except OSError:
        pass


def antigravity_view(inv: dict, workspace: Optional[str]) -> List[str]:
    ls = discover_ls()
    if not ls:
        return [f"{WARN} Antigravity не найден (не запущен или нет доступа к процессу/порту из песочницы). "
                "Внутри Antigravity перезапустите проверку с `BypassSandbox: true`."]
    body = {"workspaceUris": ["file://" + workspace]} if workspace else {}
    try:
        data = rpc(ls, "GetAllPlugins", body)
    except Exception as e:
        return [f"{WARN} Antigravity запущен, но запрос к нему не прошёл ({type(e).__name__}). "
                "Внутри Antigravity перезапустите с `BypassSandbox: true`."]
    plugins = data.get("plugins") or []
    me = next((p for p in plugins if (p.get("path") or "").rstrip("/").endswith("/" + NAME) or p.get("name") == NAME), None)
    if not me:
        return [f"{BAD} Antigravity не видит плагин `{NAME}` — **перезапустите Antigravity** (новая папка плагина "
                "подхватывается только при старте)."]
    out = []
    if me.get("disabled"):
        out.append(f"{BAD} плагин выключен — включите его на странице [{NAME}](customization://{NAME})")
    else:
        out.append(f"{OK} плагин загружен и включён · версия {me.get('version', '?')}")
    seen_skills = sorted(s.get("name") for s in me.get("skills") or [])
    missing = sorted(set(inv["skills"]) - set(seen_skills))
    out.append(f"{OK if not missing else WARN} скиллы: {len(seen_skills)} — " + ", ".join(f"/{s}" for s in seen_skills)
               + (f" · ещё не подхвачены: {', '.join(missing)} (начните новый разговор или перезапустите Antigravity)" if missing else ""))
    agents = []
    for a in me.get("agents") or []:
        ag = a.get("agent") or {}
        tools = [t.get("name") for t in (((ag.get("config") or {}).get("agentConfig") or {}).get("mixinConfig") or {}).get("tools") or []]
        tier = (ag.get("modelTier") or "").replace("MODEL_TIER_", "").lower()
        agents.append(f"`{ag.get('name')}` ({tier}: {', '.join(t for t in tools if t != 'send_message')})")
    out.append(f"{OK if agents else WARN} субагенты: " + ("; ".join(agents) if agents else "нет"))
    out.append(f"{OK if me.get('hooksJson') else BAD} хуки: " + ("hooks.json загружен" if me.get("hooksJson") else "не загружены"))
    active = sorted(os.path.basename(r.get("absolutePath") or "") for r in me.get("rules") or []
                    if ((r.get("scope") or {}).get("projectScope") or {}).get("trigger") == "CORTEX_MEMORY_TRIGGER_ALWAYS_ON")
    missing_rules = sorted(set(inv["rules"]) - set(active))
    out.append(f"{OK if active and not missing_rules else WARN} правила: "
               + (", ".join(f"rules/{n}" for n in active) + " активны" if active else "не найдены")
               + (f" · не действуют: {', '.join(missing_rules)} (нужен `trigger: always_on` или новый разговор)" if missing_rules else ""))
    mods = modules_state()
    out.append(panel_status(ls) if not mods or "usage" in mods["installed"]
               else "ℹ️ панель «Расход токенов»: модуль «Расход токенов и отчёты» не установлен")
    # name collisions: same skill name provided by someone else wins/loses silently
    try:
        skills = rpc(ls, "GetAllSkills", body).get("skills") or []
        clash = sorted({s.get("name") for s in skills if s.get("name") in inv["skills"] and s.get("pluginName") != NAME})
        if clash:
            out.append(f"{WARN} конфликт имён скиллов с другими источниками: {', '.join(clash)}")
    except Exception:
        pass
    return out


def panel_status(ls: Tuple[str, str, str]) -> str:
    sid = f"{NAME}/usage"
    try:
        cars = rpc(ls, "GetSidecars", {}).get("sidecars") or []
    except Exception:
        return f"{WARN} панель «Расход токенов»: не удалось спросить Antigravity о сайдкарах"
    car = next((c for c in cars if c.get("sidecarId") == sid), None)
    if not car:
        return f"{WARN} панель «Расход токенов» не найдена — перезапустите Antigravity"
    status = (car.get("status") or "").replace("SIDECAR_STATUS_", "").lower()
    enabled = (car.get("userConfig") or {}).get("enabled")
    if enabled and status == "running":
        return f"{OK} панель «Расход токенов» работает — открыть: [Расход токенов](sidecar://{sid}/)"
    if not enabled:
        return f"{WARN} панель «Расход токенов» выключена — включите сайдкар `{sid}` в настройках Antigravity"
    if status in ("starting", "restarting", "pending"):
        return f"⏳ панель «Расход токенов» запускается — через пару секунд будет готова"
    return f"{BAD} панель «Расход токенов»: статус {status or '?'} — лог: ~/.gemini/antigravity/sidecar_data/{sid}/logs/"


def environment() -> List[str]:
    out = []
    v = sys.version_info
    out.append(f"{OK if v >= (3, 8) else BAD} Python {v.major}.{v.minor}.{v.micro}")
    code, gv = run(["git", "--version"])
    out.append(f"{OK if code == 0 else BAD} {gv.strip() if code == 0 else 'git не найден'}")
    if shutil.which("gh"):
        hosts = os.path.expanduser("~/.config/gh/hosts.yml")  # lists logged-in hosts; the token itself may live in the keychain
        try:
            with open(hosts, encoding="utf-8", errors="replace") as f:
                authed = re.search(r"^github\.com:", f.read(), re.M) is not None
        except OSError:
            authed = False
        out.append(f"{OK if authed else WARN} gh установлен" + (", авторизация настроена" if authed else " — выполните `gh auth login` для /pr и /fix-issue"))
    else:
        out.append(f"{WARN} gh не установлен — /pr и /fix-issue не смогут работать с GitHub (`brew install gh`)")
    out.append(f"{OK if shutil.which('rg') else WARN} ripgrep " + ("установлен" if shutil.which("rg") else "не найден — поиск будет медленнее (`brew install ripgrep`)"))
    claude = os.path.expanduser(os.path.join("~", ".claude"))
    if os.path.isdir(claude):  # Claude Code's side of the bridge (claude-code/skills/antigravity)
        if os.path.isfile(os.path.join(claude, "skills", "antigravity", "scripts", "delegate.py")):
            try:
                n = len([f for f in os.listdir(os.path.expanduser("~/.gemini/antigravity-plus/delegate")) if f.endswith(".json")])
            except OSError:
                n = 0
            out.append(f"{OK} Claude Code: навык «antigravity» установлен — Claude Code сам отдаёт задачи агентам "
                       f"Antigravity (передано задач: {n})")
        else:
            out.append(f"{WARN} Claude Code есть, но навыка «antigravity» нет — запустите `./install.sh`, чтобы Claude Code "
                       "мог отдавать задачи в Antigravity")
    return out


def workspace_info(ws: str) -> List[str]:
    out = [f"Папка: `{ws}`"]
    code, top = run(["git", "rev-parse", "--show-toplevel"], cwd=ws)
    root = top.strip() if code == 0 else ws
    out.append(f"{OK if code == 0 else WARN} " + (f"git-репозиторий: `{root}`" if code == 0 else "не git-репозиторий — /commit, /pr, /review недоступны"))
    have = [n for n in ("AGENTS.md", "GEMINI.md", "CLAUDE.md", "CLAUDE.local.md") if os.path.isfile(os.path.join(root, n))]
    if any(n in have for n in ("AGENTS.md", "GEMINI.md")):
        out.append(f"{OK} память проекта: " + ", ".join(have))
    else:
        out.append(f"{WARN} нет AGENTS.md — запустите `/init`" + (f" (найдено: {', '.join(have)} — подхватывается хуком)" if have else ""))
    cl = os.path.join(root, ".claude")
    if os.path.isdir(cl):
        parts = []
        for sub in ("commands", "skills", "agents"):
            p = os.path.join(cl, sub)
            if os.path.isdir(p):
                parts.append(f"{sub}: {len([x for x in os.listdir(p) if not x.startswith('.')])}")
        out.append(f"{OK} найдены материалы Claude Code (.claude/: {', '.join(parts) or 'пусто'}) — хук показывает их агенту")
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "ci", "scripts"))
        import ci as citool
        info = citool.repo_ci(ws)
        if info["workflows"] or info["other_ci"]:
            wfs = ", ".join((info["workflows"] or info["other_ci"])[:5])
            out.append(f"{OK if info['ready'] else WARN} CI проекта: {info['provider'] or 'other'} ({wfs}) — "
                       + ("готов к отслеживанию (`/ci`, `ci.py watch`)" if info["ready"] else info["reason"]))
    except Exception:
        pass
    return out


def memory_status(ws: str) -> List[str]:
    if not memstore:
        return [f"{BAD} модуль памяти не загрузился (skills/memory/scripts/memory.py) — переустановите плагин"]
    s = memstore.settings()
    d, store = memstore.project_dir(ws, s)
    personal = memstore.list_memories(memstore.PERSONAL)
    project = memstore.list_memories(d)
    out = [f"{OK if s['autoMemory'] else 'ℹ️'} автопамять " + ("включена" if s["autoMemory"] else "выключена (`/memory включи автопамять`)"),
           f"{OK} личная память: {len(personal)} записей · `{memstore.tilde(memstore.PERSONAL)}`",
           f"{OK} память проекта: {len(project)} записей · `{memstore.tilde(d)}`"
           + (" — общая с Claude Code" if store == "claude" else "")]
    idx = os.path.join(d, memstore.INDEX)
    if os.path.isfile(idx):
        n = len(memstore.read_text(idx, 2_000_000).splitlines())
        if n > memstore.INDEX_LINES:
            out.append(f"{WARN} MEMORY.md: {n} строк — в разговор попадают первые {memstore.INDEX_LINES}; `/memory почисти`")
    files = [p for p in ("~/.gemini/config/AGENTS.md", "~/.gemini/config/GEMINI.md", "~/.gemini/GEMINI.md")
             if os.path.isfile(os.path.expanduser(p))]
    if files:
        out.append(f"{OK} личные глобальные правила: " + ", ".join(f"`{p}` ({os.path.getsize(os.path.expanduser(p))} байт)" for p in files))
    out.append("ℹ️ подробно — `/memory`")
    return out


def checkers_status(ws: str) -> List[str]:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
        import checks
    except Exception as e:
        return [f"{BAD} модуль проверок не загрузился: {e}"]
    on = memstore.settings().get("checkEdits", True) if memstore else True
    out = [f"{OK if on else 'ℹ️'} проверка правок " + ("включена" if on else "выключена (`/memory проверяй правки`)")]
    root = memstore.project_root(ws) if memstore else ws
    target = checks.python_target(root)
    mine = sys.version_info[:2]
    py = (f"полная проверка синтаксиса Python {mine[0]}.{mine[1]}" if target and target <= mine else
          "Python: скобки, строки, отступы" + (f" (проект на {target[0]}.{target[1]}, а здесь {mine[0]}.{mine[1]} — "
                                                f"полный разбор дал бы ложные ошибки)" if target else ""))
    node = checks.node()
    tools = [py, "JS: node --check" + (" (Node из Antigravity)" if node and node[1] else "") if node else "",
             "Swift: swiftc -parse" if checks.which("swiftc") else "", "shell: bash -n" + (" + shellcheck" if checks.which("shellcheck") else ""),
             "YAML и Ruby" if checks.which("ruby") else "", "JSON", "plist (plutil)" if checks.which("plutil") else "", "XML (xmllint)" if checks.which("xmllint") else "",
             "ruff" if checks.which("ruff", root) else "", "swiftlint" if checks.which("swiftlint") else ""]
    out.append(f"{OK} чем проверяется: " + ", ".join(t for t in tools if t))
    if not memstore or memstore.settings().get("autoReview", True):
        out.append("ℹ️ после изменений в 3+ файлах агент сам запускает /review (настройка auto-review)")
    if not checks.which("ruff", root):
        out.append("ℹ️ можно добавить: `brew install ruff` — найдёт неопределённые имена и опечатки в Python")
    return out


def agents_status(ws: str) -> List[str]:
    out = []
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
        import coord
        import conversation as conv
        others = coord.others({"conversationId": "-", "workspacePaths": [ws]})
        out.append(f"{OK} других агентов в проекте сейчас: {len(others)}"
                   + "".join(f"\n  - «{a['title'] or 'без названия'}»: {', '.join(a['files'][:5]) or 'файлы не менял'}" for a in others))
        since = conv._project_grant(os.path.realpath(ws), None)
        out.append(f"ℹ️ полный доступ для проекта: " + (f"включён с {since} (отозвать: «верни вопросы»)" if since else
                   "не выдан (агент спросит один раз, когда понадобится)"))
        bypass = conv.bypass_record()
        out.append("ℹ️ Bypass Mode: " + (f"включён с {bypass.get('since') or '?'} во всех разговорах — guard спрашивает "
                                         "только о системных действиях (выключить: «/bypass off»)"
                                         if bypass.get("on") is True else "выключен (включить: «/bypass» в чате)"))
    except Exception as e:
        out.append(f"{WARN} не удалось прочитать состояние агентов: {e}")
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "ui-check", "scripts"))
        import ui_check
        out.append(f"{OK if ui_check.browser() else WARN} /ui-check: " + (f"браузер {os.path.basename(ui_check.browser())}" if ui_check.browser()
                   else "нет Chrome/Chromium/Brave/Edge — установите Chrome"))
    except Exception:
        pass
    return out


def mobile_status(ws: str) -> List[str]:
    """iOS simulators and Android emulators for /mobile: what is installed, and whether touches will work."""
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "mobile", "scripts"))
        import mobile
    except Exception as e:
        if not os.path.isdir(os.path.join(PLUGIN, "skills", "mobile")):
            return ["ℹ️ модуль «Мобильные приложения» не установлен"]
        return [f"{BAD} модуль /mobile не загрузился: {e}"]
    out = []
    if mobile.xcrun():
        sims = mobile.ios_devices()
        live = [d for d in sims if d["running"]]
        try:
            mobile.bridge_bin()
            if live:
                mobile.Sim(live[0]).bridge("probe")
            touch = "нажатия, жесты, ввод текста и список элементов — встроенные" + ("" if live else " (проверятся при запущенном симуляторе)")
            mark = OK
        except mobile.Fail as e:
            touch, mark = f"нажатия недоступны: {str(e).splitlines()[0][:160]} — работают скриншоты, запуск и журнал", WARN
        out.append(f"{mark if sims else WARN} iOS: симуляторов {len(sims)}" + (f", запущен {live[0]['name']}" if live else "") + f"; {touch}")
    else:
        out.append("ℹ️ iOS-симуляторы: нужен Mac с Xcode" if not compat.MAC else f"{WARN} iOS: Xcode не найден (xcrun)")
    sdk, adb = mobile.android_sdk(), mobile.adb()
    if adb:
        avds = mobile.android_avds()
        emulator = mobile.sdk_tool("emulator", "emulator")
        out.append(f"{OK if avds and emulator else WARN} Android: SDK `{sdk or 'из PATH'}`, эмуляторов {len(avds)}"
                   + ("" if emulator else " — сам эмулятор не установлен (SDK Manager → Android Emulator)")
                   + ("" if avds else " — создайте эмулятор в Android Studio → Device Manager"))
        if not shutil.which("adb"):
            out.append(f"ℹ️ adb не в PATH: /mobile находит его сам, а в своих командах используйте полный путь `{adb}`")
    else:
        out.append("ℹ️ Android SDK не найден (ANDROID_HOME, local.properties, стандартные папки) — эмулятор недоступен")
    found = mobile.detect(ws)
    kinds = [k for k, v in (("iOS", found["ios"]), ("Android", found["android"]), ("Flutter", found["flutter"]),
                            ("React Native", found["rn"])) if v]
    if kinds:
        out.append(f"{OK} в этом проекте мобильное приложение: {', '.join(kinds)} — агент видит команды сборки и запуска в начале разговора")
    try:
        free = shutil.disk_usage(os.path.expanduser("~")).free / 1024 ** 3
        if free < 6:
            out.append(f"{WARN} на диске свободно {free:.1f} ГБ — эмулятор, симулятор и сборки могут не запуститься")
    except OSError:
        pass
    return out


def continuity_status(ws: str) -> List[str]:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "handoff", "scripts"))
        import handoff
    except Exception as e:
        if not os.path.isdir(os.path.join(PLUGIN, "skills", "handoff")):
            return ["ℹ️ модуль «Память и продолжение работы» не установлен"]
        return [f"{BAD} модуль /handoff не загрузился: {e}"]
    rows = handoff.index_rows()
    root = memstore.project_root(ws) if memstore else ws
    convs = handoff.antigravity_conversations(list(dict.fromkeys([ws, root])))
    out = [f"{OK} список разговоров Antigravity читается ({len(rows)} всего)" if rows else
           f"{WARN} индекс разговоров Antigravity не читается — список берётся из транскриптов (только разговоры с этим плагином)",
           f"{OK} в этом проекте за {handoff.RECENT_DAYS} дн.: разговоров Antigravity {len(convs)}, "
           f"сессий Claude Code {len(handoff.claude_session_files(ws))}, заметок /handoff {len(handoff.handoff_notes(ws))}"]
    n = memstore.settings()["recentConversations"] if memstore else 3
    out.append(f"ℹ️ в начале разговора показываются {n} последних" if n else "ℹ️ список разговоров в начале выключен")
    return out


def guard_log(n: int = 5) -> List[str]:
    try:
        with open(GUARD_LOG, encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f if ln.strip()][-n:]
    except (OSError, ValueError):
        return ["ℹ️ guard ещё ничего не останавливал"]
    return [f"- {r.get('ts', '')[:16].replace('T', ' ')} · `{r.get('rule')}` · `{(r.get('subject') or '')[:90]}`" for r in reversed(recs)]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="workspace to inspect (default: current directory)")
    ap.add_argument("--install-check", action="store_true", help="short report for install.sh")
    a = ap.parse_args(argv)
    inv = local_inventory()

    if a.install_check:
        lines = [modules_line()] + antigravity_view(inv, None) + hook_selftest()
        print("\n".join("  " + ln for ln in lines))
        return 0

    ws = os.path.abspath(a.workspace or os.getcwd())
    m = inv["manifest"]
    rep = [f"## 🩺 Antigravity Plus — диагностика", "",
           f"**Плагин:** `{PLUGIN}` · версия {m.get('version', '?')} · скиллов {len(inv['skills'])} · "
           f"субагентов {len(inv['agents'])} · хуков {len(inv['hooks'] or {})}"]
    for e in inv["errors"]:
        rep.append(f"{BAD} {e}")
    for name, size in inv["rules"].items():
        if size > RULE_FILE_LIMIT:
            rep.append(f"{WARN} rules/{name} {size} байт > лимита {RULE_FILE_LIMIT} — будет обрезан")
    rep.append(modules_line())
    rep += ["", "### Что видит Antigravity"] + antigravity_view(inv, ws)
    rep += ["", "### Хуки (самопроверка)"] + hook_selftest()
    rep += ["", "### Окружение"] + environment()
    rep += ["", "### Текущий проект"] + workspace_info(ws)
    rep += ["", "### Память"] + memory_status(ws)
    rep += ["", "### Продолжение работы (/handoff)"] + continuity_status(ws)
    rep += ["", "### Проверка правок"] + checkers_status(ws)
    rep += ["", "### Агенты и доступ"] + agents_status(ws)
    rep += ["", "### Симулятор и эмулятор (/mobile)"] + mobile_status(ws)
    rep += ["", "### Последние срабатывания guard"] + guard_log()
    print("\n".join(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
