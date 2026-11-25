#!/usr/bin/env python3
"""Antigravity Plus — /ci: track CI runs and inspect failures (GitHub Actions via gh, GitLab CI via glab).

  ci.py detect [--workspace DIR] [--json]
  ci.py status [--workspace DIR] [--sha SHA] [--branch BR] [--pr N] [--json]
  ci.py watch  [--workspace DIR] [--sha SHA] [--branch BR] [--pr N] [--timeout 1800] [--appear-timeout 45] [--interval 6]
  ci.py logs   [RUN_ID] [--workspace DIR] [--job JOB] [--lines 120] [--full]
  ci.py rerun  [RUN_ID] [--workspace DIR] [--failed]

Also imported by hooks/session_start.py and hooks/stop.py (`repo_ci()`, `session_lines()` — pure local checks, no
network). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # noqa: E402

SKIP_CI = re.compile(r"\[(?:skip\s+ci|ci\s+skip|no\s+ci|skip\s+actions)\]", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
TS_PREFIX = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
ERROR_LINE = re.compile(
    r"\b(?:FAIL|FAILED|FAILURE|ERROR|AssertionError|SyntaxError|TypeError|ValueError|KeyError|"
    r"AttributeError|ImportError|ModuleNotFoundError|RuntimeError|NameError|OSError|ERR_PNPM|"
    r"cargo test failed|BUILD FAILED)\b"
    r"|Traceback \(most recent call last\)|Exception:|npm ERR!|\bfatal:|\bpanic:"
    r"|❌|##\[error\]|^E\s{2,}|^---\s*FAIL:", re.I
)
_SETUP_GROUP = re.compile(r"^(?:Run |Operating System|Runner Image|GITHUB_TOKEN|Preparing |Set up job)", re.I)
NOISE_PASS = re.compile(r"^\s*(?:PASS\b|✅\s|ok\s+\S+\s+\d|Downloading\s|Installing\s|Collecting\s|Using cached\s)")
OTHER_CI_FILES = (
    (".circleci/config.yml", "CircleCI"),
    ("Jenkinsfile", "Jenkins"),
    ("azure-pipelines.yml", "Azure Pipelines"),
    ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    (".drone.yml", "Drone CI"),
    (".woodpecker.yml", "Woodpecker CI"),
)
GH_RUN_FIELDS = (
    "attempt,conclusion,createdAt,databaseId,displayTitle,event,headBranch,"
    "headSha,name,startedAt,status,updatedAt,url,workflowName"
)
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
GLAB_RUNNING = {"created", "waiting_for_resource", "preparing", "pending", "running"}


# ─────────────────────────────── local repo & CLI detection (no network) ───────────────────────────────

def _run(cmd: List[str], cwd: Optional[str] = None, timeout: float = 15.0) -> Tuple[int, str, str]:
    try:
        exe = shutil.which(cmd[0]) if cmd else None
        argv = [exe, *cmd[1:]] if exe else cmd
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def git(args: List[str], cwd: str, timeout: float = 4.0) -> Optional[str]:
    code, out, _ = _run(["git", *args], cwd=cwd, timeout=timeout)
    return out.strip() if code == 0 else None


def _remote_host(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)", u, re.I)
    if m:
        return m.group(1).lower()
    m = re.match(r"^(?:[^@/\s]+@)?([A-Za-z0-9.-]+\.[A-Za-z]{2,}):", u)
    return m.group(1).lower() if m else ""


def _workflow_triggers_push_or_pr(path: str) -> bool:
    """True when a GitHub Actions workflow file can run on `push` or `pull_request` (not manual/schedule only)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(32_000)
    except OSError:
        return False
    # Strip comments (both full-line and inline `# ...`)
    lines = [re.sub(r"(?:^|\s)#.*$", "", ln) for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln.strip())
    # Inline `on: push` or `on: [push, pull_request]`
    m = re.search(r"^(?:['\"]?on['\"]?)[ \t]*:[ \t]*([^\n]+)$", cleaned, re.M)
    if m:
        rest = m.group(1).strip()
        if rest:
            return bool(re.search(r"\b(?:push|pull_request|pull_request_target)\b", rest))
    # Block `on:\n  push:` (or multi-event / sequence block)
    m = re.search(r"^(?:['\"]?on['\"]?)[ \t]*:[ \t]*\n((?:[ \t]+[^\n]*\n?)+)", cleaned, re.M)
    if m:
        block = m.group(1)
        return bool(re.search(r"^[ \t]+(?:-[ \t]+)?(?:push|pull_request|pull_request_target)(?:[ \t]*:|[ \t]*$)", block, re.M))
    return True  # unusual YAML layout: assume triggerable rather than silently missing CI


def _gh_authed(host: str = "github.com") -> bool:
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    cands = []
    if os.environ.get("GH_CONFIG_DIR"):
        cands.append(os.path.join(os.environ["GH_CONFIG_DIR"], "hosts.yml"))
    cands.append(os.path.expanduser("~/.config/gh/hosts.yml"))
    if os.environ.get("APPDATA"):
        cands.append(os.path.join(os.environ["APPDATA"], "GitHub CLI", "hosts.yml"))
    target = (host or "github.com").lower()
    for p in cands:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                raw = f.read(32_000).lower()
            if re.search(rf"^\s*{re.escape(target)}\s*:", raw, re.M):
                return True
        except OSError:
            continue
    return False


def _glab_authed(host: str = "gitlab.com") -> bool:
    if os.environ.get("GITLAB_TOKEN") or os.environ.get("GLAB_TOKEN"):
        return True
    cands = []
    if os.environ.get("GLAB_CONFIG_DIR"):
        cands.append(os.path.join(os.environ["GLAB_CONFIG_DIR"], "config.yml"))
    cands.extend([
        os.path.expanduser("~/.config/glab-cli/config.yml"),
        os.path.expanduser("~/Library/Application Support/glab-cli/config.yml"),
    ])
    if os.environ.get("APPDATA"):
        cands.append(os.path.join(os.environ["APPDATA"], "glab-cli", "config.yml"))
    target = (host or "gitlab.com").lower()
    for p in cands:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                raw = f.read(64_000).lower()
            if re.search(rf"^\s*{re.escape(target)}\s*:", raw, re.M) and re.search(
                r"^\s*(?:token\s*:\s*['\"]?[^\s'\"#]|use_keyring\s*:\s*true\b)", raw, re.M
            ):
                return True
        except OSError:
            continue
    return False


def repo_ci(ws: str) -> dict:
    """Fast local inspection (no network): CI workflows in the repo, remote provider, and whether CLI can track runs."""
    ws = os.path.abspath(os.path.expanduser(ws or os.getcwd()))
    root = git(["rev-parse", "--show-toplevel"], ws) if os.path.isdir(ws) else None
    if not root:
        return {"root": "", "provider": "", "host": "", "workflows": [], "triggerable": [], "other_ci": [],
                "cli": "", "cli_path": "", "authed": False, "ready": False, "reason": "not a git repository"}

    remote = git(["config", "--get", "remote.origin.url"], root) or ""
    if not remote:
        remotes = (git(["remote", "-v"], root) or "").splitlines()
        remote = remotes[0].split()[1] if remotes and len(remotes[0].split()) >= 2 else ""
    host = _remote_host(remote)

    gh_dir = os.path.join(root, ".github", "workflows")
    gh_files: List[str] = []
    gh_trig: List[str] = []
    if os.path.isdir(gh_dir):
        for fn in sorted(os.listdir(gh_dir)):
            full = os.path.join(gh_dir, fn)
            if fn.lower().endswith((".yml", ".yaml")) and os.path.isfile(full):
                gh_files.append(fn)
                if _workflow_triggers_push_or_pr(full):
                    gh_trig.append(fn)

    gl_files = [fn for fn in (".gitlab-ci.yml", ".gitlab-ci.yaml") if os.path.isfile(os.path.join(root, fn))]
    other = [label for rel, label in OTHER_CI_FILES if os.path.exists(os.path.join(root, rel))]

    provider = ""
    if "github" in host or (gh_files and not gl_files and not host):
        provider = "github"
    elif "gitlab" in host or (gl_files and "github" not in host):
        provider = "gitlab"
    elif gh_files:
        provider = "github"
    elif other:
        provider = "other"

    if provider == "github":
        cli, cli_path = "gh", shutil.which("gh") or ""
        authed = bool(cli_path) and _gh_authed(host or "github.com")
        workflows, triggerable = gh_files, gh_trig
    elif provider == "gitlab":
        cli, cli_path = "glab", shutil.which("glab") or ""
        authed = bool(cli_path) and _glab_authed(host or "gitlab.com")
        workflows, triggerable = gl_files, list(gl_files)
    else:
        cli, cli_path, authed, workflows, triggerable = "", "", False, gh_files + gl_files, []

    ready = bool(provider in ("github", "gitlab") and triggerable and cli_path and authed and remote)
    if ready:
        reason = "ready"
    elif not workflows and not other:
        reason = "no CI workflows found"
    elif provider == "other":
        reason = f"{', '.join(other)} config found (no built-in CLI tracker)"
    elif not remote:
        reason = "no git remote configured"
    elif not triggerable:
        reason = "workflows are manual/scheduled only (no push/pull_request trigger)"
    elif not cli_path:
        reason = f"`{cli}` CLI is not installed"
    elif not authed:
        reason = f"`{cli}` is not authenticated (`{cli} auth login`)"
    else:
        reason = "not ready"

    return {
        "root": root,
        "remote": remote,
        "host": host,
        "provider": provider,
        "workflows": workflows,
        "triggerable": triggerable,
        "other_ci": other,
        "cli": cli,
        "cli_path": cli_path,
        "authed": authed,
        "ready": ready,
        "reason": reason,
    }


def commit_skips_ci(root: str, sha: str = "HEAD") -> bool:
    msg = git(["log", "-1", "--format=%B", sha], root) or ""
    return bool(SKIP_CI.search(msg))


def session_lines(ws: str) -> List[str]:
    """Compact SessionStart lines when the workspace has CI workflows."""
    info = repo_ci(ws)
    if not info["workflows"] and not info["other_ci"]:
        return []
    label = ("GitHub Actions" if info["provider"] == "github"
             else "GitLab CI" if info["provider"] == "gitlab"
             else ", ".join(info["other_ci"]) or "CI")
    shown = ", ".join(info["workflows"][:4]) + (f", +{len(info['workflows']) - 4}" if len(info["workflows"]) > 4 else "")
    wf_part = f" ({shown})" if shown else ""
    me = os.path.abspath(__file__)
    py = compat.python_cmd()
    if info["ready"]:
        return [f'CI: {label}{wf_part} — after `git push` or `/pr`, track the run with '
                f'`{py} "{me}" watch --workspace "{ws}"` (BypassSandbox: true).']
    return [f"CI: {label}{wf_part} — {info['reason']}."]


# ─────────────────────────────── provider adapters (gh / glab) ───────────────────────────────

def _elapsed(started: str, ended: str = "") -> str:
    def parse(ts: str) -> Optional[float]:
        if not ts or ts.startswith("0001-01-01"):
            return None
        try:
            s = re.sub(r"(?<=:\d\d)\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], ts.replace("Z", "+00:00"))
            return dt.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None

    t0 = parse(started)
    if not t0:
        return ""
    t1 = parse(ended) or time.time()
    secs = max(0, int(t1 - t0))
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def _gh_runs(root: str, sha: str = "", branch: str = "", limit: int = 20) -> Tuple[Optional[List[dict]], str]:
    cmd = ["gh", "run", "list", "-L", str(limit), "--json", GH_RUN_FIELDS]
    if sha:
        cmd += ["-c", sha]
    elif branch:
        cmd += ["-b", branch]
    code, out, err = _run(cmd, cwd=root, timeout=25.0)
    if code != 0:
        return None, (err or out).strip() or f"gh run list exited with {code}"
    try:
        raw = json.loads(out or "[]")
    except ValueError as e:
        return None, f"invalid JSON from gh run list: {e}"
    # Keep the newest run per (workflow, event, headSha) so re-runs supersede older attempts
    seen = set()
    runs = []
    for r in raw:
        wf = r.get("workflowName") or r.get("name") or "workflow"
        key = (wf, r.get("event") or "", r.get("headSha") or "")
        if key in seen:
            continue
        seen.add(key)
        status = str(r.get("status") or "").lower()
        norm_status = "completed" if status == "completed" else ("queued" if status in ("queued", "waiting", "pending", "requested") else "in_progress")
        runs.append({
            "id": str(r.get("databaseId") or ""),
            "name": wf,
            "title": r.get("displayTitle") or "",
            "status": norm_status,
            "conclusion": str(r.get("conclusion") or "").lower(),
            "branch": r.get("headBranch") or "",
            "sha": r.get("headSha") or "",
            "event": r.get("event") or "",
            "url": r.get("url") or "",
            "elapsed": _elapsed(r.get("startedAt") or r.get("createdAt") or "", r.get("updatedAt") if norm_status == "completed" else ""),
            "jobs": [],
        })
    return runs, ""


def _gh_populate_jobs(root: str, run: dict) -> None:
    if not run.get("id"):
        return
    code, out, _ = _run(["gh", "run", "view", run["id"], "--json", "jobs,status,conclusion,url,workflowName,name"], cwd=root, timeout=25.0)
    if code != 0:
        return
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return
    if data.get("workflowName") or data.get("name"):
        run["name"] = data.get("workflowName") or data.get("name")
    jobs = []
    for j in data.get("jobs") or []:
        steps = [s.get("name") or "" for s in (j.get("steps") or []) if str(s.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled")]
        jobs.append({
            "id": str(j.get("databaseId") or ""),
            "name": j.get("name") or "job",
            "status": str(j.get("status") or "").lower(),
            "conclusion": str(j.get("conclusion") or "").lower(),
            "failed_steps": steps,
            "url": j.get("url") or "",
        })
    run["jobs"] = jobs


def _gh_pr_target(root: str, pr: str) -> Tuple[str, str, str]:
    """(head_sha, branch, error) for a GitHub PR number/branch/URL."""
    code, out, err = _run(["gh", "pr", "view", str(pr), "--json", "headRefOid,headRefName"], cwd=root, timeout=20.0)
    if code != 0:
        return "", "", (err or out).strip() or f"gh pr view {pr} failed"
    try:
        d = json.loads(out or "{}")
        return d.get("headRefOid") or "", d.get("headRefName") or "", ""
    except ValueError as e:
        return "", "", str(e)


def _glab_mr_target(root: str, mr: str) -> Tuple[str, str, str]:
    """(head_sha, branch, error) for a GitLab MR iid/branch/URL."""
    code, out, err = _run(["glab", "mr", "view", str(mr), "-F", "json"], cwd=root, timeout=20.0)
    if code != 0:
        return "", "", (err or out).strip() or f"glab mr view {mr} failed"
    try:
        d = json.loads(out or "{}")
        return d.get("sha") or "", d.get("source_branch") or "", ""
    except ValueError as e:
        return "", "", str(e)


def _glab_norm_status(st: str) -> Tuple[str, str]:
    s = (st or "").lower()
    if s in ("created", "waiting_for_resource", "preparing", "pending", "scheduled"):
        return "queued", ""
    if s == "running":
        return "in_progress", ""
    if s == "success":
        return "completed", "success"
    if s in ("failed", "failure"):
        return "completed", "failure"
    if s in ("canceled", "cancelled"):
        return "completed", "cancelled"
    if s in ("skipped", "manual"):
        return "completed", "skipped"
    return ("completed", s) if s else ("in_progress", "")


def _glab_runs(root: str, sha: str = "", branch: str = "", limit: int = 20) -> Tuple[Optional[List[dict]], str]:
    cmd = ["glab", "ci", "list", "-F", "json", "-P", str(limit)]
    if branch and not sha:
        cmd += ["-b", branch]
    code, out, err = _run(cmd, cwd=root, timeout=25.0)
    if code != 0:
        return None, (err or out).strip() or f"glab ci list exited with {code}"
    try:
        raw = json.loads(out or "[]")
    except ValueError as e:
        return None, f"invalid JSON from glab ci list: {e}"
    runs = []
    seen_keys = set()
    for p in raw if isinstance(raw, list) else []:
        psha = str(p.get("sha") or "")
        if sha and (not psha or (not psha.startswith(sha) and not sha.startswith(psha))):
            continue
        source = str(p.get("source") or "push")
        ref = str(p.get("ref") or "")
        key = (source, ref, psha)
        if sha and key in seen_keys:
            continue
        seen_keys.add(key)
        norm_st, concl = _glab_norm_status(str(p.get("status") or ""))
        runs.append({
            "id": str(p.get("id") or ""),
            "name": f"pipeline #{p.get('iid') or p.get('id')}",
            "title": "",
            "status": norm_st,
            "conclusion": concl,
            "branch": ref,
            "sha": psha,
            "event": source,
            "url": str(p.get("web_url") or ""),
            "elapsed": _elapsed(p.get("created_at") or "", p.get("updated_at") if norm_st == "completed" else ""),
            "jobs": [],
        })
    return runs, ""


def _glab_populate_jobs(root: str, run: dict) -> None:
    if not run.get("id"):
        return
    code, out, _ = _run(["glab", "ci", "get", "--pipeline-id", run["id"], "-F", "json"], cwd=root, timeout=25.0)
    if code != 0:
        return
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return
    jobs = []
    for j in data.get("jobs") or []:
        norm_st, concl = _glab_norm_status(str(j.get("status") or ""))
        reason = str(j.get("failure_reason") or "")
        jobs.append({
            "id": str(j.get("id") or ""),
            "name": f"{j.get('stage')}: {j.get('name')}" if j.get("stage") else (j.get("name") or "job"),
            "status": norm_st,
            "conclusion": concl,
            "failed_steps": [reason] if reason and concl == "failure" else [],
            "url": str(j.get("web_url") or ""),
        })
    run["jobs"] = jobs


def fetch_runs(info: dict, sha: str = "", branch: str = "", limit: int = 20) -> Tuple[Optional[List[dict]], str]:
    if info["provider"] == "github":
        return _gh_runs(info["root"], sha=sha, branch=branch, limit=limit)
    if info["provider"] == "gitlab":
        return _glab_runs(info["root"], sha=sha, branch=branch, limit=limit)
    return None, info["reason"]


def populate_jobs(info: dict, run: dict) -> None:
    if info["provider"] == "github":
        _gh_populate_jobs(info["root"], run)
    elif info["provider"] == "gitlab":
        _glab_populate_jobs(info["root"], run)


# ─────────────────────────────── log cleaning ───────────────────────────────

def _clean_line(s: str) -> str:
    s = ANSI.sub("", s.replace("\r", ""))
    s = TS_PREFIX.sub("", s)
    return s.rstrip()


def _trim_section(lines: List[str], budget: int) -> List[str]:
    """Keep error regions + tail within `budget` lines, collapsing stretches of passing/download noise."""
    if len(lines) <= budget:
        return lines
    keep = set()
    for i, ln in enumerate(lines):
        if ERROR_LINE.search(ln):
            for k in range(max(0, i - 4), min(len(lines), i + 12)):
                keep.add(k)
    tail_n = max(15, budget // 3)
    for k in range(max(0, len(lines) - tail_n), len(lines)):
        keep.add(k)
    if not keep:
        return [f"  … [{len(lines) - budget} earlier lines omitted] …"] + lines[-budget:]
    ordered = sorted(keep)
    if len(ordered) > budget:
        ordered = ordered[: budget - tail_n] + list(range(len(lines) - tail_n, len(lines)))
        ordered = sorted(set(ordered))
    out: List[str] = []
    prev = -1
    for idx in ordered:
        if idx - prev > 1:
            out.append(f"  … [{idx - prev - 1} lines omitted] …")
        out.append(lines[idx])
        prev = idx
    return out


def clean_logs(raw: str, max_lines: int = 120, max_chars: int = 8000, job_filter: str = "", full: bool = False) -> str:
    """Turn raw `gh run view --log-failed` or `glab ci trace` output into a compact, readable failure excerpt."""
    if not (raw or "").strip():
        return "(no failed step log output returned)"
    sections: List[Tuple[str, List[str]]] = []
    cur_header = ""
    cur_lines: List[str] = []
    in_setup_group = False
    filt = (job_filter or "").strip().lower()

    for raw_ln in raw.splitlines():
        parts = raw_ln.split("\t", 2)
        if len(parts) == 3 and TS_PREFIX.match(ANSI.sub("", parts[2])):
            job, step, body = parts[0].strip(), parts[1].strip(), _clean_line(parts[2])
            if filt and filt not in job.lower() and filt not in step.lower():
                continue
            header = f"{job} › {step}"
        else:
            header = "Log"
            body = _clean_line(raw_ln)

        if header != cur_header:
            if cur_lines:
                sections.append((cur_header, cur_lines))
            cur_header, cur_lines, in_setup_group = header, [], False

        if body.startswith("##[group]"):
            title = body[len("##[group]"):].strip()
            if title.startswith("Run ") or full or not _SETUP_GROUP.match(title):
                cur_lines.append(f"▶ {title}")
            in_setup_group = (not full) and bool(_SETUP_GROUP.match(title))
            continue
        if body.startswith("##[endgroup]"):
            in_setup_group = False
            continue
        if body.startswith("##[error]"):
            body = "ERROR: " + body[len("##[error]"):].strip()
        elif in_setup_group:
            continue
        if not full and NOISE_PASS.match(body) and cur_lines and cur_lines[-1].startswith("  … [passing"):
            continue
        if not full and NOISE_PASS.match(body):
            cur_lines.append("  … [passing/progress lines omitted] …")
            continue
        cur_lines.append(body)

    if cur_lines:
        sections.append((cur_header, cur_lines))
    if not sections:
        return f"(no failed log lines matched job filter {job_filter!r})" if filt else "(empty log)"

    if full:
        rendered = []
        for hdr, lns in sections:
            rendered.append(f"### {hdr}" if hdr != "Log" else "")
            rendered.extend(lns)
        return "\n".join(x for x in rendered if x is not None).strip()

    per_sec = max(25, max_lines // max(1, len(sections)))
    rendered = []
    for hdr, lns in sections:
        if hdr != "Log":
            rendered.append(f"### {hdr}")
        rendered.extend(_trim_section(lns, per_sec))
    text = "\n".join(rendered).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n… [log truncated — run with --full or --job <name> for more]"
    return text


def fetch_failed_logs(info: dict, run: dict, job_filter: str = "", lines: int = 120, full: bool = False) -> str:
    rid = run.get("id") or ""
    if not rid:
        return ""
    if info["provider"] == "github":
        cmd = ["gh", "run", "view", rid, "--log-failed"]
        if job_filter and job_filter.isdigit():
            cmd = ["gh", "run", "view", rid, "--job", job_filter, "--log-failed"]
        code, out, err = _run(cmd, cwd=info["root"], timeout=45.0)
        raw = out if out.strip() else err
        return clean_logs(raw, max_lines=lines, job_filter="" if job_filter.isdigit() else job_filter, full=full)
    if info["provider"] == "gitlab":
        if not run.get("jobs"):
            populate_jobs(info, run)
        failed_jobs = [j for j in run.get("jobs") or [] if j.get("conclusion") == "failure"]
        if job_filter:
            filt = job_filter.lower()
            failed_jobs = [j for j in (run.get("jobs") or []) if filt in j["id"].lower() or filt in j["name"].lower()]
        chunks = []
        for j in failed_jobs[:5]:
            code, out, err = _run(["glab", "ci", "trace", j["id"]], cwd=info["root"], timeout=45.0)
            if code != 0 or not out.strip():
                code, out, err = _run(["glab", "api", f"projects/:id/jobs/{j['id']}/trace"], cwd=info["root"], timeout=45.0)
            body = clean_logs(out or err, max_lines=max(30, lines // max(1, len(failed_jobs))), full=full)
            chunks.append(f"### {j['name']} (job #{j['id']})\n{body}")
        return "\n\n".join(chunks) if chunks else "(no failed job logs found)"
    return ""


# ─────────────────────────────── formatting & commands ───────────────────────────────

def _mark(run: dict) -> str:
    if run["status"] != "completed":
        return "⏳"
    return "✅" if run["conclusion"] in OK_CONCLUSIONS else "❌"


def format_runs(runs: List[dict], sha: str = "", branch: str = "") -> str:
    if not runs:
        target = f"commit {sha[:8]}" if sha else (f"branch `{branch}`" if branch else "HEAD")
        return f"ℹ️ No CI runs found for {target}."
    any_running = any(r["status"] != "completed" for r in runs)
    any_failed = any(r["status"] == "completed" and r["conclusion"] not in OK_CONCLUSIONS for r in runs)
    head_sha = (sha or runs[0].get("sha") or "")[:8]
    head_br = branch or runs[0].get("branch") or ""
    where = f"{head_sha}" + (f" ({head_br})" if head_br else "")
    if any_running:
        headline = f"⏳ CI running for {where}:"
    elif any_failed:
        headline = f"❌ CI failed for {where}:"
    else:
        headline = f"✅ CI passed for {where}:"

    lines = [headline]
    for r in runs:
        jobs = r.get("jobs") or []
        if jobs:
            ok_j = sum(1 for j in jobs if j["status"] == "completed" and j["conclusion"] in OK_CONCLUSIONS)
            bad_j = [j for j in jobs if j["status"] == "completed" and j["conclusion"] not in OK_CONCLUSIONS]
            j_summary = f" · {ok_j}/{len(jobs)} jobs passed"
        else:
            bad_j, j_summary = [], ""
        state = r["conclusion"] if r["status"] == "completed" else r["status"]
        dur = f" · {r['elapsed']}" if r.get("elapsed") else ""
        url = f" · {r['url']}" if r.get("url") else ""
        lines.append(f"- {_mark(r)} {r['name']} (#{r['id']}): {state}{j_summary}{dur}{url}")
        for j in bad_j[:8]:
            step_note = f" — step: {', '.join(j['failed_steps'][:2])}" if j.get("failed_steps") else ""
            lines.append(f"  - ❌ {j['name']}{step_note}")
    return "\n".join(lines)


def resolve_target(info: dict, sha: str = "", branch: str = "", pr: str = "") -> Tuple[str, str, str]:
    """Return (full_sha, branch, error)."""
    root = info["root"]
    if pr and info["provider"] == "github":
        return _gh_pr_target(root, pr)
    if pr and info["provider"] == "gitlab":
        return _glab_mr_target(root, pr)
    if sha:
        full = git(["rev-parse", "--verify", sha + "^{commit}"], root) or sha
        br = branch or git(["branch", "--show-current"], root) or ""
        return full, br, ""
    if branch:
        return "", branch, ""
    full = git(["rev-parse", "HEAD"], root) or ""
    br = git(["branch", "--show-current"], root) or ""
    return full, br, ""


def cmd_detect(ws: str, as_json: bool = False) -> int:
    info = repo_ci(ws)
    if as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if not info["root"]:
        print(f"Not a git repository: {ws}")
        return 1
    head = (git(["rev-parse", "--short", "HEAD"], info["root"]) or "?")
    br = git(["branch", "--show-current"], info["root"]) or "(detached)"
    print(f"Repository: {info['root']} (branch `{br}`, HEAD `{head}`)")
    print(f"Provider:   {info['provider'] or 'none'} ({info['host'] or 'no remote'})")
    print(f"Workflows:  {', '.join(info['workflows']) or 'none'}"
          + (f" (push/PR triggerable: {', '.join(info['triggerable'])})" if info["workflows"] else ""))
    if info["other_ci"]:
        print(f"Other CI:   {', '.join(info['other_ci'])}")
    print(f"CLI:        {info['cli'] or 'n/a'} ({'authenticated' if info['authed'] else info['reason']})")
    print(f"Ready:      {'yes' if info['ready'] else 'no — ' + info['reason']}")
    return 0


def cmd_status(ws: str, sha: str = "", branch: str = "", pr: str = "", limit: int = 10, as_json: bool = False) -> int:
    info = repo_ci(ws)
    if not info["ready"] and not (info["cli_path"] and info["authed"] and info["provider"] in ("github", "gitlab")):
        if as_json:
            print(json.dumps({"ready": False, "reason": info["reason"], "runs": []}, ensure_ascii=False, indent=2))
        else:
            print(f"ℹ️ CI cannot be queried here: {info['reason']}.")
        return 0 if not info["workflows"] else 2
    target_sha, target_br, err = resolve_target(info, sha, branch, pr)
    if err:
        print(f"❌ {err}", file=sys.stderr)
        return 2
    runs, err = fetch_runs(info, sha=target_sha, branch=target_br, limit=limit)
    if runs is None:
        print(f"❌ {err}", file=sys.stderr)
        return 2
    for r in runs[:6]:
        populate_jobs(info, r)
    if as_json:
        print(json.dumps({"sha": target_sha, "branch": target_br, "runs": runs}, ensure_ascii=False, indent=2))
        return 0
    print(format_runs(runs, sha=target_sha, branch=target_br))
    return 0


def cmd_watch(ws: str, sha: str = "", branch: str = "", pr: str = "", timeout: float = 1800.0,
              appear_timeout: float = 45.0, interval: float = 6.0, lines: int = 120) -> int:
    info = repo_ci(ws)
    if not info["workflows"]:
        print("ℹ️ No CI workflows in this repository.")
        return 0
    if not info["triggerable"] and not pr:
        print(f"ℹ️ Skipping CI watch: {info['reason']}.")
        return 0
    if not info["cli_path"] or not info["authed"]:
        print(f"⚠️ Cannot watch CI: {info['reason']}.", file=sys.stderr)
        return 2

    target_sha, target_br, err = resolve_target(info, sha, branch, pr)
    if err:
        print(f"❌ {err}", file=sys.stderr)
        return 2
    if target_sha and commit_skips_ci(info["root"], target_sha):
        print(f"ℹ️ Commit {target_sha[:8]} carries [skip ci] — no CI run expected.")
        return 0

    started = time.time()
    appear_deadline = started + max(0.0, appear_timeout)
    deadline = started + max(5.0, timeout)
    settled = False
    runs: List[dict] = []

    while True:
        fetched, err = fetch_runs(info, sha=target_sha, branch=target_br, limit=20)
        if fetched is None:
            print(f"❌ {err}", file=sys.stderr)
            return 2
        if target_sha:
            fetched = [
                r for r in fetched
                if (r.get("sha") or "") and (r["sha"].startswith(target_sha[:12]) or target_sha.startswith(r["sha"]))
            ]
        runs = fetched

        now = time.time()
        if not runs:
            if now >= appear_deadline or now >= deadline:
                where = f"commit {target_sha[:8]}" + (f" on `{target_br}`" if target_br else "")
                print(f"ℹ️ No CI run triggered for {where} within {int(appear_timeout)}s (path/branch filter or inactive workflow).")
                return 0
            time.sleep(min(3.0, max(0.2, interval / 2)))
            continue

        # Multi-workflow repos: wait briefly once after the first run registers so sibling workflows also show up
        if not settled and len(info["triggerable"]) > 1 and len(runs) < len(info["triggerable"]) and now < appear_deadline:
            settled = True
            time.sleep(min(4.0, max(0.2, interval)))
            continue
        settled = True

        if all(r["status"] == "completed" for r in runs):
            break
        if now >= deadline:
            for r in runs:
                populate_jobs(info, r)
            print(format_runs(runs, sha=target_sha, branch=target_br))
            print(f"⏳ Timed out after {int(timeout)}s while CI is still running.")
            return 2
        time.sleep(max(0.2, interval))

    for r in runs:
        populate_jobs(info, r)
    print(format_runs(runs, sha=target_sha, branch=target_br))

    failed = [r for r in runs if r["conclusion"] not in OK_CONCLUSIONS]
    if not failed:
        return 0

    for r in failed[:3]:
        log_txt = fetch_failed_logs(info, r, lines=lines)
        if log_txt:
            print(f"\n## Failed logs — {r['name']} (#{r['id']})\n{log_txt}")
    return 1


def cmd_logs(ws: str, run_id: str = "", job: str = "", lines: int = 120, full: bool = False) -> int:
    info = repo_ci(ws)
    if not info["cli_path"] or not info["authed"]:
        print(f"❌ Cannot fetch CI logs: {info['reason']}.", file=sys.stderr)
        return 2
    if not run_id:
        runs, err = fetch_runs(info, branch=git(["branch", "--show-current"], info["root"]) or "", limit=15)
        if runs is None:
            print(f"❌ {err}", file=sys.stderr)
            return 2
        failed = next((r for r in runs if r["status"] == "completed" and r["conclusion"] not in OK_CONCLUSIONS), None)
        if not failed:
            print("ℹ️ No failed CI run found on the current branch.")
            return 0
        target = failed
    else:
        target = {"id": str(run_id), "name": f"run #{run_id}", "jobs": []}
    populate_jobs(info, target)
    print(f"## Failed logs — {target['name']} (#{target['id']})")
    print(fetch_failed_logs(info, target, job_filter=job, lines=lines, full=full))
    return 0


def cmd_rerun(ws: str, run_id: str = "", failed_only: bool = True) -> int:
    info = repo_ci(ws)
    if not info["cli_path"] or not info["authed"]:
        print(f"❌ Cannot re-run CI: {info['reason']}.", file=sys.stderr)
        return 2
    if not run_id:
        target_sha = git(["rev-parse", "HEAD"], info["root"]) or ""
        runs, err = fetch_runs(info, sha=target_sha, limit=10)
        if runs is None:
            print(f"❌ {err}", file=sys.stderr)
            return 2
        failed = next((r for r in runs if r["status"] == "completed" and r["conclusion"] not in OK_CONCLUSIONS), None)
        if not failed:
            print("ℹ️ No failed CI run found for HEAD.")
            return 0
        run_id = failed["id"]
    if info["provider"] == "github":
        cmd = ["gh", "run", "rerun", str(run_id)] + (["--failed"] if failed_only else [])
    elif info["provider"] == "gitlab":
        cmd = ["glab", "api", "-X", "POST", f"projects/:id/pipelines/{run_id}/retry"]
    else:
        print(f"❌ Unsupported provider: {info['provider']}", file=sys.stderr)
        return 2
    code, out, err = _run(cmd, cwd=info["root"], timeout=30.0)
    if code != 0:
        print(f"❌ {(err or out).strip()}", file=sys.stderr)
        return 1
    print(f"🔄 Triggered re-run for #{run_id}. Track with: {compat.python_cmd()} \"{os.path.abspath(__file__)}\" watch --workspace \"{ws}\"")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("detect")
    sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("status")
    sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    sp.add_argument("--sha", default="")
    sp.add_argument("--branch", default="")
    sp.add_argument("--pr", default="")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("watch")
    sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    sp.add_argument("--sha", default="")
    sp.add_argument("--branch", default="")
    sp.add_argument("--pr", default="")
    sp.add_argument("--timeout", type=float, default=1800.0)
    sp.add_argument("--appear-timeout", type=float, default=45.0)
    sp.add_argument("--interval", type=float, default=6.0)
    sp.add_argument("--lines", type=int, default=120)

    sp = sub.add_parser("logs")
    sp.add_argument("run_id", nargs="?", default="")
    sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    sp.add_argument("--job", default="")
    sp.add_argument("--lines", type=int, default=120)
    sp.add_argument("--full", action="store_true")

    sp = sub.add_parser("rerun")
    sp.add_argument("run_id", nargs="?", default="")
    sp.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    sp.add_argument("--failed", action="store_true", default=True)
    sp.add_argument("--all", dest="failed", action="store_false")

    a = ap.parse_args(argv)
    ws = os.path.realpath(os.path.expanduser(a.workspace))
    if a.cmd == "detect":
        return cmd_detect(ws, as_json=a.json)
    if a.cmd == "status":
        return cmd_status(ws, sha=a.sha, branch=a.branch, pr=a.pr, limit=a.limit, as_json=a.json)
    if a.cmd == "watch":
        return cmd_watch(ws, sha=a.sha, branch=a.branch, pr=a.pr, timeout=a.timeout,
                         appear_timeout=a.appear_timeout, interval=a.interval, lines=a.lines)
    if a.cmd == "logs":
        return cmd_logs(ws, run_id=a.run_id, job=a.job, lines=a.lines, full=a.full)
    if a.cmd == "rerun":
        return cmd_rerun(ws, run_id=a.run_id, failed_only=a.failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
