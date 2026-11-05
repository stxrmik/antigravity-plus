#!/usr/bin/env python3
"""Antigravity Plus — /git-init: put a project under git, safely, so every later change can be reviewed and undone.

  repo_init.py [--path DIR] [--dry-run] [--no-commit]

1. Checks that DIR (default: the current folder) is one project: not the home folder or a container such as
   ~/Downloads, not inside another repository, no other version control, no repositories nested inside (that's a
   folder of projects), not tens of thousands of files.
2. git init (branch main) and a .gitignore for the stack it finds — Python, Node, Swift/Xcode, Gradle, Rust,
   Flutter, Terraform… — plus OS junk and secrets (.env, keys); an existing .gitignore only gets what's missing.
3. Stages everything, leaves out files that hold secrets (by name or content), local databases and files over
   50 MB, and commits
   the current state as "Initial snapshot". Nothing leaves the computer: no remote, no push.

Prints what it did and how to undo it. Exit 0 = done (or already a repository), 2 = refused (the reason is printed).
Also imported by hooks/session_start.py and hooks/stop.py (`refusal`). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..", "hooks")))
import checks  # noqa: E402  (secret patterns shared with the guard and the edit checks)

HOME = os.path.realpath(os.path.expanduser("~"))
WINDOWS = os.name == "nt"
key = os.path.normcase  # Windows paths compare case-insensitively
# Folders that hold projects rather than being one: roots, the home folder and the usual folders in it.
CONTAINERS = {HOME, "/", "/Users", "/home", "/root", "/Applications", "/Library", "/System", "/Volumes", "/private",
              "/tmp", "/private/tmp", "/var", "/private/var", "/usr", "/usr/local", "/opt", "/opt/homebrew", "/srv",
              "/mnt", "/media", "/snap"} | {
    os.path.join(HOME, *d.split("/")) for d in (
        "Downloads", "Desktop", "Documents", "Library", "Movies", "Music", "Pictures", "Videos", "Public",
        "Applications", "Projects", "projects", "Developer", "dev", "src", "code", "Code", "repos", "git", "GitHub",
        "work", "Sites", "OneDrive", "source", "source/repos", "PycharmProjects", "IdeaProjects",
        "AndroidStudioProjects", "WebstormProjects", "XcodeProjects")}
SYSTEM_PREFIXES = ["/System/", "/usr/", "/bin/", "/sbin/", "/etc/", "/private/etc/", "/Library/", "/Applications/",
                   "/proc/", "/sys/", "/dev/", "/boot/", os.path.join(HOME, "Library") + os.sep]
if WINDOWS:
    drive = os.path.splitdrive(HOME)[0] + os.sep                                   # C:\
    win_dirs = [os.environ.get(v, "") for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
    CONTAINERS |= {drive, os.path.dirname(HOME)} | {d for d in win_dirs if d}       # C:\, C:\Users, C:\Windows…
    SYSTEM_PREFIXES += [d + os.sep for d in win_dirs if d] + [os.path.join(HOME, "AppData") + os.sep]
CONTAINERS = {key(c) for c in CONTAINERS}
SYSTEM_PREFIXES = tuple(key(p) for p in SYSTEM_PREFIXES)
OTHER_VCS = (".hg", ".svn", ".bzr", "_darcs", "CVS")
# Dependency and build folders: not counted, not searched for nested repositories.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "DerivedData", ".build", "build", "dist",
             "target", ".gradle", "Pods", "Carthage", ".next", ".nuxt", ".svelte-kit", ".dart_tool", ".terraform",
             ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", "vendor", ".swiftpm", "coverage"}
MAX_FILES = 30_000
LARGE = 50 * 1024 * 1024        # files above this stay out of the first commit
DATABASE = re.compile(r"\.(db|sqlite3?|db-wal|db-shm|db-journal)$", re.I)  # local databases may hold user data
SCAN_LIMIT = 1_000_000          # bytes of one text file checked for secrets
SCAN_BUDGET = 200_000_000       # bytes checked in total
COMMIT_MESSAGE = ["Initial snapshot", "The project as it was before the agent's changes "
                  "(created by Antigravity Plus /git-init)."]

COMMON = ["# OS and editor files", ".DS_Store", "Thumbs.db", "*.swp", ".idea/", "",
          "# Secrets: keep them out of git", ".env", ".env.*", "!.env.example", "!.env.sample", "!.env.template",
          "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa", "id_ed25519", "id_ecdsa",
          "credentials.json", ".netrc", "", "# Logs", "*.log"]
# (name, marker file names or suffixes, ignore rules)
STACKS: List[Tuple[str, Tuple[str, ...], List[str]]] = [
    ("Python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", ".py"),
     ["__pycache__/", "*.py[cod]", ".venv/", "venv/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
      "*.egg-info/", ".coverage", "htmlcov/", "dist/", "build/"]),
    ("Node", ("package.json",),
     ["node_modules/", "dist/", "build/", ".next/", ".nuxt/", ".svelte-kit/", ".turbo/", ".vercel/", ".parcel-cache/",
      "coverage/", "npm-debug.log*", "yarn-error.log*"]),
    ("Swift/Xcode", (".xcodeproj", ".xcworkspace", "Package.swift"),
     ["DerivedData/", "xcuserdata/", "*.xcuserstate", ".build/", ".swiftpm/", "build/", "*.ipa", "*.dSYM.zip"]),
    ("Gradle/Android", ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
     [".gradle/", "build/", "local.properties", "*.iml", ".cxx/", "captures/", "*.apk", "*.aab"]),
    ("Maven", ("pom.xml",), ["target/"]),
    ("Rust", ("Cargo.toml",), ["target/"]),
    ("Flutter/Dart", ("pubspec.yaml",), [".dart_tool/", ".flutter-plugins", ".flutter-plugins-dependencies", "build/"]),
    ("PHP", ("composer.json",), ["vendor/"]),
    ("Ruby", ("Gemfile",), [".bundle/", "vendor/bundle/"]),
    ("Terraform", (".tf",), [".terraform/", "*.tfstate", "*.tfstate.*", "*.tfvars"]),
]
SAMPLE = {"*.py[cod]": "x.pyc"}  # rules whose test path can't be made by replacing * with x


def git(args: List[str], cwd: str, timeout: int = 300) -> Tuple[int, str]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr or r.stdout).strip()


def walk(root: str):
    """(dirpath, dirnames, filenames) below root without dependency/build folders."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.endswith((".xcassets", ".app", ".framework"))]
        yield dp, dns, fns


def place_refusal(root: str) -> Optional[str]:
    """Why `root` can't be a project folder at all: the home folder, a container like ~/Downloads, a system,
    app-data or hidden folder (cheap: no file walk)."""
    if key(root) in CONTAINERS or key(HOME).startswith(key(root).rstrip(os.sep) + os.sep):
        return f"{root} holds projects rather than being one; run it in a project folder (--path)"
    inside_home = key(root).startswith(key(HOME) + os.sep)
    under_home = os.path.relpath(root, HOME).split(os.sep) if inside_home else []
    if key(root + os.sep).startswith(SYSTEM_PREFIXES) or any(part.startswith(".") for part in under_home):
        return f"{root} is a system, app-data or hidden folder, not a project"
    return None


def refusal(path: str) -> Optional[str]:
    """Why a repository shouldn't be created in `path` (None = fine, or it is a repository already)."""
    root = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(root):
        return f"{root} is not a folder"
    why = place_refusal(root)
    if why:
        return why
    vcs = [v for v in OTHER_VCS if os.path.exists(os.path.join(root, v))]
    if vcs:
        return f"the folder already uses another version control ({vcs[0]})"
    nested, count = [], 0
    for dp, dns, fns in walk(root):
        if dp != root and (".git" in fns or os.path.isdir(os.path.join(dp, ".git"))):
            nested.append(os.path.relpath(dp, root))
            dns[:] = []
            continue
        count += len(fns)
        if count > MAX_FILES:
            return f"more than {MAX_FILES:,} files outside dependency folders: it doesn't look like one project"
    if nested:
        return ("it contains other git repositories (" + ", ".join(nested[:3]) + (", …" if len(nested) > 3 else "")
                + "): a folder of projects — create the repository inside the project you work on (--path)")
    return None


def stacks(root: str) -> List[Tuple[str, List[str]]]:
    names = set()
    for dp, dns, fns in walk(root):
        names.update(fns)
        names.update(dns)
        if dp[len(root):].count(os.sep) >= 2:
            dns[:] = []
    found = []
    for name, markers, rules in STACKS:
        if any(n == m or (m.startswith(".") and n.endswith(m)) for n in names for m in markers):
            found.append((name, rules))
    return found


def sample(rule: str) -> str:
    rule = rule.lstrip("/")
    if rule in SAMPLE:
        return SAMPLE[rule]
    return rule.rstrip("/").replace("*", "x") + ("/x" if rule.endswith("/") else "")


def write_gitignore(root: str, found: List[Tuple[str, List[str]]]) -> str:
    """Create .gitignore, or add what's missing to the existing one. Returns a summary line."""
    path = os.path.join(root, ".gitignore")
    if not os.path.exists(path):
        lines = list(COMMON)
        seen = set(lines)
        for name, rules in found:
            fresh = [r for r in rules if r not in seen]
            if fresh:
                lines += ["", f"# {name}"] + fresh
                seen.update(fresh)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        rules_n = sum(1 for ln in lines if ln and not ln.startswith("#"))
        kinds = ", ".join(n for n, _r in found) or "generic"
        return f".gitignore: created ({kinds}, secrets, OS files; {rules_n} rules)"
    # Existing file: add secret rules it lacks, and stack rules for folders/files that are actually here.
    stack_rules = [r for _n, rules in found for r in rules if os.path.exists(os.path.join(root, r.rstrip("/")))]
    wanted = [r for r in COMMON[COMMON.index("# Secrets: keep them out of git") + 1:] if r and not r.startswith("#")]
    missing = []
    for rule in dict.fromkeys(wanted + stack_rules + [".DS_Store"]):
        if rule.startswith("!"):
            continue
        code, _ = git(["check-ignore", "-q", "--no-index", sample(rule)], root)
        if code == 1:  # 1 = not ignored
            missing.append(rule)
    if not missing:
        return ".gitignore: kept as is (it already covers secrets and build folders)"
    if any(r.startswith(".env") for r in missing):
        missing += [r for r in ("!.env.example", "!.env.sample", "!.env.template")]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n# Added by git-init (Antigravity Plus)\n" + "\n".join(missing) + "\n")
    shown = [r for r in missing if not r.startswith("!")]
    return f".gitignore: added {len(shown)} missing rules (" + ", ".join(shown[:6]) + (", …" if len(shown) > 6 else "") + ")"


def staged_files(root: str) -> List[str]:
    code, out = git(["diff", "--cached", "--name-only", "-z"], root)
    return [p for p in out.split("\0") if p] if code == 0 else []


def unstage(root: str, paths: List[str]) -> None:
    for i in range(0, len(paths), 100):
        git(["rm", "--cached", "-q", "--"] + paths[i:i + 100], root)


def screen(root: str) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """Unstage files that hold secrets, local databases and huge files.
    Returns ([(path, why)], [huge paths], [database paths]); the last two go to .gitignore."""
    secret, large, dbs, budget = [], [], [], SCAN_BUDGET
    for rel in staged_files(root):
        p = os.path.join(root, rel)
        if checks.SECRET_FILE.search(rel) and not rel.endswith(".pub"):
            secret.append((rel, "a secrets file"))
            continue
        if DATABASE.search(rel):
            dbs.append(rel)
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not os.path.isfile(p) or os.path.islink(p):
            continue
        if st.st_size > LARGE:
            large.append(rel)
            continue
        if st.st_size > SCAN_LIMIT or budget <= 0:
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        budget -= len(data)
        if b"\0" in data[:8000]:
            continue
        kinds = sorted({k for _line, k in checks.find_secrets(data.decode("utf-8", errors="replace"))})
        if kinds:
            secret.append((rel, "looks like it holds a secret (" + ", ".join(kinds) + ")"))
    unstage(root, [p for p, _w in secret] + large + dbs)
    blocks = [(title, paths) for title, paths in (("Over 50 MB", large), ("Local databases (may hold user data)", dbs))
              if paths]
    if blocks:
        with open(os.path.join(root, ".gitignore"), "a", encoding="utf-8") as f:
            for title, paths in blocks:
                f.write(f"\n# {title}, left out by git-init\n"
                        + "".join("/" + re.sub(r"([*?\[\]\\!#])", r"\\\1", p) + "\n" for p in paths))
        git(["add", "--", ".gitignore"], root)
    return secret, large, dbs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", "--workspace", dest="path", default=os.getcwd(), help="the project folder")
    ap.add_argument("--dry-run", action="store_true", help="only say what would happen")
    ap.add_argument("--no-commit", action="store_true", help="create the repository and .gitignore, commit nothing")
    a = ap.parse_args(argv)
    root = os.path.realpath(os.path.expanduser(a.path))

    if not shutil.which("git"):
        print("git isn't installed. On a Mac: xcode-select --install")
        return 2
    code, top = git(["rev-parse", "--show-toplevel"], root) if os.path.isdir(root) else (1, "")
    if code == 0:
        print(f"Already a git repository: {top} — nothing to do.")
        return 0
    why = refusal(root)
    if why:
        print(f"Not creating a repository here: {why}.")
        return 2
    found = stacks(root)
    if a.dry_run:
        exists = os.path.exists(os.path.join(root, ".gitignore"))
        print(f"Would create a git repository in {root} (branch main)"
              + f", {'add missing rules to' if exists else 'create'} .gitignore"
              + (f" for {', '.join(n for n, _r in found)}" if found else "")
              + ("" if a.no_commit else ", and commit the current state without secrets, local databases and "
                 "files over 50 MB") + ".")
        return 0

    created_ignore = not os.path.exists(os.path.join(root, ".gitignore"))
    code, out = git(["init", "-q", "-b", "main"], root)
    if code != 0:  # git older than 2.28
        code, out = git(["init", "-q"], root)
        if code == 0:
            git(["symbolic-ref", "HEAD", "refs/heads/main"], root)
    if code != 0:
        print(f"git init failed: {out}")
        return 2
    print(f"Created a git repository in {root} (branch main).")
    print(write_gitignore(root, found))
    undo = f'rm -rf "{os.path.join(root, ".git")}"' + (f' "{os.path.join(root, ".gitignore")}"' if created_ignore else "")
    if a.no_commit:
        print(f"Nothing committed (--no-commit). Undo: {undo}")
        return 0

    code, out = git(["add", "-A"], root)
    if code != 0:
        print(f"git add failed: {out}\nUndo: {undo}")
        return 2
    secret, large, dbs = screen(root)
    files = staged_files(root)
    code, out = git(["commit", "-q", "-m", COMMIT_MESSAGE[0], "-m", COMMIT_MESSAGE[1]], root, timeout=120)
    if code == 0:
        sha = git(["rev-parse", "--short", "HEAD"], root)[1]
        print(f'Committed the current state: {len(files)} files, "{COMMIT_MESSAGE[0]}" ({sha}). Nothing was pushed.')
    else:
        last = out.splitlines()[-1] if out else "unknown error"
        hint = (" Git doesn't know who you are: ask the user for a name and email, then "
                "git config --global user.name \"…\" && git config --global user.email \"…\"."
                if "user.name" in out or "identity" in out.lower() or "email" in out.lower() else "")
        print(f"Repository created, but the first commit failed: {last}.{hint} Files are staged; commit later.")
    if secret or large or dbs:
        print("Left out of the commit:")
        for rel, why in secret:
            print(f"  - {rel}: {why}. Move the value to .env (ignored), then commit the file.")
        for rel in large:
            print(f"  - {rel}: over 50 MB, now in .gitignore.")
        for rel in dbs:
            print(f"  - {rel}: a local database (may hold user data), now in .gitignore.")
    print(f"Undo everything: {undo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
