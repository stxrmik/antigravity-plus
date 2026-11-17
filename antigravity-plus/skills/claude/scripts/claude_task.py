#!/usr/bin/env python3
"""Antigravity Plus — hand a sub-task from an Antigravity agent to Claude Code, watch it, take the result.

The reverse of the Claude Code → Antigravity bridge (claude-code/skills/antigravity/scripts/delegate.py): the agent
starts `claude -p` (Claude Code's non-interactive mode) detached in the project folder, on the model it picks, and
later reads back only what it needs: Claude's report, the files that changed since the start (from a git snapshot),
the commands Claude ran, what it was refused, and what the run cost. The work comes out of the user's Claude plan
instead of the Antigravity quota.

  claude_task.py budget [--claude-5h PCT] [--claude-week PCT]   both sides' limits and whether handing over helps
  claude_task.py run [--model M] [--effort E] [--title T] [--workspace DIR] [--allow RULE …] [BRIEF]
                                                (no BRIEF or '-': read stdin) start a task and return at once
  claude_task.py wait ID [--timeout SEC]       wait for it (run it as a background command), then print the report
  claude_task.py result ID [--full]            the report again
  claude_task.py diff ID [PATH …] [--stat]     the changes in the folder since the task started, as a unified diff
  claude_task.py send ID MESSAGE               a follow-up in the same Claude session (it keeps its context)
  claude_task.py cancel ID                     stop it
  claude_task.py list                          recent tasks

An ID may be shortened to a unique prefix (8 characters are plenty). Exit codes: 0 done, 1 failed, canceled or an
error, 3 still running after --timeout (`wait ID` goes on waiting).

Permissions (no one can answer a prompt in a detached run): `--permission-mode acceptEdits` (file edits in the
folder go through), an allowlist of read, test and build commands (ALLOWED), a denylist (DENIED: git commit / push /
reset / stash / checkout, rm -rf, sudo, publishing, deploys), and `--permission-prompts none`, so anything else is
refused at once instead of waiting forever — the report lists the refusals. `--allow 'Bash(npm install *)'` adds rules
(Claude Code's permission-rule syntax). `--dangerously-skip-permissions` passes the CLI's flag through: only when the
user explicitly asked for it.

Verified against Claude Code 2.1.325 (2026-11-17): `claude -p` reads the prompt from stdin; `--output-format
stream-json` needs `--verbose`; the stream starts with {"type":"system","subtype":"init","session_id",…,"model"} and
ends with {"type":"result","is_error","result","total_cost_usd","num_turns","duration_ms","usage",
"permission_denials"}; `--session-id <uuid>` names the session, `--resume <id>` continues it under the same id;
`claude auth status --json` says {"loggedIn": …}; an expired login shows as an assistant message with
"error": "authentication_failed".

Stdlib only, Python 3.8+; macOS, Linux and Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..", "hooks")))  # antigravity-plus/hooks
try:
    import compat
except ImportError:
    print("❌ claude_task.py: the plugin's hooks/compat.py is missing — reinstall Antigravity Plus (./install.sh).")
    sys.exit(1)

HOME = os.path.expanduser("~")
TASKS = os.path.join(HOME, ".gemini", "antigravity-plus", "claude-tasks")
MARK = "[Delegated from Antigravity]"
BRIEF_LIMIT = 20000          # characters
REPORT_LIMIT = 3500          # characters of Claude's final message in a report (--full: all of it)
KEEP_TASKS = 100
DEFAULT_TIMEOUT = 1800
POLL = float(os.environ.get("AGP_CLAUDE_POLL") or 3)  # seconds between two looks of `wait`
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = ("acceptEdits", "plan", "auto", "dontAsk", "manual")  # `bypassPermissions` only via the explicit flag
LOGIN = "Claude Code isn't logged in: run `claude` once and log in — that's yours to do (tell the user). Then retry."

# Claude Code's permission rules (its `--allowedTools` syntax, «Bash(git *)» as in `claude --help`): reading and
# checking go through without a prompt; edits in the folder go through by acceptEdits.
ALLOWED = ["Read", "Bash(ls *)", "Bash(pwd)", "Bash(wc *)", "Bash(rg *)", "Bash(grep *)", "Bash(diff *)",
           "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git blame *)",
           "Bash(git grep *)", "Bash(python3 -m pytest *)", "Bash(python -m pytest *)", "Bash(pytest *)",
           "Bash(python3 -m unittest *)", "Bash(python -m unittest *)", "Bash(python3 tests/*)",
           "Bash(python3 -m py_compile *)", "Bash(npm test *)", "Bash(npm run test *)", "Bash(npm run lint *)",
           "Bash(npm run build *)", "Bash(npx tsc *)", "Bash(pnpm test *)", "Bash(yarn test *)", "Bash(go test *)",
           "Bash(go build *)", "Bash(go vet *)", "Bash(cargo test *)", "Bash(cargo build *)", "Bash(cargo check *)",
           "Bash(make test *)", "Bash(swift test *)", "Bash(swift build *)", "Bash(./gradlew test *)"]
DENIED = ["Bash(git commit *)", "Bash(git push *)", "Bash(git reset *)", "Bash(git stash *)", "Bash(git checkout *)",
          "Bash(git rebase *)", "Bash(git clean *)", "Bash(rm -rf *)", "Bash(sudo *)", "Bash(npm publish *)",
          "Bash(gh pr *)", "Bash(gh release *)", "Bash(ssh *)", "Bash(scp *)", "Bash(rsync *)",
          "Read(.env)", "Read(.env.*)", "Read(~/.ssh/*)", "Read(~/.aws/*)"]

RULES = """{mark} This task comes from an agent in Google Antigravity that works on this project for the same user. Nobody watches this session live and nobody can answer questions or approve prompts: decide reasonable details yourself and say so in your report.

Rules for this run:
1. Do exactly the task below. Change only what it covers, and stay inside the working folder.
2. No git commits, pushes, branches, stashes, resets or checkouts; no deploys, global installs or settings changes. Do the work yourself — don't hand it back to Antigravity.
3. Commands outside the allowed set are refused at once. If one you need is refused, find another way or name it in your report.
4. After your last change, run the checks the task names (or the tests of the code you touched) and fix what fails.
5. Stop any server or watcher you started before you finish.
6. End with one message in this format, under 250 words, without file contents:
STATUS: done | partial | blocked
CHANGED: <path> — <what changed> (one line per file, or "nothing")
CHECKS: <command> → <result, with the exact numbers> (or "none")
NOTES: <choices you made, what is left, risks> (or "none")

Working folder: {folder}

Task:
{brief}"""
FOLLOW_UP = ("Follow-up from the Antigravity agent: {message}\n\n(Same rules as before: stay in the folder, no commits or "
             "pushes, check your work after the last change, end with the STATUS / CHANGED / CHECKS / NOTES report.)")


class Failure(Exception):
    """Something to report in one line (exit code 1)."""


# ─────────────────────────────── Claude Code's CLI ───────────────────────────────

def claude_exe() -> str:
    """The `claude` executable: AGP_CLAUDE_BIN, PATH, then where the native installer puts it."""
    given = os.environ.get("AGP_CLAUDE_BIN", "")
    if given:
        if os.path.isfile(given):
            return given
        raise Failure(f"AGP_CLAUDE_BIN points at {given}, which doesn't exist.")
    found = shutil.which("claude")
    if found:
        return found
    names = ("claude.exe", "claude.cmd") if compat.WINDOWS else ("claude",)
    for folder in (os.path.join(HOME, ".local", "bin"), os.path.join(HOME, ".claude", "local")):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    raise Failure("Claude Code's `claude` command isn't installed here (https://docs.claude.com/en/docs/claude-code) "
                  "— installing it is the user's call. Do the work yourself meanwhile.")


HIDE = {"creationflags": 0x08000000} if compat.WINDOWS else {}  # CREATE_NO_WINDOW: no console flashes


def cli(exe: str, args: List[str], timeout: float = 30) -> Tuple[int, str]:
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout, **HIDE)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return r.returncode, r.stdout


def logged_in(exe: str) -> Optional[bool]:
    """`claude auth status --json` → loggedIn (None: unknown — an older CLI, or it didn't answer). Only that one
    field is read: the answer also holds the account's address."""
    code, out = cli(exe, ["auth", "status", "--json"])
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return bool(d.get("loggedIn")) if isinstance(d, dict) and "loggedIn" in d else None


def supports(exe: str, flag: str) -> bool:
    _, out = cli(exe, ["--help"])
    return flag in out


def detach_kwargs() -> dict:
    if compat.WINDOWS:  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: outlives the agent's command, no console
        return {"creationflags": 0x08000000 | 0x00000200}
    return {"start_new_session": True}


def alive(pid: int) -> bool:
    if not pid:
        return False
    if compat.WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:  # a zombie (our own child that ended) counts as gone
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        return not r.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def kill_tree(pid: int) -> None:
    """Stop the run with everything it started (its own session's process group; on Windows the whole tree)."""
    if not pid or int(pid) <= 0:
        return
    if compat.WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **HIDE)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(int(pid), sig)
        except OSError:
            return
        for _ in range(30):
            if not alive(pid):
                return
            time.sleep(0.1)


# ─────────────────────────────── git snapshot ───────────────────────────────

def git(args: List[str], cwd: str, env: Optional[dict] = None, timeout: float = 120) -> Optional[str]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL, **HIDE)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def dirty(top: str) -> List[str]:
    """Paths (relative to the repository) that differ from HEAD or are untracked."""
    out = git(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], top)
    if out is None:
        return []
    paths, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # a rename is followed by its old name
                i += 1
        i += 1
    return paths


def snapshot(top: str, scratch: str) -> Optional[str]:
    """The whole working tree as a git tree object (untracked files included, .gitignore respected), written through
    a temporary index: the user's index, branch and files stay as they are."""
    idx = (git(["rev-parse", "--git-path", "index"], top) or "").strip()
    if not idx:
        return None
    idx = idx if os.path.isabs(idx) else os.path.join(top, idx)
    tmp = os.path.join(scratch, f"index.{os.getpid()}.tmp")
    try:
        if os.path.isfile(idx):
            shutil.copyfile(idx, tmp)  # its stat data makes `add -A` fast
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        if git(["add", "-A", "--", "."], top, env) is None:
            return None
        tree = (git(["write-tree"], top, env) or "").strip()
        return tree if re.fullmatch(r"[0-9a-f]{40,64}", tree) else None
    except OSError:
        return None
    finally:
        for path in (tmp, tmp + ".lock"):
            try:
                os.remove(path)
            except OSError:
                pass


def tree_changes(top: str, before: str, after: str) -> List[dict]:
    """[{path, kind A|M|D, plus, minus}] between two snapshots (plus/minus None for a binary file)."""
    status = git(["diff", "--no-renames", "--name-status", "-z", before, after], top)
    nums = git(["diff", "--no-renames", "--numstat", "-z", before, after], top)
    if status is None:
        return []
    counts: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for rec in (nums or "").split("\0"):
        bits = rec.split("\t")
        if len(bits) == 3:
            counts[bits[2]] = (int(bits[0]) if bits[0].isdigit() else None, int(bits[1]) if bits[1].isdigit() else None)
    parts, out = status.split("\0"), []
    for i in range(0, len(parts) - 1, 2):
        kind, path = parts[i].strip(), parts[i + 1]
        if kind and path:
            plus, minus = counts.get(path, (None, None))
            out.append({"path": path, "kind": kind[0], "plus": plus, "minus": minus})
    return out


# ─────────────────────────────── tasks ───────────────────────────────

def task_dir(tid: str) -> str:
    return os.path.join(TASKS, tid)


def save(task: dict) -> None:
    folder = task_dir(task["id"])
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    path = os.path.join(folder, "task.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=1)
    compat.replace(tmp, path)


def load(tid: str) -> Optional[dict]:
    try:
        with open(os.path.join(task_dir(tid), "task.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("id") == tid else None


def update(task: dict, only_if_running: bool = False, **fields) -> dict:
    """Set fields of the record under a lock (a background `wait`, a `result` and a `cancel` may run at once), merging
    `turn` (a dict of fields for the last turn) into the record's current turn."""
    os.makedirs(TASKS, exist_ok=True)
    with open(os.path.join(TASKS, ".lock"), "a+", encoding="utf-8") as fh:
        compat.lock(fh)
        try:
            rec = load(task["id"]) or dict(task)
            if only_if_running and (not rec.get("turns") or rec["turns"][-1].get("state") != "running"):
                task.clear()
                task.update(rec)
                return task
            turn = fields.pop("turn", None)
            rec.update(fields)
            if turn and rec.get("turns"):
                rec["turns"][-1].update(turn)
            save(rec)
        finally:
            compat.unlock(fh)
    task.clear()
    task.update(rec)
    return task


def records() -> List[dict]:
    try:
        names = os.listdir(TASKS)
    except OSError:
        return []
    out = [d for d in (load(n) for n in names if not n.startswith(".")) if d]
    out.sort(key=lambda d: d.get("started") or 0, reverse=True)
    return out


def prune() -> None:
    for d in records()[KEEP_TASKS:]:
        if d.get("state") != "running":
            shutil.rmtree(task_dir(d["id"]), ignore_errors=True)


def task_for(ident: str) -> dict:
    ident = (ident or "").strip()
    if not re.fullmatch(r"[\w-]{4,80}", ident):
        raise Failure(f"not a task id: {ident!r}")
    hits = [d for d in records() if d["id"].startswith(ident)]
    if len(hits) > 1:
        raise Failure(f"{ident!r} matches {len(hits)} tasks — give more of the id.")
    if not hits:
        raise Failure(f"no Claude Code task {ident!r} (claude_task.py list shows them).")
    return hits[0]


def compose(brief: str, folder: str) -> str:
    return RULES.format(mark=MARK, folder=folder, brief=brief.strip())


def title_of(brief: str, given: str) -> str:
    t = (given or "").strip() or next((ln.strip(" #*->") for ln in brief.splitlines() if ln.strip(" #*->")), "task")
    t = re.sub(r"\s+", " ", t)
    return t[:60].rstrip() + "…" if len(t) > 60 else t


def flags_of(task: dict, exe: str) -> List[str]:
    """The permission and model flags every turn of a task runs with."""
    args = ["-p", "--output-format", "stream-json", "--verbose"]
    if task.get("model"):
        args += ["--model", task["model"]]
    if task.get("effort"):
        args += ["--effort", task["effort"]]
    if task.get("skip_permissions"):
        args += ["--dangerously-skip-permissions"]
    else:
        has_prompts = supports(exe, "--permission-prompts")
        mode = task.get("mode") or "acceptEdits"
        if not has_prompts and mode == "acceptEdits":
            mode = "dontAsk"
        args += ["--permission-mode", mode]
        if has_prompts:
            args += ["--permission-prompts", "none"]  # nobody can answer: refuse at once instead of hanging
        args += ["--allowedTools"] + ALLOWED + list(task.get("allow") or [])
        args += ["--disallowedTools"] + DENIED
    if task.get("max_budget"):
        args += ["--max-budget-usd", str(task["max_budget"])]
    return args


def start_turn(task: dict, exe: str, prompt: str, first: bool) -> dict:
    """Start one `claude -p` turn detached, its stream-json in stream-<n>.jsonl, and return the turn record."""
    n = len(task.get("turns") or []) + 1
    folder = task_dir(task["id"])
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    paths = {k: os.path.join(folder, f"{k}-{n}.{ext}") for k, ext in (("prompt", "txt"), ("stream", "jsonl"),
                                                                        ("stderr", "log"))}
    fd = os.open(paths["prompt"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    args = flags_of(task, exe)
    args += (["--session-id", task["session"], "--name", "Antigravity: " + task["title"]] if first
             else ["--resume", task["session"]])
    env = {k: v for k, v in os.environ.items() if k not in ("ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS")}
    try:
        with open(paths["prompt"], "rb") as fin, open(paths["stream"], "wb") as fout, \
                open(paths["stderr"], "wb") as ferr:
            proc = subprocess.Popen([exe] + args, cwd=task["workspace"], stdin=fin, stdout=fout, stderr=ferr, env=env,
                                    **detach_kwargs())
    except OSError as e:
        raise Failure(f"couldn't start Claude Code ({exe}): {e}")
    return {"n": n, "pid": proc.pid, "started": time.time(), "state": "running", "message": prompt[-400:]
            if not first else "", "stream": os.path.basename(paths["stream"]),
            "stderr": os.path.basename(paths["stderr"])}


# ─────────────────────────────── reading a run ───────────────────────────────

def events(task: dict, turn: dict) -> List[dict]:
    path = os.path.join(task_dir(task["id"]), turn.get("stream") or "")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        out.append(d)
    except OSError:
        pass
    return out


def tail(task: dict, turn: dict, name: str = "stderr", limit: int = 600) -> str:
    try:
        with open(os.path.join(task_dir(task["id"]), turn.get(name) or ""), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    return text.strip()[-limit:]


def result_of(evs: List[dict]) -> Optional[dict]:
    return next((e for e in reversed(evs) if e.get("type") == "result"), None)


def auth_failed(evs: List[dict]) -> bool:
    for e in evs:
        if e.get("type") == "assistant" and e.get("error") == "authentication_failed":
            return True
        if e.get("type") == "result" and e.get("is_error") and \
                re.search(r"failed to authenticate|not logged in|/login", str(e.get("result") or ""), re.I):
            return True
    return False


def tool_calls(evs: List[dict]) -> List[dict]:
    """Claude's tool calls in order, with whether each one failed: [{name, input, error}]."""
    calls: Dict[str, dict] = {}
    order: List[dict] = []
    for e in evs:
        content = ((e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if e.get("type") == "assistant" and block.get("type") == "tool_use":
                c = {"name": str(block.get("name") or "?"), "input": block.get("input") or {}, "error": None}
                calls[str(block.get("id") or len(order))] = c
                order.append(c)
            elif e.get("type") == "user" and block.get("type") == "tool_result":
                c = calls.get(str(block.get("tool_use_id") or ""))
                if c is not None:
                    c["error"] = bool(block.get("is_error"))
    return order


def edited_paths(task: dict, evs: List[dict]) -> List[str]:
    """Files Claude wrote or edited, from its tool calls (for a folder that isn't a git repository)."""
    out: List[str] = []
    for c in tool_calls(evs):
        inp = c["input"] if isinstance(c["input"], dict) else {}
        path = inp.get("file_path") or inp.get("notebook_path")
        if c["name"] in ("Write", "Edit", "MultiEdit", "NotebookEdit") and path and c["error"] is not True:
            path = str(path)
            try:
                path = os.path.relpath(path, task["workspace"]) if os.path.isabs(path) else path
            except ValueError:  # another drive on Windows
                pass
            if path not in out:
                out.append(path.replace("\\", "/"))
    return out


def last_text(evs: List[dict]) -> str:
    for e in reversed(evs):
        if e.get("type") == "assistant" and isinstance(e.get("message"), dict):
            texts = [b.get("text") for b in e["message"].get("content") or [] if isinstance(b, dict)
                     and b.get("type") == "text" and b.get("text")]
            if texts:
                return "\n".join(texts)
    return ""


def refresh(task: dict) -> dict:
    """Bring the last turn's state up to date: done / failed when its result came or its process is gone; the
    snapshot after it when the task stops running."""
    cur = load(task["id"])
    if cur:
        task.clear()
        task.update(cur)
    turns = task.get("turns") or []
    if not turns or turns[-1].get("state") != "running":
        return task
    turn = turns[-1]
    evs = events(task, turn)
    res = result_of(evs)
    if res is None and alive(int(turn.get("pid") or 0)):
        return task
    if res is None:
        time.sleep(0.3)  # the result line may still be on its way to the file
        evs = events(task, turn)
        res = result_of(evs)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), {})
    state = "failed" if res is None or res.get("is_error") or auth_failed(evs) else "done"
    fields = {"state": state, "turn": {"state": state, "finished": time.time(),
                                       "cost": float((res or {}).get("total_cost_usd") or 0)}}
    if init.get("model"):
        fields["claude_model"] = str(init["model"])
    if task.get("tree_before"):
        fields["tree_after"] = snapshot(task["root"], task_dir(task["id"]))
    return update(task, only_if_running=True, **fields)


# ─────────────────────────────── the report ───────────────────────────────

def span(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s // 3600} h {s % 3600 // 60} min"


def kilo(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def changes(task: dict) -> Tuple[List[dict], str]:
    """(changed files, how they were found) for the whole task so far."""
    if task.get("tree_before"):
        after = task.get("tree_after") if task.get("state") != "running" else None
        after = after or snapshot(task["root"], task_dir(task["id"]))
        if after:
            files = tree_changes(task["root"], task["tree_before"], after)
            prefix = task.get("prefix") or ""
            for f in files:  # relative to the workspace when it is a subfolder of the repository
                if prefix and f["path"].startswith(prefix):
                    f["path"] = f["path"][len(prefix):]
            return files, "git"
    evs: List[dict] = []
    for turn in task.get("turns") or []:
        evs += events(task, turn)
    return [{"path": p, "kind": "?", "plus": None, "minus": None} for p in edited_paths(task, evs)], "tools"


def denial_text(d) -> str:
    if not isinstance(d, dict):
        return str(d)[:160]
    name = str(d.get("tool_name") or d.get("name") or "?")
    inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
    what = inp.get("command") or inp.get("file_path") or inp.get("url") or ""
    return f"{name}: {str(what)[:140]}".rstrip(": ")


def render(task: dict, full: bool = False) -> Tuple[str, int]:
    short = task["id"][:8]
    turns = task.get("turns") or []
    turn = turns[-1] if turns else {}
    evs = events(task, turn) if turn else []
    res = result_of(evs) or {}
    state = task.get("state") or "?"
    head = f"Claude Code task {short} — {state}"
    if state == "done":
        m = re.search(r"^\W*STATUS\W*:\W*([^\n]{1,60})", str(res.get("result") or ""), re.M)
        if m:
            head += f" (STATUS: {m.group(1).strip(' *_`')})"
    bits = [task.get("claude_model") or task.get("model") or ""]
    if res.get("num_turns"):
        bits.append(f"{res['num_turns']} turns")
    if res.get("duration_ms"):
        bits.append(span(float(res["duration_ms"]) / 1000))
    elif turn.get("started"):
        bits.append(span((turn.get("finished") or time.time()) - turn["started"]) + (" so far" if state == "running" else ""))
    cost = sum(float(t.get("cost") or 0) for t in turns)
    if cost:
        bits.append(f"≈${cost:.4f} at API prices" + (f" over {len(turns)} runs" if len(turns) > 1 else "")
                    + " (on a Claude plan it counts against the plan's limits)")
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
    if usage:
        bits.append("tokens in {} · out {} · cache read {}".format(
            kilo(int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)),
            kilo(int(usage.get("output_tokens") or 0)), kilo(int(usage.get("cache_read_input_tokens") or 0))))
    lines = [head + " · " + " · ".join(b for b in bits if b), f"Folder: {task.get('workspace')}"]
    if state == "running":
        lines.append(f"Still working (run {turn.get('n')}). Wait for it: claude_task.py wait {short} "
                     "(as a background command).")
    code = {"done": 0, "running": 3}.get(state, 1)
    if auth_failed(evs):
        lines.append("❌ " + LOGIN)
        return "\n".join(lines), 1
    files, how = changes(task)
    if files:
        lines.append("Changed since the task started" + (" (git: the whole folder, anyone's edits included):"
                                                         if how == "git" else " (from Claude's own edits):"))
        for f in files[:40]:
            counts = "" if f["plus"] is None else f"  +{f['plus']} −{f['minus']}"
            lines.append(f"  {f['kind']} {f['path']}{counts}")
        if len(files) > 40:
            lines.append(f"  … {len(files) - 40} more (claude_task.py diff {short} --stat)")
    else:
        lines.append("No file changes" + (" in the folder since the task started." if how == "git" else
                                          " recorded (not a git repository: only Claude's own edits are known)."))
    cmds = [c for c in tool_calls(evs) if c["name"] in ("Bash", "PowerShell")]
    if cmds:
        lines.append(f"Commands it ran ({len(cmds)}):")
        for c in cmds[-12:]:
            inp = c["input"] if isinstance(c["input"], dict) else {}
            mark = "✗" if c["error"] else "✓" if c["error"] is False else "·"
            lines.append(f"  {mark} {str(inp.get('command') or '')[:160]}")
    denied = res.get("permission_denials") or []
    if denied:
        lines.append(f"Refused (nobody could approve) — {len(denied)}:")
        lines += ["  " + denial_text(d) for d in denied[:10]]
        lines.append("  Needed? Re-run or send with --allow '<rule>' (e.g. --allow 'Bash(npm install *)').")
    text = str(res.get("result") or "") or last_text(evs)
    if state == "failed" and not res:
        err = tail(task, turn)
        lines.append("It stopped without a result" + (f". Its error output:\n{err}" if err else "."))
    elif state == "canceled":
        lines.append("Canceled.")
    if text:
        if not full and len(text) > REPORT_LIMIT:
            text = text[:REPORT_LIMIT].rstrip() + f"\n… (cut; claude_task.py result {short} --full)"
        lines += ["", "Claude's report (its words — check them):", text]
    if state in ("done", "failed"):
        lines += ["", f"Check it yourself: claude_task.py diff {short}, then run the tests. More on the same work: "
                      f"claude_task.py send {short} \"…\"."]
    return "\n".join(lines), code


# ─────────────────────────────── budget ───────────────────────────────

def load_delegate():
    """The Claude Code bridge's delegate.py (for Antigravity's quota and Claude Code's statusLine record), or None."""
    for folder in (os.path.join(HERE, "..", "..", "..", "..", "claude-code", "skills", "antigravity", "scripts"),
                   os.path.join(HOME, ".claude", "skills", "antigravity", "scripts"), HERE):
        if os.path.isfile(os.path.join(folder, "delegate.py")):
            abs_dir = os.path.abspath(folder)
            sys.path.insert(0, abs_dir)
            try:
                import delegate
                return delegate
            except (ImportError, SystemExit, Exception):
                return None
            finally:
                if abs_dir in sys.path:
                    sys.path.remove(abs_dir)
    return None


def cmd_budget(a) -> int:
    now = time.time()
    lines = ["Limits now:"]
    try:
        exe = claude_exe()
        login = logged_in(exe)
    except Failure as e:
        exe, login = "", None
        lines.append(f"  Claude Code: not installed — {e}")
    if exe:
        lines.append("  Claude Code CLI: " + {True: "logged in", False: "NOT logged in — " + LOGIN,
                                              None: "login state unknown"}[login])
    d = load_delegate()
    cstate = "unknown"
    if d is not None:
        lim = d.claude_limits(a, now)
        cstate, ctext = d.claude_state(lim, now)
        src = " (from Claude Code's statusLine)" if lim.get("source") == "statusLine" else ""
        lines.append(f"  Claude Code's plan: {ctext}{src}" if cstate != "unknown" else
                     "  Claude Code's plan: unknown — ask the user, or /usage in Claude Code shows it; pass the "
                     "numbers as --claude-5h / --claude-week")
    else:
        lines.append("  Claude Code's plan: unknown — ask the user, or /usage in Claude Code shows it")
    states: List[dict] = []
    if d is not None:
        groups: List[dict] = []
        try:
            ls = d.discover()  # never starts Antigravity from here
            groups = d.quota(ls) if ls else []
        except Exception:
            groups = []
        states = [d.state_of(g, now) for g in groups]
        lines += d.quota_lines(groups, now)
    else:
        lines.append("  Antigravity: quota unknown (the bridge's delegate.py isn't installed)")
    best = min((s["status"] for s in states), key=lambda x: {"spend": 0, "ok": 1, "tight": 2, "exhausted": 3}.get(x, 1),
               default="")
    worst = {s["status"] for s in states}
    advice = []
    if not exe or login is False:
        advice.append("Claude Code can't take tasks right now (see above): do the work here.")
    elif cstate == "critical":
        advice.append("Claude Code's own limit is almost used up: don't hand over; do the work here.")
    else:
        if not states:
            advice.append("Antigravity's quota is unknown here: hand over only what Claude does clearly better, or an "
                          "independent part to run alongside your own work.")
        elif best in ("tight", "exhausted"):
            advice.append("Antigravity's quota is short (" + ("used up" if best == "exhausted" else "tight") +
                          " everywhere): hand bulky, self-contained parts to Claude Code — that's what it's for.")
        elif "exhausted" in worst or "tight" in worst:
            advice.append("One of Antigravity's quotas is short: hand over the parts that would land on it.")
        else:
            advice.append("Antigravity's quota is fine: hand over only what Claude does clearly better, or an "
                          "independent part to run alongside your own work.")
        if cstate == "tight":
            advice.append("Claude Code's own limit is getting tight: hand over only what's worth it, on sonnet or haiku.")
        elif cstate == "unknown":
            advice.append("Claude Code's own limit is unknown: ask the user before a big batch.")
    advice.append("Either way: only when the user asked for delegation or granted full access — it spends their "
                  "Claude plan.")
    lines += ["", "Advice:"] + ["  • " + x for x in advice]
    print("\n".join(lines))
    return 0


# ─────────────────────────────── commands ───────────────────────────────

def read_text(value: Optional[str], what: str) -> str:
    if value and value != "-":
        return value
    if sys.stdin is None or sys.stdin.isatty():
        raise Failure(f"give the {what} as an argument or on stdin (a heredoc).")
    return sys.stdin.read()


def cmd_run(a) -> int:
    brief = read_text(a.brief, "brief").strip()
    if not brief:
        raise Failure("the brief is empty.")
    if len(brief) > BRIEF_LIMIT:
        raise Failure(f"the brief has {len(brief)} characters (limit {BRIEF_LIMIT}) — put the details in a file in "
                      "the project and point the brief at it.")
    if not re.fullmatch(r"[\w.\-\[\]:]{1,80}", a.model or ""):
        raise Failure(f"not a model name: {a.model!r} (sonnet, opus, haiku or a full model id).")
    for rule in a.allow or []:
        if not re.fullmatch(r"[A-Za-z][\w]*(\([^()\n]{1,200}\))?", rule):
            raise Failure(f"not a permission rule: {rule!r} (e.g. 'Bash(npm install *)' or 'WebFetch').")
    folder = os.path.realpath(a.workspace or os.getcwd())
    if not os.path.isdir(folder):
        raise Failure(f"no such folder: {folder}")
    exe = claude_exe()
    if logged_in(exe) is False:
        raise Failure(LOGIN)
    tid = str(uuid.uuid4())
    title = title_of(brief, a.title)
    os.makedirs(task_dir(tid), exist_ok=True)
    top = (git(["rev-parse", "--show-toplevel"], folder) or "").strip()
    top = os.path.realpath(top) if top else ""
    prefix = (git(["rev-parse", "--show-prefix"], folder) or "").strip() if top else ""
    task = {"id": tid, "session": tid, "title": title, "model": a.model, "effort": a.effort or "",
            "mode": a.permission_mode, "allow": a.allow or [], "skip_permissions": bool(a.dangerously_skip_permissions),
            "max_budget": a.max_budget_usd or None, "workspace": folder, "root": top, "prefix": prefix,
            "started": time.time(), "state": "running", "brief": brief[:400], "claude": exe,
            "head_before": (git(["rev-parse", "HEAD"], top) or "").strip() if top else "",
            "dirty_before": dirty(top) if top else None,
            "tree_before": snapshot(top, task_dir(tid)) if top else None, "turns": []}
    task["turns"] = [start_turn(task, exe, compose(brief, folder), first=True)]
    save(task)
    prune()
    short = tid[:8]
    print(f"Started Claude Code task {tid} — model {a.model}" + (f", effort {a.effort}" if a.effort else "")
          + f", in {folder}.")
    if task["skip_permissions"]:
        print("  ⚠️ Permission checks are OFF for this run (--dangerously-skip-permissions): only on the user's request.")
    else:
        print(f"  Permissions: {a.permission_mode}, read/test/build commands allowed"
              + (", plus " + ", ".join(a.allow) if a.allow else "") + "; anything else is refused at once.")
    if not top:
        print("  Not a git repository: the report lists only the files Claude edited itself.")
    elif task["dirty_before"]:
        print(f"  {len(task['dirty_before'])} file(s) already had uncommitted changes — the report counts only "
              "what changes from now on.")
    print(f"Next: claude_task.py wait {short} as a background command (you're told when it ends), then "
          f"claude_task.py result {short} / diff {short}.")
    return 0


def cmd_wait(a) -> int:
    task = task_for(a.id)
    deadline = time.time() + max(float(a.timeout), 0.0)
    while True:
        refresh(task)
        if task.get("state") != "running":
            text, code = render(task)
            print(text)
            return code
        if time.time() >= deadline:
            turn = (task.get("turns") or [{}])[-1]
            print(f"⏳ Claude Code task {task['id'][:8]} is still running after "
                  f"{span(time.time() - (turn.get('started') or time.time()))} — wait again: "
                  f"claude_task.py wait {task['id'][:8]} (or cancel {task['id'][:8]}).")
            return 3
        time.sleep(min(POLL, max(deadline - time.time(), 0.05)))


def cmd_result(a) -> int:
    task = refresh(task_for(a.id))
    text, code = render(task, full=a.full)
    print(text)
    return code


def cmd_diff(a) -> int:
    task = refresh(task_for(a.id))
    short = task["id"][:8]
    if not task.get("tree_before"):
        files, _ = changes(task)
        print("Not a git repository, so no diff — files Claude edited: " + (", ".join(f["path"] for f in files) or
                                                                             "none") + ". Open them to check.")
        return 0
    after = (task.get("tree_after") if task.get("state") != "running" else None) or \
        snapshot(task["root"], task_dir(task["id"]))
    if not after:
        raise Failure("couldn't take a snapshot of the folder now (git failed).")
    prefix = task.get("prefix") or ""
    paths = []
    for p in a.paths:
        if not os.path.isabs(p):
            paths.append(os.path.join(prefix, p).replace("\\", "/"))
        else:
            try:
                paths.append(os.path.relpath(p, task["root"]).replace("\\", "/"))
            except ValueError:
                paths.append(p.replace("\\", "/"))
    args = ["diff", "--no-color", "--no-ext-diff", "--no-renames"] + (["--stat"] if a.stat else []) + \
        [task["tree_before"], after, "--"] + (paths or ([prefix] if prefix else []))
    out = git(args, task["root"])
    if out is None:
        raise Failure("git diff failed.")
    lines = out.rstrip("\n").splitlines()
    if not lines:
        print("No changes in the folder since the task started" + (" for those paths." if a.paths else "."))
        return 0
    if len(lines) > a.max_lines:
        cut = len(lines) - a.max_lines
        lines = lines[:a.max_lines] + [f"… {cut} more lines — narrow it with paths, or --max-lines {len(lines)}"]
    print("\n".join(lines))
    print(f"(Changes in the folder from the start of task {short} to "
          + ("now — it is still running" if task.get("state") == "running" else "its end") + "; edits by anyone.)")
    return 0


def cmd_send(a) -> int:
    task = refresh(task_for(a.id))
    if task.get("state") == "running":
        raise Failure(f"it is still working — wait for it (claude_task.py wait {task['id'][:8]}) or cancel it first.")
    message = read_text(" ".join(a.message or []).strip() or None, "follow-up message").strip()
    if not message:
        raise Failure("the follow-up message is empty.")
    exe = claude_exe()
    if logged_in(exe) is False:
        raise Failure(LOGIN)
    for rule in a.allow or []:
        if not re.fullmatch(r"[A-Za-z][\w]*(\([^()\n]{1,200}\))?", rule):
            raise Failure(f"not a permission rule: {rule!r}")
    allow = list(task.get("allow") or []) + [r for r in a.allow or [] if r not in (task.get("allow") or [])]
    task = update(task, allow=allow)
    turn = start_turn(task, exe, FOLLOW_UP.format(message=message), first=False)
    update(task, state="running", turns=(task.get("turns") or []) + [turn])
    print(f"Sent the follow-up to Claude Code task {task['id']} (run {turn['n']}, same session).")
    print(f"Next: claude_task.py wait {task['id'][:8]} as a background command.")
    return 0


def cmd_cancel(a) -> int:
    task = refresh(task_for(a.id))
    if task.get("state") != "running":
        print(f"Claude Code task {task['id']} wasn't running ({task.get('state')}).")
        return 0
    turn = (task.get("turns") or [{}])[-1]
    kill_tree(int(turn.get("pid") or 0))
    fields = {"state": "canceled", "turn": {"state": "canceled", "finished": time.time()}}
    if task.get("tree_before"):
        fields["tree_after"] = snapshot(task["root"], task_dir(task["id"]))
    update(task, **fields)
    print(f"Stopped Claude Code task {task['id']}. What it changed so far: claude_task.py diff {task['id'][:8]}.")
    return 0


def cmd_list(a) -> int:
    tasks = records()[:a.limit]
    if not tasks:
        print("No Claude Code tasks yet.")
        return 0
    lines = ["Recent Claude Code tasks (newest first):"]
    for t in tasks:
        t = refresh(t)
        age = span(time.time() - (t.get("started") or time.time())) + " ago"
        lines.append(f"  {t['id'][:8]}  {t.get('state') or '?':9} {age:12} {t.get('claude_model') or t.get('model')}  "
                     f"{t.get('title') or ''}  [{t.get('workspace') or ''}]")
    print("\n".join(lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="claude_task.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("budget", help="both sides' limits, and whether handing over makes sense")
    p.add_argument("--claude-5h", help="Claude Code's 5-hour use in percent, as /usage shows it (optionally @<reset>)")
    p.add_argument("--claude-week", help="Claude Code's weekly use in percent (optionally @<reset>)")
    p = sub.add_parser("run", help="start a task in Claude Code and return at once")
    p.add_argument("brief", nargs="?", help="the task; omit it (or '-') to read stdin")
    p.add_argument("--model", "-m", default="sonnet", help="sonnet (default), opus, haiku or a full model id")
    p.add_argument("--effort", choices=EFFORTS, help="Claude Code's effort level")
    p.add_argument("--title", default="", help="the session's name in Claude Code")
    p.add_argument("--workspace", "--cwd", help="the folder to work in (default: the current one)")
    p.add_argument("--allow", action="append", metavar="RULE",
                   help="one more permission rule, e.g. 'Bash(npm install *)' (repeatable)")
    p.add_argument("--permission-mode", choices=MODES, default="acceptEdits",
                   help="plan: read and plan only; default: acceptEdits")
    p.add_argument("--dangerously-skip-permissions", action="store_true",
                   help="turn Claude Code's permission checks off — only when the user explicitly asked for it")
    p.add_argument("--max-budget-usd", type=float, help="stop at this API spend (API-key accounts only)")
    p = sub.add_parser("wait", help="wait for a task and print its report")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds (default 1800)")
    p = sub.add_parser("result", help="the report of a task")
    p.add_argument("id")
    p.add_argument("--full", action="store_true", help="Claude's whole final message")
    p = sub.add_parser("diff", help="the changes in the folder since the task started")
    p.add_argument("id")
    p.add_argument("paths", nargs="*")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--max-lines", type=int, default=600)
    p = sub.add_parser("send", help="a follow-up in the same Claude session")
    p.add_argument("id")
    p.add_argument("message", nargs="*", help="the text ('-' or nothing: read stdin)")
    p.add_argument("--allow", action="append", metavar="RULE", help="one more permission rule from now on")
    p = sub.add_parser("cancel", help="stop a task")
    p.add_argument("id")
    p = sub.add_parser("list", help="recent tasks")
    p.add_argument("--limit", type=int, default=15)
    a = ap.parse_args(argv)
    handlers = {"budget": cmd_budget, "run": cmd_run, "wait": cmd_wait, "result": cmd_result, "diff": cmd_diff,
                "send": cmd_send, "cancel": cmd_cancel, "list": cmd_list}
    if a.cmd not in handlers:
        ap.print_help()
        return 1
    try:
        return handlers[a.cmd](a)
    except Failure as e:
        print(f"❌ {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
