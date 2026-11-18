#!/usr/bin/env python3
"""Antigravity Plus — /pr-comments: inspect GitHub PR review threads cleanly and reply/resolve on request.

  pr_comments.py list    [--workspace DIR] [--pr NUMBER|URL] [--all] [--json]
  pr_comments.py reply   --thread PRRT_... (--body TEXT | --body-file PATH) [--hostname HOST] [--workspace DIR] [--json]
  pr_comments.py resolve --thread PRRT_... [--unresolve] [--hostname HOST] [--workspace DIR] [--json]

Uses `gh` non-interactively (GIT_TERMINAL_PROMPT=0, GH_PROMPT_DISABLED=1, stdin=DEVNULL) and GraphQL cursor
pagination (review threads and each thread's comments). On GitHub Enterprise the PR's host goes to `gh api
--hostname` (gh would ask github.com otherwise); `list` prints it for `reply` / `resolve --hostname`.
`list` is read-only; `reply` and `resolve` are outward GitHub actions and should only run when the user explicitly
asks (or under full access). Stdlib only, Python 3.8+; macOS, Linux and Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

QUIET_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GH_PROMPT_DISABLED": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GH_PAGER": "cat",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GIT_EDITOR": "true",
    "LC_ALL": "C",
    "LANG": "C",
}

PR_URL_RE = re.compile(r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
REPO_URL_RE = re.compile(r"^https?://([^/]+)/")
URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@?#<>'\"()]+@")
MAX_HUNK_LINES = 8
MAX_PAGES = 50

LIST_QUERY = (
    "query($owner: String!, $name: String!, $num: Int!, $cursor: String) { "
    "repository(owner: $owner, name: $name) { "
    "pullRequest(number: $num) { "
    "number title url state headRefName baseRefName reviewDecision "
    "reviewThreads(first: 100, after: $cursor) { "
    "pageInfo { hasNextPage endCursor } "
    "nodes { "
    "id isResolved isOutdated path line originalLine startLine diffSide "
    "comments(first: 50) { "
    "totalCount "
    "pageInfo { hasNextPage endCursor } "
    "nodes { databaseId author { login } body createdAt path diffHunk url } "
    "} "
    "} "
    "} "
    "reviews(last: 50) { "
    "totalCount "
    "nodes { id author { login } state body submittedAt url } "
    "} "
    "} "
    "} "
    "}"
)

THREAD_COMMENTS_QUERY = (
    "query($id: ID!, $cursor: String) { "
    "node(id: $id) { "
    "... on PullRequestReviewThread { "
    "comments(first: 100, after: $cursor) { "
    "pageInfo { hasNextPage endCursor } "
    "nodes { databaseId author { login } body createdAt path diffHunk url } "
    "} "
    "} "
    "} "
    "}"
)

REPLY_MUTATION = (
    "mutation($threadId: ID!, $body: String!) { "
    "addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) { "
    "comment { id databaseId url author { login } body createdAt } "
    "} "
    "}"
)

RESOLVE_MUTATION = (
    "mutation($threadId: ID!) { "
    "resolveReviewThread(input: {threadId: $threadId}) { "
    "thread { id isResolved } "
    "} "
    "}"
)

UNRESOLVE_MUTATION = (
    "mutation($threadId: ID!) { "
    "unresolveReviewThread(input: {threadId: $threadId}) { "
    "thread { id isResolved } "
    "} "
    "}"
)


def redact_urls(text: str) -> str:
    """Mask userinfo credentials in URLs (e.g. https://user:<token>@host -> https://***@host)."""
    return URL_CREDS_RE.sub(r"\1***@", text or "")


def trim_diff_hunk(hunk: str, max_lines: int = MAX_HUNK_LINES) -> Tuple[str, bool]:
    """Return (last max_lines of diffHunk with redacted URLs, was_trimmed)."""
    cleaned = (hunk or "").rstrip("\r\n")
    if not cleaned:
        return "", False
    lines = cleaned.splitlines()
    trimmed = len(lines) > max_lines
    if trimmed:
        lines = lines[-max_lines:]
    return redact_urls("\n".join(lines)), trimmed


def format_location(
    path: str,
    line: Optional[int],
    original_line: Optional[int],
    start_line: Optional[int],
) -> str:
    """Format file:line (falling back to originalLine when outdated)."""
    eff_path = path or "(file)"
    end_line = line if line is not None else original_line
    if start_line is not None and end_line is not None and start_line != end_line:
        return f"{eff_path}:{start_line}-{end_line}"
    if end_line is not None:
        return f"{eff_path}:{end_line}"
    return eff_path


def _extract_login(author: Any) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or "ghost")
    if isinstance(author, str) and author.strip():
        return author.strip()
    return "ghost"


def api_host(netloc: str) -> Optional[str]:
    """The `gh api --hostname` for a PR or repository URL's host; None for github.com (gh's own default)."""
    host = (netloc or "").rsplit("@", 1)[-1].strip().lower()
    return None if host in ("", "github.com", "www.github.com") else host


def run_gh(args: List[str], cwd: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    """Run `gh` non-interactively with DEVNULL stdin and UTF-8 pipe decoding."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return (
            127,
            "",
            "gh CLI is not installed (macOS: `brew install gh`; then the user runs `gh auth login`).",
        )
    try:
        r = subprocess.run(
            [gh_bin, *args],
            cwd=cwd,
            env=dict(os.environ, **QUIET_ENV),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, "", str(e)


def _graphql_error_text(payload: Optional[dict], code: int, out: str, err: str) -> str:
    if isinstance(payload, dict) and payload.get("errors"):
        msgs = []
        for item in payload["errors"]:
            if isinstance(item, dict) and item.get("message"):
                msgs.append(str(item["message"]))
            elif item:
                msgs.append(str(item))
        if msgs:
            return redact_urls("; ".join(msgs))
    detail = (err or out).strip()
    if detail:
        return redact_urls(detail)
    return f"gh api graphql failed with exit code {code}"


def gh_graphql(
    query: str,
    cwd: str,
    fields: Optional[Dict[str, str]] = None,
    typed_fields: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    hostname: Optional[str] = None,
) -> Tuple[Optional[dict], str]:
    """Execute a `gh api graphql` call (against `hostname` when given) and return (parsed_json, error_message)."""
    cmd = ["api", "graphql"] + (["--hostname", hostname] if hostname else []) + ["-f", f"query={query}"]
    for key, val in (fields or {}).items():
        cmd += ["-f", f"{key}={val}"]
    for key, val in (typed_fields or {}).items():
        cmd += ["-F", f"{key}={val}"]
    code, out, err = run_gh(cmd, cwd=cwd, timeout=timeout)
    payload: Optional[dict] = None
    if out.strip().startswith("{"):
        try:
            loaded = json.loads(out)
            if isinstance(loaded, dict):
                payload = loaded
        except ValueError:
            payload = None
    if code != 0:
        return None, _graphql_error_text(payload, code, out, err)
    if payload is None:
        return None, f"invalid JSON from gh api graphql: {redact_urls((out or err).strip()[:200])}"
    if payload.get("errors"):
        return None, _graphql_error_text(payload, code, out, err)
    return payload, ""


def _repo_owner_name(cwd: str) -> Tuple[str, str, Optional[str], str]:
    """Resolve (owner, name, api host or None, error) via `gh repo view --json owner,name,url`."""
    code, out, err = run_gh(["repo", "view", "--json", "owner,name,url"], cwd=cwd, timeout=25.0)
    if code != 0:
        detail = redact_urls((err or out).strip())
        return "", "", None, detail or f"gh repo view failed with exit code {code}"
    try:
        data = json.loads(out or "{}")
    except ValueError as e:
        return "", "", None, f"invalid JSON from gh repo view: {e}"
    if not isinstance(data, dict):
        return "", "", None, "invalid JSON from gh repo view: not an object"
    owner = _extract_login(data.get("owner")) if data.get("owner") else ""
    name = str(data.get("name") or "").strip()
    if not owner or not name:
        return "", "", None, "could not determine repository owner/name from gh repo view"
    m_host = REPO_URL_RE.match(str(data.get("url") or ""))
    return owner, name, api_host(m_host.group(1)) if m_host else None, ""


def resolve_pr_target(cwd: str, pr_arg: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve PR number, owner, repo, and basic metadata."""
    raw = (pr_arg or "").strip()
    if raw:
        m = PR_URL_RE.match(raw)
        if m:
            return {
                "owner": m.group(2),
                "repo": m.group(3),
                "number": int(m.group(4)),
                "url": redact_urls(raw),
                "hostname": api_host(m.group(1)),
            }, ""
        num_str = raw[1:].strip() if raw.startswith("#") else raw
        try:
            num = int(num_str)
        except ValueError:
            return None, f"Invalid PR number: {pr_arg!r} (expected a positive integer or PR URL)."
        if num <= 0:
            return None, f"Invalid PR number: {pr_arg!r} (must be positive)."
        owner, repo, host, repo_err = _repo_owner_name(cwd)
        if owner and repo:
            return {"owner": owner, "repo": repo, "number": num, "hostname": host}, ""
        # Fallback: try `gh pr view <num>` in case PR URL resolves owner/repo
        code, out, err = run_gh(
            ["pr", "view", str(num), "--json", "number,url,title,headRefName,baseRefName"],
            cwd=cwd,
            timeout=25.0,
        )
        if code == 0:
            try:
                data = json.loads(out or "{}")
            except ValueError:
                data = {}
            url = str(data.get("url") or "") if isinstance(data, dict) else ""
            m_url = PR_URL_RE.match(url)
            if m_url:
                return {
                    "owner": m_url.group(2),
                    "repo": m_url.group(3),
                    "number": int(data.get("number") or num),
                    "url": redact_urls(url),
                    "hostname": api_host(m_url.group(1)),
                    "title": str(data.get("title") or ""),
                    "headRefName": str(data.get("headRefName") or ""),
                    "baseRefName": str(data.get("baseRefName") or ""),
                }, ""
        return None, repo_err or redact_urls((err or out).strip()) or "failed to resolve repository for PR"

    code, out, err = run_gh(
        ["pr", "view", "--json", "number,url,title,headRefName,baseRefName"],
        cwd=cwd,
        timeout=25.0,
    )
    if code != 0:
        detail = redact_urls((err or out).strip())
        if not detail or "no pull requests found" in detail.lower():
            return None, "No pull request found for the current branch (pass --pr NUMBER)."
        return None, f"gh pr view failed: {detail}"
    try:
        data = json.loads(out or "{}")
    except ValueError as e:
        return None, f"invalid JSON from gh pr view: {e}"
    num_val = data.get("number")
    if isinstance(num_val, bool) or not isinstance(num_val, int) or num_val <= 0:
        return None, "Could not determine PR number from gh pr view."
    url = str(data.get("url") or "")
    m = PR_URL_RE.match(url)
    if m:
        owner, repo, host = m.group(2), m.group(3), api_host(m.group(1))
    else:
        owner, repo, host, repo_err = _repo_owner_name(cwd)
        if not owner or not repo:
            return None, repo_err
    return {
        "owner": owner,
        "repo": repo,
        "number": num_val,
        "url": redact_urls(url),
        "hostname": host,
        "title": str(data.get("title") or ""),
        "headRefName": str(data.get("headRefName") or ""),
        "baseRefName": str(data.get("baseRefName") or ""),
    }, ""


def _normalize_thread(raw: dict, index: int) -> Dict[str, Any]:
    raw_comments = ((raw.get("comments") or {}).get("nodes") or [])
    comments: List[Dict[str, Any]] = []
    thread_path = str(raw.get("path") or "")
    hunk_source = str(raw.get("diffHunk") or "")
    for item in raw_comments:
        if not isinstance(item, dict):
            continue
        if not hunk_source and item.get("diffHunk"):
            hunk_source = str(item.get("diffHunk") or "")
        c_path = str(item.get("path") or thread_path)
        if not thread_path and c_path:
            thread_path = c_path
        comments.append({
            "databaseId": item.get("databaseId"),
            "author": _extract_login(item.get("author")),
            "body": redact_urls(str(item.get("body") or "").strip()),
            "createdAt": str(item.get("createdAt") or ""),
            "path": c_path,
            "url": redact_urls(str(item.get("url") or "")),
        })
    diff_hunk, diff_hunk_trimmed = trim_diff_hunk(hunk_source)
    total = (raw.get("comments") or {}).get("totalCount")
    total_n = total if isinstance(total, int) and not isinstance(total, bool) else len(comments)
    return {
        "index": index,
        "id": str(raw.get("id") or ""),
        "isResolved": bool(raw.get("isResolved")),
        "isOutdated": bool(raw.get("isOutdated")),
        "path": thread_path,
        "line": raw.get("line"),
        "originalLine": raw.get("originalLine"),
        "startLine": raw.get("startLine"),
        "diffSide": raw.get("diffSide"),
        "diffHunk": diff_hunk,
        "diffHunkTrimmed": diff_hunk_trimmed,
        "comments": comments,
        "commentsTotal": max(total_n, len(comments)),
        "commentsTruncated": total_n > len(comments),
        "commentsError": redact_urls(str(raw.get("_comments_error") or "")),
    }


def _fetch_more_comments(cwd: str, thread: dict, hostname: Optional[str]) -> None:
    """A thread with more comments than the first page holds: fetch the rest in place (`_comments_error` on failure)."""
    block = thread.get("comments") if isinstance(thread.get("comments"), dict) else {}
    nodes = [n for n in block.get("nodes") or [] if isinstance(n, dict)]
    page_info = block.get("pageInfo") or {}
    cursor = page_info.get("endCursor")
    seen: set = set()
    error = ""
    while page_info.get("hasNextPage") and cursor and cursor not in seen and len(seen) < MAX_PAGES and thread.get("id"):
        seen.add(cursor)
        payload, err = gh_graphql(
            THREAD_COMMENTS_QUERY,
            cwd=cwd,
            fields={"id": str(thread["id"]), "cursor": str(cursor)},
            timeout=35.0,
            hostname=hostname,
        )
        if payload is None:
            error = err
            break
        node = (payload.get("data") or {}).get("node")
        more = (node.get("comments") if isinstance(node, dict) else None) or {}
        nodes.extend(n for n in more.get("nodes") or [] if isinstance(n, dict))
        page_info = more.get("pageInfo") or {}
        cursor = page_info.get("endCursor")
    thread["comments"] = dict(block, nodes=nodes)
    if error:
        thread["_comments_error"] = error


def _normalize_reviews(raw_reviews: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in raw_reviews:
        if not isinstance(item, dict):
            continue
        body = redact_urls(str(item.get("body") or "").strip())
        if not body:
            continue
        out.append({
            "id": str(item.get("id") or ""),
            "author": _extract_login(item.get("author")),
            "state": str(item.get("state") or ""),
            "body": body,
            "submittedAt": str(item.get("submittedAt") or ""),
            "url": redact_urls(str(item.get("url") or "")),
        })
    return out


def fetch_pr_feedback(
    cwd: str,
    target: Dict[str, Any],
    include_all: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fetch review threads (paginated) and non-empty top-level PR reviews."""
    cursor: Optional[str] = None
    seen_cursors: set = set()
    pr_info: Dict[str, Any] = dict(target)
    raw_threads: List[dict] = []
    raw_reviews: List[Any] = []

    for page in range(MAX_PAGES):
        fields: Dict[str, str] = {"owner": target["owner"], "name": target["repo"]}
        if cursor is not None:
            fields["cursor"] = cursor
        payload, err = gh_graphql(
            LIST_QUERY,
            cwd=cwd,
            fields=fields,
            typed_fields={"num": target["number"]},
            timeout=35.0,
            hostname=target.get("hostname"),
        )
        if payload is None:
            return None, err
        repo_node = (payload.get("data") or {}).get("repository")
        if not isinstance(repo_node, dict):
            return None, f"Repository {target['owner']}/{target['repo']} not found."
        pr_node = repo_node.get("pullRequest")
        if not isinstance(pr_node, dict):
            return None, f"Pull request #{target['number']} not found in {target['owner']}/{target['repo']}."
        if page == 0:
            for key in ("number", "title", "url", "state", "headRefName", "baseRefName", "reviewDecision"):
                val = pr_node.get(key)
                if val is not None and val != "":
                    pr_info[key] = redact_urls(val) if key == "url" and isinstance(val, str) else val
            raw_reviews = (pr_node.get("reviews") or {}).get("nodes") or []

        rt = pr_node.get("reviewThreads") or {}
        nodes = rt.get("nodes") or []
        raw_threads.extend(n for n in nodes if isinstance(n, dict))

        page_info = rt.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = str(next_cursor)

    total_threads = len(raw_threads)
    resolved_count = sum(1 for t in raw_threads if t.get("isResolved"))
    unresolved_count = total_threads - resolved_count
    selected = [t for t in raw_threads if include_all or not t.get("isResolved")]
    for t in selected:
        if ((t.get("comments") or {}).get("pageInfo") or {}).get("hasNextPage"):
            _fetch_more_comments(cwd, t, target.get("hostname"))
    threads = [_normalize_thread(t, idx) for idx, t in enumerate(selected, 1)]
    reviews = _normalize_reviews(raw_reviews)

    return {
        "pull_request": pr_info,
        "include_resolved": include_all,
        "total_threads": total_threads,
        "unresolved_count": unresolved_count,
        "resolved_count": resolved_count,
        "threads": threads,
        "reviews": reviews,
    }, ""


def format_feedback(result: Dict[str, Any]) -> str:
    """Render PR threads and reviews as compact human-readable text."""
    pr = result["pull_request"]
    title = pr.get("title") or ""
    state = str(pr.get("state") or "").lower()
    head = pr.get("headRefName") or ""
    base = pr.get("baseRefName") or ""
    meta_parts = [p for p in (state, f"{head} -> {base}" if head and base else head) if p]
    meta = f" ({' · '.join(meta_parts)})" if meta_parts else ""
    header = f"PR #{pr.get('number')}: {title}{meta}".rstrip()
    lines = [header]
    if pr.get("url"):
        lines.append(f"URL: {pr['url']}")
    if pr.get("hostname"):
        lines.append(f"Host: {pr['hostname']} (pass --hostname {pr['hostname']} to reply / resolve)")

    total = result["total_threads"]
    unresolved = result["unresolved_count"]
    resolved = result["resolved_count"]
    include_all = result["include_resolved"]
    threads = result["threads"]

    if total == 0:
        lines.append("Review threads: none.")
    elif unresolved == 0 and not include_all:
        lines.append(
            f"Review threads: 0 unresolved ({total} total, {resolved} resolved) — pass --all to show resolved threads."
        )
    else:
        suffix = " (showing all)" if include_all and resolved > 0 else ""
        lines.append(f"Review threads: {unresolved} unresolved ({total} total, {resolved} resolved){suffix}")

    for t in threads:
        lines.append("")
        badges = []
        if t["isOutdated"]:
            badges.append("[OUTDATED]")
        if t["isResolved"]:
            badges.append("[RESOLVED]")
        loc = format_location(t["path"], t["line"], t["originalLine"], t["startLine"])
        badge_str = f"  {' '.join(badges)}" if badges else ""
        lines.append(f"[{t['index']}] {t['id']}  {loc}{badge_str}")
        if t["diffHunk"]:
            hunk_label = f"Diff hunk (last {MAX_HUNK_LINES} lines):" if t["diffHunkTrimmed"] else "Diff hunk:"
            lines.append(f"  {hunk_label}")
            for hline in t["diffHunk"].splitlines():
                lines.append(f"    {hline}")
        for c in t["comments"]:
            ts = f" ({c['createdAt']})" if c["createdAt"] else ""
            lines.append(f"  @{c['author']}{ts}:")
            body_lines = c["body"].splitlines() if c["body"] else ["(empty)"]
            for bline in body_lines:
                lines.append(f"    {bline}")
        if t.get("commentsTruncated"):
            why = f": {t['commentsError']}" if t.get("commentsError") else ""
            lines.append(f"  [TRUNCATED] {len(t['comments'])} of {t['commentsTotal']} comments shown{why}")

    reviews = result["reviews"]
    if reviews:
        lines.append("")
        lines.append(f"Top-level PR reviews ({len(reviews)}):")
        for r in reviews:
            state_badge = f" [{r['state']}]" if r["state"] else ""
            ts = f" ({r['submittedAt']})" if r["submittedAt"] else ""
            lines.append(f"- @{r['author']}{state_badge}{ts}:")
            for bline in r["body"].splitlines():
                lines.append(f"  {bline}")

    return "\n".join(lines)


def _check_workspace(ws: str) -> Optional[str]:
    if not os.path.isdir(ws):
        return f"Workspace directory does not exist: {ws}"
    return None


def cmd_list(ws: str, pr: str = "", include_all: bool = False, as_json: bool = False) -> int:
    ws_err = _check_workspace(ws)
    if ws_err:
        print(ws_err, file=sys.stderr)
        return 1
    target, err = resolve_pr_target(ws, pr_arg=pr)
    if target is None:
        print(err, file=sys.stderr)
        return 1
    result, err = fetch_pr_feedback(ws, target, include_all=include_all)
    if result is None:
        print(err, file=sys.stderr)
        return 1
    if as_json:
        out = dict(result)
        # Keep JSON clean without internal formatting flags
        out["threads"] = [
            {k: v for k, v in t.items() if k != "diffHunkTrimmed"}
            for t in result["threads"]
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(format_feedback(result))
    return 0


def cmd_reply(
    ws: str,
    thread_id: str,
    body: Optional[str] = None,
    body_file: Optional[str] = None,
    as_json: bool = False,
    hostname: Optional[str] = None,
) -> int:
    ws_err = _check_workspace(ws)
    if ws_err:
        print(ws_err, file=sys.stderr)
        return 1
    tid = (thread_id or "").strip()
    if not tid:
        print("Thread ID cannot be empty.", file=sys.stderr)
        return 1
    if body_file is not None:
        path = os.path.abspath(os.path.expanduser(body_file))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            print(f"Failed to read --body-file {path}: {e}", file=sys.stderr)
            return 1
    else:
        text = body or ""
    if not text.strip():
        print("Reply body cannot be empty.", file=sys.stderr)
        return 1

    payload, err = gh_graphql(
        REPLY_MUTATION,
        cwd=ws,
        fields={"threadId": tid, "body": text},
        timeout=30.0,
        hostname=api_host(hostname or ""),
    )
    if payload is None:
        print(err, file=sys.stderr)
        return 1
    comment = (((payload.get("data") or {}).get("addPullRequestReviewThreadReply") or {}).get("comment")) or {}
    if as_json:
        clean_comment = dict(comment)
        if clean_comment.get("url"):
            clean_comment["url"] = redact_urls(str(clean_comment["url"]))
        if clean_comment.get("body"):
            clean_comment["body"] = redact_urls(str(clean_comment["body"]))
        print(json.dumps({"threadId": tid, "comment": clean_comment}, indent=2, ensure_ascii=False))
    else:
        url = redact_urls(str(comment.get("url") or ""))
        suffix = f" ({url})" if url else ""
        print(f"Replied to thread {tid}{suffix}.")
    return 0


def cmd_resolve(
    ws: str,
    thread_id: str,
    unresolve: bool = False,
    as_json: bool = False,
    hostname: Optional[str] = None,
) -> int:
    ws_err = _check_workspace(ws)
    if ws_err:
        print(ws_err, file=sys.stderr)
        return 1
    tid = (thread_id or "").strip()
    if not tid:
        print("Thread ID cannot be empty.", file=sys.stderr)
        return 1
    mutation = UNRESOLVE_MUTATION if unresolve else RESOLVE_MUTATION
    field_name = "unresolveReviewThread" if unresolve else "resolveReviewThread"
    payload, err = gh_graphql(
        mutation,
        cwd=ws,
        fields={"threadId": tid},
        timeout=30.0,
        hostname=api_host(hostname or ""),
    )
    if payload is None:
        print(err, file=sys.stderr)
        return 1
    thread = (((payload.get("data") or {}).get(field_name) or {}).get("thread")) or {}
    resolved_id = str(thread.get("id") or tid)
    is_resolved = thread.get("isResolved")
    if as_json:
        print(json.dumps({"threadId": resolved_id, "isResolved": is_resolved}, indent=2, ensure_ascii=False))
    else:
        verb = "Unresolved" if unresolve else "Resolved"
        print(f"{verb} thread {resolved_id}.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Fetch PR review threads and top-level reviews")
    p_list.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    p_list.add_argument("--pr", default="", help="PR number or URL (defaults to current branch PR)")
    p_list.add_argument("--all", action="store_true", help="Include resolved threads too")
    p_list.add_argument("--json", action="store_true", help="Output structured JSON")

    p_reply = sub.add_parser("reply", help="Reply to a PR review thread (outward GitHub action)")
    p_reply.add_argument("--thread", required=True, help="Review thread GraphQL ID (e.g. PRRT_...)")
    body_grp = p_reply.add_mutually_exclusive_group(required=True)
    body_grp.add_argument("--body", default=None, help="Reply body text")
    body_grp.add_argument("--body-file", default=None, help="Path to a UTF-8 file containing the reply body")
    p_reply.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    p_reply.add_argument("--hostname", default=None, help="GitHub Enterprise host shown by `list` (default github.com)")
    p_reply.add_argument("--json", action="store_true", help="Output structured JSON")

    p_res = sub.add_parser("resolve", help="Resolve or unresolve a PR review thread (outward GitHub action)")
    p_res.add_argument("--thread", required=True, help="Review thread GraphQL ID (e.g. PRRT_...)")
    p_res.add_argument("--unresolve", action="store_true", help="Unresolve instead of resolving")
    p_res.add_argument("--workspace", "--project", "--path", dest="workspace", default=os.getcwd())
    p_res.add_argument("--hostname", default=None, help="GitHub Enterprise host shown by `list` (default github.com)")
    p_res.add_argument("--json", action="store_true", help="Output structured JSON")

    args = ap.parse_args(argv)
    ws = os.path.abspath(os.path.expanduser(args.workspace))
    if args.cmd == "list":
        return cmd_list(ws, pr=args.pr, include_all=args.all, as_json=args.json)
    if args.cmd == "reply":
        return cmd_reply(ws, thread_id=args.thread, body=args.body, body_file=args.body_file, as_json=args.json,
                         hostname=args.hostname)
    if args.cmd == "resolve":
        return cmd_resolve(ws, thread_id=args.thread, unresolve=args.unresolve, as_json=args.json,
                           hostname=args.hostname)
    return 1


if __name__ == "__main__":
    sys.exit(main())
