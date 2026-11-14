"""/ci skill (detect, status, logs, watch, rerun for GitHub Actions & GitLab CI) and hooks (stop, facts, session_start)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
HOOKS = os.path.join(PLUGIN, "hooks")
CI_PY = os.path.join(PLUGIN, "skills", "ci", "scripts", "ci.py")
MEMORY_PY = os.path.join(PLUGIN, "skills", "memory", "scripts", "memory.py")
WIN = os.name == "nt"

# Sanitize host CI/auth env vars so real runner tokens or Windows APPDATA configs never leak into offline tests.
for _v in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GITLAB_TOKEN", "GLAB_TOKEN", "GLAB_CONFIG_DIR"):
    os.environ.pop(_v, None)
os.environ["APPDATA"] = os.path.join(SP, "appdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(SP, "xdg")
os.makedirs(os.environ["APPDATA"], exist_ok=True)
os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

FAKE_BIN = os.path.join(SP, "fake_bin")
os.makedirs(FAKE_BIN, exist_ok=True)
STATE_FILE = os.path.join(SP, "fake_ci_state.json")
os.environ["AGP_FAKE_CI_STATE"] = STATE_FILE
ORIG_PATH = os.environ.get("PATH", "")
os.environ["PATH"] = FAKE_BIN + os.pathsep + ORIG_PATH

sys.path.insert(0, HOOKS)
sys.path.insert(0, os.path.join(PLUGIN, "skills", "ci", "scripts"))
import ci as citool  # noqa: E402
import conversation as conv  # noqa: E402

# ── Cross-platform fake `gh` and `glab` CLI shims ──
FAKE_CLI = os.path.join(SP, "fake_cli.py")
FAKE_CLI_SRC = r'''#!/usr/bin/env python3
import json
import os
import sys

RAW_LOG = (
    "test (ubuntu-latest)\tRun pytest\t\ufeff2026-11-14T14:20:01.1234567Z ##[group]Run pytest -q\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01.2000000Z env:\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01.2010000Z   INTERNAL_CI_VAR: noisy_env_value\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:01.2020000Z ##[endgroup]\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:02.0000000Z \x1b[32mPASS\x1b[0m tests/test_alpha.py\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:02.1000000Z PASS tests/test_beta.py\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:02.2000000Z Downloading wheel_pkg-1.0.whl\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03.0000000Z \x1b[31mFAIL\x1b[0m tests/test_parser.py\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03.1000000Z Traceback (most recent call last):\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03.2000000Z   File \"app/parser.py\", line 19, in parse\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03.3000000Z AssertionError: expected 200 got 500\n"
    "test (ubuntu-latest)\tRun pytest\t2026-11-14T14:20:03.4000000Z ##[error]Process completed with exit code 1.\n"
    "test (windows-latest)\tRun pytest\t\ufeff2026-11-14T14:21:05.0000000Z \x1b[31mERROR\x1b[0m: windows path mismatch\n"
    "test (windows-latest)\tRun pytest\t2026-11-14T14:21:05.1000000Z ##[error]Tests failed on windows-latest.\n"
)

GLAB_TRACE = (
    "\ufeff2026-11-14T14:01:01.000000Z ##[group]Preparing environment\n"
    "2026-11-14T14:01:01.100000ZRUNNER_TEMP=/tmp/builds\n"
    "2026-11-14T14:01:01.200000Z ##[endgroup]\n"
    "2026-11-14T14:01:02.000000Z \x1b[32mPASS\x1b[0m tests/test_ok_1.py\n"
    "2026-11-14T14:01:02.100000Z PASS tests/test_ok_2.py\n"
    "2026-11-14T14:01:03.000000Z \x1b[31mFAIL\x1b[0m tests/test_gitlab_job.py\n"
    "2026-11-14T14:01:03.100000Z Traceback (most recent call last):\n"
    "2026-11-14T14:01:03.200000Z RuntimeError: gitlab pipeline failure\n"
)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mode": "", "polls": 0, "calls": []}


def save_state(path, st):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def arg_after(argv, flags):
    for i, a in enumerate(argv):
        if a in flags and i + 1 < len(argv):
            return argv[i + 1]
        for fl in flags:
            if a.startswith(fl + "="):
                return a.split("=", 1)[1]
    return ""


def gh_run(rid, wf, sha, status, conclusion, attempt=1):
    return {
        "databaseId": rid,
        "workflowName": wf,
        "name": wf,
        "displayTitle": "ci commit",
        "event": "push",
        "headBranch": "main",
        "headSha": sha,
        "status": status,
        "conclusion": conclusion,
        "attempt": attempt,
        "createdAt": "2026-11-14T14:00:00Z",
        "startedAt": "2026-11-14T14:00:02Z",
        "updatedAt": "2026-11-14T14:01:12Z",
        "url": "https://github.com/example/demo/actions/runs/%s" % rid,
    }


def handle_gh(argv, st, state_path):
    mode = st.get("mode", "")
    head = st.get("head_sha", "")
    old = st.get("old_sha", "0000000000000000000000000000000000000000")
    if argv[:2] == ["run", "list"]:
        commit = arg_after(argv, ("-c", "--commit"))
        st["polls"] = st.get("polls", 0) + 1
        p = st["polls"]
        save_state(state_path, st)
        if mode == "status_pass":
            # Re-run deduplication: attempt 2 (success) supersedes older attempt 1 (failure) for same workflow+sha
            out = [
                gh_run(102, "Tests", head, "completed", "success", attempt=2),
                gh_run(101, "Tests", head, "completed", "failure", attempt=1),
            ]
        elif mode == "status_fail":
            out = [gh_run(205, "Tests", head, "completed", "failure", attempt=1)]
        elif mode == "watch_race":
            old_run = gh_run(99, "Tests", old, "completed", "success", attempt=1)
            if p == 1:
                out = [] if commit == head else [old_run]
            elif p == 2:
                # Even if an older commit run is present alongside HEAD, watch must only track HEAD
                out = [old_run, gh_run(401, "Tests", head, "in_progress", "")]
            else:
                out = [gh_run(401, "Tests", head, "completed", "success")]
        elif mode == "watch_multi":
            if p == 1:
                out = [gh_run(301, "lint", head, "in_progress", "")]
            elif p == 2:
                out = [
                    gh_run(301, "lint", head, "completed", "success"),
                    gh_run(302, "tests", head, "in_progress", ""),
                ]
            else:
                out = [
                    gh_run(301, "lint", head, "completed", "success"),
                    gh_run(302, "tests", head, "completed", "failure"),
                ]
        else:
            out = []
        sys.stdout.write(json.dumps(out))
        return 0

    if argv[:2] == ["run", "view"]:
        rid = argv[2] if len(argv) > 2 else ""
        if "--log-failed" in argv:
            sys.stdout.write(RAW_LOG)
            return 0
        if "--json" in argv:
            if rid in ("102", "301", "401"):
                wf = "lint" if rid == "301" else "Tests"
                data = {
                    "workflowName": wf,
                    "name": wf,
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://github.com/example/demo/actions/runs/%s" % rid,
                    "jobs": [
                        {"databaseId": 11, "name": "test (ubuntu-latest)", "status": "completed", "conclusion": "success", "steps": []},
                        {"databaseId": 12, "name": "test (macos-latest)", "status": "completed", "conclusion": "success", "steps": []},
                    ],
                }
            else:
                wf = "tests" if rid == "302" else "Tests"
                data = {
                    "workflowName": wf,
                    "name": wf,
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "https://github.com/example/demo/actions/runs/%s" % rid,
                    "jobs": [
                        {
                            "databaseId": 21,
                            "name": "test (ubuntu-latest, 3.13)",
                            "status": "completed",
                            "conclusion": "success",
                            "steps": [{"name": "Run pytest", "conclusion": "success"}],
                        },
                        {
                            "databaseId": 22,
                            "name": "test (windows-latest, 3.9)",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [
                                {"name": "Checkout", "conclusion": "success"},
                                {"name": "Run pytest", "conclusion": "failure"},
                            ],
                            "url": "https://github.com/example/demo/actions/runs/%s/job/22" % rid,
                        },
                    ],
                }
            sys.stdout.write(json.dumps(data))
            return 0

    if argv[:2] == ["pr", "view"]:
        sys.stdout.write(json.dumps({"headRefOid": head, "headRefName": "feat/pr-17"}))
        return 0
    if argv[:2] == ["run", "rerun"]:
        sys.stdout.write("Requested rerun\n")
        return 0
    return 0


def handle_glab(argv, st, state_path):
    head = st.get("head_sha", "")
    if argv[:2] == ["ci", "list"]:
        st["polls"] = st.get("polls", 0) + 1
        p = st["polls"]
        save_state(state_path, st)
        status = "running" if p == 1 else "failed"
        out = [{
            "id": 901,
            "iid": 42,
            "sha": head,
            "ref": "main",
            "status": status,
            "source": "push",
            "web_url": "https://gitlab.com/example/demo/-/pipelines/901",
            "created_at": "2026-11-14T14:00:00Z",
            "updated_at": "2026-11-14T14:01:20Z",
        }]
        sys.stdout.write(json.dumps(out))
        return 0
    if argv[:2] == ["ci", "get"]:
        data = {
            "id": 901,
            "jobs": [
                {"id": 5000, "stage": "lint", "name": "ruff", "status": "success", "web_url": "https://gitlab.com/example/demo/-/jobs/5000"},
                {
                    "id": 5001,
                    "stage": "test",
                    "name": "pytest",
                    "status": "failed",
                    "failure_reason": "script_failure",
                    "web_url": "https://gitlab.com/example/demo/-/jobs/5001",
                },
            ],
        }
        sys.stdout.write(json.dumps(data))
        return 0
    if argv[:2] == ["ci", "trace"]:
        sys.stdout.write(GLAB_TRACE)
        return 0
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    tool = sys.argv[1]
    argv = sys.argv[2:]
    state_path = os.environ.get("AGP_FAKE_CI_STATE", "")
    st = load_state(state_path)
    st.setdefault("calls", []).append({"tool": tool, "argv": argv})
    save_state(state_path, st)
    if tool == "gh":
        return handle_gh(argv, st, state_path)
    if tool == "glab":
        return handle_glab(argv, st, state_path)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

with open(FAKE_CLI, "w", encoding="utf-8") as _f:
    _f.write(FAKE_CLI_SRC)

for _tool in ("gh", "glab"):
    if WIN:
        _shim = os.path.join(FAKE_BIN, _tool + ".cmd")
        with open(_shim, "w", encoding="utf-8") as _f:
            _f.write(f'@echo off\r\n"{sys.executable}" "{FAKE_CLI}" {_tool} %*\r\n')
    else:
        _shim = os.path.join(FAKE_BIN, _tool)
        with open(_shim, "w", encoding="utf-8") as _f:
            _f.write(f'#!/usr/bin/env python3\nimport runpy, sys\nsys.argv = [sys.argv[0], {_tool!r}, *sys.argv[1:]]\nrunpy.run_path({FAKE_CLI!r}, run_name="__main__")\n')
        os.chmod(_shim, 0o755)

ok = True
cid = ""
rows = []
ART = os.path.join(SP, "ci_art")
TP = os.path.join(ART, "transcript_full.jsonl")
TASK = os.path.join(ART, "task.md")
os.makedirs(ART, exist_ok=True)


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" — {str(detail)[:600]}"))


def set_fake_state(mode: str, head_sha: str = "", old_sha: str = "") -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "polls": 0, "head_sha": head_sha, "old_sha": old_sha, "calls": []}, f)


def get_fake_state() -> dict:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def git_run(ws: str, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, encoding="utf-8", check=True)
    return (r.stdout or "").strip()


def make_repo(name: str, remote: str, files: dict, commit_msg: str = "Initial commit") -> str:
    ws = os.path.realpath(os.path.join(SP, name))
    shutil.rmtree(ws, ignore_errors=True)
    os.makedirs(ws, exist_ok=True)
    git_run(ws, "init", "-q")
    git_run(ws, "checkout", "-qb", "main")
    if remote:
        git_run(ws, "remote", "add", "origin", remote)
    base_files = {"AGENTS.md": "# Test Project\n"}
    base_files.update(files)
    for rel, content in base_files.items():
        p = os.path.join(ws, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    git_run(ws, "add", ".")
    git_run(ws, "commit", "-qm", commit_msg)
    return ws


def run_ci(*args: str, env_extra: dict = None) -> tuple:
    env = dict(os.environ)
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    r = subprocess.run([sys.executable, CI_PY, *args], env=env, capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout or "", r.stderr or ""


def config(*pairs: str) -> None:
    subprocess.run([sys.executable, MEMORY_PY, "config", *pairs], capture_output=True, text=True, encoding="utf-8")


def iso(ago: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def start_conv(name: str, user: str = "Запушь изменения в main") -> None:
    global cid, rows
    cid = f"agp-citest-{name}"
    rows = []
    for f in (TP, TASK, os.path.join(conv.STATE_DIR, cid + ".json")):
        if os.path.exists(f):
            os.remove(f)
    add_row("USER_INPUT", content=f"<USER_REQUEST>{user}</USER_REQUEST>", ago=30)


def add_row(kind: str, content: str = "", calls: list = None, ago: float = 0.0) -> None:
    row = {"step_index": len(rows), "type": kind, "status": "DONE", "created_at": iso(ago), "content": content}
    if calls is not None:
        row["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    rows.append(row)
    with open(TP, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def step_cmd(line: str, code: int = 0, output: str = "ok") -> None:
    add_row("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line})])
    add_row("GENERIC", content=f"Created At: {iso()}\nCommand exited with code {code}.\n\nCommand output:\n{output}\n")


def step_bg_cmd(line: str, task_suffix: str = "task-1") -> None:
    add_row("PLANNER_RESPONSE", calls=[("run_command", {"CommandLine": line, "WaitMsBeforeAsync": 5000})])
    add_row(
        "GENERIC",
        content=(
            f"Created At: {iso()}\n"
            f"Tool is running as a background task with task id: {cid}/{task_suffix}\n"
            f"Task Description: {line}\n"
            "Task logs are available at: file:///x/task.log"
        ),
    )


def step_edit(path: str) -> None:
    add_row("PLANNER_RESPONSE", calls=[("edit_file", {"Path": path, "Target": "x", "Replacement": "y"})])
    add_row("GENERIC", content="The following changes were made")


def step_answer(text: str) -> None:
    add_row("PLANNER_RESPONSE", content=text)


def run_stop(ws: str, extra: dict = None, env_extra: dict = None) -> tuple:
    payload = {
        "conversationId": cid,
        "transcriptPath": TP,
        "artifactDirectoryPath": ART,
        "workspacePaths": [ws],
        "terminationReason": "NO_TOOL_CALL",
        "fullyIdle": True,
        "executionNum": 0,
    }
    if extra:
        payload.update(extra)
    env = dict(os.environ)
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, encoding="utf-8")
    d = json.loads(r.stdout or "{}")
    return d, (r.stderr or "").strip()


def run_session_start(ws: str, main: bool = True, env_extra: dict = None) -> str:
    payload = {"conversationId": "agp-citest-sess", "workspacePaths": [ws]}
    if main:
        payload["lastUserInput"] = "Запушь ветку"
    env = dict(os.environ)
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "session_start.py")], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, encoding="utf-8")
    d = json.loads(r.stdout or "{}")
    steps = d.get("injectSteps") or []
    return steps[0]["systemMessage"]["systemMessage"] if steps else ""


# ═══════════════════════════════ 1. ci.py detect ═══════════════════════════════
gh_ws = make_repo(
    "gh_repo",
    "https://github.com/example/demo.git",
    {
        ".github/workflows/tests.yml": (
            "name: Tests\n"
            "on: [push, pull_request]\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: pytest -q\n"
        ),
        "a.py": "x = 1\n",
    },
)

code, out, err = run_ci("detect", "--workspace", gh_ws, "--json", env_extra={"GH_TOKEN": "test-token"})
det = json.loads(out or "{}")
check(
    "detect: GitHub Actions repo with on:[push, pull_request] + GH_TOKEN + fake gh → ready",
    code == 0 and det.get("ready") is True and det.get("provider") == "github"
    and det.get("triggerable") == ["tests.yml"] and det.get("workflows") == ["tests.yml"],
    (code, det, err),
)
code_txt, out_txt, _ = run_ci("detect", "--workspace", gh_ws, env_extra={"GH_TOKEN": "test-token"})
check(
    "detect (text): shows provider github, triggerable workflow, and Ready: yes",
    code_txt == 0 and "Provider:   github" in out_txt and "push/PR triggerable: tests.yml" in out_txt and "Ready:      yes" in out_txt,
    out_txt,
)

manual_ws = make_repo(
    "manual_repo",
    "https://github.com/example/demo.git",
    {".github/workflows/release.yml": "name: Release\non: workflow_dispatch\njobs:\n  rel:\n    runs-on: ubuntu-latest\n"},
)
code, out, err = run_ci("detect", "--workspace", manual_ws, "--json", env_extra={"GH_TOKEN": "test-token"})
det_man = json.loads(out or "{}")
check(
    "detect: manual-only workflow (on: workflow_dispatch) → triggerable: [], ready: False",
    code == 0 and det_man.get("triggerable") == [] and det_man.get("ready") is False and "manual/scheduled" in det_man.get("reason", ""),
    det_man,
)

code, out, err = run_ci("detect", "--workspace", gh_ws, "--json", env_extra={"GH_TOKEN": None, "GITHUB_TOKEN": None})
det_unauth = json.loads(out or "{}")
check(
    "detect: unauthenticated gh (no GH_TOKEN, empty HOME) → ready: False, mentions gh auth login",
    code == 0 and det_unauth.get("ready") is False and det_unauth.get("authed") is False and "gh auth login" in det_unauth.get("reason", ""),
    det_unauth,
)

gh_cfg = os.path.join(SP, "gh_cfg")
os.makedirs(gh_cfg, exist_ok=True)
with open(os.path.join(gh_cfg, "hosts.yml"), "w", encoding="utf-8") as _hf:
    _hf.write("github.com:\n    user: demo-user\n    git_protocol: https\n")
code, out, _ = run_ci("detect", "--workspace", gh_ws, "--json", env_extra={"GH_TOKEN": None, "GH_CONFIG_DIR": gh_cfg})
check("detect: hosts.yml authentication without GH_TOKEN → ready: True", json.loads(out or "{}").get("ready") is True, out)

gl_ws = make_repo(
    "gl_repo",
    "https://gitlab.com/example/demo.git",
    {".gitlab-ci.yml": "stages: [lint, test]\npytest:\n  stage: test\n  script: [pytest]\n"},
)
code, out, err = run_ci("detect", "--workspace", gl_ws, "--json", env_extra={"GITLAB_TOKEN": "test-token"})
det_gl = json.loads(out or "{}")
check(
    "detect: GitLab repo with .gitlab-ci.yml + GITLAB_TOKEN + fake glab → ready: True, provider: gitlab",
    code == 0 and det_gl.get("ready") is True and det_gl.get("provider") == "gitlab" and det_gl.get("triggerable") == [".gitlab-ci.yml"],
    det_gl,
)

nogit_ws = os.path.join(SP, "not_a_git_repo")
os.makedirs(nogit_ws, exist_ok=True)
code_ng, out_ng, _ = run_ci("detect", "--workspace", nogit_ws)
check("detect: non-git directory exits 1 with clear message", code_ng == 1 and "Not a git repository" in out_ng, (code_ng, out_ng))

# Regression check for multi-event block `on:` where `push:` follows `workflow_dispatch:`
block_wf = os.path.join(SP, "block_on.yml")
with open(block_wf, "w", encoding="utf-8") as _bf:
    _bf.write("name: CI\non:\n  workflow_dispatch:\n  push:\n    branches: [main]\njobs:\n  t:\n    runs-on: ubuntu-latest\n")
check(
    "detect helper: multi-event block `on:` with `workflow_dispatch:` before `push:`",
    citool._workflow_triggers_push_or_pr(block_wf),
    block_wf,
)


# ═══════════════════════════════ 2. ci.py status, logs & rerun ═══════════════════════════════
os.environ["GH_TOKEN"] = "test-token"
head_sha = git_run(gh_ws, "rev-parse", "HEAD")

set_fake_state("status_pass", head_sha=head_sha)
code, out, err = run_ci("status", "--workspace", gh_ws)
check(
    "status (passed): formats ✅ CI passed, job counts, and deduplicates older re-run attempt",
    code == 0 and "✅ CI passed" in out and "#102" in out and "2/2 jobs passed" in out and "#101" not in out,
    (code, out, err),
)

set_fake_state("status_pass", head_sha=head_sha)
code_pr, out_pr, _ = run_ci("status", "--workspace", gh_ws, "--pr", "17")
check("status --pr: resolves PR head SHA and branch via gh pr view", code_pr == 0 and "feat/pr-17" in out_pr and "✅ CI passed" in out_pr, out_pr)

set_fake_state("status_fail", head_sha=head_sha)
code, out, err = run_ci("status", "--workspace", gh_ws)
check(
    "status (failed): formats ❌ CI failed with failed matrix job and step",
    code == 0 and "❌ CI failed" in out and "#205" in out and "1/2 jobs passed" in out
    and "test (windows-latest, 3.9) — step: Run pytest" in out,
    (code, out, err),
)

set_fake_state("status_fail", head_sha=head_sha)
code_rr, out_rr, err_rr = run_ci("rerun", "--workspace", gh_ws)
rr_calls = [c["argv"] for c in get_fake_state().get("calls", []) if c["argv"][:2] == ["run", "rerun"]]
check(
    "rerun: finds failed HEAD run #205 and triggers `gh run rerun 205 --failed`",
    code_rr == 0 and "Triggered re-run for #205" in out_rr and rr_calls == [["run", "rerun", "205", "--failed"]],
    (code_rr, out_rr, err_rr, rr_calls),
)

set_fake_state("status_fail", head_sha=head_sha)
code, out, err = run_ci("logs", "205", "--workspace", gh_ws)
check(
    "logs: strips BOM/timestamps/ANSI/##[group] env noise, collapses PASS lines, keeps FAIL/ERROR/traceback",
    code == 0
    and "\ufeff" not in out
    and "2026-11-14T14:20" not in out
    and "\x1b[" not in out
    and "INTERNAL_CI_VAR" not in out
    and "▶ Run pytest -q" in out
    and out.count("passing/progress lines omitted") == 1
    and "test_alpha.py" not in out
    and "FAIL tests/test_parser.py" in out
    and "Traceback (most recent call last):" in out
    and "AssertionError: expected 200 got 500" in out
    and "ERROR: Process completed with exit code 1." in out
    and "ERROR: windows path mismatch" in out,
    (code, out, err),
)

code, out_filt, err = run_ci("logs", "205", "--workspace", gh_ws, "--job", "windows-latest")
check(
    "logs --job: filters output to matching matrix job only",
    code == 0 and "test (windows-latest) › Run pytest" in out_filt
    and "ERROR: windows path mismatch" in out_filt
    and "test_parser.py" not in out_filt,
    (code, out_filt, err),
)


# ═══════════════════════════════ 3. ci.py watch ═══════════════════════════════
# 3a. Post-push race: old SHA vs HEAD registration delay → in_progress → completed/success
old_sha = head_sha
with open(os.path.join(gh_ws, "a.py"), "w", encoding="utf-8") as _f:
    _f.write("x = 2\n")
git_run(gh_ws, "add", "a.py")
git_run(gh_ws, "commit", "-qm", "feat: update x")
new_head = git_run(gh_ws, "rev-parse", "HEAD")

set_fake_state("watch_race", head_sha=new_head, old_sha=old_sha)
code, out, err = run_ci("watch", "--workspace", gh_ws, "--interval", "0.2", "--appear-timeout", "5")
race_st = get_fake_state()
check(
    "watch (post-push race): ignores old SHA, waits across empty → in_progress → completed, exits 0",
    code == 0 and "✅ CI passed" in out and new_head[:8] in out and "#401" in out and "#99" not in out
    and race_st.get("polls") == 3,
    (code, out, err, race_st),
)

# 3b. Multi-workflow failure: lint.yml succeeds, tests.yml fails on windows-latest
multi_ws = make_repo(
    "multi_repo",
    "https://github.com/example/demo.git",
    {
        ".github/workflows/lint.yml": "name: lint\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n",
        ".github/workflows/tests.yml": "name: tests\non: [push]\njobs:\n  test:\n    runs-on: windows-latest\n",
    },
)
multi_head = git_run(multi_ws, "rev-parse", "HEAD")
set_fake_state("watch_multi", head_sha=multi_head)
code, out, err = run_ci("watch", "--workspace", multi_ws, "--interval", "0.2", "--appear-timeout", "5")
multi_st = get_fake_state()
check(
    "watch (multi-workflow failure): waits for both lint & tests, prints cleaned failure logs, exits 1",
    code == 1 and "❌ CI failed" in out and "✅ lint (#301)" in out and "❌ tests (#302)" in out
    and "test (windows-latest, 3.9)" in out and "## Failed logs — tests (#302)" in out
    and "AssertionError: expected 200 got 500" in out and "\x1b[" not in out and multi_st.get("polls") == 3,
    (code, out, err, multi_st),
)

# 3c. [skip ci] commit message: exits 0 immediately without polling
skip_ws = make_repo(
    "skip_repo",
    "https://github.com/example/demo.git",
    {".github/workflows/tests.yml": "name: tests\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n"},
    commit_msg="chore: update docs [skip ci]",
)
set_fake_state("watch_skip", head_sha=git_run(skip_ws, "rev-parse", "HEAD"))
t0 = time.time()
code, out, err = run_ci("watch", "--workspace", skip_ws, "--interval", "1", "--appear-timeout", "10")
took = time.time() - t0
skip_st = get_fake_state()
check(
    "watch ([skip ci]): exits 0 immediately with 0 polls",
    code == 0 and "[skip ci]" in out and skip_st.get("polls") == 0 and took < 3.0,
    (code, out, took, skip_st),
)

# 3d. GitLab watch: running → failed + cleaned job trace
os.environ.pop("GH_TOKEN", None)
os.environ["GITLAB_TOKEN"] = "test-token"
gl_head = git_run(gl_ws, "rev-parse", "HEAD")
set_fake_state("glab_watch", head_sha=gl_head)
code, out, err = run_ci("watch", "--workspace", gl_ws, "--interval", "0.2", "--appear-timeout", "5")
gl_st = get_fake_state()
check(
    "watch (GitLab): transitions running → failed, fetches & cleans job trace, exits 1",
    code == 1 and "❌ CI failed" in out and "pipeline #42 (#901)" in out
    and "test: pytest — step: script_failure" in out and "### test: pytest (job #5001)" in out
    and "FAIL tests/test_gitlab_job.py" in out and "RuntimeError: gitlab pipeline failure" in out
    and "RUNNER_TEMP" not in out and "\x1b[" not in out and gl_st.get("polls") == 2,
    (code, out, err, gl_st),
)
os.environ.pop("GITLAB_TOKEN", None)
os.environ["GH_TOKEN"] = "test-token"


# ═══════════════════════════════ 4. hooks/stop.py (autoCi) ═══════════════════════════════
start_conv("push-no-watch")
step_cmd("git push origin main", code=0, output="To https://github.com/example/demo.git\n   1111111..2222222  main -> main")
step_answer("Изменения отправлены в ветку main.")
res, err = run_stop(gh_ws)
check(
    "stop (autoCi): git push in GH Actions repo with no CI check → continue mentioning ci.py and watch",
    res.get("decision") == "continue" and "ci.py" in res.get("reason", "") and "watch" in res.get("reason", "")
    and "tests.yml" in res.get("reason", "") and not err,
    (res, err),
)
res_again, _ = run_stop(gh_ws, extra={"executionNum": 1})
check("stop (autoCi): does not repeat the same CI nudge twice in one turn", res_again == {}, res_again)

start_conv("push-uptodate")
step_cmd("git push origin main", code=0, output="Everything up-to-date")
step_answer("Ветка уже актуальна.")
res, err = run_stop(gh_ws)
check("stop (autoCi): git push returning 'Everything up-to-date' → quiet ({})", res == {} and not err, (res, err))

start_conv("push-failed")
step_cmd("git push origin main", code=1, output="error: failed to push some refs")
step_answer("Пуш отклонён удалённым сервером.")
res, err = run_stop(gh_ws)
check("stop (autoCi): failed git push (exit 1) → quiet ({})", res == {} and not err, (res, err))

start_conv("push-dryrun")
step_cmd("git push --dry-run origin main", code=0, output="To https://github.com/example/demo.git\n * [new branch] main -> main")
step_answer("Проверил dry-run отправки.")
res, err = run_stop(gh_ws)
check("stop (autoCi): git push --dry-run → quiet ({})", res == {} and not err, (res, err))

start_conv("push-watched-ok")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd(f'python3 "{CI_PY}" watch --workspace "{gh_ws}"', code=0, output="✅ CI passed for 22222222 (main):")
step_answer("Изменения отправлены, прогон завершён.")
res, err = run_stop(gh_ws)
check("stop (autoCi): git push followed by ci.py watch (code 0) → quiet ({})", res == {} and not err, (res, err))

start_conv("push-watched-fail")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd(f'python3 "{CI_PY}" watch --workspace "{gh_ws}"', code=1, output="❌ CI failed for 22222222 (main):\n## Failed logs")
step_answer("CI упал на шаге тестов, лог выше.")
res, err = run_stop(gh_ws)
check("stop (autoCi): git push followed by ci.py watch (code 1 with logs) → quiet ({})", res == {} and not err, (res, err))

start_conv("push-list-running")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd("gh run list -L 5 --json status", code=0, output='[{"status": "in_progress"}]')
step_answer("Отправил изменения, пайплайн запустился.")
res, err = run_stop(gh_ws)
check(
    "stop (autoCi): git push followed by gh run list showing in_progress → still nudges to run ci.py watch",
    res.get("decision") == "continue" and "ci.py" in res.get("reason", "") and "watch" in res.get("reason", ""),
    (res, err),
)

start_conv("user-no-wait", user="просто запушь, не жди CI")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_answer("Запушил в origin/main.")
res, err = run_stop(gh_ws)
check("stop (autoCi): user said «просто запушь, не жди CI» → quiet ({})", res == {} and not err, (res, err))

config("auto-ci", "off")
start_conv("autoci-off")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_answer("Запушил в origin/main.")
res_off, err_off = run_stop(gh_ws)
config("auto-ci", "on")
start_conv("autoci-on-again")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_answer("Запушил в origin/main.")
res_on, _ = run_stop(gh_ws)
check(
    "stop (autoCi): memory.py config auto-ci off → quiet ({}), restored auto-ci on → nudges again",
    res_off == {} and not err_off and res_on.get("decision") == "continue",
    (res_off, res_on),
)

start_conv("bg-ci-watch", user="Запушь и дождись прохождения CI")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_bg_cmd(f'python3 "{CI_PY}" watch --workspace "{gh_ws}"', "task-ci-1")
with open(TASK, "w", encoding="utf-8") as _tf:
    _tf.write("## Выкатка\n- [x] Запушить изменения\n- [/] Дождаться прохождения CI\n")
step_answer("Запустил ci.py watch в фоне, жду завершения CI.")
res, err = run_stop(gh_ws, extra={"fullyIdle": False})
check(
    "stop (background): background ci.py watch + task.md `- [/] Дождаться прохождения CI` → quiet ({})",
    res == {} and not err,
    (res, err),
)


# ═══════════════════════════ 5. hooks/facts.py & hooks/session_start.py ═══════════════════════════
config("auto-setup", "off", "auto-verify", "off", "auto-review", "off", "auto-ci", "off")

start_conv("facts-no-ci")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_answer("Запушил в main, CI зелёный.")
res, err = run_stop(gh_ws)
check(
    "facts (ci): claims «Запушил в main, CI зелёный» with no CI check → sent back (no CI check)",
    res.get("decision") == "continue" and "facts" in res.get("reason", "") and "no CI check" in res.get("reason", ""),
    (res, err),
)

start_conv("facts-running")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd("gh run list -L 5 --json status", code=0, output='[{"status": "in_progress"}]')
step_answer("CI прошёл.")
res, err = run_stop(gh_ws)
check(
    "facts (ci): gh run list shows in_progress, then claims «CI прошёл» → sent back (still running)",
    res.get("decision") == "continue" and "facts" in res.get("reason", "") and "still running" in res.get("reason", ""),
    (res, err),
)

start_conv("facts-failed-out")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd(f'python3 "{CI_PY}" status --workspace "{gh_ws}"', code=0, output="❌ CI failed for 22222222 (main):")
step_answer("CI зелёный.")
res, err = run_stop(gh_ws)
check(
    "facts (ci): ci.py status exited 0 showing ❌ CI failed, then claims «CI зелёный» → sent back (shows a failure)",
    res.get("decision") == "continue" and "facts" in res.get("reason", "") and "shows a failure" in res.get("reason", ""),
    (res, err),
)

start_conv("facts-passed")
step_cmd("git push origin main", code=0, output="1111111..2222222  main -> main")
step_cmd(f'python3 "{CI_PY}" watch --workspace "{gh_ws}"', code=0, output="✅ CI passed for 22222222 (main):")
step_answer("Все проверки в CI прошли.")
res, err = run_stop(gh_ws)
check(
    "facts (ci): ci.py watch exited 0 with ✅ CI passed, then claims «Все проверки в CI прошли» → allowed ({})",
    res == {} and not err,
    (res, err),
)

start_conv("facts-stale-after-push")
step_cmd(f'python3 "{CI_PY}" watch --workspace "{gh_ws}"', code=0, output="✅ CI passed for 11111111 (main):")
step_edit(os.path.join(gh_ws, "a.py"))
step_cmd("git push origin main", code=0, output="1111111..3333333  main -> main")
step_answer("CI зелёный.")
res, err = run_stop(gh_ws)
check(
    "facts (ci): ci.py watch passed, then edited a.py + git push without re-checking, claims «CI зелёный» → sent back",
    res.get("decision") == "continue" and "facts" in res.get("reason", "") and "after your last push/change" in res.get("reason", ""),
    (res, err),
)

start_conv("facts-local-checks-not-ci")
step_cmd("pytest -q", code=0, output="12 passed in 0.4s")
step_answer("Линтер и все проверки прошли.")
res, err = run_stop(gh_ws)
check("facts (ci): local «все проверки прошли» does not false-positive as CI claim", res == {} and not err, (res, err))

start_conv("facts-pr-checks-zero-pending")
step_cmd("gh pr checks 17", code=0, output="All checks were successful\n0 cancelled, 0 failing, 4 successful, 0 skipped, and 0 pending checks")
step_answer("Все проверки в PR прошли, CI зелёный.")
res, err = run_stop(gh_ws)
check("facts (ci): `gh pr checks` with `0 pending checks` is accepted as green", res == {} and not err, (res, err))

config("auto-setup", "on", "auto-verify", "on", "auto-review", "on", "auto-ci", "on")

start_conv("stop-commit-mentions-push")
step_cmd('git commit -m "feat: track CI after git push"', code=0, output="[main 4444444] feat: track CI after git push")
step_answer("Сделал коммит локально.")
res, err = run_stop(gh_ws)
check("stop (autoCi): `git commit -m '... git push ...'` does not trigger CI watch nudge", res == {} and not err, (res, err))

sess_msg = run_session_start(gh_ws, main=True)
check(
    "session_start: GitHub Actions repo includes `CI: GitHub Actions` and `ci.py\" watch`",
    "CI: GitHub Actions" in sess_msg and 'ci.py" watch' in sess_msg and "tests.yml" in sess_msg,
    sess_msg,
)
sess_sub = run_session_start(gh_ws, main=False)
check("session_start: subagent context omits main-agent CI watch prompt", "CI: GitHub Actions" not in sess_sub, sess_sub)

# Clean up conversation states created by this test
for _fn in os.listdir(conv.STATE_DIR):
    if _fn.startswith("agp-citest-"):
        os.remove(os.path.join(conv.STATE_DIR, _fn))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
