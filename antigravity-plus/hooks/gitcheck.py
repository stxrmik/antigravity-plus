#!/usr/bin/env python3
"""Antigravity Plus — git and gh commands that would stall the agent, and git work left half done.

What Antigravity's terminal is (verified 2026-11-14 on Antigravity 2.29 by an agent's own command): stdin, stdout and
stderr are terminals, TERM=dumb, PAGER=cat, GIT_PAGER=cat and GIT_EDITOR=false. So:
  * a pager never blocks;
  * a git command that wants an editor fails at once («There was a problem with the editor 'false'») — and
    `git -c core.editor=…` can't change that, because GIT_EDITOR wins: only `GIT_EDITOR=true git …` does. Worst hit
    is `git rebase --continue` after a conflict, which then leaves the rebase stuck;
  * whatever asks at the keyboard waits forever: `git add -p` and the other -p / -i forms, gh's questions
    (`gh pr create` without a title and body, `gh pr merge` without a method, `gh auth login`, …).

  * interactive(cmd)  → (rule, reason) for the guard: refuse with the command that works (setting gitChecks);
  * unfinished(ws)    → a rebase, merge, cherry-pick, revert, am or bisect in progress, its conflicted files and
                        conflict markers left in changed files — for the Stop hook and the session context;
  * markers(text)     → the line numbers where complete conflict blocks start (hooks/checks.py).
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Dict, List, Optional, Tuple

import compat

EDITOR_SET = re.compile(r"(^|[\s;&|(])(\$env:)?GIT_EDITOR\s*=", re.I)
SEQUENCE_SET = re.compile(r"(^|[\s;&|(])(\$env:)?GIT_SEQUENCE_EDITOR\s*=|sequence\.editor=", re.I)
GIT_GLOBAL_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
PATCH = {"-p", "--patch"}
DOC_EXT = (".md", ".markdown", ".rst", ".txt", ".adoc", ".org")
START = re.compile(r"^<{7}(?: |$)")
BASE = re.compile(r"^\|{7}(?: |$)")
MID = re.compile(r"^={7}$")
END = re.compile(r"^>{7}(?: |$)")


def _segments(cmd: str) -> List[str]:
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\s*$", "", cmd, flags=re.S | re.M)  # heredoc bodies
    return [p.strip() for p in re.split(r"\|\||&&|;|\||\r?\n", cmd) if p.strip()]


def _tokens(segment: str) -> List[str]:
    if compat.WINDOWS:
        segment = segment.replace("\\", "/")
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("env", "command", "exec", "time", "nohup", "noglob")):
        toks = toks[1:]
    return toks


def _git_sub(toks: List[str]) -> Tuple[str, List[str]]:
    """The git subcommand and its arguments, past git's own options (-c x=y, -C dir, --no-pager …)."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in GIT_GLOBAL_WITH_VALUE else 1
    return (toks[i], toks[i + 1:]) if i < len(toks) else ("", [])


def _short(args: List[str], letter: str, flags: str) -> bool:
    """`-i`, or a cluster of value-less short options (`flags`) that holds it (`-fi`) — not `-Xignore-…`."""
    return any(re.fullmatch(f"-[{flags}]+", a) and letter in a for a in args)


def _has(args: List[str], *names: str) -> bool:
    return any(a == n or a.startswith(n + "=") for a in args for n in names)


def interactive(cmd: str) -> Optional[Tuple[str, str]]:
    """A git or gh command that would wait at the keyboard, or fail on Antigravity's GIT_EDITOR=false, with the
    form that works. None when it is fine."""
    editor = bool(EDITOR_SET.search(cmd))
    sequence = bool(SEQUENCE_SET.search(cmd))
    for seg in _segments(cmd):
        toks = _tokens(seg)
        if not toks:
            continue
        prog = os.path.basename(toks[0]).lower()
        if prog in ("git", "git.exe"):
            sub, args = _git_sub(toks)
            if sub in ("add", "checkout", "reset", "restore", "stash") and _has(args, *PATCH):
                return ("git-interactive-patch", f"`git {sub} -p` asks about every hunk at the keyboard, and nobody "
                        "answers in this terminal — it would hang. Name whole files instead; for part of a file, edit "
                        "a copy or stage a hand-made patch with `git apply --cached <file.patch>`.")
            if (sub == "add" and (_has(args, "--interactive") or "-i" in args)) or \
                    (sub == "clean" and (_has(args, "--interactive") or _short(args, "i", "fdxXniq"))):
                return ("git-interactive", f"`git {sub} -i` is an interactive menu; nobody answers it here. Name the "
                        "files instead (`git clean` itself needs the user: list with `git clean -n` and ask).")
            if sub in ("rebase", "cherry-pick", "revert", "merge") and "--continue" in args and not editor:
                return ("git-continue-editor", f"Here GIT_EDITOR=false, so `git {sub} --continue` fails as soon as git "
                        f"wants the commit message (`-c core.editor=…` doesn't help: the variable wins). Run "
                        f"`GIT_EDITOR=true git {sub} --continue` (PowerShell: `$env:GIT_EDITOR='true'; git {sub} "
                        "--continue`) — it keeps the message as it is.")
            if sub == "rebase" and (_has(args, "--interactive", "--edit-todo") or _short(args, "i", "iqvnfkrm")) \
                    and not sequence:
                return ("git-rebase-interactive", "`git rebase -i` needs an editor for its todo list (GIT_EDITOR=false "
                        "here). Squash the last N commits: `git reset --soft HEAD~N && git commit -m \"…\"`; fixups: "
                        "`git commit --fixup <sha>`, then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash "
                        "<base>`; reword the last one: `git commit --amend -m \"…\"` (not pushed yet).")
        elif prog in ("gh", "gh.exe"):
            sub = " ".join(t for t in toks[1:3] if not t.startswith("-"))
            args = toks[1:]
            body = _has(args, "--body", "-b", "--body-file", "-F")
            web = _has(args, "--web", "-w")
            if sub == "auth login":
                return ("gh-auth-login", "`gh auth login` waits for the user to finish in a browser. Ask the user to "
                        "run `gh auth login` in their own terminal; `gh auth status` checks it afterwards.")
            if web and sub in ("pr create", "issue create"):
                return ("gh-web", f"`gh {sub} --web` leaves the rest to a person in a browser. Use the command's "
                        "flags (`--title`, `--body-file`, …) and give the user the link it prints.")
            if sub in ("pr create", "issue create") and not (_has(args, "--fill", "--fill-first", "--fill-verbose")
                                                             or (_has(args, "--title", "-t") and body)):
                return ("gh-prompt", f"`gh {sub}` without a title and a body asks questions at the keyboard and "
                        f"hangs here. Give both: `gh {sub} --title \"…\" --body-file <file>`"
                        + (" (or `--fill` to take them from the commits)" if sub == "pr create" else "") + ".")
            if sub in ("pr comment", "issue comment") and not body:
                return ("gh-prompt", f"`gh {sub}` without `--body` opens an editor or asks for the text. Use "
                        f"`gh {sub} <number> --body-file <file>` (or `--body \"…\"`).")
            if sub == "pr review" and not (_has(args, "--approve", "-a", "--request-changes", "-r", "--comment", "-c")
                                           and (body or _has(args, "--approve", "-a"))):
                return ("gh-prompt", "`gh pr review` without a decision and a body asks at the keyboard. Use "
                        "`gh pr review <n> --comment --body-file <file>` (approving or requesting changes only on the "
                        "user's request).")
            if sub == "pr merge" and not _has(args, "--merge", "-m", "--squash", "-s", "--rebase", "-r", "--auto",
                                               "--disable-auto"):
                return ("gh-prompt", "`gh pr merge` without a method asks which one and hangs. Add `--squash`, "
                        "`--merge` or `--rebase` (the repo's usual one) — merging still needs the user's go-ahead.")
            if sub == "release create" and not _has(args, "--notes", "-n", "--notes-file", "-F", "--generate-notes",
                                                     "--notes-from-tag"):
                return ("gh-prompt", "`gh release create` without notes asks for them interactively. Add "
                        "`--notes-file <file>` or `--generate-notes` (and `--title`).")
            if sub == "repo create" and (len([t for t in toks[3:] if not t.startswith("-")]) == 0 or
                                         not _has(args, "--public", "--private", "--internal")):
                return ("gh-prompt", "`gh repo create` without a name and a visibility asks questions. Use "
                        "`gh repo create <name> --private --source . --remote origin` (public only if the user said so).")
            if sub == "repo fork" and not _has(args, "--clone", "--remote", "--default-branch-only"):
                return ("gh-prompt", "`gh repo fork` asks whether to clone the fork. Add `--clone=false` (or "
                        "`--remote` inside a clone).")
    return None


def git(args: List[str], cwd: str, timeout: float = 5.0) -> Optional[str]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def markers(text: str) -> List[int]:
    """Where complete conflict blocks (<<<<<<< … ======= … >>>>>>>) start, by line number."""
    found, start, mid = [], 0, False
    for n, line in enumerate(text.splitlines(), 1):
        if START.match(line):
            start, mid = n, False
        elif start and MID.match(line):
            mid = True
        elif start and mid and END.match(line):
            found.append(start)
            start, mid = 0, False
        elif start and BASE.match(line):
            continue
    return found


def file_markers(path: str) -> List[int]:
    if path.lower().endswith(DOC_EXT):
        return []
    try:
        if os.path.getsize(path) > 2_000_000:
            return []
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    if b"\0" in raw[:8000]:
        return []
    return markers(raw.decode("utf-8", errors="replace"))


def unfinished(ws: str) -> Optional[Dict]:
    """A git operation left half done in the repository of `ws`: {kind, text, continue, abort, conflicts: [paths],
    markers: [(path, first line)]} — or None."""
    top = (git(["rev-parse", "--show-toplevel"], ws) or "").strip()
    if not top:
        return None
    path = lambda name: (lambda p: os.path.join(top, p) if p and not os.path.isabs(p) else p)(
        (git(["rev-parse", "--git-path", name], top) or "").strip())
    op = None
    if os.path.isdir(path("rebase-merge")) or os.path.isdir(path("rebase-apply")):
        am = os.path.isfile(os.path.join(path("rebase-apply"), "applying"))
        kind = "am" if am else "rebase"
        op = {"kind": kind, "text": "applying patches (git am)" if am else "a rebase",
              "continue": f"GIT_EDITOR=true git {kind} --continue", "abort": f"git {kind} --abort"}
    else:
        for name, kind in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
            if os.path.isfile(path(name)):
                op = {"kind": kind, "text": f"a {kind}", "abort": f"git {kind} --abort",
                      "continue": "git commit --no-edit" if kind == "merge" else f"GIT_EDITOR=true git {kind} --continue"}
                break
        if not op and os.path.isfile(path("BISECT_LOG")):
            op = {"kind": "bisect", "text": "a bisect", "continue": "git bisect run <test command>",
                  "abort": "git bisect reset"}
    stuck = [p for p in (git(["diff", "--name-only", "--diff-filter=U", "-z"], top) or "").split("\0") if p]
    changed = set()
    for args in (["diff", "--name-only", "-z"], ["diff", "--name-only", "--cached", "-z"]):
        changed.update(p for p in (git(args, top) or "").split("\0") if p)
    left = []
    for rel in sorted(changed - set(stuck))[:60]:
        lines = file_markers(os.path.join(top, rel))
        if lines:
            left.append((rel, lines[0]))
    if not op and not stuck and not left:
        return None
    return dict(op or {"kind": "", "text": "", "continue": "", "abort": ""}, conflicts=stuck, markers=left, root=top)


def describe(state: Dict) -> str:
    """One paragraph for the agent: what is half done and the commands that finish or undo it."""
    parts = []
    if state.get("kind"):
        parts.append(f"{state['text'][0].upper() + state['text'][1:]} is still in progress in {state['root']}"
                     + (f", with conflicts in {', '.join(state['conflicts'][:6])}" if state["conflicts"] else "")
                     + (f". Resolve them, `git add` each file, then `{state['continue']}`" if state["conflicts"] and
                        state["kind"] != "bisect" else f". Finish it with `{state['continue']}`")
                     + f", or undo it with `{state['abort']}`.")
    elif state.get("conflicts"):
        parts.append(f"Files in {state['root']} are still conflicted: {', '.join(state['conflicts'][:6])}.")
    if state.get("markers"):
        parts.append("Conflict markers are still in " + ", ".join(f"{p} (line {n})" for p, n in state["markers"][:6])
                     + ": resolve them, or the file won't build or will ship the markers.")
    return " ".join(parts)
