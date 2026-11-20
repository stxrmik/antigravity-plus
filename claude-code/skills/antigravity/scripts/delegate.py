#!/usr/bin/env python3
"""Antigravity Plus — hand a sub-task from Claude Code to a Google Antigravity agent.

Claude Code's skill `antigravity` (installed into ~/.claude/skills/antigravity by install.py) runs this script to
start a conversation in Antigravity — in the same folder, with the model Claude picks — wait for it, and read back
only what it needs: the agent's report, the files it changed with +/− line counts, the commands it ran with their
exit codes, and what the run cost. The reading, editing and test runs happen on the user's Antigravity quota
instead of Claude's tokens, and in parallel with Claude's own work.

  delegate.py budget [--claude-5h PCT] [--claude-week PCT]   the limits left (Antigravity's, and Claude's own when
                                               known), what each tier takes now, and how much to hand over
  delegate.py models [--json]                  every model: quota left, price, measured speed; the tiers
  delegate.py run --model M [--cwd DIR] [--title T] [--timeout SEC] [--no-wait] [BRIEF]   (no BRIEF: read stdin)
  delegate.py wait ID [--timeout SEC]          wait for a task, then print its report
  delegate.py result ID [--full]               the report of a task again
  delegate.py diff ID [PATH …] [--stat]        what the agent changed, as a unified diff
  delegate.py watch [ID …] [--timeout SEC]     for Claude Code's Monitor tool: one line per event while tasks run —
                                               the agent's questions and messages, an approval it waits for, the end
  delegate.py reply ID MESSAGE                 answer the agent's question, or tell it something while it works
  delegate.py send ID MESSAGE [--no-wait]      a follow-up in the same conversation (the agent keeps its context)
  delegate.py cancel ID                        stop a task
  delegate.py list                             recent tasks
  delegate.py statusline                       for Claude Code's statusLine in a terminal: remembers its rate_limits

Run by the Antigravity agent in its own terminal, to talk back (the brief gives it the exact commands):
  delegate.py ask "QUESTION"                   a question for Claude Code; the answer arrives in its conversation
  delegate.py tell "MESSAGE"                   something Claude Code should know now; no answer needed

An ID may be shortened to a unique prefix (8 characters are plenty). Exit codes: 0 finished, 1 error, 2 needs
attention (an approval waits in Antigravity, or the task was canceled or failed), 3 still running after --timeout,
4 the agent asks Claude Code a question and waits for the answer (`reply`, then `wait` again).

Talking while it works: `ask` and `tell` only print — a JSON line `{"to_claude_code": …}` with the text and an id.
Claude Code's side finds those lines in the conversation itself (the command's output, or the message Antigravity
sends when a command finished in the background), so they need no file access and work inside the terminal sandbox.
Replies go back with `agentapi send-message`: between the agent's steps while it works, or waking it up when it
ended its turn to wait.

Choosing by the limits left: a tier (fast, standard, strong, ultra, claude) is a list of models, best first; the
models in no tier are «Experimental» (EXPERIMENTAL: a word on when each may fit) and go by their exact id. Each model
belongs to a quota group of Antigravity's plan (Gemini; Claude and GPT) with a weekly and a 5-hour window. A group is
"exhausted", "tight" (under 15% left, or spending ahead of the week's pace), "spend" (much left and the week
resets soon, or far behind pace) or "ok". A tier skips exhausted models, takes tight groups last and — for standard
and strong — a group with quota to spend first. Claude Code's own limits (5-hour and weekly use, from the desktop
app's get_usage or a terminal's statusLine data) don't change the model; they change how much to hand over.

How it talks to Antigravity (verified on Antigravity 2.29): the language server's own `agentapi new-conversation
--model=<model id> --title=<title> <prompt>` and `agentapi send-message <id> <text>` (environment
ANTIGRAVITY_LS_ADDRESS, ANTIGRAVITY_CSRF_TOKEN, ANTIGRAVITY_PROJECT_ID), and its RPCs GetCascadeModelConfigData,
RetrieveUserQuotaSummary, WaitForConversationFullyIdle (blocks until the conversation and its background work are
done), GetCascadeTrajectorySteps, GetTrajectoryFileDiffs, GetCascadeTrajectoryGeneratorMetadata,
GetAllCascadeTrajectories, ListProjects and CancelCascadeInvocation. An approval the agent waits for shows up as a
step with status WAITING and a `requestedInteraction`, while the conversation stays RUNNING.

Stdlib only, Python 3.8+; macOS, Linux and Windows.
"""
from __future__ import annotations

import argparse
import calendar
import difflib
import http.client
import json
import os
import pathlib
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
# compat.py: next to this script once installed; in the source tree it is the plugin's own hooks/compat.py
for _dir in (HERE, os.path.join(HERE, "..", "..", "..", "..", "antigravity-plus", "hooks"),
             os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins", "antigravity-plus", "hooks")):
    if os.path.isfile(os.path.join(_dir, "compat.py")):
        sys.path.insert(0, os.path.abspath(_dir))
        break
try:
    import compat
except ImportError:  # the installer copies compat.py next to this script
    print("❌ delegate.py: compat.py is missing next to it — reinstall Antigravity Plus (./install.sh).")
    sys.exit(1)

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".gemini", "config", "projects")
TASKS = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate")
CLAUDE_LIMITS = os.path.join(TASKS, "claude-limits.json")  # written by `statusline`, read by `budget` while fresh
SPEEDS = os.path.join(TASKS, "speeds.json")                # seconds per model call, from recent conversations
WINDOW = {"weekly": 7 * 86400.0, "5h": 5 * 3600.0}
SHIM = os.path.join(HOME, ".gemini", "antigravity", "bin", "agentapi")
PRICING = [os.path.join(HOME, ".gemini", "config", "plugins", "antigravity-plus", "skills", "usage", "pricing.json"),
           os.path.join(HERE, "..", "..", "..", "..", "antigravity-plus", "skills", "usage", "pricing.json")]
SERVICE = "exa.language_server_pb.LanguageServerService"
MARK = "[Delegated from Claude Code]"  # = hooks/conversation.py DELEGATED_MARK: the plugin's hooks know these runs by it
BRIEF_LIMIT = 20000                    # characters; Windows command lines end at 32k
REPORT_LIMIT = 3500                    # characters of the agent's last message in a report (--full: all of it)
KEEP_TASKS = 200
DEFAULT_TIMEOUT = 1800
POLL = float(os.environ.get("AGP_DELEGATE_POLL") or 15)              # seconds per wait round
IDLE_GRACE = float(os.environ.get("AGP_DELEGATE_IDLE_GRACE") or 240)  # turn over, only background work left
WATCH_POLL = float(os.environ.get("AGP_DELEGATE_WATCH_POLL") or 3)    # seconds between two looks of `watch`
WATCH_TIMEOUT = 1780  # `watch` stops on its own just before the Monitor tool's 30-minute limit, saying how to go on
IDLE_PROBE = float(os.environ.get("AGP_DELEGATE_IDLE_PROBE") or 2)  # seconds: does work it started still run?
NOT_OVER = ("running", "question", "approval", "background", "timeout")  # a task record's states until it is over

# Tiers → model ids, best first; the limits left reorder them (see the docstring). Chosen by a benchmark on Antigravity
# 2.33.0 (2026-11-20): five tasks with hidden tests — a function with tests, a feature across four files, five bugs in a
# calendar library (DST, time zones), a version resolver with backtracking, and a security fix in a 2,000-line module of
# this plugin — run through this script on 16 models (48 runs). Every run got every hidden test right but one
# (Hydrogen (Low) missed a nested case of the largest task); what differed was time and quota. Model time per task
# on the four small tasks / on the 2,000-line fix, and the share of the weekly Gemini quota (Antigravity books each
# call at its API price: ~0.042% of the week per dollar for Hydrogen and Neon, ~0.125% for Carbon and Argon):
#   Gemini 4 Hydrogen (Low)       0.5–0.6 / 1.3 min      ~0.004–0.006% / 0.010%
#   Gemini 4 Hydrogen (Medium)    0.7–1.5 / 3.8 min      ~0.005–0.010% / 0.028%
#   Gemini 4 Hydrogen (High)      1.1–2.7 / 8.7 min      ~0.007–0.016% / 0.074%
#   Gemini 4 Neon (Medium)        2.3–2.8 / 8.0 min      ~0.019–0.021% / 0.073%
#   Gemini 4 Neon (High)          3.6–5.9 / 17 min       ~0.025–0.042% / 0.14%
#   Orchestrator (Neon)           4.5–6.3 / 21 min       ~0.033–0.042% / 0.20%
#   Gemini 4 Hydrogen (XHigh)     2.8–5.2 / 40 min       ~0.018–0.033% / 0.38% (it started two reviewer subagents)
#   Gemini 4 Carbon (High), Orchestrator (Carbon)  11–21 / 26 min  ~0.09–0.21% / 0.38%
#   Gemini 3.8 Flash              5.5–6.2 min even on the light task
# So Hydrogen carries fast and standard, and Neon (Medium) — a stronger model at about Hydrogen (High)'s time and
# quota — leads strong. Gemini 4 Carbon, Google's strongest model for long-horizon work, has a tier of its own after
# strong, ultra, for the complex and specific tasks that the benchmark's ones were too small to show: there it was never
# better, at 4–12× the time and up to 14× the quota (and Carbon's price doubles on 2026-12-01). Its recommended effort
# is High — every step at full depth is what ultra is for (Google: pick Carbon directly «where every intermediate step
# needs maximum reasoning depth»); Orchestrator (Carbon), where Carbon plans and Hydrogen carries the plan out, comes
# next — about as good and 10–15% cheaper per task, but real delegated tasks on it took 21–76 min, and on read-only
# audits its planner did all the work, with no Hydrogen calls at all. Plans without Carbon (Google AI Pro) fall back to
# Argon (High) and Neon (High). Higher effort levels than a tier names only took longer. Claude Sonnet 5.5 (High) was the fastest on the hard tasks (0.8–1.6 / 6.3 min), but
# Antigravity's Claude group costs ~2.5% of its week per dollar (12% of it for this benchmark's $5 of Claude runs) —
# ~60× the Gemini quota per task —, so it stays the `claude` tier and a fallback. All Gemini models share one quota
# group, so a Gemini fallback only helps where a plan lacks a model; Gemini 3.8 Flash is there for plans without
# Gemini 4.
TIERS: Dict[str, List[str]] = {
    "fast": ["gemini-4-hydrogen-low", "gemini-4-hydrogen-medium", "gemini-4-neon-low", "gemini-3.8-flash-low",
             "gemini-3.8-flash-medium", "claude-sonnet-5-5-low"],
    "standard": ["gemini-4-hydrogen-medium", "gemini-4-hydrogen-high", "gemini-4-neon-medium", "gemini-3.8-flash-high",
                 "claude-sonnet-5-5-medium"],
    "strong": ["gemini-4-neon-medium", "gemini-4-hydrogen-high", "gemini-4-neon-high", "gemini-pro-orchestrator-neon",
               "gemini-4-hydrogen-xhigh", "claude-sonnet-5-5-high", "claude-opus-5-5-high"],
    "ultra": ["gemini-4-carbon-high", "gemini-pro-orchestrator", "gemini-4-carbon-medium", "gemini-4-argon-high",
              "gemini-4-neon-high", "claude-opus-5-5-high"],
    "claude": ["claude-sonnet-5-5-high", "claude-opus-5-5-high", "claude-sonnet-5-5-medium", "claude-opus-5-5-medium",
               "gemini-4-neon-medium"],
}
TIER_USE = {
    "fast": "light tasks and lookups: a small function with its test, a rename, a lint fix, docs, «where is X used», "
            "summaries of files or logs, babysitting a long command — ~0.5 min, ~0.005% of the weekly Gemini quota",
    "standard": "most work: implementation from a spec, tests for existing code, refactors and fixes across files, "
                "debugging with a repro, read-only reviews and audits — ~1–4 min, ~0.005–0.03%",
    "strong": "the hardest parts, or a retry after `standard` got it wrong: subtle bugs, design-heavy changes in "
              "unfamiliar code — ~2–8 min, ~0.02–0.07%",
    "ultra": "complex, specific tasks where every step needs the deepest reasoning: architecture across many modules, "
             "concurrency or protocol bugs, big migrations, niche domains, or a retry after `strong` failed — Gemini 4 "
             "Carbon at its recommended High effort, ~11–26 min, ~0.1–0.4% of the weekly Gemini quota",
    "claude": "Claude-quality work on Antigravity's separate Claude quota — quick, but ~0.5–3% of that quota's week "
              "per task",
}
# Experimental: the models Antigravity offers that are in no tier — an exact `--model <id>` still takes them. `models`
# and `budget` list every such model under «Experimental» with a word on when it may fit; one Antigravity adds later
# shows up there too, with Antigravity's own description until it gets one here. Numbers from the 2026-11-20 benchmark
# where a model ran in it.
EXPERIMENTAL = {
    "gemini-4-neon-xhigh": "Neon at its Extra High reasoning budget — the deepest Neon: a hard debugging task took it "
                           "7.7 min against Neon (Medium)'s 2.6, same result; for a problem Neon (High) left unsure",
    "gemini-4-carbon-low": "Carbon with little thinking — its knowledge and long-horizon habits at less time: a "
                           "question in a niche domain that needs Carbon's knowledge more than its depth (untested here)",
    "gemini-4-argon-low": "Argon, the original Gemini 4 model, with little thinking: a second opinion from another Gemini "
                          "4 line (untested here; ~4× Hydrogen's quota per token)",
    "gemini-4-argon-medium": "Argon at medium effort: on plans without Carbon (Google AI Pro) the closest to it; ultra "
                             "falls back to Argon (High) (untested here; ~4× Hydrogen's quota per token)",
    "gemini-pro-orchestrator-argon": "Orchestrator with Argon planning and Hydrogen carrying the plan out — the Carbon "
                                     "Orchestrator's stand-in on Google AI Pro, for long edit-heavy work (untested here)",
    "claude-opus-5-5-low": "Claude Opus with little thinking: a quick Claude-quality review, wording or answer on "
                           "Antigravity's Claude quota (~2.5% of its week per API dollar)",
    "gpt-oss-120b-medium": "OpenAI's open-weight GPT-OSS 120B on the «Claude and GPT models» quota, 80k context window "
                           "in Antigravity: a second opinion from neither Google nor Anthropic; Antigravity removes it "
                           "on 2026-11-30",
}
STATE_RANK = {"spend": 0, "ok": 1, "tight": 2, "exhausted": 3}
FAILED_ENDS = {
    "EXECUTOR_TERMINATION_REASON_USER_CANCELED": "canceled",
    "EXECUTOR_TERMINATION_REASON_ERROR": "stopped by an error",
    "EXECUTOR_TERMINATION_REASON_MAX_INVOCATIONS": "stopped at Antigravity's step limit",
    "EXECUTOR_TERMINATION_REASON_MAX_FORCED_INVOCATIONS": "stopped at Antigravity's step limit",
    "EXECUTOR_TERMINATION_REASON_MAX_TOKEN_BUDGET_EXCEEDED": "stopped at its token budget",
    "EXECUTOR_TERMINATION_REASON_HALTED_STEP": "stopped at a halted step",
}

RULES = """{mark} This task comes from Claude Code, another AI coding agent that works on this project for the same user, at the same time as you. The user doesn't watch this conversation; Claude Code does, and it answers.

Rules for this run:
1. Do exactly the task below. Change only what it covers. No commits, pushes, deploys, global installs or settings changes; don't run /init, /git-init or /handoff and don't save memories.
2. Talk to Claude Code with these two commands — run them yourself with run_command; they only print and return at once:
   - a question you need answered: {ask}
   - something it should know now (a finding that changes the plan, a problem with the task, a file outside the task you need to touch): {tell}
   Its answers and messages arrive in this conversation as messages starting with "Claude Code"; follow them like this task. Ask only when a wrong guess would cost real rework — otherwise choose the most reasonable option and say so in your report. After asking, go on with what doesn't depend on the answer; if nothing is left, end your turn with one line saying what you wait for — the answer wakes you up. Don't poll, sleep or ask the same thing twice. Don't ask for approvals this way, and never send secrets.
3. Keep going with tool calls until the task is done — a message without a tool call ends your run.
4. After your last change, run the checks the task names (or the tests or build of the code you touched) and fix what fails.
5. Stop any server or watcher you started before you finish.
6. End with one message in this format, under 250 words, without file contents:
STATUS: done | partial | blocked
CHANGED: <path> — <what changed> (one line per file, or "nothing")
CHECKS: <command> → <result, with the exact numbers> (or "none")
NOTES: <choices you made, what is left, risks> (or "none")

{where}

Task:
{brief}"""

FOLLOW_UP = ("Follow-up from Claude Code: {message}\n\n(Same rules as before: questions go to Claude Code with the ask "
             "command, check your work after the last change, end with the STATUS / CHANGED / CHECKS / NOTES report.)")
# Claude Code's replies while the agent works. The plugin's hooks know them by these openings (hooks/conversation.py
# CLAUDE_SAYS): an agent that asked and waits is left alone until one of them arrives.
ANSWER = "Claude Code answers your question «{question}»:\n{message}"
MESSAGE = "Claude Code says:\n{message}"
RESUME = "\n\n(Go on with the task from where you stopped; same rules as before.)"
WIRE = "to_claude_code"                 # the key of the JSON line `ask` and `tell` print for Claude Code's side
TALK = re.compile(r"delegate\.py[\"']?\s+(ask|tell)\b")  # a command line of the agent talking to Claude Code
TALK_KINDS = {"ask": "question", "tell": "note"}


class Failure(Exception):
    """Something to report in one line (exit code 1)."""


class RpcError(Failure):
    def __init__(self, method: str, code: int, message: str):
        super().__init__(f"Antigravity's {method} answered HTTP {code}: {message}")
        self.code, self.message = code, message


TIMEOUTS = (socket.timeout, TimeoutError)  # the same class from Python 3.10 on
SOFT = (Failure,) + TIMEOUTS               # what an optional lookup may run into
EMPTY_SHA256 = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="  # base64 SHA-256 of "": a file that didn't exist yet


# ─────────────────────────────── the language server ───────────────────────────────

class LS:
    def __init__(self, addr: str, token: str, exe: str = ""):
        self.addr, self.token, self.exe = addr, token, exe


def _healthy(addr: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{addr}/healthz", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8") or "{}").get("status") == "ok"
    except Exception:
        return False


def discover() -> Optional[LS]:
    """Antigravity's running language server: its address, CSRF token and executable (which runs agentapi)."""
    addr, token = os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and token:
        return LS(addr, token, os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", ""))
    for pid, line in compat.processes("language_server"):
        if "--csrf_token" not in line or "antigravity" not in line.lower():
            continue
        tok = re.search(r"--csrf_token[ =]\"?([^\s\"]+)", line)
        exe = re.match(r"\s*\"?(.*?language_server[\w.-]*)", line)
        if not tok:
            continue
        for port in compat.listening_ports(pid):
            if _healthy(f"127.0.0.1:{port}"):
                return LS(f"127.0.0.1:{port}", tok.group(1), exe.group(1) if exe else "")
    return None


def launch() -> bool:
    """Start Antigravity in the background when it isn't running. True if a start was attempted."""
    try:
        if compat.MAC:
            for app in ("/Applications/Antigravity.app", os.path.join(HOME, "Applications", "Antigravity.app")):
                if os.path.isdir(app):
                    subprocess.run(["open", "-g", "-a", app], capture_output=True, timeout=30)
                    return True
            return False
        exe = compat.antigravity_node()
        if not exe:
            return False
        detach = ({"creationflags": 0x00000008 | 0x00000200} if compat.WINDOWS  # DETACHED_PROCESS | NEW_PROCESS_GROUP
                  else {"start_new_session": True})
        subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def connect() -> LS:
    ls = discover()
    if ls:
        return ls
    if not os.environ.get("AGP_DELEGATE_NO_LAUNCH") and launch():
        print("Antigravity wasn't running — started it in the background.", flush=True)
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(2)
            ls = discover()
            if ls:
                return ls
    raise Failure("Antigravity isn't running and couldn't be started from here. Ask the user to open Antigravity "
                  "(signed in), then retry — or do the work yourself.")


def rpc(ls: LS, method: str, body: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(f"http://{ls.addr}/{SERVICE}/{method}", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "x-codeium-csrf-token": ls.token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = str(json.loads(e.read().decode("utf-8") or "{}").get("message") or e.reason)
        except Exception:
            msg = str(e.reason)
        raise RpcError(method, e.code, msg)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TIMEOUTS):
            raise socket.timeout(str(e.reason))
        raise Failure(f"can't reach Antigravity's language server at {ls.addr} ({e.reason}). Is Antigravity running?")
    except TIMEOUTS:
        raise
    except (OSError, http.client.HTTPException) as e:
        raise Failure(f"lost the connection to Antigravity's language server ({e}).")
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        raise Failure(f"Antigravity's {method} gave an unreadable answer.")
    return data if isinstance(data, dict) else {}


def agentapi(ls: LS, args: List[str], project: str) -> dict:
    """Run Antigravity's agentapi (a mode of the language server's executable) and return its `response`."""
    stand_in = os.environ.get("AGP_DELEGATE_AGENTAPI")  # tests: a JSON list that stands in for agentapi
    if stand_in:
        cmd = json.loads(stand_in)
    elif ls.exe and os.path.isfile(ls.exe):
        cmd = [ls.exe, "agentapi"]
    else:
        shim = next((c for c in (SHIM, SHIM + ".cmd", SHIM + ".bat", SHIM + ".exe") if os.path.isfile(c)), None)
        shim = shim or shutil.which("agentapi")
        if not shim:
            raise Failure("Antigravity's agentapi wasn't found (~/.gemini/antigravity/bin/agentapi) — update Antigravity.")
        cmd = [shim]
    env = dict(os.environ, ANTIGRAVITY_LS_ADDRESS=ls.addr, ANTIGRAVITY_CSRF_TOKEN=ls.token,
               ANTIGRAVITY_PROJECT_ID=project)
    try:
        r = subprocess.run(cmd + args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failure(f"agentapi {args[0]} didn't run: {e}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = None
    if r.returncode != 0 or not isinstance(data, dict) or data.get("error"):
        detail = data.get("error") if isinstance(data, dict) and data.get("error") else (r.stderr or r.stdout)
        raise Failure(f"agentapi {args[0]} failed (exit {r.returncode}): {str(detail).strip()[-500:]}")
    resp = data.get("response")
    return resp if isinstance(resp, dict) else {}


# ─────────────────────────────── models and quota ───────────────────────────────

def models(ls: LS) -> Tuple[List[dict], str]:
    """The models Antigravity offers right now, and the id of its default one. `left` is the fraction of the
    model's quota left (None: not reported)."""
    d: dict = {}
    configs: List[dict] = []
    for attempt in range(6):  # right after Antigravity starts the list can still be empty
        d = rpc(ls, "GetCascadeModelConfigData", {})
        configs = [c for c in d.get("clientModelConfigs") or [] if isinstance(c, dict) and c.get("modelId")]
        if configs or attempt == 5:
            break
        time.sleep(2)
    out = []
    for c in configs:
        q = c.get("quotaInfo")
        out.append({"id": str(c["modelId"]), "label": str(c.get("label") or c["modelId"]),
                    "enum": str((c.get("modelOrAlias") or {}).get("model") or ""),
                    # proto3 JSON drops zeros: a quota without remainingFraction has nothing left
                    "left": float(q.get("remainingFraction") or 0.0) if isinstance(q, dict) else None,
                    "reset": str(q.get("resetTime") or "") if isinstance(q, dict) else "",
                    "tag": str(c.get("tagTitle") or ""), "note": str(c.get("tagDescription") or "")})
    if not out:
        raise Failure("Antigravity lists no models — is it signed in? Ask the user to open Antigravity and sign in.")
    enum = str(((d.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model") or "")
    return out, next((m["id"] for m in out if enum and m["enum"] == enum), "")


def quota(ls: LS) -> List[dict]:
    """The plan's limits: [{name, models (lower-case families), buckets {window: {left, reset, disabled}}}]. Asked with
    forceRefresh: without it the language server answers from a cache it refreshes only every ~5 minutes (measured
    2026-11-19), so «limit 84.8% → 78.4% left» in a report would miss the task's last minutes; the server books each
    model call within seconds. When the forced read fails (it sometimes answers 500), the cached one does."""
    d = None
    for body in ({"forceRefresh": True}, {}):
        try:
            d = rpc(ls, "RetrieveUserQuotaSummary", body)
            break
        except SOFT:
            continue
    if d is None:
        return []
    groups = []
    for g in ((d.get("response") or {}).get("groups") or []):
        if not isinstance(g, dict):
            continue
        desc = str(g.get("description") or "")
        names = [n.strip().lower() for n in desc.split(":", 1)[1].split(",")] if ":" in desc else []
        buckets = {}
        for b in g.get("buckets") or []:
            if isinstance(b, dict):
                buckets[str(b.get("window") or b.get("bucketId") or "?")] = {
                    "left": float(b.get("remainingFraction") or 0.0), "reset": str(b.get("resetTime") or ""),
                    "disabled": bool(b.get("disabled"))}
        groups.append({"name": str(g.get("displayName") or "?"), "models": names, "buckets": buckets})
    return groups


def family(label: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", label).strip().lower()


def group_of(label: str, groups: List[dict]) -> Optional[dict]:
    """The quota group a model draws on. Groups list families («gemini 4 neon») and whole labels whose brackets
    name a variant, not an effort level («orchestrator (carbon)», «orchestrator (neon)»)."""
    fam, whole = family(label), (label or "").strip().lower()
    return next((g for g in groups if (whole and whole in g["models"]) or (fam and fam in g["models"])), None)


def weekly_left(label: str, groups: List[dict]) -> Optional[float]:
    b = ((group_of(label, groups) or {}).get("buckets") or {}).get("weekly")
    return b["left"] if b else None


def usable(m: dict) -> bool:
    return m.get("left") is None or m["left"] > 0


def fuzzy(arg: str, ms: List[dict]) -> Optional[dict]:
    """The model a few words of its label or id name. When several match, the one whose id adds the fewest words
    to them, then Antigravity's order: «orchestrator» is gemini-pro-orchestrator — Orchestrator (Carbon), though
    Antigravity lists Orchestrator (Neon) first —, «neon high» isn't gemini-4-neon-xhigh."""
    words = re.findall(r"[a-z0-9.]+", arg)
    hits = [m for m in ms if words and all(w in (m["label"] + " " + m["id"]).lower() for w in words)]
    return min(hits, key=lambda m: len([p for p in re.findall(r"[a-z0-9.]+", m["id"].lower()) if p not in words]),
               default=None)  # min keeps the first of equals


def state_of(group: Optional[dict], now: Optional[float] = None) -> dict:
    """How a quota group stands: {status: exhausted | tight | ok | spend, week, h5 (fractions left or None), pace,
    text}. pace = the week's fraction left ÷ the fraction of the week left: under 1 means spending faster than the
    plan lasts, over 1 means quota to spare."""
    now = time.time() if now is None else now
    if not group:
        return {"status": "ok", "week": None, "h5": None, "pace": None, "text": "limits not reported"}
    wk, h5 = group["buckets"].get("weekly"), group["buckets"].get("5h")
    if h5 and h5.get("disabled"):  # the 5-hour window doesn't apply while the weekly one is used up
        h5 = None
    week, five = (wk["left"] if wk else None), (h5["left"] if h5 else None)
    wreset = parse_ts(wk["reset"]) if wk else None
    share = min(max((wreset - now) / WINDOW["weekly"], 0.0), 1.0) if wreset else None
    pace = week / share if week is not None and share and share > 0.02 else None
    if (week is not None and week <= 0) or (five is not None and five <= 0):
        when = until(wk["reset"] if week is not None and week <= 0 else h5["reset"])
        return {"status": "exhausted", "week": week, "h5": five, "pace": pace, "until": when,
                "text": f"used up — resets {when}"}
    parts = []
    if week is not None:
        parts.append(f"week {week * 100:.0f}% left" + (f" (resets {until(wk['reset'])})" if wk["reset"] else ""))
    if five is not None:
        parts.append(f"5 h {five * 100:.0f}% left")
    if (week is not None and week < 0.15) or (pace is not None and pace < 0.7) or (five is not None and five < 0.15):
        status = "tight"
    elif week is not None and wreset and ((wreset - now < 36 * 3600 and week >= 0.25) or
                                          (pace is not None and pace >= 1.5 and week >= 0.4)):
        status = "spend"
    else:
        status = "ok"
    if pace is not None:
        parts.append("spending faster than the week lasts" if pace < 0.85 else "on pace" if pace < 1.5
                     else "far behind pace")
    return {"status": status, "week": week, "h5": five, "pace": pace, "text": ", ".join(parts) or "?"}


STATE_TEXT = {"spend": "quota to spare — use it", "ok": "fine", "tight": "tight — save it",
              "exhausted": "unavailable"}


def experimental(ms: List[dict]) -> List[Tuple[dict, str]]:
    """The models Antigravity offers right now that are in no tier, each with a word on when it may fit."""
    tiered = {i for ids in TIERS.values() for i in ids}
    return [(m, EXPERIMENTAL.get(m["id"]) or m.get("note") or "no description yet — try it on a small task first")
            for m in ms if m["id"] not in tiered]


def generic(tier: str, ms: List[dict], default: str) -> List[dict]:
    """When Antigravity has renamed its models: the closest ones by label, best first."""
    pats = {"fast": [r"hydrogen.*\(low\)", r"hydrogen.*\(medium\)", r"neon.*\(low\)", r"flash(?!.*lite).*\(low\)",
                     r"flash(?!.*lite)"],
            "standard": [r"hydrogen.*\(medium\)", r"hydrogen.*\(high\)", r"neon.*\(medium\)", ""],
            "strong": [r"neon.*\(medium\)", r"hydrogen.*\(high\)", r"neon.*\(high\)", r"^orchestrator \(neon\)",
                       r"\(high\)"],
            "ultra": [r"carbon.*\(high\)", r"^orchestrator \(carbon\)", r"carbon.*\(medium\)", r"argon.*\(high\)",
                      r"neon.*\(high\)", r"\(high\)"],
            "claude": [r"claude.*sonnet.*\(high\)", r"claude.*opus.*\(high\)", r"claude"]}[tier]
    out: List[dict] = []
    for p in pats:
        for m in ms:
            if m not in out and ((not p and m["id"] == default) or (p and re.search(p, m["label"].lower()))):
                out.append(m)
    return out or [m for m in ms if m["id"] == default] or ms[:1]


def pick(arg: str, ms: List[dict], default: str, groups: Optional[List[dict]] = None,
         now: Optional[float] = None) -> Tuple[dict, List[str], str]:
    """The model for --model, the notes on models it passed over, and why it is this one. --model is a tier, an
    exact id or label, a few words of a label («carbon high»), or an Antigravity alias (flash_lite, flash, pro).
    Within a tier the limits left decide (see the docstring); an exact model gives way only when its quota is
    used up, to the next model of its tier."""
    a = (arg or "").strip().lower()
    groups = groups or []
    states = {g["name"]: state_of(g, now) for g in groups}
    def state(m: dict) -> dict:
        return states.get((group_of(m["label"], groups) or {}).get("name", ""), state_of(None))
    def ok(m: dict) -> bool:
        return usable(m) and state(m)["status"] != "exhausted"
    def where(m: dict) -> str:
        g = group_of(m["label"], groups)
        return f"«{g['name']}» {state(m)['text']}" if g else "limits not reported"
    def unless_fine(m: dict) -> str:  # a quota worth mentioning: tight, or with quota to spare
        return "" if state(m)["status"] == "ok" or not group_of(m["label"], groups) else f"; {where(m)}"
    by_id = {m["id"].lower(): m for m in ms}
    notes: List[str] = []
    if a in ("flash_lite", "flash", "pro"):
        return ({"id": a, "label": f"{a} (Antigravity's alias)", "left": None, "reset": "", "enum": "", "tag": "",
                 "note": ""}, notes, "Antigravity's own alias, as its subagents use it")
    if a in TIERS:
        listed = [by_id[c] for c in TIERS[a] if c in by_id] or generic(a, ms, default)
    else:
        m = by_id.get(a) or next((x for x in ms if x["label"].lower() == a), None) or fuzzy(a, ms)
        if not m:
            raise Failure(f"unknown model {arg!r}. Use a tier ({', '.join(TIERS)}) or one of: "
                          + ", ".join(x["id"] for x in ms))
        if ok(m):
            return m, notes, ("your choice" + unless_fine(m) + (" — a lower tier would save it"
                                                                if state(m)["status"] == "tight" else ""))
        tier = next((t for t, ids in TIERS.items() if m["id"] in ids), "")
        listed = [m] + [by_id[c] for c in TIERS.get(tier, []) if c in by_id and by_id[c] is not m]
    def used_up(x: dict) -> str:
        if not usable(x):
            return "quota used up" + (f" — resets {until(x['reset'])}" if x["reset"] else "")
        return "quota used up — resets " + str(state(x).get("until") or "later")
    live = [x for x in listed if ok(x)]
    if not live:
        raise Failure(f"no model with quota left for {arg!r}: "
                      + "; ".join(f"{x['label']}: {used_up(x)}" for x in listed)
                      + ". Pick another tier or model (delegate.py budget), or do the work yourself.")

    def order(x: dict) -> int:  # stable sort: the tier's own order stays within each state
        rank = STATE_RANK[state(x)["status"]]
        return max(rank, 1) if a == "fast" else rank  # fast stays fast: only a tight quota moves a model back
    # claude and ultra keep their order: they name the model family the task needs, a quota with time to spare
    # elsewhere doesn't change that (a used-up model is still skipped)
    m = live[0] if a not in TIERS or a in ("claude", "ultra") else sorted(live, key=order)[0]
    first = live[0]
    passed = [x for x in listed[:listed.index(first)] if not ok(x)]  # used up, and ahead of what was taken
    notes = [f"{x['label']}: {used_up(x)}" for x in passed]
    if m is not first:
        why = (f"instead of {first['label']}: {where(m)} — use it" if state(m)["status"] == "spend"
               else f"instead of {first['label']}: {where(first)} — tight, saving it; {where(m)}")
    elif passed:
        why = f"{notes[0]} → next with quota left" + unless_fine(m)
    else:
        why = (f"first choice of {a}" if a in TIERS else "your choice") + unless_fine(m)
    if all(state(x)["status"] == "tight" for x in live):
        why += ". Every quota this tier draws on is tight: hand over only what's worth it"
    if notes:
        notes.append(f"→ took {m['label']} instead")
    return m, notes, why


# ─────────────────────────────── Claude Code's own limits ───────────────────────────────

def parse_used(value: Optional[str], now: float) -> Tuple[Optional[float], Optional[float]]:
    """«44», «44%» or «44@2026-11-18T01:00:00Z» (percent used, and when the window resets) → (44.0, epoch)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*%?\s*(?:@\s*(\S+))?\s*$", value or "")
    if not m:
        return None, None
    return float(m.group(1)), parse_ts(m.group(2) or "")


def claude_limits(a, now: float) -> dict:
    """Claude Code's own plan use: from --claude-5h / --claude-week (what the desktop app's get_usage or /usage
    shows), or from the statusLine record when it is under 30 minutes old."""
    h5, h5_reset = parse_used(getattr(a, "claude_5h", None), now)
    week, week_reset = parse_used(getattr(a, "claude_week", None), now)
    if h5 is not None or week is not None:
        return {"h5": h5, "week": week, "h5_reset": h5_reset, "week_reset": week_reset, "source": "given"}
    try:
        with open(CLAUDE_LIMITS, encoding="utf-8") as f:
            rec = json.load(f)
        if now - float(rec.get("at") or 0) < 1800:
            out = {"source": "statusLine"}
            for key, name in (("five_hour", "h5"), ("seven_day", "week")):
                w = rec.get(key) or {}
                reset = float(w.get("resets_at") or 0)
                fresh = w.get("used_percentage") is not None and (not reset or reset > now)
                out[name] = float(w["used_percentage"]) if fresh else None
                out[name + "_reset"] = reset or None
            if out["h5"] is not None or out["week"] is not None:
                return out
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {"h5": None, "week": None, "h5_reset": None, "week_reset": None, "source": ""}


def claude_state(lim: dict, now: float) -> Tuple[str, str]:
    """("critical" | "tight" | "ok" | "unknown", text) for Claude Code's own plan."""
    h5, week = lim.get("h5"), lim.get("week")
    if h5 is None and week is None:
        return "unknown", "unknown"
    parts = []
    if h5 is not None:
        parts.append(f"5 h {h5:.0f}% used" + (f" (resets {until_epoch(lim['h5_reset'], now)})" if lim.get("h5_reset") else ""))
    ahead = False
    if week is not None:
        parts.append(f"week {week:.0f}% used" + (f" (resets {until_epoch(lim['week_reset'], now)})"
                                                  if lim.get("week_reset") else ""))
        if lim.get("week_reset"):
            elapsed = 1 - min(max((lim["week_reset"] - now) / WINDOW["weekly"], 0.0), 1.0)
            ahead = week / 100 > elapsed + 0.2
            if ahead:
                parts.append(f"ahead of the week's pace ({elapsed * 100:.0f}% of it has passed)")
    if (h5 or 0) >= 90 or (week or 0) >= 90:
        return "critical", ", ".join(parts)
    if (h5 or 0) >= 70 or (week or 0) >= 75 or ahead:
        return "tight", ", ".join(parts)
    return "ok", ", ".join(parts)


def advice(cstate: str, states: List[dict], groups: List[dict]) -> List[str]:
    """How much to hand over, from both sides' limits."""
    live = [s for s in states if s["status"] != "exhausted"]
    best = min((s["status"] for s in live), key=lambda x: STATE_RANK[x], default="exhausted" if states else "ok")
    out = []
    if best == "exhausted":
        out.append("Antigravity has no quota left (" + "; ".join(f"«{g['name']}» {s['text']}" for g, s in
                   zip(groups, states)) + "): do the work yourself until it resets.")
    elif best == "tight":
        out.append("Antigravity's quota is tight everywhere: hand over only bulky, low-judgment work, on `fast`; do "
                   "small things yourself.")
    else:
        for g, s in zip(groups, states):
            if s["status"] == "spend":
                out.append(f"«{g['name']}» has quota to spare ({s['text']}): hand over freely, light tasks too, and "
                           "run independent ones in parallel — the tiers take this quota first.")
            elif s["status"] == "tight":
                out.append(f"«{g['name']}» is tight ({s['text']}): the tiers take it last; keep what lands there "
                           "to bulky work.")
        if not any(s["status"] == "spend" for s in states):
            out.append("Antigravity's quota is fine: hand over self-contained work, light tasks included — `fast` for "
                       "light ones, `standard` for the rest, `strong` only for the hardest part or after a miss, `ultra` "
                       "(Gemini 4 Carbon) only for a complex, specific task that needs its depth (every tier got every "
                       "benchmark task right; the higher ones only took longer — ultra 4–12× standard's time).")
    if best != "exhausted":
        out.append({"critical": "Your own limit is almost used up: hand over everything you can describe in a brief, "
                                "keep checking short (diff --stat, one test run), and tell the user.",
                    "tight": "Your own limit is getting tight: lean on delegation more than usual, light tasks "
                             "included; keep design decisions and the checking yourself.",
                    "ok": "Your own limit is fine: keep design decisions, subtle bugs and the checking yourself.",
                    "unknown": "Your own limit is unknown here: in the Claude desktop app call get_usage (load it with "
                               "ToolSearch \"select:mcp__ccd_session_mgmt__get_usage\") and pass its numbers as "
                               "--claude-5h / --claude-week; in a terminal, /usage shows them."}[cstate])
    return out


def until_epoch(epoch: Optional[float], now: float) -> str:
    return "" if not epoch else ("now" if epoch <= now else "in " + span(epoch - now))


# ─────────────────────────────── measured speed ───────────────────────────────

def seconds(value) -> float:
    m = re.match(r"\s*(\d+(?:\.\d+)?)s\s*$", str(value or ""))
    return float(m.group(1)) if m else 0.0


def speeds(ls: LS) -> Dict[str, dict]:
    """Per model label: the median seconds to the first token and per call, over the newest 60 conversations
    (cached for six hours in SPEEDS): {label: {"first", "call", "n"}}."""
    try:
        with open(SPEEDS, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - float(cached.get("at") or 0) < 6 * 3600 and isinstance(cached.get("models"), dict):
            return cached["models"]
    except (OSError, ValueError, TypeError):
        pass
    try:
        summ = rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30).get("trajectorySummaries") or {}
    except SOFT:
        return {}
    newest = sorted(summ, key=lambda c: str((summ[c] or {}).get("lastModifiedTime") or ""), reverse=True)[:60]
    calls: Dict[str, List[Tuple[float, float]]] = {}
    deadline = time.time() + 20
    for cid in newest:
        if time.time() > deadline:
            break
        try:
            d = rpc(ls, "GetCascadeTrajectoryGeneratorMetadata", {"cascadeId": cid}, timeout=20)
        except SOFT:
            continue
        planner = ""  # Orchestrator: its speed per call counts the Hydrogen calls that carry out its plan too
        for g in d.get("generatorMetadata") or []:
            cm = (g or {}).get("chatModel") or {} if isinstance(g, dict) else {}
            label, first = str(cm.get("modelDisplayName") or ""), seconds(cm.get("timeToFirstToken"))
            if label.lower().startswith("orchestrator"):
                planner = label
            if label and first > 0:
                row = (first, first + seconds(cm.get("streamingDuration")))
                calls.setdefault(label, []).append(row)
                if planner and label != planner and label.lower().endswith("(orchestrator)"):
                    calls.setdefault(planner, []).append(row)
    out = {label: {"first": round(statistics.median(r[0] for r in rows), 1),
                   "call": round(statistics.median(r[1] for r in rows), 1), "n": len(rows)}
           for label, rows in calls.items()}
    try:
        os.makedirs(TASKS, exist_ok=True)
        with open(SPEEDS + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "models": out}, f, ensure_ascii=False)
        compat.replace(SPEEDS + ".tmp", SPEEDS)
    except OSError:
        pass
    return out


def speed_text(sp: Dict[str, dict], label: str) -> str:
    s = sp.get(label)
    return f"≈{s['call']:.1f} s per call ({s['n']} calls)" if s else ""


def prices() -> List[dict]:
    for path in PRICING:
        try:
            with open(path, encoding="utf-8") as f:
                text = re.sub(r"^\s*//.*$", "", f.read(), flags=re.M)
            return [m for m in json.loads(text).get("models", []) if isinstance(m, dict) and m.get("match")]
        except (OSError, ValueError, AttributeError):
            continue
    return []


def price(label: str, table: List[dict], day: str = "") -> str:
    """«$2/$10 per 1M» (input/output) from pricing.json, with its price changes dated on or before `day` (YYYY-MM-DD,
    default today, local time — as the usage panel prices calls)."""
    day = day or time.strftime("%Y-%m-%d")
    for m in table:
        try:
            if re.search(m["match"], label, re.I):
                rate = dict(m)
                for ch in sorted((c for c in m.get("price_changes") or [] if isinstance(c, dict)),
                                 key=lambda c: str(c.get("from"))):
                    if str(ch.get("from") or "9999") <= day:
                        rate.update(ch)
                return f"${rate['input']:g}/${rate['output']:g} per 1M"
        except (re.error, KeyError, TypeError, ValueError):
            continue
    return ""


# ─────────────────────────────── projects ───────────────────────────────

def uri_to_path(uri: str) -> str:
    p = urllib.parse.urlparse(uri or "")
    if p.scheme != "file":
        return ""
    path = urllib.parse.unquote(p.path)
    if compat.WINDOWS and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if path else ""


def inside(folder: str, root: str) -> bool:
    f, r = os.path.normcase(folder), os.path.normcase(root)
    return f == r or f.startswith(r.rstrip("\\/") + os.sep)


def projects() -> List[dict]:
    out = []
    try:
        names = sorted(os.listdir(PROJECTS))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(PROJECTS, name), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"]:
            continue
        roots = []
        for res in ((d.get("projectResources") or {}).get("resources") or []):
            if isinstance(res, dict):
                path = uri_to_path(str(res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri") or ""))
                if path:
                    roots.append(os.path.realpath(path))
        out.append({"id": d["id"], "name": str(d.get("name") or ""), "roots": roots})
    return out


def find_project(folder: str) -> Optional[Tuple[dict, str]]:
    """The Antigravity project whose folder is `folder` or holds it (the deepest one), and that folder."""
    best: Optional[Tuple[dict, str]] = None
    for p in projects():
        for root in p["roots"]:
            if inside(folder, root) and (best is None or len(root) > len(best[1])):
                best = (p, root)
    return best


def create_project(ls: LS, root: str) -> dict:
    home = os.path.realpath(HOME)
    if os.path.normcase(root) == os.path.normcase(home) or os.path.dirname(root) == root:
        raise Failure(f"{root} is the home folder or a whole disk, not a project — run it from the project's own "
                      "folder or pass --cwd.")
    pid = str(uuid.uuid4())
    name = os.path.basename(root.rstrip("\\/")) or root
    data = {"id": pid, "name": name, "projectResources": {"resources": [{"folderUri": pathlib.Path(root).as_uri()}]}}
    os.makedirs(PROJECTS, exist_ok=True)
    path = os.path.join(PROJECTS, pid + ".json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    compat.replace(path + ".tmp", path)
    deadline = time.time() + 15  # the language server picks a new project file up live
    while time.time() < deadline:
        try:
            if pid in (rpc(ls, "ListProjects", {}).get("projectIds") or []):
                break
        except SOFT:
            pass
        time.sleep(0.5)
    return {"id": pid, "name": name, "roots": [root]}


def git(args: List[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def dirty(root: str) -> List[str]:
    """Paths (relative to the repository) that differ from HEAD or are untracked."""
    out = git(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], root)
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


# ─────────────────────────────── tasks ───────────────────────────────

def save(task: dict) -> None:
    os.makedirs(TASKS, exist_ok=True)
    path = os.path.join(TASKS, task["id"] + ".json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=1)
    compat.replace(tmp, path)


def update(task: dict, **fields) -> dict:
    """Set fields of a task's record and save it, re-reading the record under a lock first: `watch`, `wait` and
    `reply` may run at the same time (a Monitor, a background wait, a reply) and each changes only its own fields.
    `task` gets the record's current fields too."""
    os.makedirs(TASKS, exist_ok=True)
    with open(os.path.join(TASKS, ".lock"), "a+", encoding="utf-8") as fh:
        compat.lock(fh)
        try:
            try:
                with open(os.path.join(TASKS, task["id"] + ".json"), encoding="utf-8") as f:
                    current = json.load(f)
            except (OSError, ValueError):
                current = None
            rec = dict(current) if isinstance(current, dict) and current.get("id") == task["id"] else dict(task)
            chat, told = fields.pop("chat_add", None), fields.pop("told_add", None)
            rec.update(fields)
            if chat:
                known = {m.get("id") for m in rec.get("chat") or []}
                rec["chat"] = (rec.get("chat") or []) + [m for m in chat if m.get("id") not in known]
            if told:  # events already shown to Claude Code (by watch, wait or result): never twice
                rec["told"] = sorted(set(rec.get("told") or []) | set(told))
            save(rec)
        finally:
            compat.unlock(fh)
    task.clear()
    task.update(rec)
    return task


def records() -> List[dict]:
    out = []
    try:
        names = os.listdir(TASKS)
    except OSError:
        return out
    for name in names:
        if name.endswith(".json"):
            try:
                with open(os.path.join(TASKS, name), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                out.append(d)
    out.sort(key=lambda d: d.get("started") or 0, reverse=True)
    return out


def prune() -> None:
    for d in records()[KEEP_TASKS:]:
        try:
            os.remove(os.path.join(TASKS, d["id"] + ".json"))
        except OSError:
            pass


def task_for(ident: str) -> dict:
    ident = (ident or "").strip()
    if not re.fullmatch(r"[\w-]{4,80}", ident):
        raise Failure(f"not a task id: {ident!r}")
    hits = [d for d in records() if d["id"].startswith(ident)]
    if len(hits) > 1:
        raise Failure(f"{ident!r} matches {len(hits)} tasks — give more of the id.")
    if hits:
        return hits[0]
    if re.fullmatch(r"[0-9a-fA-F-]{36}", ident):  # a conversation started some other way: work with it anyway
        return {"id": ident, "cwd": os.getcwd(), "root": os.getcwd(), "turn": 0, "label": "", "model": "",
                "project": "", "started": 0}
    raise Failure(f"no delegated task {ident!r} (delegate.py list shows them).")


def talk_command(verb: str) -> str:
    """The exact command the agent runs to talk to Claude Code: this script's path quoted (weak models copy a path
    with a space unquoted and break it), started the way the plugin's hooks start Python on this system."""
    text = "<your question>" if verb == "ask" else "<what it should know>"
    return f'{compat.python_cmd()} "{os.path.abspath(__file__)}" {verb} "{text}"'


def compose(brief: str, folder: str, root: str) -> str:
    where = f"Working folder: {folder}" + ("" if os.path.normcase(folder) == os.path.normcase(root)
                                           else f" (inside the workspace {root})")
    return RULES.format(mark=MARK, where=where, brief=brief.strip(), ask=talk_command("ask"),
                        tell=talk_command("tell"))


def title_of(brief: str, given: str) -> str:
    t = (given or "").strip() or next((ln.strip(" #*->") for ln in brief.splitlines() if ln.strip(" #*->")), "task")
    t = re.sub(r"\s+", " ", t)
    return "Claude Code: " + (t[:60].rstrip() + "…" if len(t) > 60 else t)


# ─────────────────────────────── waiting ───────────────────────────────

def summary(ls: LS, cid: str) -> dict:
    try:
        d = rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30)
    except TIMEOUTS:
        return {}
    s = (d.get("trajectorySummaries") or {}).get(cid)
    return s if isinstance(s, dict) else {}


def approval(ask: dict) -> dict:
    perm = ask.get("permission") if isinstance(ask.get("permission"), dict) else {}
    res = perm.get("resource") if isinstance(perm.get("resource"), dict) else {}
    out = {"action": str(res.get("action") or next(iter(ask), "?")), "target": str(res.get("target") or ""),
           "reason": str(perm.get("reason") or "")}
    if not out["target"] and not out["reason"]:
        out["target"] = json.dumps(ask, ensure_ascii=False)[:300]
    return out


def pending(ls: LS, cid: str, offset: int = 0) -> Tuple[Optional[dict], int]:
    """An approval the conversation waits for (from step `offset` on), and the offset to look from next time."""
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": offset}, timeout=30).get("steps")
    except TIMEOUTS:
        return None, offset
    steps = steps if isinstance(steps, list) else []
    for i, s in enumerate(steps, offset):
        ask = s.get("requestedInteraction") if isinstance(s, dict) else None
        if s.get("status") == "CORTEX_STEP_STATUS_WAITING" and isinstance(ask, dict) and ask:
            return dict(approval(ask), step=i), offset
    return None, offset + max(len(steps) - 1, 0)  # the last one again next time: it may still change


def answered(ls: LS, cid: str, turn: int) -> bool:
    """The agent has responded since step `turn` (right after a start or a follow-up the conversation can still
    look idle for a moment)."""
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": turn}, timeout=30).get("steps")
    except TIMEOUTS:
        return False
    return any(isinstance(s, dict) and s.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" for s in steps or [])


def wait(ls: LS, task: dict, timeout: float) -> Tuple[str, dict]:
    """("done" | "approval" | "question" | "background" | "timeout", details). "question": the agent asked Claude
    Code something (`ask`) that no reply has answered yet. "background": the agent ended its turn but something it
    started (a subagent, a command) still runs for longer than IDLE_GRACE."""
    cid = task["id"]
    since = max(int(task.get("turn") or 0), int(task.get("since") or 0))  # it must answer after this step
    began = time.time()
    deadline = began + max(timeout, 1.0)
    offset, idle_since = since, None
    while True:
        listen(ls, task)
        asked = open_questions(task)
        if asked:
            return "question", {"questions": asked}
        left = deadline - time.time()
        if left <= 0:
            return "timeout", {}
        try:
            rpc(ls, "WaitForConversationFullyIdle", {"conversationId": cid}, timeout=min(POLL, max(left, 1.0)))
            listen(ls, task, start=int(task.get("turn") or 0))  # once more over the whole turn before calling it done
            asked = open_questions(task)
            if asked:
                return "question", {"questions": asked}
            if answered(ls, cid, since) or time.time() - began > 30:
                return "done", {}
            time.sleep(1)
            continue
        except TIMEOUTS:
            pass
        ask, offset = pending(ls, cid, offset)
        if ask:
            return "approval", ask
        if summary(ls, cid).get("status") == "CASCADE_RUN_STATUS_IDLE":
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= IDLE_GRACE:
                return "background", {}
        else:
            idle_since = None


def background_runs(ls: LS, cid: str) -> bool:
    """The conversation ended its turn (its summary says IDLE), but something it started — a command that went to the
    background, a subagent — still runs: WaitForConversationFullyIdle blocks until that is over, so a short wait for
    it times out. The agent goes on by itself when that work finishes."""
    try:
        rpc(ls, "WaitForConversationFullyIdle", {"conversationId": cid}, timeout=IDLE_PROBE)
    except TIMEOUTS:
        return True
    except RpcError:
        return False  # Antigravity can't tell (no such call, or the conversation is gone): the summary decides
    return False


def last_sent(task: dict) -> float:
    """When Claude Code last wrote into the conversation: the start, a follow-up or a reply."""
    return max([float(task.get("started") or 0), float(task.get("sent") or 0)]
               + [float(m.get("at") or 0) for m in task.get("chat") or [] if m.get("from") == "claude"])


def idle_state(ls: LS, task: dict) -> str:
    """A task whose conversation is IDLE, without waiting: "background" while something it started still runs,
    "running" while a message from Claude Code hasn't been picked up yet (right after a start, a follow-up or a
    reply it can look idle for a moment — `wait` gives it 30 s too), else "done"."""
    if background_runs(ls, task["id"]):
        return "background"
    since = max(int(task.get("turn") or 0), int(task.get("since") or 0))
    if time.time() - last_sent(task) < 30 and not answered(ls, task["id"], since):
        return "running"
    return "done"


def state_now(ls: LS, task: dict) -> Tuple[str, dict]:
    """Without waiting: "question", "running", "approval", "background" (it ended its turn, but something it
    started still runs) or "done"."""
    status = summary(ls, task["id"]).get("status")
    running = status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY")
    listen(ls, task, start=None if running else int(task.get("turn") or 0))
    asked = open_questions(task)
    if asked:
        return "question", {"questions": asked}
    if running:
        ask, _ = pending(ls, task["id"])
        return ("approval", ask) if ask else ("running", {})
    return (idle_state(ls, task) if status == "CASCADE_RUN_STATUS_IDLE" else "done"), {}


# ─────────────────────────────── talking while it works ───────────────────────────────

TALK_LIMIT = 4000  # characters of one message either way
UNFINISHED = {"CORTEX_STEP_STATUS_RUNNING", "CORTEX_STEP_STATUS_WAITING", "CORTEX_STEP_STATUS_PENDING",
              "CORTEX_STEP_STATUS_QUEUED", "CORTEX_STEP_STATUS_GENERATING", "CORTEX_STEP_STATUS_UNSPECIFIED", ""}


def wire_lines(text: str) -> List[dict]:
    """The messages `ask` and `tell` printed: JSON lines {"to_claude_code": "question" | "note", "id", "text"}."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith('{"' + WIRE + '"'):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if (isinstance(d, dict) and d.get(WIRE) in ("question", "note") and isinstance(d.get("text"), str)
                and d["text"].strip() and re.fullmatch(r"[0-9a-f]{6,32}", str(d.get("id") or ""))):
            out.append(d)
    return out


def talk_in(steps: List[dict], base: int) -> Tuple[List[dict], int]:
    """What the agent said to Claude Code in `steps` (numbered from `base`): its `ask` and `tell` commands' output —
    in the command's step, or in the message Antigravity sends when a command that went to the background finished.
    Returns the messages and the step to look from next time: the first one that may still turn into a message — a
    command step not filled in yet (seen live: two commands from one response, the second without its command line
    while the first ran), an `ask` still running or waiting for approval, or one that exited 0 with no output yet."""
    out: List[dict] = []
    nxt = base + len(steps)
    for i, st in enumerate(steps, base):
        texts = []
        unfinished = str(st.get("status") or "") in UNFINISHED
        kind = str(st.get("type") or "")
        if kind == "CORTEX_STEP_TYPE_RUN_COMMAND":
            rc = st.get("runCommand") or {}
            line = str(rc.get("commandLine") or rc.get("proposedCommandLine") or "")
            if not TALK.search(line):
                if unfinished and not line.strip():
                    nxt = min(nxt, i)  # not filled in yet: it may be an ask
                continue
            said = output_text(rc)
            if not wire_lines(said) and (unfinished or rc.get("exitCode") == 0):
                nxt = min(nxt, i)  # its output isn't in yet (one that went to the background reports in a message)
                continue
            texts.append(said)
        elif st.get("type") == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE":
            msg = (st.get("systemMessage") or {}).get("agentMessage") or {}
            call = (((msg.get("sourceMetadata") or {}).get("tool") or {}).get("toolCall") or {}) if isinstance(msg, dict) else {}
            try:
                args = json.loads(str(call.get("argumentsJson") or "{}"))
            except ValueError:
                args = {}
            line = str(args.get("CommandLine") or "") if isinstance(args, dict) else ""
            if call.get("name") == "run_command" and TALK.search(line):
                texts.append(str(msg.get("content") or ""))
        elif unfinished and kind in ("", "CORTEX_STEP_TYPE_UNSPECIFIED"):
            nxt = min(nxt, i)  # a step still being set up
        for said in texts:
            for d in wire_lines(said):
                out.append({"id": str(d["id"]), "from": "antigravity", "kind": d[WIRE],
                            "text": d["text"].strip()[:TALK_LIMIT], "step": i, "at": time.time()})
    return out, nxt


def listen(ls: LS, task: dict, start: Optional[int] = None) -> List[dict]:
    """Read what the agent said to Claude Code since the last look, keep it in the task's record (`chat`), and
    return what is new. `start`: look again from that step (before calling a task finished), cursor untouched."""
    off = int(task.get("heard") or 0) if start is None else max(int(start), 0)
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": task["id"], "stepOffset": off}, timeout=30).get("steps")
    except SOFT:
        return []
    found, nxt = talk_in([s for s in steps or [] if isinstance(s, dict)], off)
    known = {m.get("id") for m in task.get("chat") or []}
    new = [m for m in found if m["id"] not in known]
    if start is None and (new or nxt != off):
        update(task, heard=nxt, chat_add=new)
    elif new:
        update(task, chat_add=new)
    return new


def open_questions(task: dict) -> List[dict]:
    """The agent's questions that no message from Claude Code has followed: asked at or after the step the
    conversation had reached when Claude Code last wrote to it."""
    chat = task.get("chat") or []
    mine = [m for m in chat if m.get("from") == "claude"]
    asked = [m for m in chat if m.get("from") == "antigravity" and m.get("kind") == "question"]
    if any(not isinstance(m.get("step"), int) for m in mine):  # an older record: the order it was heard in
        last = max((i for i, m in enumerate(chat) if m.get("from") == "claude"), default=-1)
        return [m for m in chat[last + 1:] if m in asked]
    last_step = max((m["step"] for m in mine), default=-1)
    return [m for m in asked if int(m.get("step") or 0) >= last_step]


def indent(text: str, limit: int = TALK_LIMIT) -> str:
    text = text if len(text) <= limit else text[:limit].rstrip() + " …"
    return "\n".join("  " + ln for ln in text.strip().splitlines())


def chat_lines(task: dict, since_step: int = 0) -> List[str]:
    """The talk between the agent and Claude Code in this turn, for the report: one line each, newest last."""
    out = []
    chat = sorted(task.get("chat") or [], key=lambda m: (int(m.get("step") or 0) if isinstance(m.get("step"), int)
                                                         else 0, float(m.get("at") or 0)))
    for m in chat:
        if m.get("from") == "antigravity" and int(m.get("step") or 0) < since_step:
            continue
        text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
        text = text if len(text) <= 220 else text[:219] + "…"
        who = {"question": "it asked", "note": "it told you"}.get(m.get("kind"), "") if m.get("from") == "antigravity" \
            else {"answer": "you answered", "follow-up": "your follow-up"}.get(m.get("kind"), "you said")
        out.append(f"  {who}: {text}")
    return out[-8:]


def deliver(ls: LS, task: dict, message: str, kind: str = "") -> Tuple[dict, bool]:
    """Send Claude Code's message into the agent's conversation: the answer to its open question, a message while
    it works, or (kind "follow-up") more work after it finished. Returns the conversation's summary as it was before,
    and whether this answered a question."""
    s = summary(ls, task["id"])
    if not s:
        raise Failure(f"Antigravity doesn't know conversation {task['id']}.")
    listen(ls, task)
    asked = open_questions(task)
    message = message.strip()[:TALK_LIMIT]
    if kind == "follow-up":
        text = FOLLOW_UP.format(message=message)
    elif asked:
        q = re.sub(r"\s+", " ", asked[-1]["text"]).strip()
        text = ANSWER.format(question=q if len(q) <= 100 else q[:99] + "…", message=message)
    else:
        text = MESSAGE.format(message=message)
    if kind != "follow-up" and s.get("status") == "CASCADE_RUN_STATUS_IDLE":
        text += RESUME  # it ended its turn to wait: this message wakes it up
    project = task.get("project") or ((find_project(task.get("cwd") or os.getcwd()) or ({"id": ""}, ""))[0]["id"])
    agentapi(ls, ["send-message", "--title=Claude Code", task["id"], text], project)
    entry = {"id": "c" + uuid.uuid4().hex[:8], "from": "claude", "text": message, "at": time.time(),
             "kind": kind or ("answer" if asked else "message"), "step": int(s.get("stepCount") or 0)}
    update(task, chat_add=[entry], since=int(s.get("stepCount") or 0), state="running")
    return s, bool(asked) and kind != "follow-up"


def nobody_watches(cid: str) -> bool:
    """Run by the agent: its conversation wasn't started by Claude Code (when Antigravity names it in the terminal and
    the task records can be read — inside a sandbox they may not be, and then the message goes out anyway)."""
    if not re.fullmatch(r"[\w-]{8,80}", cid or ""):
        return False
    try:
        names = os.listdir(TASKS)
    except OSError:
        return False
    return cid + ".json" not in names


# ─────────────────────────────── the report ───────────────────────────────

def parse_ts(ts: str) -> Optional[float]:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts or "")
    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))) if m else None


def span(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 36000:
        return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")
    if s < 172800:
        return f"{round(s / 3600)} h"
    return f"{s // 86400} days" + (f" {s % 86400 // 3600} h" if s % 86400 // 3600 else "")


def until(ts: str) -> str:
    t = parse_ts(ts)
    return "" if t is None else ("now" if t <= time.time() else "in " + span(t - time.time()))


def kilo(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def rel(path: str, task: dict) -> str:
    for base in (task.get("cwd"), task.get("root")):
        if base and inside(path, base) and os.path.normcase(path) != os.path.normcase(base):
            return os.path.relpath(path, base).replace("\\", "/")
    return path


def changed_files(ls: LS, task: dict) -> List[dict]:
    """The files the agent wrote or edited (not its plan artifacts), with the text before and after."""
    try:
        d = rpc(ls, "GetTrajectoryFileDiffs", {"conversationId": task["id"]}, timeout=60)
    except TIMEOUTS:
        return []
    out = []
    for x in d.get("diffs") or []:
        if not isinstance(x, dict) or x.get("isArtifactFile"):
            continue
        path = uri_to_path(str(x.get("uri") or ""))
        if not path:
            continue
        path = os.path.realpath(path)  # /tmp vs /private/tmp: compare like the task's folders
        old, new = x.get("originalContent"), x.get("modifiedContent")  # proto3 JSON leaves out empty texts
        kind = ("A" if old is None and x.get("originalContentHash", EMPTY_SHA256) == EMPTY_SHA256
                else "D" if new is None else "M")
        old, new = str(old or ""), str(new or "")
        plus = minus = 0
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                minus += 1
        out.append({"path": path, "rel": rel(path, task), "kind": kind, "old": old, "new": new,
                    "plus": plus, "minus": minus})
    return out


def output_text(rc: dict) -> str:
    out = rc.get("combinedOutput")
    if isinstance(out, dict):
        out = out.get("full") or next((v for v in out.values() if isinstance(v, str)), "")
    return str(out or "")


OUTCOME = re.compile(r"(\d+\s+(?:passed|failed|errors?|tests?|skipped|warnings?)\b[^\n]{0,60}|^Ran \d+ tests?[^\n]*"
                     r"|^(?:OK|FAILED|PASSED|ERROR)\b[^\n]{0,60}|\b(?:error|Error|ERROR)\b[^\n]{0,80})", re.M)


def outcome(text: str) -> str:
    body = text.split("Command output:", 1)[-1]
    hits = OUTCOME.findall(body[-4000:])
    if hits:
        return " · ".join(dict.fromkeys(h.strip() for h in hits[-2:]))[:140]
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    return lines[-1][:120] if lines else ""


def clean(text: str, task: dict) -> str:
    text = re.sub(r"\[([^\]]+)\]\((?:file|cci):[^)]*\)", r"\1", text)  # links into files: keep the name only
    for base in (task.get("cwd"), task.get("root")):
        if base:
            text = text.replace(base.rstrip("\\/") + os.sep, "").replace(base.rstrip("\\/") + "/", "")
    return text.strip()


def _count(u: dict, key: str) -> int:
    v = str(u.get(key) or "0")
    return int(v) if v.isdigit() else 0


def usage(ls: LS, cid: str, turn: int) -> dict:
    """Model calls and tokens since step `turn` (proto3 JSON: int64 counts come as strings)."""
    try:
        d = rpc(ls, "GetCascadeTrajectoryGeneratorMetadata", {"cascadeId": cid}, timeout=60)
    except SOFT:
        return {}
    tot = {"calls": 0, "input": 0, "cached": 0, "output": 0, "models": [], "by_label": {}}
    for g in d.get("generatorMetadata") or []:
        if not isinstance(g, dict):
            continue
        idx = [int(i) for i in g.get("stepIndices") or [] if str(i).isdigit()]
        if idx and max(idx) < turn:
            continue
        cm = g.get("chatModel") or {}
        u = cm.get("usage") or {}
        tot["calls"] += 1
        tot["input"] += _count(u, "inputTokens")
        tot["cached"] += _count(u, "cacheReadTokens")
        tot["output"] += _count(u, "outputTokens")
        name = str(cm.get("responseModel") or "")
        if name and name not in tot["models"]:
            tot["models"].append(name)
        label = str(cm.get("modelDisplayName") or name)  # Orchestrator: «Orchestrator (Carbon)» plans, Hydrogen carries out
        if label:
            tot["by_label"][label] = tot["by_label"].get(label, 0) + 1
    return tot


def split_text(by_label: Dict[str, int]) -> str:
    """« (3 on Orchestrator (Carbon), 11 on Gemini 4 Hydrogen (Orchestrator))» when more than one model made the
    calls — Orchestrator's planner and the Hydrogen that carries out its plan; "" otherwise."""
    if len(by_label) < 2:
        return ""
    return " (" + ", ".join(f"{n} on {label}" for label, n in by_label.items()) + ")"


def render(ls: LS, task: dict, state: str, info: dict, full: bool = False) -> Tuple[str, int]:
    cid, short = task["id"], task["id"][:8]
    s = summary(ls, cid)
    turn = int(task.get("turn") or 0)
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid}, timeout=60).get("steps") or []
    except TIMEOUTS:
        steps = []
    steps = [x for x in steps if isinstance(x, dict)]
    final = ""
    commands = []
    for i, st in enumerate(steps):
        if i < turn:
            continue
        if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse") or {}
            text = str(pr.get("modifiedResponse") or pr.get("response") or "").strip()
            if text:
                final = text
        elif st.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
            rc = st.get("runCommand") or {}
            line = re.sub(r"\s+", " ", str(rc.get("commandLine") or rc.get("proposedCommandLine") or "")).strip()
            if line and not TALK.search(line):  # its ask / tell show up as the talk with you instead
                commands.append({"line": line, "code": rc.get("exitCode"), "status": st.get("status", ""),
                                 "out": outcome(output_text(rc))})
    ended = str(s.get("latestExecutorTerminationReason") or "")
    failed = FAILED_ENDS.get(ended) if state == "done" else None
    start = task.get("sent") or parse_ts(str(s.get("createdTime") or "")) or task.get("started") or 0
    stop = parse_ts(str(s.get("lastModifiedTime") or "")) if state != "timeout" else time.time()
    took = f" in {span((stop or time.time()) - start)}" if start else ""
    head = {"done": f"❌ Antigravity task {cid} {failed}{took}" if failed else f"✅ Antigravity finished task {cid}{took}",
            "approval": f"⏸ Antigravity task {cid} is waiting for the user's approval in Antigravity",
            "question": f"❓ Antigravity task {cid} asks you (Claude Code) — it waits for your answer",
            "background": f"✅ Antigravity task {cid} ended its turn{took}; something it started still runs there",
            "running": f"⏳ Antigravity task {cid} is still running ({len(steps)} steps so far)",
            "timeout": f"⏳ Antigravity task {cid} is still running after{took} ({len(steps)} steps so far)"}[state]
    lines = [head]
    if state == "question":
        asked = info.get("questions") or open_questions(task)
        told = set(task.get("told") or [])
        lines.append(f"Model: {task.get('label') or task.get('model') or '?'}")
        for m in task.get("chat") or []:  # what it told you since you last heard from it
            if m.get("from") == "antigravity" and m.get("kind") == "note" and m.get("id") not in told:
                lines += ["", "It told you (its words, not the user's):", indent(m["text"])]
        for q in asked:
            lines += ["", "Its question (its words, not the user's):", indent(q["text"])]
        lines += ["", f"Next: answer → delegate.py reply {short} \"…\" (it goes on with your answer), then wait again → "
                      f"delegate.py wait {short} (in the background). Don't know? Say what you'd choose, or that it's "
                      "its call — an unanswered question keeps it waiting."]
        return "\n".join(lines), 4

    use = usage(ls, cid, turn)
    model = task.get("label") or ", ".join(use.get("models") or []) or "?"
    cost = f"Model: {model}"
    if use.get("calls"):
        cost += (f" · {use['calls']} model calls{split_text(use.get('by_label') or {})}"
                 f" · {kilo(use['input'] + use['cached'])} tokens read"
                 + (f" ({kilo(use['cached'])} from cache)" if use["cached"] else "") + f" · {kilo(use['output'])} written")
    before = task.get("quota_before")
    if isinstance(before, (int, float)) and task.get("label"):
        groups = quota(ls)
        now = weekly_left(task["label"], groups)
        if now is not None:
            grp = (group_of(task["label"], groups) or {}).get("name", "")
            cost += f" · weekly «{grp}» limit {before * 100:.1f}% → {now * 100:.1f}% left"
    lines.append(cost)
    title = str(s.get("summary") or task.get("title") or "")
    if title or task.get("project_name"):
        lines.append(f"In Antigravity: «{title}»" + (f" · project «{task['project_name']}»" if task.get("project_name") else ""))
    if task.get("created_project"):
        lines.append(f"(This first task here created the Antigravity project «{task.get('project_name')}» for {task.get('root')}.)")

    if state == "approval":
        lines += ["", f"Waiting for: {info.get('action')} {info.get('target')}".rstrip()]
        if info.get("reason"):
            lines.append(f"Reason: {info['reason'][:300]}")

    files = changed_files(ls, task)
    if files:
        lines += ["", "Files it changed (+added −removed lines):"]
        lines += [f"  {f['kind']} {f['rel']}  +{f['plus']} −{f['minus']}" for f in files[:30]]
        if len(files) > 30:
            lines.append(f"  … and {len(files) - 30} more (delegate.py diff {short} --stat)")
    elif state == "done" and not failed:
        lines += ["", "Files it changed: none through its file tools."]
    before_dirty = task.get("dirty_before")
    if isinstance(before_dirty, list) and task.get("root"):
        mine = {os.path.normcase(f["path"]) for f in files}
        extra = [p for p in dirty(task["root"]) if p not in before_dirty
                 and os.path.normcase(os.path.join(task["root"], p)) not in mine]
        if extra:
            lines.append("Also changed in the working tree since it started (by its commands — or by you): "
                         + ", ".join(extra[:12]) + (" …" if len(extra) > 12 else ""))
    if commands:
        lines += ["", "Commands it ran (exit code):"]
        for c in commands[-10:]:
            mark = "✓" if c["code"] == 0 else "✗" if isinstance(c["code"], int) else "·"
            code = c["code"] if isinstance(c["code"], int) else "?"
            shown = c["line"] if len(c["line"]) <= 110 else c["line"][:107] + "…"
            lines.append(f"  {mark} {shown} → {code}" + (f" · {c['out']}" if c["out"] else ""))
        if len(commands) > 10:
            lines.append(f"  ({len(commands) - 10} earlier commands not shown)")
    talk = chat_lines(task, turn)
    if talk:
        lines += ["", "Talk with you while it worked:"] + talk
    if final:
        text = clean(final, task)
        if not full and len(text) > REPORT_LIMIT:
            text = text[:REPORT_LIMIT].rstrip() + f"\n… [cut — delegate.py result {short} --full]"
        lines += ["", "The agent's report (its own words — check before you rely on it):", text]
    elif state == "done":
        lines += ["", "The agent left no final message."]

    nxt = {"done": f"Next: review → delegate.py diff {short} (paths to narrow it, --stat for counts); "
                   f"follow-up in the same conversation → delegate.py send {short} \"…\".",
           "approval": "Next: the user approves or rejects it in Antigravity (only they should), or stop the task → "
                       f"delegate.py cancel {short}. Don't approve the agent's actions yourself.",
           "background": f"Next: review → delegate.py diff {short}; wait for the rest → delegate.py wait {short}.",
           "running": f"Next: wait → delegate.py wait {short} (in the background), or stop it → delegate.py cancel {short}.",
           "timeout": f"Next: wait more → delegate.py wait {short} (in the background), or stop it → "
                      f"delegate.py cancel {short}."}[state]
    lines += ["", nxt]
    code = 2 if state == "approval" or failed else 3 if state in ("timeout", "running") else 0
    return "\n".join(lines), code


def finish(ls: LS, task: dict, timeout: float) -> int:
    state, info = wait(ls, task, timeout)
    text, code = render(ls, task, state, info)
    heard = [m["id"] for m in task.get("chat") or [] if m.get("from") == "antigravity" and m.get("id")]
    update(task, state=state, told_add=heard,  # a watch won't repeat them
           **({"finished": time.time()} if state in ("done", "background") else {}))
    print(text)
    return code


# ─────────────────────────────── commands ───────────────────────────────

def tier_lines(ms: List[dict], default: str, groups: List[dict], sp: Dict[str, dict], now: float,
               uses: bool) -> List[str]:
    lines = []
    for t in TIERS:
        try:
            m, _notes, why = pick(t, ms, default, groups, now)
            speed = speed_text(sp, m["label"])
            lines.append(f"  {t:9} {m['id']:26} {m['label']}" + (f" · {speed}" if speed else ""))
            lines.append(f"            {why}")
        except Failure:
            lines.append(f"  {t:9} nothing has quota left now")
        if uses:
            lines.append(f"            for {TIER_USE[t]}")
    return lines


def experimental_lines(ms: List[dict], sp: Dict[str, dict]) -> List[str]:
    exp = experimental(ms)
    if not exp:
        return []
    lines = ["", "Experimental — in no tier; --model <id> takes one when it fits:"]
    for m, note in exp:
        speed = speed_text(sp, m["label"])
        lines.append(f"  {m['id']:28} {m['label']}" + (f" · {speed}" if speed else ""))
        lines.append(f"            {note}")
    return lines


def quota_lines(groups: List[dict], now: float) -> List[str]:
    out = []
    for g in groups:
        s = state_of(g, now)
        out.append(f"  Antigravity «{g['name']}»: {s['text']} → {STATE_TEXT[s['status']]}")
    return out or ["  Antigravity: limits not reported"]


def cmd_models(a) -> int:
    ls = connect()
    ms, default = models(ls)
    groups = quota(ls)
    now = time.time()
    table = prices()
    sp = speeds(ls)
    if a.json:
        tiers = {}
        for t in TIERS:
            try:
                tiers[t] = pick(t, ms, default, groups, now)[0]["id"]
            except Failure:
                tiers[t] = None
        print(json.dumps({"models": ms, "default": default, "quota": groups, "tiers": tiers, "speeds": sp,
                          "experimental": {m["id"]: note for m, note in experimental(ms)},
                          "states": {g["name"]: state_of(g, now) for g in groups}}, ensure_ascii=False, indent=1))
        return 0
    lines = ["Quota left:"] + quota_lines(groups, now)
    lines += ["", "Tiers (--model) — what each takes now and why:"] + tier_lines(ms, default, groups, sp, now, True)
    lines += experimental_lines(ms, sp)
    lines += ["", "All models (--model <id>): quota left · price · measured speed"]
    for m in ms:
        left = "?" if m["left"] is None else f"{m['left'] * 100:.0f}%"
        extra = [x for x in (price(m["label"], table), speed_text(sp, m["label"]),
                             "Antigravity's default" if m["id"] == default else "", m["note"]) if x]
        lines.append(f"  {m['id']:28} {m['label']:28} {left:>4}" + (" · " + " · ".join(extra) if extra else ""))
    print("\n".join(lines))
    return 0


def cmd_budget(a) -> int:
    """Both sides' limits, what each tier takes now, and how much to hand over."""
    ls = connect()
    ms, default = models(ls)
    groups = quota(ls)
    now = time.time()
    lim = claude_limits(a, now)
    cstate, ctext = claude_state(lim, now)
    sp = speeds(ls)
    src = {"given": "", "statusLine": " (from the statusLine)"}.get(lim.get("source") or "", "")
    lines = ["Limits now:", f"  Claude Code, your own plan: {ctext}{src}"
             + {"critical": " → almost used up", "tight": " → getting tight", "ok": " → fine", "unknown": ""}[cstate]]
    lines += quota_lines(groups, now)
    lines += ["", "What each tier takes now (--model):"] + tier_lines(ms, default, groups, sp, now, False)
    states = [state_of(g, now) for g in groups]
    exp = experimental(ms)
    if exp:
        lines += ["", "Experimental — in no tier, an exact --model takes them (delegate.py models says when each fits): "
                  + ", ".join(m["id"] for m, _ in exp)]
    lines += ["", "How much to hand over:"] + ["  • " + x for x in advice(cstate, states, groups)]
    print("\n".join(lines))
    return 0


def cmd_statusline(a) -> int:
    """Claude Code's statusLine command in a terminal: it gets the session's JSON on stdin, rate_limits included.
    Remember them for `budget` and print a short line."""
    try:
        d = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    rl = d.get("rate_limits") if isinstance(d.get("rate_limits"), dict) else {}
    parts = []
    for key, name in (("five_hour", "5h"), ("seven_day", "week")):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
            parts.append(f"{name} {w['used_percentage']:.0f}%")
    if parts:
        try:
            os.makedirs(TASKS, exist_ok=True)
            tmp = f"{CLAUDE_LIMITS}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": time.time(), "five_hour": rl.get("five_hour"), "seven_day": rl.get("seven_day")}, f)
            compat.replace(tmp, CLAUDE_LIMITS)
        except OSError:
            pass
    model = str((d.get("model") or {}).get("display_name") or "") if isinstance(d.get("model"), dict) else ""
    print(" · ".join(x for x in (model, ("Claude used: " + " · ".join(parts)) if parts else "") if x))
    return 0


def read_brief(a) -> str:
    if a.brief_file:
        try:
            with open(a.brief_file, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise Failure(f"can't read {a.brief_file}: {e}")
    if a.brief and a.brief != "-":
        return a.brief
    if sys.stdin is None or sys.stdin.isatty():
        raise Failure("give the brief as an argument, with --brief-file, or on stdin (a heredoc).")
    return sys.stdin.read()


def cmd_run(a) -> int:
    brief = read_brief(a).strip()
    if not brief:
        raise Failure("the brief is empty.")
    if len(brief) > BRIEF_LIMIT:
        raise Failure(f"the brief has {len(brief)} characters (limit {BRIEF_LIMIT}) — put the details in a file in the "
                      "project and point the brief at it.")
    folder = os.path.realpath(a.cwd or os.getcwd())
    if not os.path.isdir(folder):
        raise Failure(f"no such folder: {folder}")
    ls = connect()
    ms, default = models(ls)
    groups = quota(ls)
    model, notes, why = pick(a.model, ms, default, groups)
    found = find_project(folder)
    created = False
    if found:
        project, root = found
    else:
        root = os.path.realpath((git(["rev-parse", "--show-toplevel"], folder) or "").strip() or folder)
        project, created = create_project(ls, root), True
    title = title_of(brief, a.title)
    top = (git(["rev-parse", "--show-toplevel"], folder) or "").strip()
    top = os.path.realpath(top) if top else ""
    before = dirty(top) if top else None  # before the agent starts: what its commands change shows up later
    resp = agentapi(ls, ["new-conversation", f"--model={model['id']}", f"--title={title}",
                         compose(brief, folder, root)], project["id"])
    cid = str((resp.get("newConversation") or {}).get("conversationId") or "")
    if not cid:
        raise Failure(f"agentapi didn't return a conversation id: {json.dumps(resp)[:300]}")
    task = {"id": cid, "title": title, "model": model["id"], "label": model["label"], "project": project["id"],
            "project_name": project["name"], "created_project": created, "cwd": folder, "root": top or root,
            "started": time.time(), "turn": 0, "state": "running", "brief": brief[:400],
            "quota_before": weekly_left(model["label"], groups), "dirty_before": before,
            "claude_session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "heard": 0, "chat": []}
    save(task)
    prune()
    print(f"Started Antigravity task {cid} — {model['label']}, project «{project['name']}»"
          + (" (created for this folder)" if created else "") + ".", flush=True)
    for n in notes:
        print("  " + n, flush=True)
    print(f"  Why this model: {why}", flush=True)
    if a.no_wait:
        print(f"Follow it with: delegate.py watch {cid[:8]} (with the Monitor tool: its questions and messages reach "
              f"you as it works), or delegate.py wait {cid[:8]} in the background.")
        return 0
    return finish(ls, task, a.timeout)


def cmd_wait(a) -> int:
    task = task_for(a.id)
    return finish(connect(), task, a.timeout)


def recorded(task: dict, state: str) -> dict:
    """The fields that bring a task's record up to the state just found (for `list`, `watch` and the Control Center),
    or {} when it says so already, the task isn't one of ours, or it was canceled and hasn't started again."""
    if (not task.get("started") or state == task.get("state")
            or (task.get("state") == "canceled" and state in ("done", "background"))):
        return {}
    return dict(state=state, **({"finished": time.time()} if state in ("done", "background") else {}))


def cmd_result(a) -> int:
    task = task_for(a.id)
    ls = connect()
    state, info = state_now(ls, task)
    text, code = render(ls, task, state, info, full=a.full)
    fields = recorded(task, state)
    shown = [m["id"] for m in task.get("chat") or [] if m.get("from") == "antigravity" and m.get("id")]
    if shown:
        fields["told_add"] = shown  # a watch won't repeat them
    if fields:
        update(task, **fields)
    print(text)
    return code


def read_message(a, what: str) -> str:
    text = " ".join(a.message) if isinstance(a.message, list) else (a.message or "")
    if text.strip() in ("", "-"):
        text = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    if not text.strip():
        raise Failure(f"the {what} is empty.")
    return text.strip()


def cmd_reply(a) -> int:
    """Answer the agent's question, or tell it something while it works: delivered at once, no waiting."""
    task = task_for(a.id)
    message = read_message(a, "reply")
    s, asked = deliver(connect(), task, message)
    short = task["id"][:8]
    idle = s.get("status") == "CASCADE_RUN_STATUS_IDLE"
    print(("Answered its question" if asked else "Sent your message") + f" to Antigravity task {task['id']}"
          + (" — it had ended its turn, so this wakes it up." if idle else " — it reads it between its steps."))
    print(f"Next: delegate.py wait {short} in the background (or keep your watch running) for its report.")
    return 0


def watched(ids: List[str]) -> List[dict]:
    """The tasks `watch` follows: the ids given, or else the running ones this Claude Code session started (from
    this folder, when the session is unknown) in the last day."""
    if ids:
        return [task_for(i) for i in ids]
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    here = os.path.realpath(os.getcwd())
    out = []
    for t in records():
        if time.time() - float(t.get("started") or 0) > 86400 or t.get("state") not in NOT_OVER:
            continue
        cwd = str(t.get("cwd") or "")
        if (t.get("claude_session") == session) if session else (cwd and (inside(cwd, here) or inside(here, cwd))):
            out.append(t)
    return out


def final_status(ls: LS, cid: str, count: int) -> str:
    """The STATUS line of the agent's last message («done», «partial: …»), or ""."""
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": max(count - 6, 0)},
                    timeout=30).get("steps") or []
    except SOFT:
        return ""
    for st in reversed([s for s in steps if isinstance(s, dict)]):
        if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse") or {}
            m = re.search(r"^\W*STATUS\W*:\W*([^\n]{1,60})", str(pr.get("modifiedResponse") or pr.get("response") or ""), re.M)
            if m:  # «STATUS: done», also as «**STATUS:** done»
                return m.group(1).strip(" *_`")
    return ""


def talk_events(task: dict, told: set) -> List[str]:
    """The agent's messages Claude Code hasn't been shown yet, as `watch` events (and mark them shown)."""
    short, out = task["id"][:8], []
    still_open = {m.get("id") for m in open_questions(task)}
    for m in task.get("chat") or []:
        if m.get("from") != "antigravity" or m.get("id") in told:
            continue
        told.add(m["id"])
        if m.get("kind") == "question" and m.get("id") not in still_open:
            continue  # you have answered it already
        if m.get("kind") == "question":
            out.append(f"❓ Antigravity task {short} asks you (its words, not the user's):\n{indent(m['text'])}\n"
                       f"  → answer: delegate.py reply {short} \"…\" — it waits for the answer")
        else:
            out.append(f"💬 Antigravity task {short} tells you (its words, not the user's):\n{indent(m['text'])}\n"
                       f"  (it keeps working; reply only if it helps: delegate.py reply {short} \"…\")")
    return out


def watch_events(ls: LS, task: dict, told: set) -> Tuple[List[str], bool]:
    """New events of one task for `watch`, and whether the task is over."""
    short = task["id"][:8]
    listen(ls, task)
    out = talk_events(task, told)
    s = summary(ls, task["id"])
    status = str(s.get("status") or "")
    if status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY"):
        ask, _ = pending(ls, task["id"], max(int(task.get("turn") or 0), int(task.get("since") or 0)))
        key = f"approval:{ask.get('step')}" if ask else ""
        if ask and key not in told:
            told.add(key)
            out.append(f"⏸ Antigravity task {short} waits for the user's approval in Antigravity: {ask.get('action')} "
                       f"{ask.get('target')}".rstrip() + f" → tell the user (only they approve), or delegate.py cancel {short}")
        return out, False
    if not status or open_questions(task):
        return out, False  # it ended its turn to wait for your answer (or the summary didn't come): not over
    if background_runs(ls, task["id"]):
        return out, False  # idle, but something it started still runs — it goes on when that finishes
    since = max(int(task.get("turn") or 0), int(task.get("since") or 0))
    if not answered(ls, task["id"], since):
        return out, False  # a message to it hasn't been picked up yet
    listen(ls, task, start=int(task.get("turn") or 0))  # once more over the whole turn before calling it over
    out += talk_events(task, told)
    if open_questions(task):
        return out, False  # it ended its turn to wait for your answer
    ended = str(s.get("latestExecutorTerminationReason") or "")
    failed = FAILED_ENDS.get(ended)
    if failed:
        out.append(f"❌ Antigravity task {short} {failed} → delegate.py result {short}")
    else:
        verdict = final_status(ls, task["id"], int(s.get("stepCount") or 0))
        out.append(f"✅ Antigravity task {short} finished" + (f" (STATUS: {verdict})" if verdict else "")
                   + f" → read its report: delegate.py result {short}, then check it (delegate.py diff {short})")
    update(task, state="canceled" if ended.endswith("USER_CANCELED") else "done", finished=time.time())
    return out, True


def cmd_watch(a) -> int:
    """For Claude Code's Monitor tool: every stdout line is an event, so print only what to act on — the agent's
    questions and messages, an approval it waits for, its end — and exit when every watched task is over."""
    tasks = watched(a.ids)
    if not tasks:
        print("No running Antigravity tasks to watch (delegate.py list shows the recent ones).")
        return 0
    ls = connect()
    told = {t["id"]: set(t.get("told") or []) for t in tasks}
    deadline = time.time() + max(a.timeout, 1.0)
    live = list(tasks)
    while live:
        for t in list(live):
            told[t["id"]] |= set(t.get("told") or [])  # what a wait or result has shown meanwhile
            events, over = watch_events(ls, t, told[t["id"]])
            if events:
                print("\n".join(events), flush=True)
            if events or over:
                update(t, told_add=sorted(told[t["id"]]))
            if over:
                live.remove(t)
        if not live:
            break
        if time.time() >= deadline:
            print("⏳ Still running: " + ", ".join(t["id"][:8] for t in live) + " — watch again: delegate.py watch "
                  + " ".join(t["id"][:8] for t in live), flush=True)
            return 3
        time.sleep(WATCH_POLL)
    return 0


def cmd_talk(a) -> int:
    """Run by the Antigravity agent: a question (`ask`) or a message (`tell`) for Claude Code. It only prints — a
    JSON line that Claude Code's side reads from the conversation — so it works anywhere, sandbox included."""
    kind = TALK_KINDS[a.cmd]
    text = " ".join(a.text or []).strip()
    if text in ("", "-") and sys.stdin is not None and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print(f'Nothing to send: put the text in quotes, e.g. {a.cmd} "Should the old function keep its name?"')
        return 1
    if nobody_watches(os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")):
        print("This conversation wasn't started by Claude Code, so nobody reads this — nothing was sent. Decide "
              "yourself and say so in your report.")
        return 0
    print(json.dumps({WIRE: kind, "id": uuid.uuid4().hex[:10], "text": text[:TALK_LIMIT]}, ensure_ascii=False))
    if kind == "question":
        print("Sent to Claude Code. Its answer arrives in this conversation as a message starting with \"Claude Code "
              "answers\", usually within minutes. Meanwhile go on with what doesn't depend on it; if nothing is left, "
              "end your turn with one line saying what you wait for — the answer wakes you up. Don't poll, sleep or "
              "ask again.")
    else:
        print("Sent to Claude Code. Go on with the task — no answer needed; if it replies, the message arrives in "
              "this conversation.")
    return 0


def cmd_diff(a) -> int:
    task = task_for(a.id)
    files = changed_files(connect(), task)
    if a.paths:
        want = [p if os.path.isabs(p) else re.sub(r"^(?:\./)+", "", p.replace("\\", "/")) for p in a.paths]
        files = [f for f in files if any(f["rel"] == w or f["rel"].endswith("/" + w) or
                                         os.path.normcase(f["path"]) == os.path.normcase(w) for w in want)]
    if not files:
        print("No file changes by this task" + (" match those paths." if a.paths else "."))
        return 0
    if a.stat:
        print("\n".join(f"{f['kind']} {f['rel']}  +{f['plus']} −{f['minus']}" for f in files))
        return 0
    out: List[str] = []
    for f in files:
        old = f["old"].splitlines() if f["kind"] != "A" else []
        new = f["new"].splitlines() if f["kind"] != "D" else []
        out += list(difflib.unified_diff(old, new, "/dev/null" if f["kind"] == "A" else "a/" + f["rel"],
                                         "/dev/null" if f["kind"] == "D" else "b/" + f["rel"], lineterm="", n=3))
    if len(out) > a.max_lines:
        cut = len(out) - a.max_lines
        out = out[:a.max_lines] + [f"… {cut} more lines — narrow it with paths, or --max-lines {len(out)}"]
    print("\n".join(out))
    print("(The agent's changes as it left them; later edits to these files aren't shown.)")
    return 0


def cmd_send(a) -> int:
    task = task_for(a.id)
    message = read_message(a, "follow-up message")
    ls = connect()
    s = summary(ls, task["id"])
    if not s:
        raise Failure(f"Antigravity doesn't know conversation {task['id']}.")
    listen(ls, task)
    if open_questions(task) or s.get("status") in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY"):
        # it waits for an answer or still works: a reply, and the report keeps covering the whole task
        _, asked = deliver(ls, task, message)
        print(("Answered its question" if asked else "It is still working — sent as a message it reads between its "
               "steps") + f" (Antigravity task {task['id']}).", flush=True)
    else:
        before = weekly_left(task["label"], quota(ls)) if task.get("label") else task.get("quota_before")
        s, _ = deliver(ls, task, message, kind="follow-up")
        update(task, turn=int(s.get("stepCount") or 0), sent=time.time(), quota_before=before)
        print(f"Sent the follow-up to Antigravity task {task['id']}.", flush=True)
    if a.no_wait:
        print(f"Wait for it with: delegate.py wait {task['id'][:8]}  (run that in the background)")
        return 0
    return finish(ls, task, a.timeout)


def cmd_cancel(a) -> int:
    task = task_for(a.id)
    ls = connect()
    try:
        rpc(ls, "CancelCascadeInvocation", {"cascadeId": task["id"]})
        print(f"Stopped Antigravity task {task['id']}.")
    except RpcError as e:
        if "not currently running" not in e.message:
            raise
        print(f"Antigravity task {task['id']} wasn't running.")
    update(task, state="canceled")
    return 0


def cmd_list(a) -> int:
    tasks = records()[:a.limit]
    if not tasks:
        print("No delegated tasks yet.")
        return 0
    ls = discover()  # just a list: don't start Antigravity for it
    try:
        live = (rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30).get("trajectorySummaries") or {}) if ls else {}
    except SOFT:
        live = {}
    status = {t["id"]: str((live.get(t["id"]) or {}).get("status") or "") for t in tasks}
    # idle, while its record says it isn't over: done, or does something it started still run? Only those are asked,
    # all at once (a busy one takes IDLE_PROBE seconds); without the language server the records answer.
    probe = [t for t in tasks if ls and t.get("state") in NOT_OVER and status[t["id"]].endswith("IDLE")
             and not open_questions(t)]
    def look(t: dict) -> str:
        try:
            return idle_state(ls, t)
        except SOFT:
            return ""
    found: Dict[str, str] = {}
    if probe:
        with ThreadPoolExecutor(max_workers=min(len(probe), 8)) as pool:
            found = dict(zip([t["id"] for t in probe], pool.map(look, probe)))
    lines = ["Recent Antigravity tasks (newest first):"]
    for t in tasks:
        state = "running" if status[t["id"]].endswith(("RUNNING", "BUSY")) else t.get("state") or "?"
        if open_questions(t) and state in NOT_OVER + ("done",):
            state = "question"  # it asked Claude Code something and waits for the answer
        elif found.get(t["id"]):
            state = found[t["id"]]  # done, background (something it started still runs) or running (not picked up)
            fields = recorded(t, state)
            if fields:
                update(t, **fields)
        elif state in ("running", "question") and status[t["id"]].endswith("IDLE"):
            state = "done"  # the language server couldn't say more
        age = span(time.time() - (t.get("started") or time.time())) + " ago"
        lines.append(f"  {t['id'][:8]}  {state:10} {age:12} {t.get('label') or t.get('model') or '?'}  "
                     f"{t.get('title') or ''}  [{t.get('cwd') or ''}]")
    print("\n".join(lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="delegate.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("budget", help="limits left on both sides, the model each tier takes now, how much to hand over")
    p.add_argument("--claude-5h", help="Claude Code's own 5-hour use in percent, as get_usage or /usage shows it "
                                       "(optionally @<reset time>)")
    p.add_argument("--claude-week", help="Claude Code's own weekly use in percent (optionally @<reset time>)")
    p = sub.add_parser("models", help="every model: quota left, price, measured speed; the tiers")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run", help="start a task (and wait for it)")
    p.add_argument("brief", nargs="?", help="the task; omit it (or '-') to read stdin")
    p.add_argument("--model", "-m", default="standard", help="tier (fast, standard, strong, ultra, claude), model id or label")
    p.add_argument("--cwd", help="the folder to work in (default: the current one)")
    p.add_argument("--title", default="", help="title of the conversation in Antigravity")
    p.add_argument("--brief-file", help="read the brief from this file")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait (default 1800)")
    p.add_argument("--no-wait", action="store_true", help="print the task id and return at once")
    p = sub.add_parser("wait", help="wait for a task and print its report")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p = sub.add_parser("result", help="the report of a task")
    p.add_argument("id")
    p.add_argument("--full", action="store_true", help="the agent's whole last message")
    p = sub.add_parser("diff", help="the agent's changes as a unified diff")
    p.add_argument("id")
    p.add_argument("paths", nargs="*")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--max-lines", type=int, default=600)
    p = sub.add_parser("send", help="a follow-up message in the same conversation")
    p.add_argument("id")
    p.add_argument("message", nargs="?")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("watch", help="for the Monitor tool: one line per event (questions, messages, approvals, the "
                                     "end) until the tasks are over")
    p.add_argument("ids", nargs="*", help="task ids (default: the running tasks this session started)")
    p.add_argument("--timeout", type=float, default=WATCH_TIMEOUT, help="seconds before it stops (default 1780)")
    p = sub.add_parser("reply", help="answer the agent's question, or tell it something while it works")
    p.add_argument("id")
    p.add_argument("message", nargs="*", help="the text ('-' or nothing: read stdin)")
    for verb in TALK_KINDS:
        p = sub.add_parser(verb, help=("run by the Antigravity agent: " + ("a question for Claude Code" if verb == "ask"
                                                                           else "a message for Claude Code")))
        p.add_argument("text", nargs="*")
    p = sub.add_parser("cancel", help="stop a task")
    p.add_argument("id")
    p = sub.add_parser("list", help="recent tasks")
    p.add_argument("--limit", type=int, default=15)
    sub.add_parser("statusline", help="as Claude Code's statusLine command: remember its rate_limits for budget")
    a = ap.parse_args(argv)
    handlers = {"budget": cmd_budget, "models": cmd_models, "run": cmd_run, "wait": cmd_wait, "result": cmd_result,
                "diff": cmd_diff, "send": cmd_send, "watch": cmd_watch, "reply": cmd_reply, "ask": cmd_talk,
                "tell": cmd_talk, "cancel": cmd_cancel, "list": cmd_list, "statusline": cmd_statusline}
    if a.cmd not in handlers:
        ap.print_help()
        return 1
    try:
        return handlers[a.cmd](a)
    except Failure as e:
        print(f"❌ {e}")
        return 1
    except TIMEOUTS:
        print("❌ Antigravity's language server stopped answering. Is Antigravity busy or frozen? Retry in a minute.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
