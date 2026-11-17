#!/usr/bin/env python3
"""Antigravity Plus — /release: cut a release of any project safely.

  release.py plan    [--path DIR] [--fetch] [--no-ci] [--json]
        version sources and mismatches, the last release tag and the commits since, the suggested bump (conventional
        commits), draft notes, CHANGELOG.md, and the state: clean tree, default branch, in sync, CI of HEAD (gh)
  release.py bump    <major|minor|patch|X.Y.Z> [--path DIR] [--dry-run] [--no-changelog]
        every version source to the new version (minimal textual edits: formatting and comments survive) and a new
        CHANGELOG.md section when the project keeps one
  release.py notes   [--since TAG] [--until REF] [--out FILE] [--path DIR]
        markdown release notes from the commit subjects (breaking / features / fixes / other)
  release.py tag     [--version X.Y.Z] [--message TEXT] [--path DIR] [--dry-run]
        a local annotated tag (git tag -a vX.Y.Z -m …); undo with git tag -d
  release.py publish [--version X.Y.Z] [--remote NAME] [--title TEXT] [--notes-file FILE] [--draft] [--prerelease]
                     [--path DIR] --yes
        pushes the tag and creates the GitHub release (gh release create --notes-file); outward-facing, so it only
        prints what it would do unless --yes is given

Everything except bump, tag and publish only reads. No command waits for input (no editor, pager or password prompt).
Exit codes: 0 done, 1 refused or failed, 2 not a git repository (where one is needed), 3 publish without --yes.
Stdlib only, Python 3.8+; macOS, Linux and Windows.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GH_PAGER": "cat", "GH_PROMPT_DISABLED": "1",
             "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GIT_EDITOR": "true", "LC_ALL": "C", "LANG": "C"}
SKIP_DIRS = {"node_modules", "vendor", "build", "dist", "target", "venv", "env", "Pods", "DerivedData", "__pycache__",
             "out", "tests", "test", "docs", "site-packages"}
VERSION_RE = re.compile(r"^(v?)(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
FULL_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
TAG_RE = re.compile(r"^(v?)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
CC_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()]*)\))?(?P<bang>!)?:\s*(?P<desc>\S.*)$")
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.M)
SKIP_SUBJECT_RE = re.compile(r"^(?:chore\(release\)|release\b|bump version|version bump|v?\d+\.\d+\.\d+$)", re.I)
JSON_TOKEN = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,"/]+|/', re.S)
PARTS = ("major", "minor", "patch")
GROUPS = (("breaking", "Breaking changes"), ("features", "Features"), ("fixes", "Fixes"), ("other", "Other"))


# ─────────────────────────────── processes ───────────────────────────────

def run(cmd: List[str], cwd: str, timeout: float = 20.0) -> Tuple[int, str, str]:
    try:
        exe = shutil.which(cmd[0])
        argv = [exe] + cmd[1:] if exe else cmd
        r = subprocess.run(argv, cwd=cwd, env=dict(os.environ, **QUIET_ENV), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, "", str(e)


def git(args: List[str], cwd: str, timeout: float = 15.0) -> Optional[str]:
    code, out, _ = run(["git"] + args, cwd, timeout)
    return out if code == 0 else None


def first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def repo_root(path: str) -> Optional[str]:
    out = git(["rev-parse", "--show-toplevel"], path)
    return os.path.normpath(out.strip()) if out and out.strip() else None


# ─────────────────────────────── versions ───────────────────────────────

def parse_version(s: str) -> Optional[tuple]:
    m = VERSION_RE.match((s or "").strip())
    if not m:
        return None
    return int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0), m.group(5) or ""


def fmt(v: tuple) -> str:
    return "%d.%d.%d" % v[:3] + (f"-{v[3]}" if v[3] else "")


def vkey(v: tuple) -> tuple:
    """Semver precedence: a pre-release sorts before its release, numeric identifiers numerically."""
    if not v[3]:
        pre: tuple = (1,)
    else:
        pre = (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in v[3].split(".")))
    return v[0], v[1], v[2], pre


def bump_version(v: tuple, part: str) -> tuple:
    major, minor, patch, pre = v
    if part == "major":
        return (major, 0, 0, "") if pre and minor == 0 and patch == 0 else (major + 1, 0, 0, "")
    if part == "minor":
        return (major, minor, 0, "") if pre and patch == 0 else (major, minor + 1, 0, "")
    return (major, minor, patch, "") if pre else (major, minor, patch + 1, "")


# ─────────────────────────────── version sources ───────────────────────────────

def read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def json_strings(text: str, wanted: List[tuple]) -> Dict[tuple, Tuple[int, int, str]]:
    """String values at the given key paths of a JSON (or JSON-with-comments) text: path → (start, end, value).

    A token scan, not a parse: comments, trailing commas and the rest of the file are left exactly as they are, and the
    spans point inside the quotes so an edit replaces only the value."""
    want = set(wanted)
    found: Dict[tuple, Tuple[int, int, str]] = {}
    path: List[object] = []
    stack: List[list] = []  # [kind, key or index, expecting a key]
    for m in JSON_TOKEN.finditer(text):
        tok = m.group(0)
        c = tok[0]
        if c.isspace() or tok.startswith("//") or tok.startswith("/*"):
            continue
        top = stack[-1] if stack else None
        if c in "{[":
            if top is not None:
                path.append(top[1])
            stack.append(["obj", None, True] if c == "{" else ["arr", 0, False])
        elif c in "}]":
            if stack:
                stack.pop()
            if stack and path:
                path.pop()
        elif c == ":":
            if top is not None:
                top[2] = False
        elif c == ",":
            if top is not None:
                if top[0] == "obj":
                    top[2] = True
                else:
                    top[1] += 1
        elif c == '"':
            try:
                value = json.loads(tok)
            except ValueError:
                value = tok[1:-1]
            if top is not None and top[0] == "obj" and top[2]:
                top[1] = value
                continue
            key = tuple(path) + ((top[1],) if top is not None else ())
            if key in want and key not in found:
                found[key] = (m.start() + 1, m.end() - 1, value)
                if len(found) == len(want):
                    break
    return found


SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:[#;].*)?$")


def section_match(text: str, sections: Tuple[str, ...], pattern: "re.Pattern[str]") -> Dict[str, Tuple[int, int, str]]:
    """The first `pattern` match (group "v") inside each of the INI/TOML sections: section → (start, end, value)."""
    found: Dict[str, Tuple[int, int, str]] = {}
    section, pos = "", 0
    for line in text.splitlines(True):
        body = line.rstrip("\r\n")
        m = SECTION_RE.match(body)
        if m:
            section = m.group(1)
        elif section in sections and section not in found:
            m = pattern.match(body)
            if m:
                found[section] = (pos + m.start("v"), pos + m.end("v"), m.group("v"))
        pos += len(line)
    return found


def section_text(text: str, section: str) -> str:
    out, cur = [], ""
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            cur = m.group(1)
        elif cur == section:
            out.append(line)
    return "\n".join(out)


TOML_VERSION = re.compile(r"""^\s*version\s*=\s*(["'])(?P<v>[^"'\r\n]*)\1""")
CFG_VERSION = re.compile(r"^\s*version\s*[=:]\s*(?P<v>[^\s#;]+)\s*$")
PY_VERSION = re.compile(r"""^__version__\s*(?::\s*str\s*)?=\s*(["'])(?P<v>[^"'\r\n]+)\1""", re.M)
GRADLE_VERSION = re.compile(r"""\bversionName\s*(?:=\s*)?(["'])(?P<v>[^"'\r\n]+)\1""")
GRADLE_PROPS = re.compile(r"^[ \t]*(?:VERSION_NAME|version)[ \t]*=[ \t]*(?P<v>[^\s#]+)[ \t]*$", re.M)
XCODE_VERSION = re.compile(r"""\bMARKETING_VERSION = (?P<q>"?)(?P<v>[^";\r\n]+)(?P=q);""")


def subdirs(root: str) -> List[str]:
    try:
        names = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except OSError:
        return []
    return [n for n in names if n not in SKIP_DIRS and (not n.startswith(".") or n == ".claude-plugin")]


def find_sources(root: str) -> Tuple[List[dict], List[str]]:
    """Every literal version the project declares, in priority order, and notes about versions left alone."""
    sources: List[dict] = []
    notes: List[str] = []

    def add(rel: str, label: str, matches: List[Tuple[int, int, str]]) -> None:
        by_value: Dict[str, List[Tuple[int, int]]] = {}
        for start, end, value in matches:
            if parse_version(value) is None:
                notes.append(f"{rel}: {label} = {value!r} is not a literal version — left alone")
                continue
            by_value.setdefault(value, []).append((start, end))
        for value, spans in by_value.items():
            sources.append({"file": rel, "label": label, "value": value, "spans": spans})

    def rel(path: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    def at(*parts: str) -> str:
        return os.path.join(root, *parts)

    # npm
    for name, paths in (("package.json", [("version",)]), ("package-lock.json", [("version",), ("packages", "", "version")])):
        text = read_text(at(name))
        if text is not None:
            found = json_strings(text, paths)
            for key in paths:
                if key in found:
                    add(name, '"' + '"."'.join(str(k) for k in key) + '"' if len(key) > 1 else '"version"', [found[key]])
    # Python
    text = read_text(at("pyproject.toml"))
    if text is not None:
        found = section_match(text, ("project", "tool.poetry"), TOML_VERSION)
        for sec in ("project", "tool.poetry"):
            if sec in found:
                add("pyproject.toml", f"[{sec}] version", [found[sec]])
        if "project" not in found and re.search(r"""^\s*dynamic\s*=\s*\[[^\]]*["']version["']""",
                                                section_text(text, "project"), re.M):
            notes.append("pyproject.toml: [project] version is dynamic (set by the build backend from another file)")
    text = read_text(at("setup.cfg"))
    if text is not None:
        found = section_match(text, ("metadata",), CFG_VERSION)
        if "metadata" in found:
            add("setup.cfg", "[metadata] version", [found["metadata"]])
        elif re.search(r"^\s*version\s*[=:]\s*(?:attr|file):", section_text(text, "metadata"), re.M):
            notes.append("setup.cfg: [metadata] version comes from attr:/file: — the file it names is the source")
    # Rust
    text = read_text(at("Cargo.toml"))
    if text is not None:
        found = section_match(text, ("package", "workspace.package"), TOML_VERSION)
        for sec in ("package", "workspace.package"):
            if sec in found:
                add("Cargo.toml", f"[{sec}] version", [found[sec]])
    # plugin and extension manifests
    for folder in [""] + subdirs(root) + [d + "/.claude-plugin" for d in subdirs(root) if d != ".claude-plugin"]:
        for name in ("plugin.json", "manifest.json"):
            path = at(*folder.split("/"), name) if folder else at(name)
            text = read_text(path)
            if text is not None:
                found = json_strings(text, [("version",)])
                if ("version",) in found:
                    add(rel(path), '"version"', [found[("version",)]])
    # plain version files
    for name in ("VERSION", "VERSION.txt", "version.txt"):
        text = read_text(at(name))
        if text is not None and os.path.isfile(at(name)):
            m = re.match(r"\s*(?P<v>\S+)", text)
            if m:
                add(name, "file content", [(m.start("v"), m.end("v"), m.group("v"))])
    # __version__ in a Python package
    for base in ("", "src"):
        top = at(base) if base else root
        for pkg in subdirs(top):
            if not pkg.isidentifier() or not os.path.isfile(os.path.join(top, pkg, "__init__.py")):
                continue
            for name in ("__init__.py", "_version.py", "__about__.py", "version.py"):
                path = os.path.join(top, pkg, name)
                text = read_text(path)
                if text is not None:
                    m = PY_VERSION.search(text)
                    if m:
                        add(rel(path), "__version__", [(m.start("v"), m.end("v"), m.group("v"))])
    # Android / Gradle
    for folder in [""] + subdirs(root):
        for name in ("build.gradle", "build.gradle.kts"):
            path = at(folder, name) if folder else at(name)
            text = read_text(path)
            if text is not None:
                matches = [(m.start("v"), m.end("v"), m.group("v")) for m in GRADLE_VERSION.finditer(text)]
                if matches:
                    add(rel(path), "versionName", matches)
    text = read_text(at("gradle.properties"))
    if text is not None:
        matches = [(m.start("v"), m.end("v"), m.group("v")) for m in GRADLE_PROPS.finditer(text)]
        if matches:
            add("gradle.properties", "VERSION_NAME / version", matches)
    # Xcode
    for folder in [""] + subdirs(root):
        top = at(folder) if folder else root
        try:
            projects = sorted(n for n in os.listdir(top) if n.endswith(".xcodeproj"))
        except OSError:
            continue
        for proj in projects:
            path = os.path.join(top, proj, "project.pbxproj")
            text = read_text(path)
            if text is not None:
                matches = [(m.start("v"), m.end("v"), m.group("v")) for m in XCODE_VERSION.finditer(text)]
                if matches:
                    add(rel(path), "MARKETING_VERSION", matches)
    return sources, notes


def current_version(sources: List[dict]) -> Tuple[Optional[tuple], List[dict]]:
    """The version most sources agree on (ties: the earliest source) and the sources that say something else."""
    counts: Dict[tuple, int] = {}
    order: List[tuple] = []
    for s in sources:
        v = parse_version(s["value"])
        if v is None:
            continue
        if v not in counts:
            order.append(v)
        counts[v] = counts.get(v, 0) + 1
    if not order:
        return None, []
    best = max(order, key=lambda v: (counts[v], -order.index(v)))
    return best, [s for s in sources if parse_version(s["value"]) != best]


# ─────────────────────────────── tags and commits ───────────────────────────────

def release_tags(root: str, merged: str = "") -> List[Tuple[str, tuple]]:
    out = git(["tag", "--list"] + (["--merged", merged] if merged else []), root) or ""
    tags = []
    for t in out.split():
        m = TAG_RE.match(t)
        v = parse_version(m.group(2)) if m else None
        if v is not None:
            tags.append((t, v))
    return sorted(tags, key=lambda tv: vkey(tv[1]), reverse=True)


def last_tag(root: str, ref: str = "HEAD") -> Optional[Tuple[str, tuple]]:
    tags = release_tags(root, ref)
    return tags[0] if tags else None


def tag_prefix(root: str) -> str:
    tags = release_tags(root)
    if tags:
        m = TAG_RE.match(tags[0][0])
        return m.group(1) if m else "v"
    return "v"


def tag_exists(root: str, tag: str) -> bool:
    return git(["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], root) is not None


def commits(root: str, since: str = "", until: str = "HEAD") -> List[dict]:
    rng = f"{since}..{until}" if since else until
    out = git(["log", "--no-merges", "--format=%h%x1f%s%x1f%b%x1e", rng], root) or ""
    result = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\r\n")
        if not rec.strip():
            continue
        short, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        result.append(classify({"sha": short.strip(), "subject": subject.strip(), "body": body}))
    return result


def classify(c: dict) -> dict:
    m = CC_RE.match(c["subject"])
    kind = m.group("type").lower() if m else ""
    breaking = bool(m and m.group("bang")) or bool(BREAKING_RE.search(c["body"]))
    group = "breaking" if breaking else "features" if kind == "feat" else "fixes" if kind in ("fix", "perf") else "other"
    if m:
        scope = (m.group("scope") or "").strip()
        text = (f"{scope}: " if scope else "") + m.group("desc").strip()
    else:
        text = c["subject"]
    return dict(c, type=kind, group=group, text=text, skip=bool(SKIP_SUBJECT_RE.match(c["subject"])))


def suggest(cs: List[dict], cur: Optional[tuple]) -> Tuple[Optional[str], str]:
    cs = [c for c in cs if not c["skip"]]
    if not cs:
        return None, "no commits since the last release"

    def sample(group: str) -> str:
        items = [c["subject"] for c in cs if c["group"] == group]
        return f"{len(items)} — " + "; ".join(items[:3]) + (" …" if len(items) > 3 else "")

    if any(c["group"] == "breaking" for c in cs):
        why = "breaking changes (`!` or BREAKING CHANGE): " + sample("breaking")
        if cur is not None and cur[0] == 0:
            why += " (before 1.0 many projects bump minor for breaking changes — the user's call)"
        return "major", why
    if any(c["group"] == "features" for c in cs):
        return "minor", "new features (feat:): " + sample("features")
    if any(c["group"] == "fixes" for c in cs):
        return "patch", "fixes only (fix:/perf:): " + sample("fixes")
    return "patch", ("no feat/fix/breaking commits by conventional-commit prefixes — patch by default; read the "
                     "subjects below: if they add features, choose minor")


def notes_md(cs: List[dict], since: str = "") -> str:
    cs = [c for c in cs if not c["skip"]]
    if not cs:
        return f"_No changes since {since}._\n" if since else "_No changes._\n"
    out = []
    for group, title in GROUPS:
        items = [c for c in cs if c["group"] == group]
        if items:
            out.append(f"### {title}\n")
            out.extend(f"- {c['text']} ({c['sha']})" for c in items)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ─────────────────────────────── CHANGELOG.md ───────────────────────────────

UNRELEASED_RE = re.compile(r"^##\s*\[?unreleased\]?.*$", re.I | re.M)
H2_RE = re.compile(r"^##\s+.*$", re.M)
VERSION_HEADING_RE = re.compile(r"^##\s+.*?\bv?\d+\.\d+(?:\.\d+)?\b.*$", re.M)


def changelog_path(root: str) -> Optional[str]:
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for name in sorted(names):
        if name.lower() == "changelog.md" and os.path.isfile(os.path.join(root, name)):
            return os.path.join(root, name)
    return None


def changelog_heading(text: str, version: str, today: str) -> str:
    """A heading for the new section in the style of the newest existing one; Keep a Changelog style otherwise."""
    for m in VERSION_HEADING_RE.finditer(text):
        line = m.group(0)
        if UNRELEASED_RE.match(line):
            continue
        new = re.sub(r"(?<![\w.])(v?)\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?", lambda mm: mm.group(1) + version, line,
                     count=1)
        return re.sub(r"\d{4}-\d{2}-\d{2}", today, new, count=1)
    return f"## [{version}] - {today}"


def changelog_update(text: str, version: str, notes: str, today: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    body = text.replace("\r\n", "\n")
    heading = changelog_heading(body, version, today)
    notes = notes.strip("\n")
    unreleased = UNRELEASED_RE.search(body)
    if unreleased:
        nxt = H2_RE.search(body, unreleased.end())
        end = nxt.start() if nxt else len(body)
        kept = body[unreleased.end():end].strip("\n")
        section = heading + "\n\n" + (kept if kept.strip() else notes)
        new = body[:unreleased.end()] + "\n\n" + section.rstrip("\n") + "\n\n" + body[end:].lstrip("\n")
    else:
        section = heading + "\n\n" + notes + "\n\n"
        first = H2_RE.search(body)
        if first:
            new = body[:first.start()] + section + body[first.start():]
        else:
            title = re.search(r"^#\s+.*$", body, re.M)
            if title:
                new = body[:title.end()] + "\n\n" + section + body[title.end():].lstrip("\n")
            else:
                new = section + body
    if not new.endswith("\n"):
        new += "\n"
    return new.replace("\n", nl)


# ─────────────────────────────── repository state ───────────────────────────────

def default_branch(root: str) -> str:
    ref = (git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root) or "").strip()
    if ref:
        return ref.split("/", 1)[-1]
    for name in ("main", "master", "develop", "trunk"):
        if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root) is not None or \
                git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"], root) is not None:
            return name
    return ""


def github_repo(root: str, remote: str = "origin") -> str:
    """OWNER/REPO (HOST/OWNER/REPO off github.com) from a remote URL, without credentials."""
    url = (git(["remote", "get-url", remote], root) or "").strip()
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/\s]+@)?([^:/\s]+)(?::\d+)?[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        return ""
    host, owner, repo = m.groups()
    return f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"


def ci_status(root: str, sha: str) -> Tuple[Optional[bool], str]:
    """(True green / False red / None unknown or running, text) for the commit's GitHub Actions runs, through gh."""
    has_gh_ci = os.path.isdir(os.path.join(root, ".github", "workflows"))
    if not has_gh_ci:
        if os.path.isfile(os.path.join(root, ".gitlab-ci.yml")):
            return None, "CI: GitLab CI — check it with /ci (ci.py status)"
        return None, "CI: no GitHub Actions workflows found — nothing to check here (other CI: /ci)"
    if not shutil.which("gh"):
        return None, "CI: gh is not installed — check CI with /ci (ci.py status), or install gh"
    code, out, err = run(["gh", "run", "list", "-c", sha, "-L", "30", "--json", "workflowName,status,conclusion,url"],
                         root, timeout=25)
    if code != 0:
        return None, f"CI: gh couldn't tell ({first_line(err or out) or f'exit {code}'}) — check with /ci"
    try:
        runs = json.loads(out or "[]")
    except ValueError:
        return None, "CI: gh returned something unreadable — check with /ci"
    if not runs:
        return None, f"CI: no runs for {sha[:7]} yet (not pushed, or workflows still starting) — /ci watch after push"
    latest: Dict[str, dict] = {}
    for r in runs:
        latest.setdefault(str(r.get("workflowName") or "workflow"), r)
    failed = [n for n, r in latest.items() if str(r.get("status")).lower() == "completed"
              and str(r.get("conclusion") or "").lower() not in ("success", "neutral", "skipped")]
    running = [n for n, r in latest.items() if str(r.get("status")).lower() != "completed"]
    if failed:
        return False, f"CI: red for {sha[:7]} — failed: {', '.join(failed)} (logs: /ci, ci.py logs)"
    if running:
        return None, f"CI: still running for {sha[:7]}: {', '.join(running)} — wait with /ci (ci.py watch)"
    return True, f"CI: green for {sha[:7]} ({', '.join(latest)})"


def tree_state(root: str) -> Tuple[List[str], List[str]]:
    """(tracked changes, untracked files) from git status."""
    out = git(["status", "--porcelain", "--untracked-files=normal"], root) or ""
    tracked, untracked = [], []
    for line in out.splitlines():
        if line.startswith("??"):
            untracked.append(line[3:])
        elif line.strip():
            tracked.append(line[3:])
    return tracked, untracked


def state_checks(root: str, fetch: bool, check_ci: bool) -> List[dict]:
    checks: List[dict] = []

    def add(ok: Optional[bool], text: str) -> None:
        checks.append({"ok": ok, "text": text})

    tracked, untracked = tree_state(root)
    if tracked:
        add(False, f"working tree has {len(tracked)} uncommitted change(s): {', '.join(tracked[:5])}"
                   + (" …" if len(tracked) > 5 else "") + " — commit or stash them before the release")
    else:
        add(True, "working tree clean" + (f" ({len(untracked)} untracked file(s))" if untracked else ""))
    branch = (git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "").strip()
    main = default_branch(root)
    if branch == "HEAD":
        add(False, "detached HEAD — release from the default branch" + (f" ({main})" if main else ""))
    elif not main:
        add(None, f"on {branch}; the default branch is unknown (no origin/HEAD, no main/master)")
    elif branch == main:
        add(True, f"on {branch} (the default branch)")
    else:
        add(False, f"on {branch}, not the default branch {main} — merge first (/pr), or confirm releasing from {branch}")
    fetched = ""
    if fetch:
        code, _, err = run(["git", "fetch", "--quiet", "--tags", "--prune"], root, timeout=60)
        fetched = "just fetched" if code == 0 else f"fetch failed: {first_line(err)}"
    upstream = (git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root) or "").strip()
    if not upstream:
        add(False if branch != "HEAD" else None, "no upstream branch — push it first (git push -u origin <branch>)")
    else:
        counts = (git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], root) or "").split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        when = fetched or "as of the last fetch; --fetch refreshes"
        if ahead or behind:
            add(False, f"{ahead} ahead / {behind} behind {upstream} ({when}) — "
                       + ("push" if ahead and not behind else "pull" if behind and not ahead else "sync") + " first")
        else:
            add(True, f"in sync with {upstream} ({when})")
    if check_ci:
        sha = (git(["rev-parse", "HEAD"], root) or "").strip()
        ok, text = ci_status(root, sha) if sha else (None, "CI: no commit yet")
        add(ok, text)
    else:
        add(None, "CI: not checked (--no-ci) — /ci status")
    return checks


# ─────────────────────────────── commands ───────────────────────────────

def project_root(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path or "."))
    return repo_root(path) or path


def source_line(s: dict) -> str:
    count = len(s["spans"])
    return f"{s['file']}  {s['label']}{f' ×{count}' if count > 1 else ''}  {s['value']}"


def build_plan(root: str, fetch: bool, check_ci: bool) -> dict:
    sources, notes = find_sources(root)
    cur, odd = current_version(sources)
    plan: dict = {"root": root, "sources": [dict({k: v for k, v in s.items() if k != "spans"}, count=len(s["spans"]))
                                            for s in sources],
                  "mismatches": [source_line(s) for s in odd], "left_alone": notes,
                  "current": fmt(cur) if cur else None}
    is_git = repo_root(root) is not None
    plan["git"] = is_git
    if not is_git:
        return plan
    lt = last_tag(root)
    cs = commits(root, lt[0] if lt else "")
    prefix = tag_prefix(root)
    plan["last_tag"] = lt[0] if lt else None
    plan["commits_since"] = len(cs)
    plan["tag_prefix"] = prefix
    part, why = suggest(cs, cur)
    suggestion: dict = {"part": part, "reason": why, "version": None}
    if lt is None:
        suggestion["reason"] = ("no release tag yet — the first release can be the current version as is "
                                f"({prefix}{fmt(cur)})" if cur else "no release tag yet") + "; commits: " + why
    if cur and part:
        if lt and vkey(cur) > vkey(lt[1]) and not tag_exists(root, prefix + fmt(cur)):
            suggestion["part"] = None
            suggestion["version"] = fmt(cur)
            suggestion["reason"] = (f"the version ({fmt(cur)}) is already ahead of the last tag {lt[0]} — the bump looks "
                                    f"done; tag it as is. Commits: " + why)
        else:
            suggestion["version"] = fmt(bump_version(cur, part))
    plan["suggested"] = suggestion
    cl = changelog_path(root)
    if cl:
        text = read_text(cl) or ""
        un = UNRELEASED_RE.search(text.replace("\r\n", "\n"))
        plan["changelog"] = {"path": os.path.relpath(cl, root).replace(os.sep, "/"), "unreleased": bool(un)}
    else:
        plan["changelog"] = None
    plan["state"] = state_checks(root, fetch, check_ci)
    plan["notes"] = notes_md(cs, lt[0] if lt else "")
    newer = [t for t, v in release_tags(root) if lt is None or vkey(v) > vkey(lt[1])]
    plan["unmerged_newer_tags"] = newer[:5]
    return plan


def render_plan(p: dict) -> str:
    mark = {True: "✓", False: "✗", None: "?"}
    out = [f"Repository: {p['root']}"]
    if p["sources"]:
        out.append(f"Version sources ({len(p['sources'])}):")
        out.extend("  " + source_line(dict(s, spans=[0] * s["count"])) for s in p["sources"])
    else:
        out.append("Version sources: none found (package.json, pyproject.toml, setup.cfg, Cargo.toml, plugin.json/"
                   "manifest.json, VERSION, __version__, versionName, MARKETING_VERSION)")
    if p["mismatches"]:
        out.append(f"  ⚠ mismatch — most sources say {p['current']}, these don't:")
        out.extend("    " + m for m in p["mismatches"])
    out.extend("  · " + n for n in p["left_alone"])
    if not p["git"]:
        out.append("Not a git repository — tags, commits and state need git (/git-init).")
        return "\n".join(out)
    lt = p["last_tag"]
    out.append(f"Last release tag: {lt} ({p['commits_since']} commit(s) since)" if lt
               else f"Last release tag: none ({p['commits_since']} commit(s) in history)")
    if p["unmerged_newer_tags"]:
        out.append(f"  ⚠ newer release tags not in HEAD's history: {', '.join(p['unmerged_newer_tags'])}")
    s = p["suggested"]
    if s["part"]:
        out.append(f"Suggested bump: {s['part']} → {s['version'] or '?'} — {s['reason']}")
    elif s["version"]:
        out.append(f"Suggested: release {p['tag_prefix']}{s['version']} as is — {s['reason']}")
    else:
        out.append(f"Suggested bump: none — {s['reason']}")
    cl = p["changelog"]
    out.append(f"Changelog: {cl['path']}" + (" (has an [Unreleased] section — bump turns it into the release)"
                                              if cl["unreleased"] else " — bump adds a section on top")
               if cl else "Changelog: no CHANGELOG.md — bump won't create one; notes go into the GitHub release")
    out.append("State:")
    out.extend(f"  {mark[c['ok']]} {c['text']}" for c in p["state"])
    out.append("Draft notes:")
    out.extend("  " + ln if ln else "" for ln in p["notes"].rstrip("\n").splitlines())
    out.append("Next: agree on the version with the user → bump → tests → /commit → push and /ci → tag → "
               "publish only on the user's request")
    return "\n".join(out)


def cmd_plan(a) -> int:
    root = project_root(a.path)
    p = build_plan(root, a.fetch, not a.no_ci)
    print(json.dumps(p, ensure_ascii=False, indent=2) if a.json else render_plan(p))
    return 0


def cmd_bump(a) -> int:
    root = project_root(a.path)
    sources, notes = find_sources(root)
    if not sources:
        print("No version source found — nothing to bump. Looked for: package.json, pyproject.toml, setup.cfg, "
              "Cargo.toml, plugin.json/manifest.json, VERSION, __version__, versionName, MARKETING_VERSION.")
        return 1
    cur, odd = current_version(sources)
    if a.target in PARTS:
        if odd:
            print("The version sources disagree, so a relative bump has no single base:")
            print("\n".join("  " + source_line(s) for s in sources))
            print("Pass the version explicitly: release.py bump X.Y.Z — every source gets it.")
            return 1
        new = bump_version(cur, a.target)  # type: ignore[arg-type]
    else:
        parsed = parse_version(a.target) if FULL_RE.match(a.target) else None
        if parsed is None:
            print(f"{a.target!r} is neither major/minor/patch nor a version like 1.2.3 (or 1.2.3-rc.1).")
            return 1
        highest = max((parse_version(s["value"]) for s in sources), key=vkey)  # type: ignore[arg-type]
        if vkey(parsed) <= vkey(highest) and not (odd and vkey(parsed) == vkey(highest)):
            print(f"{fmt(parsed)} is not higher than the current version {fmt(highest)} — pass a higher one.")
            return 1
        new = parsed
    version = fmt(new)
    is_git = repo_root(root) is not None
    prefix = tag_prefix(root) if is_git else "v"
    if is_git and tag_exists(root, prefix + version):
        print(f"Tag {prefix}{version} already exists — that version is released; pick another.")
        return 1
    edits: Dict[str, List[Tuple[int, int, str]]] = {}
    lines = []
    for s in sources:
        value = ("v" if s["value"].startswith("v") else "") + version
        edits.setdefault(s["file"], []).extend((st, en, value) for st, en in s["spans"])
        if s["value"] != value:
            lines.append(f"  {source_line(s)} → {value}")
    cl = changelog_path(root) if not a.no_changelog else None
    cl_text, cl_new = "", ""
    if cl:
        lt = last_tag(root) if is_git else None
        cs = commits(root, lt[0] if lt else "") if is_git else []
        today = datetime.date.today().isoformat()
        cl_text = read_text(cl) or ""
        cl_new = changelog_update(cl_text, version, notes_md(cs, lt[0] if lt else "") if cs else "", today)
    tracked = tree_state(root)[0] if is_git else []
    head = "Would set" if a.dry_run else "Set"
    print(f"{head} version {fmt(cur) if cur and not odd else '(mixed)'} → {version}:")
    print("\n".join(lines) if lines else "  (every source already says it)")
    if cl:
        old_lines = set(cl_text.replace("\r\n", "\n").split("\n"))
        added = [ln for ln in cl_new.replace("\r\n", "\n").split("\n") if ln not in old_lines]
        print(f"  {os.path.relpath(cl, root).replace(os.sep, '/')}: new section {added[0] if added else ''}")
        if a.dry_run and len(added) > 1:
            print("\n".join("    " + ln for ln in added[1:40] if ln.strip()))
    for n in notes:
        print("  · " + n)
    if a.dry_run:
        return 0
    for rel, spans in edits.items():
        path = os.path.join(root, *rel.split("/"))
        text = read_text(path)
        if text is None:
            print(f"Couldn't read {rel} — nothing written.")
            return 1
        for st, en, value in sorted(spans, reverse=True):
            text = text[:st] + value + text[en:]
        write_text(path, text)
    if cl:
        write_text(cl, cl_new)
    if tracked:
        print(f"⚠ The tree already had {len(tracked)} other uncommitted change(s) — commit them separately from the bump.")
    print(f"Next: run the tests, review `git diff`, then commit (/commit, e.g. \"chore(release): {prefix}{version}\"); "
          f"after push and green CI: release.py tag.")
    return 0


def cmd_notes(a) -> int:
    root = repo_root(os.path.abspath(os.path.expanduser(a.path or ".")))
    if not root:
        print("Not a git repository.")
        return 2
    until_sha = git(["rev-parse", f"{a.until}^{{commit}}"], root)
    if until_sha is None:
        print(f"Unknown revision {a.until!r}.")
        return 1
    since = a.since
    if since is None:  # the newest release tag before `until` (a tag on `until` itself is the release being described)
        tags = [t for t, _ in release_tags(root, a.until) if git(["rev-parse", f"{t}^{{commit}}"], root) != until_sha]
        since = tags[0] if tags else ""
    md = notes_md(commits(root, since, a.until), since)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
        print(f"Wrote {a.out}")
    else:
        sys.stdout.write(md)
    return 0


def resolve_version(root: str, given: Optional[str]) -> Tuple[Optional[str], str]:
    sources, _ = find_sources(root)
    cur, odd = current_version(sources)
    if given:
        v = parse_version(given) if FULL_RE.match(given) else None
        if v is None:
            return None, f"{given!r} is not a version like 1.2.3"
        if cur and not odd and vkey(v) != vkey(cur):
            return None, f"the version sources say {fmt(cur)}, not {fmt(v)} — bump first (release.py bump {fmt(v)})"
        return fmt(v), ""
    if cur is None:
        return None, "no version source found — pass --version X.Y.Z"
    if odd:
        return None, "the version sources disagree (release.py plan shows them) — fix that, or pass --version"
    return fmt(cur), ""


def cmd_tag(a) -> int:
    root = repo_root(os.path.abspath(os.path.expanduser(a.path or ".")))
    if not root:
        print("Not a git repository.")
        return 2
    version, err = resolve_version(root, a.version)
    if not version:
        print(f"Refused: {err}.")
        return 1
    tag = tag_prefix(root) + version
    if tag_exists(root, tag):
        print(f"Refused: tag {tag} already exists (git show {tag}).")
        return 1
    tracked = tree_state(root)[0]
    if tracked:
        print(f"Refused: {len(tracked)} uncommitted change(s) ({', '.join(tracked[:5])}) — the tag would miss them; "
              "commit the release first (/commit).")
        return 1
    msg = a.message or f"Release {tag}"
    cmd = ["git", "tag", "-a", tag, "-m", msg]
    sha = (git(["rev-parse", "--short", "HEAD"], root) or "").strip()
    if a.dry_run:
        print(f"Would run: git tag -a {tag} -m {json.dumps(msg, ensure_ascii=False)}   (on {sha})")
        return 0
    code, out, err2 = run(cmd, root)
    if code != 0:
        print(f"git tag failed: {(err2 or out).strip()}")
        return 1
    print(f"Created the annotated tag {tag} on {sha} (local only). Undo: git tag -d {tag}.")
    print("Publishing (push the tag + GitHub release) is outward-facing: only on the user's request — "
          "release.py publish --yes.")
    return 0


def cmd_publish(a) -> int:
    root = repo_root(os.path.abspath(os.path.expanduser(a.path or ".")))
    if not root:
        print("Not a git repository.")
        return 2
    version, err = resolve_version(root, a.version)
    if not version:
        print(f"Refused: {err}.")
        return 1
    tag = tag_prefix(root) + version
    if not tag_exists(root, tag):
        print(f"Refused: there is no local tag {tag} — create it first: release.py tag.")
        return 1
    if git(["remote", "get-url", a.remote], root) is None:
        print(f"Refused: no remote {a.remote!r} (git remote -v).")
        return 1
    if not shutil.which("gh"):
        print(f"Refused: gh is not installed, so the GitHub release can't be created from here. Nothing was pushed.\n"
              f"Install gh (https://cli.github.com) and run `gh auth login` yourself, or push the tag "
              f"(git push {a.remote} {tag}) and create the release on GitHub's Releases page.")
        return 1
    code, out, err2 = run(["gh", "auth", "status"], root, timeout=20)
    if code != 0:
        print(f"Refused: gh is not signed in ({first_line(err2 or out)}). Nothing was pushed. "
              "The user runs `gh auth login` themselves.")
        return 1
    sha = (git(["rev-parse", f"{tag}^{{commit}}"], root) or "").strip()
    on_remote = (git(["branch", "-r", "--contains", sha], root) or "").split()
    on_remote = [b for b in on_remote if b.startswith(a.remote + "/") and b != "->" and not b.endswith("/HEAD")]
    if not on_remote:
        print(f"Refused: {tag}'s commit {sha[:7]} isn't on any {a.remote} branch (as of the last fetch) — push the "
              "branch first and let CI finish (/ci), then publish.")
        return 1
    ok, ci_text = ci_status(root, sha)
    if ok is False:
        print(f"Refused: {ci_text}. Fix CI before releasing.")
        return 1
    repo = github_repo(root, a.remote)
    title = a.title or tag
    prerelease = a.prerelease or "-" in version
    gh_cmd = ["gh", "release", "create", tag, "--title", title, "--verify-tag"] + (["-R", repo] if repo else []) \
        + (["--draft"] if a.draft else []) + (["--prerelease"] if prerelease else [])
    notes_file = os.path.abspath(os.path.expanduser(a.notes_file)) if a.notes_file else ""
    if a.notes_file and not os.path.isfile(notes_file):
        print(f"Notes file not found: {notes_file} — nothing published.")
        return 1
    notes_desc = notes_file or "<notes generated from the commits since the previous tag>"
    preview = [f"  git push -q {a.remote} refs/tags/{tag}",
               "  " + " ".join(json.dumps(x, ensure_ascii=False) if " " in x else x for x in gh_cmd)
               + f" --notes-file {notes_desc}",
               f"  ({ci_text}; on {', '.join(on_remote[:3])})"]
    if not a.yes:
        print("Publishing is outward-facing (everyone sees the tag and the release). Would run:")
        print("\n".join(preview))
        print("Refused without --yes: run it only on the user's explicit request (or under full access), "
              "then add --yes.")
        return 3
    tmp_dir = None
    if not notes_file:
        prev = [t for t, v in release_tags(root, tag) if t != tag and vkey(v) < vkey(parse_version(version))]  # type: ignore[arg-type]
        tmp_dir = tempfile.mkdtemp(prefix="agp-release-")
        notes_file = os.path.join(tmp_dir, "notes.md")
        with open(notes_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(notes_md(commits(root, prev[0] if prev else "", tag), prev[0] if prev else ""))
    try:
        code, out, err2 = run(["git", "push", "-q", a.remote, f"refs/tags/{tag}"], root, timeout=120)
        if code != 0:
            print(f"git push of {tag} failed: {(err2 or out).strip()} — nothing published.")
            return 1
        print(f"Pushed {tag} to {a.remote}.")
        code, out, err2 = run(gh_cmd + ["--notes-file", notes_file], root, timeout=120)
        if code != 0:
            print(f"gh release create failed: {(err2 or out).strip()}\nThe tag is pushed; retry the release with "
                  f"gh release create {tag} --notes-file <file>, or create it on GitHub. (Removing the pushed tag "
                  f"would be `git push {a.remote} :refs/tags/{tag}` — only if the user asks.)")
            return 1
        url = first_line(out)
        print(f"Released {tag}" + (f": {url}" if url else "") + (" (draft)" if a.draft else ""))
        return 0
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="release.py", description="Cut a release of any project safely.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan", help="what a release would be (read-only)")
    sp.add_argument("--path", default=".")
    sp.add_argument("--fetch", action="store_true", help="git fetch first (network)")
    sp.add_argument("--no-ci", action="store_true", help="skip the gh CI check (network)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("bump", help="set every version source to the new version")
    sp.add_argument("target", help="major, minor, patch or X.Y.Z")
    sp.add_argument("--path", default=".")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-changelog", action="store_true")
    sp = sub.add_parser("notes", help="markdown release notes")
    sp.add_argument("--since", default=None, help="tag or ref (default: the last release tag)")
    sp.add_argument("--until", default="HEAD")
    sp.add_argument("--out", default="")
    sp.add_argument("--path", default=".")
    sp = sub.add_parser("tag", help="local annotated tag for the current version")
    sp.add_argument("--version", default=None)
    sp.add_argument("--message", default="")
    sp.add_argument("--path", default=".")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("publish", help="push the tag and create the GitHub release (needs --yes)")
    sp.add_argument("--version", default=None)
    sp.add_argument("--remote", default="origin")
    sp.add_argument("--title", default="")
    sp.add_argument("--notes-file", default="")
    sp.add_argument("--draft", action="store_true")
    sp.add_argument("--prerelease", action="store_true")
    sp.add_argument("--path", default=".")
    sp.add_argument("--yes", action="store_true", help="the user explicitly asked to publish")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "bump": cmd_bump, "notes": cmd_notes, "tag": cmd_tag, "publish": cmd_publish}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
