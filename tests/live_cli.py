#!/usr/bin/env python3
r"""Antigravity Plus — live probe of the Antigravity CLI (`agy`).

Runs a minimal diagnostic against a local `agy` installation without installing
anything or modifying user/global settings:
  1. Locate `agy` and read its version (`compat.agy()`, `compat.agy_version()`).
  2. Run one cheap headless `agy -p` probe in a fresh temporary workspace using
     `--output-format stream-json` (never `--dangerously-skip-permissions`).
  3. Compare `init.tools[]` against `PreToolUse` matcher regexes in both
     `antigravity-plus/hooks.json` and the installed plugin copy (if present).
  4. Check whether Antigravity Plus hooks fired for that conversation
     (`~/.gemini/antigravity-plus/state/<cid>.json` and/or `guard.log`).
  5. Locate `transcript*.jsonl` under `compat.data_dirs()` and
     `~/.gemini/antigravity-cli`, and list observed top-level `type` values.
  6. Optional (`--hook-env`): write a temporary `.agents/hooks.json` in the
     temp workspace with a `SessionStart` hook that records only env var names
     (sorted, no values) and stdin JSON keys.

To inspect hook environment variables manually in any workspace, add a
`SessionStart` hook in `.agents/hooks.json` that dumps `os.environ.keys()`.

Usage:
  python3 tests/live_cli.py [--model MODEL] [--hook-env] [--json] [--dry-run] [--keep]

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS_DIR = os.path.join(REPO_ROOT, "antigravity-plus", "hooks")

if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

import compat  # type: ignore[import-not-found]  # noqa: E402
import conversation  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_MODEL = "gemini-4-hydrogen-low"
PRINT_TIMEOUT = "180s"
SUBPROCESS_TIMEOUT = 240
PROBE_PROMPT = "Run the shell command `echo agp-probe` once and reply with OK."

CORE_TOOLS = ("run_command", "read_file", "write_file", "edit_file")
REPO_HOOKS_JSON = os.path.join(REPO_ROOT, "antigravity-plus", "hooks.json")
INSTALLED_HOOKS_JSON = os.path.expanduser(
    os.path.join("~", ".gemini", "config", "plugins", "antigravity-plus", "hooks.json")
)
STATE_DIR = getattr(
    conversation,
    "STATE_DIR",
    os.path.expanduser(os.path.join("~", ".gemini", "antigravity-plus", "state")),
)
GUARD_LOG = os.path.expanduser(os.path.join("~", ".gemini", "antigravity-plus", "guard.log"))

_AUTH_TERMS_RE = re.compile(
    r"(authentication required|sign[\s-]?in|log[\s-]?in|not authenticated|accept.*terms|terms of service|unauthorized)",
    re.IGNORECASE,
)


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            reconf = getattr(stream, "reconfigure", None)
            if callable(reconf):
                reconf(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _build_agy_cmd(agy_exe: str, model: str) -> List[str]:
    return [
        agy_exe or "agy",
        "-p",
        PROBE_PROMPT,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--print-timeout",
        PRINT_TIMEOUT,
    ]


def _step_agy_info() -> Tuple[bool, str, Dict[str, Any]]:
    exe = compat.agy()
    ver = compat.agy_version() if exe else ""
    if not exe:
        return False, "agy not found on PATH or in ~/.local/bin", {"path": "", "version": ""}
    ver_label = ver or "unknown"
    return True, f"agy: {exe} (version {ver_label})", {"path": exe, "version": ver}


def _setup_hook_env(ws: str) -> str:
    agents_dir = os.path.join(ws, ".agents")
    os.makedirs(agents_dir, exist_ok=True)
    dump_script = os.path.join(agents_dir, "dump_env.py")
    dump_out = os.path.join(agents_dir, "hook_env_dump.json")
    hooks_file = os.path.join(agents_dir, "hooks.json")

    script_src = (
        "import json, os, sys\n"
        "raw = sys.stdin.read()\n"
        "try:\n"
        "    payload = json.loads(raw) if raw.strip() else {}\n"
        "except ValueError:\n"
        "    payload = {}\n"
        "stdin_keys = sorted(payload.keys()) if isinstance(payload, dict) else []\n"
        "data = {'env_names': sorted(os.environ.keys()), 'stdin_keys': stdin_keys}\n"
        f"with open({dump_out!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump(data, f, indent=2)\n"
        "print('{}')\n"
    )
    with open(dump_script, "w", encoding="utf-8") as f:
        f.write(script_src)

    hooks_cfg = {
        "agp-live-cli-env-probe": {
            "SessionStart": [
                {
                    "type": "command",
                    "command": f"{sys.executable} {dump_script}",
                    "timeout": 10,
                }
            ]
        }
    }
    with open(hooks_file, "w", encoding="utf-8") as f:
        json.dump(hooks_cfg, f, indent=2)
    return dump_out


def _parse_stream_json(stdout: str) -> Dict[str, Any]:
    init_payload: Dict[str, Any] = {}
    result_payload: Dict[str, Any] = {}
    step_updates: List[Dict[str, Any]] = []
    cid = ""
    non_json_lines = 0

    for line in (stdout or "").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except ValueError:
            non_json_lines += 1
            continue
        if not isinstance(ev, dict):
            non_json_lines += 1
            continue

        if not cid and isinstance(ev.get("conversation_id"), str):
            cid = ev["conversation_id"]

        kind = ev.get("event")
        if kind == "init":
            if isinstance(ev.get("init"), dict):
                init_payload = ev["init"]
        elif kind == "step_update":
            su = ev.get("step_update")
            if isinstance(su, dict):
                step_updates.append(su)
                if not cid and isinstance(su.get("conversation_id"), str):
                    cid = su["conversation_id"]
        elif kind == "result":
            res = ev.get("result")
            if isinstance(res, dict):
                result_payload = res
                if not cid and isinstance(res.get("conversation_id"), str):
                    cid = res["conversation_id"]

    return {
        "conversation_id": cid,
        "init": init_payload,
        "result": result_payload,
        "step_updates": step_updates,
        "non_json_lines": non_json_lines,
    }


def _step_headless_run(agy_exe: str, model: str, ws: str) -> Tuple[bool, str, Dict[str, Any]]:
    cmd = _build_agy_cmd(agy_exe, model)
    try:
        proc = subprocess.run(
            cmd,
            cwd=ws,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"agy timed out after {SUBPROCESS_TIMEOUT}s", {"returncode": None, "auth_required": False}
    except OSError as exc:
        return False, f"failed to launch agy: {exc}", {"returncode": None, "auth_required": False}

    parsed = _parse_stream_json(proc.stdout)
    res = parsed["result"]
    combined_err = "\n".join(
        p for p in (proc.stderr or "", str(res.get("error") or ""), proc.stdout or "") if p
    )

    if proc.returncode != 0:
        if _AUTH_TERMS_RE.search(combined_err):
            return (
                False,
                "agy requires sign-in or terms acceptance (run `agy` interactively first)",
                {"returncode": proc.returncode, "auth_required": True, "parsed": parsed},
            )
        err_hint = (str(res.get("error") or "") or (proc.stderr or "").strip() or "non-zero exit").splitlines()[0][:200]
        return (
            False,
            f"agy exited {proc.returncode}: {err_hint}",
            {"returncode": proc.returncode, "auth_required": False, "parsed": parsed},
        )

    status = str(res.get("status") or "")
    cid = parsed["conversation_id"]
    if status and status != "SUCCESS":
        err_hint = str(res.get("error") or status)[:200]
        return (
            False,
            f"headless run ended with status={status}: {err_hint}",
            {"returncode": proc.returncode, "auth_required": False, "parsed": parsed},
        )

    steps_n = len(parsed["step_updates"])
    cid_short = cid[:8] if cid else "none"
    return (
        True,
        f"headless run ok (cid={cid_short}, model={model}, steps={steps_n}, status={status or 'OK'})",
        {"returncode": proc.returncode, "auth_required": False, "parsed": parsed},
    )


def _load_pretool_matchers(path: str) -> Tuple[List[str], str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return [], f"failed to read {path}: {exc}"
    if not isinstance(data, dict):
        return [], f"{path} is not a JSON object"

    matchers: List[str] = []
    for _hook_name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("enabled") is False:
            continue
        for entry in cfg.get("PreToolUse") or []:
            if isinstance(entry, dict) and isinstance(entry.get("matcher"), str):
                matchers.append(entry["matcher"])
    return matchers, ""


def _matcher_matches(pattern: str, tool_name: str) -> bool:
    if pattern in ("", "*"):
        return True
    try:
        return re.fullmatch(pattern, tool_name) is not None
    except re.error:
        return False


def _step_compare_tools(tools: List[str]) -> Tuple[bool, str, Dict[str, Any]]:
    hooks_files: List[Tuple[str, str]] = [("repo", REPO_HOOKS_JSON)]
    if os.path.isfile(INSTALLED_HOOKS_JSON):
        hooks_files.append(("installed", INSTALLED_HOOKS_JSON))

    known_set = set(CORE_TOOLS)
    unknown_tools: List[str] = []
    target_tools: List[str] = []
    for name in tools:
        canon = compat.tool(name)
        if canon in known_set:
            target_tools.append(name)
        else:
            unknown_tools.append(name)

    problems: List[str] = []
    checked: Dict[str, Any] = {}
    for label, path in hooks_files:
        matchers, err = _load_pretool_matchers(path)
        if err:
            problems.append(err)
            checked[label] = {"path": path, "error": err}
            continue
        unmatched = [
            t for t in target_tools
            if not any(_matcher_matches(m, t) for m in matchers)
        ]
        if unmatched:
            problems.append(f"{label} hooks.json misses: {', '.join(unmatched)}")
        checked[label] = {
            "path": path,
            "matchers_count": len(matchers),
            "unmatched": unmatched,
        }

    detail = {
        "tools_count": len(tools),
        "core_tools_seen": target_tools,
        "unknown_tools": unknown_tools,
        "hooks_files": checked,
    }
    if problems:
        return False, "; ".join(problems), detail

    unk_note = f", unknown={len(unknown_tools)} ({', '.join(unknown_tools)})" if unknown_tools else ""
    files_note = "+".join(label for label, _p in hooks_files)
    return (
        True,
        f"hooks.json ({files_note}) matches {len(target_tools)}/{len(target_tools)} core tools{unk_note}",
        detail,
    )


def _guard_log_mentions(cid: str) -> bool:
    if not cid or not os.path.isfile(GUARD_LOG):
        return False
    try:
        with open(GUARD_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if cid in line:
                    return True
    except OSError:
        return False
    return False


def _step_hooks_fired(cid: str) -> Tuple[bool, str, Dict[str, Any]]:
    if not cid:
        return False, "no conversation_id returned; cannot check plugin hook state", {"state_file": "", "in_guard_log": False}
    state_file = os.path.join(STATE_DIR, f"{cid}.json")
    has_state = os.path.isfile(state_file)
    in_log = _guard_log_mentions(cid)
    detail = {"state_file": state_file, "state_exists": has_state, "in_guard_log": in_log}
    if has_state or in_log:
        parts = []
        if has_state:
            parts.append(f"state={state_file}")
        if in_log:
            parts.append("guard.log=matched")
        return True, f"plugin hooks fired ({', '.join(parts)})", detail
    return False, f"no state file ({state_file}) or guard.log entry for {cid}", detail


def _step_transcript(cid: str) -> Tuple[bool, str, Dict[str, Any]]:
    if not cid:
        return False, "no conversation_id returned; cannot locate transcript", {"files": [], "types": []}

    search_roots = list(dict.fromkeys(os.path.normpath(d) for d in (compat.data_dirs() + [compat.cli_data_dir()])))
    found_files: List[str] = []
    seen_types: List[str] = []
    type_set = set()

    for root in search_roots:
        logs_dir = os.path.join(root, "brain", cid, ".system_generated", "logs")
        if not os.path.isdir(logs_dir):
            continue
        for fp in sorted(glob.glob(os.path.join(logs_dir, "transcript*.jsonl"))):
            if os.path.isfile(fp):
                found_files.append(fp)
                try:
                    with open(fp, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            s = line.strip()
                            if not s:
                                continue
                            try:
                                row = json.loads(s)
                            except ValueError:
                                continue
                            if isinstance(row, dict):
                                typ = row.get("type")
                                if isinstance(typ, str) and typ and typ not in type_set:
                                    type_set.add(typ)
                except OSError:
                    pass

    seen_types = sorted(type_set)
    detail = {"roots_checked": search_roots, "files": found_files, "types": seen_types}
    if not found_files:
        return False, f"no transcript*.jsonl found for {cid} under {', '.join(search_roots)}", detail

    basenames = ", ".join(os.path.basename(p) for p in found_files)
    logs_parent = os.path.dirname(found_files[0])
    types_str = ", ".join(seen_types) if seen_types else "(none)"
    return True, f"transcript at {logs_parent} [{basenames}]; types={{{types_str}}}", detail


def _step_hook_env(dump_out: str) -> Tuple[bool, str, Dict[str, Any]]:
    if not os.path.isfile(dump_out):
        return False, f"--hook-env enabled but {dump_out} was not created", {"env_names": [], "stdin_keys": []}
    try:
        with open(dump_out, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return False, f"failed to read {dump_out}: {exc}", {"env_names": [], "stdin_keys": []}

    env_names = [str(x) for x in (data.get("env_names") or [])]
    stdin_keys = [str(x) for x in (data.get("stdin_keys") or [])]
    return (
        True,
        f"SessionStart hook saw {len(env_names)} env vars and stdin keys [{', '.join(stdin_keys)}]",
        {"env_names": env_names, "stdin_keys": stdin_keys},
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="live_cli.py",
        description="Live probe of the Antigravity CLI (agy) and Antigravity Plus hooks.",
        epilog=(
            "Note on hook environment inspection: Antigravity loads workspace hooks from "
            ".agents/hooks.json. Pass --hook-env to write a temporary .agents/hooks.json in "
            "the temp workspace that records SessionStart env var names (no values) and stdin keys."
        ),
    )
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model slug for the headless probe (default: {DEFAULT_MODEL})")
    ap.add_argument("--hook-env", action="store_true", help="Write temporary .agents/hooks.json to dump SessionStart env var names")
    ap.add_argument("--json", action="store_true", help="Print summary as JSON")
    ap.add_argument("--dry-run", action="store_true", help="Print the agy command and run steps 1 and 3 (empty tools) only")
    ap.add_argument("--keep", action="store_true", help="Keep the temporary workspace after the run")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    _reconfigure_stdio()
    args = parse_args(argv)

    summary_items: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        summary_items.append({
            "step": name,
            "ok": ok,
            "icon": "✅" if ok else "⚠️",
            "message": message,
            "detail": detail or {},
        })

    # 1. agy path and version
    agy_ok, agy_msg, agy_detail = _step_agy_info()
    record("agy", agy_ok, agy_msg, agy_detail)
    agy_exe = agy_detail.get("path", "")

    if args.dry_run:
        cmd = _build_agy_cmd(agy_exe, args.model)
        tools_ok, tools_msg, tools_detail = _step_compare_tools([])
        record("tools_match", tools_ok, tools_msg, tools_detail)

        exit_code = 2 if not agy_ok else (0 if all(it["ok"] for it in summary_items) else 1)
        if args.json:
            print(json.dumps({"dry_run": True, "command": cmd, "exit_code": exit_code, "items": summary_items}, indent=2, ensure_ascii=False))
        else:
            print("Dry-run command:", " ".join(cmd))
            for it in summary_items:
                print(f"{it['icon']} {it['message']}")
        return exit_code

    if not agy_ok:
        if args.json:
            print(json.dumps({"exit_code": 2, "items": summary_items}, indent=2, ensure_ascii=False))
        else:
            for it in summary_items:
                print(f"{it['icon']} {it['message']}")
        return 2

    ws = os.path.realpath(tempfile.mkdtemp(prefix="agp-live-cli-"))
    dump_out = ""
    try:
        if args.hook_env:
            dump_out = _setup_hook_env(ws)

        # 2. One cheap headless run
        run_ok, run_msg, run_detail = _step_headless_run(agy_exe, args.model, ws)
        record("headless_run", run_ok, run_msg, run_detail)

        parsed = run_detail.get("parsed") or {}
        cid = str(parsed.get("conversation_id") or "")
        init_tools = list((parsed.get("init") or {}).get("tools") or [])

        if run_ok or init_tools:
            # 3. Compare init.tools[] with hooks.json
            # init.tools[] spells some tools the docs' way (view_file, write_to_file — seen live in agy 1.5.18), while
            # hooks get the names the steps carry (read_file, write_file): those must match; init.tools is a note.
            step_tools = sorted({str(s.get("tool_name")) for s in parsed.get("step_updates") or []
                                 if isinstance(s, dict) and s.get("tool_name")})
            tools_ok, tools_msg, tools_detail = _step_compare_tools(step_tools)
            init_known = [t for t in init_tools if compat.tool(t) in CORE_TOOLS]
            tools_detail["init_tools_core"] = init_known
            tools_msg += f"; steps used {', '.join(step_tools) or 'no tools'}; init.tools core: {', '.join(init_known)}"
            record("tools_match", tools_ok, tools_msg, tools_detail)

        if not run_detail.get("auth_required") and (run_ok or cid):
            # 4. Did plugin hooks fire?
            hooks_ok, hooks_msg, hooks_detail = _step_hooks_fired(cid)
            record("hooks_fired", hooks_ok, hooks_msg, hooks_detail)

            # 5. Where the transcript is
            tr_ok, tr_msg, tr_detail = _step_transcript(cid)
            record("transcript", tr_ok, tr_msg, tr_detail)

            # 6. Optional --hook-env
            if args.hook_env:
                env_ok, env_msg, env_detail = _step_hook_env(dump_out)
                record("hook_env", env_ok, env_msg, env_detail)
    finally:
        if args.keep:
            if not args.json:
                print(f"[kept] workspace={ws}")
        else:
            shutil.rmtree(ws, ignore_errors=True)

    exit_code = 0 if all(it["ok"] for it in summary_items) else 1
    if args.json:
        print(json.dumps({"exit_code": exit_code, "items": summary_items}, indent=2, ensure_ascii=False))
    else:
        for it in summary_items:
            print(f"{it['icon']} {it['message']}")
        if args.hook_env:
            for it in summary_items:
                if it["step"] == "hook_env" and it["detail"].get("env_names"):
                    print("Hook env names:", ", ".join(it["detail"]["env_names"]))
                    print("Hook stdin keys:", ", ".join(it["detail"]["stdin_keys"]))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
