#!/usr/bin/env python3
"""Antigravity Plus — /diff-cover: test coverage over git-changed lines.

  diff_cover.py [--workspace DIR] [--base REF] [--coverage PATH] [--min PCT] [--json]

Supports Cobertura XML (`coverage.xml`, `cobertura.xml`, `reports/coverage.xml`), LCOV
(`coverage/lcov.info`, `lcov.info`), Go coverprofile (`coverage.out`, `cover.out`), and
Xcode `.xcresult` bundles on macOS (`xcrun xccov`).

Exit codes:
  0 — diff coverage computed (and >= --min if specified)
  1 — diff coverage is below --min PCT
  2 — invalid workspace / not a git repo / invalid ref / missing or unreadable coverage report

Stdlib only, Python 3.8+; macOS, Linux, and Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"

QUIET_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "NO_COLOR": "1",
    "GIT_EDITOR": "true",
    "LC_ALL": "C",
    "LANG": "C",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "Pods",
    "Carthage",
    ".build",
    "DerivedData",
}

COVERAGE_CANDIDATES = (
    "coverage.xml",
    "cobertura.xml",
    "reports/coverage.xml",
    "coverage/coverage.xml",
    "coverage/lcov.info",
    "lcov.info",
    "coverage.out",
    "cover.out",
)
COVERAGE_FILENAMES = {"coverage.xml", "cobertura.xml", "lcov.info", "coverage.out", "cover.out"}
COVERAGE_ARTIFACT_BASENAMES = COVERAGE_FILENAMES | {".coverage"}
COVERAGE_ARTIFACT_DIRS = {"htmlcov", ".nyc_output"}  # coverage.py's HTML report, nyc's raw data

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GO_LINE_RE = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+)\s+(\d+)\s+(\d+)\s*$")
GO_MOD_RE = re.compile(r'^\s*module\s+(?:"([^"]+)"|(\S+))')
WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


class UserError(Exception):
    """Actionable failure that exits with status 2."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _to_posix(path: str) -> str:
    return path.replace("\\", "/")


def _strip_dot_slash(path: str) -> str:
    """`./src/a.py` → `src/a.py`. Only a `./` prefix goes: the dot of `.coverage` or `.github/…` is the name's."""
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_abs_any(path: str) -> bool:
    return path.startswith("/") or bool(WIN_DRIVE_RE.match(path))


def _norm_key(path: str) -> str:
    return path.lower() if WINDOWS else path


def run_cmd(cmd: List[str], cwd: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            env=dict(os.environ, **QUIET_ENV),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout:.0f} s"
    except OSError as exc:
        return 127, "", str(exc)


def git_stdout(args: List[str], cwd: str, timeout: float = 30.0) -> Optional[str]:
    code, out, _ = run_cmd(["git", "-c", "core.quotePath=false"] + args, cwd=cwd, timeout=timeout)
    return out if code == 0 else None


def find_git_root(workspace: str) -> Optional[str]:
    if not shutil.which("git"):
        raise UserError("`git` is not installed or not on PATH.")
    out = git_stdout(["rev-parse", "--show-toplevel"], cwd=workspace)
    if not out or not out.strip():
        return None
    return os.path.realpath(out.strip())


def has_head(cwd: str) -> bool:
    code, _, _ = run_cmd(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=cwd)
    return code == 0


def is_branch_ref(ref: str, cwd: str) -> bool:
    if ref.startswith("origin/"):
        return True
    for prefix in ("refs/heads/", "refs/remotes/"):
        code, _, _ = run_cmd(["git", "show-ref", "--verify", "--quiet", prefix + ref], cwd=cwd)
        if code == 0:
            return True
    return False


def is_coverage_artifact(rel_path: str, cov_rel: Optional[str]) -> bool:
    norm = posixpath.normpath(_strip_dot_slash(_to_posix(rel_path)))
    if cov_rel:
        if norm == cov_rel or norm.startswith(cov_rel.rstrip("/") + "/"):
            return True
        cov_dir = posixpath.dirname(cov_rel)
        if posixpath.basename(cov_dir) == "coverage" and norm.startswith(cov_dir + "/"):
            return True  # jest / vitest / c8 write their other reports next to coverage/lcov.info
    parts = norm.split("/")
    if parts[-1] in COVERAGE_ARTIFACT_BASENAMES or parts[-1].startswith(".coverage."):
        return True  # .coverage and the .coverage.<host>.<pid>.<n> files of parallel runs
    return any(part.endswith(".xcresult") for part in parts) or any(p in COVERAGE_ARTIFACT_DIRS for p in parts[:-1])


def _missing_coverage_hint() -> str:
    lines = [
        "Generate a coverage report and re-run diff_cover.py:",
        "  - Python (pytest-cov):  pytest --cov --cov-report=xml           -> coverage.xml",
        "  - JS/TS (vitest/jest):  npx vitest run --coverage               -> coverage/lcov.info",
        "  - Go:                   go test -coverprofile=coverage.out ./... -> coverage.out",
    ]
    if MAC:
        lines.append(
            "  - Xcode (macOS):        xcodebuild test -enableCodeCoverage YES -resultBundlePath TestResults.xcresult"
        )
    return "\n".join(lines)


def _looks_like_cobertura(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096).lower()
        return "<coverage" in head
    except OSError:
        return False


def _entry_mtime_ns(path: str) -> int:
    try:
        best = os.stat(path).st_mtime_ns
        if os.path.isdir(path):
            info = os.path.join(path, "Info.plist")
            if os.path.isfile(info):
                best = max(best, os.stat(info).st_mtime_ns)
        return best
    except OSError:
        return 0


def discover_coverage(workspace: str, explicit_path: Optional[str]) -> Tuple[str, str]:
    """Return (absolute_path, format) for the coverage report."""
    if explicit_path:
        raw = os.path.expanduser(explicit_path)
        candidates = [raw] if os.path.isabs(raw) else [raw, os.path.join(workspace, raw)]
        found = next((os.path.realpath(c) for c in candidates if os.path.exists(c)), None)
        if not found:
            raise UserError(f"Coverage report not found: {explicit_path}", _missing_coverage_hint())
        fmt = detect_format(found)
        if not fmt:
            raise UserError(f"Unrecognized coverage report format: {explicit_path}", _missing_coverage_hint())
        return found, fmt

    priority = {rel: i for i, rel in enumerate(COVERAGE_CANDIDATES)}
    found_map: Dict[str, Tuple[int, int, str]] = {}

    def record(full_path: str, rel_posix: str) -> None:
        real = os.path.realpath(full_path)
        if real in found_map:
            return
        fmt = detect_format(real, strict_xml=True)
        if not fmt:
            return
        prio = priority.get(rel_posix, len(COVERAGE_CANDIDATES) + (1 if fmt == "xcresult" else 0))
        found_map[real] = (_entry_mtime_ns(real), -prio, fmt)

    for rel in COVERAGE_CANDIDATES:
        full = os.path.join(workspace, *rel.split("/"))
        if os.path.isfile(full):
            record(full, rel)

    base_depth = workspace.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(workspace):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        kept_dirs: List[str] = []
        for d in sorted(dirs):
            if MAC and d.endswith(".xcresult"):
                rel_dir = _to_posix(os.path.relpath(os.path.join(root, d), workspace))
                record(os.path.join(root, d), rel_dir)
                continue  # never descend inside .xcresult bundles
            if d in SKIP_DIRS or d.startswith("."):
                continue
            if depth < 3:
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if name in COVERAGE_FILENAMES:
                full = os.path.join(root, name)
                rel_file = _to_posix(os.path.relpath(full, workspace))
                record(full, rel_file)

    if not found_map:
        raise UserError(f"No coverage report found in {workspace}.", _missing_coverage_hint())

    best_path, (_mtime, _neg_prio, best_fmt) = max(
        found_map.items(),
        key=lambda item: (item[1][0], item[1][1]),
    )
    return best_path, best_fmt


def detect_format(path: str, strict_xml: bool = False) -> Optional[str]:
    if os.path.isdir(path):
        return "xcresult" if path.rstrip("/\\").lower().endswith(".xcresult") else None
    if not os.path.isfile(path):
        return None
    lower = os.path.basename(path).lower()
    if lower.endswith(".xml"):
        if strict_xml and not _looks_like_cobertura(path):
            return None
        return "cobertura"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            sample = fh.read(8192)
    except OSError:
        return None
    stripped = sample.lstrip()
    if stripped.startswith("<?xml") or stripped.lower().startswith("<coverage"):
        return "cobertura"
    for line in sample.split("\n"):
        s = line.rstrip("\r").strip()
        if not s:
            continue
        if s.startswith("mode:") or GO_LINE_RE.match(s):
            return "gocover"
        if s.startswith(("TN:", "SF:", "DA:", "FN:")) or s == "end_of_record":
            return "lcov"
    if lower.endswith(".info") or "lcov" in lower:
        return "lcov"
    if lower.endswith(".out"):
        return "gocover"
    return None


class PathResolver:
    """Normalize coverage file paths cleanly to workspace-relative POSIX paths."""

    def __init__(self, workspace: str, git_root: str, cov_path: str) -> None:
        self.workspace = workspace
        self.workspace_real = os.path.realpath(workspace)
        self.git_root = git_root
        self.git_root_real = os.path.realpath(git_root)

        self._ws_prefixes = self._unique_prefixes(workspace, self.workspace_real)
        self._git_prefixes = self._unique_prefixes(git_root, self.git_root_real)

        try:
            ws_rel = os.path.relpath(self.workspace_real, self.git_root_real)
            self.ws_rel_from_git = "" if ws_rel == "." else _to_posix(ws_rel)
        except ValueError:
            self.ws_rel_from_git = ""

        cov_dir = os.path.dirname(cov_path)
        self.cov_dir_rel = ""
        rel_cov_dir = self._strip_ws_prefix(_to_posix(cov_dir))
        if rel_cov_dir and rel_cov_dir != ".":
            self.cov_dir_rel = rel_cov_dir

        self.go_modules = self._find_go_modules(workspace)
        self.known_files = self._collect_known_files(workspace)
        self._known_lower = {p.lower(): p for p in self.known_files} if WINDOWS else {}

    @staticmethod
    def _unique_prefixes(*dirs: str) -> List[str]:
        seen: List[str] = []
        for d in dirs:
            p = _to_posix(os.path.abspath(d)).rstrip("/")
            if p and _norm_key(p) not in [_norm_key(x) for x in seen]:
                seen.append(p)
        return seen

    @staticmethod
    def _strip_prefix(cand: str, prefixes: List[str]) -> Optional[str]:
        cand_clean = posixpath.normpath(cand)
        key_cand = _norm_key(cand_clean)
        for pref in prefixes:
            key_pref = _norm_key(pref)
            if key_cand.startswith(key_pref + "/"):
                return cand_clean[len(pref) + 1 :]
        return None

    def _strip_ws_prefix(self, cand: str) -> Optional[str]:
        rel = self._strip_prefix(cand, self._ws_prefixes)
        if rel is not None:
            return rel
        git_rel = self._strip_prefix(cand, self._git_prefixes)
        if git_rel is not None and self.ws_rel_from_git:
            if _norm_key(git_rel).startswith(_norm_key(self.ws_rel_from_git) + "/"):
                return git_rel[len(self.ws_rel_from_git) + 1 :]
        return git_rel if not self.ws_rel_from_git else None

    @staticmethod
    def _find_go_modules(workspace: str) -> List[Tuple[str, str]]:
        mods: List[Tuple[str, str]] = []
        base_depth = workspace.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(workspace):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS and not d.startswith(".") and depth < 3]
            if "go.mod" in files:
                try:
                    with open(os.path.join(root, "go.mod"), encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            m = GO_MOD_RE.match(line)
                            if m:
                                mod_name = (m.group(1) or m.group(2) or "").strip()
                                if mod_name:
                                    rel = os.path.relpath(root, workspace)
                                    sub = "" if rel == "." else _to_posix(rel)
                                    mods.append((mod_name, sub))
                                break
                except OSError:
                    pass
        mods.sort(key=lambda item: len(item[0]), reverse=True)
        return mods

    @staticmethod
    def _collect_known_files(workspace: str) -> Set[str]:
        known: Set[str] = set()
        for args in (["ls-files", "-z"], ["ls-files", "-z", "--others", "--exclude-standard"]):
            out = git_stdout(args, cwd=workspace)
            if out:
                for p in out.split("\0"):
                    if p:
                        known.add(posixpath.normpath(_strip_dot_slash(_to_posix(p))))
        return known

    def _exists_in_workspace(self, rel_posix: str) -> Optional[str]:
        if not rel_posix or rel_posix in (".", "..") or rel_posix.startswith("../"):
            return None
        if rel_posix in self.known_files:
            return rel_posix
        if WINDOWS and rel_posix.lower() in self._known_lower:
            return self._known_lower[rel_posix.lower()]
        full = os.path.join(self.workspace, *rel_posix.split("/"))
        if os.path.isfile(full):
            return rel_posix
        return None

    def _canon_rel(self, rel_posix: str) -> str:
        norm = posixpath.normpath(rel_posix)
        if WINDOWS and norm.lower() in self._known_lower:
            return self._known_lower[norm.lower()]
        return norm

    def _resolve_candidate(self, cand: str, use_go_mods: bool) -> Optional[str]:
        cand = _to_posix(cand.strip())
        if not cand:
            return None

        if _is_abs_any(cand):
            stripped = self._strip_ws_prefix(cand)
            if stripped is not None:
                return self._canon_rel(stripped)
            # Symlink-aware check when path exists on disk (e.g. macOS /tmp -> /private/tmp)
            if os.path.exists(cand):
                real_cand = _to_posix(os.path.realpath(cand))
                stripped_real = self._strip_ws_prefix(real_cand)
                if stripped_real is not None:
                    return self._canon_rel(stripped_real)
                return None  # existing file on disk outside workspace: never suffix-match

        if use_go_mods:
            for mod_name, sub_dir in self.go_modules:
                if cand == mod_name or cand.startswith(mod_name + "/"):
                    rest = cand[len(mod_name) :].lstrip("/")
                    joined = posixpath.normpath(f"{sub_dir}/{rest}" if sub_dir else rest)
                    return self._canon_rel(joined)

        if not _is_abs_any(cand):
            norm = posixpath.normpath(_strip_dot_slash(cand))
            hit = self._exists_in_workspace(norm)
            if hit:
                return hit
            if self.cov_dir_rel:
                sub = posixpath.normpath(f"{self.cov_dir_rel}/{norm}")
                hit_sub = self._exists_in_workspace(sub)
                if hit_sub:
                    return hit_sub

        # Progressive suffix match (CI container prefixes or Go paths without local go.mod)
        parts = [p for p in posixpath.normpath(cand).split("/") if p and p != "."]
        if WIN_DRIVE_RE.match(cand) and parts and parts[0].endswith(":"):
            parts = parts[1:]
        for idx in range(1, len(parts)):
            suffix = "/".join(parts[idx:])
            hit = self._exists_in_workspace(suffix)
            if hit:
                return hit
        return None

    def resolve(self, raw_path: str, extra_prefixes: Iterable[str] = (), use_go_mods: bool = False) -> str:
        raw_posix = _to_posix(raw_path.strip())
        candidates: List[str] = []
        if not _is_abs_any(raw_posix):
            for pref in extra_prefixes:
                pref_posix = _to_posix(pref.strip())
                if not pref_posix:
                    continue
                if _is_abs_any(pref_posix):
                    candidates.append(posixpath.normpath(pref_posix.rstrip("/") + "/" + _strip_dot_slash(raw_posix)))
                else:
                    rel_pref = posixpath.normpath(pref_posix.strip("/"))
                    joined = posixpath.normpath(
                        raw_posix if rel_pref in ("", ".") else f"{rel_pref}/{_strip_dot_slash(raw_posix)}"
                    )
                    candidates.append(joined)
        candidates.append(raw_posix)

        for cand in candidates:
            resolved = self._resolve_candidate(cand, use_go_mods=use_go_mods)
            if resolved is not None:
                return resolved

        # Fallback: best normalized relative form
        cleaned = posixpath.normpath(_strip_dot_slash(raw_posix))
        if _is_abs_any(cleaned):
            stripped = self._strip_ws_prefix(cleaned)
            if stripped is not None:
                return self._canon_rel(stripped)
        return cleaned


def _merge_hits(target: Dict[str, Dict[int, int]], rel_path: str, line_hits: Dict[int, int]) -> None:
    bucket = target.setdefault(rel_path, {})
    for ln, hits in line_hits.items():
        if ln > 0:
            prev = bucket.get(ln, 0)
            bucket[ln] = max(prev, max(0, hits))


def parse_cobertura(path: str, resolver: PathResolver) -> Dict[str, Dict[int, int]]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise UserError(f"Failed to parse Cobertura XML {path}: {exc}")
    root = tree.getroot()
    tag = root.tag.split("}")[-1].lower()
    if tag != "coverage":
        raise UserError(f"Expected <coverage> root in Cobertura XML {path}, got <{root.tag}>.")

    sources = [el.text.strip() for el in root.findall("./sources/source") if el.text and el.text.strip()]
    cov: Dict[str, Dict[int, int]] = {}
    for cls in root.findall(".//class"):
        filename = (cls.attrib.get("filename") or "").strip()
        if not filename:
            continue
        rel = resolver.resolve(filename, extra_prefixes=sources)
        line_els = cls.findall("./lines/line")
        if not line_els:
            line_els = cls.findall(".//lines/line")
        hits_map: Dict[int, int] = {}
        for el in line_els:
            num_s = el.attrib.get("number")
            hits_s = el.attrib.get("hits")
            if num_s is None or hits_s is None:
                continue
            try:
                ln = int(num_s)
                hits = int(float(hits_s))
            except ValueError:
                continue
            if ln > 0:
                hits_map[ln] = max(hits_map.get(ln, 0), max(0, hits))
        _merge_hits(cov, rel, hits_map)
    return cov


def parse_lcov(path: str, resolver: PathResolver) -> Dict[str, Dict[int, int]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise UserError(f"Failed to read LCOV file {path}: {exc}")

    cov: Dict[str, Dict[int, int]] = {}
    current_sf: Optional[str] = None
    current_lines: Dict[int, int] = {}

    def flush() -> None:
        nonlocal current_sf, current_lines
        if current_sf is not None:
            rel = resolver.resolve(current_sf)
            _merge_hits(cov, rel, current_lines)
        current_sf = None
        current_lines = {}

    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("SF:"):
            if current_sf is not None:
                flush()
            current_sf = line[3:].strip()
            current_lines = {}
        elif line.startswith("DA:") and current_sf is not None:
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    ln = int(parts[0].strip())
                    hits = int(float(parts[1].strip()))
                except ValueError:
                    continue
                if ln > 0:
                    current_lines[ln] = max(current_lines.get(ln, 0), max(0, hits))
        elif line == "end_of_record":
            flush()
    flush()
    return cov


def parse_gocover(path: str, resolver: PathResolver) -> Dict[str, Dict[int, int]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise UserError(f"Failed to read Go coverprofile {path}: {exc}")

    cov: Dict[str, Dict[int, int]] = {}
    saw_header_or_entry = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r").strip()
        if not line:
            continue
        if line.startswith("mode:"):
            saw_header_or_entry = True
            continue
        m = GO_LINE_RE.match(line)
        if not m:
            if not saw_header_or_entry:
                raise UserError(f"Invalid Go coverprofile line in {path}: {line}")
            continue
        saw_header_or_entry = True
        raw_file = m.group(1)
        start_line, _start_col = int(m.group(2)), int(m.group(3))
        end_line, end_col = int(m.group(4)), int(m.group(5))
        num_stmt, count = int(m.group(6)), int(m.group(7))
        if start_line <= 0 or end_line < start_line:
            continue
        rel = resolver.resolve(raw_file, use_go_mods=True)
        cov.setdefault(rel, {})
        if num_stmt <= 0:
            continue
        last_line = end_line - 1 if (end_line > start_line and end_col == 1) else end_line
        hits = max(0, count)
        bucket = cov[rel]
        for ln in range(start_line, last_line + 1):
            bucket[ln] = max(bucket.get(ln, 0), hits)
    return cov


def _extract_xccov_lines(entries: Any) -> Dict[int, int]:
    out: Dict[int, int] = {}
    if not isinstance(entries, list):
        return out
    for item in entries:
        if not isinstance(item, dict):
            continue
        if not item.get("isExecutable"):
            continue
        ln = int(item.get("line") or 0)
        cnt = int(item.get("executionCount") or 0)
        if ln > 0:
            out[ln] = max(out.get(ln, 0), max(0, cnt))
    return out


def parse_xcresult(path: str, resolver: PathResolver, changed_files: Set[str]) -> Dict[str, Dict[int, int]]:
    if not MAC:
        raise UserError("Xcode .xcresult coverage reports are only supported on macOS (requires `xcrun xccov`).")
    if not shutil.which("xcrun"):
        raise UserError("`xcrun` not found; install Xcode Command Line Tools (`xcode-select --install`).")

    code, out, err = run_cmd(["xcrun", "xccov", "view", "--report", "--json", path], cwd=resolver.workspace, timeout=60.0)
    if code != 0:
        detail = (err or out).strip()
        raise UserError(f"Failed to read .xcresult report {path}: {detail}")
    try:
        report = json.loads(out)
    except ValueError as exc:
        raise UserError(f"Invalid JSON from `xcrun xccov view --report --json`: {exc}")

    cov: Dict[str, Dict[int, int]] = {}
    orig_by_rel: Dict[str, List[str]] = {}
    for target in report.get("targets") or []:
        for file_entry in target.get("files") or []:
            orig_path = (file_entry.get("path") or "").strip()
            if not orig_path:
                continue
            rel = resolver.resolve(orig_path)
            cov.setdefault(rel, {})
            orig_by_rel.setdefault(rel, [])
            if orig_path not in orig_by_rel[rel]:
                orig_by_rel[rel].append(orig_path)
            if isinstance(file_entry.get("lines"), list):
                _merge_hits(cov, rel, _extract_xccov_lines(file_entry["lines"]))

    full_archive: Optional[Dict[str, Any]] = None
    for rel in sorted(changed_files):
        if rel not in orig_by_rel or cov.get(rel):
            continue
        for orig_path in orig_by_rel[rel]:
            acode, aout, _aerr = run_cmd(
                ["xcrun", "xccov", "view", "--archive", "--file", orig_path, "--json", path],
                cwd=resolver.workspace,
                timeout=60.0,
            )
            if acode == 0 and aout.strip():
                try:
                    adata = json.loads(aout)
                    if isinstance(adata, list):
                        _merge_hits(cov, rel, _extract_xccov_lines(adata))
                        continue
                    if isinstance(adata, dict):
                        entries = adata.get(orig_path) or next(iter(adata.values()), [])
                        _merge_hits(cov, rel, _extract_xccov_lines(entries))
                        continue
                except ValueError:
                    pass
            if full_archive is None:
                fcode, fout, _ferr = run_cmd(
                    ["xcrun", "xccov", "view", "--archive", "--json", path],
                    cwd=resolver.workspace,
                    timeout=90.0,
                )
                if fcode == 0 and fout.strip():
                    try:
                        parsed = json.loads(fout)
                        full_archive = parsed if isinstance(parsed, dict) else {}
                    except ValueError:
                        full_archive = {}
                else:
                    full_archive = {}
            if orig_path in full_archive:
                _merge_hits(cov, rel, _extract_xccov_lines(full_archive[orig_path]))
    return cov


def parse_coverage(path: str, fmt: str, resolver: PathResolver, changed_files: Set[str]) -> Dict[str, Dict[int, int]]:
    if fmt == "cobertura":
        return parse_cobertura(path, resolver)
    if fmt == "lcov":
        return parse_lcov(path, resolver)
    if fmt == "gocover":
        return parse_gocover(path, resolver)
    if fmt == "xcresult":
        return parse_xcresult(path, resolver, changed_files)
    raise UserError(f"Unsupported coverage format: {fmt}")


def _unquote_c_str(s: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            escapes = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"'}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt in "01234567" and i + 3 < len(s):
                oct_digits = s[i + 1 : i + 4]
                if all(c in "01234567" for c in oct_digits):
                    raw_bytes = [int(oct_digits, 8)]
                    i += 4
                    while i + 3 < len(s) and s[i] == "\\" and all(c in "01234567" for c in s[i + 1 : i + 4]):
                        raw_bytes.append(int(s[i + 1 : i + 4], 8))
                        i += 4
                    out.append(bytes(raw_bytes).decode("utf-8", errors="replace"))
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


def _diff_header_path(name: str, prefix: str) -> Optional[str]:
    """The path of a `--- a/…` / `+++ b/…` line. git ends the line with a TAB when the name has a space, and C-quotes
    a name with a quote, a backslash or a control character. None for /dev/null."""
    if name.endswith("\t"):
        name = name[:-1]
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = _unquote_c_str(name[1:-1])
    if not name.startswith(prefix) or len(name) == len(prefix):
        return None
    return posixpath.normpath(name[len(prefix):])


def parse_unified_diff(diff_text: str) -> Dict[str, Set[int]]:
    """Parse `git diff -U0` into {repo_rel_posix_path: {1-based changed line numbers}}."""
    changed: Dict[str, Set[int]] = {}
    current_file: Optional[str] = None
    in_header = False
    old_rem = 0
    new_rem = 0

    for raw in diff_text.split("\n"):
        line = raw.rstrip("\r")
        if old_rem > 0 or new_rem > 0:
            if line.startswith("-"):
                if old_rem > 0:
                    old_rem -= 1
            elif line.startswith("+"):
                if new_rem > 0:
                    new_rem -= 1
            elif line.startswith("\\"):
                pass
            else:
                if old_rem > 0:
                    old_rem -= 1
                if new_rem > 0:
                    new_rem -= 1
            continue

        if line.startswith("diff --git "):
            in_header = True
            current_file = None
            continue

        if in_header and line.startswith("+++ "):
            current_file = _diff_header_path(line[4:], "b/")
            continue

        m = HUNK_RE.match(line)
        if m:
            in_header = False
            old_rem = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_cnt = int(m.group(4)) if m.group(4) is not None else 1
            new_rem = new_cnt
            if new_cnt > 0 and current_file:
                changed.setdefault(current_file, set()).update(range(new_start, new_start + new_cnt))

    return changed


def _untracked_file_lines(workspace: str, rel_path: str) -> Set[int]:
    full = os.path.join(workspace, *rel_path.split("/"))
    if not os.path.isfile(full) or os.path.islink(full):
        return set()
    try:
        with open(full, "rb") as fh:
            head = fh.read(8192)
        if b"\x00" in head:
            return set()
        with open(full, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return set()
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return set(range(1, len(lines) + 1))


def collect_changed_lines(
    workspace: str,
    git_root: str,
    base: Optional[str],
    cov_rel: Optional[str],
) -> Tuple[str, Dict[str, List[int]]]:
    """Return (base_label, {workspace_rel_posix_path: sorted_changed_line_numbers})."""
    use_relative = os.path.realpath(workspace) != os.path.realpath(git_root)
    rel_flag = ["--relative"] if use_relative else []

    changed_sets: Dict[str, Set[int]] = {}
    include_untracked = False

    if base:
        if ".." in base:
            code, out, err = run_cmd(
                ["git", "-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"]
                + rel_flag
                + [base],
                cwd=workspace,
            )
            if code != 0:
                raise UserError(f"Invalid git base/range {base!r}: {(err or out).strip()}")
            base_label = base
            changed_sets = parse_unified_diff(out)
        else:
            code, _, _ = run_cmd(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=workspace)
            if code != 0:
                raise UserError(f"Unknown git ref: {base}")
            diff_target = base
            base_label = base
            if has_head(workspace) and is_branch_ref(base, workspace):
                mb = (git_stdout(["merge-base", "HEAD", base], cwd=workspace) or "").strip()
                if mb:
                    diff_target = mb
                    base_label = f"{base} (merge-base {mb[:8]})"
            diff_out = git_stdout(
                ["diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"] + rel_flag + [diff_target],
                cwd=workspace,
            )
            if diff_out is None:
                raise UserError(f"Failed to run `git diff` against {diff_target}.")
            changed_sets = parse_unified_diff(diff_out)
            include_untracked = True
    else:
        if not has_head(workspace):
            base_label = "EMPTY (unborn HEAD)"
            ls_out = git_stdout(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=workspace) or ""
            for p in ls_out.split("\0"):
                if not p or is_coverage_artifact(p, cov_rel):
                    continue
                norm = posixpath.normpath(_strip_dot_slash(_to_posix(p)))
                lines = _untracked_file_lines(workspace, norm)
                if lines:
                    changed_sets[norm] = lines
            return base_label, {k: sorted(v) for k, v in sorted(changed_sets.items()) if v}

        names_out = git_stdout(["diff", "--name-only", "-z"] + rel_flag + ["HEAD"], cwd=workspace) or ""
        dirty_tracked = [p for p in names_out.split("\0") if p and not is_coverage_artifact(p, cov_rel)]
        untracked_out = git_stdout(["ls-files", "-z", "--others", "--exclude-standard"], cwd=workspace) or ""
        untracked = [p for p in untracked_out.split("\0") if p and not is_coverage_artifact(p, cov_rel)]

        if dirty_tracked or untracked:
            base_label = "HEAD"
            diff_out = git_stdout(["diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"] + rel_flag + ["HEAD"], cwd=workspace)
            if diff_out is None:
                raise UserError("Failed to run `git diff HEAD`.")
            changed_sets = parse_unified_diff(diff_out)
            include_untracked = True
        else:
            branch = (git_stdout(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace) or "").strip()
            diff_target = ""
            base_label = ""
            if branch and branch not in ("main", "master"):
                for cand_ref in ("origin/main", "main", "origin/master", "master"):
                    vcode, _, _ = run_cmd(
                        ["git", "rev-parse", "--verify", "--quiet", f"{cand_ref}^{{commit}}"],
                        cwd=workspace,
                    )
                    if vcode == 0:
                        mb = (git_stdout(["merge-base", "HEAD", cand_ref], cwd=workspace) or "").strip()
                        if mb:
                            diff_target = mb
                            base_label = f"{cand_ref} (merge-base {mb[:8]})"
                            break
            if not diff_target:
                vcode, _, _ = run_cmd(["git", "rev-parse", "--verify", "--quiet", "HEAD~1^{commit}"], cwd=workspace)
                if vcode == 0:
                    diff_target = "HEAD~1"
                    base_label = "HEAD~1"
                else:
                    empty_tree = (git_stdout(["hash-object", "-t", "tree", os.devnull], cwd=workspace) or "").strip()
                    diff_target = empty_tree
                    base_label = "EMPTY"
            if diff_target:
                diff_out = git_stdout(
                    ["diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"] + rel_flag + [diff_target],
                    cwd=workspace,
                )
                if diff_out is None:
                    raise UserError(f"Failed to run `git diff {diff_target}`.")
                changed_sets = parse_unified_diff(diff_out)

    if include_untracked:
        untracked_out = git_stdout(["ls-files", "-z", "--others", "--exclude-standard"], cwd=workspace) or ""
        for p in untracked_out.split("\0"):
            if not p or is_coverage_artifact(p, cov_rel):
                continue
            norm = posixpath.normpath(_strip_dot_slash(_to_posix(p)))
            lines = _untracked_file_lines(workspace, norm)
            if lines:
                changed_sets.setdefault(norm, set()).update(lines)

    cleaned = {
        k: sorted(v)
        for k, v in sorted(changed_sets.items())
        if v and not is_coverage_artifact(k, cov_rel)
    }
    return base_label, cleaned


def group_ranges(lines: List[int]) -> List[Tuple[int, int]]:
    if not lines:
        return []
    ordered = sorted(set(lines))
    ranges: List[Tuple[int, int]] = []
    start = end = ordered[0]
    for ln in ordered[1:]:
        if ln == end + 1:
            end = ln
        else:
            ranges.append((start, end))
            start = end = ln
    ranges.append((start, end))
    return ranges


def format_ranges(ranges: List[Tuple[int, int]]) -> str:
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges)


def read_preview(workspace: str, rel_path: str, start: int, end: int) -> str:
    full = os.path.join(workspace, *rel_path.split("/"))
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return ""
    fallback = ""
    for ln in range(start, end + 1):
        idx = ln - 1
        if 0 <= idx < len(lines):
            stripped = lines[idx].strip()
            if stripped:
                return stripped[:97] + "..." if len(stripped) > 100 else stripped
            if not fallback:
                fallback = stripped
    return fallback


def compute_diff_cover(
    workspace: str,
    base_label: str,
    cov_display: str,
    cov_fmt: str,
    changed: Dict[str, List[int]],
    coverage: Dict[str, Dict[int, int]],
    min_pct: Optional[float],
) -> Dict[str, Any]:
    files_report: List[Dict[str, Any]] = []
    not_in_coverage: List[str] = []
    total_covered = 0
    total_exec = 0

    for rel_path, changed_lines in sorted(changed.items()):
        if rel_path not in coverage:
            not_in_coverage.append(rel_path)
            continue
        line_hits = coverage[rel_path]
        exec_changed = [ln for ln in changed_lines if ln in line_hits]
        covered = [ln for ln in exec_changed if line_hits[ln] > 0]
        uncovered = [ln for ln in exec_changed if line_hits[ln] == 0]

        file_exec = len(exec_changed)
        file_cov = len(covered)
        total_exec += file_exec
        total_covered += file_cov

        ranges = group_ranges(uncovered)
        range_entries = [
            {
                "start": a,
                "end": b,
                "lines": f"{a}" if a == b else f"{a}-{b}",
                "preview": read_preview(workspace, rel_path, a, b),
            }
            for a, b in ranges
        ]
        file_pct = 100.0 if file_exec == 0 else round(file_cov * 100.0 / file_exec, 2)
        files_report.append(
            {
                "path": rel_path,
                "changed_lines": changed_lines,
                "executable_changed_lines": exec_changed,
                "covered_lines": file_cov,
                "total_executable_changed_lines": file_exec,
                "percent": file_pct,
                "uncovered_lines": uncovered,
                "uncovered_ranges": range_entries,
                "uncovered_summary": format_ranges(ranges),
            }
        )

    # Changed source files (an extension the report has, or any file when the report lists none) of which not one
    # is in the report: the paths don't match or the report is stale — never a silent 100%.
    cov_exts = {posixpath.splitext(p)[1].lower() for p in coverage if posixpath.splitext(p)[1]}
    unmatched = [p for p in not_in_coverage if not coverage or posixpath.splitext(p)[1].lower() in cov_exts]
    warning: Optional[str] = None
    percent: Optional[float]
    if unmatched and not files_report:
        warning = (
            f"None of the {len(unmatched)} changed source file(s) is in the coverage report: its paths don't match "
            "the repository's, or it is stale or from another checkout. Diff coverage can't be computed — "
            "regenerate the report or pass --coverage."
        )
        percent = None
        passed = min_pct is None
    else:
        raw_pct = 100.0 if total_exec == 0 else (total_covered * 100.0 / total_exec)
        percent = round(raw_pct, 2)
        passed = True if min_pct is None else (raw_pct >= min_pct)

    return {
        "workspace": workspace,
        "base": base_label,
        "coverage_file": cov_display,
        "coverage_format": cov_fmt,
        "covered_lines": total_covered,
        "total_executable_changed_lines": total_exec,
        "percent": percent,
        "min": min_pct,
        "passed": passed,
        "files": files_report,
        "not_in_coverage": not_in_coverage,
        "warning": warning,
    }


def render_text(report: Dict[str, Any]) -> str:
    cov = report["covered_lines"]
    tot = report["total_executable_changed_lines"]
    pct = report["percent"]
    pct_text = "n/a" if pct is None else f"{pct:.1f}%"
    lines = [
        f"# Diff Coverage — {pct_text} ({cov}/{tot} executable changed lines)",
        f"Base: {report['base']} · Coverage: {report['coverage_file']} ({report['coverage_format']})",
        "",
    ]
    files = report["files"]
    not_in = report["not_in_coverage"]
    if report.get("warning"):
        lines.append(f"⚠️ {report['warning']}")
    elif not files and not not_in:
        lines.append(f"No changed lines vs {report['base']}.")
    elif not files:
        lines.append("No changed files matched entries in the coverage report.")
    else:
        for entry in files:
            fcov = entry["covered_lines"]
            ftot = entry["total_executable_changed_lines"]
            fpct = entry["percent"]
            if ftot == 0:
                lines.append(f"- {entry['path']}: no executable lines in diff")
                continue
            suffix = f" — uncovered: {entry['uncovered_summary']}" if entry["uncovered_summary"] else " — fully covered"
            lines.append(f"- {entry['path']}: {fpct:.1f}% ({fcov}/{ftot}){suffix}")
            for rng in entry["uncovered_ranges"]:
                preview = f" `{rng['preview']}`" if rng["preview"] else ""
                lines.append(f"  - L{rng['lines']}:{preview}")

    if not_in:
        lines.extend(["", f"Not in coverage report ({len(not_in)}):"])
        for path in not_in:
            lines.append(f"- {path}")

    if report["min"] is not None and not report["passed"]:
        if pct is None:
            lines.extend(["", "FAILED: no diff coverage could be computed, so the "
                                  f"{report['min']:.1f}% minimum isn't met."])
        else:
            lines.extend(["", f"FAILED: diff coverage {pct:.1f}% is below minimum {report['min']:.1f}%."])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="diff_cover.py",
        description="Compute test coverage specifically over git-changed lines.",
    )
    ap.add_argument("--workspace", default=".", help="project / git repository directory (default: .)")
    ap.add_argument("--base", default=None, help="git ref or range to diff against")
    ap.add_argument("--coverage", default=None, help="coverage report file or .xcresult directory")
    ap.add_argument("--min", type=float, default=None, metavar="PCT", help="minimum required diff coverage percent")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args(argv)

    workspace = os.path.realpath(os.path.expanduser(args.workspace))
    try:
        if not os.path.isdir(workspace):
            raise UserError(f"Workspace directory does not exist: {workspace}")
        git_root = find_git_root(workspace)
        if not git_root:
            raise UserError(f"Not a git repository: {workspace}. Initialize one (`git init` or `/git-init`) first.")

        cov_path, cov_fmt = discover_coverage(workspace, args.coverage)
        cov_rel: Optional[str] = None
        try:
            if os.path.commonpath([workspace, cov_path]) == workspace:
                cov_rel = _to_posix(os.path.relpath(cov_path, workspace))
        except ValueError:
            pass
        cov_display = cov_rel or cov_path

        base_label, changed = collect_changed_lines(workspace, git_root, args.base, cov_rel)
        resolver = PathResolver(workspace, git_root, cov_path)
        coverage = parse_coverage(cov_path, cov_fmt, resolver, set(changed.keys()))
        report = compute_diff_cover(workspace, base_label, cov_display, cov_fmt, changed, coverage, args.min)
    except UserError as exc:
        if args.json:
            payload: Dict[str, Any] = {"error": exc.message}
            if exc.hint:
                payload["hint"] = exc.hint
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(exc.message, file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
