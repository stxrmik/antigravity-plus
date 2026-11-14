#!/usr/bin/env python3
"""Antigravity Plus — /git: the repository's state in one look, conflicts, and a pull request's review threads.

  git_state.py status    [--path DIR] [--fetch] [--github]   branch, upstream and ahead/behind, an operation in
                                                             progress (rebase, merge, cherry-pick, revert, bisect),
                                                             conflicts, changes, stash, last commits, remotes; with
                                                             --github also gh's login and this branch's PR — and
                                                             the exact next commands for what it found
  git_state.py conflicts [--path DIR]                        every conflicted file: its blocks and line numbers,
                                                             which side is which, how to see each side
  git_state.py review    [PR] [--path DIR] [--all]           a PR's unresolved review threads (path:line, who,
                                                             what) and its latest comments, through gh

Every command runs without waiting for input (no editor, pager or password prompt) and prints a compact text for
the agent; exit 2 means "not a git repository". Remote URLs are printed without credentials.
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
from typing import Dict, List, Optional, Tuple

QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GH_PAGER": "cat", "GH_PROMPT_DISABLED": "1",
             "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GIT_EDITOR": "true", "LC_ALL": "C", "LANG": "C"}
SHOW = 8  # files listed per kind


def run(cmd: List[str], cwd: str, timeout: float = 15) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, env=dict(os.environ, **QUIET_ENV), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, "", str(e)


def git(args: List[str], cwd: str, timeout: float = 15) -> Optional[str]:
    code, out, _ = run(["git"] + args, cwd, timeout)
    return out if code == 0 else None


def safe_url(url: str) -> str:
    """https://user:token@host/x → https://host/x."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", url.strip())


def git_path(name: str, cwd: str) -> str:
    p = (git(["rev-parse", "--git-path", name], cwd) or "").strip()
    return os.path.join(cwd, p) if p and not os.path.isabs(p) else p


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def ref_name(sha: str, root: str) -> str:
    """A readable name for a commit: origin/main rather than remotes/origin/HEAD."""
    name = (git(["name-rev", "--name-only", "--always", "--exclude=*/HEAD", sha], root) or "").strip()
    return name[len("remotes/"):] if name.startswith("remotes/") else name


def in_progress(root: str) -> Optional[dict]:
    """The operation a previous command left unfinished, with its continue/abort commands."""
    rm, ra = git_path("rebase-merge", root), git_path("rebase-apply", root)
    if rm and os.path.isdir(rm):
        step, end = read(os.path.join(rm, "msgnum")), read(os.path.join(rm, "end"))
        onto = ref_name(read(os.path.join(rm, "onto")), root)
        return {"kind": "rebase", "text": "a rebase" + (f" onto {onto}" if onto else "")
                + (f" (commit {step} of {end})" if step and end else ""),
                "continue": "GIT_EDITOR=true git rebase --continue", "abort": "git rebase --abort",
                "skip": "git rebase --skip"}
    if ra and os.path.isdir(ra):
        am = os.path.isfile(os.path.join(ra, "applying"))
        return {"kind": "am" if am else "rebase", "text": "git am applying patches" if am else "a rebase",
                "continue": "GIT_EDITOR=true git am --continue" if am else "GIT_EDITOR=true git rebase --continue",
                "abort": "git am --abort" if am else "git rebase --abort"}
    for name, kind, label in (("MERGE_HEAD", "merge", "a merge"), ("CHERRY_PICK_HEAD", "cherry-pick", "a cherry-pick"),
                              ("REVERT_HEAD", "revert", "a revert")):
        p = git_path(name, root)
        if p and os.path.isfile(p):
            other = ref_name(read(p).split()[0] if read(p) else "HEAD", root)
            cont = ("git commit --no-edit" if kind == "merge" else f"GIT_EDITOR=true git {kind} --continue")
            return {"kind": kind, "text": label + (f" of {other}" if other and kind != "revert" else ""),
                    "continue": cont, "abort": f"git {kind} --abort"}
    bl = git_path("BISECT_LOG", root)
    if bl and os.path.isfile(bl):
        return {"kind": "bisect", "text": "a bisect", "continue": "git bisect run <test command>",
                "abort": "git bisect reset"}
    return None


def conflicted(root: str) -> List[str]:
    out = git(["diff", "--name-only", "--diff-filter=U", "-z"], root) or ""
    return [p for p in out.split("\0") if p]


def changes(root: str) -> Dict[str, List[str]]:
    out = git(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], root) or ""
    kinds: Dict[str, List[str]] = {"staged": [], "modified": [], "untracked": []}
    parts, i = out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x in "RC":
            i += 1  # the old name follows a rename
        if x == "?":
            kinds["untracked"].append(path)
            continue
        if x == "U" or y == "U" or (x, y) in (("A", "A"), ("D", "D")):
            continue  # conflicts are listed on their own
        if x not in " ?":
            kinds["staged"].append(path)
        if y not in " ?":
            kinds["modified"].append(path)
    return kinds


def listed(paths: List[str]) -> str:
    return ", ".join(paths[:SHOW]) + (f" … (+{len(paths) - SHOW})" if len(paths) > SHOW else "")


def default_branch(root: str) -> str:
    ref = (git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root) or "").strip()
    if ref:
        return ref.split("/", 1)[-1]
    for name in ("main", "master", "develop", "trunk"):
        if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root) is not None or \
                git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"], root) is not None:
            return name
    return ""


def cmd_status(a) -> int:
    root = repo_root(a.path)
    lines = [f"Repository: {root}"]
    if a.fetch:
        code, _, err = run(["git", "fetch", "-q", "--prune"], root, timeout=90)
        lines.append("Fetched from the remote." if code == 0 else
                     f"Fetch failed: {first_line(err)} (no network, or credentials needed — don't retry in a loop).")
    branch = (git(["symbolic-ref", "--quiet", "--short", "HEAD"], root) or "").strip()
    head = (git(["rev-parse", "--short", "HEAD"], root) or "").strip()
    base = default_branch(root)
    op = in_progress(root)
    nxt: List[str] = []
    if branch:
        up = (git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root) or "").strip()
        text = f"Branch: {branch}" + (f" (default branch: {base})" if base and base != branch else
                                      " (the default branch)" if base == branch else "")
        if up:
            counts = (git(["rev-list", "--left-right", "--count", f"{up}...HEAD"], root) or "0 0").split()
            behind, ahead = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
            text += f" · tracks {up}: " + ("in sync" if not (ahead or behind) else
                                            ", ".join(x for x in (f"{ahead} ahead" if ahead else "",
                                                                  f"{behind} behind" if behind else "") if x))
            if behind and ahead:
                nxt.append(f"Diverged from {up}: your own unpushed commits → `git rebase {up}`; commits others "
                           f"already have → `git merge --no-edit {up}`. Then push.")
            elif behind:
                nxt.append(f"Behind {up}: `git pull --ff-only -q` (commit or stash local changes first).")
            elif ahead:
                nxt.append("Ahead: `git push -q` when the user wants it pushed (/pr for a pull request).")
        else:
            text += " · no upstream" + ("" if not git(["remote"], root) else
                                        " — first push: `git push -q -u origin HEAD`")
        lines.append(text)
    elif op and op["kind"] in ("rebase", "am", "bisect"):
        lines.append(f"Detached HEAD at {head or '?'} (normal while {op['text']} runs)")
    else:
        lines.append(f"Detached HEAD at {head or '?'}")
        nxt.append("Detached HEAD: keep work made here with `git switch -c <new-branch>`, or go back with "
                   f"`git switch {base or '<branch>'}`.")
    stuck = conflicted(root)
    if op:
        lines.append(f"In progress: {op['text']}" + (f" — conflicts in {len(stuck)} file(s)" if stuck else ""))
        if op["kind"] == "bisect":
            nxt.insert(0, "Bisect running: `git bisect run <test command>` finds the commit; then `git bisect reset` "
                          "(never leave it running).")
        elif stuck:
            nxt.insert(0, f"Resolve the conflicts (`git_state.py conflicts`), `git add <file>` each, then "
                          f"`{op['continue']}`; or undo the whole {op['kind']} with `{op['abort']}`.")
        else:
            nxt.insert(0, f"No conflicts left: finish with `{op['continue']}` (or `{op['abort']}`).")
    elif stuck:
        lines.append(f"Conflicts in {len(stuck)} file(s) (from a stash pop or checkout)")
    if stuck:
        lines.append("  conflicted: " + listed(stuck))
    ch = changes(root)
    if any(ch.values()):
        lines.append("Changes: " + ", ".join(f"{len(v)} {k}" for k, v in ch.items() if v))
        for kind in ("staged", "modified", "untracked"):
            if ch[kind]:
                lines.append(f"  {kind}: {listed(ch[kind])}")
    else:
        lines.append("Changes: none (clean working tree)")
    stashes = [s for s in (git(["stash", "list"], root) or "").splitlines() if s.strip()]
    if stashes:
        lines.append(f"Stash: {len(stashes)} entr{'y' if len(stashes) == 1 else 'ies'} (newest: {stashes[0][:80]})")
    log = (git(["log", "--oneline", "--no-decorate", "-n", "5"], root) or "").strip()
    if log:
        lines.append("Last commits:\n" + "\n".join("  " + x[:100] for x in log.splitlines()))
    remotes = {}
    for line in (git(["remote", "-v"], root) or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = safe_url(parts[1])
    lines.append("Remotes: " + (", ".join(f"{k} → {v}" for k, v in remotes.items()) if remotes else "none"))
    if a.github:
        lines += github_lines(root, branch)
    if nxt:
        lines.append("Next:")
        lines += [f"  - {x}" for x in nxt]
    print("\n".join(lines))
    return 0


def github_lines(root: str, branch: str) -> List[str]:
    if not shutil.which("gh"):
        return ["GitHub: gh isn't installed (macOS: `brew install gh`; then the user runs `gh auth login`)."]
    code, _, err = run(["gh", "auth", "status"], root, timeout=20)
    if code != 0:
        return ["GitHub: gh isn't logged in — the user runs `gh auth login` (it opens a browser; never run it yourself)."]
    out = ["GitHub: gh is logged in"]
    if not branch:
        return out
    code, js, err = run(["gh", "pr", "view", "--json", "number,title,state,isDraft,url,reviewDecision,statusCheckRollup"],
                        root, timeout=30)
    if code != 0:
        out[-1] += " · no pull request for this branch" if "no pull requests found" in err.lower() or not err.strip() \
            else f" · PR lookup failed: {first_line(err)}"
        return out
    try:
        pr = json.loads(js)
    except ValueError:
        return out
    checks = pr.get("statusCheckRollup") or []
    def verdict(c: dict) -> str:
        v = str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        return "passed" if v in ("SUCCESS", "NEUTRAL", "SKIPPED") else "failed" if v in (
            "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") else "pending"
    tally = {}
    for c in checks:
        tally[verdict(c)] = tally.get(verdict(c), 0) + 1
    out.append(f"Pull request #{pr.get('number')}: {pr.get('title', '')[:80]} — {str(pr.get('state', '')).lower()}"
               + (" (draft)" if pr.get("isDraft") else "")
               + (f", review: {str(pr.get('reviewDecision')).lower().replace('_', ' ')}" if pr.get("reviewDecision") else "")
               + (", checks: " + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())) if tally else "")
               + f" · {pr.get('url', '')}")
    if tally.get("failed"):
        out.append("  failed checks → /ci (`ci.py logs`) shows why")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        out.append("  changes requested → `git_state.py review` lists the open threads")
    return out


def first_line(text: str) -> str:
    return next((x.strip() for x in (text or "").splitlines() if x.strip()), "?")[:200]


def repo_root(path: Optional[str]) -> str:
    where = os.path.abspath(path or os.getcwd())
    out = git(["rev-parse", "--show-toplevel"], where)
    if not out:
        print(f"Not a git repository: {where}. Don't run git commands here. If the user's project should have one, "
              "/git-init creates it.")
        sys.exit(2)
    return os.path.normpath(out.strip())


CONFLICT_START = re.compile(r"^<{7}(?: (.*))?$")
CONFLICT_MID = re.compile(r"^={7}$")
CONFLICT_END = re.compile(r"^>{7}(?: (.*))?$")


def blocks(path: str) -> Tuple[List[Tuple[int, int]], str, str]:
    """Conflict blocks (start, end line numbers) and the labels of the two sides."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().splitlines()
    except OSError:
        return [], "", ""
    found, start, ours, theirs = [], 0, "", ""
    for n, line in enumerate(text, 1):
        m = CONFLICT_START.match(line)
        if m:
            start, ours = n, ours or (m.group(1) or "")
        elif start and CONFLICT_END.match(line):
            theirs = theirs or (CONFLICT_END.match(line).group(1) or "")
            found.append((start, n))
            start = 0
    return found, ours, theirs


def cmd_conflicts(a) -> int:
    root = repo_root(a.path)
    stuck = conflicted(root)
    op = in_progress(root)
    if not stuck:
        print("No conflicted files." + (f" {op['text'].capitalize()} is in progress: finish it with `{op['continue']}`."
                                         if op and op["kind"] != "bisect" else ""))
        return 0
    codes = {}
    for line in (git(["status", "--porcelain=v1"], root) or "").splitlines():
        if len(line) > 3:
            codes[line[3:].strip('"')] = line[:2]
    meaning = {"UU": "both changed it", "AA": "both added it", "DU": "deleted on our side, changed on theirs",
               "UD": "changed on our side, deleted on theirs", "AU": "added by us", "UA": "added by them",
               "DD": "both deleted it"}
    lines = [f"{len(stuck)} conflicted file(s)" + (f" — {op['text']}" if op else "") + ":"]
    for path in stuck:
        found, ours, theirs = blocks(os.path.join(root, path))
        code = codes.get(path, "UU")
        desc = meaning.get(code, code)
        where = ", ".join(f"lines {s}–{e}" for s, e in found[:6]) + (" …" if len(found) > 6 else "")
        lines.append(f"  {path}: {desc}" + (f"; {len(found)} block(s): {where}" if found else
                                            "; no text markers (binary, or a delete/modify conflict)"))
    if op and op["kind"] == "rebase":
        lines.append("In a rebase, «ours» (HEAD, the upper half) is the branch you rebase onto, «theirs» (the lower "
                     "half) is your commit being replayed.")
    else:
        lines.append("«Ours» (the upper half, HEAD) is your current branch, «theirs» (the lower half) is what comes in.")
    if ours or theirs:
        lines.append(f"Labels: upper = {ours or 'HEAD'}, lower = {theirs or '?'}")
    lines += ["How to resolve each file:",
              "  1. Read the block(s) and why each side changed: `git log --oneline -3 -- <file>` on both sides, "
              "`git show :1:<file>` (common base), `:2:` (ours), `:3:` (theirs).",
              "  2. Edit the file so both intentions survive; delete every <<<<<<<, ======= and >>>>>>> line.",
              "  3. A whole side wins: `git checkout --ours <file>` or `--theirs <file>`. Lock files (package-lock, "
              "poetry.lock, Podfile.lock…): take one side, then regenerate them with the package manager.",
              "  4. Run the tests, `git add <file>`, then " + (f"`{op['continue']}`" if op else "commit") +
              (f" — or give up on the whole operation with `{op['abort']}`." if op else ".")]
    print("\n".join(lines))
    return 0


REVIEW_QUERY = """query($owner:String!,$name:String!,$num:Int!){repository(owner:$owner,name:$name){pullRequest(number:$num){
number title url state reviewDecision
reviewThreads(first:100){nodes{isResolved isOutdated path line originalLine comments(first:30){nodes{author{login} body url}}}}
comments(last:10){nodes{author{login} body url}}}}}"""


def cmd_review(a) -> int:
    root = repo_root(a.path)
    if not shutil.which("gh"):
        print("gh isn't installed: review threads come from GitHub through gh (`brew install gh`, then the user runs "
              "`gh auth login`).")
        return 1
    code, js, err = run(["gh", "repo", "view", "--json", "owner,name"], root, timeout=30)
    if code != 0:
        print(f"gh can't see this repository on GitHub: {first_line(err)}")
        return 1
    repo = json.loads(js)
    num = a.pr
    if not num:
        code, js, err = run(["gh", "pr", "view", "--json", "number"], root, timeout=30)
        if code != 0:
            print(f"No pull request for this branch ({first_line(err)}). Name one: `git_state.py review <number>`.")
            return 1
        num = str(json.loads(js)["number"])
    code, js, err = run(["gh", "api", "graphql", "-f", f"query={REVIEW_QUERY}", "-F", f"owner={repo['owner']['login']}",
                         "-F", f"name={repo['name']}", "-F", f"num={int(num)}"], root, timeout=45)
    if code != 0:
        print(f"GitHub didn't answer: {first_line(err)}")
        return 1
    pr = (((json.loads(js).get("data") or {}).get("repository") or {}).get("pullRequest")) or {}
    threads = [t for t in (pr.get("reviewThreads") or {}).get("nodes") or [] if a.all or not t.get("isResolved")]
    lines = [f"PR #{pr.get('number')}: {pr.get('title', '')} — {str(pr.get('state', '')).lower()}"
             + (f", review: {str(pr.get('reviewDecision')).lower().replace('_', ' ')}" if pr.get("reviewDecision") else "")
             + f" · {pr.get('url', '')}",
             f"{len(threads)} {'' if a.all else 'unresolved '}review thread(s):"]
    for t in threads:
        notes = (t.get("comments") or {}).get("nodes") or []
        line = t.get("line") or t.get("originalLine")
        lines.append(f"  {t.get('path')}:{line or '?'}" + (" (outdated)" if t.get("isOutdated") else "")
                     + (" (resolved)" if t.get("isResolved") else ""))
        for c in notes[:6]:
            body = re.sub(r"\s+", " ", str(c.get("body") or "")).strip()
            lines.append(f"    {((c.get('author') or {}).get('login') or '?')}: {body[:300]}")
        if notes:
            lines.append(f"    {notes[0].get('url', '')}")
    comments = (pr.get("comments") or {}).get("nodes") or []
    if comments:
        lines.append("Latest comments on the PR:")
        for c in comments[-5:]:
            body = re.sub(r"\s+", " ", str(c.get("body") or "")).strip()
            lines.append(f"  {((c.get('author') or {}).get('login') or '?')}: {body[:300]}")
    lines.append("Replying to, resolving or approving anything on GitHub is visible to others: only when the user asks.")
    print("\n".join(lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not shutil.which("git"):
        print("git isn't installed (macOS: `xcode-select --install`; Linux: the package manager; Windows: git-scm.com).")
        return 1
    ap = argparse.ArgumentParser(prog="git_state.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("status")
    p.add_argument("--path")
    p.add_argument("--fetch", action="store_true", help="fetch from the remote first (network)")
    p.add_argument("--github", action="store_true", help="also gh's login and this branch's pull request (network)")
    p = sub.add_parser("conflicts")
    p.add_argument("--path")
    p = sub.add_parser("review")
    p.add_argument("pr", nargs="?")
    p.add_argument("--path")
    p.add_argument("--all", action="store_true", help="resolved threads too")
    a = ap.parse_args(argv)
    if a.cmd is None:
        a = ap.parse_args(["status"])
    return {"status": cmd_status, "conflicts": cmd_conflicts, "review": cmd_review}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
