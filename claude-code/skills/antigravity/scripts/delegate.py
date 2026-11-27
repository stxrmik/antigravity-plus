#!/usr/bin/env python3
"""Antigravity Plus — hand a sub-task from Claude Code to a Google Antigravity agent.

Claude Code's skill `antigravity` (installed into ~/.claude/skills/antigravity by install.py) runs this script to
start a conversation in Antigravity — in the same folder, with the model Claude picks — wait for it, and read back
only what it needs: the agent's report, the files it changed with +/− line counts, the commands it ran with their
exit codes, and what the run cost. The reading, editing and test runs happen on the user's Antigravity quota
instead of Claude's tokens, and in parallel with Claude's own work.

  delegate.py budget [--claude-5h PCT] [--claude-week PCT]   the limits left (Antigravity's, and Claude's own when
                                               known), whether they last until the reset (% per day), what each tier
                                               takes now, and how much to hand over
  delegate.py models [--json]                  every model: quota left, price, measured speed; the tiers
  delegate.py estimate [--model M]             before handing over: what a task costs on each tier here, in share of
                                               the weekly quota, API dollars and minutes — from your own past tasks
  delegate.py run --model M [--cwd DIR] [--title T] [--timeout SEC] [--no-wait] [--template FILE …]
                  [--series NAME] [--retries N] [--max-parallel N] [--dry-run] [BRIEF]   (no BRIEF: read stdin)
  delegate.py next NAME [--fresh] [--no-wait] [BRIEF]   the next piece of a series (run --series NAME): the same
                                               conversation, after the previous piece is over — its context and
                                               cache carry over; --fresh (or a nearly full context) starts a new one
  delegate.py series NAME                      the pieces of a series and how each ended
  delegate.py wait ID [--timeout SEC]          wait for a task, then print its report
  delegate.py result ID [--full]               the report of a task again
  delegate.py diff ID [PATH …] [--stat]        what the agent changed, as a unified diff
  delegate.py watch [ID …] [--follow] [--timeout SEC]   for Claude Code's Monitor tool: one numbered line per event
                                               while tasks run — the agent's questions and messages, an approval it
                                               waits for, a resume after an error, the end; --follow also takes the
                                               tasks this session starts later. One watcher per task: a newer watch
                                               takes a task over, so events never arrive twice
  delegate.py reply ID MESSAGE                 answer the agent's question, or tell it something while it works
  delegate.py send ID MESSAGE [--no-wait]      a follow-up in the same conversation (the agent keeps its context)
  delegate.py cancel ID                        stop a task (or take a queued one off the queue)
  delegate.py list                             recent tasks, queued ones included
  delegate.py settings [--max-parallel N] [--retries N]   the bridge's defaults: how many tasks run at once (more
                                               wait in a queue), how many times a task stopped by an error is resumed
  delegate.py wake [--timeout SEC]             start Antigravity if it isn't running and wait until it is ready
  delegate.py statusline                       for Claude Code's statusLine in a terminal: remembers its rate_limits
  delegate.py supervise                        (starts by itself) starts queued tasks when a slot frees up and
                                               resumes tasks Antigravity stopped with an error, while any are active

Run by the Antigravity agent in its own terminal, to talk back (the brief gives it the exact commands):
  delegate.py ask "QUESTION"                   a question for Claude Code; the answer arrives in its conversation
  delegate.py tell "MESSAGE"                   something Claude Code should know now; no answer needed

An ID may be shortened to a unique prefix (8 characters are plenty); a queued task has an id «q-…» until it starts.
Exit codes: 0 finished, 1 error, 2 needs attention (an approval waits in Antigravity, or the task was canceled or
failed), 3 still running after --timeout, 4 the agent asks Claude Code a question and waits for the answer (`reply`,
then `wait` again).

Errors: when Antigravity ends a turn with an error (an API stream that broke off — «unexpected EOF» —, even on the
first model call), the task is resumed with a short «go on» message after RETRY_DELAY seconds, up to --retries times
(2 by default; never for quota, permission or model errors). Whoever notices first resumes it — the supervisor, a
wait, a watch —, the task's record keeps the others from doing it twice, and the report quotes the error.

Queue: with --max-parallel N (or `settings --max-parallel N`), a task that would be the N+1th running one waits in
a queue; the supervisor (a small background process of this script that ends when nothing is left) starts it when
one finishes. A task counts while its conversation runs or its background work does.

Talking while it works: `ask` and `tell` only print — a JSON line `{"to_claude_code": …}` with the text and an id.
Claude Code's side finds those lines in the conversation itself (the command's output, or the message Antigravity
sends when a command finished in the background), so they need no file access and work inside the terminal sandbox.
Replies go back with `agentapi send-message`: between the agent's steps while it works, or waking it up when it
ended its turn to wait.

Choosing by the limits left: a tier (fast, standard, strong, ultra, claude) is a list of models, best first; the
models in no tier are «Experimental» (Antigravity's own tag and note, FAMILY_NOTES) and go by their exact id. Each model
belongs to a quota group of Antigravity's plan (Gemini; Claude and GPT) with a weekly and a 5-hour window. A group is
"exhausted", "tight" (under 15% left, or spending ahead of the week's pace), "spend" (much left and the week
resets soon, or far behind pace) or "ok". A tier skips exhausted models, takes tight groups last and — for standard
and strong — a group with quota to spend first. Claude Code's own limits (5-hour and weekly use, from the desktop
app's get_usage or a terminal's statusLine data) don't change the model; they change how much to hand over.

How it talks to Antigravity (verified on Antigravity 2.29–2.35.1): the language server's own `agentapi new-conversation
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
import contextlib
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
try:  # client.py: what the Antigravity client itself says about its models (next to compat.py; optional)
    import client
except ImportError:
    client = None
sys.path.insert(0, HERE)
import agy_cli  # noqa: E402  the Antigravity CLI backend (--via cli), next to this script
agy_cli.bind(sys.modules[__name__])

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
NOT_OVER = ("running", "question", "approval", "background", "timeout", "queued")  # a record's states until it is over
BRIDGE_SETTINGS = os.path.join(TASKS, "settings.json")  # `delegate.py settings`: the bridge's own defaults
BRIDGE_DEFAULTS = {"max_parallel": 0, "retries": 2}      # tasks at once (0: no limit); resumes per task after errors
SUPERVISOR_LOCK = os.path.join(TASKS, ".supervisor.lock")
SUPERVISOR_LOG = os.path.join(TASKS, "supervisor.log")
DISPATCH_LOCK = os.path.join(TASKS, ".dispatch.lock")  # counting the running tasks and starting one: slots()
START_TIMEOUT = float(os.environ.get("AGP_DELEGATE_START_TIMEOUT") or 180)  # seconds for a just-started Antigravity
RETRY_DELAY = float(os.environ.get("AGP_DELEGATE_RETRY_DELAY") or 20)       # seconds after an error before a resume
SUPERVISE_IDLE = float(os.environ.get("AGP_DELEGATE_SUPERVISE_IDLE") or 90)  # the supervisor ends after this long idle
PROMPT_LIMIT = 30000  # characters of a whole first message (rules, templates, brief): it goes on agentapi's command line
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif")
# Share of a week's quota one API dollar takes (forced quota reads, 2026-11): Gemini 4 Hydrogen/Neon ~0.042%,
# Carbon ~0.125%, Claude ~2.5%. `estimate` uses what your own tasks measured first, these when it has too few.
PCT_PER_USD = (("claude", 2.5), ("gpt", 2.5), ("carbon", 0.125), ("", 0.042))  # first word in the label wins

# Tiers → model ids, best first; the limits left reorder them (see the docstring). Chosen from two benchmarks:
# 1) 2026-11-20 (Antigravity 2.33.0): five tasks with hidden tests (from a small function to a security fix in guard.py,
#    48 runs on 16 models). Every run passed except Hydrogen (Low) on one nested case of the largest task. Model time on
#    the four small tasks / largest task, and share of the weekly Gemini quota (~0.042%/$ for Hydrogen/Neon, ~0.125%/$
#    for Carbon): Hydrogen (Low) 0.5–0.6 / 1.3 min (~0.005%), Hydrogen (Medium) 0.7–1.5 / 3.8 (~0.005–0.028%), Hydrogen
#    (High) 1.1–2.7 / 8.7, Neon (Medium) 2.3–2.8 / 8.0, Neon (High) 3.6–5.9 / 17, Orchestrator (Neon) 4.5–6.3 / 21,
#    Hydrogen (XHigh) 2.8–5.2 / 40, Carbon (High) & Orchestrator (Carbon) 11–21 / 26 (~0.1–0.4%), Claude Sonnet 5.5
#    (High) 0.8–1.6 / 6.3 (~2.5% of Claude's week per $, ~60× Gemini per task).
# 2) 2026-11-24 (Antigravity 2.35.0): 20 runs on two hidden-test tasks (A: DAG scheduler with backfilling/retries/cycles,
#    14 hidden tests; B: 4 bugs in command-line segmenter, UTF-8 byte offsets, quota window, clause negation; the 20 runs
#    cost ~$10.5 at API prices in all). Model time & API cost (Task A / Task B):
#    Hydrogen (Low)     23.7 / 36.5 s   $0.12 / $0.15  (14/14, 4/4)
#    Hydrogen (Medium)  52.6 / 71.5 s   $0.17 / $0.30  (14/14, 4/4)
#    Neon (Low)         70.3 / 74.8 s   $0.30 / $0.31  (14/14, 4/4 — 2× faster than Hydrogen (High) at same quota cost)
#    Hydrogen (High)   124.6 / 159.7 s  $0.31 / $0.36  (14/14, 4/4)
#    Neon (Medium)     128.4 / 163.3 s  $0.43 / $0.49  (14/14, 4/4)
#    Hydrogen (XHigh)  202.2 / 463.2 s  $0.49 / $1.03  (14/14, 4/4; also what Antigravity's own `pro` subagent alias uses)
#    Neon (High)       277.5 / 334.1 s  $0.73 / $1.00  (14/14, 4/4)
#    Orchestrator (Neon) 327.3 s on A (3 Neon + 4 Hydrogen calls, $0.95, 14/14); B & Carbon/Orchestrator (Carbon) hit the
#    benchmark's 540 s (9 min) cap — every Carbon (High) and Orchestrator (Carbon) run did (Carbon (Medium) still
#    finished 4/4 hidden tests at 499.6 s before cutoff).
# Reclassification: `gemini-4-neon-low` enters `standard` right after Hydrogen (Medium); `gemini-4-hydrogen-xhigh` moves
# ahead of `orchestrator-neon` in `strong`. `--timeout` defaults to 1800 s: don't lower it when delegating to `ultra` or
# Orchestrator models (on 2026-11-20 Carbon (High) took 11–26 min).
# A tier is a list of rules (model family, effort) in that measured order — keep it by hand. client.resolve() turns a
# rule into the live model id of what Antigravity offers now (hooks/client.py: a renamed family goes on as its
# successor); a rule without a live model is skipped, an empty tier takes Antigravity's default model.
TIERS: Dict[str, List[Tuple[str, str]]] = {
    "fast": [("gemini-4-hydrogen", "low"), ("gemini-4-hydrogen", "medium"), ("gemini-4-neon", "low"),
             ("claude-sonnet-5-5", "low")],
    "standard": [("gemini-4-hydrogen", "medium"), ("gemini-4-neon", "low"), ("gemini-4-hydrogen", "high"),
                 ("gemini-4-neon", "medium"), ("claude-sonnet-5-5", "medium")],
    "strong": [("gemini-4-neon", "medium"), ("gemini-4-hydrogen", "high"), ("gemini-4-neon", "high"),
               ("gemini-4-hydrogen", "xhigh"), ("orchestrator", "neon"), ("claude-sonnet-5-5", "high"),
               ("claude-opus-5-5", "high")],
    "ultra": [("gemini-4-carbon", "high"), ("orchestrator", "carbon"), ("gemini-4-carbon", "medium"),
              ("gemini-4-neon", "high"), ("claude-opus-5-5", "high")],
    "claude": [("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "high"), ("claude-sonnet-5-5", "medium"),
               ("claude-opus-5-5", "medium"), ("gemini-4-neon", "medium")],
}
# Today's ids for a rule when there is no client snapshot at all (client.py missing): its naming scheme.
LEGACY_IDS = {("orchestrator", "carbon"): "gemini-pro-orchestrator", ("orchestrator", "neon"): "gemini-pro-orchestrator-neon",
              ("orchestrator", "argon"): "gemini-pro-orchestrator-argon"}
TIER_USE = {
    "fast": "light tasks and lookups: a small function with its test, a rename, a lint fix, docs, «where is X used», "
            "summaries of files or logs, babysitting a long command — ~0.5 min, ~0.005% of the weekly Gemini quota",
    "standard": "most work: implementation from a spec, tests for existing code, refactors and fixes across files, "
                "debugging with a repro, read-only reviews and audits — ~1–4 min (~0.005–0.015%) on Hydrogen (Medium) / Neon (Low)",
    "strong": "the hardest parts, or a retry after `standard` got it wrong: subtle bugs, design-heavy changes in "
              "unfamiliar code — ~2–8 min, ~0.02–0.07%",
    "ultra": "complex, specific tasks where every step needs the deepest reasoning: architecture across many modules, "
             "concurrency or protocol bugs, big migrations, niche domains, or a retry after `strong` failed — Gemini 4 "
             "Carbon at its recommended High effort (slow: on 2026-11-24 every Carbon (High) and Orchestrator (Carbon) run hit "
             "that benchmark's 9-minute cap, on 2026-11-20 they took 11–26 min, ~0.1–0.4% of weekly Gemini quota; "
             "don't lower --timeout, it defaults to 1800 s)",
    "claude": "Claude-quality work on Antigravity's separate Claude quota — quick, but ~0.5–3% of that quota's week "
              "per task",
}
# Experimental: the models Antigravity offers that are in no tier — an exact `--model <id>` still takes them. `models`
# and `budget` list each with Antigravity's own tag and description (client.py: «New», «Leaving on …») and, for some
# families, a word of ours on when it may fit (numbers from the 2026-11-20 & 2026-11-24 benchmarks).
FAMILY_NOTES = {
    "gemini-4-neon": "Neon at its deepest effort: a hard debugging task took Neon (XHigh) 7.7 min against Neon "
                     "(Medium)'s 2.6, same result — for a problem Neon (High) left unsure",
    "gemini-4-carbon": "Carbon's knowledge and long-horizon habits: at a low effort for a question in a niche domain "
                       "that needs its knowledge more than its depth",
    "gemini-4-argon": "Argon, the original Gemini 4 pro model (~4× Hydrogen's quota per token)",
    "claude-opus-5-5": "Claude Opus on Antigravity's Claude quota (~2.5% of its week per API dollar): a quick "
                       "Claude-quality review or answer",
    "claude-sonnet-5-5": "Claude Sonnet on Antigravity's Claude quota (~2.5% of its week per API dollar)",
    "gpt-oss": "an open-weight model on the «Claude and GPT models» quota: a second opinion from neither Google nor "
               "Anthropic",
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

RULES = """{mark} This task comes from Claude Code, another AI coding agent that works on this project for the same user, at the same time as you. Claude Code watches this conversation and answers; the user doesn't answer here, but may read it in Antigravity.

Rules for this run:
1. Do exactly the task below. Change only what it covers. No commits, pushes, deploys, global installs or settings changes; don't run /init, /git-init or /handoff and don't save memories.
2. Talk to Claude Code with these two commands — run them yourself with run_command; they only print and return at once:
   - a question you need answered: {ask}
   - something it should know now (a finding that changes the plan, a problem with the task, a file outside the task you need to touch): {tell}
   Its answers and messages arrive in this conversation as messages starting with "Claude Code"; follow them like this task. Ask only when a wrong guess would cost real rework — otherwise choose the most reasonable option and say so in your report. After asking, go on with what doesn't depend on the answer; if nothing is left, end your turn with one line saying what you wait for — the answer wakes you up. Don't poll, sleep or ask the same thing twice. Don't ask for approvals this way, and never send secrets.
3. Keep going with tool calls until the task is done — a message without a tool call ends your run.
4. After your last change, run the checks the task names (or the tests or build of the code you touched) and fix what fails.
5. Stop any server or watcher you started before you finish. When you end your turn to wait for a long command running in the background, say in one line which command you wait for.
6. End with one message that sums the task up, for Claude Code and for the user reading along: plain words in the language the task below is written in, under 200 words, no file contents.
   - Its first line starts with ✅ (done), ⚠️ (partly done) or ⛔ (blocked) and says what you did and how it came out, with the numbers that matter.
   - Then, after an empty line, a short list, one item per line: each file you changed (`path` — what changed), each check you ran (`command` → its result, with the exact numbers), the choices you made, what is left, risks. Leave out what doesn't apply.
   For example:
   ✅ Empty passwords are rejected now; all 14 tests pass.

   - `app/forms.py` — an empty-password check in `LoginForm.clean()`
   - `pytest -q` → 14 passed
   - Kept the old error text: the UI tests look for it.
   Only this last message takes that shape: a line you wait with, or a note beside a tool call, is a plain sentence.

{reference}{where}

Task:
{brief}"""
REFERENCE = "Reference for this kind of task (part of the task, the same in every task like it):\n\n{text}\n\n"
# Sent when Antigravity ended the agent's turn with an error (an API stream that broke off, …): see auto_resume()
RESUME_ERROR = ("Antigravity stopped your last turn with an error{what} — a hiccup on its side, not a problem with the "
                "task. Go on from where you stopped; check what your last steps did before you repeat one. Same rules "
                "as before.")
PIECE = "Piece {n} of the series «{name}»:\n{message}"
FRESH_SERIES = ("This continues the series «{name}»: pieces 1–{done} ran in an earlier conversation, and what they made is "
                "in the project's files. The last piece's report:\n{last}\n\n")

FOLLOW_UP = ("Follow-up from Claude Code: {message}\n\n(Same rules as before: questions go to Claude Code with the ask "
             "command, check your work after the last change, end with the short report: ✅, ⚠️ or ⛔ and what came out, "
             "in plain words, then the list.)")
# The first line of the agent's report (RULES 6) starts with one of these. Reports from before 0.30.1 had a
# «STATUS: done | partial | blocked» line instead (then CHANGED / CHECKS / NOTES): report_head() reads both.
VERDICTS = (("✅", "done"), ("⚠", "partly done"), ("⛔", "blocked"), ("❌", "blocked"), ("🚫", "blocked"))
STATUS_LINE = re.compile(r"^\W*STATUS\W*:\W*([^\n]{1,80})", re.M)
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
    def __init__(self, addr: str, token: str, exe: Optional[str] = "", pid: int = 0):
        self.addr, self.token, self.pid = addr, token, pid
        self._exe = exe  # None: not looked up yet (see executable())

    @property
    def exe(self) -> str:
        """The language server's executable, which also runs agentapi — looked up the first time it is needed (on
        Windows that takes a PowerShell call, which the supervisor would otherwise make every few seconds)."""
        if self._exe is None:
            self._exe = compat.ls_executable(self.pid) if self.pid else ""
        return self._exe


def discover() -> Optional[LS]:
    """Antigravity's running language server: its address, CSRF token and executable (which runs agentapi) — from
    the environment, the server's own ~/.gemini/antigravity/daemon/hub_*.json (Antigravity 2.34 no longer puts the
    token on its command line) or, up to 2.33, its command line: compat.language_server()."""
    found = compat.language_server()
    if not found:
        return None
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "") if found.source == "env" else None
    return LS(found.addr, found.token, exe, found.pid)


def spawn_detached(cmd: List[str], env: Optional[dict] = None, gui: bool = False) -> bool:
    """Start a process that outlives this one (and the terminal command that ran this script): its own session on
    macOS/Linux; on Windows in its own process group and — where the job this runs in allows it — out of that job,
    so a tool runner that kills its job on exit doesn't take it along. A console program (the supervisor) gets a
    hidden console of its own (CREATE_NO_WINDOW), so the git, agentapi and PowerShell it starts flash no window — a
    DETACHED_PROCESS would have none, and each of them would open one; an app (`gui`: Antigravity) is detached.
    True if it started."""
    base = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True)
    if not compat.WINDOWS:
        try:
            subprocess.Popen(cmd, start_new_session=True, **base)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    mode = 0x00000008 if gui else 0x08000000  # DETACHED_PROCESS for an app, CREATE_NO_WINDOW for a console program
    for flags in (mode | 0x00000200 | 0x01000000, mode | 0x00000200):  # … | NEW_PROCESS_GROUP (| BREAKAWAY_FROM_JOB)
        try:
            subprocess.Popen(cmd, creationflags=flags, **base)
            return True
        except (OSError, subprocess.SubprocessError):  # a job without breakaway refuses the first: try without it
            continue
    return False


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
        return bool(exe) and spawn_detached([exe], gui=True)
    except (OSError, subprocess.SubprocessError):
        return False


def connect(timeout: Optional[float] = None) -> LS:
    """The running language server — after starting Antigravity when it isn't running and waiting for it: right after
    a cold start (a reboot, an update) it takes a minute or more to come up and sign in, so up to START_TIMEOUT
    seconds (AGP_DELEGATE_START_TIMEOUT; `wake --timeout`)."""
    ls = discover()
    if ls:
        return ls
    limit = START_TIMEOUT if timeout is None else timeout
    if not os.environ.get("AGP_DELEGATE_NO_LAUNCH") and launch():
        print(f"Antigravity wasn't running — started it in the background; waiting for it (up to {span(limit)}) …",
              flush=True)
        deadline = time.time() + limit
        while time.time() < deadline:
            time.sleep(2)
            ls = discover()
            if ls:
                ready_models(ls, deadline)
                return ls
        raise Failure(f"Antigravity was started but its language server didn't come up within {span(limit)}. It may "
                      "wait for the user (sign-in, an update): ask them to look at it — or run `delegate.py wake "
                      "--timeout 600` and retry, or do the work yourself.")
    raise Failure("Antigravity isn't running and couldn't be started from here. Ask the user to open Antigravity "
                  "(signed in), then retry — or do the work yourself.")


def ready_models(ls: "LS", deadline: float) -> int:
    """Wait until a just-started Antigravity lists its models (it is signed in); how many it lists (0: not yet)."""
    while True:
        try:
            d = rpc(ls, "GetCascadeModelConfigData", {})
            n = len([c for c in d.get("clientModelConfigs") or [] if isinstance(c, dict) and c.get("modelId")])
            if n:
                return n
        except SOFT:
            pass
        if time.time() >= deadline:
            return 0
        time.sleep(2)


VIAS = ("auto", "desktop", "cli")


def choose_via(arg: Optional[str] = None) -> str:
    """Where a new task runs: --via, else AGP_DELEGATE_VIA, else auto — Antigravity Desktop when its language server
    answers, else the Antigravity CLI when `agy` is installed, else Desktop (which connect() then tries to start,
    with its usual error when it can't)."""
    via = (arg or os.environ.get("AGP_DELEGATE_VIA") or "auto").strip().lower()
    if via not in VIAS:
        raise Failure(f"--via is one of {', '.join(VIAS)}, not {via!r}.")
    if via == "cli" and not agy_cli.available():
        raise Failure(agy_cli.NOT_INSTALLED)
    if via == "auto":
        via = "desktop" if discover() else "cli" if agy_cli.available() else "desktop"
    return via


def connect_for(task: Optional[dict]) -> Optional[LS]:
    """The language server a task's commands need — none for a task of the Antigravity CLI. A Desktop task whose
    Antigravity was closed meanwhile: connect() starts it again or says why it can't."""
    return None if agy_cli.is_cli(task) else connect()


def client_snapshot_models() -> Dict[str, dict]:
    """{id: entry} from the client's model snapshot (hooks/client.py), {} without one."""
    try:
        got = client.models() if client else {}
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def client_default_model() -> str:
    try:
        return str(client.default_model() or "") if client else ""
    except Exception:
        return ""


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
    raw = d.get("response") if isinstance(d, dict) and isinstance(d.get("response"), dict) else d
    groups = []
    for g in (raw.get("groups") or [] if isinstance(raw, dict) else []):
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
    out = {"status": status, "week": week, "h5": five, "pace": pace, "lasts": None, "allow": None, "forecast": ""}
    if week is not None and wreset and wreset > now:
        out.update(forecast(week, wreset, now))
    return dict(out, text=", ".join(parts) or "?")


def pct(x: float) -> str:
    """A percentage for a person: 12%, 3.4%, 0.42%, 0.012%."""
    if x >= 9.95:
        return f"{x:.0f}%"
    if x >= 0.995:
        return f"{x:.1f}%"
    return f"{x:.2g}%" if x > 0 else "0%"


def forecast(week: float, reset: float, now: float) -> dict:
    """Does the week's quota last until it resets? From the fraction left and the reset time: how much a day keeps it
    lasting from now (`allow`, % per day), what the week has spent per day since it began (`spent`), and whether it
    lasts at that pace (`lasts`: True / False / None when it's too early to tell) — with the text for a person."""
    days_left = max((reset - now) / 86400, 0.01)
    elapsed = 7 - days_left
    allow = week * 100 / days_left
    used = (1 - week) * 100
    if elapsed < 0.25 or used <= 0.05:
        return {"allow": allow, "spent": None, "lasts": None,
                "forecast": f"{pct(allow)} a day lasts until the reset"}
    spent = used / elapsed
    runs = week * 100 / spent  # days the rest lasts at this week's pace
    if runs >= days_left:
        text = f"spent {pct(spent)} a day so far → at that pace it lasts until the reset ({pct(allow)} a day would)"
    else:
        text = (f"spent {pct(spent)} a day so far → at that pace it runs out ~{span((days_left - runs) * 86400)} "
                f"before the reset; {pct(allow)} a day would last")
    return {"allow": allow, "spent": spent, "lasts": runs >= days_left, "forecast": text}


STATE_TEXT = {"spend": "quota to spare — use it", "ok": "fine", "tight": "tight — save it",
              "exhausted": "unavailable"}


def verdict(s: dict) -> str:
    """The word on a group's state that agrees with its forecast: «fine» only while the quota lasts until the
    reset at this week's pace."""
    if s["status"] == "ok" and s.get("lasts") is False and s.get("allow") is not None:
        return f"fine for now — keep to about {pct(s['allow'])} a day"
    return STATE_TEXT[s["status"]]


_CLIENT_FRESH = [False]


def fresh_client() -> None:
    """Refresh the client's model snapshot when it is stale (once per run; the bridge isn't a hook, it may ask the
    language server). Nothing happens without client.py, and a failed refresh keeps the old snapshot."""
    if _CLIENT_FRESH[0] or client is None:
        return
    _CLIENT_FRESH[0] = True
    try:
        if client.stale():
            client.refresh(timeout=6.0)
    except Exception:
        pass


def client_line() -> str:
    """Where the model data comes from: the client's version and how fresh its snapshot is."""
    if client is None:
        return "Model data: client.py isn't installed next to this script — tiers use today's id scheme."
    try:
        snap = client.snapshot() or {}
        at = float(snap.get("at") or 0)
        age = f", taken {span(time.time() - at)} ago" if at else ""
        src = {"desktop": "from Antigravity Desktop", "bundled": "the snapshot shipped with the plugin"}.get(
            str(snap.get("source") or ""), str(snap.get("source") or "?"))
        stale = " (stale)" if client.stale(snap) else ""
        return f"Model data: Antigravity {client.version() or '?'}, {src}{age}{stale}."
    except Exception as e:
        return f"Model data: client.py failed ({e!r}) — tiers use today's id scheme."


def rule_id(family: str, effort: str) -> str:
    """The live model id of a tier rule; "" when Antigravity has no such model."""
    if client is not None:
        try:
            got = str(client.resolve(family, effort) or "")
            if got:
                return got
            if client.snapshot():
                return ""
        except Exception:
            pass
    return LEGACY_IDS.get((family, effort), f"{family}-{effort}")


def tier_ids(tier: str, ms: List[dict], default: str = "") -> List[str]:
    """A tier's live model ids, best first: its rules resolved and found among the models offered now; an empty
    tier takes Antigravity's default model."""
    live = {m["id"] for m in ms}
    out: List[str] = []
    for family_, effort in TIERS.get(tier, []):
        mid = rule_id(family_, effort)
        if mid in live and mid not in out:
            out.append(mid)
        elif mid not in live:  # an id the snapshot knows, not offered here: today's scheme as a second look
            alt = LEGACY_IDS.get((family_, effort), f"{family_}-{effort}")
            if alt in live and alt not in out:
                out.append(alt)
    if not out:
        dflt = client_default_model() or default
        out = [dflt] if dflt in live else ([default] if default in live else [])
    return out


def family_of(mid: str) -> str:
    entry = client_snapshot_models().get(mid) or {}
    if entry.get("family"):
        return str(entry["family"])
    return re.sub(r"-(?:low|medium|high|xhigh|max)$", "", mid)


def experimental(ms: List[dict]) -> List[Tuple[dict, str]]:
    """The models Antigravity offers right now that are in no tier, each with Antigravity's own tag and description
    and our word on its family, when there is one."""
    tiered = {i for t in TIERS for i in tier_ids(t, ms)}
    snap = client_snapshot_models()
    out = []
    for m in ms:
        if m["id"] in tiered:
            continue
        entry = snap.get(m["id"]) or {}
        tag, said = str(m.get("tag") or entry.get("tag") or ""), str(m.get("note") or entry.get("note") or "")
        ours = next((v for k, v in FAMILY_NOTES.items() if family_of(m["id"]).startswith(k)), "")
        parts = [x for x in ((f"«{tag}» " if tag else "") + said, ours) if x.strip()]
        out.append((m, " — ".join(p.strip() for p in parts) or "no description yet — try it on a small task first"))
    return out


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
    aliases = {}
    try:
        aliases = dict(client.aliases() or {}) if client else {}
    except Exception:
        aliases = {}
    if a in ("flash_lite", "flash", "pro") or a in aliases:
        to = f" → {aliases[a]}" if aliases.get(a) else ""
        return ({"id": a, "label": f"{a} (Antigravity's alias{to})", "left": None, "reset": "", "enum": "", "tag": "",
                 "note": ""}, notes, "Antigravity's own alias, as its subagents use it")
    stand_in = False
    if a in TIERS:
        listed = [by_id[c.lower()] for c in tier_ids(a, ms, default) if c.lower() in by_id]
        if not listed:
            stand_in, listed = True, ms[:1]
    else:
        m = by_id.get(a) or next((x for x in ms if x["label"].lower() == a), None) or fuzzy(a, ms)
        if not m:
            near = difflib.get_close_matches(a, list(by_id), n=3, cutoff=0.5)
            raise Failure(f"unknown model {arg!r} — Antigravity doesn't offer it now"
                          + (f"; the closest: {', '.join(near)}" if near else "")
                          + f". Use a tier ({', '.join(TIERS)}) or one of: " + ", ".join(x["id"] for x in ms))
        if ok(m):
            return m, notes, ("your choice" + unless_fine(m) + (" — a lower tier would save it"
                                                                if state(m)["status"] == "tight" else ""))
        tier = next((t for t in TIERS if m["id"] in tier_ids(t, ms)), "")
        listed = [m] + [by_id[c.lower()] for c in (tier_ids(tier, ms) if tier else []) if c.lower() in by_id
                        and by_id[c.lower()] is not m]
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
        why = (f"none of {a}'s models is offered here — took {m['label']}, the first one listed" if stand_in
               else f"first choice of {a}" if a in TIERS else "your choice") + unless_fine(m)
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


def rates_of(label: str, table: List[dict], day: str = "") -> Optional[dict]:
    """The pricing.json entry for a model label, with its price changes dated on or before `day` (YYYY-MM-DD, default
    today, local time — as the usage panel prices calls); None when it isn't priced."""
    day = day or time.strftime("%Y-%m-%d")
    for m in table:
        try:
            if re.search(m["match"], label, re.I):
                if m.get("unpriced") or "input" not in m or "output" not in m:
                    return None
                rate = dict(m)
                for ch in sorted((c for c in m.get("price_changes") or [] if isinstance(c, dict)),
                                 key=lambda c: str(c.get("from"))):
                    if str(ch.get("from") or "9999") <= day:
                        rate.update(ch)
                return rate
        except (re.error, KeyError, TypeError, ValueError):
            continue
    return None


def price(label: str, table: List[dict], day: str = "") -> str:
    """«$2/$10 per 1M» (input/output) from pricing.json."""
    rate = rates_of(label, table, day)
    try:
        return f"${rate['input']:g}/${rate['output']:g} per 1M" if rate else ""
    except (KeyError, TypeError, ValueError):
        return ""


def call_cost(label: str, fresh: int, cached: int, out: int, table: List[dict]) -> Optional[float]:
    """One model call at API prices, in dollars, as usage.py prices it (Claude bills uncached input as a cache write);
    None when the model isn't priced."""
    rate = rates_of(label, table)
    if not rate:
        return None
    try:
        in_rate = float(rate.get("cache_write", rate["input"]) if rate.get("uncached_input_as_cache_write")
                        else rate["input"])
        return (fresh * in_rate + cached * float(rate.get("cache_read", rate["input"])) + out * float(rate["output"])) / 1e6
    except (KeyError, TypeError, ValueError):
        return None


def window_of(label: str, table: List[dict]) -> int:
    """The context window Antigravity gives this model: the client snapshot's window, else pricing.json's
    `context_window`, 512k when neither knows."""
    try:
        got = client.window(label) if client else None
        if isinstance(got, int) and got > 0:
            return got
    except Exception:
        pass
    for m in table:
        try:
            if re.search(m["match"], label, re.I) and isinstance(m.get("context_window"), int):
                return int(m["context_window"])
        except (re.error, KeyError, TypeError):
            continue
    return 512000


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


def load(task_id: str) -> Optional[dict]:
    """A task's record as it is on disk now, None when there is none."""
    try:
        with open(os.path.join(TASKS, task_id + ".json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("id") == task_id else None


def transact(task: dict, change) -> bool:
    """Re-read a task's record under the lock and let `change(record)` edit it; True from it saves the record. A
    check-and-set: of two processes that see the same event (the supervisor and a watch, two watches) only one acts
    on it. `task` gets the record's current fields either way. Returns what `change` returned."""
    os.makedirs(TASKS, exist_ok=True)
    with open(os.path.join(TASKS, ".lock"), "a+", encoding="utf-8") as fh:
        compat.lock(fh)
        try:
            rec = load(task["id"]) or dict(task)
            done = bool(change(rec))
            if done:
                save(rec)
        finally:
            compat.unlock(fh)
    task.clear()
    task.update(rec)
    return done


def claim_events(task: dict, keys: List[str], shown: List[str]) -> Tuple[set, int]:
    """Mark events as told to Claude Code, under the lock: returns the keys nobody had told yet, and the number the
    first newly told one of `shown` gets (each task numbers the events it printed 1, 2, 3 … across all watches)."""
    new: set = set()
    first = [0]

    def change(rec: dict) -> bool:
        told = set(rec.get("told") or [])
        new.update(k for k in keys if k not in told)
        if not new:
            return False
        first[0] = int(rec.get("events") or 0) + 1
        rec["told"] = sorted(told | new)
        rec["events"] = int(rec.get("events") or 0) + len([k for k in shown if k in new])
        return True

    transact(task, change)
    return new, first[0]


def bridge_settings() -> dict:
    """The bridge's own defaults (`delegate.py settings`): max_parallel (tasks running at once; 0 = no limit) and
    retries (resumes per task after Antigravity stops it with an error). AGP_DELEGATE_MAX_PARALLEL and
    AGP_DELEGATE_RETRIES override them."""
    s = dict(BRIDGE_DEFAULTS)
    try:
        with open(BRIDGE_SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
        for key in BRIDGE_DEFAULTS:
            v = d.get(key) if isinstance(d, dict) else None
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 50:
                s[key] = v
    except (OSError, ValueError):
        pass
    for key, var in (("max_parallel", "AGP_DELEGATE_MAX_PARALLEL"), ("retries", "AGP_DELEGATE_RETRIES")):
        v = (os.environ.get(var) or "").strip()
        if v.isdigit():
            s[key] = min(int(v), 50)
    return s


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
    out.sort(key=lambda d: float(d.get("started") or d.get("queued") or 0), reverse=True)  # a queued one: when queued
    return out


def prune() -> None:
    for d in records()[KEEP_TASKS:]:
        try:
            os.remove(os.path.join(TASKS, d["id"] + ".json"))
        except OSError:
            pass


def task_for(ident: str, follow: bool = True) -> dict:
    """The task record an id (or a unique prefix of one) names. A queued task («q-…») that has started meanwhile
    stands for the task it became (unless `follow` is False)."""
    ident = (ident or "").strip()
    if not re.fullmatch(r"[\w-]{4,80}", ident):
        raise Failure(f"not a task id: {ident!r}")
    hits = [d for d in records() if d["id"].startswith(ident)]
    if len(hits) > 1:
        raise Failure(f"{ident!r} matches {len(hits)} tasks — give more of the id.")
    if hits:
        if follow and hits[0].get("state") == "started" and hits[0].get("started_as"):
            return load(str(hits[0]["started_as"])) or hits[0]
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


def compose(brief: str, folder: str, root: str, reference: str = "") -> str:
    """The first message of a task: the run rules, the reference texts (--template), the folder, the task — the
    parts that change from task to task last."""
    where = f"Working folder: {folder}" + ("" if os.path.normcase(folder) == os.path.normcase(root)
                                           else f" (inside the workspace {root})")
    ref = REFERENCE.format(text=reference.strip()) if reference.strip() else ""
    return RULES.format(mark=MARK, where=where, brief=brief.strip(), ask=talk_command("ask"),
                        tell=talk_command("tell"), reference=ref)


def read_templates(paths: List[str]) -> str:
    """The text of --template files, each under a heading with its name."""
    parts = []
    for p in paths or []:
        try:
            with open(p, encoding="utf-8-sig") as f:
                text = f.read().strip()
        except UnicodeDecodeError:
            raise Failure(f"the template {p} isn't UTF-8 text — save it as UTF-8.")
        except OSError as e:
            raise Failure(f"can't read the template {p}: {e}")
        if text:
            parts.append(f"## {os.path.basename(p)}\n{text}")
    return "\n\n".join(parts)


def title_of(brief: str, given: str) -> str:
    t = (given or "").strip() or next((ln.strip(" #*->") for ln in brief.splitlines() if ln.strip(" #*->")), "task")
    t = re.sub(r"\s+", " ", t)
    return "Claude Code: " + (t[:60].rstrip() + "…" if len(t) > 60 else t)


# ─────────────────────────────── errors: resuming a stopped turn ───────────────────────────────

# An error a «go on» won't fix: the plan's quota, permissions, a model Antigravity doesn't offer, a prompt too long.
PERMANENT_ERROR = re.compile(r"(?i)quota|resource[ _]?exhausted|exceeded your|billing|credits\b|permission denied|"
                             r"unauthori[sz]ed|forbidden|not (?:found|supported)|invalid (?:argument|model|request)|"
                             r"unsupported|context (?:length|window)|too many tokens|(?:prompt|input) is too long")
SLOW_ERROR = re.compile(r"(?i)\b429\b|rate.?limit|too many requests|overloaded|capacity|try again later")


def error_text(st: dict) -> str:
    """A step's error message on one line: an ERROR_MESSAGE step's, or a failed step's own."""
    em = st.get("errorMessage") if isinstance(st.get("errorMessage"), dict) else {}
    err = em.get("error") if isinstance(em.get("error"), dict) else st.get("error")
    err = err if isinstance(err, dict) else {}
    text = err.get("shortError") or err.get("fullError") or err.get("message") or em.get("message") or ""
    return re.sub(r"\s+", " ", str(text)).strip()


def turn_error(steps: List[dict], base: int = 0) -> Tuple[str, int]:
    """What ended a turn Antigravity stopped with an error: the newest error of a model call in `steps` (numbered
    from `base`) — an ERROR_MESSAGE step («API error (attempt 1): stream reading error: unexpected EOF») or a model
    response that broke off —, else a failed step among the last three. ("", -1) when there is none."""
    model, other = ("", -1), ("", -1)
    last = base + len(steps) - 1
    for i, st in enumerate(steps, base):
        kind, failed = str(st.get("type") or ""), st.get("status") == "CORTEX_STEP_STATUS_ERROR"
        if kind == "CORTEX_STEP_TYPE_ERROR_MESSAGE" or (failed and kind == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"):
            model = (error_text(st) or model[0], i)
        elif failed and i >= last - 2:
            other = (error_text(st), i)
    return model if model[1] >= 0 else other


def stopped_by_error(s: dict) -> bool:
    return (str(s.get("status") or "") == "CASCADE_RUN_STATUS_IDLE"
            and str(s.get("latestExecutorTerminationReason") or "") == "EXECUTOR_TERMINATION_REASON_ERROR")


def turn_resumes(task: dict) -> List[dict]:
    """The resumes after errors in the task's current turn: --retries counts per turn (a follow-up or the next piece
    of a series starts with all of them again)."""
    turn = int(task.get("turn") or 0)
    return [r for r in task.get("resumes") or [] if int(r.get("count") or 0) > turn]


def resume_state(task: dict, s: dict) -> str:
    """For a task whose conversation is idle: "due" when Antigravity ended its turn with an error and it has
    resumes left (run --retries), "wait" when the same error is younger than RETRY_DELAY seconds, "" otherwise (it
    ended another way, it was canceled, no resumes left or none asked for, this error was resumed or judged not
    worth it already, or the stop is hours old — then it's the user's call)."""
    if not task.get("retries_max") or task.get("state") == "canceled" or not stopped_by_error(s):
        return ""
    count = int(s.get("stepCount") or 0)
    done = turn_resumes(task)
    if any(int(r.get("count", -1)) == count for r in task.get("resumes") or []) \
            or int(task.get("no_resume_at", -1)) == count:
        return ""
    if len(done) >= int(task.get("retries_max") or 0):
        return ""
    when = parse_ts(str(s.get("lastModifiedTime") or "")) or 0.0
    if when and time.time() - when > 6 * 3600:
        return ""
    return "wait" if when and time.time() - when < RETRY_DELAY else "due"


def auto_resume(ls: LS, task: dict, s: Optional[dict] = None) -> Tuple[str, Optional[dict]]:
    """Resume a task whose turn Antigravity ended with an error — an API stream that broke off, even on the first
    model call — with a short «go on» message, up to its `retries_max` (run --retries). Once per error, by whoever
    notices first (the supervisor, a wait, a watch, a result): the claim in the record keeps the others from sending
    it too. Returns ("resumed", the entry — None when another process sent it), ("wait", None) while the error is
    fresh or the send failed (it is tried again), or ("", None): nothing to resume, or an error a resume won't fix
    (quota, permissions, a model Antigravity doesn't offer — kept in the record, so the report says so)."""
    if agy_cli.is_cli(task):
        return "", None  # agy_cli.resume() does it between turns
    s = summary(ls, task["id"]) if s is None else s
    state = resume_state(task, s)
    if state != "due":
        return state, None
    count = int(s.get("stepCount") or 0)
    base = max(int(task.get("turn") or 0), count - 40)
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": task["id"], "stepOffset": base}, timeout=60).get("steps")
    except SOFT:
        return "wait", None
    steps = [x for x in steps or [] if isinstance(x, dict)]
    err, at = turn_error(steps, base)
    culprit = steps[at - base] if 0 <= at - base < len(steps) else {}
    from_model = str(culprit.get("type") or "") in ("CORTEX_STEP_TYPE_ERROR_MESSAGE", "CORTEX_STEP_TYPE_PLANNER_RESPONSE")
    if from_model and PERMANENT_ERROR.search(err):  # a tool's own error («target content not found») decides nothing
        update(task, no_resume_at=count, last_error=err[:300])
        return "", None
    when = parse_ts(str(s.get("lastModifiedTime") or "")) or 0.0
    if SLOW_ERROR.search(err) and when and time.time() - when < 4 * RETRY_DELAY:
        return "wait", None  # a rate limit: give it a little longer
    entry = {"at": time.time(), "count": count, "step": at, "error": err[:300]}
    taken = [False]  # another process resumed this error already

    def claim(rec: dict) -> bool:
        done = rec.get("resumes") or []
        if any(int(r.get("count", -1)) == count for r in done):
            taken[0] = True
            return False
        if rec.get("state") == "canceled" or len(turn_resumes(rec)) >= int(rec.get("retries_max") or 0):
            return False  # stopped by the user meanwhile, or no resumes left in this turn
        entry["n"] = len(turn_resumes(rec)) + 1
        rec.update(resumes=done + [entry], state="running", since=count, last_error=err[:300])
        return True

    if not transact(task, claim):
        return ("resumed", None) if taken[0] else ("", None)
    text = MESSAGE.format(message=RESUME_ERROR.format(what=f" («{err[:200]}»)" if err else ""))
    project = task.get("project") or ((find_project(task.get("cwd") or os.getcwd()) or ({"id": ""}, ""))[0]["id"])
    try:
        agentapi(ls, ["send-message", "--title=Claude Code", task["id"], text], project)
    except Failure:
        def undo(rec: dict) -> bool:  # not sent: the next look tries again
            rec["resumes"] = [r for r in rec.get("resumes") or [] if int(r.get("count", -1)) != count]
            return True
        transact(task, undo)
        return "wait", None
    return "resumed", entry


def resume_line(task: dict, r: dict) -> str:
    """A resume after an error, as a watch event and a report line."""
    err = f" («{r['error'][:160]}»)" if r.get("error") else ""
    return (f"🔁 Antigravity task {task['id'][:8]}: Antigravity stopped its turn with an error{err} — resumed it "
            f"automatically ({r.get('n', '?')} of {task.get('retries_max', '?')}); it goes on with its context")


# ─────────────────────────────── waiting ───────────────────────────────

def summary(ls: LS, cid: str) -> dict:
    s = listed(ls, cid)
    return s or {}


def listed(ls: LS, cid: str) -> Optional[dict]:
    """A conversation's summary; {} when Antigravity no longer lists it (deleted), None when it didn't answer in time
    — a slow answer is no reason to call a task finished."""
    try:
        d = rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30)
    except TIMEOUTS:
        return None
    s = (d.get("trajectorySummaries") or {}).get(cid)
    return s if isinstance(s, dict) else {}


GONE_AFTER = 120.0  # seconds: a conversation missing from Antigravity's list this long after its start is gone


def gone(task: dict, s: Optional[dict]) -> bool:
    """Antigravity answered, and no longer has this task's conversation (the user deleted it)."""
    return s == {} and bool(task.get("started")) and time.time() - float(task["started"]) > GONE_AFTER


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
    if agy_cli.is_cli(task):
        return agy_cli.wait(task, timeout)
    cid = task["id"]
    began = time.time()
    deadline = began + max(timeout, 1.0)
    offset, idle_since = max(int(task.get("turn") or 0), int(task.get("since") or 0)), None
    while True:
        since = max(int(task.get("turn") or 0), int(task.get("since") or 0))  # it must answer after this step
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
            s = listed(ls, cid)
            if s is None:  # its summary didn't come in time: no reason to call it finished — look again
                time.sleep(min(POLL, 5.0))
                continue
            if gone(task, s):
                return "done", {"gone": True}
            what, _ = auto_resume(ls, task, s)  # stopped by an error: resumed now, or after a short pause
            if what:
                time.sleep(min(POLL, 5.0))
                continue
            since = max(int(task.get("turn") or 0), int(task.get("since") or 0))
            if answered(ls, cid, since) or time.time() - max(began, last_sent(task)) > 30:
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
               + [float(m.get("at") or 0) for m in task.get("chat") or [] if m.get("from") == "claude"]
               + [float(r.get("at") or 0) for r in task.get("resumes") or []])


def idle_state(ls: LS, task: dict, s: Optional[dict] = None) -> str:
    """A task whose conversation is IDLE, without waiting: "running" while it is about to be resumed after an error
    (`s`: its summary, when the caller has it), "background" while something it started still runs, "running"
    while a message from Claude Code hasn't been picked up yet (right after a start, a follow-up or a reply it can
    look idle for a moment — `wait` gives it 30 s too), else "done"."""
    if s is not None and resume_state(task, s):
        return "running"
    if background_runs(ls, task["id"]):
        return "background"
    since = max(int(task.get("turn") or 0), int(task.get("since") or 0))
    if time.time() - last_sent(task) < 30 and not answered(ls, task["id"], since):
        return "running"
    return "done"


def state_now(ls: LS, task: dict) -> Tuple[str, dict]:
    """Without waiting: "question", "running", "approval", "background" (it ended its turn, but something it
    started still runs) or "done"."""
    if agy_cli.is_cli(task):
        return agy_cli.state_now(task)
    found = listed(ls, task["id"])
    if gone(task, found):
        return "done", {"gone": True}
    s = found or {}
    status = s.get("status")
    running = status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY")
    listen(ls, task, start=None if running else int(task.get("turn") or 0))
    asked = open_questions(task)
    if asked:
        return "question", {"questions": asked}
    if running:
        ask, _ = pending(ls, task["id"])
        return ("approval", ask) if ask else ("running", {})
    if found is None:
        return "running", {}  # Antigravity didn't answer in time: it can't be called finished from that
    if status == "CASCADE_RUN_STATUS_IDLE" and auto_resume(ls, task, s)[0]:
        return "running", {}  # stopped by an error: resumed now, or in a moment
    return (idle_state(ls, task, s) if status == "CASCADE_RUN_STATUS_IDLE" else "done"), {}


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
    if agy_cli.is_cli(task):
        return []  # agy_cli.state_now() reads the talk from the turn's events
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
    if agy_cli.is_cli(task):
        asked = bool(open_questions(task)) and kind != "follow-up"
        how = agy_cli.deliver(task, message, kind)
        return {"status": "CASCADE_RUN_STATUS_IDLE" if how == "sent" else "CASCADE_RUN_STATUS_RUNNING",
                "via": "cli", "how": how}, asked
    s = summary(ls, task["id"])
    if not s:
        raise Failure(f"Antigravity doesn't know conversation {task['id']}.")
    listen(ls, task)
    asked = open_questions(task)
    message = message.strip()
    limit = BRIEF_LIMIT if kind == "follow-up" else TALK_LIMIT  # more work (send, next) may be as long as a brief
    if len(message) > limit:
        raise Failure(f"the message has {len(message)} characters, more than the {limit} one "
                      + ("follow-up" if kind == "follow-up" else "reply") + " takes — nothing was sent. Shorten it, "
                      "or put the details in a file in the project and point to it.")
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
    if cid + ".json" in names:
        return False
    return not any(r.get("cid") == cid for r in records())  # a CLI task: its record is named by its own id


# ─────────────────────────────── the report ───────────────────────────────

def parse_ts(ts: str) -> Optional[float]:
    m = re.match(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})", ts or "")
    if not m:
        return None
    try:
        return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))
    except (ValueError, OverflowError):  # a month 13
        return None


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
    if agy_cli.is_cli(task):
        return agy_cli.changed_files(task)[0]
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


def usage(ls: LS, cid: str, turn: int, table: Optional[List[dict]] = None) -> dict:
    """Model calls and tokens since step `turn` (proto3 JSON: int64 counts come as strings), what they cost at API
    prices (`usd`; None when a model isn't priced), the turn's first call (`first`: its fresh and cached input — a
    new conversation reads its whole prompt uncached) and the conversation's size at its newest call (`context`)."""
    try:
        d = rpc(ls, "GetCascadeTrajectoryGeneratorMetadata", {"cascadeId": cid}, timeout=60)
    except SOFT:
        return {}
    table = prices() if table is None else table
    tot = {"calls": 0, "input": 0, "cached": 0, "output": 0, "models": [], "by_label": {}, "usd": 0.0,
           "first": None, "context": 0}
    lowest, newest = None, -1
    for g in d.get("generatorMetadata") or []:
        if not isinstance(g, dict):
            continue
        idx = [int(i) for i in g.get("stepIndices") or [] if str(i).isdigit()]
        if idx and max(idx) < turn:
            continue
        cm = g.get("chatModel") or {}
        u = cm.get("usage") or {}
        fresh, cached, out = _count(u, "inputTokens"), _count(u, "cacheReadTokens"), _count(u, "outputTokens")
        tot["calls"] += 1
        tot["input"] += fresh
        tot["cached"] += cached
        tot["output"] += out
        name = str(cm.get("responseModel") or "")
        if name and name not in tot["models"]:
            tot["models"].append(name)
        label = str(cm.get("modelDisplayName") or name)  # Orchestrator: configurable Planner (Carbon/Neon/Opus) + Executor (Hydrogen/Sonnet)
        if label:
            tot["by_label"][label] = tot["by_label"].get(label, 0) + 1
        cost = call_cost(label, fresh, cached, out, table)
        tot["usd"] = None if cost is None or tot["usd"] is None else tot["usd"] + cost
        lo, hi = (min(idx), max(idx)) if idx else (0, 0)
        if lowest is None or lo < lowest:
            lowest, tot["first"] = lo, {"input": fresh, "cached": cached}
        if hi >= newest:
            newest, tot["context"] = hi, fresh + cached + out
    return tot


def background_commands(steps: List[dict], base: int = 0) -> List[dict]:
    """The agent's commands that went to the background (Antigravity's `taskDetails`) in `steps` (numbered from
    `base`): task id, title, command line, when it moved to the background, whether it still runs, its exit code,
    its last line of output and its log file."""
    finished: Dict[str, str] = {}  # task id → the notification Antigravity sent when it was over
    for st in steps:
        if st.get("type") == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE":
            msg = (st.get("systemMessage") or {}).get("agentMessage") or {}
            m = re.match(r'Task id "([^"]+)" finished with result', str(msg.get("content") or "")) \
                if isinstance(msg, dict) else None
            if m:
                finished[m.group(1)] = str(msg.get("content") or "")
    out = []
    for i, st in enumerate(steps, base):
        td = st.get("taskDetails")
        if st.get("type") != "CORTEX_STEP_TYPE_RUN_COMMAND" or not isinstance(td, dict) or not td.get("movedToBackgroundAt"):
            continue
        rc = st.get("runCommand") or {}
        tid = str(td.get("id") or "")
        done = finished.get(tid, "")
        code = rc.get("exitCode")
        if not isinstance(code, int) and done:
            m = re.search(r"exited with code (-?\d+)", done)
            code = int(m.group(1)) if m else None
        lines = [ln.strip() for ln in str(td.get("progress") or "").splitlines()
                 if ln.strip() and not ln.strip().startswith("<truncated")]
        out.append({"step": i, "task": tid.rsplit("/", 1)[-1] or f"task-{i}", "title": str(td.get("title") or ""),
                    "line": re.sub(r"\s+", " ", str(rc.get("commandLine") or td.get("description") or "")).strip(),
                    "since": parse_ts(str(td.get("movedToBackgroundAt") or "")),
                    "running": str(st.get("status") or "") in UNFINISHED and not done,
                    "code": code if isinstance(code, int) else None, "last": lines[-1][:160] if lines else "",
                    "log": uri_to_path(str(td.get("logUri") or ""))})
    return out


def command_pids(lines: List[str], ls_pid: int = 0) -> Dict[str, List["compat.Proc"]]:
    """The processes that run these command lines now, under Antigravity's language server (`ls_pid`, else every
    process named language_server*; AGP_DELEGATE_LS_PID for tests): for each line, the process running it (on
    Windows the PowerShell Antigravity wraps a command in) and what that started. Found by the command line's text —
    for the user to look at or stop, not proof."""
    if not lines:
        return {}
    table = compat.process_table(cmdlines=True)
    given = int(os.environ.get("AGP_DELEGATE_LS_PID") or 0) or ls_pid
    servers = [given] if given in table else [p.pid for p in table.values() if p.name.startswith("language_server")]
    under = {pid for root in servers for pid in compat.descendants(table, root)}

    def norm(text: str) -> str:
        return re.sub(r"[\s\"'`\\^]+", " ", (text or "").lower()).strip()

    out: Dict[str, List[compat.Proc]] = {}
    for line in lines:
        key = norm(line)[:120]
        if len(key) < 4:
            continue
        hits = {pid for pid in under if key in norm(table[pid].cmdline)}
        rest = key.partition(" ")[2]  # its words after the program: a program that runs under another path than
        if not hits and len(rest) >= 8:  # the one written (macOS's framework Python, a resolved shim) has them too
            hits = {pid for pid in under if rest in norm(table[pid].cmdline)}
        procs: List[compat.Proc] = []
        for top in sorted(pid for pid in hits if table[pid].ppid not in hits)[:2]:  # the outermost of each
            procs += [table[top]] + [table[c] for c in compat.descendants(table, top) if c in table]
        if procs:
            out[line] = procs[:8]
    return out


def viewed_images(steps: List[dict], base: int = 0) -> Tuple[List[Tuple[str, int, int]], List[Tuple[str, str]]]:
    """The images the agent opened with its file viewer in `steps` (numbered from `base`): (path, bytes, times) for
    each it saw — the model got the picture —, and (path, why) for each it couldn't open."""
    seen: Dict[str, List[int]] = {}
    missed: List[Tuple[str, str]] = []
    for st in steps:
        if st.get("type") != "CORTEX_STEP_TYPE_VIEW_FILE":
            continue
        vf = st.get("viewFile") or {}
        path = uri_to_path(str(vf.get("absolutePathUri") or ""))
        media = vf.get("mediaData") if isinstance(vf.get("mediaData"), dict) else {}
        if not path or not (path.lower().endswith(IMAGE_EXT) or str(media.get("mimeType") or "").startswith("image/")):
            continue
        size = str(vf.get("numBytes") or "0")
        if st.get("status") == "CORTEX_STEP_STATUS_DONE" and (media or size not in ("", "0")):
            rec = seen.setdefault(path, [int(size) if size.isdigit() else 0, 0])
            rec[1] += 1
        else:
            why = error_text(st) or str(st.get("status") or "").replace("CORTEX_STEP_STATUS_", "").lower() or "?"
            missed.append((path, why))
    return [(p, v[0], v[1]) for p, v in seen.items()], missed


def size_text(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(n, 1) / 1024:.0f} KB"


def split_text(by_label: Dict[str, int]) -> str:
    """« (3 on Orchestrator (Carbon), 11 on Gemini 4 Hydrogen (Orchestrator))» when more than one model made the
    calls — Orchestrator's planner and its executor (Hydrogen or Claude Sonnet 5.5); "" otherwise."""
    if len(by_label) < 2:
        return ""
    return " (" + ", ".join(f"{n} on {label}" for label, n in by_label.items()) + ")"


def render(ls: LS, task: dict, state: str, info: dict, full: bool = False) -> Tuple[str, int, dict]:
    """The report of a task in `state`, its exit code, and what the turn measured (calls, tokens, API dollars,
    seconds, the weekly quota before and after) for `estimate`."""
    if agy_cli.is_cli(task):
        return agy_cli.render(task, state, info, full)
    cid, short = task["id"], task["id"][:8]
    if info.get("gone"):
        return (f"❌ Antigravity task {cid} is gone: Antigravity no longer lists its conversation (deleted in the "
                f"app?), so nothing more comes from it.\nWhat it changed is in the working tree — git status shows it; "
                f"delegate.py diff {short} may no longer know.", 2, {"ended": "gone"})
    s = summary(ls, cid)
    turn = int(task.get("turn") or 0)
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid}, timeout=60).get("steps") or []
    except TIMEOUTS:
        steps = []
    steps = [x for x in steps if isinstance(x, dict)]
    final = assemble_report(turn_messages(steps, turn))
    commands = []
    for i, st in enumerate(steps):
        if i < turn:
            continue
        if st.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
            rc = st.get("runCommand") or {}
            line = re.sub(r"\s+", " ", str(rc.get("commandLine") or rc.get("proposedCommandLine") or "")).strip()
            still = isinstance(st.get("taskDetails"), dict) and str(st.get("status") or "") in UNFINISHED
            if line and not TALK.search(line) and not still:  # ask / tell show up as the talk; a running one below
                commands.append({"line": line, "code": rc.get("exitCode"), "status": st.get("status", ""),
                                 "out": outcome(output_text(rc))})
    ended = str(s.get("latestExecutorTerminationReason") or "")
    failed = FAILED_ENDS.get(ended) if state == "done" else None
    err = turn_error(steps[turn:], turn)[0] if failed and ended.endswith("_ERROR") else ""
    start = task.get("sent") or parse_ts(str(s.get("createdTime") or "")) or task.get("started") or 0
    stop = parse_ts(str(s.get("lastModifiedTime") or "")) if state != "timeout" else time.time()
    took = f" in {span((stop or time.time()) - start)}" if start else ""
    head = {"done": (f"❌ Antigravity task {cid} {failed}{took}" + (f": «{err[:240]}»" if err else ""))
                    if failed else f"✅ Antigravity finished task {cid}{took}",
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
        return "\n".join(lines), 4, {}

    table = prices()
    use = usage(ls, cid, turn, table)
    model = task.get("label") or ", ".join(use.get("models") or []) or "?"
    ran = [x for x in (use.get("by_label") or {}) if x]
    asked_for = str(task.get("label") or "")
    mismatch = (ran and asked_for and asked_for not in ran and "orchestrator" not in asked_for.lower()
                and "alias" not in asked_for)
    cost = f"Model: {model}"
    if use.get("calls"):
        cost += (f" · {use['calls']} model calls{split_text(use.get('by_label') or {})}"
                 f" · {kilo(use['input'] + use['cached'])} tokens read"
                 + (f" ({kilo(use['cached'])} from cache)" if use["cached"] else "") + f" · {kilo(use['output'])} written")
        if use.get("usd"):
            cost += f" · ≈${use['usd']:.2f} at API prices"
        first = use.get("first") or {}
        if turn == 0 and first.get("input", 0) + first.get("cached", 0) > 0:
            whole = first["input"] + first["cached"]
            cost += (f" · first call {kilo(whole)}, "
                     + (f"{first['cached'] * 100 // whole}% from cache" if first["cached"] else "none from cache"))
    before = task.get("quota_before")
    after = None
    if isinstance(before, (int, float)) and task.get("label"):
        groups = quota(ls)
        after = weekly_left(task["label"], groups)
        if after is not None:
            grp = (group_of(task["label"], groups) or {}).get("name", "")
            cost += f" · weekly «{grp}» limit {before * 100:.1f}% → {after * 100:.1f}% left"
    lines.append(cost)
    if mismatch:
        lines.append(f"⚠ Antigravity ran its calls on {', '.join(ran)}, not on the {asked_for} it was started with — "
                     "it may have fallen back to another model (the costs above are by the model of each call).")
    title = str(s.get("summary") or task.get("title") or "")
    if title or task.get("project_name"):
        lines.append(f"In Antigravity: «{title}»" + (f" · project «{task['project_name']}»" if task.get("project_name") else ""))
    if task.get("created_project"):
        lines.append(f"(This first task here created the Antigravity project «{task.get('project_name')}» for {task.get('root')}.)")
    if task.get("series"):
        pieces = task.get("pieces") or []
        lines.append(f"Series «{task['series']}», piece {pieces[-1].get('n') if pieces else 1}"
                     f" (the next one: delegate.py next {task['series']} \"…\")")
    window = window_of(model, table)
    if use.get("context", 0) > 0.6 * window:
        lines.append(f"Context: {kilo(use['context'])} of {kilo(window)} tokens — the conversation is nearly full; more "
                     "work in it gets compacted soon" + (f" (next piece: delegate.py next {task['series']} --fresh)"
                                                        if task.get("series") else ""))
    resumed = turn_resumes(task)
    if resumed:
        lines.append(f"Resumed automatically after {len(resumed)} error{'s' if len(resumed) > 1 else ''} in this turn: "
                     + "; ".join(f"step {r.get('step')} «{str(r.get('error') or '?')[:120]}»" for r in resumed))

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
    running_bg = [b for b in background_commands(steps[turn:], turn) if b["running"]]
    if running_bg:
        pids = command_pids([b["line"] for b in running_bg], ls.pid) if state != "done" else {}
        lines += ["", "Still running in the background (Antigravity wakes the agent when one finishes):"]
        for b in running_bg:
            shown = b["line"] if len(b["line"]) <= 110 else b["line"][:107] + "…"
            lines.append(f"  ⏳ {b['task']} {shown}" + (f" — for {span(time.time() - b['since'])}" if b["since"] else ""))
            if b["last"]:
                lines.append(f"     last output: {b['last']}")
            procs = pids.get(b["line"]) or []
            if procs:
                lines.append("     processes: " + " › ".join(f"{p.pid} {p.name}" for p in procs))
            if b["log"]:
                lines.append(f"     log: {b['log']}")
    seen, missed = viewed_images(steps[turn:], turn)
    if seen or missed:
        shown_imgs = [f"{rel(p, task)} ({size_text(n)}" + (f", {k}×" if k > 1 else "") + ")" for p, n, k in seen[:12]]
        lines += ["", f"Images it opened ({len(seen)}): " + (", ".join(shown_imgs) or "none")
                  + (f" … and {len(seen) - 12} more" if len(seen) > 12 else "")]
        if missed:
            lines.append(f"  couldn't open {len(missed)}: "
                         + "; ".join(f"{rel(p, task)} — {why[:80]}" for p, why in missed[:5]))
    talk = chat_lines(task, turn)
    if talk:
        lines += ["", "Talk with you while it worked:"] + talk
    if final:
        text = clean(final, task)
        if not full and len(text) > REPORT_LIMIT:
            text = text[:REPORT_LIMIT].rstrip() + f"\n… [cut — delegate.py result {short} --full]"
        lines += ["", "The agent's report (its own words — check before you rely on it):", text]
    elif state == "done":
        lines += ["", "The agent left no final message" + (" — Antigravity's error above stopped it." if err else ".")]

    nxt = {"done": f"Next: review → delegate.py diff {short} (paths to narrow it, --stat for counts); "
                   f"follow-up in the same conversation → delegate.py send {short} \"…\".",
           "approval": "Next: the user approves or rejects it in Antigravity (only they should), or stop the task → "
                       f"delegate.py cancel {short}. Don't approve the agent's actions yourself.",
           "background": f"Next: review → delegate.py diff {short}; wait for the rest → delegate.py wait {short}.",
           "running": f"Next: wait → delegate.py wait {short} (in the background), or stop it → delegate.py cancel {short}.",
           "timeout": f"Next: wait more → delegate.py wait {short} (in the background), or stop it → "
                      f"delegate.py cancel {short}."}[state]
    if failed and ended.endswith("_ERROR"):
        count = int(s.get("stepCount") or 0)
        times = len(turn_resumes(task))
        why = ("the error doesn't look like a hiccup a resume fixes (quota, permissions, the model)"
               if int(task.get("no_resume_at", -1)) == count else
               "automatic resumes are off for it (--retries 0, or a task from before they existed)"
               if not task.get("retries_max") else
               f"it was resumed {times} time{'s' if times != 1 else ''} already"
               if times >= int(task.get("retries_max") or 0) else "")
        nxt = ((f"Not resumed automatically: {why}. " if why else "")
               + f"Next: go on in the same conversation (it keeps its context) → delegate.py send {short} "
               f"\"Go on from where you stopped.\", or look at what it did → delegate.py diff {short}.")
    lines += ["", nxt]
    code = 2 if state == "approval" or failed else 3 if state in ("timeout", "running") else 0
    first = use.get("first") or {}
    facts = {"calls": use.get("calls", 0), "input": use.get("input", 0), "cached": use.get("cached", 0),
             "output": use.get("output", 0), "usd": round(use["usd"], 4) if use.get("usd") else None,
             "first_call": (first.get("input", 0) + first.get("cached", 0)) if turn == 0 and first else None,
             "seconds": round((stop or time.time()) - start) if start else None,
             "quota_before": before if isinstance(before, (int, float)) else None, "quota_after": after,
             "ended": "error" if failed and ended.endswith("_ERROR") else ("failed" if failed else state)}
    return "\n".join(lines), code, facts


def shown_end(task: dict, state: str) -> List[str]:
    """The event keys a report of a finished turn has shown already: the agent's messages and the turn's end (a watch
    started later doesn't announce them again)."""
    heard = [m["id"] for m in task.get("chat") or [] if m.get("from") == "antigravity" and m.get("id")]
    resumed = [f"resume:{r.get('count')}" for r in turn_resumes(task)]  # the report lists them
    return heard + resumed + ([f"end:{int(task.get('turn') or 0)}"] if state == "done" else [])


def finish(ls: LS, task: dict, timeout: float) -> int:
    state, info = wait(ls, task, timeout)
    text, code, facts = render(ls, task, state, info)
    fields: Dict[str, object] = dict(state="gone" if info.get("gone") else state,
                                     told_add=shown_end(task, state))  # a watch won't repeat them
    if state in ("done", "background"):
        fields["finished"] = time.time()
    if state == "done" and facts.get("calls"):
        fields["costs"] = dict(task.get("costs") or {}, **{str(int(task.get("turn") or 0)): facts})
    update(task, **fields)
    print(text)
    return code


# ─────────────────────────────── what a task costs ───────────────────────────────

def first_costs(label: str, recs: List[dict], limit: int = 40) -> List[dict]:
    """What your recent tasks on this model measured in their first turn (`costs["0"]`, saved by every report of a
    finished task), newest first — a follow-up turn isn't a task of its own, so it doesn't count."""
    out = []
    for r in recs:
        c = (r.get("costs") or {}).get("0")
        if r.get("label") == label and isinstance(c, dict) and c.get("calls") and c.get("ended") in ("done", None):
            out.append(dict(c, started=r.get("started"), finished=r.get("finished"), id=r["id"]))
            if len(out) >= limit:
                break
    return out


def backfill(ls: LS, recs: List[dict], budget: float = 20.0) -> int:
    """Fill in `costs["0"]` for finished tasks from before reports saved it (asking Antigravity for their calls), for
    at most `budget` seconds. Returns how many it filled."""
    deadline, done, table = time.time() + budget, 0, prices()
    for r in recs:
        if time.time() > deadline:
            break
        if r.get("state") != "done" or (r.get("costs") or {}).get("0") or not r.get("started") or r["id"].startswith("q-") \
                or agy_cli.is_cli(r):
            continue
        try:
            use = usage(ls, r["id"], 0, table)
        except SOFT:
            continue
        if not use.get("calls"):
            continue
        seconds = (float(r["finished"]) - float(r["started"])) if r.get("finished") else None
        if int(r.get("turn") or 0) > 0:
            seconds = None  # a follow-up came later: the first turn's own length is unknown
        facts = {"calls": use["calls"], "input": use["input"], "cached": use["cached"], "output": use["output"],
                 "usd": round(use["usd"], 4) if use.get("usd") else None, "seconds": round(seconds) if seconds else None,
                 "quota_before": None, "quota_after": None, "ended": "done", "backfilled": True}
        if int(r.get("turn") or 0) > 0:
            facts["approx"] = True  # usage since step 0 covers its follow-ups too
        update(r, costs=dict(r.get("costs") or {}, **{"0": facts}))
        done += 1
    return done


def pct_per_usd(label: str, recs: List[dict]) -> Tuple[float, str]:
    """The share of a week's quota (in %) one API dollar takes on this model's quota group: the median your own
    tasks measured — only tasks that ran alone, so others didn't spend the same quota meanwhile — when there are
    three or more, else PCT_PER_USD. Returns (value, where it comes from)."""
    low = (label or "").lower()
    kind = next((w for w, _ in PCT_PER_USD if w and w in low), "")
    spans = [(float(r.get("started") or 0), float(r.get("finished") or 0), r["id"]) for r in recs if r.get("started")]
    ratios = []
    for r in recs:
        c = (r.get("costs") or {}).get("0") or {}
        b, a, usd = c.get("quota_before"), c.get("quota_after"), c.get("usd")
        if not all(isinstance(x, (int, float)) for x in (b, a, usd)) or usd < 0.02 or b <= a:
            continue
        if next((w for w, _ in PCT_PER_USD if w and w in str(r.get("label") or "").lower()), "") != kind:
            continue
        s0, s1 = float(r.get("started") or 0), float(r.get("finished") or 0)
        if not s1 or any(o != r["id"] and o0 < s1 and (o1 or o0 + 3600) > s0 for o0, o1, o in spans):
            continue
        ratios.append((b - a) * 100 / usd)
    if len(ratios) >= 3:
        return statistics.median(ratios), f"measured on {len(ratios)} of your tasks"
    return next(v for w, v in PCT_PER_USD if not w or w in low), "the plugin's measurement from 2026-11"


def quantile(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def estimate_of(m: dict, groups: List[dict], recs: List[dict]) -> dict:
    """What a task on model `m` costs here, from your past tasks on it: median and 90th percentile share of its
    quota group's week, API dollars, minutes, and how many such tasks the week's rest pays for."""
    smp = first_costs(m["label"], recs)
    usd = [float(x["usd"]) for x in smp if isinstance(x.get("usd"), (int, float)) and x["usd"] > 0]
    mins = [float(x["seconds"]) / 60 for x in smp if isinstance(x.get("seconds"), (int, float)) and x["seconds"] > 0]
    rate, source = pct_per_usd(m["label"], recs)
    g = group_of(m["label"], groups) or {}
    left = ((g.get("buckets") or {}).get("weekly") or {}).get("left")
    out = {"n": len(smp), "group": g.get("name", ""), "rate": rate, "rate_source": source, "left": left}
    if usd:
        med = statistics.median(usd)
        out.update(usd=med, usd90=quantile(usd, 0.9), share=med * rate, share90=quantile(usd, 0.9) * rate)
        if isinstance(left, (int, float)) and out["share"] > 0:
            out["tasks_left"] = left * 100 / out["share"]
    if mins:
        out["minutes"] = statistics.median(mins)
    return out


def estimate_text(est: dict) -> str:
    """«≈0.012% of «Gemini Models»'s week per task (median of 31; 9 in 10 under 0.04%) · ≈$0.27 · ≈6 min · the 62%
    left pays for ~5000 such tasks»."""
    if not est.get("share"):
        return "no finished tasks on it here yet — `models` and the tiers' notes give the benchmark's numbers"
    grp = f" of «{est['group']}»'s week" if est.get("group") else " of its quota's week"
    out = (f"≈{pct(est['share'])}{grp} per task (median of {est['n']}; 9 in 10 under {pct(est['share90'])})"
           f" · ≈${est['usd']:.2f} at API prices")
    if est.get("minutes"):
        out += f" · ≈{est['minutes']:.0f} min"
    if est.get("tasks_left"):
        n = est["tasks_left"]
        out += (f" · the {est['left'] * 100:.0f}% left pays for ~{n:,.0f} such tasks" if n >= 2
                else f" · the {est['left'] * 100:.0f}% left won't pay for one more")
    return out


# ─────────────────────────────── commands ───────────────────────────────

def tier_lines(ms: List[dict], default: str, groups: List[dict], sp: Dict[str, dict], now: float,
               uses: bool, recs: Optional[List[dict]] = None) -> List[str]:
    lines = []
    for t in TIERS:
        try:
            m, _notes, why = pick(t, ms, default, groups, now)
            speed = speed_text(sp, m["label"])
            lines.append(f"  {t:9} {m['id']:26} {m['label']}" + (f" · {speed}" if speed else ""))
            lines.append(f"            {why}")
            if recs is not None:
                est = estimate_of(m, groups, recs)
                if est.get("share"):
                    lines.append(f"            a task here: {estimate_text(est)}")
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
        out.append(f"  Antigravity «{g['name']}»: {s['text']}" + (f"; {s['forecast']}" if s.get("forecast") else "")
                   + f" → {verdict(s)}")
    return out or ["  Antigravity: limits not reported"]


CLI_QUOTA = ("  Antigravity CLI: it shows its quotas only to its own status line, not to other programs — the tiers "
             "below follow the model list alone (open Antigravity Desktop for the limits)")


def backend(a) -> Tuple[Optional[LS], List[dict], str, List[dict], Dict[str, dict]]:
    """For models and budget: (the language server or None for the CLI, models, default, quota groups, speeds)."""
    fresh_client()
    if choose_via(getattr(a, "via", None)) == "cli":
        ms, default = agy_cli.models()
        return None, ms, default, [], {}
    ls = connect()
    ms, default = models(ls)
    return ls, ms, default, quota(ls), speeds(ls)


def cmd_models(a) -> int:
    ls, ms, default, groups, sp = backend(a)
    now = time.time()
    table = prices()
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
    lines = [client_line(), "Quota left:"] + (quota_lines(groups, now) if ls else [CLI_QUOTA])
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
    ls, ms, default, groups, sp = backend(a)
    now = time.time()
    lim = claude_limits(a, now)
    cstate, ctext = claude_state(lim, now)
    src = {"given": "", "statusLine": " (from the statusLine)"}.get(lim.get("source") or "", "")
    lines = ["Limits now:", f"  Claude Code, your own plan: {ctext}{src}"
             + {"critical": " → almost used up", "tight": " → getting tight", "ok": " → fine", "unknown": ""}[cstate]]
    lines += quota_lines(groups, now) if ls else [CLI_QUOTA]
    lines += ["", "What each tier takes now (--model):"] + tier_lines(ms, default, groups, sp, now, False, records())
    states = [state_of(g, now) for g in groups]
    exp = experimental(ms)
    if exp:
        lines += ["", "Experimental — in no tier, an exact --model takes them (delegate.py models says when each fits): "
                  + ", ".join(m["id"] for m, _ in exp)]
    lines += ["", "How much to hand over:"] + ["  • " + x for x in advice(cstate, states, groups)]
    if not ls:
        lines.append("  • Antigravity's own limits are unknown from the CLI: hand over as if they were fine, and check "
                     "them in Antigravity Desktop before a big batch.")
    lines.append(client_line())
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
            with open(a.brief_file, encoding="utf-8-sig") as f:
                return f.read()
        except UnicodeDecodeError:
            raise Failure(f"{a.brief_file} isn't UTF-8 text — save it as UTF-8.")
        except OSError as e:
            raise Failure(f"can't read {a.brief_file}: {e}")
    if a.brief and a.brief != "-":
        return a.brief
    if sys.stdin is None or sys.stdin.isatty():
        raise Failure("give the brief as an argument, with --brief-file, or on stdin (a heredoc).")
    return sys.stdin.read()


def start_task(ls: LS, p: dict) -> Tuple[dict, List[str], str, bool]:
    """Start a task from its parameters — a run now, or a queued one when a slot frees up: pick the model by the limits
    left, find (or create) the Antigravity project, start the conversation, save the record. Returns (the record,
    notes on models passed over, why this model, whether it created the project)."""
    folder = p["cwd"]
    if not os.path.isdir(folder):
        raise Failure(f"no such folder: {folder}")
    fresh_client()
    if p.get("via") == "cli":  # the Antigravity CLI: no projects, no quotas; one headless agy process per turn
        ms, default = agy_cli.models()
        model, notes, why = pick(p["model"], ms, default, [])
        if model["id"] in ("flash_lite", "flash", "pro"):
            raise Failure("the Antigravity CLI takes no alias — give a tier or a model id (delegate.py models --via cli).")
        task = agy_cli.start(p, model, compose(p["brief"], folder, folder, p.get("reference") or ""))
        return task, notes, why + " (Antigravity CLI: no quota figures)", False
    if ls is None:
        raise Failure("this task runs in Antigravity Desktop, which isn't reachable now.")
    ms, default = models(ls)
    groups = quota(ls)
    model, notes, why = pick(p["model"], ms, default, groups)
    found = find_project(folder)
    created = False
    if found:
        project, root = found
    else:
        root = os.path.realpath((git(["rev-parse", "--show-toplevel"], folder) or "").strip() or folder)
        project, created = create_project(ls, root), True
    title = title_of(p["brief"], p.get("title") or "")
    top = (git(["rev-parse", "--show-toplevel"], folder) or "").strip()
    top = os.path.realpath(top) if top else ""
    before = dirty(top) if top else None  # before the agent starts: what its commands change shows up later
    resp = agentapi(ls, ["new-conversation", f"--model={model['id']}", f"--title={title}",
                         compose(p["brief"], folder, root, p.get("reference") or "")], project["id"])
    cid = str((resp.get("newConversation") or {}).get("conversationId") or "")
    if not cid:
        raise Failure(f"agentapi didn't return a conversation id: {json.dumps(resp)[:300]}")
    task = {"id": cid, "title": title, "model": model["id"], "label": model["label"], "project": project["id"],
            "project_name": project["name"], "created_project": created, "cwd": folder, "root": top or root,
            "started": time.time(), "turn": 0, "state": "running", "brief": p["brief"][:400],
            "quota_before": weekly_left(model["label"], groups), "dirty_before": before,
            "claude_session": p.get("claude_session") or "", "heard": 0, "chat": [],
            "retries_max": int(p.get("retries") or 0), "via": "desktop", "owner": p.get("owner") or ""}
    if p.get("series"):
        task.update(series=p["series"], series_reference=p.get("reference") or "",
                    pieces=[{"n": int(p.get("piece") or 1), "turn": 0, "at": time.time(), "brief": p["brief"][:200]}])
    save(task)
    prune()
    return task, notes, why, created


def started_lines(task: dict, notes: List[str], why: str, created: bool, recs: Optional[List[dict]] = None) -> List[str]:
    if agy_cli.is_cli(task):
        lines = [f"Started Antigravity task {task['id']} — {task['label']}, in the Antigravity CLI (agy, headless, "
                 f"{' '.join(agy_cli.FLAG_MODE)}) in {task['cwd']}."]
    else:
        lines = [f"Started Antigravity task {task['id']} — {task['label']}, project «{task['project_name']}»"
                 + (" (created for this folder)" if created else "") + "."]
    lines += ["  " + n for n in notes]
    lines.append(f"  Why this model: {why}")
    est = estimate_of({"label": task["label"]}, [], recs if recs is not None else records())
    if est.get("usd"):
        lines.append(f"  Expected (your last {est['n']} tasks on it): ≈${est['usd']:.2f} at API prices"
                     + (f", ≈{est['minutes']:.0f} min" if est.get("minutes") else "")
                     + f", ≈{pct(est['share'])} of its quota's week")
    return lines


def valid_series(name: str) -> str:
    name = (name or "").strip()
    if not re.fullmatch(r"[\w.-]{1,40}", name):
        raise Failure(f"a series name is up to 40 letters, digits, «_», «.» or «-»: {name!r}")
    return name


def series_task(name: str) -> Optional[dict]:
    """The newest task of a series (`run --series NAME`, `next NAME`)."""
    return next((r for r in records() if r.get("series") == name and not r["id"].startswith("q-")), None)


def cmd_run(a) -> int:
    brief = read_brief(a).strip()
    if not brief:
        raise Failure("the brief is empty.")
    if len(brief) > BRIEF_LIMIT:
        raise Failure(f"the brief has {len(brief)} characters (limit {BRIEF_LIMIT}) — put the details in a file in the "
                      "project and point the brief at it.")
    reference = read_templates(a.template or [])
    if len(brief) + len(reference) > PROMPT_LIMIT - 4000:
        raise Failure(f"the brief and its templates come to {len(brief) + len(reference)} characters (limit "
                      f"{PROMPT_LIMIT - 4000}: the first message goes on agentapi's command line) — point the brief at "
                      "the bigger files instead of inlining them.")
    folder = os.path.realpath(a.cwd or os.getcwd())
    if not os.path.isdir(folder):
        raise Failure(f"no such folder: {folder}")
    series = valid_series(a.series) if a.series else ""
    if series and series_task(series):
        raise Failure(f"the series «{series}» exists already — its next piece: delegate.py next {series} \"…\" (or "
                      "choose another name).")
    conf = bridge_settings()
    limit = conf["max_parallel"] if a.max_parallel is None else max(0, min(a.max_parallel, 50))
    p = {"brief": brief, "model": a.model, "cwd": folder, "title": a.title, "reference": reference, "series": series,
         "piece": 1, "retries": conf["retries"] if a.retries is None else max(0, min(a.retries, 10)),
         "max_parallel": limit, "claude_session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
         "owner": owner_of(a), "timeout": a.timeout}
    p["via"] = choose_via(getattr(a, "via", None))
    ls = connect() if p["via"] == "desktop" else None
    if a.dry_run:
        return dry_run(ls, p)
    began = time.time()
    queued: Optional[dict] = None
    with slots(wait=True) if limit else contextlib.nullcontext():  # counting and starting in one step
        busy, ahead = (active_tasks(ls), queued_tasks()) if limit else ([], [])
        if limit and (len(busy) >= limit or ahead):
            queued = enqueue(p)
        else:
            task, notes, why, created = start_task(ls, p)
    if queued:
        q = queued
        ensure_supervisor()
        print(f"⏸ Queued Antigravity task {q['id']}: {len(busy)} task{'s' if len(busy) != 1 else ''} run now "
              f"(--max-parallel {limit})" + (f", {len(ahead)} queued ahead of it" if ahead else "")
              + " — it starts by itself when a slot frees up.", flush=True)
        if a.no_wait:
            print(f"Follow it: delegate.py watch --follow (the Monitor tool), or delegate.py wait {q['id']} in the "
                  "background; take it off the queue: delegate.py cancel " + q["id"])
            return 0
        task = wait_started(q, a.timeout)
        if task is None:
            print(f"⏳ Antigravity task {q['id']} is still queued after {span(time.time() - began)} — wait more: "
                  f"delegate.py wait {q['id']} (in the background), or delegate.py cancel {q['id']}.")
            return 3
        print(f"Started queued task {q['id']} as {task['id']} — {task['label']}.", flush=True)
        return finish(ls, task, max(a.timeout - (time.time() - began), 1.0))
    print("\n".join(started_lines(task, notes, why, created)), flush=True)
    if task.get("retries_max"):
        ensure_supervisor()  # resumes it after an error even when nobody waits for it
    if a.no_wait:
        print(f"Follow it with: delegate.py watch {task['id'][:8]} (with the Monitor tool: its questions and messages "
              f"reach you as it works), or delegate.py wait {task['id'][:8]} in the background.")
        return 0
    return finish(ls, task, a.timeout)


def dry_run(ls: LS, p: dict) -> int:
    """run --dry-run: what would start, why, and what it is likely to cost — nothing is started."""
    ms, default = models(ls) if ls else agy_cli.models()
    groups = quota(ls) if ls else []
    model, notes, why = pick(p["model"], ms, default, groups)
    recs = records()
    est = estimate_of(model, groups, recs)
    prompt = compose(p["brief"], p["cwd"], p["cwd"], p.get("reference") or "")
    firsts = [int(c["first_call"]) for c in first_costs(model["label"], recs) if isinstance(c.get("first_call"), int)]
    lines = [f"Would start on {model['label']} ({model['id']}) — {why}"
             + (" — in the Antigravity CLI (no quota figures there)." if not ls else ".")] + ["  " + n for n in notes]
    lines.append(f"  A task here: {estimate_text(est)}")
    lines.append(f"  Its first message: {len(prompt)} characters (≈{kilo(len(prompt) // 4)} tokens), on top of "
                 "Antigravity's own prompt"
                 + (f" (a new conversation's first call reads ≈{kilo(int(statistics.median(firsts)))} tokens here, none of "
                    "them from cache; follow-ups in the same conversation read it from the cache)" if firsts else ""))
    if p.get("max_parallel"):
        busy, ahead = active_tasks(ls), queued_tasks()
        lines.append(f"  Slots: {len(busy)} of {p['max_parallel']} in use, {len(ahead)} queued"
                     + (" — it would wait in the queue" if len(busy) >= p["max_parallel"] or ahead else ""))
    lines.append("Nothing was started (--dry-run).")
    print("\n".join(lines))
    return 0


def cmd_wait(a) -> int:
    task = task_for(a.id)
    began = time.time()
    if task["id"].startswith("q-") and task.get("state") in ("canceled", "failed"):
        text, code = queue_report(task)  # it never started: there is no conversation to wait for
        print(text)
        return code
    if task["id"].startswith("q-"):
        started = wait_started(task, a.timeout)
        if started is None:
            print(f"⏳ Antigravity task {task['id']} is still queued after {span(time.time() - began)} ("
                  + queue_position(task) + ") — wait more: delegate.py wait " + task["id"])
            return 3
        print(f"Started queued task {task['id']} as {started['id']} — {started.get('label') or '?'}.", flush=True)
        task = started
    return finish(connect_for(task), task, max(a.timeout - (time.time() - began), 1.0))


# ─────────────────────────────── the queue and the supervisor ───────────────────────────────

def enqueue(p: dict) -> dict:
    """A task that waits for a free slot: a record «q-…» with everything start_task() needs."""
    rec = {"id": "q-" + uuid.uuid4().hex[:10], "state": "queued", "queued": time.time(), "started": 0,
           "params": p, "title": title_of(p["brief"], p.get("title") or ""), "cwd": p["cwd"], "model": p["model"],
           "label": "", "brief": p["brief"][:400], "claude_session": p.get("claude_session") or "",
           "series": p.get("series") or ""}
    save(rec)
    return rec


def queued_tasks() -> List[dict]:
    """The tasks waiting in the queue, oldest first. One a dispatcher claimed but didn't start within five minutes
    (it was stopped) goes back in line."""
    out = []
    for r in records():
        if r.get("state") == "starting" and not r.get("started_as") and time.time() - float(r.get("starting") or 0) > 300:
            update(r, state="queued")
        if r.get("state") == "queued":
            out.append(r)
    return sorted(out, key=lambda r: float(r.get("queued") or 0))


def queue_position(q: dict) -> str:
    queue = [r["id"] for r in queued_tasks()]
    return f"number {queue.index(q['id']) + 1} of {len(queue)} in the queue" if q["id"] in queue else "not in the queue"


def active_tasks(ls: Optional[LS], live: Optional[dict] = None) -> List[dict]:
    """Our tasks that take a slot now (started in the last day): the conversation runs (an approval it waits for
    included), or it ended its turn while work it started still runs; also one being started from the queue, and
    one just started that the language server doesn't list yet."""
    if live is None:
        try:
            live = (rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30).get("trajectorySummaries") or {}) if ls else {}
        except SOFT:
            live = {}
    out = []
    for r in records():
        if r.get("state") == "starting" and time.time() - float(r.get("starting") or 0) < 300:
            out.append(r)
            continue
        if (r["id"].startswith("q-") or not r.get("started") or time.time() - float(r["started"]) > 86400
                or r.get("state") not in ("running", "background", "timeout", "approval", "question")):
            continue  # a question: it holds its slot while it works on, not while it waits for the answer
        if agy_cli.is_cli(r):
            if agy_cli.running(r):
                out.append(r)
            continue
        status = str((live.get(r["id"]) or {}).get("status") or "")
        if status.endswith(("RUNNING", "BUSY")):
            out.append(r)
        elif status.endswith("IDLE") and r.get("state") in ("running", "background", "timeout") and ls:
            if resume_state(r, live.get(r["id"]) or {}) or background_runs(ls, r["id"]):
                out.append(r)  # about to be resumed, or its background work still runs
        elif not status and time.time() - float(r["started"]) < 60:
            out.append(r)
    return out


class slots:
    """The lock that makes counting the running tasks and starting one a single step (DISPATCH_LOCK): dispatch()
    and `run --max-parallel` hold it, so two of them never both take the last free slot. `wait`: block until it is
    free (a run); else give up at once when another process holds it (a dispatcher: that one does the work)."""

    def __init__(self, wait: bool) -> None:
        self.wait, self.fh, self.held = wait, None, False

    def __enter__(self) -> bool:
        try:
            os.makedirs(TASKS, exist_ok=True)
            self.fh = open(DISPATCH_LOCK, "a+", encoding="utf-8")
            if self.wait:
                compat.lock(self.fh)
                self.held = True
            else:
                self.held = try_lock(self.fh)
        except OSError:
            self.held = False
        return self.held

    def __exit__(self, *exc) -> None:
        if self.fh:
            if self.held:
                compat.unlock(self.fh)
            self.fh.close()


def dispatch(ls: LS, live: Optional[dict] = None) -> List[str]:
    """Start queued tasks while there are free slots, oldest first (each carries its own --max-parallel); one
    dispatcher at a time (slots). Lines for the supervisor's log."""
    log: List[str] = []
    if not queued_tasks():
        return log
    with slots(wait=False) as mine:
        if not mine:
            return log  # another process is starting tasks right now
        queue = queued_tasks()
        busy = len(active_tasks(ls, live)) if queue else 0

        def claim(rec: dict) -> bool:
            if rec.get("state") != "queued":
                return False
            rec.update(state="starting", starting=time.time())
            return True

        for q in queue:
            limit = int((q.get("params") or {}).get("max_parallel") or 0)
            if limit and busy >= limit:
                break  # first come, first served: the others wait behind it
            if ls is None and (q.get("params") or {}).get("via") != "cli":
                continue  # a Desktop task waits for Antigravity Desktop
            if not transact(q, claim):
                continue
            try:
                task, _notes, why, _created = start_task(ls, q["params"])
            except Exception as e:  # Failure, a timeout, a file that can't be written: back in line, tried 3 times
                tries = int(q.get("tries") or 0) + 1
                update(q, state="queued" if tries < 3 else "failed", tries=tries, error=str(e)[:300] or repr(e))
                log.append(f"couldn't start {q['id']} ({tries} of 3): {e!r}")
                break
            update(q, state="started", started_as=task["id"], why=why, label=task["label"])
            update(task, queued_as=q["id"], queued=q.get("queued"))
            busy += 1
            log.append(f"started {q['id']} as {task['id']} on {task['label']}")
    return log


def try_lock(fh) -> bool:
    """Lock a lock file without waiting: False when another process holds it."""
    try:
        if compat.WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def ensure_supervisor() -> None:
    """Start the supervisor unless one runs already (it holds its lock; a second one would end at once)."""
    if os.environ.get("AGP_DELEGATE_NO_SUPERVISOR"):
        return
    try:
        os.makedirs(TASKS, exist_ok=True)
        with open(SUPERVISOR_LOCK, "a+", encoding="utf-8") as fh:
            if not try_lock(fh):
                return
    except OSError:
        return
    spawn_detached([sys.executable, "-X", "utf8", os.path.abspath(__file__), "supervise"])


def note(msg: str) -> None:
    """A line in the supervisor's log (kept under ~200 KB)."""
    try:
        if os.path.isfile(SUPERVISOR_LOG) and os.path.getsize(SUPERVISOR_LOG) > 200_000:
            compat.replace(SUPERVISOR_LOG, SUPERVISOR_LOG + ".1")
        with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[{os.getpid()}] {msg}\n")
    except OSError:
        pass


def supervise_once(ls: LS, mine: List[dict], seen: Dict[str, int]) -> Tuple[List[str], bool]:
    """One look: resume the tasks Antigravity stopped with an error, bring the records of tasks whose turn is over up
    to date (done, background, a question for Claude Code, gone from Antigravity — once per change: `seen` holds the
    step count each was last looked at), then start queued tasks for the free slots. Returns the lines for the log,
    and whether anything still needs the supervisor: a task at work (a question or an approval waiting doesn't
    count — a reply or a follow-up starts the supervisor again), or the queue."""
    try:
        live = rpc(ls, "GetAllCascadeTrajectories", {}, timeout=30).get("trajectorySummaries") or {}
    except SOFT:
        return [], True
    out, busy = [], False
    for r in mine:
        s = live.get(r["id"])
        if not isinstance(s, dict):
            if gone(r, {}):  # Antigravity answered and no longer has it (deleted): nothing will come from it
                update(r, state="gone", finished=time.time())
                out.append(f"{r['id']} is gone from Antigravity")
            else:
                busy = True  # just started: not listed yet
            continue
        status = str(s.get("status") or "")
        if status != "CASCADE_RUN_STATUS_IDLE":
            if status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY") and not busy:
                # an approval the user hasn't given yet keeps the conversation RUNNING without any work going on
                busy = r.get("state") != "approval" or pending(ls, r["id"], int(r.get("turn") or 0))[0] is None
            continue
        what, entry = auto_resume(ls, r, s)
        if what:
            busy = True
            if what == "resumed" and entry:
                out.append(f"resumed {r['id']} after an error: {entry.get('error') or '?'}")
            continue
        count = int(s.get("stepCount") or 0)
        if seen.get(r["id"]) == count and r.get("state") != "background":
            continue  # nothing new since the last look
        seen[r["id"]] = count
        listen(ls, r, start=int(r.get("turn") or 0))  # a question at the end must not be taken for the end
        state = "question" if open_questions(r) else idle_state(ls, r, s)
        busy = busy or state in ("running", "background")
        fields = recorded(r, state)
        if fields:
            update(r, **fields)
            out.append(f"{r['id']} is {state}")
    out += dispatch(ls, live)
    return out, busy or bool(queued_tasks())


def cmd_supervise(a) -> int:
    """Runs by itself, detached (ensure_supervisor): while a task of ours is at work or waits in the queue, every
    POLL seconds it resumes the ones Antigravity stopped with an error, keeps their records up to date and starts
    queued ones for free slots. It ends SUPERVISE_IDLE seconds after the last of them, ten minutes after Antigravity
    went away, or after 12 h. One at a time (its lock file)."""
    os.makedirs(TASKS, exist_ok=True)
    fh = open(SUPERVISOR_LOCK, "a+", encoding="utf-8")
    try:
        if not try_lock(fh):
            return 0
        note("started")
        idle_since = away_since = time.time()
        ends = time.time() + 12 * 3600
        seen: Dict[str, int] = {}
        while time.time() < ends:
            recs = records()
            queue = [r for r in recs if r.get("state") in ("queued", "starting")]
            mine = [r for r in recs if r.get("state") in ("running", "background", "timeout", "question", "approval")
                    and r.get("started") and not r["id"].startswith("q-") and time.time() - float(r["started"]) < 86400]
            busy = bool(queue)
            cli = [r for r in mine if agy_cli.is_cli(r)]
            mine = [r for r in mine if not agy_cli.is_cli(r)]
            for r in cli:  # the CLI's tasks need no language server: errors resumed, messages sent, records kept
                try:
                    state, info = agy_cli.state_now(r)
                    busy = busy or state == "running"
                    fields = recorded(r, "canceled" if info.get("failed") == "canceled" else state)
                    if fields:
                        update(r, **fields)
                        note(f"{r['id']} is {state} (CLI)")
                except Exception as e:  # keep supervising
                    note(f"a look at CLI task {r['id']} failed: {e!r}")
            if queue and not mine and not discover():
                try:
                    for line in dispatch(None):
                        note(line)
                except Exception as e:
                    note(f"dispatch failed: {e!r}")
            if queue or mine:
                ls = discover()  # never starts Antigravity: a closed one is the user's choice
                if ls:
                    away_since = time.time()
                    try:
                        lines, at_work = supervise_once(ls, mine, seen)
                        busy = busy or at_work
                        for line in lines:
                            note(line)
                    except SOFT as e:
                        busy = True
                        note(f"a look failed: {e}")
                    except Exception as e:  # keep supervising: one bad look mustn't end it
                        busy = True
                        note(f"a look crashed: {e!r}")
                elif time.time() - away_since > 600:
                    note("Antigravity isn't running — ending")
                    break
                else:
                    busy = True
            if busy:
                idle_since = time.time()
            elif time.time() - idle_since >= SUPERVISE_IDLE:
                break
            time.sleep(POLL)
        note("ended")
        return 0
    finally:
        fh.close()


def wait_started(q: dict, timeout: float) -> Optional[dict]:
    """Wait until a queued task has started and return the task it became; None if it is still queued after
    `timeout`. While waiting it starts queued tasks itself too, in case no supervisor could run."""
    deadline, last = time.time() + max(timeout, 1.0), 0.0
    while time.time() < deadline:
        rec = load(q["id"]) or {}
        state = rec.get("state")
        if state == "started" and rec.get("started_as"):
            return load(str(rec["started_as"])) or task_for(str(rec["started_as"]))
        if state == "canceled":
            raise Failure(f"queued task {q['id']} was taken off the queue.")
        if state == "failed":
            raise Failure(f"queued task {q['id']} couldn't start: {rec.get('error') or '?'}")
        if time.time() - last >= POLL:
            last = time.time()
            try:
                dispatch(discover())
            except SOFT:
                pass
        time.sleep(min(POLL, 3.0))
    return None


def recorded(task: dict, state: str) -> dict:
    """The fields that bring a task's record up to the state just found (for `list`, `watch` and the Control Center),
    or {} when it says so already, the task isn't one of ours, or it was canceled and hasn't started again."""
    if (not task.get("started") or state == task.get("state")
            or (task.get("state") == "canceled" and state in ("done", "background"))):
        return {}
    return dict(state=state, **({"finished": time.time()} if state in ("done", "background", "gone") else {}))


def queue_report(q: dict) -> Tuple[str, int]:
    """The report of a task that hasn't started from the queue (yet)."""
    short = q["id"]
    if q.get("state") == "failed":
        return f"❌ Antigravity task {short} couldn't start from the queue: {q.get('error') or '?'}", 1
    if q.get("state") == "canceled":
        return f"Antigravity task {short} was taken off the queue before it started.", 2
    limit = int((q.get("params") or {}).get("max_parallel") or 0)
    brief = next((ln.strip() for ln in str(q.get("brief") or "").splitlines() if ln.strip()), "")
    return (f"⏸ Antigravity task {short} waits in the queue ({queue_position(q)}; at most {limit} run at once) — it "
            f"starts by itself when a slot frees up.\nBrief: {brief[:160]}\nNext: wait for it → delegate.py wait {short} "
            f"(in the background), or take it off the queue → delegate.py cancel {short}."), 3


def cmd_result(a) -> int:
    task = task_for(a.id)
    if task["id"].startswith("q-"):
        text, code = queue_report(task)
        print(text)
        return code
    ls = connect_for(task)
    state, info = state_now(ls, task)
    text, code, facts = render(ls, task, state, info, full=a.full)
    fields = recorded(task, "gone" if info.get("gone") else state)
    shown = shown_end(task, state)
    if shown:
        fields["told_add"] = shown  # a watch won't repeat them
    turn = str(int(task.get("turn") or 0))
    if state == "done" and facts.get("calls") and turn not in (task.get("costs") or {}):
        fields["costs"] = dict(task.get("costs") or {}, **{turn: facts})
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
    s, asked = deliver(connect_for(task), task, message)
    if s.get("how") == "queued":
        print(f"Its turn is still running in the Antigravity CLI, which takes no message mid-turn: your "
              f"{'answer' if asked else 'message'} goes out as its next turn the moment this one ends "
              f"(Antigravity task {task['id']}).")
        print(f"Next: delegate.py wait {task['id'][:8]} in the background (or keep your watch running).")
        if task.get("retries_max"):
            ensure_supervisor()
        return 0
    if task.get("retries_max"):
        ensure_supervisor()
    short = task["id"][:8]
    idle = s.get("status") == "CASCADE_RUN_STATUS_IDLE"
    print(("Answered its question" if asked else "Sent your message") + f" to Antigravity task {task['id']}"
          + (" — it had ended its turn, so this wakes it up." if idle else " — it reads it between its steps."))
    print(f"Next: delegate.py wait {short} in the background (or keep your watch running) for its report.")
    return 0


def untold_end(t: dict) -> bool:
    """A task that is over, but no watch and no report has told Claude Code so yet (the supervisor or `list` saw the
    end first, between two watches): the next watch tells it."""
    if time.time() - float(t.get("finished") or 0) > 6 * 3600:
        return False
    told = set(t.get("told") or [])
    if t["id"].startswith("q-"):
        return t.get("state") == "failed" and f"start:{t['id']}" not in told
    return t.get("state") in ("done", "gone") and f"end:{int(t.get('turn') or 0)}" not in told


def owner_of(a) -> str:
    """The name of the agent a task or a watch belongs to (--owner, else AGP_DELEGATE_OWNER): with several agents of
    one Claude Code session handing over work side by side, each names its own and its watch takes only those."""
    name = (getattr(a, "owner", None) or os.environ.get("AGP_DELEGATE_OWNER") or "").strip()
    if name and not re.fullmatch(r"[\w.@-]{1,40}", name):
        raise Failure(f"--owner is up to 40 letters, digits, «_», «.», «@» or «-»: {name!r}")
    return name


def watched(ids: List[str], owner: str = "") -> List[dict]:
    """The tasks `watch` follows: the ids given (a queued one too), or else the ones that this Claude Code session
    started (from this folder, when the session is unknown) in the last day and that aren't over — or are, without
    anyone having told Claude Code yet. With an owner only that owner's tasks; without one, only tasks run without
    an owner (another agent's named tasks are its own watch's)."""
    if ids:
        return [task_for(i) for i in ids]
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    here = os.path.realpath(os.getcwd())
    out = []
    for t in records():
        when = float(t.get("started") or t.get("queued") or 0)
        if time.time() - when > 86400 or (t.get("state") not in NOT_OVER + ("starting",) and not untold_end(t)):
            continue
        if str(t.get("owner") or "") != owner:
            continue
        cwd = str(t.get("cwd") or "")
        if (t.get("claude_session") == session) if session else (cwd and (inside(cwd, here) or inside(here, cwd))):
            out.append(t)
    return out


CLARIFIED = "Clarification after the check: "  # a short note the agent added after the plugin's Stop check
SHORT_NOTE = (3, 500)  # lines, characters: a note, not a report


def turn_messages(steps: List[dict], turn: int) -> List[Tuple[str, bool]]:
    """The agent's messages of a turn after the last message from the user or the bridge in it, each with whether a
    system message (a hook's return — its own text never goes into the report) came between it and the one before."""
    lo = turn
    for i in range(len(steps) - 1, turn - 1, -1):
        if str(steps[i].get("type") or "") == "CORTEX_STEP_TYPE_USER_INPUT":
            lo = i + 1
            break
    out: List[Tuple[str, bool]] = []
    between = False
    for st in steps[lo:]:
        kind = str(st.get("type") or "")
        if kind == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE" and out:
            between = True
        elif kind == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse") or {}
            text = str(pr.get("modifiedResponse") or pr.get("response") or "").strip()
            if text:
                out.append((text, between))
                between = False
    return out


def _bare(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>|\[\]()~-]+", " ", text or "")).strip().lower()[:6000]


def assemble_report(msgs: List[Tuple[str, bool]]) -> str:
    """The turn's report from its messages (turn_messages()): the last one — but a later near-repeat of an earlier
    report replaces it (one copy, the later), and a short note written after a hook sent the report back (the Stop
    check) goes below the full report as a clarification instead of standing in for it."""
    best, notes = "", []
    for text, after_hook in msgs:
        if not best:
            best = text
            continue
        a, b = _bare(best), _bare(text)
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        if a and b and sm.real_quick_ratio() >= 0.8 and sm.quick_ratio() >= 0.8 and sm.ratio() >= 0.8:
            best, notes = text, []
            continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        short = len(lines) <= SHORT_NOTE[0] and len(text) <= SHORT_NOTE[1]
        if after_hook and short and report_head(best)[0] and not report_head(text)[0]:
            notes.append(re.sub(r"\s+", " ", text).strip())
            continue
        best, notes = text, []
    return best + "".join(f"\n\n{CLARIFIED}{n}" for n in notes)


def report_head(text: str) -> Tuple[str, str]:
    """How the agent's report says the task went: («done» | «partly done» | «blocked», the rest of its first line)
    for a first line that starts with ✅ ⚠️ ⛔ (also under a heading, in bold or as a list item); the words after
    «STATUS:» and no line for a report in the older STATUS / CHANGED / CHECKS / NOTES form; ("", "") for a message
    that is no report (a line it waits with, a question)."""
    text = str(text or "")
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()][:3]:
        bare = line.lstrip("*_#>-•· \t")
        for mark, word in VERDICTS:
            if bare.startswith(mark):
                rest = re.sub(r"\*\*|__|`", "", bare[len(mark):]).lstrip("\ufe0f").strip(" *_:—–-")
                return word, re.sub(r"\s+", " ", rest).strip()
    m = STATUS_LINE.search(text)  # «STATUS: done», also as «**STATUS:** done»
    return (m.group(1).strip(" *_`"), "") if m else ("", "")


def verdict_text(text: str, n: int = 90) -> str:
    """«done — <its first line>» for `watch` and `series`; "" for a message that is no report."""
    word, line = report_head(text)
    if word and line:
        return f"{word} — «{line if len(line) <= n else line[:n - 1].rstrip() + '…'}»"
    return word


def final_status(ls: LS, cid: str, count: int) -> str:
    """How the agent's last message says the task went («done — «…»», «partly done — «…»», «partial: …» from an
    older report), or ""."""
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": max(count - 6, 0)},
                    timeout=30).get("steps") or []
    except SOFT:
        return ""
    for st in reversed([s for s in steps if isinstance(s, dict)]):
        if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse") or {}
            said = verdict_text(str(pr.get("modifiedResponse") or pr.get("response") or ""))
            if said:
                return said
    return ""


Event = Tuple[str, Optional[str]]  # (key, text): told once per key; a None text is only marked as told


def talk_events(task: dict) -> List[Event]:
    """The agent's messages to Claude Code as `watch` events (a question already answered is only marked told)."""
    short, out = task["id"][:8], []
    still_open = {m.get("id") for m in open_questions(task)}
    for m in task.get("chat") or []:
        if m.get("from") != "antigravity" or not m.get("id"):
            continue
        if m.get("kind") == "question" and m["id"] not in still_open:
            out.append((m["id"], None))  # you have answered it already
        elif m.get("kind") == "question":
            out.append((m["id"], f"❓ Antigravity task {short} asks you (its words, not the user's):\n{indent(m['text'])}\n"
                                 f"  → answer: delegate.py reply {short} \"…\" — it waits for the answer"))
        else:
            out.append((m["id"], f"💬 Antigravity task {short} tells you (its words, not the user's):\n{indent(m['text'])}\n"
                                 f"  (it keeps working; reply only if it helps: delegate.py reply {short} \"…\")"))
    return out


def watch_events(ls: LS, task: dict) -> Tuple[List[Event], bool]:
    """The events of one task for `watch` (each with its key), and whether the task is over."""
    if agy_cli.is_cli(task):
        return agy_cli.watch_events(task)
    short = task["id"][:8]
    listen(ls, task)
    out = talk_events(task)
    out += [(f"resume:{r.get('count')}", resume_line(task, r)) for r in task.get("resumes") or []]
    found = listed(ls, task["id"])
    key = f"end:{int(task.get('turn') or 0)}"
    if gone(task, found):
        out.append((key, f"❌ Antigravity task {short} is gone: Antigravity no longer lists its conversation (deleted in "
                         "the app?) — nothing more comes from it"))
        update(task, state="gone", finished=time.time())
        return out, True
    s = found or {}
    status = str(s.get("status") or "")
    if status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY"):
        ask, _ = pending(ls, task["id"], max(int(task.get("turn") or 0), int(task.get("since") or 0)))
        if ask:
            out.append((f"approval:{ask.get('step')}",
                        f"⏸ Antigravity task {short} waits for the user's approval in Antigravity: {ask.get('action')} "
                        f"{ask.get('target')}".rstrip() + f" → tell the user (only they approve), or delegate.py cancel {short}"))
        return out, False
    if not status or open_questions(task):
        return out, False  # it ended its turn to wait for your answer (or the summary didn't come): not over
    what, entry = auto_resume(ls, task, s)
    if what:
        if entry:
            out.append((f"resume:{entry['count']}", resume_line(task, entry)))
        return out, False  # Antigravity stopped it with an error: resumed, or about to be
    if background_runs(ls, task["id"]):
        return out, False  # idle, but something it started still runs — it goes on when that finishes
    since = max(int(task.get("turn") or 0), int(task.get("since") or 0))
    if not answered(ls, task["id"], since):
        return out, False  # a message to it hasn't been picked up yet
    listen(ls, task, start=int(task.get("turn") or 0))  # once more over the whole turn before calling it over
    out += talk_events(task)
    if open_questions(task):
        return out, False  # it ended its turn to wait for your answer
    ended = str(s.get("latestExecutorTerminationReason") or "")
    failed = FAILED_ENDS.get(ended)
    if failed:
        err = ""
        if ended.endswith("_ERROR"):
            count = int(s.get("stepCount") or 0)
            base = max(int(task.get("turn") or 0), count - 40)
            try:
                steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": task["id"], "stepOffset": base}, timeout=30)
                err = turn_error([x for x in steps.get("steps") or [] if isinstance(x, dict)], base)[0]
            except SOFT:
                pass
        out.append((key, f"❌ Antigravity task {short} {failed}" + (f": «{err[:200]}»" if err else "")
                    + f" → delegate.py result {short}"))
    else:
        verdict = final_status(ls, task["id"], int(s.get("stepCount") or 0))
        out.append((key, f"✅ Antigravity task {short} finished" + (f" ({verdict})" if verdict else "")
                    + f" → read its report: delegate.py result {short}, then check it (delegate.py diff {short})"))
    update(task, state="canceled" if ended.endswith("USER_CANCELED") else "done", finished=time.time())
    return out, True


def queue_events(q: dict) -> Tuple[List[Event], Optional[dict], bool]:
    """A queued task for `watch`: its start (the task it became), or its end without starting. (events, the task
    to follow now or None, whether this entry is over)."""
    state = q.get("state")
    if state == "started" and q.get("started_as"):
        task = load(str(q["started_as"]))
        if not task:
            return [], None, True
        return ([(f"start:{task['id']}", f"▶ Antigravity task {q['id']} left the queue: it runs now as {task['id'][:8]} "
                                         f"on {task.get('label') or '?'} — events follow")], task, True)
    if state == "failed":
        return [(f"start:{q['id']}", f"❌ Antigravity task {q['id']} couldn't start: {q.get('error') or '?'} → "
                                      "delegate.py list")], None, True
    if state == "canceled":
        return [], None, True
    return [], None, False


def number(text: str, n: int) -> str:
    """«❓ Antigravity task …» → «❓ #3 Antigravity task …»."""
    head, _, rest = text.partition(" ")
    return f"{head} #{n} {rest}"


def claim_rank(task: dict, w: dict) -> int:
    """How strong a watch's claim on a task is: 2 — it named the task's id, or it is the watch of the task's owner;
    1 — a general watch (`watch` with no ids) of a task without an owner; 0 — none (a task another agent owns)."""
    if task["id"] in (w.get("named") or []) or task.get("queued_as") in (w.get("named") or []):
        return 2
    owner = str(task.get("owner") or "")
    if owner:
        return 2 if w.get("owner") == owner else 0
    return 1 if "rank" not in w or w.get("rank") is None else max(int(w.get("rank") or 0), 1)


def watching(task: dict, me: dict) -> bool:
    """One watch per task: True when this watch (`me`) has the task — it takes over from an older watch with no
    stronger claim (claim_rank), or from one that stopped beating; False when a live watch has a stronger claim, or
    an equal one and is newer (then this one leaves it alone). A general watch never takes a task that another
    live watch named or whose owner it isn't."""
    mine = claim_rank(task, me)
    if mine <= 0:
        return False
    w = task.get("watcher") or {}
    if w.get("id") == me["id"]:
        if time.time() - float(w.get("beat") or 0) > 30:
            take(task, me)
        return True
    live = time.time() - float(w.get("beat") or 0) < 90
    theirs = int(w.get("rank") or 1) if w else 0
    if live and (theirs > mine or (theirs == mine and float(w.get("since") or 0) > me["since"])):
        return False
    take(task, me)
    return True


def take(task: dict, me: dict) -> None:
    def change(rec: dict) -> bool:
        rec["watcher"] = dict({k: v for k, v in me.items() if k != "named"}, beat=time.time(), pid=os.getpid(),
                              rank=claim_rank(rec, me))
        return True
    transact(task, change)


def cmd_watch(a) -> int:
    """For Claude Code's Monitor tool: every stdout line is an event, so print only what to act on — the agent's
    questions and messages, an approval it waits for, a resume after an error, a queued task that starts, the end —,
    numbered per task (#1, #2 …) and never twice (each is claimed in the task's record before it is printed, and a
    newer watch takes a task over). Ends when every watched task is over — with --follow it also takes the tasks
    this session starts later and keeps going until --timeout (0: no limit)."""
    follow = bool(getattr(a, "follow", False))
    owner = owner_of(a)
    tasks = watched(a.ids, owner)
    if not tasks and not follow:
        print("No running Antigravity tasks to watch (delegate.py list shows the recent ones; watch --follow waits "
              "for new ones).")
        return 0
    ls: Optional[LS] = None

    def server() -> Optional[LS]:  # only a Desktop task needs it: a watch of CLI tasks runs without Antigravity
        nonlocal ls
        if ls is None:
            ls = connect()
        return ls
    me = {"id": uuid.uuid4().hex[:10], "since": time.time(), "owner": owner,
          "named": sorted({t["id"] for t in tasks} if a.ids else set())}
    live: Dict[str, dict] = {}
    for t in tasks:
        if a.ids or watching(t, me):
            if a.ids:
                take(t, me)
            live[t["id"]] = t
    deadline = time.time() + a.timeout if a.timeout and a.timeout > 0 else None
    printed, scanned, dispatched = 0, time.time(), 0.0
    while True:
        for tid in list(live):
            t = load(tid) or live[tid]  # what a wait, a reply or the supervisor wrote meanwhile
            if not watching(t, me):
                del live[tid]  # a newer watch has it now: its events go there
                continue
            if tid.startswith("q-"):
                events, task, over = queue_events(t)
            else:
                (events, over), task = watch_events(None if agy_cli.is_cli(t) else server(), t), None
            if events:
                keys = list(dict.fromkeys(k for k, _ in events))  # the last look reads the talk twice: once each
                new, n = claim_events(t, keys, list(dict.fromkeys(k for k, text in events if text)))
                lines, done_keys = [], set()
                for key, text in events:
                    if text and key in new and key not in done_keys:
                        done_keys.add(key)
                        lines.append(number(text, n))
                        n += 1
                if lines:
                    print("\n".join(lines), flush=True)
                    printed += len(lines)
            if over:
                del live[tid]
                if task and task["id"] not in live:
                    if tid in me["named"]:
                        me["named"].append(task["id"])  # a queued task this watch named: the task it became too
                    if watching(task, me):
                        live[task["id"]] = task
        if follow and time.time() - scanned >= 10:
            scanned = time.time()
            for t in watched([], owner):
                if t["id"] not in live and watching(t, me):
                    live[t["id"]] = t
        if not live and not follow:
            return 0
        if deadline and time.time() >= deadline:
            if live:
                ids = [i if i.startswith("q-") else i[:8] for i in live]
                again = "delegate.py watch --follow" if follow else "delegate.py watch " + " ".join(ids)
                print(f"⏳ watch: still running — {', '.join(ids)}; {printed} event{'s' if printed != 1 else ''} in "
                      f"this watch. Start it again to go on: {again}", flush=True)
            return 0
        if any(i.startswith("q-") for i in live) and time.time() - dispatched >= POLL:
            dispatched = time.time()  # in case no supervisor could run: start what a free slot allows
            try:
                dispatch(ls or discover())
            except SOFT:
                pass
        time.sleep(WATCH_POLL)


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
    note = ""
    if agy_cli.is_cli(task):
        files, note = agy_cli.changed_files(task)
    else:
        files = changed_files(connect(), task)
    if a.paths:
        want = [p if os.path.isabs(p) else re.sub(r"^(?:\./)+", "", p.replace("\\", "/")) for p in a.paths]
        files = [f for f in files if any(f["rel"] == w or f["rel"].endswith("/" + w) or
                                         os.path.normcase(f["path"]) == os.path.normcase(w) for w in want)]
    if note:
        print(f"(Diff: {note}.)")
    if not files:
        print("No file changes by this task" + (" match those paths." if a.paths else "."))
        return 0
    if a.stat:
        print("\n".join(f"{f['kind']} {f['rel']}  +{f['plus']} −{f['minus']}" for f in files))
        return 0
    out: List[str] = []
    for f in files:
        if f.get("no_old"):
            out.append(f"~ {f['rel']}: changed (its old text wasn't kept: not a git folder)")
            continue
        old = f["old"].splitlines() if f["kind"] != "A" else []
        new = f["new"].splitlines() if f["kind"] != "D" else []
        out += list(difflib.unified_diff(old, new, "/dev/null" if f["kind"] == "A" else "a/" + f["rel"],
                                         "/dev/null" if f["kind"] == "D" else "b/" + f["rel"], lineterm="", n=3))
    if len(out) > a.max_lines:
        cut = len(out) - a.max_lines
        out = out[:a.max_lines] + [f"… {cut} more lines — narrow it with paths, or --max-lines {len(out)}"]
    print("\n".join(out))
    print("(Changes in the folder since the task started, as they are now.)" if agy_cli.is_cli(task) else
          "(The agent's changes as it left them; later edits to these files aren't shown.)")
    return 0


def follow_up(ls: LS, task: dict, message: str) -> dict:
    """Send more work into a task's conversation once its turn is over (a follow-up or the next piece of a series):
    the report from here on covers only this new turn."""
    if agy_cli.is_cli(task):
        s, _ = deliver(ls, task, message, kind="follow-up")
        if task.get("retries_max"):
            ensure_supervisor()
        return s
    before = weekly_left(task["label"], quota(ls)) if task.get("label") else task.get("quota_before")
    s, _ = deliver(ls, task, message, kind="follow-up")
    update(task, turn=int(s.get("stepCount") or 0), sent=time.time(), quota_before=before, state="running")
    if task.get("retries_max"):
        ensure_supervisor()
    return s


def cmd_send(a) -> int:
    task = task_for(a.id)
    if task["id"].startswith("q-"):
        raise Failure(f"{task['id']} hasn't started yet (it waits in the queue): wait for it first, or cancel it and "
                      "run a new task with the whole brief.")
    message = read_message(a, "follow-up message")
    if agy_cli.is_cli(task):
        state, _ = agy_cli.state_now(task)
        if state in ("running", "question"):
            s, asked = deliver(None, task, message)
            print(("Answered its question" if asked and s.get("how") == "sent" else
                   "Its turn still runs — your message goes out as its next turn when this one ends" if
                   s.get("how") == "queued" else "Sent your message") + f" (Antigravity CLI task {task['id']}).",
                  flush=True)
        else:
            follow_up(None, task, message)
            print(f"Sent the follow-up to Antigravity CLI task {task['id']} (a new turn in the same conversation).",
                  flush=True)
        if a.no_wait:
            print(f"Wait for it with: delegate.py wait {task['id'][:8]}  (run that in the background)")
            return 0
        return finish(None, task, a.timeout)
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
        follow_up(ls, task, message)
        print(f"Sent the follow-up to Antigravity task {task['id']}.", flush=True)
    if a.no_wait:
        print(f"Wait for it with: delegate.py wait {task['id'][:8]}  (run that in the background)")
        return 0
    return finish(ls, task, a.timeout)


def last_report(ls: LS, task: dict) -> str:
    """A task's last report (RULES 6, or the older STATUS / CHANGED / CHECKS / NOTES form), for a fresh conversation
    of its series."""
    if agy_cli.is_cli(task):
        return agy_cli.last_report(task)
    try:
        steps = rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": task["id"]}, timeout=60).get("steps") or []
    except SOFT:
        return "(not available)"
    for st in reversed([x for x in steps if isinstance(x, dict)]):
        if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = st.get("plannerResponse") or {}
            text = str(pr.get("modifiedResponse") or pr.get("response") or "").strip()
            if report_head(text)[0]:
                return clean(text, task)[:2500]
    return "(it left no report)"


def cmd_next(a) -> int:
    """The next piece of a series: in the same conversation once the previous piece is over, so the agent keeps what
    it read and the cache keeps Antigravity's prompt — a new conversation reads its whole prompt uncached first. A
    fresh conversation (--fresh, or when the context is nearly full) gets the series' templates, the last piece's
    report and the new piece."""
    name = valid_series(a.name)
    task = series_task(name)
    if not task:
        raise Failure(f"no series «{name}» — start it with delegate.py run --series {name} … (its first piece).")
    message = read_message(a, "piece")
    if len(message) > BRIEF_LIMIT:  # before waiting for the last piece: it would be refused after the wait anyway
        raise Failure(f"the piece has {len(message)} characters, more than the {BRIEF_LIMIT} a brief takes — nothing "
                      "was sent. Put the details in a file in the project and point the piece at it.")
    ls = connect_for(task)
    began = time.time()
    state, info = state_now(ls, task)
    pieces = task.get("pieces") or [{"n": 1, "turn": 0}]
    n = int(pieces[-1].get("n") or len(pieces)) + 1
    if state == "question":
        raise Failure(f"piece {n - 1} of «{name}» waits for your answer to its question — delegate.py reply "
                      f"{task['id'][:8]} \"…\" first (delegate.py result {task['id'][:8]} shows it).")
    if state == "approval":
        raise Failure(f"piece {n - 1} of «{name}» waits for the user's approval in Antigravity — that comes first.")
    if state in ("running", "background", "timeout"):
        print(f"Piece {n - 1} of «{name}» is still running — waiting for it to end before piece {n} …", flush=True)
        state, info = wait(ls, task, a.timeout)
        text, code, facts = render(ls, task, state, info)
        if state != "done":
            print(text)
            return code or 3
        print(f"Piece {n - 1} of «{name}» ended — its report (the next piece's report replaces it in `result`):\n"
              f"{text}\n", flush=True)  # read it now: after the next piece, `result` covers only the new turn
        update(task, state="done", finished=time.time(),
               costs=dict(task.get("costs") or {}, **{str(int(task.get("turn") or 0)): facts}))
    table = prices()
    use = agy_cli.usage(dict(task, turn=0)) if agy_cli.is_cli(task) else usage(ls, task["id"], 0, table)
    full = use.get("context", 0) > 0.75 * window_of(task.get("label") or "", table)
    if a.fresh or full:
        why = "--fresh" if a.fresh else f"its context is nearly full ({kilo(use['context'])} tokens)"
        brief = FRESH_SERIES.format(name=name, done=n - 1, last=last_report(ls, task)) + message
        p = {"brief": brief, "model": a.model or task.get("model") or "standard", "cwd": task.get("cwd") or os.getcwd(),
             "title": f"{name} #{n}", "reference": task.get("series_reference") or "", "series": name, "piece": n,
             "retries": int(task.get("retries_max") or 0), "claude_session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
             "via": task.get("via") or "desktop", "owner": task.get("owner") or "", "timeout": a.timeout}
        new, notes, model_why, created = start_task(ls, p)
        print(f"Piece {n} of «{name}» starts a new conversation ({why}).", flush=True)
        print("\n".join(started_lines(new, notes, model_why, created)), flush=True)
        if new.get("retries_max"):
            ensure_supervisor()
        task = new
    else:
        follow_up(ls, task, PIECE.format(n=n, name=name, message=message))
        update(task, pieces=(task.get("pieces") or [{"n": 1, "turn": 0, "at": task.get("started")}])
               + [{"n": n, "turn": int(task.get("turn") or 0), "at": time.time(), "brief": message[:200]}])
        print(f"Sent piece {n} of «{name}» to Antigravity task {task['id']} (same conversation, context "
              f"{kilo(use.get('context', 0))} tokens).", flush=True)
    if a.no_wait:
        print(f"Wait for it with: delegate.py wait {task['id'][:8]}  (in the background), or keep a watch running.")
        return 0
    return finish(ls, task, max(a.timeout - (time.time() - began), 1.0))


def cmd_series(a) -> int:
    """The pieces of a series: which conversation, when, and how each ended by its report."""
    name = valid_series(a.name)
    tasks = sorted((r for r in records() if r.get("series") == name and not r["id"].startswith("q-")),
                   key=lambda r: float(r.get("started") or 0))
    if not tasks:
        raise Failure(f"no series «{name}».")
    ls = discover()
    lines = [f"Series «{name}» ({len(tasks)} conversation{'s' if len(tasks) != 1 else ''}):"]
    for t in tasks:
        steps: List[dict] = []
        if agy_cli.is_cli(t):
            said = verdict_text(agy_cli.report_text(dict(t, turn=0)), 70)
            lines.append(f"  {t['id'][:8]} — {t.get('label') or t.get('model') or '?'} (CLI) · {t.get('state') or '?'}"
                         f" · {len(t.get('pieces') or [])} piece(s), the last → {said or '(no report yet)'}")
            continue
        if ls:
            try:
                steps = [x for x in rpc(ls, "GetCascadeTrajectorySteps", {"cascadeId": t["id"]}, timeout=60).get("steps")
                         or [] if isinstance(x, dict)]
            except SOFT:
                steps = []
        pieces = t.get("pieces") or [{"n": 1, "turn": 0, "at": t.get("started")}]
        lines.append(f"  {t['id'][:8]} — {t.get('label') or t.get('model') or '?'} · {t.get('state') or '?'}")
        for i, pc in enumerate(pieces):
            lo = int(pc.get("turn") or 0)
            hi = int(pieces[i + 1].get("turn") or 0) if i + 1 < len(pieces) else len(steps)
            status = ""
            for st in steps[lo:hi]:
                if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                    pr = st.get("plannerResponse") or {}
                    status = verdict_text(str(pr.get("modifiedResponse") or pr.get("response") or ""), 70) or status
            when = time.strftime("%H:%M", time.localtime(float(pc.get("at") or t.get("started") or 0)))
            brief = re.sub(r"\s+", " ", str(pc.get("brief") or "")).strip()
            lines.append(f"    #{pc.get('n')} {when}" + (f" «{brief[:60]}»" if brief else "")
                         + f" → {status or '(no report yet)'}")
    lines.append(f"Next piece: delegate.py next {name} \"…\" (same conversation; --fresh for a new one)")
    print("\n".join(lines))
    return 0


def cmd_cancel(a) -> int:
    task = task_for(a.id, follow=False)
    if task["id"].startswith("q-"):
        deadline, seen = time.time() + 120, [""]

        def take_off(rec: dict) -> bool:  # under the lock: a dispatcher can't start it between the look and the cancel
            seen[0] = str(rec.get("state") or "")
            if seen[0] in ("queued", "failed"):
                rec["state"] = "canceled"
                return True
            return False

        while True:
            if transact(task, take_off):
                print(f"Took Antigravity task {task['id']} off the queue — it never started.")
                return 0
            if seen[0] == "starting" and time.time() < deadline:
                time.sleep(1)  # it is being started this very moment: stop what it becomes
                continue
            break
        if seen[0] != "started" or not task.get("started_as"):
            print(f"Antigravity task {task['id']} isn't in the queue ({seen[0] or '?'}).")
            return 0
        print(f"Queued task {task['id']} has started as {str(task['started_as'])[:8]} — stopping that.", flush=True)
        task = task_for(str(task["started_as"]))
    if agy_cli.is_cli(task):
        was = agy_cli.cancel(task)
        print(f"Stopped Antigravity CLI task {task['id']}." if was else f"Antigravity CLI task {task['id']} wasn't running.")
        update(task, state="canceled", outbox=[])
        return 0
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
    every = [r for r in records() if r.get("state") != "started"]  # a queued task that started is listed as itself
    queue = sorted((r for r in every if r.get("state") in ("queued", "starting")), key=lambda r: float(r.get("queued") or 0))
    tasks = [r for r in every if r not in queue][:a.limit]
    if not tasks and not queue:
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
            return idle_state(ls, t, live.get(t["id"]) or {})
        except SOFT:
            return ""
    found: Dict[str, str] = {}
    if probe:
        with ThreadPoolExecutor(max_workers=min(len(probe), 8)) as pool:
            found = dict(zip([t["id"] for t in probe], pool.map(look, probe)))
    lines = []
    if queue:
        limit = max(int((q.get("params") or {}).get("max_parallel") or 0) for q in queue)
        lines.append(f"Queued (they start by themselves, oldest first; at most {limit} run at once):")
        for q in queue:
            age = span(time.time() - float(q.get("queued") or time.time())) + " ago"
            lines.append(f"  {q['id']}  {q.get('state'):10} {age:12} --model {q.get('model') or '?'}  "
                         f"{q.get('title') or ''}  [{q.get('cwd') or ''}]")
        lines.append("")
    lines.append("Recent Antigravity tasks (newest first):")
    for t in tasks:
        state = "running" if status[t["id"]].endswith(("RUNNING", "BUSY")) else t.get("state") or "?"
        if agy_cli.is_cli(t) and state in NOT_OVER:
            try:
                state = agy_cli.state_now(t)[0]
            except Exception:
                pass
            fields = recorded(t, state)
            if fields:
                update(t, **fields)
        elif open_questions(t) and state in NOT_OVER + ("done",):
            state = "question"  # it asked Claude Code something and waits for the answer
        elif found.get(t["id"]):
            state = found[t["id"]]  # done, background (something it started still runs) or running (not picked up)
            fields = recorded(t, state)
            if fields:
                update(t, **fields)
        elif state in ("running", "question") and status[t["id"]].endswith("IDLE"):
            state = "done"  # the language server couldn't say more
        age = span(time.time() - float(t.get("started") or t.get("queued") or time.time())) + " ago"
        extra = [f"resumed ×{len(t['resumes'])}"] if t.get("resumes") else []
        if t.get("series"):
            extra.append(f"series «{t['series']}» #{(t.get('pieces') or [{'n': 1}])[-1].get('n')}")
        if agy_cli.is_cli(t):
            extra.append("CLI")
        lines.append(f"  {t['id'][:8]}  {state:10} {age:12} {t.get('label') or t.get('model') or '?'}  "
                     f"{t.get('title') or ''}  [{t.get('cwd') or ''}]" + (f"  ({', '.join(extra)})" if extra else ""))
    print("\n".join(lines))
    return 0


def cmd_settings(a) -> int:
    """Show or change the bridge's own defaults."""
    changes = {k: v for k, v in (("max_parallel", a.max_parallel), ("retries", a.retries)) if v is not None}
    for key, v in changes.items():
        if not 0 <= v <= (50 if key == "max_parallel" else 10):
            raise Failure(f"--{key.replace('_', '-')} must be between 0 and {50 if key == 'max_parallel' else 10}.")
    if changes:
        try:
            with open(BRIDGE_SETTINGS, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = {}
        stored = dict(stored if isinstance(stored, dict) else {}, **changes)
        os.makedirs(TASKS, exist_ok=True)
        tmp = f"{BRIDGE_SETTINGS}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stored, f, indent=1)
        compat.replace(tmp, BRIDGE_SETTINGS)
    s = bridge_settings()
    print(("Saved. " if changes else "") + "The bridge's defaults (delegate.py settings; per task: run --max-parallel / "
          "--retries):")
    print(f"  max-parallel {s['max_parallel']}: " + (f"at most {s['max_parallel']} tasks run at once — more wait in a "
                                                     "queue and start by themselves" if s["max_parallel"] else
                                                     "no limit (0) — every run starts at once"))
    print(f"  retries {s['retries']}: " + (f"a task Antigravity stops with an error is resumed up to {s['retries']} "
                                           "time(s)" if s["retries"] else "no automatic resume (0)"))
    for var in ("AGP_DELEGATE_MAX_PARALLEL", "AGP_DELEGATE_RETRIES"):
        if os.environ.get(var):
            print(f"  ({var}={os.environ[var]} overrides the saved value)")
    return 0


def cmd_wake(a) -> int:
    """Start Antigravity if it isn't running, and wait until its language server answers and lists its models."""
    began = time.time()
    ls = connect(timeout=a.timeout)
    n = ready_models(ls, began + max(a.timeout, 1.0))
    if not n:
        raise Failure(f"Antigravity runs but lists no models after {span(time.time() - began)} — is it signed in? Ask "
                      "the user to look at it.")
    found = compat.language_server()
    version = f" {found.version}" if found and found.version else ""
    print(f"Antigravity{version} is ready: {n} models (took {span(time.time() - began)}).")
    return 0


def cmd_estimate(a) -> int:
    """What a task costs here, before handing it over: for the model --model picks now and for every tier."""
    ls, ms, default, groups, _sp = backend(a)
    now = time.time()
    recs = records()
    filled = backfill(ls, recs) if ls else 0
    if filled:
        recs = records()
    m, _notes, why = pick(a.model, ms, default, groups, now)
    est = estimate_of(m, groups, recs)
    lines = [f"--model {a.model} → {m['label']} ({why}):", f"  {estimate_text(est)}"]
    if est.get("share"):
        lines.append(f"  (quota per API dollar: {pct(est['rate'])} of the week, {est['rate_source']})")
    firsts = [int(c["first_call"]) for c in first_costs(m["label"], recs) if isinstance(c.get("first_call"), int)]
    if firsts:
        lines.append(f"  A new conversation's first call reads ≈{kilo(int(statistics.median(firsts)))} tokens here, none "
                     "from cache; a follow-up or the next piece of a series (run --series, next) reads it from the cache.")
    lines += ["", "Every tier now:"]
    for t in TIERS:
        try:
            tm, _n, _w = pick(t, ms, default, groups, now)
        except Failure:
            lines.append(f"  {t:9} nothing has quota left now")
            continue
        lines.append(f"  {t:9} {tm['label']}: {estimate_text(estimate_of(tm, groups, recs))}")
    lines += ["", "Quota left:"] + (quota_lines(groups, now) if ls else [CLI_QUOTA])
    if filled:
        lines.append(f"(Read the cost of {filled} earlier task{'s' if filled != 1 else ''} from Antigravity for this.)")
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
    p.add_argument("--via", choices=VIAS, help="Antigravity Desktop or the CLI (default: AGP_DELEGATE_VIA, else auto)")
    p = sub.add_parser("models", help="every model: quota left, price, measured speed; the tiers")
    p.add_argument("--json", action="store_true")
    p.add_argument("--via", choices=VIAS, help="Antigravity Desktop or the CLI (default: AGP_DELEGATE_VIA, else auto)")
    p = sub.add_parser("run", help="start a task (and wait for it)")
    p.add_argument("brief", nargs="?", help="the task; omit it (or '-') to read stdin")
    p.add_argument("--model", "-m", default="standard", help="tier (fast, standard, strong, ultra, claude), model id or label")
    p.add_argument("--cwd", help="the folder to work in (default: the current one)")
    p.add_argument("--title", default="", help="title of the conversation in Antigravity")
    p.add_argument("--brief-file", help="read the brief from this file")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait (default 1800)")
    p.add_argument("--no-wait", action="store_true", help="print the task id and return at once")
    p.add_argument("--template", action="append", metavar="FILE", help="a reference text for this kind of task, put "
                   "before the brief (repeatable)")
    p.add_argument("--series", metavar="NAME", help="the first piece of a series: later pieces go on in the same "
                                                    "conversation with `next NAME`")
    p.add_argument("--retries", type=int, help="resumes after Antigravity stops it with an error (default: settings, 2)")
    p.add_argument("--max-parallel", type=int, help="queue it while this many tasks run (default: settings; 0: no limit)")
    p.add_argument("--dry-run", action="store_true", help="show the model, why, and the expected cost; start nothing")
    p.add_argument("--via", choices=VIAS, help="where it runs: Antigravity Desktop, the Antigravity CLI (agy, "
                   "headless), or auto — Desktop when it answers, else the CLI (default: AGP_DELEGATE_VIA, else auto)")
    p.add_argument("--owner", help="the name of the agent this task belongs to: `watch --owner NAME` takes only its "
                                   "own (default: AGP_DELEGATE_OWNER)")
    p = sub.add_parser("estimate", help="what a task costs here on each tier, from your past tasks")
    p.add_argument("--model", "-m", default="standard", help="tier, model id or label (default standard)")
    p.add_argument("--via", choices=VIAS, help="Antigravity Desktop or the CLI (default: AGP_DELEGATE_VIA, else auto)")
    p = sub.add_parser("next", help="the next piece of a series, in the same conversation")
    p.add_argument("name")
    p.add_argument("message", nargs="*", help="the piece's brief ('-' or nothing: read stdin)")
    p.add_argument("--fresh", action="store_true", help="start a new conversation for it (the series' templates and "
                                                        "the last report go along)")
    p.add_argument("--model", "-m", help="for a fresh conversation: the model (default: the series' own)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("series", help="the pieces of a series and how each ended")
    p.add_argument("name")
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
    p = sub.add_parser("watch", help="for the Monitor tool: one numbered line per event (questions, messages, "
                                     "approvals, resumes, the end) until the tasks are over")
    p.add_argument("ids", nargs="*", help="task ids (default: the tasks this session started that aren't over)")
    p.add_argument("--follow", action="store_true", help="also the tasks this session starts later; run until --timeout")
    p.add_argument("--owner", help="only the tasks run with this --owner (default: AGP_DELEGATE_OWNER; without one, "
                                   "only tasks run without an owner)")
    p.add_argument("--timeout", type=float, default=WATCH_TIMEOUT, help="seconds before it stops (default 1780, just "
                                                                        "under the Monitor tool's limit; 0: no limit)")
    p = sub.add_parser("reply", help="answer the agent's question, or tell it something while it works")
    p.add_argument("id")
    p.add_argument("message", nargs="*", help="the text ('-' or nothing: read stdin)")
    for verb in TALK_KINDS:
        p = sub.add_parser(verb, help=("run by the Antigravity agent: " + ("a question for Claude Code" if verb == "ask"
                                                                           else "a message for Claude Code")))
        p.add_argument("text", nargs="*")
    p = sub.add_parser("cancel", help="stop a task, or take a queued one off the queue")
    p.add_argument("id")
    p = sub.add_parser("list", help="recent tasks, the queue first")
    p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("settings", help="the bridge's defaults: tasks at once (a queue for more), resumes after errors")
    p.add_argument("--max-parallel", type=int, help="tasks running at once; more wait in a queue (0: no limit)")
    p.add_argument("--retries", type=int, help="resumes per task after Antigravity stops it with an error (0: none)")
    p = sub.add_parser("wake", help="start Antigravity if it isn't running and wait until it is ready")
    p.add_argument("--timeout", type=float, default=START_TIMEOUT, help="seconds to wait (default 180)")
    sub.add_parser("statusline", help="as Claude Code's statusLine command: remember its rate_limits for budget")
    sub.add_parser("supervise", help="(starts by itself) start queued tasks, resume tasks stopped by an error")
    p = sub.add_parser("cli-turn", help=argparse.SUPPRESS)  # (started by itself) one turn of an Antigravity CLI task
    p.add_argument("id")
    p.add_argument("n", type=int)
    a, extra = ap.parse_known_args(argv)
    # Before Python 3.12, argparse doesn't give a text that follows an option to the positional before the option
    # (`next NAME --fresh "…"`, `send ID --no-wait "…"`): those words come back unparsed. They are the text.
    if extra and a.cmd in ("next", "reply", "send") and not any(x.startswith("-") for x in extra):
        if isinstance(a.message, list):
            a.message = list(a.message) + extra
        elif not a.message:
            a.message = " ".join(extra)
        else:
            ap.error(f"unrecognized arguments: {' '.join(extra)}")
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    handlers = {"budget": cmd_budget, "models": cmd_models, "run": cmd_run, "wait": cmd_wait, "result": cmd_result,
                "diff": cmd_diff, "send": cmd_send, "watch": cmd_watch, "reply": cmd_reply, "ask": cmd_talk,
                "tell": cmd_talk, "cancel": cmd_cancel, "list": cmd_list, "statusline": cmd_statusline,
                "estimate": cmd_estimate, "next": cmd_next, "series": cmd_series, "settings": cmd_settings,
                "wake": cmd_wake, "supervise": cmd_supervise,
                "cli-turn": lambda a: agy_cli.run_turn(a.id, a.n)}
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
