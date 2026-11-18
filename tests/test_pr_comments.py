"""/pr-comments skill: GraphQL list (with cursor pagination of threads and of each thread's comments, filtering, badges,
diffHunk trimming, URL credential masking, --all, --json, --pr), GitHub Enterprise hosts (--hostname), reply
(--body / --body-file), resolve / --unresolve, and error handling."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

PLUGIN, SP = sys.argv[1], sys.argv[2]
SKILL_DIR = os.path.join(PLUGIN, "skills", "pr-comments")
PR_COMMENTS_PY = os.path.join(SKILL_DIR, "scripts", "pr_comments.py")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
WIN = os.name == "nt"

# Isolate from host GitHub auth/config variables
for _var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
    os.environ.pop(_var, None)
os.environ["APPDATA"] = os.path.join(SP, "appdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(SP, "xdg")
os.makedirs(os.environ["APPDATA"], exist_ok=True)
os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

FAKE_BIN = os.path.join(SP, "fake_bin")
os.makedirs(FAKE_BIN, exist_ok=True)
STATE_FILE = os.path.join(SP, "fake_gh_state.json")
os.environ["AGP_FAKE_GH_STATE"] = STATE_FILE
ORIG_PATH = os.environ.get("PATH", "")
os.environ["PATH"] = FAKE_BIN + os.pathsep + ORIG_PATH

WS = os.path.join(SP, "workspace")
os.makedirs(WS, exist_ok=True)

FAKE_GH = os.path.join(SP, "fake_gh.py")
FAKE_GH_SRC = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

CRED_USERPASS = "https://alice:" + "s3cr3tPass" + "@example.com/org/repo"
CRED_TOKEN = "https://tok_" + "secret999" + "@example.com/build/log"
HUNK_12 = "\n".join(
    [f" line_{i:02d}" for i in range(1, 10)]
    + [f"+line_10 = '{CRED_TOKEN}'", " line_11", "+line_12"]
)


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mode": "normal", "calls": []}


def save_state(path: str, st: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def parse_graphql_vars(argv: list) -> dict:
    vars_out = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-f", "-F") and i + 1 < len(argv):
            kv = argv[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                vars_out[k] = v
            i += 2
            continue
        i += 1
    return vars_out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    argv = sys.argv[1:]
    state_path = os.environ.get("AGP_FAKE_GH_STATE", "")
    st = load_state(state_path)
    stdin_data = "" if sys.stdin.isatty() else sys.stdin.read()
    gvars = parse_graphql_vars(argv) if argv[:2] == ["api", "graphql"] else {}

    st.setdefault("calls", []).append({
        "argv": argv,
        "vars": gvars,
        "git_terminal_prompt": os.environ.get("GIT_TERMINAL_PROMPT", ""),
        "gh_prompt_disabled": os.environ.get("GH_PROMPT_DISABLED", ""),
        "stdin_isatty": sys.stdin.isatty(),
        "stdin_data": stdin_data,
    })
    save_state(state_path, st)

    mode = st.get("mode", "normal")

    if argv[:2] == ["pr", "view"]:
        if mode == "no_pr":
            sys.stderr.write('no pull requests found for branch "feat/demo"\n')
            return 1
        if mode == "pr_view_no_url":
            sys.stdout.write(json.dumps({
                "number": 55,
                "url": "",
                "title": "Fallback repo view test",
                "headRefName": "feat/fallback",
                "baseRefName": "main",
            }))
            return 0
        sys.stdout.write(json.dumps({
            "number": 42,
            "url": ("https://ghe.example.com" if mode == "ghes" else "https://github.com") + "/example/repo/pull/42",
            "title": "Fix parser edge cases",
            "headRefName": "feat/parser",
            "baseRefName": "main",
        }))
        return 0

    if argv[:2] == ["repo", "view"]:
        repo_info = {"owner": {"login": "example"}, "name": "repo"}
        if mode == "ghes":
            repo_info["url"] = "https://ghe.example.com/example/repo"
        sys.stdout.write(json.dumps(repo_info))
        return 0

    if argv[:2] == ["api", "graphql"]:
        query = gvars.get("query", "")
        if mode == "graphql_error":
            sys.stdout.write(json.dumps({
                "errors": [{"message": f"Could not resolve to a PullRequest ({CRED_TOKEN})"}]
            }))
            return 1
        if mode == "graphql_null":
            sys.stdout.write(json.dumps({
                "data": {"repository": {"pullRequest": None}}
            }))
            return 0
        if "node(id:" in query:
            if mode == "long_thread_fail":
                sys.stdout.write(json.dumps({"errors": [{"message": "Something went wrong while executing your query."}]}))
                return 1
            sys.stdout.write(json.dumps({"data": {"node": {"comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": "CC2"},
                "nodes": [{
                    "databaseId": 105,
                    "author": {"login": "reviewer1"},
                    "body": "Third comment from the second page.",
                    "createdAt": "2026-11-18T10:10:00Z",
                    "path": "src/app.py",
                    "diffHunk": "",
                    "url": "https://github.com/example/repo/pull/42#discussion_r105",
                }],
            }}}}))
            return 0
        if "addPullRequestReviewThreadReply" in query:
            tid = gvars.get("threadId", "")
            body = gvars.get("body", "")
            sys.stdout.write(json.dumps({
                "data": {
                    "addPullRequestReviewThreadReply": {
                        "comment": {
                            "id": "PRRC_kwDO1",
                            "databaseId": 501,
                            "url": "https://github.com/example/repo/pull/42#discussion_r501",
                            "author": {"login": "author1"},
                            "body": body,
                            "createdAt": "2026-11-18T12:00:00Z",
                            "threadId": tid,
                        }
                    }
                }
            }))
            return 0
        if "unresolveReviewThread" in query:
            tid = gvars.get("threadId", "")
            sys.stdout.write(json.dumps({
                "data": {"unresolveReviewThread": {"thread": {"id": tid, "isResolved": False}}}
            }))
            return 0
        if "resolveReviewThread" in query:
            tid = gvars.get("threadId", "")
            sys.stdout.write(json.dumps({
                "data": {"resolveReviewThread": {"thread": {"id": tid, "isResolved": True}}}
            }))
            return 0

        # reviewThreads query
        num = int(gvars.get("num", "42"))
        cursor = gvars.get("cursor")
        if mode == "all_resolved":
            threads = [{
                "id": "PRRT_99",
                "isResolved": True,
                "isOutdated": False,
                "path": "README.md",
                "line": 5,
                "originalLine": 5,
                "startLine": None,
                "diffSide": "RIGHT",
                "comments": {"nodes": [{
                    "databaseId": 99,
                    "author": {"login": "reviewer1"},
                    "body": "Looks good now.",
                    "createdAt": "2026-11-18T09:00:00Z",
                    "path": "README.md",
                    "diffHunk": "@@ -1,3 +1,3 @@\n+hello",
                    "url": "https://github.com/example/repo/pull/42#discussion_r99",
                }]},
            }]
            page_info = {"hasNextPage": False, "endCursor": "CUR_END"}
            reviews = []
        elif not cursor:
            threads = [
                {
                    "id": "PRRT_1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "src/app.py",
                    "line": 14,
                    "originalLine": 14,
                    "startLine": 12,
                    "diffSide": "RIGHT",
                    "comments": {**({"totalCount": 3, "pageInfo": {"hasNextPage": True, "endCursor": "CC1"}}
                                     if mode in ("long_thread", "long_thread_fail") else {}), "nodes": [
                        {
                            "databaseId": 101,
                            "author": {"login": "reviewer1"},
                            "body": (
                                f"See {CRED_USERPASS} and dev@example.com or "
                                "https://registry.example.org/@scope/pkg\nSecond line of feedback."
                            ),
                            "createdAt": "2026-11-18T10:00:00Z",
                            "path": "src/app.py",
                            "diffHunk": "@@ -10,3 +10,4 @@\n line_a\n+line_b\n line_c",
                            "url": "https://github.com/example/repo/pull/42#discussion_r101",
                        },
                        {
                            "databaseId": 102,
                            "author": {"login": "author1"},
                            "body": "Will fix.",
                            "createdAt": "2026-11-18T10:05:00Z",
                            "path": "src/app.py",
                            "diffHunk": "",
                            "url": "https://github.com/example/repo/pull/42#discussion_r102",
                        },
                    ]},
                },
                {
                    "id": "PRRT_2",
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "src/done.py",
                    "line": 7,
                    "originalLine": 7,
                    "startLine": None,
                    "diffSide": "RIGHT",
                    "comments": {"nodes": [{
                        "databaseId": 103,
                        "author": {"login": "reviewer2"},
                        "body": "Already fixed nit.",
                        "createdAt": "2026-11-18T09:30:00Z",
                        "path": "src/done.py",
                        "diffHunk": "@@ -5,2 +5,2 @@\n-old\n+new",
                        "url": "https://github.com/example/repo/pull/42#discussion_r103",
                    }]},
                },
            ]
            page_info = {"hasNextPage": True, "endCursor": "CUR_1"}
            reviews = [
                {
                    "id": "PRR_1",
                    "author": {"login": "reviewer1"},
                    "state": "CHANGES_REQUESTED",
                    "body": f"Please fix the parser error and check {CRED_TOKEN}.",
                    "submittedAt": "2026-11-18T10:01:00Z",
                    "url": "https://github.com/example/repo/pull/42#pullrequestreview-1",
                },
                {
                    "id": "PRR_2",
                    "author": {"login": "reviewer2"},
                    "state": "COMMENTED",
                    "body": "   \n  ",
                    "submittedAt": "2026-11-18T10:02:00Z",
                    "url": "https://github.com/example/repo/pull/42#pullrequestreview-2",
                },
            ]
        else:
            threads = [
                {
                    "id": "PRRT_3",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "src/legacy.py",
                    "line": None,
                    "originalLine": 88,
                    "startLine": None,
                    "diffSide": "RIGHT",
                    "comments": {"nodes": [{
                        "databaseId": 104,
                        "author": None,
                        "body": "This block shifted after rebase.",
                        "createdAt": "2026-11-18T11:00:00Z",
                        "path": "src/legacy.py",
                        "diffHunk": HUNK_12,
                        "url": "https://github.com/example/repo/pull/42#discussion_r104",
                    }]},
                }
            ]
            page_info = {"hasNextPage": False, "endCursor": "CUR_2"}
            reviews = []

        sys.stdout.write(json.dumps({
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": num,
                        "title": f"PR #{num} title",
                        "url": f"https://github.com/example/repo/pull/{num}",
                        "state": "OPEN",
                        "headRefName": "feat/parser",
                        "baseRefName": "main",
                        "reviewDecision": "CHANGES_REQUESTED",
                        "reviewThreads": {"pageInfo": page_info, "nodes": threads},
                        "reviews": {"nodes": reviews},
                    }
                }
            }
        }))
        return 0

    sys.stderr.write(f"unexpected gh command: {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

with open(FAKE_GH, "w", encoding="utf-8") as _f:
    _f.write(FAKE_GH_SRC)

if WIN:
    _shim = os.path.join(FAKE_BIN, "gh.cmd")
    with open(_shim, "w", encoding="utf-8") as _f:
        _f.write(f'@echo off\r\n"{sys.executable}" "{FAKE_GH}" %*\r\n')
else:
    _shim = os.path.join(FAKE_BIN, "gh")
    with open(_shim, "w", encoding="utf-8") as _f:
        _f.write(
            f'#!/usr/bin/env python3\nimport runpy, sys\nrunpy.run_path({FAKE_GH!r}, run_name="__main__")\n'
        )
    os.chmod(_shim, 0o755)


ok = True


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond or detail == "" else f" → {str(detail)[-700:]}"))


def reset_state(mode: str = "normal") -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "calls": []}, f)


def read_calls() -> list:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f).get("calls", [])


def run_cli(*args: str, env_override: Optional[dict] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, PR_COMMENTS_PY, *args],
        cwd=WS,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


# 1. SKILL.md contract check
skill_txt = open(SKILL_MD, encoding="utf-8").read() if os.path.isfile(SKILL_MD) else ""
check(
    "SKILL.md has pr-comments frontmatter and distinguishes read-only list vs outward reply/resolve",
    skill_txt.startswith("---\nname: pr-comments\n")
    and "list" in skill_txt
    and "reply" in skill_txt
    and "resolve" in skill_txt
    and "only" in skill_txt.lower(),
    skill_txt[:300],
)

# 2. Default `list` (resolves PR via `gh pr view`, paginates reviewThreads, filters resolved, trims hunk, redacts URLs)
reset_state("normal")
r = run_cli("list", "--workspace", WS)
calls = read_calls()
out = r.stdout
check(
    "list exits 0 and runs gh pr view + 2 paginated graphql calls",
    r.returncode == 0
    and len(calls) == 3
    and calls[0]["argv"][:2] == ["pr", "view"]
    and calls[1]["argv"][:2] == ["api", "graphql"]
    and "cursor" not in calls[1]["vars"]
    and calls[2]["vars"].get("cursor") == "CUR_1"
    and calls[2]["vars"].get("owner") == "example"
    and calls[2]["vars"].get("name") == "repo"
    and calls[2]["vars"].get("num") == "42",
    {"rc": r.returncode, "stderr": r.stderr, "calls": calls},
)
check(
    "gh invoked non-interactively (GIT_TERMINAL_PROMPT=0, GH_PROMPT_DISABLED=1, stdin=DEVNULL)",
    all(
        c["git_terminal_prompt"] == "0"
        and c["gh_prompt_disabled"] == "1"
        and (os.name == "nt" or not c["stdin_isatty"])  # Windows reports NUL (DEVNULL) as a TTY
        and c["stdin_data"] == ""
        for c in calls
    ),
    calls,
)
check(
    "list filters out resolved threads by default and keeps unresolved + outdated + non-empty reviews",
    "PR #42:" in out
    and "2 unresolved (3 total, 1 resolved)" in out
    and "[1] PRRT_1  src/app.py:12-14" in out
    and "[2] PRRT_3  src/legacy.py:88  [OUTDATED]" in out
    and "PRRT_2" not in out
    and "Already fixed nit." not in out
    and "@ghost (" in out
    and "Top-level PR reviews (1):" in out
    and "PRR_2" not in out
    and "COMMENTED" not in out,
    out,
)
check(
    "diffHunk trimmed to last 8 lines (lines 01-04 omitted, 05-12 shown)",
    "line_04" not in out and "line_05" in out and "line_12" in out,
    out,
)
check(
    "URL credentials redacted while emails and scoped package URLs remain intact",
    "s3cr3tPass" not in out
    and "tok_secret999" not in out
    and "https://***@example.com/org/repo" in out
    and "https://***@example.com/build/log" in out
    and "dev@example.com" in out
    and "https://registry.example.org/@scope/pkg" in out,
    out,
)

# 3. `list --all` includes resolved threads with [RESOLVED] badge
reset_state("normal")
r = run_cli("list", "--workspace", WS, "--all")
check(
    "list --all includes resolved threads with [RESOLVED] badge",
    r.returncode == 0
    and "[2] PRRT_2  src/done.py:7  [RESOLVED]" in r.stdout
    and "[3] PRRT_3  src/legacy.py:88  [OUTDATED]" in r.stdout
    and "Already fixed nit." in r.stdout,
    r.stdout,
)

# 4. `list --pr 77` uses gh repo view and queries PR #77
reset_state("normal")
r = run_cli("list", "--workspace", WS, "--pr", "77", "--json")
calls = read_calls()
try:
    jdata = json.loads(r.stdout or "{}")
except ValueError:
    jdata = {}
check(
    "list --pr 77 --json queries PR #77 via gh repo view and outputs redacted, trimmed JSON",
    r.returncode == 0
    and calls[0]["argv"][:2] == ["repo", "view"]
    and calls[1]["vars"].get("num") == "77"
    and jdata.get("pull_request", {}).get("number") == 77
    and jdata.get("total_threads") == 3
    and jdata.get("unresolved_count") == 2
    and jdata.get("resolved_count") == 1
    and len(jdata.get("threads", [])) == 2
    and len(jdata.get("reviews", [])) == 1
    and "s3cr3tPass" not in r.stdout
    and "tok_secret999" not in r.stdout
    and "line_04" not in jdata["threads"][1]["diffHunk"]
    and "line_05" in jdata["threads"][1]["diffHunk"],
    {"rc": r.returncode, "out": r.stdout[:500], "err": r.stderr},
)

# 5. Fallback to `gh repo view` when `gh pr view` has no url
reset_state("pr_view_no_url")
r = run_cli("list", "--workspace", WS)
calls = read_calls()
check(
    "list falls back to gh repo view when gh pr view omits url",
    r.returncode == 0
    and calls[0]["argv"][:2] == ["pr", "view"]
    and calls[1]["argv"][:2] == ["repo", "view"]
    and calls[2]["vars"].get("num") == "55",
    {"rc": r.returncode, "calls": [c["argv"][:2] for c in calls], "err": r.stderr},
)

# 6. All threads resolved message
reset_state("all_resolved")
r = run_cli("list", "--workspace", WS)
check(
    "all threads resolved reports 0 unresolved and hints --all",
    r.returncode == 0 and "0 unresolved (1 total, 1 resolved)" in r.stdout and "--all" in r.stdout,
    r.stdout,
)

# 7. `reply` via --body and --body-file + validation errors
reset_state("normal")
r = run_cli("reply", "--thread", "PRRT_1", "--body", "Fixed in latest commit.", "--workspace", WS)
calls = read_calls()
check(
    "reply --body calls addPullRequestReviewThreadReply with threadId and body",
    r.returncode == 0
    and len(calls) == 1
    and "addPullRequestReviewThreadReply" in calls[0]["vars"].get("query", "")
    and calls[0]["vars"].get("threadId") == "PRRT_1"
    and calls[0]["vars"].get("body") == "Fixed in latest commit."
    and "Replied to thread PRRT_1" in r.stdout,
    {"rc": r.returncode, "out": r.stdout, "err": r.stderr, "calls": calls},
)

body_path = os.path.join(SP, "reply_body.txt")
with open(body_path, "w", encoding="utf-8") as f:
    f.write("Updated parser handling per review feedback.")
reset_state("normal")
r = run_cli("reply", "--thread", "PRRT_3", "--body-file", body_path, "--workspace", WS)
calls = read_calls()
check(
    "reply --body-file reads UTF-8 file and passes contents to mutation",
    r.returncode == 0
    and calls[0]["vars"].get("threadId") == "PRRT_3"
    and calls[0]["vars"].get("body") == "Updated parser handling per review feedback.",
    {"rc": r.returncode, "err": r.stderr, "calls": calls},
)

reset_state("normal")
r_empty_tid = run_cli("reply", "--thread", "   ", "--body", "ok", "--workspace", WS)
r_empty_body = run_cli("reply", "--thread", "PRRT_1", "--body", "  \n ", "--workspace", WS)
r_missing_file = run_cli("reply", "--thread", "PRRT_1", "--body-file", os.path.join(SP, "nope.txt"), "--workspace", WS)
check(
    "reply rejects empty thread, empty body, and missing body-file without calling gh",
    r_empty_tid.returncode != 0
    and r_empty_body.returncode != 0
    and r_missing_file.returncode != 0
    and read_calls() == [],
    {
        "empty_tid": (r_empty_tid.returncode, r_empty_tid.stderr),
        "empty_body": (r_empty_body.returncode, r_empty_body.stderr),
        "missing_file": (r_missing_file.returncode, r_missing_file.stderr),
        "calls": read_calls(),
    },
)

# 8. `resolve` and `--unresolve` + empty thread validation
reset_state("normal")
r = run_cli("resolve", "--thread", "PRRT_1", "--workspace", WS)
calls = read_calls()
check(
    "resolve calls resolveReviewThread mutation",
    r.returncode == 0
    and len(calls) == 1
    and "resolveReviewThread" in calls[0]["vars"].get("query", "")
    and "unresolveReviewThread" not in calls[0]["vars"].get("query", "")
    and calls[0]["vars"].get("threadId") == "PRRT_1"
    and "Resolved thread PRRT_1." in r.stdout,
    {"rc": r.returncode, "out": r.stdout, "err": r.stderr, "calls": calls},
)

reset_state("normal")
r = run_cli("resolve", "--thread", "PRRT_1", "--unresolve", "--workspace", WS)
calls = read_calls()
check(
    "resolve --unresolve calls unresolveReviewThread mutation",
    r.returncode == 0
    and len(calls) == 1
    and "unresolveReviewThread" in calls[0]["vars"].get("query", "")
    and calls[0]["vars"].get("threadId") == "PRRT_1"
    and "Unresolved thread PRRT_1." in r.stdout,
    {"rc": r.returncode, "out": r.stdout, "err": r.stderr, "calls": calls},
)

reset_state("normal")
r_res_empty = run_cli("resolve", "--thread", "", "--workspace", WS)
check(
    "resolve rejects empty thread ID without calling gh",
    r_res_empty.returncode != 0 and read_calls() == [],
    (r_res_empty.returncode, r_res_empty.stderr),
)

# 8b. GitHub Enterprise: the PR's host goes to `gh api --hostname`; github.com needs none
reset_state("normal")
run_cli("list", "--workspace", WS)
check(
    "github.com PRs: gh api graphql runs without --hostname",
    all("--hostname" not in c["argv"] for c in read_calls()),
    read_calls(),
)
reset_state("normal")
r = run_cli("list", "--workspace", WS, "--pr", "https://ghe.example.com/acme/tools/pull/9")
calls = read_calls()
gql = [c for c in calls if c["argv"][:2] == ["api", "graphql"]]
check(
    "list --pr <GitHub Enterprise URL> queries that host and prints it for reply/resolve",
    r.returncode == 0
    and gql
    and all(c["argv"][2:4] == ["--hostname", "ghe.example.com"] for c in gql)
    and gql[0]["vars"].get("owner") == "acme"
    and gql[0]["vars"].get("name") == "tools"
    and gql[0]["vars"].get("num") == "9"
    and "Host: ghe.example.com (pass --hostname ghe.example.com to reply / resolve)" in r.stdout,
    {"rc": r.returncode, "out": r.stdout, "err": r.stderr, "calls": calls},
)
reset_state("ghes")
r = run_cli("list", "--workspace", WS, "--json")
calls = read_calls()
check(
    "list on a GitHub Enterprise branch takes the host from gh pr view",
    r.returncode == 0
    and all(c["argv"][2:4] == ["--hostname", "ghe.example.com"] for c in calls if c["argv"][:2] == ["api", "graphql"])
    and json.loads(r.stdout or "{}").get("pull_request", {}).get("hostname") == "ghe.example.com",
    {"rc": r.returncode, "err": r.stderr, "calls": calls},
)
reset_state("ghes")
r = run_cli("list", "--workspace", WS, "--pr", "77")
calls = read_calls()
check(
    "list --pr <number> on GitHub Enterprise takes the host from gh repo view",
    r.returncode == 0
    and calls[0]["argv"][:2] == ["repo", "view"]
    and calls[1]["argv"][2:4] == ["--hostname", "ghe.example.com"],
    {"rc": r.returncode, "err": r.stderr, "calls": calls},
)
reset_state("normal")
r_rep = run_cli("reply", "--thread", "PRRT_1", "--body", "Done.", "--hostname", "ghe.example.com", "--workspace", WS)
r_res = run_cli("resolve", "--thread", "PRRT_1", "--hostname", "GitHub.com", "--workspace", WS)
calls = read_calls()
check(
    "reply/resolve --hostname: Enterprise host passed to gh api, github.com left to gh's default",
    r_rep.returncode == 0
    and r_res.returncode == 0
    and calls[0]["argv"][2:4] == ["--hostname", "ghe.example.com"]
    and "--hostname" not in calls[1]["argv"],
    {"reply": (r_rep.returncode, r_rep.stderr), "resolve": (r_res.returncode, r_res.stderr), "calls": calls},
)

# 8c. A thread with more comments than the first page: the rest is fetched, or the thread says it's truncated
reset_state("long_thread")
r = run_cli("list", "--workspace", WS, "--json")
calls = read_calls()
node_calls = [c for c in calls if "node(id:" in c["vars"].get("query", "")]
try:
    jlong = json.loads(r.stdout or "{}")
except ValueError:
    jlong = {}
t1 = next((t for t in jlong.get("threads", []) if t.get("id") == "PRRT_1"), {})
check(
    "list fetches a thread's further comment pages (node query with the thread id and cursor)",
    r.returncode == 0
    and len(node_calls) == 1
    and node_calls[0]["vars"].get("id") == "PRRT_1"
    and node_calls[0]["vars"].get("cursor") == "CC1"
    and [c.get("databaseId") for c in t1.get("comments", [])] == [101, 102, 105]
    and t1.get("commentsTotal") == 3
    and t1.get("commentsTruncated") is False,
    {"rc": r.returncode, "err": r.stderr, "thread": t1, "calls": [c["argv"][:2] for c in calls]},
)
reset_state("long_thread_fail")
r = run_cli("list", "--workspace", WS)
check(
    "a comment page that can't be fetched leaves a [TRUNCATED] note instead of silently dropping comments",
    r.returncode == 0
    and "[TRUNCATED] 2 of 3 comments shown: Something went wrong while executing your query." in r.stdout
    and "Will fix." in r.stdout,
    (r.returncode, r.stdout, r.stderr),
)

# 9. Error handling: no PR, GraphQL error (with redacted URL), null PR, missing workspace, missing gh
reset_state("no_pr")
r_nopr = run_cli("list", "--workspace", WS)
check(
    "list fails cleanly when no PR exists for the current branch",
    r_nopr.returncode != 0 and "No pull request found" in r_nopr.stderr,
    (r_nopr.returncode, r_nopr.stderr),
)

reset_state("graphql_error")
r_gql = run_cli("list", "--workspace", WS, "--pr", "999")
check(
    "list surfaces GraphQL error and redacts any URL credentials in error text",
    r_gql.returncode != 0
    and "Could not resolve to a PullRequest" in r_gql.stderr
    and "tok_secret999" not in r_gql.stderr
    and "https://***@example.com/build/log" in r_gql.stderr,
    (r_gql.returncode, r_gql.stderr),
)

reset_state("graphql_null")
r_null = run_cli("list", "--workspace", WS, "--pr", "999")
check(
    "list fails cleanly when pullRequest node is null",
    r_null.returncode != 0 and "Pull request #999 not found" in r_null.stderr,
    (r_null.returncode, r_null.stderr),
)

r_bad_ws = run_cli("list", "--workspace", os.path.join(SP, "missing_ws"))
check(
    "fails cleanly when workspace directory does not exist",
    r_bad_ws.returncode != 0 and "does not exist" in r_bad_ws.stderr,
    (r_bad_ws.returncode, r_bad_ws.stderr),
)

empty_bin = os.path.join(SP, "empty_bin")
os.makedirs(empty_bin, exist_ok=True)
r_no_gh = run_cli("list", "--workspace", WS, env_override={"PATH": empty_bin})
check(
    "fails cleanly when gh is not on PATH",
    r_no_gh.returncode != 0 and "gh" in r_no_gh.stderr.lower(),
    (r_no_gh.returncode, r_no_gh.stderr),
)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
