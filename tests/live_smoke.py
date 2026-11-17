#!/usr/bin/env python3
r"""Antigravity Plus — scripted live smoke tests against a running Antigravity instance.

Runs small, cheap, scripted scenarios end-to-end in throw-away git workspaces, waits via WaitForConversationFullyIdle,
checks tool usage, Stop hook behavior, and factual grounding on the resulting transcript/output, and removes both the
throw-away workspace and the temporary ~/.gemini/config/projects/<uuid>.json entry in `finally`.

Usage:
  python3 tests/live_smoke.py --list
  python3 tests/live_smoke.py [--only NAME] [--model MODEL] [--timeout SEC] [--keep]

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(REPO_ROOT, "antigravity-plus")
HOOKS_DIR = os.path.join(PLUGIN_DIR, "hooks")
DELEGATE_DIR = os.path.join(REPO_ROOT, "claude-code", "skills", "antigravity", "scripts")
DOCTOR_DIR = os.path.join(PLUGIN_DIR, "skills", "doctor", "scripts")

for _p in (HOOKS_DIR, DELEGATE_DIR, DOCTOR_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import compat  # type: ignore[import-not-found]  # noqa: E402
import delegate  # type: ignore[import-not-found]  # noqa: E402
import doctor  # type: ignore[import-not-found]  # noqa: E402
import facts  # type: ignore[import-not-found]  # noqa: E402
import stop as stop_hook  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_MODEL = "gemini-4-hydrogen-low"
DEFAULT_TIMEOUT = 180.0
SHIM = os.path.expanduser(os.path.join("~", ".gemini", "antigravity", "bin", "agentapi"))
PROJECTS_DIR = os.path.expanduser(os.path.join("~", ".gemini", "config", "projects"))
BRAIN_DIR = os.path.expanduser(os.path.join("~", ".gemini", "antigravity", "brain"))

# Scripted smoke scenarios: each scenario is a self-contained Python dict.
SCENARIOS: List[Dict[str, Any]] = [
    {
        "name": "lsp_refs",
        "description": "Navigate a tiny Python module using /lsp (lsp.py refs or lsp_refs MCP tool) and list real callers.",
        "model": DEFAULT_MODEL,
        "files": {
            "AGENTS.md": (
                "# Smoke Fixture\n"
                "Tiny Python 3 fixture. Read-only inspection tasks should not run fake tests or edit files.\n"
            ),
            "pricing.py": (
                "def compute_total(subtotal: float, tax_rate: float) -> float:\n"
                '    """Return subtotal plus tax."""\n'
                "    return round(subtotal * (1.0 + tax_rate), 2)\n"
            ),
            "checkout.py": (
                "from pricing import compute_total\n\n\n"
                "def finalize_order(amount: float) -> float:\n"
                "    # compute_total is mentioned in this comment too\n"
                "    return compute_total(amount, 0.1)\n"
            ),
            "invoice.py": (
                "from pricing import compute_total\n\n\n"
                "def build_invoice(base: float) -> str:\n"
                "    total = compute_total(base, 0.2)\n"
                '    return f"total={total}"\n'
            ),
        },
        "prompt": (
            "Read-only task (do not edit or create any files): find all code references to `compute_total` defined in "
            "pricing.py line 1 using the /lsp tool (`lsp.py refs` or the `lsp_refs` MCP tool). Report which functions "
            "call `compute_total` and their file:line locations. Do not run tests or make file changes."
        ),
        "checks": {
            "tool_kind": "lsp_refs",
            "answer_contains": ["finalize_order", "build_invoice"],
            "no_unbacked_claims": True,
            "max_stop_blocks": 1,
        },
    },
    {
        "name": "mcp_tools",
        "description": "Call antigravity-plus_tools MCP (git_state and/or lsp_symbols) read-only and summarize results.",
        "model": DEFAULT_MODEL,
        "files": {
            "AGENTS.md": (
                "# Smoke Fixture\n"
                "Tiny Python 3 fixture. Read-only inspection tasks should prefer MCP tools when asked.\n"
            ),
            "sample.py": (
                "class Greeter:\n"
                "    def hello(self, name: str) -> str:\n"
                '        return f"Hello, {name}"\n\n\n'
                "def main_entry() -> str:\n"
                '    return Greeter().hello("world")\n'
            ),
        },
        "prompt": (
            "Read-only task (do not edit files or run git commands): call the `antigravity-plus_tools` MCP tools "
            "`git_state` (for this workspace) and `lsp_symbols` (for `sample.py`). Then report the current git branch "
            "and list the class/functions in `sample.py`."
        ),
        "checks": {
            "tool_kind": "mcp_tools",
            "answer_contains": ["main", "Greeter", "hello"],
            "no_unbacked_claims": True,
            "max_stop_blocks": 1,
        },
    },
]


def _discover_ls() -> delegate.LS:
    """Discover the running Antigravity language server via delegate.py / doctor.py helpers."""
    ls = delegate.discover()
    if ls:
        return ls
    doc_ls = doctor.discover_ls()
    if doc_ls:
        return delegate.LS(doc_ls[0], doc_ls[1], "")
    raise RuntimeError("Antigravity language server is not running. Start Antigravity and sign in before running smoke tests.")


def _create_workspace(files: Dict[str, str], prefix: str) -> str:
    ws = os.path.realpath(tempfile.mkdtemp(prefix=f"agp-smoke-{prefix}-"))
    for rel_path, content in files.items():
        full = os.path.join(ws, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    if "AGENTS.md" not in files:
        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# Smoke Test Workspace\nMinimal fixture for automated smoke tests.\n")
    git_env = dict(
        os.environ,
        GIT_AUTHOR_NAME="AGP Smoke",
        GIT_AUTHOR_EMAIL="smoke@example.com",
        GIT_COMMITTER_NAME="AGP Smoke",
        GIT_COMMITTER_EMAIL="smoke@example.com",
        GIT_CONFIG_NOSYSTEM="1",
    )
    init = subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=ws,
        env=git_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if init.returncode != 0:
        subprocess.run(["git", "init"], cwd=ws, env=git_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=ws, env=git_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    subprocess.run(["git", "add", "-A"], cwd=ws, env=git_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial smoke fixture"],
        cwd=ws,
        env=git_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return ws


def _register_project(ls: delegate.LS, ws: str, name: str) -> Tuple[str, str]:
    """Write ~/.gemini/config/projects/<uuid>.json and wait until the LS sees it."""
    pid = str(uuid.uuid4())
    proj_name = f"agp-smoke-{name}-{pid[:8]}"
    data = {
        "id": pid,
        "name": proj_name,
        "projectResources": {"resources": [{"folderUri": pathlib.Path(ws).as_uri()}]},
    }
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    proj_path = os.path.join(PROJECTS_DIR, f"{pid}.json")
    tmp_path = f"{proj_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    compat.replace(tmp_path, proj_path)

    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            ids = delegate.rpc(ls, "ListProjects", {}).get("projectIds") or []
            if pid in ids:
                break
        except Exception:
            pass
        time.sleep(0.5)
    return pid, proj_path


def _agentapi_bin(ls: delegate.LS) -> List[str]:
    for cand in (SHIM, SHIM + ".cmd", SHIM + ".bat", SHIM + ".exe"):
        if os.path.isfile(cand):
            return [cand]
    if ls.exe and os.path.isfile(ls.exe):
        return [ls.exe, "agentapi"]
    found = shutil.which("agentapi")
    if found:
        return [found]
    raise RuntimeError("agentapi binary not found (~/.gemini/antigravity/bin/agentapi)")


def _start_conversation(ls: delegate.LS, project_id: str, model: str, title: str, prompt: str) -> str:
    cmd = _agentapi_bin(ls) + [
        "new-conversation",
        f"--model={model}",
        f"--title={title}",
        prompt,
    ]
    env = dict(
        os.environ,
        ANTIGRAVITY_LS_ADDRESS=ls.addr,
        ANTIGRAVITY_CSRF_TOKEN=ls.token,
        ANTIGRAVITY_PROJECT_ID=project_id,
    )
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = None
    if r.returncode != 0 or not isinstance(data, dict) or data.get("error"):
        detail = data.get("error") if isinstance(data, dict) and data.get("error") else (r.stderr or r.stdout)
        raise RuntimeError(f"agentapi new-conversation failed (exit {r.returncode}): {str(detail).strip()[:400]}")
    cid = str(((data.get("response") or {}).get("newConversation") or {}).get("conversationId") or "")
    if not cid:
        raise RuntimeError(f"agentapi did not return a conversationId: {r.stdout[:300]}")
    return cid


def _wait_idle(ls: delegate.LS, cid: str, timeout: float) -> Tuple[str, List[dict]]:
    """Wait via WaitForConversationFullyIdle while watching for approvals or timeout."""
    deadline = time.time() + max(timeout, 5.0)
    offset = 0
    while True:
        left = deadline - time.time()
        if left <= 0:
            return "timeout", _fetch_steps(ls, cid)
        try:
            delegate.rpc(ls, "WaitForConversationFullyIdle", {"conversationId": cid}, timeout=min(15.0, max(left, 1.0)))
            steps = _fetch_steps(ls, cid)
            if any(isinstance(s, dict) and s.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" for s in steps):
                return "done", steps
            time.sleep(0.5)
            continue
        except delegate.TIMEOUTS:
            pass
        ask, offset = delegate.pending(ls, cid, offset)
        if ask:
            return f"approval ({ask.get('action', '')} {ask.get('target', '')})", _fetch_steps(ls, cid)


def _fetch_steps(ls: delegate.LS, cid: str) -> List[dict]:
    try:
        raw = delegate.rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid}, timeout=30).get("steps") or []
        return [s for s in raw if isinstance(s, dict)]
    except Exception:
        return []


def _load_transcript(cid: str) -> Tuple[str, List[dict]]:
    tp = os.path.join(BRAIN_DIR, cid, ".system_generated", "logs", "transcript_full.jsonl")
    for _ in range(8):
        if os.path.isfile(tp):
            break
        time.sleep(0.25)
    rows: List[dict] = []
    if not os.path.isfile(tp):
        return tp, rows
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    d = json.loads(s)
                    if isinstance(d, dict):
                        rows.append(d)
                except ValueError:
                    continue
    except OSError:
        pass
    return tp, rows


def _extract_calls_and_answer(rows: List[dict], rpc_steps: List[dict]) -> Tuple[List[Tuple[str, Any]], str, int]:
    """Return (tool_calls [(name, args)], final_answer, stop_blocks)."""
    calls: List[Tuple[str, Any]] = []
    answer = ""
    stop_blocks = 0

    for d in rows:
        typ = d.get("type") or ""
        if typ == "PLANNER_RESPONSE":
            for tc in d.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name"):
                    calls.append((str(tc["name"]), tc.get("args")))
            txt = (d.get("content") or "").strip()
            if txt:
                answer = txt
        elif typ == "SYSTEM_MESSAGE":
            if "Stop hook blocked termination:" in (d.get("content") or ""):
                stop_blocks += 1

    if not calls or not answer:
        for st in rpc_steps:
            typ = st.get("type") or ""
            if typ == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                pr = st.get("plannerResponse") or {}
                for tc in pr.get("toolCalls") or []:
                    if isinstance(tc, dict) and tc.get("name"):
                        raw_args = tc.get("argumentsJson")
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except ValueError:
                            args = raw_args
                        calls.append((str(tc["name"]), args))
                txt = str(pr.get("modifiedResponse") or pr.get("response") or "").strip()
                if txt:
                    answer = txt
            elif typ == "CORTEX_STEP_TYPE_RUN_COMMAND":
                rc = st.get("runCommand") or {}
                cmd = str(rc.get("commandLine") or rc.get("proposedCommandLine") or "")
                if cmd:
                    calls.append(("run_command", {"CommandLine": cmd}))
            elif typ == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE" and not rows:
                msg = str(((st.get("systemMessage") or {}).get("agentMessage") or {}).get("content") or "")
                if "Stop hook blocked termination:" in msg:
                    stop_blocks += 1

    return calls, answer, stop_blocks


def _used_expected_tool(kind: str, calls: List[Tuple[str, Any]], rpc_steps: List[dict]) -> bool:
    if kind == "lsp_refs":
        for name, args in calls:
            ad = args if isinstance(args, dict) else {}
            if "lsp_refs" in name:
                return True
            if name == "call_mcp_tool" and "lsp_refs" in str(ad.get("ToolName") or ""):
                return True
            if name == "run_command":
                cmd = str(ad.get("CommandLine") or ad.get("command") or "")
                if "lsp.py" in cmd and re.search(r"\b(refs|references|ref|calls|def)\b", cmd):
                    return True
        return False

    if kind == "mcp_tools":
        wanted = ("git_state", "lsp_symbols")
        for name, args in calls:
            ad = args if isinstance(args, dict) else {}
            if any(w in name for w in wanted):
                return True
            if name == "call_mcp_tool" and str(ad.get("ToolName") or "") in wanted:
                return True
        for st in rpc_steps:
            blob = json.dumps(st, ensure_ascii=False)
            if any(w in blob for w in wanted) and "mcp" in blob.lower():
                return True
        return False

    return True


def _evaluate_checks(scenario: Dict[str, Any], cid: str, ws: str, wait_status: str, rpc_steps: List[dict]) -> List[Tuple[str, bool, str]]:
    checks = scenario.get("checks") or {}
    tp, rows = _load_transcript(cid)
    calls, answer, stop_blocks = _extract_calls_and_answer(rows, rpc_steps)
    results: List[Tuple[str, bool, str]] = []

    results.append(("completed", wait_status == "done", wait_status))

    max_blocks = int(checks.get("max_stop_blocks", 2))
    results.append((
        f"stop_blocks <= {max_blocks}",
        stop_blocks <= max_blocks,
        f"stop_blocks={stop_blocks}",
    ))

    tool_kind = checks.get("tool_kind")
    if tool_kind:
        used = _used_expected_tool(str(tool_kind), calls, rpc_steps)
        names = ", ".join(dict.fromkeys(n for n, _a in calls)) or "(none)"
        results.append((f"tool_used ({tool_kind})", used, f"tools=[{names}]"))

    for needle in checks.get("answer_contains") or []:
        found = needle.lower() in answer.lower()
        results.append((f"answer mentions {needle!r}", found, f"answer_len={len(answer)}"))

    if checks.get("no_unbacked_claims") and os.path.isfile(tp) and answer:
        payload = {"conversationId": cid, "transcriptPath": tp, "workspacePaths": [ws]}
        st = {"users": [{"step": 0, "text": scenario["prompt"]}]}
        probs = facts.problems(
            payload,
            st,
            answer,
            stop_hook.VERIFY,
            tuple(stop_hook.CODE_EXT),
            stop_hook.WEB_EXT,
        )
        results.append(("no_unbacked_claims", not probs, "; ".join(probs[:3]) if probs else "ok"))

    return results


def run_scenario(ls: delegate.LS, scenario: Dict[str, Any], model_override: Optional[str], timeout: float, keep: bool) -> bool:
    name = scenario["name"]
    model = model_override or scenario.get("model") or DEFAULT_MODEL
    ws: Optional[str] = None
    proj_path: Optional[str] = None
    cid: Optional[str] = None
    try:
        ws = _create_workspace(scenario.get("files") or {}, name)
        proj_id, proj_path = _register_project(ls, ws, name)
        title = f"AGP smoke {name}"
        cid = _start_conversation(ls, proj_id, model, title, scenario["prompt"])
        print(f"Running [{name}] model={model} cid={cid[:8]} ws={ws}", flush=True)
        wait_status, rpc_steps = _wait_idle(ls, cid, timeout)
        if wait_status != "done":
            try:
                delegate.rpc(ls, "CancelCascadeInvocation", {"cascadeId": cid})
            except Exception:
                pass
        checks = _evaluate_checks(scenario, cid, ws, wait_status, rpc_steps)
        passed = all(ok for _lbl, ok, _detail in checks)
        mark = "PASS" if passed else "FAIL"
        print(f"{mark}  {name}  (cid={cid})")
        for lbl, ok, detail in checks:
            icon = "✅" if ok else "❌"
            print(f"  {icon} {lbl:<32} {detail}")
        return passed
    except (RuntimeError, delegate.Failure) as exc:
        print(f"FAIL  {name}  (error: {exc})")
        return False
    finally:
        if keep:
            print(f"  [kept] ws={ws} project={proj_path} cid={cid}")
        else:
            if proj_path and os.path.exists(proj_path):
                try:
                    os.remove(proj_path)
                except OSError:
                    pass
            if ws and os.path.exists(ws):
                shutil.rmtree(ws, ignore_errors=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="live_smoke.py", description="Run scripted live smoke tests against Antigravity.")
    ap.add_argument("--list", action="store_true", help="List available smoke scenarios and exit")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", help="Run only scenario NAME (repeatable)")
    ap.add_argument("--model", default=None, help=f"Override model id (default per scenario: {DEFAULT_MODEL})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"Wait timeout in seconds (default {int(DEFAULT_TIMEOUT)})")
    ap.add_argument("--keep", action="store_true", help="Keep throw-away workspace and project JSON after run")
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
    if args.list:
        for sc in SCENARIOS:
            print(f"{sc['name']:<16} model={sc.get('model', DEFAULT_MODEL)}  {sc.get('description', '')}")
        return 0

    selected = SCENARIOS
    if args.only:
        wanted = {name.strip() for item in args.only for name in item.split(",") if name.strip()}
        known = {sc["name"] for sc in SCENARIOS}
        unknown = sorted(wanted - known)
        if unknown:
            print(f"Unknown scenario(s): {', '.join(unknown)}. Available: {', '.join(sorted(known))}", file=sys.stderr)
            return 2
        selected = [sc for sc in SCENARIOS if sc["name"] in wanted]

    try:
        ls = _discover_ls()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ok = True
    for sc in selected:
        ok &= run_scenario(ls, sc, args.model, args.timeout, args.keep)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
