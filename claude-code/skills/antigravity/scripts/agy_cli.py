"""The Antigravity CLI (`agy`) backend of delegate.py: a task runs as one headless `agy -p … --output-format
stream-json` process per turn, detached, in the task's folder; its events go to a file next to the task's record and
the task's state is read from that file and from whether the process still runs.

Everything about the real `agy` — its flags, the names of its events and fields, its tools, its exit codes, the
notice of an action it wasn't allowed — is in the constants below, taken from Antigravity's headless documentation
(https://antigravity.google/docs/cli/headless, CLI 1.5.x) and `agy --help` of 1.5.18. Not all of it was seen in a real
run yet: tests/live_cli_bridge.py checks it against a real `agy`, and a mismatch is fixed here, in one place.

delegate.py binds itself in (bind()): this module uses its records, locks, prices and report helpers."""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

d = None  # the delegate module (bind())


def bind(module) -> None:
    global d
    d = module


# ─────────────────────────────── the real agy: names from its documentation ───────────────────────────────

AGY_ENV = "AGP_AGY"                    # a path to another agy (tests: tests/probe/fake_agy.py)
FLAG_PRINT = "-p"                      # headless: one prompt, then exit
FLAG_MODEL = "--model"                 # a model slug = Antigravity's model id (`agy models`)
FLAG_OUTPUT = ("--output-format", "stream-json")
FLAG_INPUT = ("--input-format", "stream-json")  # the prompt on stdin: for a .cmd/.bat agy on Windows (cmd.exe breaks
                                                # quotes and line breaks in arguments) and for very long prompts.
# Seen live (1.5.18): no -p with it; one {"event":"user","message":{"content":…}} line, stdin closed → one turn, its
# `result`, exit 0; with --conversation too. (Windows .cmd itself: not seen live.)
STDIN_OVER = 24000                     # characters of a prompt that go on stdin instead of the command line
STDIN_ENV = "AGP_AGY_STDIN"            # "1": always on stdin (tests/live_cli_bridge.py --stdin)
FLAG_MODE = ("--mode", "accept-edits")  # seen live (1.5.18): edits and ordinary commands run without a question and the
                                       # plugin's hooks stay on; what needs an approval (a guard «ask») is denied in a
                                       # headless run. Not `auto` (its own judgement on commands) and never
                                       # --dangerously-skip-permissions or --settings.
FLAG_CONVERSATION = "--conversation"   # resume a conversation by id: a follow-up turn
FLAG_TIMEOUT = "--print-timeout"       # «15m», «90s»
NEVER = ("--dangerously-skip-permissions", "--settings")
MODELS_CMD = ("models",)               # `agy models`: «<id>\t<label>» per line (seen live, 1.5.18)
EV_INIT, EV_STEP, EV_RESULT = "init", "step_update", "result"
STEP_RESPONSE, STEP_TOOL = "agent_response", "tool"
STEP_USER, STEP_SYSTEM = "user_input", "system_message"  # a hook's injection (the Stop check) is a system message
STATE_DONE, STATE_ERROR = "DONE", "ERROR"  # a denied tool step ends in ERROR (live)
TOOL_RUN = "run_command"
PARAM_COMMAND = ("CommandLine", "command", "commandLine")
FILE_TOOLS = ("write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content")
PARAM_FILE = ("Path", "TargetFile", "AbsolutePath", "file_path", "path", "FilePath")  # write_file: Path (live)
STATUS_OK, STATUS_ERROR, STATUS_CANCELED, STATUS_WAITING = "SUCCESS", "ERROR", "CANCELED", "WAITING"
USAGE_IN, USAGE_OUT, USAGE_CACHED = "input_tokens", "output_tokens", "cache_read_tokens"  # input counts cached too
DENIED_ACTIONS = "denied_actions"      # result: [{"action": "command", "display_name": "Bash"}] (live, not in the docs)
FINISHED_REASON, PRINT_TIMEOUT = "finished_reason", "print_timeout"  # result on a --print-timeout (live)
EXIT_BAD_MODEL, EXIT_PRINT_TIMEOUT = 1, 3  # exit codes seen live; 0 also when a hook denied a tool
DENIED_ERROR = re.compile(r"(?i)denied by pre-tool hook|denied permission|permission check failed|not allowed")
EXIT_CODE = re.compile(r"(?i)exit(?:ed with)? code[:= ]+(-?\d+)")
# A tool call that needed an approval a headless run can't get is soft-denied: the run goes on and exits 0; the step
# ends in ERROR with the reason, and the result lists `denied_actions`. The docs also promise a stderr notice.
SOFT_DENIED = re.compile(r"(?i)^.*\b(?:soft[- ]?denied|requires? (?:approval|permission)|permission required)\b.*$", re.M)
SETTINGS = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "settings.json")
START_GRACE = 20.0     # seconds: a turn just spawned, its runner not up yet, counts as running
MODELS_TTL = 6 * 3600  # seconds the `agy models` list is kept
MANIFEST_LIMIT = 20000  # files in a non-git folder's snapshot
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle", "build", "dist", ".build",
             "DerivedData", ".next", "target", ".idea", ".tox", ".mypy_cache", ".pytest_cache"}


def exe() -> str:
    """The agy to run: AGP_AGY, compat.agy() (PATH, ~/.local/bin), shutil.which. "" when not installed."""
    over = os.environ.get(AGY_ENV, "").strip()
    if over:
        return over
    found = getattr(d.compat, "agy", None)
    try:
        path = found() if callable(found) else ""
    except Exception:
        path = ""
    return path or shutil.which("agy") or ""


def command(path: str) -> List[str]:
    """How to start it: a Python script (the fake one in the tests) through this Python."""
    return [sys.executable, "-X", "utf8", path] if path.endswith(".py") else [path]


def available() -> bool:
    return bool(exe())


def is_cli(task: Optional[dict]) -> bool:
    """A record of a CLI task; a record without `via` is from before 0.31: Desktop."""
    return bool(task) and task.get("via") == "cli"


def run_text(args: List[str], timeout: float = 30) -> Tuple[int, str, str]:
    path = exe()
    if not path:
        raise d.Failure(NOT_INSTALLED)
    try:
        r = subprocess.run(command(path) + list(args), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise d.Failure(f"`agy {' '.join(args)}` didn't answer within {int(timeout)} s.")
    except OSError as e:
        raise d.Failure(f"can't run agy ({path}): {e}")
    return r.returncode, r.stdout or "", r.stderr or ""


NOT_INSTALLED = ("the Antigravity CLI (agy) isn't installed here (not on PATH, not in ~/.local/bin). Install it "
                 "(https://antigravity.google/docs/cli), or use Antigravity Desktop (--via desktop).")


# ─────────────────────────────── models ───────────────────────────────

def models() -> Tuple[List[dict], str]:
    """The models agy offers (`agy models`, kept MODELS_TTL seconds), and its default one (its settings' `model`, else
    the client snapshot's). The CLI reports no quotas: `left` is None everywhere."""
    cache = os.path.join(d.TASKS, "agy-models.json")
    rows: List[dict] = []
    try:
        with open(cache, encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict) and time.time() - float(c.get("at") or 0) < MODELS_TTL and c.get("exe") == exe():
            rows = [m for m in c.get("models") or [] if isinstance(m, dict) and m.get("id")]
    except (OSError, ValueError, TypeError):
        rows = []
    if not rows:
        code, out, err = run_text(list(MODELS_CMD), timeout=60)
        for line in out.splitlines():
            parts = re.split(r"\t+|\s{2,}", line.strip(), maxsplit=1)
            if parts and re.fullmatch(r"[a-z0-9][\w.\-]*", parts[0] or ""):
                rows.append({"id": parts[0], "label": (parts[1].strip() if len(parts) > 1 else "") or parts[0]})
        if not rows:
            raise d.Failure("`agy models` listed no models" + (f": {err.strip()[-300:]}" if err.strip() else "")
                            + " — is the CLI signed in? Run `agy` once yourself (the user's call), then retry.")
        try:
            os.makedirs(d.TASKS, exist_ok=True)
            tmp = f"{cache}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": time.time(), "exe": exe(), "models": rows}, f)
            d.compat.replace(tmp, cache)
        except OSError:
            pass
    snap = d.client_snapshot_models()
    out = []
    for r in rows:
        extra = snap.get(r["id"]) or {}
        out.append({"id": r["id"], "label": r["label"], "enum": "", "left": None, "reset": "",
                    "tag": str(extra.get("tag") or ""), "note": str(extra.get("note") or "")})
    default = ""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        default = str(s.get("model") or "") if isinstance(s, dict) else ""
    except (OSError, ValueError):
        pass
    ids = {m["id"] for m in out}
    if default not in ids:
        default = d.client_default_model()
    return out, default if default in ids else ""


# ─────────────────────────────── a task's files ───────────────────────────────

def folder(task: dict) -> str:
    return os.path.join(d.TASKS, task["id"] + ".cli")


def turn_path(task: dict, n: int, ext: str) -> str:
    return os.path.join(folder(task), f"turn-{n}.{ext}")


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    d.compat.replace(tmp, path)


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


BASE = 1_000_000  # step numbers of turn n start at n × BASE: they only grow, whatever agy numbers its steps


# ─────────────────────────────── the snapshot for diff ───────────────────────────────

def git_tree(root: str) -> str:
    """The whole working tree as a git tree object — tracked files as they are now and untracked ones git doesn't
    ignore — written through a copy of the index, so the real index, HEAD and the refs stay as they are. "" when it
    can't be made."""
    gitdir = (d.git(["rev-parse", "--absolute-git-dir"], root) or "").strip()
    if not gitdir:
        return ""
    tmp_index = os.path.join(d.TASKS, f".index-{uuid.uuid4().hex[:8]}")
    try:
        os.makedirs(d.TASKS, exist_ok=True)
        real = os.path.join(gitdir, "index")
        if os.path.isfile(real):
            shutil.copyfile(real, tmp_index)  # keeps the stat cache: only changed files are hashed
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index, GIT_OPTIONAL_LOCKS="0")
        for args in (["add", "-A", "--", "."], ["write-tree"]):
            r = subprocess.run(["git"] + args, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                return ""
        tree = (r.stdout or "").strip()
        return tree if re.fullmatch(r"[0-9a-f]{40,64}", tree) else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.remove(tmp_index)
        except OSError:
            pass


def manifest(root: str) -> Tuple[Dict[str, List[float]], bool]:
    """{relative path: [size, mtime]} of a folder that isn't in git, and whether MANIFEST_LIMIT cut it short."""
    out: Dict[str, List[float]] = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith(".git"))
        for name in sorted(files):
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root).replace(os.sep, "/")] = [st.st_size, round(st.st_mtime, 3)]
            if len(out) >= MANIFEST_LIMIT:
                return out, True
    return out, False


def snapshot(root: str) -> dict:
    tree = git_tree(root)
    if tree:
        return {"kind": "git", "tree": tree, "at": time.time()}
    files, cut = manifest(root)
    return {"kind": "files", "files": files, "cut": cut, "at": time.time()}


def changed_files(task: dict) -> Tuple[List[dict], str]:
    """The files changed since the task started — in delegate.changed_files()'s shape — and a note on what the diff
    can't tell ("" when nothing). Git: the tree at the start against the tree now, contents included. Elsewhere: sizes
    and times, so a changed file is listed without its old text."""
    root = str(task.get("root") or task.get("cwd") or "")
    snap = task.get("snapshot") or {}
    if not root or not os.path.isdir(root) or not snap:
        return [], "no snapshot of the folder was taken at the start — the diff isn't known (git status shows it)"
    note = overlap(task)
    out: List[dict] = []
    if snap.get("kind") == "git":
        now = git_tree(root)
        if not now:
            return [], "git couldn't write the working tree now — the diff isn't known (git status shows it)"
        listing = d.git(["diff", "--no-renames", "--numstat", "-z", snap["tree"], now], root) or ""
        status = d.git(["diff", "--no-renames", "--name-status", "-z", snap["tree"], now], root) or ""
        kinds: Dict[str, str] = {}
        parts = status.split("\0")
        for i in range(0, len(parts) - 1, 2):
            kinds[parts[i + 1]] = parts[i][:1]
        for item in listing.split("\0"):
            m = re.match(r"(-|\d+)\t(-|\d+)\t(.+)$", item.strip("\n"), re.S)
            if not m:
                continue
            relp = m.group(3)
            kind = kinds.get(relp, "M")
            old = "" if kind == "A" else (d.git(["show", f"{snap['tree']}:{relp}"], root) or "")
            new = "" if kind == "D" else (d.git(["show", f"{now}:{relp}"], root) or "")
            out.append({"path": os.path.realpath(os.path.join(root, relp)), "rel": relp, "kind": kind,
                        "old": old, "new": new, "plus": int(m.group(1)) if m.group(1).isdigit() else 0,
                        "minus": int(m.group(2)) if m.group(2).isdigit() else 0})
        return out, note
    before = snap.get("files") or {}
    after, cut = manifest(root)
    for relp in sorted(set(before) | set(after)):
        a, b = before.get(relp), after.get(relp)
        if a == b:
            continue
        kind = "A" if a is None else "D" if b is None else "M"
        new = ""
        if kind == "A":
            try:
                with open(os.path.join(root, relp), encoding="utf-8") as f:
                    new = f.read(200_000)
            except (OSError, UnicodeDecodeError):
                new = ""
        out.append({"path": os.path.realpath(os.path.join(root, relp)), "rel": relp, "kind": kind, "old": "",
                    "new": new, "plus": len(new.splitlines()), "minus": 0, "no_old": kind == "M"})
    why = "not a git folder: changed files are found by size and time, without their old text"
    if snap.get("cut") or cut:
        why += f"; only the first {MANIFEST_LIMIT} files were compared"
    return out, "; ".join(x for x in (why, note) if x)


def overlap(task: dict) -> str:
    """Other tasks that worked in the same folder while this one did: the diff covers their changes too."""
    root = os.path.normcase(os.path.realpath(str(task.get("root") or task.get("cwd") or "")))
    began = float(task.get("started") or 0)
    ends = float(task.get("finished") or 0) or time.time()
    others = []
    for r in d.records():
        if r["id"] == task["id"] or r["id"].startswith("q-") or not r.get("started"):
            continue
        if os.path.normcase(os.path.realpath(str(r.get("root") or r.get("cwd") or ""))) != root:
            continue
        r_end = float(r.get("finished") or 0) or (time.time() if r.get("state") in d.NOT_OVER else float(r["started"]))
        if float(r["started"]) < ends and r_end > began:
            others.append(r["id"][:8])
    return ("the diff includes what other tasks in this folder changed meanwhile: " + ", ".join(others[:5])
            if others else "")


# ─────────────────────────────── running a turn ───────────────────────────────

def start(p: dict, model: dict, prompt: str) -> dict:
    """A new CLI task: the snapshot of its folder, then its first turn. Returns the saved record."""
    path = exe()
    if not path:
        raise d.Failure(NOT_INSTALLED)
    folder_ = p["cwd"]
    top = (d.git(["rev-parse", "--show-toplevel"], folder_) or "").strip()
    root = os.path.realpath(top) if top else folder_
    task = {"id": str(uuid.uuid4()), "via": "cli", "cid": "", "title": d.title_of(p["brief"], p.get("title") or ""),
            "model": model["id"], "label": model["label"], "project": "", "project_name": "", "created_project": False,
            "cwd": folder_, "root": root, "started": time.time(), "turn": 0, "state": "running",
            "brief": p["brief"][:400], "quota_before": None, "claude_session": p.get("claude_session") or "",
            "owner": p.get("owner") or "", "heard": 0, "chat": [], "retries_max": int(p.get("retries") or 0),
            "timeout": float(p.get("timeout") or d.DEFAULT_TIMEOUT), "turns": [], "agy": path}
    if p.get("series"):
        task.update(series=p["series"], series_reference=p.get("reference") or "",
                    pieces=[{"n": int(p.get("piece") or 1), "turn": 0, "at": time.time(), "brief": p["brief"][:200]}])
    task["snapshot"] = snapshot(root)
    d.save(task)
    begin_turn(task, prompt, first=True)
    d.prune()
    return task


def begin_turn(task: dict, prompt: str, first: bool = False, kind: str = "") -> int:
    """Start the next turn of a CLI task: `agy -p <prompt>` (resuming its conversation after the first turn), in a
    detached runner that records how the process ended. Only when the previous turn's process has ended."""
    path = exe()
    if not path:
        raise d.Failure("the Antigravity CLI (agy) isn't installed any more (not on PATH, not in ~/.local/bin) — "
                        "the task can't go on. Reinstall agy, or start a new task (--via desktop).")
    turns = task.get("turns") or []
    if turns and alive(task, len(turns) - 1):
        raise d.Failure(f"its turn is still running — a new one starts only after it ends (delegate.py wait "
                        f"{task['id'][:8]}).")
    if not first and not task.get("cid"):
        raise d.Failure("the CLI never named this task's conversation (its first turn ended before it started), so it "
                        "can't be resumed — start a new task.")
    n = len(turns)
    timeout = float(task.get("timeout") or d.DEFAULT_TIMEOUT)
    argv = command(path)
    stdin_mode = (path.lower().endswith((".cmd", ".bat")) or len(prompt) > STDIN_OVER
                  or os.environ.get(STDIN_ENV, "") == "1")
    argv += [FLAG_MODEL, str(task["model"]), *FLAG_OUTPUT, *FLAG_MODE, FLAG_TIMEOUT, f"{int(timeout)}s"]
    if stdin_mode:
        argv += list(FLAG_INPUT)
    else:
        argv += [FLAG_PRINT, prompt]
    if not first:
        argv += [FLAG_CONVERSATION, task["cid"]]
    assert not any(x in NEVER for x in argv[:-2]) and FLAG_MODE[-1] != "auto"
    os.makedirs(folder(task), exist_ok=True)
    for ext in ("ndjson", "err", "exit", "cancel", "sum"):
        try:
            os.remove(turn_path(task, n, ext))
        except OSError:
            pass
    write_json(turn_path(task, n, "spec"), {"argv": argv, "cwd": task["cwd"], "stdin": prompt if stdin_mode else None,
                                            "timeout": timeout})
    entry = {"n": n, "at": time.time(), "kind": kind or ("brief" if first else "follow-up")}
    turns = turns + [entry]
    d.update(task, turns=turns, turn=n * BASE, since=n * BASE, sent=time.time(), state="running",
             heard=n * BASE)
    ok = d.spawn_detached([sys.executable, "-X", "utf8", os.path.join(d.HERE, "delegate.py"), "cli-turn",
                           task["id"], str(n)])
    if not ok:
        raise d.Failure("couldn't start the agy runner process.")
    return n


def run_turn(task_id: str, n: int) -> int:
    """The detached runner of one turn (`delegate.py cli-turn ID N`): holds the turn's lock while agy runs, writes how
    it ended (`turn-N.exit`), and stops agy's whole process tree at the time limit or when the turn is canceled."""
    task = d.load(task_id)
    if not task:
        return 1
    spec = read_json(turn_path(task, n, "spec")) or {}
    lock_fh = open(turn_path(task, n, "lock"), "a+", encoding="utf-8")
    if not d.try_lock(lock_fh):
        return 0  # another runner has it
    reason, code = "", None
    try:
        with open(turn_path(task, n, "ndjson"), "wb") as out, open(turn_path(task, n, "err"), "wb") as err:
            kw = dict(cwd=spec.get("cwd") or None, stdout=out, stderr=err,
                      stdin=subprocess.PIPE if spec.get("stdin") is not None else subprocess.DEVNULL)
            if d.compat.WINDOWS:
                kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True  # its own process group: kill_tree() takes all of it
            try:
                proc = subprocess.Popen(spec.get("argv") or [], **kw)
            except OSError as e:
                err.write(f"can't start agy: {e}\n".encode("utf-8"))
                reason = "spawn"
                proc = None
            if proc is not None:
                write_json(turn_path(task, n, "pid"), {"pid": proc.pid, "at": time.time()})
                if spec.get("stdin") is not None:
                    try:
                        line = json.dumps({"event": "user", "message": {"content": spec["stdin"]}}, ensure_ascii=False)
                        proc.stdin.write((line + "\n").encode("utf-8"))
                        proc.stdin.close()
                    except OSError:
                        pass
                limit = float(spec.get("timeout") or d.DEFAULT_TIMEOUT) + 60  # agy's own --print-timeout comes first
                deadline = time.time() + limit
                while True:
                    try:
                        code = proc.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if os.path.exists(turn_path(task, n, "cancel")):
                        reason = "canceled"
                    elif time.time() >= deadline:
                        reason = "timeout"
                    if reason:
                        d.compat.kill_tree(proc.pid)
                        try:
                            code = proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            code = None
                        break
        if not reason and os.path.exists(turn_path(task, n, "cancel")):
            reason = "canceled"
        write_json(turn_path(task, n, "exit"), {"code": code, "reason": reason, "at": time.time()})
    finally:
        try:
            d.compat.unlock(lock_fh)
        except OSError:
            pass
        lock_fh.close()
    return 0


def alive(task: dict, n: int) -> bool:
    """Does turn n's runner still run? Its lock tells (no pid probing: os.kill(pid, 0) would kill it on Windows); a
    turn spawned a moment ago whose runner hasn't taken the lock yet counts as running too."""
    if os.path.exists(turn_path(task, n, "exit")):
        return False
    try:
        fh = open(turn_path(task, n, "lock"), "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        if d.try_lock(fh):
            d.compat.unlock(fh)
            turns = task.get("turns") or []
            at = float(turns[n].get("at") or 0) if n < len(turns) else 0.0
            return time.time() - at < START_GRACE and not os.path.exists(turn_path(task, n, "exit"))
        return True
    finally:
        fh.close()


# ─────────────────────────────── reading a turn's events ───────────────────────────────

def parse(task: dict, n: int, final: bool) -> dict:
    """What turn n's event file says so far, read as a stream from where the last look stopped (`turn-N.sum` keeps the
    offset and what was found). A last line without its line break is still being written: it waits for the next
    look, unless the process has ended (`final`)."""
    path = turn_path(task, n, "ndjson")
    s = read_json(turn_path(task, n, "sum"))
    if not isinstance(s, dict) or s.get("v") != 2:
        s = {"v": 2, "offset": 0, "cid": "", "init": None, "steps": 0, "text": "", "last_text": "", "commands": [],
             "files": [], "talk": [], "calls": 0, "input": 0, "cached": 0, "output": 0, "result": None,
             "first": None, "errors": [], "bad": 0}
    try:
        size = os.path.getsize(path)
    except OSError:
        return s
    if size < int(s["offset"]):  # a new file: start over
        s.update(offset=0)
    if size == int(s["offset"]):
        return s
    base = n * BASE
    with open(path, "rb") as f:
        f.seek(int(s["offset"]))
        while True:
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n") and not final:
                break  # still being written
            s["offset"] = int(s["offset"]) + len(raw)
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                s["bad"] = int(s.get("bad") or 0) + 1
                continue
            if isinstance(ev, dict):
                take_event(s, ev, base)
    write_json(turn_path(task, n, "sum"), s)
    return s


def first_of(d_: dict, keys) -> str:
    for k in keys:
        v = d_.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def take_event(s: dict, ev: dict, base: int) -> None:
    kind = ev.get("event")
    body = ev.get(kind) if isinstance(ev.get(kind), dict) else {}
    cid = str(ev.get("conversation_id") or body.get("conversation_id") or "")
    if cid and not s.get("cid"):
        s["cid"] = cid
    if kind == EV_INIT:
        s["init"] = {k: body.get(k) for k in ("cwd", "permission_mode", "model", "agent") if k in body}
        return
    if kind == EV_RESULT:
        s["result"] = {k: body.get(k) for k in ("status", "response", "error", "duration_seconds", "num_turns", "usage",
                                                DENIED_ACTIONS, FINISHED_REASON)}
        return
    if kind != EV_STEP:
        return
    idx = body.get("step_index")
    step = base + (int(idx) if isinstance(idx, int) and idx >= 0 else int(s["steps"]))
    s["steps"] = max(int(s["steps"]), step - base + 1)
    u = body.get("usage") if isinstance(body.get("usage"), dict) else None
    if u and body.get("state") == STATE_DONE:
        inp, cached, out = (int(u.get(k) or 0) for k in (USAGE_IN, USAGE_CACHED, USAGE_OUT))
        s["calls"] += 1
        s["input"] += max(inp - cached, 0)
        s["cached"] += cached
        s["output"] += out
        if s.get("first") is None:
            s["first"] = {"input": max(inp - cached, 0), "cached": cached}
        s["context"] = inp + out
    seq = s.setdefault("seq", [])
    if body.get("step_type") == STEP_USER:
        seq[:] = []  # the report covers what came after the last message to the agent
    elif body.get("step_type") == STEP_SYSTEM and body.get("state") == STATE_DONE:
        if seq and seq[-1].get("k") != "s":
            seq.append({"k": "s"})
    if body.get("step_type") == STEP_RESPONSE:
        delta = body.get("text_delta")
        if isinstance(delta, str):
            if not seq or seq[-1].get("step") != step:
                seq.append({"k": "r", "step": step, "text": ""})
            seq[-1]["text"] += delta
            if s.get("text_step") != step:
                s["text_step"], s["text"] = step, ""
            s["text"] += delta
            if s["text"].strip():
                s["last_text"] = s["text"]
        return
    info = body.get("tool_info") if isinstance(body.get("tool_info"), dict) else {}
    tool = d.compat.tool(str(body.get("tool_name") or info.get("name") or "")) if hasattr(d.compat, "tool") \
        else str(body.get("tool_name") or info.get("name") or "")
    if body.get("step_type") != STEP_TOOL or body.get("state") not in (STATE_DONE, STATE_ERROR):
        return
    params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
    output = info.get("output") if isinstance(info.get("output"), str) else ""
    error = info.get("error") if isinstance(info.get("error"), dict) else None
    if error:
        msg = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()
        s["errors"].append({"step": step, "tool": tool, "type": str(error.get("type") or ""), "message": msg[:300]})
        if DENIED_ERROR.search(msg):
            what = first_of(params, PARAM_COMMAND) or first_of(params, PARAM_FILE) or tool
            s.setdefault("denied", []).append(f"{tool}: {what[:120]} — {msg[:200]}")
    if tool == TOOL_RUN:
        line = re.sub(r"\s+", " ", first_of(params, PARAM_COMMAND)).strip()
        if d.TALK.search(line):
            for w in d.wire_lines(output):
                s["talk"].append({"id": str(w["id"]), "from": "antigravity", "kind": w[d.WIRE],
                                  "text": w["text"].strip()[:d.TALK_LIMIT], "step": step, "at": time.time()})
        elif line:
            m = EXIT_CODE.search(output)
            code = int(m.group(1)) if m else (1 if error else None)
            s["commands"].append({"line": line, "code": code, "out": d.outcome(output)})
            s["commands"] = s["commands"][-40:]
    elif tool in FILE_TOOLS or tool in ("write_file", "edit_file"):
        p = first_of(params, PARAM_FILE)
        if p and p not in s["files"]:
            s["files"].append(p)


def stderr_tail(task: dict, n: int, limit: int = 600) -> str:
    try:
        with open(turn_path(task, n, "err"), "rb") as f:
            f.seek(max(os.path.getsize(turn_path(task, n, "err")) - 8000, 0))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)[-limit:]


def denied(task: dict, n: int) -> List[str]:
    """The actions it wasn't allowed — a headless run can't ask a person, so what the guard or the CLI would have asked
    about is denied: the denied tool steps, the result's `denied_actions`, notices on stderr."""
    s = parse(task, n, final=not alive(task, n))
    out = list(s.get("denied") or [])
    for a_ in (s.get("result") or {}).get(DENIED_ACTIONS) or []:
        if isinstance(a_, dict) and not out:
            out.append(f"{a_.get('action') or '?'} ({a_.get('display_name') or '?'})")
    try:
        with open(turn_path(task, n, "err"), "rb") as f:
            text = f.read(400_000).decode("utf-8", "replace")
    except OSError:
        return out[:10]
    for m in SOFT_DENIED.finditer(text):
        line = re.sub(r"\s+", " ", m.group(0)).strip()
        if line and line not in out:
            out.append(line[:240])
    return out[:10]


# ─────────────────────────────── the state of a task ───────────────────────────────

def turn_of(task: dict) -> int:
    return max(len(task.get("turns") or []) - 1, 0)


def sync(task: dict, s: dict) -> None:
    """Bring the record up to what the events show: the conversation id, the agent's messages to Claude Code."""
    fields: Dict[str, object] = {}
    if s.get("cid") and not task.get("cid"):
        fields["cid"] = s["cid"]
    known = {m.get("id") for m in task.get("chat") or []}
    new = [m for m in s.get("talk") or [] if m["id"] not in known]
    if new:
        fields["chat_add"] = new
    if fields:
        d.update(task, **fields)


def ended(task: dict) -> Tuple[str, dict]:
    """How the current turn ended, for a turn whose process is gone: ("done" | "question" | "approval", info) —
    info["failed"] says what went wrong (and info["error"] its words)."""
    n = turn_of(task)
    s = parse(task, n, final=True)
    sync(task, s)
    ex = read_json(turn_path(task, n, "exit")) or {}
    res = s.get("result") or {}
    status = str(res.get("status") or "")
    info: Dict[str, object] = {"denied": denied(task, n)}
    if ex.get("reason") == "canceled" or status == STATUS_CANCELED:
        info["failed"] = "canceled"
        return "done", info
    if d.open_questions(task):
        return "question", dict(info, questions=d.open_questions(task))
    if ex.get("reason") == "timeout" or res.get(FINISHED_REASON) == PRINT_TIMEOUT or ex.get("code") == EXIT_PRINT_TIMEOUT:
        info.update(failed=f"stopped at its time limit ({d.span(float(task.get('timeout') or 0))}, --timeout)")
        return "done", info
    if status == STATUS_OK:
        return "done", info
    if status == STATUS_WAITING:
        info.update(failed="ended waiting for input the headless CLI can't give",
                    error=str(res.get("error") or "")[:300])
        return "done", info
    if status:
        info.update(failed="stopped by an error", error=str(res.get("error") or stderr_tail(task, n, 300))[:400])
        return "done", info
    code = ex.get("code")
    info.update(failed=f"ended without a result (exit code {code if code is not None else '?'})",
                error=stderr_tail(task, n, 400) or ("the runner itself was stopped" if not ex else ""))
    return "done", info


def state_now(task: dict) -> Tuple[str, dict]:
    """Without waiting: "running", "question" or "done" (see ended()) — and, between turns, the bridge's own work:
    messages that came while the turn ran go out as the next turn, an error turn is resumed (--retries)."""
    n = turn_of(task)
    if not task.get("turns"):
        return "done", {"failed": "never started", "error": ""}
    if alive(task, n):
        s = parse(task, n, final=False)
        sync(task, s)
        asked = d.open_questions(task)
        return ("question", {"questions": asked}) if asked else ("running", {})
    state, info = ended(task)
    if task.get("outbox") and state in ("done", "question") and info.get("failed") != "canceled":
        flush_outbox(task)
        return "running", {}
    if state == "done" and resume(task, info):
        return "running", {}
    return state, info


def flush_outbox(task: dict) -> None:
    """Claude Code's messages that came while a turn ran: the next turn carries them."""
    box = list(task.get("outbox") or [])
    if not box:
        return
    asked = d.open_questions(task)
    parts = []
    for m in box:
        if asked and m.get("kind") == "answer":
            q = re.sub(r"\s+", " ", asked[-1]["text"]).strip()
            parts.append(d.ANSWER.format(question=q if len(q) <= 100 else q[:99] + "…", message=m["text"]))
        else:
            parts.append(d.MESSAGE.format(message=m["text"]))
    d.update(task, outbox=[])
    begin_turn(task, "\n\n".join(parts) + d.RESUME, kind="message")
    d.update(task, chat_add=[dict(m, step=int(task.get("turn") or 0)) for m in box])


def resume(task: dict, info: dict) -> bool:
    """An error turn with resumes left (--retries): a «go on» turn in the same conversation — once per error, by
    whoever sees it first (the claim in the record), unless the error is one a resume won't fix."""
    if info.get("failed") != "stopped by an error" or not task.get("retries_max") or not task.get("cid"):
        return False
    err = str(info.get("error") or "")
    n = turn_of(task)
    if d.PERMANENT_ERROR.search(err):
        if int(task.get("no_resume_at", -1)) != n:
            d.update(task, no_resume_at=n, last_error=err[:300])
        return False
    entry = {"at": time.time(), "count": (n + 1) * BASE, "step": n * BASE, "error": err[:300]}
    taken = [False]

    def claim(rec: dict) -> bool:
        if any(int(r.get("count", -1)) == entry["count"] for r in rec.get("resumes") or []):
            taken[0] = True
            return False
        if rec.get("state") == "canceled" or len(d.turn_resumes(rec)) >= int(rec.get("retries_max") or 0):
            return False
        entry["n"] = len(d.turn_resumes(rec)) + 1
        rec.update(resumes=(rec.get("resumes") or []) + [entry], last_error=err[:300])
        return True

    if not d.transact(task, claim):
        return taken[0]
    keep_turn = int(task.get("turn") or 0)
    try:
        begin_turn(task, d.MESSAGE.format(message=d.RESUME_ERROR.format(what=f" («{err[:200]}»)" if err else "")),
                   kind="resume")
    except d.Failure:
        return False
    d.update(task, turn=keep_turn)  # a resume continues the turn: the report covers it whole
    return True


def wait(task: dict, timeout: float) -> Tuple[str, dict]:
    deadline = time.time() + max(timeout, 1.0)
    turns = task.get("turns") or []
    if turns:  # the turn's own limit (agy's --print-timeout, then the runner's) ends just after: see it end
        hard = float(turns[-1].get("at") or 0) + float(task.get("timeout") or 0) + 75
        if deadline < hard <= deadline + 90:
            deadline = hard
    while True:
        state, info = state_now(task)
        if state != "running":
            return state, info
        if time.time() >= deadline:
            return "timeout", {}
        time.sleep(min(2.0, max(deadline - time.time(), 0.2)))
        task.update(d.load(task["id"]) or {})


def cancel(task: dict) -> bool:
    """Stop the running turn (its runner kills agy's process tree). True if one was running."""
    n = turn_of(task)
    if not task.get("turns") or not alive(task, n):
        return False
    with open(turn_path(task, n, "cancel"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    pid = (read_json(turn_path(task, n, "pid")) or {}).get("pid")
    if isinstance(pid, int) and pid > 0:
        d.compat.kill_tree(pid)
    for _ in range(40):
        if os.path.exists(turn_path(task, n, "exit")):
            break
        time.sleep(0.25)
    return True


def deliver(task: dict, message: str, kind: str = "") -> str:
    """Claude Code's message to a CLI task. A headless turn can't take a message while it runs: then it waits in the
    record (`outbox`) and goes out as the next turn when this one ends. Returns "sent", "queued"."""
    message = message.strip()
    limit = d.BRIEF_LIMIT if kind == "follow-up" else d.TALK_LIMIT
    if len(message) > limit:
        raise d.Failure(f"the message has {len(message)} characters, more than the {limit} one "
                        + ("follow-up" if kind == "follow-up" else "reply") + " takes — nothing was sent.")
    n = turn_of(task)
    asked = d.open_questions(task)
    entry = {"id": "c" + uuid.uuid4().hex[:8], "from": "claude", "text": message, "at": time.time(),
             "kind": kind or ("answer" if asked else "message")}
    if task.get("turns") and alive(task, n):
        d.update(task, outbox=(task.get("outbox") or []) + [entry])
        return "queued"
    if kind == "follow-up":
        text = d.FOLLOW_UP.format(message=message)
    elif asked:
        q = re.sub(r"\s+", " ", asked[-1]["text"]).strip()
        text = d.ANSWER.format(question=q if len(q) <= 100 else q[:99] + "…", message=message) + d.RESUME
    else:
        text = d.MESSAGE.format(message=message) + d.RESUME
    keep = int(task.get("turn") or 0)
    begin_turn(task, text, kind=kind or "message")
    entry["step"] = int(task.get("turn") or 0)
    d.update(task, chat_add=[entry])
    if kind != "follow-up":
        d.update(task, turn=keep)  # an answer continues the task: the report keeps covering it whole
    return "sent"


# ─────────────────────────────── the report ───────────────────────────────

def usage(task: dict) -> dict:
    """Calls and tokens of the turns that belong to the current report (from task['turn'] on), in usage()'s shape."""
    table = d.prices()
    lo = int(task.get("turn") or 0) // BASE
    tot = {"calls": 0, "input": 0, "cached": 0, "output": 0, "models": [], "by_label": {}, "usd": 0.0, "first": None,
           "context": 0}
    label = task.get("label") or task.get("model") or ""
    for n in range(lo, len(task.get("turns") or [])):
        s = parse(task, n, final=not alive(task, n))
        calls, fresh, cached, out = s["calls"], s["input"], s["cached"], s["output"]
        if not calls and isinstance((s.get("result") or {}).get("usage"), dict):  # no per-step usage: the total
            u = s["result"]["usage"]
            inp, cached, out = (int(u.get(k) or 0) for k in (USAGE_IN, USAGE_CACHED, USAGE_OUT))
            fresh, calls = max(inp - cached, 0), 1 if inp or out else 0
        if not calls:
            continue
        tot["calls"] += calls
        tot["input"] += fresh
        tot["cached"] += cached
        tot["output"] += out
        tot["by_label"][label] = tot["by_label"].get(label, 0) + calls
        cost = d.call_cost(label, fresh, cached, out, table)
        tot["usd"] = None if cost is None or tot["usd"] is None else tot["usd"] + cost
        if tot["first"] is None:
            tot["first"] = s.get("first")
        tot["context"] = int(s.get("context") or tot["context"])
    if label:
        tot["models"] = [label]
    return tot


def report_text(task: dict) -> str:
    """The report of the newest turn of the current report that has one: its messages after the last message to the
    agent, put together like Desktop's (delegate.assemble_report(): one copy of a repeated report, a short note
    after the Stop check as a clarification); else the result's response."""
    final = ""
    for n in range(int(task.get("turn") or 0) // BASE, len(task.get("turns") or [])):
        s = parse(task, n, final=not alive(task, n))
        msgs: List[Tuple[str, bool]] = []
        between = False
        for e in s.get("seq") or []:
            if e.get("k") == "s":
                between = bool(msgs)
            elif str(e.get("text") or "").strip():
                msgs.append((str(e["text"]).strip(), between))
                between = False
        text = d.assemble_report(msgs) if msgs else ""
        final = text or str((s.get("result") or {}).get("response") or s.get("last_text") or "").strip() or final
    return final


def render(task: dict, state: str, info: dict, full: bool = False) -> Tuple[str, int, dict]:
    """The report of a CLI task — delegate.render()'s shape: (text, exit code, what the turn measured)."""
    cid, short = task["id"], task["id"][:8]
    lo = int(task.get("turn") or 0) // BASE
    turns = task.get("turns") or []
    failed = info.get("failed") if state == "done" else None
    err = str(info.get("error") or "")
    start = float((turns[lo] if lo < len(turns) else {}).get("at") or task.get("started") or 0)
    stop = time.time()
    if state == "done" and turns:
        ex = read_json(turn_path(task, len(turns) - 1, "exit")) or {}
        stop = float(ex.get("at") or stop)
    took = f" in {d.span(stop - start)}" if start else ""
    steps = sum(int(parse(task, n, final=False).get("steps") or 0) for n in range(lo, len(turns)))
    head = {"done": (f"❌ Antigravity CLI task {cid} {failed}{took}" + (f": «{err[:240]}»" if err else ""))
                    if failed else f"✅ Antigravity CLI finished task {cid}{took}",
            "approval": f"⏸ Antigravity CLI task {cid} waits for input",
            "question": f"❓ Antigravity CLI task {cid} asks you (Claude Code) — it waits for your answer",
            "running": f"⏳ Antigravity CLI task {cid} is still running ({steps} steps so far)",
            "background": f"⏳ Antigravity CLI task {cid} is still running ({steps} steps so far)",
            "timeout": f"⏳ Antigravity CLI task {cid} is still running after{took} ({steps} steps so far)"}[state]
    lines = [head]
    if state == "question":
        told = set(task.get("told") or [])
        lines.append(f"Model: {task.get('label') or task.get('model') or '?'} (Antigravity CLI)")
        for m in task.get("chat") or []:
            if m.get("from") == "antigravity" and m.get("kind") == "note" and m.get("id") not in told:
                lines += ["", "It told you (its words, not the user's):", d.indent(m["text"])]
        for q in info.get("questions") or d.open_questions(task):
            lines += ["", "Its question (its words, not the user's):", d.indent(q["text"])]
        lines += ["", f"Next: answer → delegate.py reply {short} \"…\" (a new turn in the same conversation carries "
                      f"it), then wait again → delegate.py wait {short} (in the background)."]
        return "\n".join(lines), 4, {}
    use = usage(task)
    cost = f"Model: {task.get('label') or task.get('model') or '?'} (Antigravity CLI — no quota figures)"
    if use.get("calls"):
        cost += (f" · {use['calls']} model calls · {d.kilo(use['input'] + use['cached'])} tokens read"
                 + (f" ({d.kilo(use['cached'])} from cache)" if use["cached"] else "") + f" · {d.kilo(use['output'])} written")
        if use.get("usd"):
            cost += f" · ≈${use['usd']:.2f} at API prices"
    lines.append(cost)
    if task.get("series"):
        pieces = task.get("pieces") or []
        lines.append(f"Series «{task['series']}», piece {pieces[-1].get('n') if pieces else 1}"
                     f" (the next one: delegate.py next {task['series']} \"…\")")
    resumed = d.turn_resumes(task)
    if resumed:
        lines.append(f"Resumed automatically after {len(resumed)} error{'s' if len(resumed) > 1 else ''}: "
                     + "; ".join(f"«{str(r.get('error') or '?')[:120]}»" for r in resumed))
    if task.get("outbox"):
        lines.append(f"{len(task['outbox'])} message(s) from you wait for its turn to end — they go out as the next turn.")
    files, note = changed_files(task) if state != "running" else ([], "")
    if files:
        lines += ["", "Files changed since it started (+added −removed lines):"]
        lines += [f"  {f['kind']} {f['rel']}" + ("  (old text not kept)" if f.get("no_old") else f"  +{f['plus']} −{f['minus']}")
                  for f in files[:30]]
        if len(files) > 30:
            lines.append(f"  … and {len(files) - 30} more (delegate.py diff {short} --stat)")
    elif state == "done" and not failed:
        lines += ["", "Files changed: none."]
    if note:
        lines.append(f"(Diff: {note}.)")
    commands = []
    for n in range(lo, len(turns)):
        commands += parse(task, n, final=False).get("commands") or []
    if commands:
        lines += ["", "Commands it ran (exit code):"]
        for c in commands[-10:]:
            mark = "✓" if c["code"] == 0 else "✗" if isinstance(c["code"], int) else "·"
            code = c["code"] if isinstance(c["code"], int) else "?"
            shown = c["line"] if len(c["line"]) <= 110 else c["line"][:107] + "…"
            lines.append(f"  {mark} {shown} → {code}" + (f" · {c['out']}" if c["out"] else ""))
    blocked = info.get("denied") or []
    if blocked:
        lines += ["", "⚠ Not allowed without a person's approval (a headless run can't ask — the agent went on "
                      "without them):"] + [f"  {b}" for b in blocked]
    talk = d.chat_lines(task, int(task.get("turn") or 0))
    if talk:
        lines += ["", "Talk with you while it worked:"] + talk
    final = report_text(task)
    if final:
        text = d.clean(final, task)
        if not full and len(text) > d.REPORT_LIMIT:
            text = text[:d.REPORT_LIMIT].rstrip() + f"\n… [cut — delegate.py result {short} --full]"
        lines += ["", "The agent's report (its own words — check before you rely on it):", text]
    elif state == "done":
        lines += ["", "The agent left no final message."]
    nxt = {"done": f"Next: review → delegate.py diff {short}; follow-up in the same conversation → delegate.py send "
                   f"{short} \"…\".",
           "approval": f"Next: go on with a follow-up → delegate.py send {short} \"…\", or look → delegate.py diff {short}.",
           "running": f"Next: wait → delegate.py wait {short} (in the background), or stop it → delegate.py cancel {short}.",
           "background": f"Next: wait → delegate.py wait {short} (in the background).",
           "timeout": f"Next: wait more → delegate.py wait {short} (in the background), or stop it → delegate.py "
                      f"cancel {short}."}[state]
    if failed and failed != "canceled":
        nxt = (f"Next: go on in the same conversation → delegate.py send {short} \"Go on from where you stopped.\", "
               f"or look at what it did → delegate.py diff {short}.")
    if blocked and state == "done":
        nxt += (" What it wasn't allowed: allow it in ~/.gemini/antigravity-cli/settings.json `permissions.allow` "
                "(the user's call), do it yourself, or use Antigravity Desktop, where the user can approve.")
    lines += ["", nxt]
    code = 2 if failed or (blocked and state == "done") else 3 if state in ("timeout", "running", "background") else 0
    first = use.get("first") or {}
    facts = {"calls": use.get("calls", 0), "input": use.get("input", 0), "cached": use.get("cached", 0),
             "output": use.get("output", 0), "usd": round(use["usd"], 4) if use.get("usd") else None,
             "first_call": (first.get("input", 0) + first.get("cached", 0)) if lo == 0 and first else None,
             "seconds": round(stop - start) if start else None, "quota_before": None, "quota_after": None,
             "ended": ("error" if failed == "stopped by an error" else "failed") if failed else state, "via": "cli"}
    return "\n".join(lines), code, facts


def watch_events(task: dict) -> Tuple[list, bool]:
    """The events of a CLI task for `watch`, and whether it is over."""
    short = task["id"][:8]
    state, info = state_now(task)
    out = d.talk_events(task)
    out += [(f"resume:{r.get('count')}", d.resume_line(task, r)) for r in task.get("resumes") or []]
    if state in ("running", "question"):
        return out, False
    key = f"end:{int(task.get('turn') or 0)}"
    failed = info.get("failed")
    blocked = info.get("denied") or []
    if failed:
        err = str(info.get("error") or "")
        out.append((key, f"❌ Antigravity CLI task {short} {failed}" + (f": «{err[:200]}»" if err else "")
                    + f" → delegate.py result {short}"))
    else:
        said = d.verdict_text(report_text(task))
        out.append((key, f"✅ Antigravity CLI task {short} finished" + (f" ({said})" if said else "")
                    + (f" — {len(blocked)} action(s) it wasn't allowed" if blocked else "")
                    + f" → read its report: delegate.py result {short}, then check it (delegate.py diff {short})"))
    d.update(task, state="canceled" if failed == "canceled" else "done", finished=time.time())
    return out, True


def running(task: dict) -> bool:
    """Does it take a slot now (for --max-parallel)?"""
    return bool(task.get("turns")) and alive(task, turn_of(task))


def last_report(task: dict) -> str:
    text = report_text(dict(task, turn=0))
    return d.clean(text, task)[:2500] if text else "(it left no report)"
