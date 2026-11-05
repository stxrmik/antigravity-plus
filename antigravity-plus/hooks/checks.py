#!/usr/bin/env python3
"""Antigravity Plus — quick checks of a file the agent just wrote: syntax first, then the project's own linters.

Only errors, only tools that are already there, nothing installed or reformatted:
  .py                     Python syntax: full compile when this interpreter is at least the project's Python
                          version, otherwise a tokenizer check (unclosed brackets and strings, broken indentation)
                          that doesn't depend on the version; ruff (project config, or its error-only rules) if installed
  .js .mjs .cjs           node --check (system Node or the one bundled with Antigravity); eslint with the project config
  .ts .tsx .jsx           eslint with the project config
  .swift                  swiftc -parse; swiftlint errors with the project's .swiftlint.yml
  .sh .bash .zsh          bash -n / sh -n / zsh -n; shellcheck -S error
  .json                   strict JSON (files with comments or trailing commas, i.e. JSONC, are skipped)
  .yml .yaml              YAML via Ruby's Psych (templated files with {{ }} are skipped)
  .rb Fastfile Podfile …  ruby -c
  .plist .entitlements …  plutil -lint
  .xml .storyboard .xib   xmllint --noout

  checks.py FILE [--root DIR]     print the problems of a file (exit 1 if any)

Used by hooks/check_edit.py (PostToolUse) and /doctor. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
from typing import Dict, List, Optional, Tuple

import compat

HOME = os.path.expanduser("~")
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local", "bin"), "/usr/bin", "/bin"]
TIMEOUT, LINT_TIMEOUT = 10, 30
MAX_BYTES = 2_000_000
RUBY_NAMES = {"Fastfile", "Appfile", "Matchfile", "Pluginfile", "Podfile", "Gemfile", "Rakefile", "Brewfile",
              "Dangerfile", "Snapfile", "Scanfile", "Gymfile", "Deliverfile", "Vagrantfile", "Guardfile"}
PLIST_EXT = (".plist", ".entitlements", ".strings", ".stringsdict", ".xcprivacy")
XML_EXT = (".xml", ".storyboard", ".xib", ".xcscheme", ".svg")
ESLINT_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts", ".eslintrc",
                  ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml")
RUFF_HIGH_SIGNAL = "E9,F63,F7,F82"   # syntax errors, invalid comparisons, misplaced statements, undefined names


# Secrets that must not land in a repository or in the conversation (also used by guard.py and /export).
SECRETS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("OpenAI / Anthropic key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("password in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:([^\s@/'\"]{4,})@", re.I)),
    ("hard-coded password or key", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
                                             r"auth[_-]?token|token|private[_-]?key)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")),
]
# Files that hold secrets by their name (the guard won't print them; /git-init keeps them out of the first commit).
SECRET_FILE = re.compile(r"(^|/)(\.env(\.(?!example\b|sample\b|template\b|dist\b)[\w.-]+)?|[\w.-]+\.env|"
                         r"id_(rsa|ed25519|ecdsa|dsa)|[\w.-]+\.(pem|key|p12|pfx|keystore|jks)|credentials\.json|"
                         r"secrets?\.(ya?ml|json|toml))$", re.I)
PLACEHOLDER = re.compile(r"(?i)example|changeme|change_me|your[_-]|xxx|\*\*\*|<[^>]*>|\$\{|\{\{|\$[A-Z_]|placeholder|"
                         r"dummy|fake|sample|redacted|скрыт|getenv|environ|process\.env")


def find_secrets(text: str) -> List[Tuple[int, str]]:
    """(line, kind) of likely secrets in text; values that look like placeholders or env lookups are skipped."""
    out = []
    for kind, rx in SECRETS:
        for m in rx.finditer(text):
            value = m.group(m.lastindex) if m.lastindex else m.group(0)
            if PLACEHOLDER.search(value) or PLACEHOLDER.search(m.group(0)):
                continue
            out.append((text.count("\n", 0, m.start()) + 1, kind))
    return sorted(set(out))


def redact(text: str) -> str:
    """Replace likely secrets with «[скрыто]» (for exports)."""
    for _kind, rx in SECRETS:
        def hide(m):
            value = m.group(m.lastindex) if m.lastindex else m.group(0)
            return m.group(0) if PLACEHOLDER.search(value) else m.group(0).replace(value, "[скрыто]")
        text = rx.sub(hide, text)
    return text


def secret_problems(path: str, root: str) -> List[dict]:
    """Secrets written into a file git would commit (in a git repo, not ignored)."""
    r = run(["git", "check-ignore", "-q", path], cwd=os.path.dirname(path) or root)
    if not r or r[0] == 0 or "not a git repository" in r[1]:
        return []  # ignored by git, or no git here: nothing would be committed
    return [problem("secrets", f"possible {kind}: keep it out of the repository (.env or another ignored file)",
                    line, None, "secret") for line, kind in find_secrets(read(path, MAX_BYTES))[:5]]


def problem(tool: str, msg: str, line=None, col=None, kind: str = "syntax", code: str = "") -> dict:
    return {"tool": tool, "msg": re.sub(r"\s+", " ", msg).strip()[:300], "line": line, "col": col,
            "kind": kind, "code": code}


def env() -> Dict[str, str]:
    e = dict(os.environ)
    parts = e.get("PATH", "").split(os.pathsep)
    e["PATH"] = os.pathsep.join(parts + [p for p in EXTRA_PATH if p not in parts])
    return e


def which(name: str, root: Optional[str] = None) -> Optional[str]:
    if root:
        for sub in (".venv/bin", "venv/bin", "node_modules/.bin"):
            p = os.path.join(root, sub, name)
            if os.access(p, os.X_OK):
                return p
    return shutil.which(name, path=env()["PATH"])


def run(cmd: List[str], cwd: Optional[str] = None, timeout: int = TIMEOUT, extra_env: Optional[dict] = None):
    """(exit code, stdout+stderr) or None when the tool can't run in time."""
    e = env()
    e.update(extra_env or {})
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=e)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def node() -> Optional[Tuple[str, dict]]:
    """A Node to run JS tools with: the system one, or the one Antigravity bundles (Node 24)."""
    p = which("node")
    if p:
        return p, {}
    own = compat.antigravity_node()
    if own:
        return own, {"ELECTRON_RUN_AS_NODE": "1"}
    return None


# ─────────────────────────────── Python ───────────────────────────────

def python_target(root: str) -> Optional[Tuple[int, int]]:
    """The project's Python version: .python-version, requires-python, runtime.txt or a python Docker image."""
    def ver(text: str, rx: str) -> Optional[Tuple[int, int]]:
        m = re.search(rx, text, re.M | re.I)
        return (int(m.group(1)), int(m.group(2))) if m else None
    for name, rx in ((".python-version", r"^\s*(?:python-)?(\d)\.(\d+)"),
                     ("pyproject.toml", r"^\s*requires-python\s*=\s*[\"'][^\"'\d]*(\d)\.(\d+)"),
                     ("runtime.txt", r"python-(\d)\.(\d+)"),
                     ("Dockerfile", r"^\s*FROM\s+(?:\S+/)?python:(\d)\.(\d+)")):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                v = ver(f.read(100_000), rx)
        except OSError:
            continue
        if v:
            return v
    return None


def tokenizer_problem(src: str) -> Optional[dict]:
    """Errors any Python version would reject: unclosed or mismatched brackets, unterminated strings, bad dedent."""
    stack: List[Tuple[str, int]] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.OP:
                if tok.string in "([{":
                    stack.append((tok.string, tok.start[0]))
                elif tok.string in ")]}":
                    if not stack:
                        return problem("python", f"unmatched '{tok.string}'", tok.start[0], tok.start[1] + 1)
                    opener, line = stack.pop()
                    if pairs[tok.string] != opener:
                        return problem("python", f"'{tok.string}' does not match '{opener}' opened on line {line}",
                                       tok.start[0], tok.start[1] + 1)
            elif tok.type == tokenize.ERRORTOKEN and tok.string.strip():
                what = "unterminated string" if tok.string.strip()[:1] in "'\"" else f"invalid character {tok.string!r}"
                return problem("python", what, tok.start[0], tok.start[1] + 1)
    except IndentationError as e:
        return problem("python", e.msg, e.lineno, None)
    except tokenize.TokenError as e:
        msg, (line, col) = e.args[0], e.args[1]
        if "multi-line statement" in msg and stack:
            opener, at = stack[-1]
            return problem("python", f"'{opener}' opened on line {at} is never closed", at, None)
        return problem("python", "unterminated triple-quoted string" if "string" in msg else msg, line, None)
    return None


def check_python(path: str, root: str) -> List[dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    tok = tokenizer_problem(src)
    if tok:
        return [tok]
    target = python_target(root)
    if target and target <= sys.version_info[:2]:  # this interpreter knows all the syntax the project may use
        try:
            compile(src, path, "exec", dont_inherit=True)
        except SyntaxError as e:
            return [problem("python", e.msg, e.lineno, e.offset)]
        except ValueError:
            pass
    ruff = which("ruff", root)
    if not ruff:
        return []
    own = any(os.path.isfile(os.path.join(root, n)) for n in ("ruff.toml", ".ruff.toml")) or \
        "[tool.ruff" in read(os.path.join(root, "pyproject.toml"))
    cmd = [ruff, "check", "--no-fix", "--output-format", "concise", "--quiet", path]
    if not own:
        cmd[2:2] = ["--isolated", "--select", RUFF_HIGH_SIGNAL]
    r = run(cmd, cwd=root, timeout=LINT_TIMEOUT)
    out = []
    for ln in (r[1].splitlines() if r and r[0] else []):
        m = re.match(r"^.*?:(\d+):(\d+): (?:([A-Z]+\d+) )?(.*)$", ln)
        if m:
            code = m.group(3) or ""
            kind = "syntax" if not code or code.startswith("E9") else "lint"
            out.append(problem("ruff", m.group(4), int(m.group(1)), int(m.group(2)), kind, code))
    return out


def read(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


# ─────────────────────────────── other languages ───────────────────────────────

def check_js(path: str, root: str) -> List[dict]:
    out: List[dict] = []
    if path.endswith((".js", ".mjs", ".cjs")) and "@flow" not in read(path, 2000) \
            and not any(os.path.exists(os.path.join(root, n)) for n in (".babelrc", "babel.config.js", ".babelrc.json")):
        n = node()
        r = run([n[0], "--check", path], extra_env=n[1]) if n else None
        if r and r[0]:
            line = re.search(r":(\d+)\s*$", r[1].splitlines()[0]) if r[1].strip() else None
            msg = re.search(r"^(SyntaxError|ReferenceError|TypeError|Error): (.*)$", r[1], re.M)
            if msg and "Unexpected token '<'" not in msg.group(2):  # JSX in a .js file is not ours to judge
                out.append(problem("node", msg.group(2), int(line.group(1)) if line else None))
    if out:
        return out
    if any(os.path.exists(os.path.join(root, c)) for c in ESLINT_CONFIGS) or '"eslintConfig"' in read(os.path.join(root, "package.json")):
        js = os.path.join(root, "node_modules", "eslint", "bin", "eslint.js")
        n = node()
        if os.path.isfile(js) and n:
            r = run([n[0], js, "--quiet", "--format", "json", path], cwd=root, timeout=LINT_TIMEOUT, extra_env=n[1])
            try:
                results = json.loads(r[1][r[1].index("["):]) if r and "[" in r[1] else []
            except ValueError:
                results = []
            for res in results:
                for m in res.get("messages") or []:
                    if m.get("severity") == 2:
                        out.append(problem("eslint", m.get("message", ""), m.get("line"), m.get("column"),
                                           "syntax" if m.get("fatal") else "lint", m.get("ruleId") or ""))
    return out


def check_swift(path: str, root: str) -> List[dict]:
    out = []
    swiftc = which("swiftc")
    r = run([swiftc, "-parse", path]) if swiftc else None
    for ln in (r[1].splitlines() if r and r[0] else []):
        m = re.match(r"^.*?:(\d+):(\d+): error: (.*)$", ln)
        if m:
            out.append(problem("swiftc", m.group(3), int(m.group(1)), int(m.group(2))))
    if out:
        return out
    swiftlint = which("swiftlint")
    if swiftlint and os.path.isfile(os.path.join(root, ".swiftlint.yml")):
        r = run([swiftlint, "lint", "--quiet", path], cwd=root, timeout=LINT_TIMEOUT)
        for ln in (r[1].splitlines() if r else []):
            m = re.match(r"^.*?:(\d+):(\d+): error: (.*?)(?: \((\w+)\))?$", ln)
            if m:
                out.append(problem("swiftlint", m.group(3), int(m.group(1)), int(m.group(2)), "lint", m.group(4) or ""))
    return out


def check_shell(path: str, root: str) -> List[dict]:
    first = read(path, 200).split("\n", 1)[0]
    shell = "zsh" if path.endswith(".zsh") or "zsh" in first else "sh" if re.search(r"\bsh\b", first) and "bash" not in first else "bash"
    r = run([shell, "-n", path])
    out = []
    for ln in (r[1].splitlines() if r and r[0] else []):
        m = re.search(r"line (\d+): (.*)$", ln) or re.search(r":(\d+): (.*)$", ln)
        if m:
            out.append(problem(f"{shell} -n", m.group(2), int(m.group(1))))
    if out or shell == "zsh":
        return out
    sc = which("shellcheck")
    r = run([sc, "-S", "error", "-f", "gcc", path]) if sc else None
    for ln in (r[1].splitlines() if r and r[0] else []):
        m = re.match(r"^.*?:(\d+):(\d+): error: (.*?)(?: \[(SC\d+)\])?$", ln)
        if m:
            out.append(problem("shellcheck", m.group(3), int(m.group(1)), int(m.group(2)), "syntax", m.group(4) or ""))
    return out


def check_json(path: str, root: str) -> List[dict]:
    text = read(path, MAX_BYTES)
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        if re.search(r"^\s*//|/\*|,\s*[}\]]", text, re.M):
            return []  # JSONC (tsconfig, VS Code settings…): comments and trailing commas are allowed there
        return [problem("json", e.msg, e.lineno, e.colno)]
    return []


def check_yaml(path: str, root: str) -> List[dict]:
    text = read(path, MAX_BYTES)
    if re.search(r"(^|[:\-]\s+)\{\{|\{%", text, re.M):
        return []  # Helm / Jinja templates aren't plain YAML
    ruby = which("ruby")
    r = run([ruby, "-e", "require 'psych'; Psych.parse_stream(File.read(ARGV[0]))", path]) if ruby else None
    if not r or not r[0]:
        return []
    m = re.search(r"\): (.*?) at line (\d+) column (\d+)", r[1])
    return [problem("yaml", m.group(1), int(m.group(2)), int(m.group(3)))] if m else []


def check_ruby(path: str, root: str) -> List[dict]:
    ruby = which("ruby")
    r = run([ruby, "-c", path]) if ruby else None
    if not r or not r[0]:
        return []
    m = re.search(r":(\d+): (.*)$", r[1], re.M)
    return [problem("ruby -c", m.group(2), int(m.group(1)))] if m else []


def check_plist(path: str, root: str) -> List[dict]:
    r = run(["plutil", "-lint", path])
    if not r or not r[0]:
        return []
    m = re.search(r": \((.*?)(?: at line (\d+))?\)\s*$", r[1].strip(), re.M)
    return [problem("plutil", m.group(1) if m else r[1].strip()[-200:], int(m.group(2)) if m and m.group(2) else None)]


def check_xml(path: str, root: str) -> List[dict]:
    r = run(["xmllint", "--noout", path])
    if not r or not r[0]:
        return []
    m = re.search(r":(\d+): (?:parser )?error ?: (.*)$", r[1], re.M)
    return [problem("xmllint", m.group(2), int(m.group(1)))] if m else []


def checker_for(path: str):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext == ".py":
        return check_python
    if ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts"):
        return check_js
    if ext == ".swift":
        return check_swift
    if ext in (".sh", ".bash", ".zsh"):
        return check_shell
    if ext == ".json":
        return check_json
    if ext in (".yml", ".yaml"):
        return check_yaml
    if ext in (".rb", ".rake", ".gemspec", ".podspec") or name in RUBY_NAMES:
        return check_ruby
    if ext in PLIST_EXT:
        return check_plist
    if ext in XML_EXT:
        return check_xml
    return None


def check_file(path: str, root: Optional[str] = None) -> Optional[List[dict]]:
    """Problems found in `path` ([] = clean), or None when there is nothing to check it with."""
    fn = checker_for(path)
    if not fn or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
        return None
    return fn(path, root or os.path.dirname(path))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--root")
    a = ap.parse_args(argv)
    probs = check_file(os.path.abspath(a.file), a.root and os.path.abspath(a.root))
    if probs is None:
        print("no checker for this file type")
        return 0
    for p in probs:
        where = ":".join(str(x) for x in (p["line"], p["col"]) if x)
        print(f"{p['tool']}: {where} {p['msg']}" + (f" [{p['code']}]" if p["code"] else ""))
    print("OK" if not probs else f"{len(probs)} problem(s)")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
