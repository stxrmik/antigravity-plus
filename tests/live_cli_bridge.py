#!/usr/bin/env python3
r"""Live probe for the Claude Code -> Antigravity CLI bridge (`delegate.py --via cli`).

Run this manually after the Antigravity CLI (`agy`) is installed and signed in.
It creates a throw-away git workspace, invokes `delegate.py` end-to-end (`run`,
`diff`, `result`, `send`, `list`), and spends a small amount of real quota.

Usage:
  python3 tests/live_cli_bridge.py [--model MODEL] [--keep]

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DELEGATE = os.path.join(REPO_ROOT, "claude-code", "skills", "antigravity", "scripts", "delegate.py")
DEFAULT_MODEL = "gemini-4-hydrogen-low"
TASK_TIMEOUT = 600


def _find_agy() -> Optional[str]:
    """Locate the `agy` CLI binary without installing or modifying anything."""
    found = shutil.which("agy")
    if found:
        return found
    cand = os.path.expanduser(os.path.join("~", ".local", "bin", "agy"))
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            if os.path.isfile(cand + ext):
                return cand + ext
    return None


def _run_cmd(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _delegate(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run_cmd([sys.executable, "-X", "utf8", DELEGATE, *args], cwd=REPO_ROOT, timeout=timeout)


def _create_temp_repo() -> str:
    ws = os.path.realpath(tempfile.mkdtemp(prefix="agp-cli-bridge-"))
    with open(os.path.join(ws, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("Initial notes.\n")

    git_env = dict(
        os.environ,
        GIT_AUTHOR_NAME="AGP Probe",
        GIT_AUTHOR_EMAIL="probe@example.com",
        GIT_COMMITTER_NAME="AGP Probe",
        GIT_COMMITTER_EMAIL="probe@example.com",
        GIT_CONFIG_NOSYSTEM="1",
    )
    init = _run_cmd(["git", "init", "-b", "main"], cwd=ws, env=git_env, timeout=15)
    if init.returncode != 0:
        _run_cmd(["git", "init"], cwd=ws, env=git_env, timeout=15)
    _run_cmd(["git", "add", "notes.txt"], cwd=ws, env=git_env, timeout=15)
    commit = _run_cmd(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
        cwd=ws,
        env=git_env,
        timeout=15,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {(commit.stderr or commit.stdout).strip()}")
    return ws


def _record(results: List[Tuple[str, bool, str]], label: str, ok: bool, reason: str) -> None:
    icon = "✅" if ok else "❌"
    print(f"{icon} {label}: {reason}", flush=True)
    results.append((label, ok, reason))


def run_probe(model: str, keep: bool) -> int:
    results: List[Tuple[str, bool, str]] = []
    ws: Optional[str] = None
    try:
        ws = _create_temp_repo()
        hello_path = os.path.join(ws, "hello.txt")
        view_cmd = "type hello.txt" if os.name == "nt" else "cat hello.txt"
        brief = (
            f'Тест CLI: create "hello.txt" containing exactly "OK", '
            f'change nothing else, and run `{view_cmd}`.'
        )

        print(f"Probing CLI bridge: model={model} ws={ws}", flush=True)
        run_res = _delegate(
            ["run", "--via", "cli", "--model", model, "--cwd", ws, "--timeout", str(TASK_TIMEOUT), brief],
            timeout=TASK_TIMEOUT + 60,
        )
        run_out = "\n".join(part for part in (run_res.stdout, run_res.stderr) if part)

        _record(
            results,
            "run exit code",
            run_res.returncode == 0,
            f"exit={run_res.returncode}" if run_res.returncode == 0 else f"exit={run_res.returncode} ({run_out.strip()[:160]})",
        )

        has_check_line = any(line.startswith("✅") for line in run_out.splitlines())
        _record(
            results,
            "run output ✅ line",
            has_check_line,
            "found line starting with ✅" if has_check_line else "no line starting with ✅",
        )

        match = re.search(r"Started Antigravity task\s+(\S+)", run_out)
        task_id = match.group(1) if match else ""
        _record(
            results,
            "task id parsed",
            bool(task_id),
            f"id={task_id}" if task_id else "missing 'Started Antigravity task <id>'",
        )

        if os.path.isfile(hello_path):
            with open(hello_path, "r", encoding="utf-8", errors="replace") as f:
                hello_content = f.read()
            hello_ok = hello_content.rstrip("\r\n") == "OK"
            _record(
                results,
                "hello.txt created",
                hello_ok,
                "contains 'OK'" if hello_ok else f"unexpected content: {hello_content!r}",
            )
        else:
            _record(results, "hello.txt created", False, "file does not exist")

        if not task_id:
            for lbl in ("diff --stat", "result", "send follow-up", "hello.txt 2 lines", "list"):
                _record(results, lbl, False, "skipped (no task id)")
        else:
            diff_res = _delegate(["diff", task_id, "--stat"], timeout=60)
            diff_out = "\n".join(part for part in (diff_res.stdout, diff_res.stderr) if part)
            diff_ok = diff_res.returncode == 0 and "hello.txt" in diff_out
            _record(
                results,
                "diff --stat",
                diff_ok,
                "mentions hello.txt" if diff_ok else f"exit={diff_res.returncode}, output={diff_out.strip()[:120]!r}",
            )

            res_cmd = _delegate(["result", task_id], timeout=60)
            _record(
                results,
                "result",
                res_cmd.returncode == 0,
                "exit=0" if res_cmd.returncode == 0 else f"exit={res_cmd.returncode}",
            )

            send_res = _delegate(
                ["send", task_id, "Append a second line `OK2` to hello.txt", "--timeout", str(TASK_TIMEOUT)],
                timeout=TASK_TIMEOUT + 60,
            )
            send_out = "\n".join(part for part in (send_res.stdout, send_res.stderr) if part)
            _record(
                results,
                "send follow-up",
                send_res.returncode == 0,
                "exit=0" if send_res.returncode == 0 else f"exit={send_res.returncode} ({send_out.strip()[:120]})",
            )

            if os.path.isfile(hello_path):
                with open(hello_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = [ln for ln in f.read().splitlines() if ln.strip()]
                two_lines = lines == ["OK", "OK2"]
                _record(
                    results,
                    "hello.txt 2 lines",
                    two_lines,
                    f"lines={lines!r}" if two_lines else f"expected 2 lines, got {len(lines)}: {lines!r}",
                )
            else:
                _record(results, "hello.txt 2 lines", False, "hello.txt missing")

            list_res = _delegate(["list"], timeout=60)
            list_out = "\n".join(part for part in (list_res.stdout, list_res.stderr) if part)
            prefix = task_id[:8]
            list_ok = list_res.returncode == 0 and prefix in list_out
            _record(
                results,
                "list",
                list_ok,
                f"contains {prefix}" if list_ok else f"exit={list_res.returncode}, missing {prefix}",
            )
    finally:
        if ws:
            if keep:
                print(f"[kept] {ws}", flush=True)
            else:
                shutil.rmtree(ws, ignore_errors=True)

    passed = sum(1 for _lbl, ok, _reason in results if ok)
    total = len(results)
    print(f"Total: {passed}/{total} passed", flush=True)
    return 0 if passed == total else 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="live_cli_bridge.py",
        description="Live probe of the Claude Code -> Antigravity CLI bridge (spends real quota).",
    )
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id to pass to delegate.py (default: {DEFAULT_MODEL})")
    ap.add_argument("--keep", action="store_true", help="Keep the temporary git workspace after the probe")
    ap.add_argument("--stdin", action="store_true", help="Pass every prompt to agy on stdin (--input-format "
                    "stream-json) instead of -p, as for a .cmd agy or a very long brief (sets AGP_AGY_STDIN=1)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            reconf = getattr(stream, "reconfigure", None)
            if callable(reconf):
                reconf(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    if args.stdin:
        os.environ["AGP_AGY_STDIN"] = "1"  # delegate.py (and its turn runners) inherit it
    if not _find_agy():
        print("agy not installed — nothing to probe")
        return 0
    return run_probe(args.model, args.keep)


if __name__ == "__main__":
    sys.exit(main())
