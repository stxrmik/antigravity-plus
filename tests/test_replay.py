#!/usr/bin/env python3
"""Offline tests for tests/replay.py and tests/live_smoke.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
REPLAY_PY = os.path.join(HERE, "replay.py")
LIVE_SMOKE_PY = os.path.join(HERE, "live_smoke.py")

if len(sys.argv) >= 3:
    PLUGIN, SP = sys.argv[1], sys.argv[2]
    _OWN_SP = False
else:
    PLUGIN = os.path.join(REPO_ROOT, "antigravity-plus")
    SP = tempfile.mkdtemp(prefix="agp-test-replay-")
    _OWN_SP = True

ok = True


def check(name: str, cond: Any, got: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:240]}"))


def _iso(offset_sec: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_sec))


def _write_transcript(path: str, rows: List[Dict[str, Any]], extra_raw: Optional[List[str]] = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        for line in extra_raw or ():
            f.write(line)


def _run_cmd(cmd: List[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def main() -> int:
    try:
        root = os.path.join(SP, "replay_suite")
        shutil.rmtree(root, ignore_errors=True)
        ws = os.path.join(root, "ws")
        os.makedirs(ws, exist_ok=True)

        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# Test Workspace\nUse `pytest` for tests.\n")
        broken_py = os.path.join(ws, "broken.py")
        with open(broken_py, "w", encoding="utf-8") as f:
            f.write("def bad():\n    return (1 + \n")
        ok_py = os.path.join(ws, "ok.py")
        with open(ok_py, "w", encoding="utf-8") as f:
            f.write("def add(a: int, b: int) -> int:\n    return a + b\n")

        # Isolated outer HOME to verify replay.py never writes into caller's HOME.
        caller_home = os.path.join(root, "caller_home")
        os.makedirs(caller_home, exist_ok=True)
        env = dict(
            os.environ,
            HOME=caller_home,
            USERPROFILE=caller_home,
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            AGP_UPDATE_CHECK="off",
            AGP_QUOTA_CHECK="off",
            AGP_LSP_DAEMON="0",
        )

        cid_a = "agp-replay-conv-aaaa"
        tp_a = os.path.join(root, "brain", cid_a, ".system_generated", "logs", "transcript_full.jsonl")
        rows_a: List[Dict[str, Any]] = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": _iso(-120),
                "content": "<USER_REQUEST>Где у нас считается скидка? И запомни: всегда используй pytest</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "EPHEMERAL_MESSAGE",
                "status": "DONE",
                "created_at": _iso(-119),
                "content": '<grounding source="Antigravity Plus">\nhistorical\n</grounding>',
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-110),
                "content": "",
                "tool_calls": [
                    {"name": "write_file", "args": {"Path": broken_py, "Content": "def bad():\n    return (1 + \n", "Overwrite": True}}
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": _iso(-108),
                "content": "Created broken.py",
                "error": "",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-100),
                "content": "",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": os.path.join(ws, "AGENTS.md")}}
                ],
            },
            {
                "step_index": 5,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": _iso(-98),
                "content": "File AGENTS.md read.",
                "error": "",
            },
            {
                "step_index": 6,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": _iso(-90),
                "content": "<USER_REQUEST>Мне кажется, дело именно в кэше. Не пушь изменения на сервер.</USER_REQUEST>",
            },
            {
                "step_index": 7,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-80),
                "content": "",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "git reset --hard HEAD"}},
                    {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                    {"name": "run_command", "args": {"CommandLine": 'echo "tests passed"'}},
                    {"name": "read_file", "args": {"Path": os.path.join(ws, ".env")}},
                ],
            },
            {
                "step_index": 8,
                "type": "GENERIC",
                "status": "ERROR",
                "created_at": _iso(-78),
                "content": "",
                "error": "tool call denied by pre-tool hook: blocked",
            },
            {"step_index": 9, "type": "GENERIC", "status": "DONE", "created_at": _iso(-77), "content": "ok", "error": ""},
            {"step_index": 10, "type": "GENERIC", "status": "ERROR", "created_at": _iso(-76), "content": "", "error": "tool call denied by pre-tool hook"},
            {"step_index": 11, "type": "GENERIC", "status": "ERROR", "created_at": _iso(-75), "content": "", "error": "tool call denied by pre-tool hook"},
            {
                "step_index": 12,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": _iso(-70),
                "content": "Stop hook blocked termination: old historical block",
            },
            {
                "step_index": 13,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-60),
                "content": "Готово, все тесты прошли.",
            },
        ]
        _write_transcript(tp_a, rows_a, extra_raw=["{not valid json\n"])

        cid_b = "agp-replay-conv-bbbb"
        tp_b = os.path.join(root, "brain", cid_b, ".system_generated", "logs", "transcript_full.jsonl")
        rows_b: List[Dict[str, Any]] = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": _iso(-50),
                "content": "<USER_REQUEST>Проверь проект и ответь</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-45),
                "content": "",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": ok_py}},
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": _iso(-44),
                "content": "def add...",
                "error": "",
            },
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": _iso(-40),
                "content": "Готово, all tests pass, проверил [отчёт](missing_report.py).",
            },
        ]
        _write_transcript(tp_b, rows_b)

        save1 = os.path.join(root, "save1.json")
        r = _run_cmd(
            [
                sys.executable,
                REPLAY_PY,
                "--plugin",
                PLUGIN,
                "--workspace",
                ws,
                "--transcript",
                tp_a,
                "--transcript",
                tp_b,
                "--save",
                save1,
                "--workers",
                "2",
            ],
            env,
        )
        check("replay exits 0 on synthetic transcripts", r.returncode == 0, r.stderr + r.stdout)
        check("replay does not pollute caller HOME", not os.path.exists(os.path.join(caller_home, ".gemini")))

        with open(save1, encoding="utf-8") as f:
            save_txt = f.read()
        data = json.loads(save_txt)
        check("saved JSON counts 2 main conversations", data.get("conversations") == 2 and data.get("main_conversations") == 2, data)
        check("saved JSON counts 1 malformed line and 0 hook errors", data.get("malformed_lines") == 1 and data.get("hook_errors") == 0, data)
        check("saved JSON contains no user request text", "pytest" not in save_txt and "кэше" not in save_txt)

        hooks = data.get("hooks") or {}
        check("session_start fired twice", hooks.get("session_start", {}).get("fired") == 2, hooks.get("session_start"))
        check("check_edit ran and queued finding", hooks.get("check_edit", {}).get("fired", 0) >= 1, hooks.get("check_edit"))
        check("stop triggered continue at least twice", hooks.get("stop", {}).get("fired", 0) >= 2, hooks.get("stop"))

        hints = data.get("hints") or {}
        for h in ("grounding", "memory_hint", "edit_check", "user_instructions", "objectivity"):
            check(f"hint <{h}> counted", hints.get(h, 0) >= 1, hints)

        gd = data.get("guard_decisions") or {}
        check("guard decisions include force_ask, deny, allow", gd.get("force_ask", 0) >= 2 and gd.get("deny", 0) >= 2 and gd.get("allow", 0) >= 2, gd)

        gr = data.get("guard_rules") or {}
        for rule in ("git-reset-hard", "user-said-no-push", "fake-check", "secret-read"):
            check(f"guard rule {rule} counted", gr.get(rule, 0) >= 1, gr)

        sr = data.get("stop_reasons") or {}
        check("stop category syntax counted", sr.get("syntax", 0) >= 1, sr)
        check("stop category facts counted", sr.get("facts", 0) >= 1, sr)

        # --compare same run -> No differences.
        rc_same = _run_cmd(
            [
                sys.executable,
                REPLAY_PY,
                "--plugin",
                PLUGIN,
                "--workspace",
                ws,
                "--transcript",
                tp_a,
                "--transcript",
                tp_b,
                "--compare",
                save1,
                "--workers",
                "1",
            ],
            env,
        )
        check("--compare against identical baseline reports No differences", rc_same.returncode == 0 and "No differences." in rc_same.stdout, rc_same.stdout)

        # --compare against modified baseline -> shows delta.
        save2 = os.path.join(root, "save2.json")
        mod = json.loads(save_txt)
        mod["hints"]["grounding"] = 0
        with open(save2, "w", encoding="utf-8") as f:
            json.dump(mod, f)
        rc_diff = _run_cmd(
            [
                sys.executable,
                REPLAY_PY,
                "--plugin",
                PLUGIN,
                "--workspace",
                ws,
                "--transcript",
                tp_a,
                "--transcript",
                tp_b,
                "--compare",
                save2,
                "--workers",
                "1",
            ],
            env,
        )
        check("--compare against changed baseline prints delta", rc_diff.returncode == 0 and "grounding" in rc_diff.stdout and "(+1)" in rc_diff.stdout, rc_diff.stdout)

        # --show KIND checks (hint, guard rule, stop category).
        for kind, must_have in (
            ("grounding", "[hint:grounding]"),
            ("git-reset-hard", "[guard:force_ask:git-reset-hard]"),
            ("syntax", "[stop:syntax]"),
        ):
            rs = _run_cmd(
                [
                    sys.executable,
                    REPLAY_PY,
                    "--plugin",
                    PLUGIN,
                    "--workspace",
                    ws,
                    "--transcript",
                    tp_a,
                    "--show",
                    kind,
                    "--workers",
                    "1",
                ],
                env,
            )
            lines = [ln for ln in rs.stdout.splitlines() if f"[{kind}" in ln or must_have in ln]
            within_limit = bool(lines) and all(len(ln.split("] ", 1)[-1]) <= 200 for ln in lines)
            check(f"--show {kind} prints matching spot <= 200 chars", rs.returncode == 0 and cid_a in rs.stdout and "step=" in rs.stdout and within_limit, rs.stdout)

        # --limit 1 check.
        r_lim = _run_cmd(
            [
                sys.executable,
                REPLAY_PY,
                "--plugin",
                PLUGIN,
                "--workspace",
                ws,
                "--transcript",
                tp_a,
                "--transcript",
                tp_b,
                "--limit",
                "1",
                "--workers",
                "1",
            ],
            env,
        )
        check("--limit 1 restricts replay to 1 conversation", r_lim.returncode == 0 and "1 conversations" in r_lim.stdout, r_lim.stdout)

        # live_smoke.py --list check.
        r_smoke = _run_cmd([sys.executable, LIVE_SMOKE_PY, "--list"], env)
        check("live_smoke.py --list exits 0 and lists scenarios", r_smoke.returncode == 0 and "lsp_refs" in r_smoke.stdout and "mcp_tools" in r_smoke.stdout, r_smoke.stdout + r_smoke.stderr)

        import importlib.util as _imp
        _spec = _imp.spec_from_file_location("agp_replay_mod", REPLAY_PY)
        _rmod = _imp.module_from_spec(_spec)  # type: ignore[arg-type]
        _spec.loader.exec_module(_rmod)  # type: ignore[union-attr]
        sample_uuid = "12345678-1234-1234-1234-123456789abc"
        home_dir = os.path.expanduser("~")
        masked = _rmod._mask_excerpt(
            f"password=superSecretVal --token myTok9999 {home_dir}/work «привет секрет» "
            f"ab12cd34ef56gh78ij90kl12mn34 {sample_uuid}",
            limit=300,
        )
        check("_mask_excerpt masks assignments, flags, long tokens, guillemets, and HOME while keeping UUID",
              "superSecretVal" not in masked and "myTok9999" not in masked
              and "ab12cd34ef56" not in masked and "привет секрет" not in masked
              and (len(home_dir) <= 1 or home_dir not in masked) and "~/work" in masked
              and sample_uuid in masked and "«…»" in masked, masked)

        print("ALL OK" if ok else "FAILURES")
        return 0 if ok else 1
    finally:
        if _OWN_SP:
            shutil.rmtree(SP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
