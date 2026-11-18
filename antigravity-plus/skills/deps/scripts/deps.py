#!/usr/bin/env python3
"""Antigravity Plus — /deps: dependency audit (known vulnerabilities and outdated packages).

  deps.py scan     [--path DIR] [--json]                  # ecosystems, manifests, lockfiles, which tools are installed
  deps.py audit    [--path DIR] [--json] [--timeout 120]  # known vulnerabilities (pip-audit, npm/pnpm/yarn/bun audit, …)
  deps.py outdated [--path DIR] [--json] [--timeout 120]  # outdated packages, classified major / minor / patch
  deps.py all      [--path DIR] [--json] [--timeout 120]  # audit + outdated

Runs only tools that are already installed and never installs or upgrades anything: a missing tool is reported with
the command that would install it. Every tool runs with a timeout (the network may be slow or offline).
Exit code: 0 — nothing found, 1 — findings (vulnerable or outdated packages), 2 — something couldn't be checked and
nothing was found elsewhere. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

WINDOWS = os.name == "nt"
DEFAULT_TIMEOUT = 120.0
MAX_DEPTH = 6
SKIP_DIRS = {"node_modules", "bower_components", "jspm_packages", "vendor", "build", "dist", "target", "out", "venv",
             "env", "site-packages", "__pycache__", "Pods", "Carthage", "DerivedData", "coverage", "htmlcov",
             "testdata", "fixtures", "__fixtures__", "__mocks__"}
LABELS = {"python": "Python", "js": "JavaScript", "rust": "Rust", "go": "Go", "ruby": "Ruby", "swift": "Swift",
          "gradle": "Gradle (JVM)", "php": "PHP"}
ORDER = list(LABELS)
PY_FILES = ("pyproject.toml", "setup.py", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock", "pylock.toml")
REQ_RE = re.compile(r"^requirements[\w.-]*\.txt$", re.I)
TRIGGERS = {
    "js": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "ruby": ("Gemfile", "Gemfile.lock"),
    "swift": ("Package.swift", "Package.resolved"),
    "gradle": ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "gradle.lockfile"),
    "php": ("composer.json", "composer.lock"),
}
EXTRA = {
    "python": ("setup.cfg",),
    "js": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb",
           ".yarnrc.yml"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum", "go.work"),
}
LOCKS = {  # files that pin exact versions (what osv-scanner can read); Python also counts requirements*.txt
    "python": ("poetry.lock", "Pipfile.lock", "uv.lock", "pylock.toml"),
    "js": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
    "rust": ("Cargo.lock",), "go": ("go.mod",), "ruby": ("Gemfile.lock",), "swift": ("Package.resolved",),
    "gradle": ("gradle.lockfile", "buildscript-gradle.lockfile"), "php": ("composer.lock",),
}
VENV_NAMES = (".venv", "venv", "env", ".env")
OSV_ECOS = {"pypi": "python", "npm": "js", "crates.io": "rust", "go": "go", "rubygems": "ruby", "packagist": "php",
            "maven": "gradle", "swifturl": "swift"}
INSTALL = {
    "pip-audit": "pipx install pip-audit",
    "osv-scanner": "brew install osv-scanner  (or: go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest)",
    "cargo-audit": "cargo install cargo-audit --locked",
    "cargo-outdated": "cargo install cargo-outdated --locked",
    "govulncheck": "go install golang.org/x/vuln/cmd/govulncheck@latest",
    "bundle-audit": "gem install bundler-audit",
    "bundle": "gem install bundler",
    "npm": "install Node.js (https://nodejs.org), it brings npm",
    "pnpm": "corepack enable pnpm  (or: npm install -g pnpm)",
    "yarn": "corepack enable yarn  (or: npm install -g yarn)",
    "bun": "see https://bun.sh",
    "go": "install Go (https://go.dev/dl/)",
    "composer": "see https://getcomposer.org/download/",
    "uv": "see https://docs.astral.sh/uv/",
}
UPGRADE = {
    "npm": "npm install <pkg>@<version>  (or `npm audit fix` — never `npm audit fix --force`)",
    "pnpm": "pnpm update <pkg>@<version>  (a transitive one: `pnpm.overrides` in package.json)",
    "yarn": "yarn upgrade <pkg>@<version>  (a transitive one: `resolutions` in package.json)",
    "yarn-berry": "yarn up <pkg>@<version>  (a transitive one: `yarn up -R <pkg>`)",
    "bun": "bun update <pkg>  (or `bun add <pkg>@<version>`)",
    "uv": "uv lock --upgrade-package <pkg>, then `uv sync`",
    "poetry": "poetry update <pkg>",
    "pipenv": "pipenv update <pkg>",
    "python": "raise the pin in requirements / pyproject, then reinstall into .venv",
    "rust": "cargo update -p <pkg>  (a major bump: edit Cargo.toml)",
    "go": "go get <pkg>@<version> && go mod tidy",
    "ruby": "bundle update --conservative <pkg>",
    "php": "composer update <pkg> --with-dependencies",
    "swift": "raise the version requirement in Package.swift / Xcode, then resolve packages",
    "gradle": "raise the version in build.gradle(.kts) or the version catalog",
}
SEV_RANK = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4, "": 5}
KIND_RANK = {"major": 0, "minor": 1, "patch": 2, "unknown": 3}
GHSA_RE = re.compile(r"GHSA-\w{4}-\w{4}-\w{4}", re.I)
VER_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
NOT_CHECKED = ("missing", "failed", "timeout", "unsupported")
LIST_SCRIPT = (
    "import os, sys; cwd = os.path.realpath(os.getcwd()); "
    "sys.path[:] = [p for p in sys.path if p not in ('', '.') and os.path.realpath(p) != cwd]; "
    "import json; from importlib import metadata; "
    "seen = {((d.metadata['Name'] or '') if d.metadata else '', d.version or '') "
    "for d in metadata.distributions()}; "
    "print(json.dumps(sorted([n, v] for n, v in seen if n and v)))"
)


class ToolError(Exception):
    """A tool answered with an explicit error (no lockfile, registry down, …)."""


# ─────────────────────────────────────────────── detection (local, no network) ───────────────────────────────────────

def _slash(path: str) -> str:
    return path.replace(os.sep, "/")


def _rel(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    return "." if rel == "." else _slash(rel)


def _inside(parent: str, child: str) -> bool:
    try:
        return parent != child and os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _walk_into(folder: str, name: str) -> bool:
    if name.startswith(".") or name in SKIP_DIRS:
        return False
    return not os.path.isfile(os.path.join(folder, name, "pyvenv.cfg"))  # any virtualenv, whatever its name


def _manifests(eco: str, folder: str, names: set) -> List[str]:
    if eco == "python":
        hits = [n for n in PY_FILES if n in names] + sorted(n for n in names if REQ_RE.match(n))
        reqdir = os.path.join(folder, "requirements")
        if os.path.isdir(reqdir):
            try:
                hits += sorted("requirements/" + n for n in os.listdir(reqdir)
                               if n.endswith(".txt") and os.path.isfile(os.path.join(reqdir, n)))
            except OSError:
                pass
    else:
        hits = [n for n in TRIGGERS[eco] if n in names]
    if not hits:
        return []
    return hits + [n for n in EXTRA.get(eco, ()) if n in names and n not in hits]


def _owner(root: str, folder: str, eco: str) -> str:
    """Package.resolved inside App.xcodeproj/… or App.xcworkspace/… belongs to the folder holding the project."""
    if eco != "swift":
        return folder
    parts = os.path.relpath(folder, root).split(os.sep)
    for i, part in enumerate(parts):
        if part.endswith((".xcodeproj", ".xcworkspace")):
            return os.path.join(root, *parts[:i]) if i else root
    return folder


def _is_lock(eco: str, path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in LOCKS.get(eco, ()):
        return True
    return eco == "python" and (bool(REQ_RE.match(name)) or (path.startswith("requirements/") and name.endswith(".txt")))


def _names(entry: Dict[str, Any]) -> set:
    return {m for m in entry["manifests"] if "/" not in m}


def _foldable(entry: Dict[str, Any], parent: Dict[str, Any]) -> bool:
    """A workspace member checked through its parent's lockfile (npm/pnpm/yarn workspaces, Cargo, Gradle, uv/poetry)."""
    eco, own, up = entry["ecosystem"], _names(entry), _names(parent)
    if eco == "js":
        return not any(_is_lock("js", n) for n in own) and any(_is_lock("js", n) for n in up)
    if eco == "rust":
        return "Cargo.lock" not in own and "Cargo.lock" in up
    if eco == "gradle":
        return not own & {"settings.gradle", "settings.gradle.kts"} and bool(up & {"settings.gradle", "settings.gradle.kts"})
    if eco == "python":
        return (not own & set(LOCKS["python"]) and bool(up & {"uv.lock", "poetry.lock"})
                and not any(os.path.isfile(os.path.join(entry["abs"], v, "pyvenv.cfg")) for v in VENV_NAMES))
    return False


def _git_tracked(folder: str, path: str) -> bool:
    """True when any file in venv directory `path` is tracked in git (so committed repos can't supply code)."""
    git_bin = shutil.which("git")
    if not git_bin:
        if WINDOWS:
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            local = os.environ.get("LocalAppData", "")
            candidates: Tuple[str, ...] = (
                os.path.join(pf, "Git", "cmd", "git.exe"),
                os.path.join(pf, "Git", "bin", "git.exe"),
                os.path.join(pf86, "Git", "cmd", "git.exe"),
            ) + ((os.path.join(local, "Programs", "Git", "cmd", "git.exe"),) if local else ())
        else:
            candidates = ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
        git_bin = next((p for p in candidates if os.path.isfile(p)), "")
    if not git_bin:
        return False
    try:
        rel = os.path.relpath(path, folder).replace(os.sep, "/")
        r = subprocess.run([git_bin, "-C", folder, "ls-files", "--", rel],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def find_venv(folder: str, root: str) -> Tuple[Optional[str], Optional[str]]:
    """The project's own virtualenv: (venv dir, its python) in the folder or a parent up to the scan root."""
    exes = (
        (("Scripts", "python.exe"), ("Scripts", "python3.exe"), ("Scripts", "python.cmd"), ("Scripts", "python.bat"),
         ("bin", "python3"), ("bin", "python"))
        if WINDOWS
        else (("bin", "python3"), ("bin", "python"), ("Scripts", "python.exe"))
    )
    cur = folder
    while True:
        for name in VENV_NAMES:
            venv = os.path.join(cur, name)
            if os.path.isfile(os.path.join(venv, "pyvenv.cfg")) and not _git_tracked(cur, venv):
                for exe in exes:
                    py = os.path.join(venv, *exe)
                    if os.path.isfile(py):
                        return venv, py
        if cur == root or not _inside(root, cur):
            return None, None
        cur = os.path.dirname(cur)


YARN_EXEC_RE = re.compile(r"(?m)^\s*(?:yarnPath|yarn-path|plugins)\s*(?::|\s+\S)")


def _unsafe_yarnrc(folder: str, root: str) -> Optional[str]:
    """Name of a .yarnrc/.yarnrc.yml setting yarnPath or plugins inside the scanned tree, or None."""
    cur = os.path.realpath(folder)
    top_dir = os.path.realpath(root)
    while True:
        for name in (".yarnrc.yml", ".yarnrc"):
            p = os.path.join(cur, name)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    if YARN_EXEC_RE.search(f.read(65536)):
                        return name
            except OSError:
                pass
        if cur == top_dir or not _inside(top_dir, cur):
            return None
        cur = os.path.dirname(cur)


def js_manager(folder: str, names: set) -> str:
    if "pnpm-lock.yaml" in names:
        return "pnpm"
    if "bun.lock" in names or "bun.lockb" in names:
        return "bun"
    if "yarn.lock" in names:
        if ".yarnrc.yml" in names:
            return "yarn-berry"
        try:
            with open(os.path.join(folder, "yarn.lock"), encoding="utf-8", errors="replace") as f:
                return "yarn-berry" if "__metadata:" in f.read(4096) else "yarn"
        except OSError:
            return "yarn"
    if "package-lock.json" in names or "npm-shrinkwrap.json" in names:
        return "npm"
    try:
        with open(os.path.join(folder, "package.json"), encoding="utf-8") as f:
            declared = str(json.load(f).get("packageManager") or "")
    except (OSError, ValueError, AttributeError):
        declared = ""
    name = declared.split("@", 1)[0]
    if name == "yarn":
        major = VER_RE.search(declared.split("@", 1)[1]) if "@" in declared else None
        return "yarn-berry" if major and int(major.group(1)) >= 2 else "yarn"
    return name if name in ("pnpm", "bun") else "npm"


def scan(root: str, max_depth: int = MAX_DEPTH) -> List[Dict[str, Any]]:
    """Every ecosystem under root: [{ecosystem, dir, abs, manifests, locks, members, manager?, venv?, python?}]."""
    root = os.path.realpath(root)
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for folder, dirs, files in os.walk(root):
        rel = os.path.relpath(folder, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if depth < max_depth and _walk_into(folder, d))
        names = set(files)
        for eco in ORDER:
            hits = _manifests(eco, folder, names)
            if not hits:
                continue
            owner = _owner(root, folder, eco)
            entry = found.setdefault((eco, owner), {"ecosystem": eco, "abs": owner, "manifests": [], "members": []})
            prefix = os.path.relpath(folder, owner)
            entry["manifests"] += [n if prefix == "." else _slash(os.path.join(prefix, n)) for n in hits]
    kept: List[Dict[str, Any]] = []
    for entry in sorted(found.values(), key=lambda e: (e["abs"].count(os.sep), e["abs"])):
        parents = [p for p in kept if p["ecosystem"] == entry["ecosystem"] and _inside(p["abs"], entry["abs"])]
        parent = max(parents, key=lambda p: len(p["abs"])) if parents else None
        if parent and _foldable(entry, parent):
            parent["members"].append(_rel(parent["abs"], entry["abs"]))
            continue
        kept.append(entry)
    for entry in kept:
        eco = entry["ecosystem"]
        entry["dir"] = _rel(root, entry["abs"])
        entry["locks"] = [m for m in entry["manifests"] if _is_lock(eco, m)]
        if eco == "js":
            entry["manager"] = js_manager(entry["abs"], _names(entry))
        if eco == "python":
            venv, py = find_venv(entry["abs"], root)
            entry["venv"], entry["python"] = (_rel(root, venv) if venv else None), py
            names = _names(entry)
            entry["manager"] = ("uv" if "uv.lock" in names else "poetry" if "poetry.lock" in names
                                else "pipenv" if names & {"Pipfile", "Pipfile.lock"} else "python")
    return sorted(kept, key=lambda e: (ORDER.index(e["ecosystem"]), e["dir"] != ".", e["dir"]))


# ─────────────────────────────────────────────── running tools ───────────────────────────────────────────────────────

class Ctx:
    def __init__(self, root: str, timeout: float) -> None:
        self.root = root
        self.timeout = timeout
        # No colors, no update nags; corepack must not download a package manager (that would be an install).
        self.env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0", CARGO_TERM_COLOR="never",
                        NPM_CONFIG_UPDATE_NOTIFIER="false", NPM_CONFIG_FUND="false", COREPACK_ENABLE_NETWORK="0",
                        PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", COMPOSER_NO_INTERACTION="1")


def _kill_tree(pid: int) -> None:
    if WINDOWS:
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def run(cmd: List[str], cwd: str, ctx: Ctx, timeout: Optional[float] = None) -> Tuple[str, int, str, str]:
    """(status ok|missing|error|timeout, exit code, stdout, stderr); kills the whole process tree on timeout."""
    exe = cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0], path=ctx.env.get("PATH"))
    if not exe:
        return "missing", 127, "", ""
    kw: Dict[str, Any] = {}
    if WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen([exe, *cmd[1:]], cwd=cwd, env=ctx.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", **kw)
    except OSError as e:
        return "error", 126, "", str(e)
    try:
        out, err = proc.communicate(timeout=timeout or ctx.timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out, err = "", ""
        return "timeout", -1, out or "", err or ""
    return "ok", proc.returncode, out or "", err or ""


def _tail(*texts: str, limit: int = 300) -> str:
    lines = [ln.strip() for t in texts for ln in (t or "").splitlines() if ln.strip()]
    return " · ".join(lines[-3:])[-limit:] or "no output"


def result(tool: str, status: str = "ok", message: str = "", install: str = "", **kw: Any) -> Dict[str, Any]:
    return {"tool": tool, "status": status, "message": message, "install": install, **kw}


def run_parse(label: str, cmd: List[str], cwd: str, ctx: Ctx, parser: Callable[[str], Optional[List[Dict[str, Any]]]],
              key: str, install: str = "", empty_ok: bool = False) -> Dict[str, Any]:
    status, code, out, err = run(cmd, cwd, ctx)
    if status == "missing":
        name = os.path.basename(cmd[0])
        return result(label, "missing", f"`{name}` isn't installed", install or INSTALL.get(name, ""))
    if status == "timeout":
        return result(label, "timeout", f"{label} didn't finish in {ctx.timeout:g} s (slow or no network?)")
    if status == "error":
        return result(label, "failed", f"{label} couldn't start: {err}")
    try:
        items = parser(out)
    except ToolError as e:
        return result(label, "failed", f"{label}: {e}")
    if items is None and code == 0 and (empty_ok or not out.strip()):
        items = []
    if items is None:
        return result(label, "failed", f"{label} exited with {code}: {_tail(err, out)}")
    return result(label, **{key: items})


# ─────────────────────────────────────────────── parsing helpers ─────────────────────────────────────────────────────

def json_stream(text: str) -> List[Any]:
    """Every JSON value in text: one document, NDJSON, or pretty-printed objects back to back (non-JSON lines skipped)."""
    dec, out, i, n = json.JSONDecoder(), [], 0, len(text or "")
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] in "{[":
            try:
                obj, i = dec.raw_decode(text, i)
                out.append(obj)
                continue
            except ValueError:
                pass
        nl = text.find("\n", i)
        if nl < 0:
            break
        i = nl + 1
    return out


def load_json(text: str) -> Any:
    for obj in json_stream(text):
        if isinstance(obj, (dict, list)):
            return obj
    return None


def norm_sev(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip().lower()
    try:
        score = float(text)
    except ValueError:
        return {"medium": "moderate", "informational": "info", "none": "info"}.get(text, text if text in SEV_RANK else "")
    return "critical" if score >= 9 else "high" if score >= 7 else "moderate" if score >= 4 else "low" if score > 0 else ""


def vt(version: Any) -> Optional[Tuple[int, int, int]]:
    m = VER_RE.search(str(version or ""))
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else None


def bump_kind(current: Any, latest: Any) -> str:
    """major / minor / patch / unknown, or "" when latest isn't newer. In 0.x a minor bump counts as major (semver)."""
    a, b = vt(current), vt(latest)
    cs, ls = str(current or "").strip(), str(latest or "").strip()
    if not a or not b:
        return "unknown" if ls and ls != cs else ""
    if b < a:
        return ""
    if b == a:
        if "-" in ls and "-" not in cs:
            return ""
        return "patch" if cs.lstrip("v") != ls.lstrip("v") else ""
    if b[0] != a[0] or (a[0] == 0 and b[1] != a[1]):
        return "major"
    return "minor" if b[1] != a[1] else "patch"


def pick_fixed(current: str, fixed: List[str]) -> str:
    """The nearest fixed version above current, preferring the same major line."""
    cur = vt(current)
    cands = [f for f in fixed if vt(f) and (not cur or vt(f) > cur)] or [f for f in fixed if vt(f)]  # type: ignore[operator]
    same = [f for f in cands if cur and vt(f)[0] == cur[0]]  # type: ignore[index]
    pool = same or cands
    return min(pool, key=lambda f: vt(f)) if pool else ""  # type: ignore[arg-type,return-value]


def _first_sentence(text: str, limit: int = 110) -> str:
    line = " ".join(str(text or "").split())
    cut = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[: limit - 1] + "…"


def vuln(package: str, version: str = "", severity: Any = "", ident: str = "", aliases: Any = (), fixed: str = "",
         title: str = "", url: str = "", affected: str = "", fix: str = "", note: str = "") -> Dict[str, Any]:
    aliases = [a for a in (aliases or []) if a and a != ident]
    return {"package": str(package or "?"), "version": str(version or ""), "severity": norm_sev(severity),
            "id": str(ident or ""), "aliases": aliases[:4], "fixed": str(fixed or ""), "fix": fix,
            "title": _first_sentence(title), "url": url, "affected": str(affected or ""), "note": note}


def package(name: str, current: Any = "", wanted: Any = "", latest: Any = "", note: str = "") -> Dict[str, Any]:
    current, wanted, latest = str(current or ""), str(wanted or ""), str(latest or "")
    return {"package": name, "current": current, "wanted": wanted, "latest": latest,
            "kind": bump_kind(current or wanted, latest), "note": note}


def _ghsa(text: str) -> str:
    m = GHSA_RE.search(text or "")
    return m.group(0) if m else ""


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for v in items:
        key = (v["package"], v["id"], v["version"])
        if key not in seen:
            seen.add(key)
            out.append(v)
    return sorted(out, key=lambda v: (SEV_RANK.get(v["severity"], 5), v["package"].lower(), v["id"]))


# ─────────────────────────────────────────────── vulnerability parsers ───────────────────────────────────────────────

def parse_pip_audit(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    deps = data.get("dependencies") if isinstance(data, dict) else data
    if not isinstance(deps, list):
        return None
    out = []
    for dep in deps:
        if isinstance(dep, dict):
            for v in dep.get("vulns") or []:
                ident = v.get("id", "")
                out.append(vuln(dep.get("name", ""), dep.get("version", ""), "", ident, v.get("aliases"),
                                ", ".join(v.get("fix_versions") or []), v.get("description", ""),
                                f"https://osv.dev/vulnerability/{ident}" if ident else ""))
    return _dedupe(out)


def _v6(adv: Dict[str, Any]) -> Dict[str, Any]:
    """An npm v6-style advisory (npm 6, pnpm audit, yarn classic, Yarn 3)."""
    versions = sorted({f.get("version") for f in adv.get("findings") or [] if isinstance(f, dict) and f.get("version")})
    patched = str(adv.get("patched_versions") or "").strip()
    none = patched in ("", "<0.0.0")
    url = adv.get("url", "")
    return vuln(adv.get("module_name", ""), ", ".join(versions), adv.get("severity"),
                adv.get("github_advisory_id") or _ghsa(url) or str(adv.get("id", "")), adv.get("cves"),
                "" if none else patched, adv.get("title", ""), url, adv.get("vulnerable_versions", ""),
                fix="no fixed version yet" if patched == "<0.0.0" else "")


def parse_npm_family(text: str) -> Optional[List[Dict[str, Any]]]:
    """npm audit --json (v2 and v6), pnpm audit --json, yarn audit --json (NDJSON), yarn npm audit --json, bun audit."""
    out: List[Dict[str, Any]] = []
    known = False
    for obj in json_stream(text):
        if not isinstance(obj, dict):
            continue
        err = obj.get("error")
        if err:
            raise ToolError(_first_sentence(err.get("summary") or err.get("message") or err.get("code") or "error", 300)
                            if isinstance(err, dict) else _first_sentence(str(err), 300))
        if obj.get("type") == "error":
            raise ToolError(_first_sentence(str(obj.get("data", "")), 300))
        if "auditReportVersion" in obj and isinstance(obj.get("vulnerabilities"), dict):  # npm 7+
            known = True
            for name, v in obj["vulnerabilities"].items():
                fix_av = v.get("fixAvailable")
                if isinstance(fix_av, dict):
                    fix = f"upgrade {fix_av.get('name')} to {fix_av.get('version')}" + (
                        " (major)" if fix_av.get("isSemVerMajor") else "")
                else:
                    fix = "`npm audit fix`" if fix_av is True else "no fix available"
                for adv in (a for a in v.get("via") or [] if isinstance(a, dict)):  # strings = via another package
                    url = adv.get("url", "")
                    out.append(vuln(adv.get("name") or name, "", adv.get("severity") or v.get("severity"),
                                    _ghsa(url) or str(adv.get("source", "")), (), "", adv.get("title", ""), url,
                                    adv.get("range") or v.get("range", ""), fix=fix))
        elif isinstance(obj.get("advisories"), dict):  # npm 6, pnpm, Yarn 3
            known = True
            out += [_v6(a) for a in obj["advisories"].values() if isinstance(a, dict)]
        elif obj.get("type") in ("auditAdvisory", "auditSummary", "auditAction", "info", "warning"):  # yarn classic
            known = True
            if obj.get("type") == "auditAdvisory":
                out.append(_v6((obj.get("data") or {}).get("advisory") or {}))
        elif "value" in obj and isinstance(obj.get("children"), dict):  # Yarn 4: one line per advisory
            known = True
            c = obj["children"]
            url = str(c.get("URL", ""))
            tv = c.get("Tree Versions")
            vers = ", ".join(str(x) for x in tv) if isinstance(tv, list) else str(tv or "")
            out.append(vuln(obj["value"], vers, c.get("Severity"),
                            _ghsa(url) or str(c.get("ID", "")), (), "", c.get("Issue", ""), url,
                            c.get("Vulnerable Versions", "")))
        elif all(isinstance(v, list) for v in obj.values()):  # bun audit: the registry's bulk advisory answer
            known = True
            for name, advs in obj.items():
                for adv in (a for a in advs if isinstance(a, dict)):
                    url = adv.get("url", "")
                    out.append(vuln(name, "", adv.get("severity"), _ghsa(url) or str(adv.get("id", "")), (), "",
                                    adv.get("title", ""), url, adv.get("vulnerable_versions", "")))
    return _dedupe(out) if known else None


def parse_cargo_audit(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("vulnerabilities"), dict):
        return None
    out = []
    for item in data["vulnerabilities"].get("list") or []:
        adv, pkg = item.get("advisory") or {}, item.get("package") or {}
        patched = (item.get("versions") or {}).get("patched") or []
        out.append(vuln(pkg.get("name") or adv.get("package", ""), pkg.get("version", ""), "", adv.get("id", ""),
                        adv.get("aliases"), ", ".join(patched), adv.get("title", ""), adv.get("url", ""),
                        fix="" if patched else "no fixed version yet"))
    return _dedupe(out)


def parse_govulncheck(text: str) -> Optional[List[Dict[str, Any]]]:
    osvs: Dict[str, Dict[str, Any]] = {}
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    known = False
    for obj in json_stream(text):
        if not isinstance(obj, dict):
            continue
        known = known or bool(obj.keys() & {"config", "progress", "osv", "finding", "SBOM"})
        if isinstance(obj.get("osv"), dict):
            osvs[obj["osv"].get("id", "")] = obj["osv"]
        finding = obj.get("finding")
        if isinstance(finding, dict):
            trace = finding.get("trace") or [{}]
            frame = trace[0] if isinstance(trace[0], dict) else {}  # trace[0] is the vulnerable symbol
            cur = found.setdefault((finding.get("osv", ""), frame.get("module", "")),
                                   {"version": frame.get("version", ""), "fixed": finding.get("fixed_version", ""),
                                    "called": False})
            cur["called"] = cur["called"] or bool(frame.get("function"))
    if not known:
        return None
    out = []
    for (oid, module), f in found.items():
        osv = osvs.get(oid, {})
        out.append(vuln(module, f["version"], "", oid, osv.get("aliases"), f["fixed"], osv.get("summary", ""),
                        f"https://pkg.go.dev/vuln/{oid}", fix="" if f["fixed"] else "no fixed version yet",
                        note="your code calls it" if f["called"] else "imported, not called"))
    return sorted(_dedupe(out), key=lambda v: v["note"] != "your code calls it")


def parse_bundle_audit(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    out = []
    for r in data["results"]:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "unpatched_gem":
            gem, adv = r.get("gem") or {}, r.get("advisory") or {}
            aliases = []
            if adv.get("cve"):
                aliases.append(adv["cve"] if str(adv["cve"]).startswith("CVE-") else f"CVE-{adv['cve']}")
            if adv.get("ghsa"):
                aliases.append(adv["ghsa"] if str(adv["ghsa"]).startswith("GHSA-") else f"GHSA-{adv['ghsa']}")
            patched = adv.get("patched_versions") or []
            out.append(vuln(gem.get("name", ""), gem.get("version", ""), adv.get("criticality"),
                            adv.get("id") or (aliases[0] if aliases else ""), aliases, ", ".join(patched),
                            adv.get("title", ""), adv.get("url", ""), fix="" if patched else "no fixed version yet"))
        elif r.get("type") == "insecure_source":
            out.append(vuln(str(r.get("source", "")), "", "", "insecure-source", (), "",
                            "Gem source over plain http", fix="use an https source"))
    return _dedupe(out)


def parse_composer_audit(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    if not isinstance(data, dict) or "advisories" not in data:
        return None
    advs = data["advisories"] if isinstance(data["advisories"], dict) else {}  # PHP prints an empty map as []
    out = []
    for name, items in advs.items():
        for adv in (items.values() if isinstance(items, dict) else items or []):
            if isinstance(adv, dict):
                out.append(vuln(adv.get("packageName") or name, "", adv.get("severity"), adv.get("advisoryId", ""),
                                [adv["cve"]] if adv.get("cve") else [], "", adv.get("title", ""), adv.get("link", ""),
                                adv.get("affectedVersions", "")))
    return _dedupe(out)


def parse_osv(text: str) -> Optional[List[Tuple[str, str, Dict[str, Any]]]]:
    """osv-scanner --format json → [(ecosystem, source file, vulnerability)], one entry per group of aliases."""
    data = load_json(text)
    if not isinstance(data, dict) or "results" not in data:
        return None
    out = []
    for r in data.get("results") or []:
        src = str((r.get("source") or {}).get("path", ""))
        for p in r.get("packages") or []:
            pk = p.get("package") or {}
            eco = OSV_ECOS.get(str(pk.get("ecosystem", "")).split(":")[0].lower())
            if not eco:
                continue
            by_id = {v.get("id"): v for v in p.get("vulnerabilities") or [] if isinstance(v, dict) and v.get("id")}
            groups = p.get("groups") or [{"ids": [i]} for i in by_id]
            for g in groups:
                ids = [i for i in g.get("ids") or [] if i]
                if not ids:
                    continue
                main = next((i for i in ids if i.startswith("GHSA-")), ids[0])
                vs = [by_id[i] for i in ids if i in by_id]
                sev = g.get("max_severity") or next(
                    ((v.get("database_specific") or {}).get("severity") for v in vs
                     if (v.get("database_specific") or {}).get("severity")), "")
                pkg_norm = str(pk.get("name") or "").lower().replace("_", "-") if eco == "python" else str(pk.get("name") or "").lower()
                fixed = sorted({str(ev["fixed"]) for v in vs for a in v.get("affected") or []
                                if (lambda an: (an.replace("_", "-") if eco == "python" else an) == pkg_norm)(
                                    str((a.get("package") or {}).get("name") or "").lower())
                                for rg in a.get("ranges") or [] for ev in rg.get("events") or [] if ev.get("fixed")})
                aliases = sorted(set(ids[1:] + ids[:1] + [al for v in vs for al in v.get("aliases") or []]) - {main})
                title = next((v.get("summary") for v in vs if v.get("summary")), "")
                version = str(pk.get("version", ""))
                fix_to = pick_fixed(version, fixed)
                out.append((eco, src, vuln(pk.get("name", ""), version, sev, main, aliases, fix_to, title,
                                           f"https://osv.dev/vulnerability/{main}",
                                           fix="" if fix_to else "no fixed version yet")))
    return out


# ─────────────────────────────────────────────── outdated parsers ────────────────────────────────────────────────────

def _finish(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep = [p for p in items if p["kind"]]
    return sorted(keep, key=lambda p: (KIND_RANK.get(p["kind"], 3), p["package"].lower()))


def parse_js_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    """npm outdated --json / pnpm outdated --format json: {name: {current, wanted, latest}} (npm: a list per name)."""
    data = load_json(text)
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise ToolError(_first_sentence(data["error"].get("summary") or data["error"].get("message") or "error", 300))
    rows: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(data, dict):
        for name, info in data.items():
            rows += [(name, i) for i in (info if isinstance(info, list) else [info]) if isinstance(i, dict)]
    elif isinstance(data, list):
        rows = [(str(i.get("packageName") or i.get("name") or ""), i) for i in data if isinstance(i, dict)]
    else:
        return None
    return _finish([package(n, i.get("current"), i.get("wanted"), i.get("latest"),
                            "" if i.get("current") else "not installed") for n, i in rows if n])


def parse_yarn_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    """yarn (classic) outdated --json: NDJSON with a {"type": "table", "data": {head, body}} line."""
    out, known = [], False
    for obj in json_stream(text):
        if isinstance(obj, dict) and obj.get("type") == "table":
            known = True
            data = obj.get("data") or {}
            head = [str(h).lower() for h in data.get("head") or []]
            col = {k: head.index(k) for k in ("package", "current", "wanted", "latest") if k in head}
            for row in data.get("body") or []:
                cell = lambda k: row[col[k]] if k in col and col[k] < len(row) else ""  # noqa: E731
                out.append(package(cell("package"), cell("current"), cell("wanted"), cell("latest")))
    return _finish(out) if known else None


def parse_bun_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    """bun outdated prints a table only: | Package | Current | Update | Latest |."""
    header: Optional[List[str]] = None
    out = []
    for line in (text or "").splitlines():
        if "│" not in line and "|" not in line:
            continue
        cells = [c.strip() for c in re.split(r"[│|]", line)[1:-1]]
        if not cells or all(set(c) <= set("─-+┼├┤ ") for c in cells):
            continue
        low = [c.lower() for c in cells]
        if header is None:
            if "package" in low and "latest" in low:
                header = low
            continue
        get = lambda k: cells[header.index(k)] if k in header and header.index(k) < len(cells) else ""  # noqa: E731
        name = re.sub(r"\s*\((dev|peer|optional)\)$", "", get("package"))
        if name:
            out.append(package(name, get("current"), get("update") or get("wanted"), get("latest")))
    return _finish(out) if header else None


def parse_pip_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    if not isinstance(data, list):
        return None
    return _finish([package(str(i.get("name", "")), i.get("version"), "", i.get("latest_version"))
                    for i in data if isinstance(i, dict) and i.get("name")])


def parse_cargo_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    out, known = [], False
    for obj in json_stream(text):
        if isinstance(obj, dict) and isinstance(obj.get("dependencies"), list):
            known = True
            for d in obj["dependencies"]:
                latest = str(d.get("latest") or "")
                if not vt(latest):  # "---", "Removed"
                    continue
                compat = str(d.get("compat") or "")
                out.append(package(str(d.get("name", "")), d.get("project"), compat if vt(compat) else "", latest,
                                   "" if d.get("kind") in (None, "Normal") else str(d.get("kind")).lower()))
    return _finish(out) if known else None


def parse_go_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    out, known = [], False
    for obj in json_stream(text):
        if not isinstance(obj, dict) or "Path" not in obj:
            continue
        known = True
        upd = obj.get("Update") or {}
        if obj.get("Main") or not upd.get("Version"):
            continue
        out.append(package(obj["Path"], obj.get("Version"), "", upd["Version"],
                           "indirect" if obj.get("Indirect") else ""))
    return _finish(out) if known else None


def parse_composer_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    data = load_json(text)
    if not isinstance(data, dict) or "installed" not in data:
        return None
    return _finish([package(str(i.get("name", "")), i.get("version"), "", i.get("latest"))
                    for i in data.get("installed") or [] if isinstance(i, dict)
                    and i.get("latest-status") != "up-to-date"])


def parse_bundle_outdated(text: str) -> Optional[List[Dict[str, Any]]]:
    """bundle outdated --parseable: `rails (newest 7.1.3, installed 6.1.7, requested ~> 6.1)`."""
    out = []
    for m in re.finditer(r"^\s*([\w.-]+) \(newest ([^,)]+), installed ([^,)]+)", text or "", re.M):
        out.append(package(m.group(1), m.group(3).strip(), "", m.group(2).strip()))
    return _finish(out) if out else None


# ─────────────────────────────────────────────── per-ecosystem checks ────────────────────────────────────────────────

def _pinned(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().replace("\\\r\n", " ").replace("\\\n", " ")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r", "-c", "--requirement", "--constraint", "--index-url", "--extra-index-url", "-i ",
                            "--hash", "--find-links", "-f ", "--trusted-host", "--pre", "--only-binary", "--no-binary")):
            continue
        if "==" not in line or line.startswith("-e"):
            return False
    return True


def _venv_tool(entry: Dict[str, Any], name: str) -> Optional[str]:
    py = entry.get("python")
    if py:
        folder = os.path.dirname(py)
        exts = (".exe", ".cmd", ".bat") if WINDOWS else ("",)
        for ext in exts:
            path = os.path.join(folder, name + ext)
            if os.path.isfile(path) and (WINDOWS or os.access(path, os.X_OK)):
                return path
    return shutil.which(name)


def audit_python(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    py, cwd = entry.get("python"), entry["abs"]
    tool = _venv_tool(entry, "pip-audit")
    if py:
        list_timeout = min(60.0, ctx.timeout)
        status, code, out, err = run([py, "-c", LIST_SCRIPT], tempfile.gettempdir(), ctx, timeout=list_timeout)
        if status == "timeout":
            return result("pip-audit", "timeout", f"listing packages in {entry['venv']} didn't finish in {list_timeout:g} s")
        try:
            pkgs = json.loads(out) if status == "ok" and code == 0 else None
        except ValueError:
            pkgs = None
        if pkgs is None:
            return result("pip-audit", "failed", f"couldn't list the packages in {entry['venv']}: {_tail(err, out)}")
        if not tool:
            return result("pip-audit", "missing", "`pip-audit` isn't installed", INSTALL["pip-audit"])
        if not pkgs:
            return result("pip-audit", vulns=[], message=f"no packages installed in {entry['venv']}")
        fd, req = tempfile.mkstemp(prefix="agp-deps-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(f"{n}=={v}\n" for n, v in pkgs))
            res = run_parse("pip-audit", [tool, "-r", req, "--no-deps", "--disable-pip", "--format", "json"], cwd, ctx,
                            parse_pip_audit, "vulns")
        finally:
            try:
                os.unlink(req)
            except OSError:
                pass
        res["message"] = res["message"] or f"{len(pkgs)} packages installed in {entry['venv']}"
        return res
    reqs = [m for m in entry["manifests"]
            if REQ_RE.match(m.rsplit("/", 1)[-1]) or (m.startswith("requirements/") and m.endswith(".txt"))]
    if reqs and all(_pinned(os.path.join(cwd, r)) for r in reqs):
        if not tool:
            return result("pip-audit", "missing", "`pip-audit` isn't installed", INSTALL["pip-audit"])
        cmd = [tool] + [a for r in reqs for a in ("-r", r)] + ["--no-deps", "--disable-pip", "--format", "json"]
        return run_parse("pip-audit", cmd, cwd, ctx, parse_pip_audit, "vulns")
    return result("pip-audit", "unsupported", "no project environment (.venv) and no fully pinned requirements file — "
                  "nothing exact for pip-audit to check")


JS_AUDIT = {
    "npm": ("npm audit", ["npm", "audit", "--json"]),
    "pnpm": ("pnpm audit", ["pnpm", "audit", "--json"]),
    "yarn": ("yarn audit", ["yarn", "audit", "--json"]),
    "yarn-berry": ("yarn npm audit", ["yarn", "npm", "audit", "--all", "--recursive", "--json"]),
    "bun": ("bun audit", ["bun", "audit", "--json"]),
}
JS_OUTDATED = {
    "npm": ("npm outdated", ["npm", "outdated", "--json"], parse_js_outdated),
    "pnpm": ("pnpm outdated", ["pnpm", "outdated", "--format", "json"], parse_js_outdated),
    "yarn": ("yarn outdated", ["yarn", "outdated", "--json"], parse_yarn_outdated),
    "bun": ("bun outdated", ["bun", "outdated"], parse_bun_outdated),
}


def audit_js(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    manager = entry.get("manager", "npm")
    label, cmd = JS_AUDIT.get(manager, JS_AUDIT["npm"])
    if not entry["locks"]:
        return result(label, "failed", f"no lockfile — {label} needs one (`{manager} install` writes it; "
                      "ask the user first)")
    if cmd[0] == "yarn":
        bad_rc = _unsafe_yarnrc(entry["abs"], ctx.root)
        if bad_rc:
            return result(label, "failed", f"{bad_rc} configures custom yarnPath/plugins — skipped executing yarn")
    res = run_parse(label, cmd, entry["abs"], ctx, parse_npm_family, "vulns", INSTALL.get(cmd[0], ""))
    if manager == "bun" and res["status"] == "failed" and "audit" in res["message"].lower():
        res["message"] += " (bun audit needs a recent bun: `bun upgrade`)"
    return res


def _needs_lock(entry: Dict[str, Any], lock: str, label: str, how: str) -> Optional[Dict[str, Any]]:
    if lock in _names(entry):
        return None
    return result(label, "failed", f"no {lock} — {label} needs it ({how} writes it; ask the user first)")


def audit_rust(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return (_needs_lock(entry, "Cargo.lock", "cargo audit", "`cargo generate-lockfile`")
            or run_parse("cargo audit", ["cargo-audit", "audit", "--json"], entry["abs"], ctx, parse_cargo_audit,
                         "vulns", INSTALL["cargo-audit"]))


def audit_go(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return run_parse("govulncheck", ["govulncheck", "-json", "./..."], entry["abs"], ctx, parse_govulncheck, "vulns")


def audit_ruby(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    tool = "bundle-audit" if shutil.which("bundle-audit") or not shutil.which("bundler-audit") else "bundler-audit"
    return (_needs_lock(entry, "Gemfile.lock", "bundle-audit", "`bundle lock`")
            or run_parse("bundle-audit", [tool, "check", "--format", "json"], entry["abs"], ctx, parse_bundle_audit,
                         "vulns", INSTALL["bundle-audit"]))


def audit_php(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return (_needs_lock(entry, "composer.lock", "composer audit", "`composer update --lock`")
            or run_parse("composer audit", ["composer", "audit", "--format", "json", "--locked", "--no-interaction"],
                         entry["abs"], ctx, parse_composer_audit, "vulns"))


def audit_none(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return result("osv-scanner", "unsupported", f"{LABELS[entry['ecosystem']]} has no native audit tool")


AUDIT = {"python": audit_python, "js": audit_js, "rust": audit_rust, "go": audit_go, "ruby": audit_ruby,
         "php": audit_php, "swift": audit_none, "gradle": audit_none}


def osv_fallback(entries: List[Dict[str, Any]], ctx: Ctx) -> None:
    """osv-scanner checks the lockfiles of every ecosystem whose own tool is missing or failed."""
    need = [e for e in entries if e["audit"]["status"] in ("missing", "unsupported", "failed") and e["locks"]]
    if not need:
        return
    exe = shutil.which("osv-scanner")
    if not exe:
        for e in need:
            e["audit"]["message"] += "; the universal fallback `osv-scanner` isn't installed either"
            e["audit"]["fallback_install"] = INSTALL["osv-scanner"]
        return
    norm = lambda p: os.path.normcase(os.path.realpath(p))  # noqa: E731
    dirs = sorted({os.path.join(e["abs"], *lock.split("/")[:-1]) for e in need for lock in e["locks"]})
    status, code, out, err = run([exe, "--format", "json", *dirs], ctx.root, ctx)
    found = parse_osv(out) if status == "ok" else None
    if found is None and status == "ok" and code == 0 and not out.strip():
        found = []
    if found is None:
        why = (f"didn't finish in {ctx.timeout:g} s" if status == "timeout"
               else f"couldn't start: {err}" if status == "error"
               else f"exited with {code}: {_tail(err, out)}")
        for e in need:
            e["audit"]["message"] += f"; osv-scanner {why}"
        return
    for e in need:
        mine = {norm(os.path.join(e["abs"], *lock.split("/")[:-1])) for lock in e["locks"]}
        vulns = [
            v for eco, src, v in found
            if eco == e["ecosystem"]
            and norm(os.path.dirname(src if os.path.isabs(src) else os.path.join(ctx.root, src))) in mine
        ]
        before = e["audit"]
        e["audit"] = result("osv-scanner", vulns=_dedupe(vulns),
                            message=f"{before['message']} — checked the lockfiles with osv-scanner instead")


def outdated_python(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    py = entry.get("python")
    if not py:
        return result("pip list", "unsupported", "no project environment (.venv) — the outdated check reads the "
                      "installed packages; create the venv the project's usual way first")
    status, code, out, err = run([py, "-m", "pip", "list", "--outdated", "--format", "json",
                                  "--disable-pip-version-check"], tempfile.gettempdir(), ctx)
    if status == "timeout":
        return result("pip list", "timeout", f"pip list --outdated didn't finish in {ctx.timeout:g} s (slow or no network?)")
    if status == "error":
        return result("pip list", "failed", f"pip list --outdated couldn't start: {err}")
    if status == "ok" and code != 0 and "No module named pip" in err:
        uv = _venv_tool(entry, "uv")
        if not uv:
            return result("pip list", "failed", f"{entry['venv']} has no pip and `uv` isn't on PATH",
                          INSTALL["uv"])
        return run_parse("uv pip list", [uv, "pip", "list", "--outdated", "--format", "json", "--python", py],
                         tempfile.gettempdir(), ctx, parse_pip_outdated, "packages")
    items = parse_pip_outdated(out) if status == "ok" else None
    if items is None and status == "ok" and code == 0 and not out.strip():
        items = []
    if items is None:
        return result("pip list", "failed", f"pip list --outdated exited with {code}: {_tail(err, out)}")
    return result("pip list", packages=items)


def outdated_js(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    manager = entry.get("manager", "npm")
    if manager == "yarn-berry":
        return result("yarn", "unsupported", "Yarn 2+ has no non-interactive outdated report (`yarn upgrade-interactive` "
                      "is interactive) — not checked")
    label, cmd, parser = JS_OUTDATED.get(manager, JS_OUTDATED["npm"])
    if cmd[0] == "yarn":
        bad_rc = _unsafe_yarnrc(entry["abs"], ctx.root)
        if bad_rc:
            return result(label, "failed", f"{bad_rc} configures custom yarnPath/plugins — skipped executing yarn")
    return run_parse(label, cmd, entry["abs"], ctx, parser, "packages", INSTALL.get(cmd[0], ""),
                     empty_ok=manager in ("yarn", "bun"))


def outdated_rust(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return run_parse("cargo outdated", ["cargo-outdated", "outdated", "--format", "json", "--root-deps-only"],
                     entry["abs"], ctx, parse_cargo_outdated, "packages", INSTALL["cargo-outdated"])


def outdated_go(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return run_parse("go list -m -u", ["go", "list", "-m", "-u", "-json", "all"], entry["abs"], ctx,
                     parse_go_outdated, "packages")


def outdated_ruby(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return run_parse("bundle outdated", ["bundle", "outdated", "--parseable"], entry["abs"], ctx,
                     parse_bundle_outdated, "packages", empty_ok=True)


def outdated_php(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return run_parse("composer outdated", ["composer", "outdated", "--direct", "--format", "json", "--no-interaction"],
                     entry["abs"], ctx, parse_composer_outdated, "packages")


def outdated_swift(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return result("swift", "unsupported", "SwiftPM has no outdated report — not checked")


def outdated_gradle(entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    return result("gradle", "unsupported", "needs the Gradle Versions plugin (`./gradlew dependencyUpdates`) — not checked")


OUTDATED = {"python": outdated_python, "js": outdated_js, "rust": outdated_rust, "go": outdated_go,
            "ruby": outdated_ruby, "php": outdated_php, "swift": outdated_swift, "gradle": outdated_gradle}


def _guarded(fn: Callable[[Dict[str, Any], Ctx], Dict[str, Any]], entry: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    try:
        return fn(entry, ctx)
    except Exception as e:  # one broken check must not hide the others
        return result(fn.__name__, "failed", f"internal error: {type(e).__name__}: {e}")


def check(root: str, what: str, timeout: float) -> Dict[str, Any]:
    entries = scan(root)
    ctx = Ctx(os.path.realpath(root), timeout)
    with ThreadPoolExecutor(max_workers=4) as pool:
        if what in ("audit", "all"):
            for entry, res in zip(entries, pool.map(lambda e: _guarded(AUDIT[e["ecosystem"]], e, ctx), entries)):
                entry["audit"] = res
            osv_fallback(entries, ctx)
        if what in ("outdated", "all"):
            for entry, res in zip(entries, pool.map(lambda e: _guarded(OUTDATED[e["ecosystem"]], e, ctx), entries)):
                entry["outdated"] = res
    vulns = sum(len(e.get("audit", {}).get("vulns") or []) for e in entries)
    outdated = [p for e in entries for p in e.get("outdated", {}).get("packages") or []]
    unchecked = sum(1 for e in entries for k in ("audit", "outdated") if e.get(k, {}).get("status") in NOT_CHECKED)
    summary = {"ecosystems": len(entries), "vulnerabilities": vulns, "outdated": len(outdated),
               "outdated_major": sum(1 for p in outdated if p["kind"] == "major"), "not_checked": unchecked}
    code = 1 if vulns or outdated else 2 if unchecked else 0
    return {"path": ctx.root, "command": what, "ecosystems": entries, "summary": summary, "exit": code}


# ─────────────────────────────────────────────── output ──────────────────────────────────────────────────────────────

def _title(entry: Dict[str, Any]) -> str:
    eco = entry["ecosystem"]
    label = LABELS[eco] + (f" ({entry['manager']})" if eco == "js" else "")
    bits = ", ".join(entry["manifests"][:6]) + (" …" if len(entry["manifests"]) > 6 else "")
    if entry.get("members"):
        bits += f"; +{len(entry['members'])} workspace member(s)"
    if eco == "python":
        bits += f"; env: {entry['venv']}" if entry.get("venv") else "; no .venv"
    return f"{label} · `{entry['dir']}` — {bits}"


def _vuln_line(v: Dict[str, Any]) -> str:
    parts = [f"**{v['severity'] or 'severity n/a'}** `{v['package']}`" + (f" {v['version']}" if v["version"] else "")]
    ident = v["id"] + (f" ({', '.join(v['aliases'][:2])})" if v["aliases"] else "")
    if ident:
        parts.append(ident)
    if v["affected"] and not v["version"]:
        parts.append(f"affects {v['affected']}")
    if v["fixed"]:
        parts.append(f"fixed in {v['fixed']}")
    if v["fix"]:
        parts.append(f"fix: {v['fix']}")
    line = " · ".join(parts) + (f" — {v['title']}" if v["title"] else "")
    return f"- {line}" + (f" ({v['note']})" if v["note"] else "")


def _not_checked(what: str, res: Dict[str, Any]) -> List[str]:
    lines = [f"- ⚠️ {what} not checked ({res['status']}): {res['message']}"]
    for key in ("install", "fallback_install"):
        if res.get(key):
            lines.append(f"  Install (ask the user first): `{res[key]}`")
    return lines


def _plan(report: Dict[str, Any]) -> List[str]:
    out = []
    for e in report["ecosystems"]:
        vulns = (e.get("audit") or {}).get("vulns") or []
        if not vulns:
            continue
        how = UPGRADE.get(e.get("manager") if e["ecosystem"] in ("js", "python") else e["ecosystem"], "")
        targets = []
        for v in vulns:
            target = f"`{v['package']}`" + (f" → {v['fixed']}" if v["fixed"] else "")
            if target not in targets:
                targets.append(target)
        out.append(f"  - {LABELS[e['ecosystem']]} `{e['dir']}`: {', '.join(targets[:8])}"
                   + (f" … (+{len(targets) - 8})" if len(targets) > 8 else "") + (f" — e.g. `{how}`" if how else ""))
    return out


def render(report: Dict[str, Any]) -> str:
    s, entries = report["summary"], report["ecosystems"]
    lines = [f"# Dependencies — {report['path']}", ""]
    if not entries:
        return "\n".join(lines + ["No dependency manifests found (looked for pyproject.toml, requirements*.txt, "
                                  "package.json, Cargo.toml, go.mod, Gemfile, Package.swift, build.gradle, composer.json)."])
    head = [f"{s['ecosystems']} ecosystem(s)"]
    if report["command"] in ("audit", "all"):
        head.append(f"{s['vulnerabilities']} known vulnerabilit{'y' if s['vulnerabilities'] == 1 else 'ies'}")
    if report["command"] in ("outdated", "all"):
        head.append(f"{s['outdated']} outdated ({s['outdated_major']} major)")
    if s["not_checked"]:
        head.append(f"{s['not_checked']} check(s) couldn't run")
    lines += [" · ".join(head), ""]
    for e in entries:
        lines.append(f"## {_title(e)}")
        aud = e.get("audit")
        if aud:
            if aud["status"] in NOT_CHECKED:
                lines += _not_checked("Vulnerabilities", aud)
            else:
                vulns = aud.get("vulns") or []
                note = f" ({aud['message']})" if aud.get("message") else ""
                lines.append(f"Vulnerabilities — {aud['tool']}: {len(vulns) or 'none known'}{note}")
                lines += [_vuln_line(v) for v in vulns[:15]]
                if len(vulns) > 15:
                    lines.append(f"- … and {len(vulns) - 15} more (`--json` lists all)")
        out = e.get("outdated")
        if out:
            if out["status"] in NOT_CHECKED:
                lines += _not_checked("Outdated", out)
            else:
                pkgs = out.get("packages") or []
                counts = {k: sum(1 for p in pkgs if p["kind"] == k) for k in KIND_RANK}
                summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "everything is current"
                lines.append(f"Outdated — {out['tool']}: {summary}")
                majors = [p for p in pkgs if p["kind"] == "major"]
                for p in majors[:15]:
                    note = f" ({p['note']})" if p["note"] else ""
                    lines.append(f"- major: `{p['package']}` {p['current'] or p['wanted'] or '?'} → {p['latest']}{note}")
                if len(majors) > 15:
                    lines.append(f"- … and {len(majors) - 15} more majors (`--json` lists all)")
                rest = [p for p in pkgs if p["kind"] != "major"]
                if rest:
                    shown = ", ".join(f"`{p['package']}` {p['current'] or p['wanted'] or '?'} → {p['latest']}"
                                      for p in rest[:6])
                    lines.append(f"- minor/patch: {shown}" + (f" … (+{len(rest) - 6})" if len(rest) > 6 else ""))
        lines.append("")
    lines.append("## What to do")
    plan = _plan(report)
    if plan:
        lines += ["- Propose this upgrade plan to the user — upgrade only on their request; critical/high first, "
                  "patch/minor fixes before majors:"] + plan
    if s.get("outdated_major"):
        lines.append("- Major updates may break the API: one at a time, after reading the changelog; not urgent "
                     "unless a security fix needs one.")
    if s["not_checked"]:
        lines.append("- Tell the user what couldn't be checked and the install command; never install tools yourself.")
    lines.append("- After any upgrade the user approves: run the project's tests (and build), then re-run "
                 "`deps.py audit`." if plan or s["outdated"] else "- Nothing to upgrade for what could be checked.")
    return "\n".join(lines)


def _tools(entry: Dict[str, Any]) -> List[Tuple[str, bool]]:
    eco = entry["ecosystem"]
    if eco == "python":
        names = ["pip-audit"] + (["uv"] if entry.get("python") else [])
        return [(n, bool(_venv_tool(entry, n))) for n in names]
    if eco == "js":
        exe = {"yarn-berry": "yarn"}.get(entry.get("manager", "npm"), entry.get("manager", "npm"))
        return [(exe, bool(shutil.which(exe)))]
    names = {"rust": ["cargo-audit", "cargo-outdated"], "go": ["govulncheck", "go"],
             "ruby": ["bundle-audit", "bundle"], "php": ["composer"]}.get(eco, [])
    return [(n, bool(shutil.which(n))) for n in names]


def render_scan(root: str, entries: List[Dict[str, Any]]) -> str:
    lines = [f"# Dependency manifests — {root}", ""]
    if not entries:
        return "\n".join(lines + ["No dependency manifests found."])
    for e in entries:
        tools = ", ".join(f"{n} {'✓' if ok else '✗ not installed'}" for n, ok in _tools(e))
        lines.append(f"- {_title(e)}" + (f" · tools: {tools}" if tools else ""))
    osv = bool(shutil.which("osv-scanner"))
    lines += ["", f"osv-scanner (fallback for any lockfile): {'✓' if osv else '✗ not installed — ' + INSTALL['osv-scanner']}"]
    return "\n".join(lines)


def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in ("abs", "python")}


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default=".", help="project folder (default: the current folder)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per tool (default 120)")
    ap = argparse.ArgumentParser(prog="deps.py", description="Dependency audit: known vulnerabilities and outdated "
                                 "packages, with the tools already installed.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("scan", "ecosystems, manifests and tools"), ("audit", "known vulnerabilities"),
                       ("outdated", "outdated packages"), ("all", "audit + outdated")):
        sub.add_parser(name, parents=[common], help=text)
    args = ap.parse_args(argv)
    root = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        print(f"deps.py: no such folder: {root}", file=sys.stderr)
        return 2
    if args.cmd == "scan":
        entries = scan(root)
        if args.json:
            print(json.dumps({"path": root, "ecosystems": [dict(_public(e), tools=dict(_tools(e))) for e in entries],
                              "osv_scanner": bool(shutil.which("osv-scanner"))}, indent=1, ensure_ascii=False))
        else:
            print(render_scan(root, entries))
        return 0
    report = check(root, args.cmd, max(0.1, args.timeout))
    if args.json:
        print(json.dumps(dict(report, ecosystems=[_public(e) for e in report["ecosystems"]]), indent=1,
                         ensure_ascii=False))
    else:
        print(render(report))
    return report["exit"]


if __name__ == "__main__":
    sys.exit(main())
