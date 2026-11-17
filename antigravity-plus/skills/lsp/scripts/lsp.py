#!/usr/bin/env python3
"""Antigravity Plus — /lsp: code navigation for agents through the project's language servers (LSP).

Antigravity's agents have no code-intelligence tool: they look symbols up with rg, which can't tell a call from a
comment, a string or another symbol with the same name. This script asks a real language server — pyright for
Python, sourcekit-lsp for Swift, kotlin-lsp for Kotlin, typescript-language-server, gopls, rust-analyzer, clangd,
… whichever is installed — and prints short answers sized for an agent's context:

  def | refs | hover | impl | type | calls  <file>:<line> <name>    also <file>:<line>:<col>, <file> <name>, <name>
  symbols <file>     find <name>     check <file>…     rename <position> --to <new> [--apply]
  servers [--path P]     status     stop [--all]

Servers stay warm: the first command for a (server, project root) starts a background process (`lsp.py daemon`)
that keeps the server running and answers later commands over a local socket (a Unix socket; on Windows TCP on
127.0.0.1 with a token). It stops the server after AGP_LSP_IDLE seconds without a command (600), and at most
AGP_LSP_MAX (3) run at once — the oldest stops first. `--no-daemon` (or AGP_LSP_DAEMON=0) runs a server for just
one command. Files on disk are the truth: open documents and changed files are synced before every command.
State and logs: ~/.gemini/antigravity-plus/lsp/. Other servers or preferences: ~/.gemini/antigravity-plus/lsp.json,
e.g. {"python": "basedpyright"} or {"python": {"command": ["/path/server", "--stdio"]}}.

For the hooks: session_lines(ws) (the languages here and the exact commands, for the session context) and
edit_problems(path) (errors an edit introduced, asked only of a server that is already running).

Stdlib only, Python 3.8+, macOS / Linux / Windows.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import compat  # noqa: E402

WINDOWS = os.name == "nt"
HIDE = {"creationflags": 0x08000000} if WINDOWS else {}  # CREATE_NO_WINDOW: the detached daemon has no console
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp")
CONFIG = os.path.join(HOME, ".gemini", "antigravity-plus", "lsp.json")
IDLE = float(os.environ.get("AGP_LSP_IDLE") or 600)
MAX_DAEMONS = int(os.environ.get("AGP_LSP_MAX") or 3)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", ".env", "dist", "build", "out", "target", "Pods",
             "DerivedData", ".build", ".swiftpm", "__pycache__", ".gradle", ".idea", "vendor", "Carthage", ".tox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", "coverage", "site-packages"}
SCAN_LIMIT = 60000      # files looked at when syncing a workspace or counting its languages
SHOW_REFS = 80          # reference lines printed unless --limit says otherwise
TEXT_WIDTH = 150        # a code line is cut around the symbol beyond this

# Language → file extensions and the servers that speak it, best first.
LANGS: Dict[str, dict] = {
    "python": {"ext": (".py", ".pyi"), "servers": ("pyright", "basedpyright", "ty", "pyrefly", "pylsp", "jedi")},
    "swift": {"ext": (".swift",), "servers": ("sourcekit-lsp",)},
    "kotlin": {"ext": (".kt", ".kts"), "servers": ("kotlin-lsp", "kotlin-language-server")},
    "typescript": {"ext": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
                   "servers": ("typescript-language-server", "vtsls")},
    "go": {"ext": (".go",), "servers": ("gopls",)},
    "rust": {"ext": (".rs",), "servers": ("rust-analyzer",)},
    "c": {"ext": (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm"), "servers": ("clangd",)},
    "java": {"ext": (".java",), "servers": ("jdtls",)},
    "ruby": {"ext": (".rb",), "servers": ("ruby-lsp", "solargraph")},
    "php": {"ext": (".php",), "servers": ("intelephense", "phpactor")},
    "csharp": {"ext": (".cs",), "servers": ("csharp-ls",)},
    "dart": {"ext": (".dart",), "servers": ("dart",)},
    "lua": {"ext": (".lua",), "servers": ("lua-language-server",)},
}
ALL_EXTS = {e for info in LANGS.values() for e in info["ext"]}
LANGUAGE_IDS = {".tsx": "typescriptreact", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
                ".jsx": "javascriptreact", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
                ".hxx": "cpp", ".m": "objective-c", ".mm": "objective-cpp"}

# Server → executables to look for, arguments, readiness signal, how long the first command waits for it ("wait", 30 s
# by default; later commands never wait, they add a note while the server is busy), start-up budget for `initialize`,
# whether it needs a file opened before questions about it, and how to install it. "ready": pyright = its «Found N
# source files» log line (before it, references come only from open files); rust = rust-analyzer's quiescent status;
# otherwise no $/progress for a while. clangd indexes in the background for minutes and answers meanwhile: it hardly
# waits.
SERVERS: Dict[str, dict] = {
    "pyright": {"bin": ("pyright-langserver",), "args": ["--stdio"], "ready": "pyright", "wait": 30, "startup": 30,
                "install": "pipx install 'pyright[nodejs]'  (or: npm install -g pyright)"},
    "basedpyright": {"bin": ("basedpyright-langserver",), "args": ["--stdio"], "ready": "pyright", "wait": 30,
                     "startup": 30, "install": "pipx install basedpyright"},
    "ty": {"bin": ("ty",), "args": ["server"], "install": "uv tool install ty"},
    "pyrefly": {"bin": ("pyrefly",), "args": ["lsp"], "install": "pipx install pyrefly"},
    "pylsp": {"bin": ("pylsp",), "args": [], "install": "pipx install python-lsp-server"},
    "jedi": {"bin": ("jedi-language-server",), "args": [], "install": "pipx install jedi-language-server"},
    "sourcekit-lsp": {"bin": ("sourcekit-lsp",), "args": [], "xcrun": True, "wait": 60, "startup": 180, "open": True,
                      "install": "Xcode (macOS) or a Swift toolchain from swift.org"},
    "kotlin-lsp": {"bin": ("kotlin-lsp",), "args": ["--stdio"], "wait": 600, "startup": 600, "settle": 5, "open": True,
                   "install": "brew install JetBrains/utils/kotlin-lsp  (JetBrains' Kotlin LSP)"},
    "kotlin-language-server": {"bin": ("kotlin-language-server",), "args": [], "wait": 300, "startup": 300,
                               "settle": 3, "open": True, "install": "brew install kotlin-language-server"},
    "typescript-language-server": {"bin": ("typescript-language-server",), "args": ["--stdio"], "open": True,
                                   "startup": 90, "install": "npm install -g typescript typescript-language-server"},
    "vtsls": {"bin": ("vtsls",), "args": ["--stdio"], "open": True, "startup": 90,
              "install": "npm install -g @vtsls/language-server"},
    "gopls": {"bin": ("gopls",), "args": [], "wait": 120, "startup": 180,
              "install": "go install golang.org/x/tools/gopls@latest"},
    "rust-analyzer": {"bin": ("rust-analyzer",), "args": [], "ready": "rust", "wait": 300, "startup": 600,
                      "install": "rustup component add rust-analyzer"},
    "clangd": {"bin": ("clangd",), "args": ["--background-index"], "xcrun": True, "wait": 3, "startup": 120, "open": True,
               "install": "clangd from LLVM (macOS: Xcode's command line tools; Linux: apt install clangd)"},
    "jdtls": {"bin": ("jdtls",), "args": [], "wait": 300, "startup": 600, "settle": 3, "open": True,
              "install": "brew install jdtls"},
    "ruby-lsp": {"bin": ("ruby-lsp",), "args": [], "open": True, "install": "gem install ruby-lsp"},
    "solargraph": {"bin": ("solargraph",), "args": ["stdio"], "open": True, "install": "gem install solargraph"},
    "intelephense": {"bin": ("intelephense",), "args": ["--stdio"], "open": True,
                     "install": "npm install -g intelephense"},
    "phpactor": {"bin": ("phpactor",), "args": ["language-server"], "open": True,
                 "install": "phpactor (phpactor.readthedocs.io)"},
    "csharp-ls": {"bin": ("csharp-ls",), "args": [], "open": True, "startup": 300,
                  "install": "dotnet tool install --global csharp-ls"},
    "dart": {"bin": ("dart",), "args": ["language-server", "--protocol=lsp"], "open": True,
             "install": "the Dart or Flutter SDK"},
    "lua-language-server": {"bin": ("lua-language-server",), "args": [], "open": True,
                            "install": "brew install lua-language-server"},
}

# Project roots: (markers whose OUTERMOST folder wins — the whole build or config, markers whose NEAREST folder
# wins). Above them all: the git repository. "*.ext" = any file with that extension.
ROOTS: Dict[str, Tuple[tuple, tuple]] = {
    "python": (("pyrightconfig.json",), ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")),
    "swift": (("buildServer.json",), ("Package.swift", "compile_commands.json")),
    "kotlin": (("settings.gradle.kts", "settings.gradle"), ("build.gradle.kts", "build.gradle", "pom.xml")),
    "typescript": ((), ("tsconfig.json", "jsconfig.json", "package.json")),
    "go": (("go.work",), ("go.mod",)),
    "rust": ((), ("Cargo.toml",)),
    "c": ((), ("compile_commands.json", "compile_flags.txt", ".clangd")),
    "java": (("settings.gradle.kts", "settings.gradle"), ("pom.xml", "build.gradle.kts", "build.gradle")),
    "ruby": ((), ("Gemfile",)),
    "php": ((), ("composer.json",)),
    "csharp": ((), ("*.sln", "*.csproj")),
    "dart": ((), ("pubspec.yaml",)),
    "lua": ((), (".luarc.json",)),
}

KINDS = {1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property", 8: "field",
         9: "constructor", 10: "enum", 11: "interface", 12: "function", 13: "variable", 14: "constant", 15: "string",
         16: "number", 17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null", 22: "enum member",
         23: "struct", 24: "event", 25: "operator", 26: "type parameter"}
# Symbols a reference can be "in": types and callables (and properties: SwiftUI's `var body`, Kotlin getters).
CONTAINER_KINDS = {2, 3, 5, 6, 7, 9, 10, 11, 12, 19, 23}
CALLABLE_KINDS = {6, 9, 12}
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
PYRIGHT_READY = re.compile(r"^(Found \d+ source files?|No source files found)", re.M)
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
DECL_WORDS = (r"def|class|func|fun|function|fn|struct|enum|protocol|interface|trait|type|typealias|object|extension|"
              r"record|module|actor|const|let|var|val|macro|union|namespace")
KEYWORDS = set(DECL_WORDS.split("|")) | {"async", "await", "return", "if", "else", "for", "while", "in", "of", "self",
                                         "this", "public", "private", "internal", "static", "override", "open",
                                         "final", "suspend", "export", "import", "from", "new", "try", "case", "true",
                                         "false", "none", "nil", "null", "None", "True", "False", "elif", "with",
                                         "lambda", "pass", "raise", "data", "sealed", "abstract", "inline", "init"}


class UserError(Exception):
    """A problem with the request: printed as is, with an exit code (2 usage/no server, 3 ambiguous, 4 not found)."""

    def __init__(self, text: str, code: int = 2):
        super().__init__(text)
        self.code = code


class LspError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- paths & positions

def real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def under(path: str, folder: str) -> bool:
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def path_uri(path: str) -> str:
    return Path(path).as_uri()


def uri_path(uri: str) -> str:
    if not uri.startswith("file:"):
        return uri
    parts = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parts.path)
    if WINDOWS and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    if parts.netloc and parts.netloc != "localhost":
        path = "//" + parts.netloc + path
    return os.path.realpath(os.path.normpath(path))


def split_lines(text: str) -> List[str]:
    return re.split(r"\r\n|\r|\n", text)


def lsp_col(line: str, index: int) -> int:
    """A code-point index in `line` → LSP's UTF-16 column."""
    return len(line[:index].encode("utf-16-le")) // 2


def py_col(line: str, units: int) -> int:
    """LSP's UTF-16 column → a code-point index in `line`."""
    n = 0
    for i, ch in enumerate(line):
        if n >= units:
            return i
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def read_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(8 * 1024 * 1024)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def stamp(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def word_re(name: str) -> "re.Pattern":
    return re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")


def short_name(name: str) -> str:
    """`Class.method`, `Class/method`, `Type::method`, `Class#method`, `method(x:)` → `method`."""
    name = re.split(r"[(<\s]", name.strip(), 1)[0]
    return re.split(r"\.|/|::|#", name)[-1] if name else name


def me() -> str:
    """How to call this script, for the commands printed to the agent."""
    return f'{compat.python_cmd()} "{os.path.abspath(__file__)}"'


# ----------------------------------------------------------------------------------------------- servers & roots

def user_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lang_of(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    for lang, info in LANGS.items():
        if ext in info["ext"]:
            return lang
    return None


def language_id(path: str, lang: str) -> str:
    return LANGUAGE_IDS.get(os.path.splitext(path)[1].lower(), lang)


def extra_bins() -> List[str]:
    dirs = [os.path.join(HOME, p) for p in (".local/bin", ".cargo/bin", "go/bin", ".bun/bin", ".deno/bin",
                                            ".npm-global/bin", "Library/pnpm", ".volta/bin", ".dotnet/tools",
                                            ".pub-cache/bin", ".local/share/nvim/mason/bin", ".ghcup/bin")]
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin"]
    if WINDOWS:
        appdata = os.environ.get("APPDATA") or ""
        dirs += [os.path.join(appdata, "npm"), os.path.join(HOME, "scoop", "shims")]
    return [d for d in dirs if os.path.isdir(d)]


def which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_bins():
        found = shutil.which(name, path=d)
        if found:
            return found
    if name == "pyrefly":  # bundled with the Pyrefly extension of Antigravity's editor days
        hits = sorted(glob.glob(os.path.join(HOME, ".antigravity", "extensions", "meta.pyrefly-*", "bin", "pyrefly")))
        if hits:
            return hits[-1]
    return None


_FOUND: Dict[str, Optional[List[str]]] = {}
_DEV_TOOLS: List[bool] = []


def dev_tools() -> bool:
    """macOS: are Xcode or its command line tools installed? Without them /usr/bin/sourcekit-lsp and clangd are
    stubs that open an install dialog instead of running."""
    if not _DEV_TOOLS:
        try:
            ok = subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        _DEV_TOOLS.append(ok)
    return _DEV_TOOLS[0]


def server_command(sid: str) -> Optional[List[str]]:
    """The command line that starts server `sid` here, or None when it isn't installed."""
    if sid in _FOUND:
        return _FOUND[sid]
    spec = SERVERS.get(sid) or {}
    cmd = None
    if spec.get("command"):
        cmd = list(spec["command"])
    else:
        for name in spec.get("bin") or ():
            exe = which(name)
            if not exe and spec.get("xcrun") and sys.platform == "darwin" and shutil.which("xcrun"):
                exe = compat.run(["xcrun", "--find", name], timeout=10).strip() or None
                exe = exe if exe and os.path.isfile(exe) else None
            if exe and spec.get("xcrun") and sys.platform == "darwin" and exe.startswith("/usr/bin/") and not dev_tools():
                exe = None
            if exe:
                cmd = [exe] + list(spec.get("args") or [])
                break
    _FOUND[sid] = cmd
    return cmd


def apply_config() -> None:
    """~/.gemini/antigravity-plus/lsp.json: a preferred server per language, or a command of your own."""
    for lang, value in user_config().items():
        if lang not in LANGS:
            continue
        if isinstance(value, str) and value in SERVERS:
            rest = [s for s in LANGS[lang]["servers"] if s != value]
            LANGS[lang] = dict(LANGS[lang], servers=tuple([value] + rest))
        elif isinstance(value, dict) and isinstance(value.get("command"), list) and value["command"]:
            sid = str(value.get("name") or f"{lang}-custom")
            SERVERS[sid] = {"command": [str(x) for x in value["command"]], "ready": value.get("ready"),
                            "startup": float(value.get("startup") or 120), "open": bool(value.get("open", True)),
                            "wait": float(value.get("wait") or 30),
                            "settle": float(value.get("settle") or 1), "install": "your lsp.json"}
            LANGS[lang] = dict(LANGS[lang], servers=(sid,) + tuple(s for s in LANGS[lang]["servers"] if s != sid))


def pick_server(lang: str) -> Optional[str]:
    for sid in LANGS.get(lang, {}).get("servers", ()):
        if server_command(sid):
            return sid
    return None


def install_hint(lang: str) -> str:
    first = LANGS[lang]["servers"][0]
    return SERVERS[first].get("install", "")


def git_top(path: str) -> Optional[str]:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def has_marker(folder: str, marker: str) -> bool:
    if marker.startswith("*."):
        try:
            return any(n.endswith(marker[1:]) for n in os.listdir(folder))
        except OSError:
            return False
    return os.path.exists(os.path.join(folder, marker))


def top_marker(folder: str, lang: str) -> bool:
    if any(has_marker(folder, m) for m in ROOTS.get(lang, ((), ()))[0]):
        return True
    if lang == "python":  # [tool.pyright] in pyproject.toml configures the whole project like pyrightconfig.json
        return re.search(r"^\[tool\.(based)?pyright\]", read_text(os.path.join(folder, "pyproject.toml")), re.M) is not None
    if lang == "rust":
        return re.search(r"^\[workspace\]", read_text(os.path.join(folder, "Cargo.toml")), re.M) is not None
    return False


def find_root(path: str, lang: str, boundary: Optional[str] = None) -> Tuple[str, str]:
    """The folder the server should open for this file, and why (the marker that decided it)."""
    path = os.path.normpath(path)  # «C:\\proj\\ios/Pkg» → one kind of separator, so roots compare and print alike
    start = path if os.path.isdir(path) else os.path.dirname(path)
    limit = os.path.normpath(boundary) if boundary else (git_top(start) or start)
    near_markers = ROOTS.get(lang, ((), ()))[1]
    outer, nearest, why_near = None, None, ""
    d = start
    while under(d, limit):
        if top_marker(d, lang):
            outer = d
        if nearest is None:
            hit = next((m for m in near_markers if has_marker(d, m)), None)
            if hit:
                nearest, why_near = d, hit
        if d == limit:
            break
        d = os.path.dirname(d)
    if outer:
        found = next((m for m in ROOTS.get(lang, ((), ()))[0] if has_marker(outer, m)), "")
        return outer, found or ("pyproject.toml [tool.pyright]" if lang == "python" else "Cargo.toml [workspace]")
    if nearest:
        return nearest, why_near
    return limit, "the git repository" if git_top(start) == limit else "the folder"


def find_root_below(folder: str, lang: str, depth: int = 3) -> Optional[Tuple[str, str]]:
    """When a command names no file: the shallowest folder under `folder` that holds the language's project file."""
    tops, nears = ROOTS.get(lang, ((), ()))
    level = [os.path.normpath(folder)]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                sub_dir = os.path.join(d, n)
                if n in SKIP_DIRS or n.startswith(".") or not os.path.isdir(sub_dir):
                    continue
                hit = next((m for m in tops + nears if has_marker(sub_dir, m)), None)
                if hit:
                    return sub_dir, hit
                nxt.append(sub_dir)
        level = nxt
    return None


def python_env(root: str, path: str) -> Optional[str]:
    """The project's virtualenv interpreter (nearest to the file, else in the root or one folder below it)."""
    exe = os.path.join("Scripts", "python.exe") if WINDOWS else os.path.join("bin", "python")
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while under(d, root):
        for name in (".venv", "venv", "env"):
            cand = os.path.join(d, name, exe)
            if os.path.isfile(cand) and os.path.isfile(os.path.join(d, name, "pyvenv.cfg")):
                return cand
        if d == root:
            break
        d = os.path.dirname(d)
    for cand in sorted(glob.glob(os.path.join(root, "*", ".venv", exe))):
        return cand
    return None


def server_settings(sid: str, root: str, path: str) -> dict:
    if sid in ("pyright", "basedpyright"):
        python: dict = {"analysis": {"autoSearchPaths": True, "useLibraryCodeForTypes": True,
                                     "diagnosticMode": "openFilesOnly"}}
        env = python_env(root, path)
        if env:
            python["pythonPath"] = env
        return {"python": python, "basedpyright": {"analysis": python["analysis"]}}
    return {}


# ----------------------------------------------------------------------------------------------- the project's files

def project_files(root: str, exts: Optional[tuple] = None, limit: int = SCAN_LIMIT) -> List[str]:
    """Files under `root` (git's view when it is a repository: tracked plus new, ignored ones left out)."""
    out: List[str] = []
    if git_top(root):
        try:
            r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, timeout=10, **HIDE)
            if r.returncode == 0:
                for rel in r.stdout.decode("utf-8", errors="replace").split("\0"):
                    if rel and (exts is None or rel.lower().endswith(exts)):
                        out.append(os.path.join(root, rel))
                        if len(out) >= limit:
                            break
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for name in files:
            if exts is None or name.lower().endswith(exts):
                out.append(os.path.join(d, name))
                if len(out) >= limit:
                    return out
    return out


def languages_in(folder: str) -> List[Tuple[str, int]]:
    counts: Dict[str, int] = {}
    for p in project_files(folder):
        lang = lang_of(p)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


# ----------------------------------------------------------------------------------------------- the LSP session

def read_message(stream) -> Optional[dict]:
    """One LSP frame from a binary stream; None when the stream closed."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                break
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                length = None
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        msg = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


class Session:
    """A language server running over stdio, with the documents we opened and what it told us."""

    def __init__(self, sid: str, lang: str, root: str, log=None, first: str = ""):
        self.sid, self.lang, self.root = sid, lang, root
        self.spec = SERVERS.get(sid) or {}
        self.cmd = server_command(sid) or []
        if sid == "jdtls":
            self.cmd = self.cmd + ["-data", os.path.join(STATE, "jdtls", hashlib.sha1(root.encode()).hexdigest()[:12])]
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.cond = threading.Condition()
        self.next_id = 0
        self.responses: Dict[int, dict] = {}
        self.diags: Dict[str, dict] = {}
        self.progress: Dict[str, str] = {}
        self.saw_progress = False
        self.changed_at = 0.0
        self.init_at = 0.0
        self.logs: List[str] = []
        self.pyright_ready = False
        self.quiescent = False
        self.caps: dict = {}
        self.docs: Dict[str, dict] = {}
        self.snapshot: Optional[Dict[str, tuple]] = None
        self.settings = server_settings(sid, root, first or root)
        self.alive = False
        self.ready_once = False
        self.waited = False
        self.outlines: Dict = {}
        self.outq: "queue.Queue[Optional[bytes]]" = queue.Queue()

    # -- process
    def note(self, text: str) -> None:
        if self.log:
            try:
                self.log.write(f"{time.strftime('%H:%M:%S')} lsp.py: {text}\n")
                self.log.flush()
            except (OSError, ValueError):
                pass

    def start(self) -> None:
        if not self.cmd:
            raise LspError(f"{self.sid} isn't installed")
        kw: dict = {}
        if WINDOWS:
            kw["creationflags"] = 0x08000000 | 0x00000200  # no console window, its own process group
        else:
            kw["start_new_session"] = True  # its own process group: stop() takes helpers along (kotlin-lsp's java)
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self.log or subprocess.DEVNULL, **kw)
        except OSError as e:
            raise LspError(f"can't start {self.cmd[0]}: {e}")
        self.alive = True
        self.note(f"started {' '.join(self.cmd)} in {self.root} (pid {self.proc.pid})")
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()
        root_uri = path_uri(self.root)
        params = {
            "processId": os.getpid(), "clientInfo": {"name": "antigravity-plus-lsp", "version": "1"},
            "rootUri": root_uri, "rootPath": self.root,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root) or self.root}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "workspace": {"workspaceFolders": True, "configuration": True, "applyEdit": False,
                              "didChangeConfiguration": {"dynamicRegistration": True},
                              "didChangeWatchedFiles": {"dynamicRegistration": True},
                              "symbol": {"symbolKind": {"valueSet": list(range(1, 27))}},
                              "workspaceEdit": {"documentChanges": True}},
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "definition": {"linkSupport": True}, "declaration": {"linkSupport": True},
                    "typeDefinition": {"linkSupport": True}, "implementation": {"linkSupport": True},
                    "references": {}, "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True,
                                       "symbolKind": {"valueSet": list(range(1, 27))}},
                    "callHierarchy": {}, "rename": {"prepareSupport": True},
                    "publishDiagnostics": {"versionSupport": True}, "diagnostic": {}},
                "window": {"workDoneProgress": True},
                "experimental": {"serverStatusNotification": True}},
        }
        init = self.request("initialize", params, timeout=max(30.0, float(self.spec.get("startup") or 60)))
        self.caps = (init or {}).get("capabilities") or {}
        self.notify("initialized", {})
        self.init_at = time.time()
        if self.settings:
            self.notify("workspace/didChangeConfiguration", {"settings": self.settings})
        self.snapshot = self.scan()  # files changed while it starts are sent with the first command

    def stop(self) -> None:
        if not self.proc:
            return
        if self.alive:
            try:
                self.request("shutdown", None, timeout=4)
                self.notify("exit", None)
            except (LspError, OSError):
                pass
        self.outq.put(None)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        kill_tree(self.proc.pid)
        self.alive = False
        self.note("stopped")

    def _reader(self) -> None:
        try:
            while True:
                msg = read_message(self.proc.stdout)
                if msg is None:
                    break
                self._dispatch(msg)
        except Exception as e:  # noqa: BLE001 — a broken server must not take the daemon down
            self.note(f"reader: {e!r}")
        finally:
            with self.cond:
                self.alive = False
                self.cond.notify_all()

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if method is None:
            if "id" in msg:
                with self.cond:
                    self.responses[msg["id"]] = msg
                    self.cond.notify_all()
            return
        params = msg.get("params")
        if "id" in msg:
            self._answer(msg["id"], method, params)
            return
        with self.cond:
            if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
                path = uri_path(str(params.get("uri") or ""))
                version, old = params.get("version"), self.diags.get(path) or {}
                # a late answer for an older version of the text must not replace the newer one's
                if not (isinstance(version, int) and isinstance(old.get("version"), int) and version < old["version"]):
                    self.diags[path] = {"items": params.get("diagnostics") or [], "version": version, "at": time.time()}
            elif method == "$/progress" and isinstance(params, dict):
                value = params.get("value") if isinstance(params.get("value"), dict) else {}
                token = str(params.get("token"))
                if value.get("kind") == "begin":
                    self.progress[token] = str(value.get("title") or value.get("message") or "")
                    self.saw_progress = True
                elif value.get("kind") == "end":
                    self.progress.pop(token, None)
                elif value.get("kind") == "report" and value.get("message") and token in self.progress:
                    self.progress[token] = f"{self.progress[token].split(' — ')[0]} — {value['message']}"[:120]
                self.changed_at = time.time()
            elif method in ("window/logMessage", "window/showMessage") and isinstance(params, dict):
                text = str(params.get("message") or "")
                self.logs.append(text[:400])
                del self.logs[:-100]
                if PYRIGHT_READY.search(text):
                    self.pyright_ready = True
            elif method == "experimental/serverStatus" and isinstance(params, dict):
                self.quiescent = bool(params.get("quiescent"))
                self.changed_at = time.time()
            self.cond.notify_all()

    def _answer(self, rid, method: str, params) -> None:
        result, error = None, None
        if method == "workspace/configuration":
            result = [self._config(item.get("section") if isinstance(item, dict) else None)
                      for item in ((params or {}).get("items") or [])]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": path_uri(self.root), "name": os.path.basename(self.root) or self.root}]
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "this client applies edits itself"}
        elif method == "window/showDocument":
            result = {"success": False}
        elif method in ("client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create",
                        "window/showMessageRequest") or method.endswith("/refresh"):
            result = None
        else:
            error = {"code": -32601, "message": f"not supported: {method}"}
        reply: dict = {"jsonrpc": "2.0", "id": rid}
        if error:
            reply["error"] = error
        else:
            reply["result"] = result
        try:
            self._send(reply)
        except LspError:
            pass

    def _config(self, section: Optional[str]):
        node = self.settings
        for part in (section or "").split(".") if section else []:
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def _send(self, msg: dict) -> None:
        """Queue a message for the writer thread. Never blocks: the reader answers the server's own requests through
        here, and a reader stuck behind a big write while the server waits for us to read would hang both."""
        if not self.proc or self.proc.poll() is not None:
            raise LspError(f"{self.sid} isn't running" + self.last_words())
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self.outq.put(b"Content-Length: %d\r\n\r\n" % len(data) + data)

    def _writer(self) -> None:
        while True:
            data = self.outq.get()
            if data is None:
                return
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError, AttributeError) as e:
                self.note(f"writer: {e!r}")
                with self.cond:
                    self.alive = False
                    self.cond.notify_all()
                return

    def notify(self, method: str, params) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params, timeout: float = 60.0):
        with self.cond:
            self.next_id += 1
            rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        with self.cond:
            while rid not in self.responses:
                left = deadline - time.time()
                if not self.alive:
                    raise LspError(f"{self.sid} stopped while answering {method}" + self.last_words())
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            msg = self.responses.pop(rid, None)
        if msg is None:
            try:
                self.notify("$/cancelRequest", {"id": rid})
            except LspError:
                pass
            raise LspError(f"{self.sid} didn't answer {method} within {int(timeout)} s")
        if "error" in msg:
            err = msg.get("error") or {}
            raise LspError(f"{self.sid}: {err.get('message') or err}")
        return msg.get("result")

    def last_words(self) -> str:
        return f" (last log: {self.logs[-1][:200]})" if self.logs else ""

    # -- readiness
    def _ready(self) -> bool:
        kind = self.spec.get("ready")
        if kind == "pyright":
            return self.pyright_ready
        if kind == "rust":
            return self.quiescent
        if self.progress:
            return False
        settle = float(self.spec.get("settle") or 1.0)
        quiet = time.time() - max(self.init_at, self.changed_at)
        return quiet >= (min(settle, 0.5) if self.saw_progress else settle)

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if self._ready():
                    self.ready_once = True
                    return True
                left = deadline - time.time()
                if left <= 0 or not self.alive:
                    return False
                self.cond.wait(min(left, 0.25))

    def busy_now(self) -> bool:
        """Answers may be incomplete: not ready yet, or indexing right now."""
        with self.cond:
            return not self.ready_once or bool(self.progress) or (
                self.spec.get("ready") == "pyright" and not self.pyright_ready)

    def busy(self) -> str:
        """What the server is still doing (its progress titles), for a note under partial results."""
        with self.cond:
            if self.spec.get("ready") == "pyright" and not self.pyright_ready:
                return "listing the project's files"
            return "; ".join(t for t in self.progress.values() if t)[:200] or "starting"

    # -- documents
    def open(self, path: str, text: Optional[str] = None) -> str:
        """Make the server see `path` as it is on disk now (or with `text`); returns its URI."""
        uri = path_uri(path)
        disk = text is None
        if disk:
            text = read_text(path)
        now = stamp(path) if disk else None  # another text than the disk's: the next sync() puts the disk's back
        doc = self.docs.get(path)
        if doc is None:
            self.docs[path] = {"version": 1, "text": text, "stamp": now, "used": time.time(), "sent": time.time()}
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id(path, self.lang),
                                                                  "version": 1, "text": text}})
            self._trim()
        elif doc["text"] != text:
            doc.update(version=doc["version"] + 1, text=text, stamp=now, sent=time.time())
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": doc["version"]},
                                                   "contentChanges": [{"text": text}]})
        else:
            doc["stamp"] = now
        self.docs[path]["used"] = time.time()
        return uri

    def close(self, path: str) -> None:
        if self.docs.pop(path, None) is not None:
            self.notify("textDocument/didClose", {"textDocument": {"uri": path_uri(path)}})

    def _trim(self, keep: int = 40) -> None:
        if len(self.docs) > keep:
            for path, _doc in sorted(self.docs.items(), key=lambda kv: kv[1]["used"])[:len(self.docs) - keep]:
                self.close(path)

    def sync(self) -> None:
        """Before a command: open documents follow the disk, and the server hears which files changed since."""
        for path, doc in list(self.docs.items()):
            now = stamp(path)
            if now is None:
                self.close(path)
            elif now != doc["stamp"]:
                self.open(path)
        current = self.scan()
        if self.snapshot is not None:
            changes = []
            for p, s in current.items():
                old = self.snapshot.get(p)
                if old is None:
                    changes.append({"uri": path_uri(p), "type": 1})
                elif old != s and p not in self.docs:
                    changes.append({"uri": path_uri(p), "type": 2})
            changes += [{"uri": path_uri(p), "type": 3} for p in self.snapshot if p not in current]
            if changes:
                self.notify("workspace/didChangeWatchedFiles", {"changes": changes[:5000]})
                self.outlines.clear()
        self.snapshot = current

    def scan(self) -> Dict[str, tuple]:
        """The language's files and project files under the root, with their stamps (for didChangeWatchedFiles)."""
        names = tuple(m for m in ROOTS.get(self.lang, ((), ()))[0] + ROOTS.get(self.lang, ((), ()))[1]
                      if not m.startswith("*"))
        current = {}
        for p in project_files(self.root, tuple(LANGS[self.lang]["ext"]) + names):
            st = stamp(p)
            if st:
                current[os.path.realpath(p)] = st
        return current

    # -- diagnostics
    def diagnostics(self, path: str, text: Optional[str] = None, timeout: float = 15.0) -> Optional[List[dict]]:
        """The server's problems for `path` as it is on disk now (or as `text`) — published AFTER that text was sent
        (and for its version, when the server says) — or None when it didn't say within `timeout`. Never an older
        answer passed off as this one."""
        self.open(path, text)
        doc = self.docs[path]
        if self.caps.get("diagnosticProvider"):
            try:
                rep = self.request("textDocument/diagnostic", {"textDocument": {"uri": path_uri(path)}}, timeout=timeout)
            except LspError:
                rep = None
            if isinstance(rep, dict) and rep.get("kind") == "full":
                return rep.get("items") or []
        deadline = time.time() + timeout
        with self.cond:
            while True:
                got = self.diags.get(path)
                if got and got["at"] >= doc["sent"] and (got.get("version") is None or got["version"] >= doc["version"]):
                    break
                left = deadline - time.time()
                if left <= 0 or not self.alive:
                    return None
                self.cond.wait(min(left, 0.2))
            settle_until = min(got["at"] + 0.3, deadline)  # a second publish often follows (syntax, then types)
            while time.time() < settle_until:
                self.cond.wait(max(0.01, settle_until - time.time()))
            return self.diags[path]["items"]


def kill_tree(pid: int) -> None:
    """Stop a server with everything it started (its own process group; on Windows the whole tree)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **HIDE)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (OSError, PermissionError):
            return
        for _ in range(20):
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.1)


# ----------------------------------------------------------------------------------------------- answers → text

def norm_locations(result) -> List[Tuple[str, int, int]]:
    """Location | Location[] | LocationLink[] → [(path, line0, utf16 col)]."""
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        uri = it.get("targetUri") or it.get("uri")
        rng = it.get("targetSelectionRange") or it.get("targetRange") or it.get("range")
        if not uri or not isinstance(rng, dict):
            continue
        start = rng.get("start") or {}
        out.append((uri_path(uri), int(start.get("line", 0)), int(start.get("character", 0))))
    seen, unique = set(), []
    for loc in out:
        if loc not in seen:
            seen.add(loc)
            unique.append(loc)
    return unique


def hover_text(result) -> str:
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    parts: List[str] = []
    for c in contents if isinstance(contents, list) else [contents]:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict) and isinstance(c.get("value"), str):
            parts.append(c["value"])
    text = "\n".join(parts)
    text = re.sub(r"^```[\w+-]*\s*$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:2500] + ("\n…" if len(text) > 2500 else "")


class Ctx:
    """One command against a running session: file texts, outlines and how paths are shown."""

    def __init__(self, sess: Session, args, cwd: str):
        self.sess, self.args, self.cwd = sess, args, cwd
        self.root = sess.root
        self.base = cwd if under(self.root, cwd) or not under(cwd, self.root) else self.root
        self.texts: Dict[str, List[str]] = {}
        self.notes: List[str] = []

    def lines(self, path: str) -> List[str]:
        if path not in self.texts:
            self.texts[path] = split_lines(read_text(path))
        return self.texts[path]

    def show(self, path: str) -> str:
        if under(path, self.base):
            return os.path.relpath(path, self.base)
        if under(path, HOME):
            return "~" + os.sep + os.path.relpath(path, HOME)
        return path

    def code(self, path: str, line0: int, ch16: int = 0) -> str:
        lines = self.lines(path)
        if not 0 <= line0 < len(lines):
            return ""
        text = lines[line0].rstrip()
        start = len(text) - len(text.lstrip())
        text = text.strip()
        if len(text) > TEXT_WIDTH:
            at = max(0, py_col(lines[line0], ch16) - start - TEXT_WIDTH // 3)
            text = ("…" if at else "") + text[at:at + TEXT_WIDTH] + "…"
        return text

    def col1(self, path: str, line0: int, ch16: int) -> int:
        lines = self.lines(path)
        return py_col(lines[line0], ch16) + 1 if 0 <= line0 < len(lines) else ch16 + 1

    def outline(self, path: str) -> List[dict]:
        """The file's symbols, flattened: name, qualified name, kind, range lines, name position, depth."""
        st = stamp(path)
        cached = self.sess.outlines.get(path)
        if cached and cached[0] == st:
            return cached[1]
        if self.sess.spec.get("open"):
            self.sess.open(path)
        try:
            res = self.sess.request("textDocument/documentSymbol", {"textDocument": {"uri": path_uri(path)}}, timeout=30)
        except LspError as e:
            self.notes.append(f"no outline for {self.show(path)}: {e}")
            res = None
        flat: List[dict] = []

        def walk(items, parents: List[str], depth: int) -> None:
            for s in items or []:
                if not isinstance(s, dict):
                    continue
                kind = int(s.get("kind") or 0)
                name = str(s.get("name") or "")
                if "location" in s:  # SymbolInformation: flat, with containerName
                    loc = s.get("location") or {}
                    rng = loc.get("range") or {}
                    sel = rng
                    chain = [s["containerName"]] if s.get("containerName") else []
                else:
                    rng = s.get("range") or {}
                    sel = s.get("selectionRange") or rng
                    chain = parents
                entry = {"name": name, "kind": kind, "detail": str(s.get("detail") or ""), "depth": depth,
                         "qual": ".".join(chain + [short_name(name) or name]),
                         "start": int((rng.get("start") or {}).get("line", 0)),
                         "end": int((rng.get("end") or {}).get("line", 0)),
                         "line": int((sel.get("start") or {}).get("line", 0)),
                         "char": int((sel.get("start") or {}).get("character", 0))}
                flat.append(entry)
                keep = kind in CONTAINER_KINDS
                walk(s.get("children"), parents + [short_name(name) or name] if keep else parents, depth + 1)

        walk(res if isinstance(res, list) else [], [], 0)
        if flat and "location" in (res[0] if isinstance(res, list) and res else {}):
            # flat answers have no end lines worth trusting: a symbol runs until the next one at its level
            flat.sort(key=lambda e: e["start"])
            for i, e in enumerate(flat):
                if e["end"] <= e["start"]:
                    later = [x["start"] for x in flat[i + 1:] if x["start"] > e["start"]]
                    e["end"] = (later[0] - 1) if later else 10 ** 9
        self.sess.outlines[path] = (st, flat)
        return flat

    def container(self, path: str, line0: int) -> str:
        """The innermost type, function or property around a line ("" at the top level)."""
        around = [e for e in self.outline(path)
                  if (e["kind"] in CONTAINER_KINDS or (e["kind"] in (13, 14) and e["depth"] == 0))
                  and e["start"] <= line0 <= e["end"]]
        return max(around, key=lambda e: (e["start"], -e["end"]))["qual"] if around else ""


# ----------------------------------------------------------------------------------------------- finding the symbol

def parse_target(what: str, cwd: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """'<file>:<line>[:<col>]' or '<file>' → (path, line, col), 1-based; a bare name → (None, None, None)."""
    m = re.match(r"^(.*?):(\d+)(?::(\d+))?$", what)
    if m and m.group(1):
        path = real(os.path.join(cwd, m.group(1)))
        if os.path.isfile(path):
            return path, int(m.group(2)), int(m.group(3)) if m.group(3) else None
    path = real(os.path.join(cwd, what))
    if os.path.isfile(path):
        return path, None, None
    return None, None, None


def decl_re(name: str) -> "re.Pattern":
    n = re.escape(name)
    return re.compile(rf"(?:^|[\s(])(?:{DECL_WORDS})\s+(?:[\w.]+\.)?{n}(?![\w$])|^\s*func\s*\([^)]*\)\s*{n}\b")


def position_in_line(line: str, name: str, decl: bool = False) -> Optional[int]:
    """Code-point index of `name` as a whole word in `line` (after a declaration keyword when `decl`)."""
    if decl:
        m = decl_re(name).search(line)
        if m:
            hit = word_re(name).search(line, m.start())
            if hit:
                return hit.start()
    m = word_re(name).search(line)
    return m.start() if m else None


def guess_name(line: str, index: Optional[int] = None) -> Optional[Tuple[str, int]]:
    """The identifier at `index`, or the name a declaration line declares, or its first identifier."""
    words = [(m.group(0), m.start()) for m in IDENT.finditer(line)]
    if index is not None:
        for w, at in words:
            if at <= index <= at + len(w):
                return w, at
        return None
    m = re.search(rf"\b(?:{DECL_WORDS})\s+([A-Za-z_$][\w$]*)", line)
    if m:
        return m.group(1), m.start(1)
    for w, at in words:
        if w not in KEYWORDS:
            return w, at
    return None


def locate_in_file(ctx: Ctx, path: str, name: str) -> Tuple[str, int, int, str]:
    want = short_name(name)
    qual = name.replace("::", ".").replace("/", ".").replace("#", ".")
    hits = [e for e in ctx.outline(path) if short_name(e["name"]) == want]
    if len(hits) > 1 and "." in qual:
        hits = [e for e in hits if e["qual"].endswith(qual)] or hits
    if hits:
        e = hits[0]
        line = ctx.lines(path)[e["line"]] if e["line"] < len(ctx.lines(path)) else ""
        idx = position_in_line(line, want)
        ch = lsp_col(line, idx) if idx is not None else e["char"]
        if len(hits) > 1:
            ctx.notes.append(f"{len(hits)} symbols named {want} in this file — used the first (line {e['line'] + 1}); "
                             f"give <file>:<line> for another")
        return path, e["line"], ch, want
    lines = ctx.lines(path)
    for decl in (True, False):
        for i, line in enumerate(lines):
            idx = position_in_line(line, want, decl)
            if idx is not None and (not decl or decl_re(want).search(line)):
                return path, i, lsp_col(line, idx), want
    raise UserError(f"`{want}` doesn't occur in {ctx.show(path)}", 4)


def locate_symbol(ctx: Ctx, query: str, within: Optional[str] = None) -> Tuple[str, int, int, str]:
    """A symbol by name across the workspace (or declared under `within`): the server's symbol index, then
    declarations in the files."""
    want = short_name(query)
    parent = None
    parts = [p for p in re.split(r"\.|/|::|#", query.split("(")[0]) if p]
    if len(parts) > 1:
        parent = parts[-2]
    found: List[Tuple[str, int, str, str]] = []
    try:
        res = ctx.sess.request("workspace/symbol", {"query": want}, timeout=60) or []
    except LspError as e:
        ctx.notes.append(f"symbol index: {e}")
        res = []
    for s in res if isinstance(res, list) else []:
        if not isinstance(s, dict) or short_name(str(s.get("name") or "")) != want:
            continue
        loc = s.get("location") or {}
        path = uri_path(str(loc.get("uri") or ""))
        rng = loc.get("range") or {}
        line0 = int((rng.get("start") or {}).get("line", -1))
        container = str(s.get("containerName") or "")
        if parent and parent not in container.replace("::", "."):
            continue
        found.append((path, line0, KINDS.get(int(s.get("kind") or 0), "symbol"), container))
    inside = [f for f in found if under(f[0], within or ctx.root)]
    found = inside if within else (inside or found)
    strong = [f for f in found if f[2] not in ("variable", "constant", "field", "property", "enum member")]
    found = strong or found  # a function or a type, not the locals that share its name
    if not found:  # no index (or nothing in it): declarations in the files themselves
        rx = decl_re(want)
        for p in project_files(within or ctx.root, LANGS[ctx.sess.lang]["ext"], limit=20000):
            p = os.path.realpath(p)
            text = read_text(p)
            if want not in text:
                continue
            for i, line in enumerate(split_lines(text)):
                if rx.search(line):
                    ctx.texts.setdefault(p, split_lines(text))
                    found.append((p, i, "declaration", ""))
                    if len(found) > 30:
                        break
    found = sorted(set(found), key=lambda f: (f[0], f[1]))
    if not found:
        raise UserError(f"no symbol named `{want}` here (searched the {ctx.sess.sid} index and the declarations in "
                        f"{ctx.show(ctx.root) or '.'}) — check the spelling, or give <file>:<line> {want}", 4)
    if len(found) > 1:
        rows = [f"  {ctx.show(p)}:{line0 + 1}  {kind}  {(container + '.') if container else ''}{want}"
                for p, line0, kind, container in found[:12]]
        more = f"\n  … {len(found) - 12} more" if len(found) > 12 else ""
        raise UserError(f"{len(found)} symbols named `{want}` — say which one, as <file>:<line> {want}:\n"
                        + "\n".join(rows) + more, 3)
    path, line0, _kind, _container = found[0]
    lines = ctx.lines(path)
    for i in range(max(0, line0), min(len(lines), max(0, line0) + 6)):
        idx = position_in_line(lines[i], want, decl=True)
        if idx is not None:
            return path, i, lsp_col(lines[i], idx), want
    return locate_in_file(ctx, path, want)


def resolve(ctx: Ctx, what: str, name: Optional[str]) -> Tuple[str, int, int, str]:
    """(path, line0, utf16 col, name) of the symbol the agent means."""
    path, line, col = parse_target(what, ctx.cwd)
    folder = real(os.path.join(ctx.cwd, what))
    if path is None and name and os.path.isdir(folder):
        return locate_symbol(ctx, name, within=folder)
    if path is None:
        if name:
            raise UserError(f"no such file: {what}")
        bare = re.sub(r"(:\d+)+$", "", what)
        if re.search(r"[\\/]", bare) or os.path.splitext(bare)[1].lower() in ALL_EXTS or bare != what:
            raise UserError(f"no such file: {bare}")
        return locate_symbol(ctx, what)
    if line is None:
        if not name:
            raise UserError("say which symbol: <file>:<line> <name>, <file> <name>, or <file>:<line>:<col>")
        return locate_in_file(ctx, path, name)
    lines = ctx.lines(path)
    if not 1 <= line <= len(lines):
        raise UserError(f"{ctx.show(path)} has {len(lines)} lines — there is no line {line}", 4)
    text = lines[line - 1]
    if col:
        got = guess_name(text, col - 1)
        if not got:
            raise UserError(f"{ctx.show(path)}:{line}:{col} isn't on a name: `{text.strip()[:80]}`", 4)
        return path, line - 1, lsp_col(text, got[1]), got[0]
    if name:
        want = short_name(name)
        idx = position_in_line(text, want)
        if idx is not None:
            return path, line - 1, lsp_col(text, idx), want
        for delta in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
            i = line - 1 + delta
            if 0 <= i < len(lines):
                idx = position_in_line(lines[i], want)
                if idx is not None:
                    ctx.notes.append(f"`{want}` is on line {i + 1}, not {line} — used line {i + 1}")
                    return path, i, lsp_col(lines[i], idx), want
        raise UserError(f"`{want}` isn't on line {line} of {ctx.show(path)} (or near it): `{text.strip()[:100]}`", 4)
    got = guess_name(text)
    if not got:
        raise UserError(f"line {line} of {ctx.show(path)} has no name on it — add the symbol: {what} <name>", 4)
    return path, line - 1, lsp_col(text, got[1]), got[0]


def doc_pos(path: str, line0: int, ch: int) -> dict:
    return {"textDocument": {"uri": path_uri(path)}, "position": {"line": line0, "character": ch}}


# ----------------------------------------------------------------------------------------------- the commands

def header(ctx: Ctx, text: str) -> str:
    where = f"; paths from {ctx.base}" if os.path.normcase(ctx.base) != os.path.normcase(ctx.cwd) else ""
    return f"{text} ({ctx.sess.lang} · {ctx.sess.sid}{where})"


def cmd_locations(ctx: Ctx, kind: str, what: str, name: Optional[str]) -> Tuple[int, str]:
    method, cap, label = {"def": ("textDocument/definition", "definitionProvider", "definition"),
                          "type": ("textDocument/typeDefinition", "typeDefinitionProvider", "type definition"),
                          "impl": ("textDocument/implementation", "implementationProvider", "implementations")}[kind]
    path, line0, ch, sym = resolve(ctx, what, name)
    if not ctx.sess.caps.get(cap):
        hint = " — use `refs` (it lists every use, overrides included)" if kind == "impl" else ""
        return 1, f"{ctx.sess.sid} can't answer «{label}»{hint}"
    if ctx.sess.spec.get("open"):
        ctx.sess.open(path)
    locs = norm_locations(ctx.sess.request(method, doc_pos(path, line0, ch)))
    if not locs:
        return 0, header(ctx, f"{label} of {sym}: none found") + "\n" + "\n".join(ctx.notes)
    out = [header(ctx, f"{label} of {sym}" + (f": {len(locs)}" if len(locs) > 1 else ""))]
    for p, l0, c in locs[:60]:
        out.append(f"{ctx.show(p)}:{l0 + 1}:{ctx.col1(p, l0, c)}  {ctx.code(p, l0, c)}")
    return 0, "\n".join(out + ctx.notes)


def cmd_hover(ctx: Ctx, what: str, name: Optional[str]) -> Tuple[int, str]:
    path, line0, ch, sym = resolve(ctx, what, name)
    if ctx.sess.spec.get("open"):
        ctx.sess.open(path)
    text = hover_text(ctx.sess.request("textDocument/hover", doc_pos(path, line0, ch)))
    head = header(ctx, f"{sym} at {ctx.show(path)}:{line0 + 1}")
    return 0, "\n".join([head, text or "(the server has nothing to say about it)"] + ctx.notes)


def cmd_refs(ctx: Ctx, what: str, name: Optional[str]) -> Tuple[int, str]:
    path, line0, ch, sym = resolve(ctx, what, name)
    if ctx.sess.spec.get("open"):
        ctx.sess.open(path)
    pos = doc_pos(path, line0, ch)
    defs = set()
    try:
        defs = {(p, l0) for p, l0, _c in norm_locations(ctx.sess.request("textDocument/definition", pos, timeout=30))}
    except LspError:
        pass
    params = dict(pos, context={"includeDeclaration": not ctx.args.no_decl})
    locs = norm_locations(ctx.sess.request("textDocument/references", params, timeout=180))
    if ctx.args.path:
        scope = real(os.path.join(ctx.cwd, ctx.args.path))
        locs = [x for x in locs if under(x[0], scope)]
    if not locs:
        return 0, header(ctx, f"references to {sym}: none") + "\n" + "\n".join(ctx.notes)
    locs.sort(key=lambda x: (ctx.show(x[0]), x[1], x[2]))
    files = []
    for p, _l, _c in locs:
        if p not in files:
            files.append(p)
    limit = ctx.args.limit if ctx.args.limit is not None else SHOW_REFS
    out = [header(ctx, f"references to {sym}: {len(locs)} in {len(files)} file{'s' if len(files) != 1 else ''}")]
    shown = 0
    for f in files:
        if limit and shown >= limit:
            break
        out.append(ctx.show(f))
        for p, l0, c in locs:
            if p != f:
                continue
            if limit and shown >= limit:
                break
            mark = "  [definition]" if (p, l0) in defs else ""
            where = "" if mark else ctx.container(p, l0) if len(files) <= 60 else ""
            out.append(f"  {l0 + 1}:{ctx.col1(p, l0, c)}  {ctx.code(p, l0, c)}{mark}"
                       + (f"  — in {where}" if where else ""))
            shown += 1
    if limit and shown < len(locs):
        out.append(f"… {len(locs) - shown} more (--limit 0 shows all; --path <folder> narrows)")
    if ctx.sess.busy_now():
        out.append(f"(the server is still busy: {ctx.sess.busy()} — results may be incomplete; run it again shortly)")
    scope = real(os.path.join(ctx.cwd, ctx.args.path)) if ctx.args.path else None
    extra = text_mentions(ctx, sym, set(files), scope, called=is_callable(ctx, defs))
    if extra:
        out.append(extra)
    return 0, "\n".join(out + ctx.notes)


COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|--)")
STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def text_mentions(ctx: Ctx, name: str, linked: set, scope: Optional[str] = None, called: bool = False) -> str:
    """Files of this language with `name` in code (comments and strings left out; for a function, only `name(`)
    where the server linked nothing: another symbol with the same name — or code the server can't resolve (imports
    through sys.path, reflection, generated code, a stale index), which the agent has to check. One line, or ""."""
    rx = re.compile(word_re(name).pattern + (r"\s*\(" if called else ""))
    hits = []
    for p in project_files(scope or ctx.root, LANGS[ctx.sess.lang]["ext"], limit=20000):
        p = os.path.realpath(p)
        if p in linked:
            continue
        text = read_text(p)
        if name not in text:
            continue
        n = sum(len(rx.findall(STRING.sub('""', line))) for line in split_lines(text)
                if name in line and not COMMENT_LINE.match(line))
        if n:
            hits.append((ctx.show(p), n))
    if not hits:
        return ""
    hits.sort()
    listed = ", ".join(f"{p} ({n})" for p, n in hits[:6]) + (f", +{len(hits) - 6} more files" if len(hits) > 6 else "")
    what = f"`{name}(` calls" if called else f"`{name}` in code"
    return (f"Not linked by the server, but {what} also in: {listed} — another symbol with this name, or code the "
            f"server can't resolve (sys.path imports, reflection, a stale index). Check them with rg if they matter.")


def is_callable(ctx: Ctx, defs: set) -> bool:
    """Is the symbol (at one of its definitions) a function or a method?"""
    for p, l0 in defs:
        for e in ctx.outline(p) if os.path.isfile(p) else []:
            if e["line"] == l0:
                return e["kind"] in CALLABLE_KINDS
    return False


def call_name(item: dict) -> str:
    name, detail = str(item.get("name") or "?"), str(item.get("detail") or "")
    if detail and re.match(r"^[\w.$]+$", detail) and not name.startswith(detail):
        return f"{detail}.{name}"
    return name


def cmd_calls(ctx: Ctx, what: str, name: Optional[str]) -> Tuple[int, str]:
    path, line0, ch, sym = resolve(ctx, what, name)
    out_dir = bool(ctx.args.out)
    depth = max(1, min(int(ctx.args.depth or 1), 5))
    if ctx.sess.spec.get("open"):
        ctx.sess.open(path)
    if not ctx.sess.caps.get("callHierarchyProvider"):
        if out_dir:
            return 1, f"{ctx.sess.sid} has no call hierarchy: read the function's body instead (`symbols` shows where it is)"
        ctx.notes.append(f"({ctx.sess.sid} has no call hierarchy: callers worked out from the references)")
        ctx.args.no_decl = True
        return cmd_refs(ctx, what, name)
    items = ctx.sess.request("textDocument/prepareCallHierarchy", doc_pos(path, line0, ch)) or []
    if not items:
        return 4, f"{sym} at {ctx.show(path)}:{line0 + 1} isn't something that is called (a function or a method?)"
    root_item = items[0]
    label = "calls made by" if out_dir else "callers of"
    sel = (root_item.get("selectionRange") or root_item.get("range") or {}).get("start") or {}
    out = [header(ctx, f"{label} {call_name(root_item)} ({ctx.show(uri_path(root_item.get('uri', '')))}:"
                       f"{int(sel.get('line', 0)) + 1})" + (f", {depth} levels" if depth > 1 else ""))]
    seen = set()
    budget = [150]

    def walk(item: dict, level: int) -> None:
        if level > depth or budget[0] <= 0:
            return
        method = "callHierarchy/outgoingCalls" if out_dir else "callHierarchy/incomingCalls"
        try:
            calls = ctx.sess.request(method, {"item": item}, timeout=120) or []
        except LspError as e:
            out.append("  " * level + f"(stopped: {e})")
            return
        rows = []
        for c in calls:
            other = c.get("to" if out_dir else "from") or {}
            ranges = c.get("fromRanges") or []
            p = uri_path(str(other.get("uri") or ""))
            osel = ((other.get("selectionRange") or other.get("range") or {}).get("start") or {})
            if out_dir:
                at_lines = [int((r.get("start") or {}).get("line", 0)) + 1 for r in ranges]
                where = f"{ctx.show(p)}:{int(osel.get('line', 0)) + 1}"
                extra = (" (called at line " + ", ".join(str(x) for x in at_lines[:4]) + ")") if at_lines else ""
            else:
                first = (ranges[0].get("start") or {}) if ranges else osel
                where = f"{ctx.show(p)}:{int(first.get('line', 0)) + 1}"
                extra = f" (+{len(ranges) - 1} more calls there)" if len(ranges) > 1 else ""
            rows.append((ctx.show(p), int(osel.get("line", 0)), other, where, extra))
        rows.sort(key=lambda r: (r[0], r[1]))
        for _f, _l, other, where, extra in rows:
            if budget[0] <= 0:
                out.append("  " * level + "… (cut at 150 entries)")
                return
            budget[0] -= 1
            key = (other.get("uri"), json.dumps(other.get("selectionRange"), sort_keys=True))
            arrow = "→" if out_dir else "←"
            again = key in seen
            out.append("  " * level + f"{arrow} {call_name(other)}  {where}{extra}" + ("  (see above)" if again else ""))
            if not again:
                seen.add(key)
                walk(other, level + 1)

    walk(root_item, 1)
    if not out_dir:
        linked = {uri_path(str(root_item.get("uri") or ""))}
        for line in out[1:]:
            m = re.search(r"  (\S+?):\d+", line)
            if m:
                linked.add(real(os.path.join(ctx.base, m.group(1))))
        extra = text_mentions(ctx, sym, linked, called=True)
        if extra:
            ctx.notes.append(extra)
    if len(out) == 1:
        out.append("  none found" + ("" if out_dir else " — nothing in the project calls it (check `refs` for "
                                                         "uses that aren't calls)"))
    if ctx.sess.busy_now():
        out.append(f"(the server is still busy: {ctx.sess.busy()} — results may be incomplete)")
    return 0, "\n".join(out + ctx.notes)


def cmd_symbols(ctx: Ctx, files: List[str]) -> Tuple[int, str]:
    out: List[str] = []
    depth = ctx.args.depth if ctx.args.depth is not None else 2
    for f in files:
        path = real(os.path.join(ctx.cwd, f))
        if not os.path.isfile(path):
            out.append(f"{f}: no such file")
            continue
        rows = []
        inside_callable: List[Tuple[int, int]] = []
        for e in ctx.outline(path):
            if depth and e["depth"] >= depth:
                continue
            if not ctx.args.all and any(a <= e["start"] and e["end"] <= b and (a, b) != (e["start"], e["end"])
                                        for a, b in inside_callable):
                continue  # locals of a function
            if e["kind"] in CALLABLE_KINDS:
                inside_callable.append((e["start"], e["end"]))
            detail = f" {e['detail']}" if e["detail"] and len(e["detail"]) <= 80 and e["detail"] not in e["name"] else ""
            span = f"{e['start'] + 1}-{e['end'] + 1}" if e["end"] > e["start"] else f"{e['start'] + 1}"
            rows.append(f"{'  ' * e['depth']}{KINDS.get(e['kind'], 'symbol')} {e['name']}{detail}  {span}")
        out.append(header(ctx, f"{ctx.show(path)} — {len(rows)} symbols") if rows else f"{ctx.show(path)}: no symbols")
        out += rows[:400]
        if len(rows) > 400:
            out.append(f"… {len(rows) - 400} more")
    return 0, "\n".join(out + ctx.notes)


def cmd_find(ctx: Ctx, query: str) -> Tuple[int, str]:
    want = short_name(query) or query
    res = ctx.sess.request("workspace/symbol", {"query": want}, timeout=120) or []
    scope = real(os.path.join(ctx.cwd, ctx.args.path)) if ctx.args.path else None
    kind = (ctx.args.kind or "").lower()
    rows = []
    for s in res if isinstance(res, list) else []:
        if not isinstance(s, dict):
            continue
        loc = s.get("location") or {}
        p = uri_path(str(loc.get("uri") or ""))
        if scope and not under(p, scope):
            continue
        k = KINDS.get(int(s.get("kind") or 0), "symbol")
        if kind and kind not in k:
            continue
        n = str(s.get("name") or "")
        sn = short_name(n)
        rank = 0 if sn == want else 1 if sn.lower() == want.lower() else 2 if sn.lower().startswith(want.lower()) else 3
        line0 = int(((loc.get("range") or {}).get("start") or {}).get("line", -1))
        rows.append((rank, 0 if under(p, ctx.root) else 1, ctx.show(p), line0, k, n, str(s.get("containerName") or "")))
    rows.sort()
    if not rows:
        return 0, header(ctx, f"symbols matching {want}: none") + (
            f"\n(the server is still busy: {ctx.sess.busy()} — try again shortly)" if ctx.sess.busy_now() else "")
    out = [header(ctx, f"symbols matching {want}: {len(rows)}" + (", first 40" if len(rows) > 40 else ""))]
    for _r, _in, p, line0, k, n, cont in rows[:40]:
        out.append(f"{k} {(cont + '.') if cont else ''}{n}  {p}" + (f":{line0 + 1}" if line0 >= 0 else ""))
    return 0, "\n".join(out + ctx.notes)


def diag_rows(ctx: Ctx, path: str, items: List[dict], errors_only: bool) -> List[str]:
    rows = []
    for d in sorted(items, key=lambda d: (int(d.get("severity") or 1), ((d.get("range") or {}).get("start") or {}).get("line", 0))):
        sev = int(d.get("severity") or 1)
        if sev > (1 if errors_only else 2):
            continue
        start = (d.get("range") or {}).get("start") or {}
        l0, c = int(start.get("line", 0)), int(start.get("character", 0))
        code = d.get("code")
        if isinstance(code, dict):
            code = code.get("value")
        msg = re.sub(r"\s+", " ", str(d.get("message") or "")).strip()[:300]
        rows.append(f"  {l0 + 1}:{ctx.col1(path, l0, c)}  {SEVERITY.get(sev, 'error')}  {msg}"
                    + (f"  [{code}]" if code not in (None, "") else ""))
    return rows


def cmd_check(ctx: Ctx, files: List[str]) -> Tuple[int, str]:
    out = []
    total_e = total_w = 0
    for f in files[:30]:
        path = real(os.path.join(ctx.cwd, f))
        if not os.path.isfile(path):
            out.append(f"{f}: no such file")
            continue
        if lang_of(path) != ctx.sess.lang:
            out.append(f"{ctx.show(path)}: not {ctx.sess.lang} — check it separately")
            continue
        items = ctx.sess.diagnostics(path, timeout=float(ctx.args.timeout or 20))
        if items is None:
            out.append(f"{ctx.show(path)}: {ctx.sess.sid} gave no diagnostics in time ({ctx.sess.busy()})")
            continue
        errors = sum(1 for d in items if int(d.get("severity") or 1) == 1)
        warnings = sum(1 for d in items if int(d.get("severity") or 1) == 2)
        total_e += errors
        total_w += 0 if ctx.args.errors else warnings
        rows = diag_rows(ctx, path, items, ctx.args.errors)
        if not rows:
            out.append(f"{ctx.show(path)}: no problems")
            continue
        out.append(f"{ctx.show(path)}: {errors} error{'s' if errors != 1 else ''}"
                   + ("" if ctx.args.errors else f", {warnings} warning{'s' if warnings != 1 else ''}"))
        out += rows[:60] + ([f"  … {len(rows) - 60} more"] if len(rows) > 60 else [])
    head = header(ctx, f"diagnostics: {total_e} errors" + ("" if ctx.args.errors else f", {total_w} warnings"))
    return 0, "\n".join([head] + out + ctx.notes)


def workspace_edits(edit: dict) -> Tuple[Dict[str, List[dict]], List[str]]:
    """WorkspaceEdit → ({path: [TextEdit]}, [file operations we won't do])."""
    changes: Dict[str, List[dict]] = {}
    ops: List[str] = []
    for uri, edits in (edit.get("changes") or {}).items():
        changes.setdefault(uri_path(uri), []).extend(edits or [])
    for dc in edit.get("documentChanges") or []:
        if not isinstance(dc, dict):
            continue
        if "textDocument" in dc:
            changes.setdefault(uri_path(str((dc.get("textDocument") or {}).get("uri") or "")), []).extend(dc.get("edits") or [])
        elif dc.get("kind"):
            ops.append(f"{dc['kind']} {uri_path(str(dc.get('oldUri') or dc.get('uri') or ''))}"
                       + (f" → {uri_path(str(dc.get('newUri')))}" if dc.get("newUri") else ""))
    return changes, ops


def apply_edits(text: str, edits: List[dict]) -> str:
    lines = split_lines(text)
    starts, pos = [], 0
    for line in re.split(r"(?<=\n)|(?<=\r)(?!\n)", text):
        starts.append(pos)
        pos += len(line)
    starts.append(pos)

    def offset(p: dict) -> int:
        l0 = min(int(p.get("line", 0)), len(lines) - 1)
        return starts[l0] + py_col(lines[l0], int(p.get("character", 0)))

    spans = sorted(((offset(e["range"]["start"]), offset(e["range"]["end"]), e.get("newText", "")) for e in edits),
                   reverse=True)
    for i in range(len(spans) - 1):
        if spans[i + 1][1] > spans[i][0]:
            raise UserError("the server's edits overlap — not applied")
    for a, b, new in spans:
        text = text[:a] + new + text[b:]
    return text


def edits_match(text: str, edits: List[dict], old: str, new: str) -> bool:
    """Every edit that writes the new name replaces exactly the old one in `text` as it is on disk now."""
    lines = split_lines(text)
    for e in edits:
        rng = e.get("range") or {}
        a, b = rng.get("start") or {}, rng.get("end") or {}
        l0 = int(a.get("line", -1))
        if e.get("newText") != new or l0 != int(b.get("line", -2)) or not 0 <= l0 < len(lines):
            continue  # labels, imports and the like: the server's own business
        line = lines[l0]
        if line[py_col(line, int(a.get("character", 0))):py_col(line, int(b.get("character", 0)))] != old:
            return False
    return True


def cmd_rename(ctx: Ctx, what: str, name: Optional[str]) -> Tuple[int, str]:
    new = ctx.args.to
    if not new:
        raise UserError("the new name goes after --to: rename <file>:<line> <old> --to <new>")
    path, line0, ch, sym = resolve(ctx, what, name)
    if not ctx.sess.caps.get("renameProvider"):
        return 1, f"{ctx.sess.sid} can't rename — use `refs` and edit the places it lists"
    if ctx.sess.spec.get("open"):
        ctx.sess.open(path)
    pos = doc_pos(path, line0, ch)
    prov = ctx.sess.caps.get("renameProvider")
    if isinstance(prov, dict) and prov.get("prepareProvider"):
        try:
            if ctx.sess.request("textDocument/prepareRename", pos) is None:
                return 4, f"{ctx.sess.sid}: {sym} at {ctx.show(path)}:{line0 + 1} can't be renamed"
        except LspError as e:
            return 4, f"{ctx.sess.sid}: {sym} can't be renamed here ({e})"
    edit = ctx.sess.request("textDocument/rename", dict(pos, newName=new), timeout=180) or {}
    changes, ops = workspace_edits(edit)
    count = sum(len(v) for v in changes.values())
    if not count:
        return 4, f"{ctx.sess.sid} found nothing to rename for {sym}"
    outside = [p for p in changes if not under(p, ctx.root)]
    out = [header(ctx, f"rename {sym} → {new}: {count} edits in {len(changes)} files")]
    for p in sorted(changes, key=ctx.show):
        out.append(f"{ctx.show(p)}: {len(changes[p])}")
        for e in sorted(changes[p], key=lambda e: e["range"]["start"]["line"])[:6]:
            l0 = int(e["range"]["start"]["line"])
            out.append(f"  {l0 + 1}: {ctx.code(p, l0, int(e['range']['start']['character']))}")
        if len(changes[p]) > 6:
            out.append(f"  … {len(changes[p]) - 6} more")
    if ops:
        out.append("File operations the server also wants (not done — do them yourself if right): " + "; ".join(ops))
    if outside:
        out.append("Not applied: edits outside the project — " + ", ".join(ctx.show(p) for p in outside[:5]))
        return 1, "\n".join(out)
    if not ctx.args.apply:
        out.append("Preview only. Apply with the same command plus --apply, then re-read the files you edit next.")
        return 0, "\n".join(out + ctx.notes)
    plan = []
    for p, edits in changes.items():
        try:
            with open(p, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise UserError(f"{ctx.show(p)} isn't a readable UTF-8 file — nothing was changed")
        doc = ctx.sess.docs.get(p)
        if doc is not None and doc["text"] != text:
            raise UserError(f"{ctx.show(p)} changed while renaming — nothing was changed; run the command again")
        if not edits_match(text, edits, sym, new):
            raise UserError(f"{ctx.show(p)} doesn't have `{sym}` where the server put it (the file changed after the "
                            "server read it?) — nothing was changed; run the command again")
        plan.append((p, apply_edits(text, edits)))
    staged: List[Tuple[str, str]] = []
    try:  # every new file is written next to its original first: a failure here leaves nothing changed
        for p, new_text in plan:
            tmp = f"{p}.agp-rename.{os.getpid()}"
            staged.append((p, tmp))
            with open(tmp, "wb") as f:
                f.write(new_text.encode("utf-8"))
            try:
                os.chmod(tmp, os.stat(p).st_mode)
            except OSError:
                pass
    except OSError as e:
        for _p, tmp in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise UserError(f"couldn't write the renamed files ({e}) — nothing was changed")
    written, failed = [], []
    for p, tmp in staged:
        try:
            compat.replace(tmp, p)
            written.append(p)
        except OSError as e:
            failed.append(f"{ctx.show(p)} ({e})")
            try:
                os.remove(tmp)
            except OSError:
                pass
    for p in written:
        if p in ctx.sess.docs:
            try:
                ctx.sess.open(p)
            except LspError:
                pass
    try:
        ctx.sess.sync()
    except LspError:
        pass
    if failed:
        out.append(f"Renamed in {len(written)} files, but NOT in: " + "; ".join(failed) + " — finish those by hand "
                   "(or undo with git), then re-read the files and run the tests.")
        return 1, "\n".join(out + ctx.notes)
    out.append(f"Applied: {count} edits in {len(written)} files. Re-read these files before editing them; run the "
               f"tests (and `check` the files) to confirm.")
    return 0, "\n".join(out + ctx.notes)


def head_text(path: str) -> Optional[str]:
    """The file as last committed (None: not in git, or new)."""
    top = git_top(path)
    if not top:
        return None
    rel = os.path.relpath(path, top).replace(os.sep, "/")
    try:
        r = subprocess.run(["git", "-C", top, "show", f"HEAD:{rel}"], capture_output=True, timeout=5, **HIDE)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


def edit_check(sess: Session, path: str, budget: float) -> Optional[List[dict]]:
    """Errors in `path` that its committed version doesn't have (a new file: all of them), for hooks/check_edit;
    None when the server didn't say in time (unknown — not «no errors»)."""
    deadline = time.time() + budget
    path = os.path.realpath(path)
    new = read_text(path)
    base = head_text(path)
    if base is not None and base == new:
        return []  # back to the committed version: nothing the edit added
    base_items: List[dict] = []
    if base is not None:
        key = hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()
        cached = sess.outlines.get(("base", path))
        if cached and cached[0] == key:
            base_items = cached[1]
        else:
            got = sess.diagnostics(path, base, timeout=max(1.0, (deadline - time.time()) / 2))
            if got is None:
                sess.open(path)
                return None
            base_items = got
            sess.outlines[("base", path)] = (key, got)
    items = sess.diagnostics(path, None, timeout=max(1.0, deadline - time.time()))
    if items is None:
        return None
    if not items:
        return []

    def keyof(d: dict, lines: List[str]) -> tuple:
        l0 = int(((d.get("range") or {}).get("start") or {}).get("line", 0))
        code = d.get("code")
        return (str(d.get("message") or ""), str(code.get("value") if isinstance(code, dict) else code),
                lines[l0].strip() if 0 <= l0 < len(lines) else "")

    old: Dict[tuple, int] = {}
    base_lines = split_lines(base) if base is not None else []
    for d in base_items:
        if int(d.get("severity") or 1) == 1:
            k = keyof(d, base_lines)
            old[k] = old.get(k, 0) + 1
    new_lines = split_lines(new)
    out = []
    for d in items:
        if int(d.get("severity") or 1) != 1:
            continue
        k = keyof(d, new_lines)
        if old.get(k):
            old[k] -= 1
            continue
        start = (d.get("range") or {}).get("start") or {}
        l0 = int(start.get("line", 0))
        code = d.get("code")
        out.append({"kind": "type", "tool": sess.sid, "line": l0 + 1,
                    "col": (py_col(new_lines[l0], int(start.get("character", 0))) + 1) if l0 < len(new_lines) else 1,
                    "msg": re.sub(r"\s+", " ", str(d.get("message") or "")).strip()[:300],
                    "code": str(code.get("value") if isinstance(code, dict) else code or "")})
    return out[:20]


def execute(sess: Session, argv: List[str], cwd: str) -> Tuple[int, str]:
    """Run one command against a started session (in the daemon, or in this process with --no-daemon)."""
    args = parse_args(argv)
    ctx = Ctx(sess, args, cwd)
    first = time.time()
    if not sess.waited:  # the first command waits for the server to be ready; later ones answer at once, with a note
        sess.wait_ready(float(args.wait if args.wait is not None else sess.spec.get("wait") or 30))
        sess.waited = True
    sess.sync()
    cmd = COMMANDS.get(args.command, args.command)
    rest = args.rest
    try:
        if cmd in ("def", "type", "impl"):
            need(rest, 1, "<file>:<line> <name>")
            code, text = cmd_locations(ctx, cmd, rest[0], rest[1] if len(rest) > 1 else None)
        elif cmd == "hover":
            need(rest, 1, "<file>:<line> <name>")
            code, text = cmd_hover(ctx, rest[0], rest[1] if len(rest) > 1 else None)
        elif cmd == "refs":
            need(rest, 1, "<file>:<line> <name>")
            code, text = cmd_refs(ctx, rest[0], rest[1] if len(rest) > 1 else None)
        elif cmd == "calls":
            need(rest, 1, "<file>:<line> <name>")
            code, text = cmd_calls(ctx, rest[0], rest[1] if len(rest) > 1 else None)
        elif cmd == "symbols":
            need(rest, 1, "<file>")
            code, text = cmd_symbols(ctx, rest)
        elif cmd == "find":
            need(rest, 1, "<name>")
            code, text = cmd_find(ctx, rest[0])
        elif cmd == "check":
            need(rest, 1, "<file>…")
            code, text = cmd_check(ctx, rest)
        elif cmd == "rename":
            need(rest, 1, "<file>:<line> <name> --to <new>")
            code, text = cmd_rename(ctx, rest[0], rest[1] if len(rest) > 1 else None)
        else:
            raise UserError(f"unknown command: {args.command}")
    except UserError as e:
        return e.code, str(e)
    except LspError as e:
        return 1, f"{e}"
    took = time.time() - first
    if took > 8:
        text += f"\n({took:.0f} s — the server was starting or indexing; the next commands are faster)"
    return code, text


def need(rest: List[str], n: int, usage: str) -> None:
    if len(rest) < n:
        raise UserError(f"usage: lsp.py <command> {usage}")


# ----------------------------------------------------------------------------------------------- the daemon

def daemon_key(sid: str, root: str) -> str:
    return hashlib.sha1(f"{sid}\0{os.path.normcase(root)}".encode("utf-8")).hexdigest()[:16]


def code_version() -> str:
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return "?"


VERSION = code_version()


def state_path(key: str, ext: str = ".json") -> str:
    return os.path.join(STATE, key + ext)


def read_state(key: str) -> Optional[dict]:
    try:
        with open(state_path(key), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_state(key: str, data: dict) -> None:
    os.makedirs(STATE, exist_ok=True)
    tmp = f"{state_path(key)}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    if not WINDOWS:
        os.chmod(tmp, 0o600)
    compat.replace(tmp, state_path(key))


def all_states() -> List[Tuple[str, dict]]:
    out = []
    for p in sorted(glob.glob(os.path.join(STATE, "*.json"))):
        key = os.path.basename(p)[:-5]
        st = read_state(key)
        if st and st.get("server"):
            out.append((key, st))
    return out


def connect(st: dict, timeout: float) -> socket.socket:
    if st.get("transport") == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(st["address"])
    else:
        s = socket.create_connection(("127.0.0.1", int(st["address"])), timeout=timeout)
        s.settimeout(timeout)
    return s


def call(st: dict, request: dict, timeout: float, connect_timeout: float = 5.0) -> dict:
    """One request to a daemon: a JSON line there, a JSON line back."""
    s = connect(st, min(timeout, connect_timeout))
    try:
        s.settimeout(timeout)
        s.sendall(json.dumps(dict(request, token=st.get("token")), ensure_ascii=False).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf.strip():
        raise LspError("the background server closed the connection")
    return json.loads(buf.decode("utf-8", errors="replace"))


def ping(st: dict) -> Optional[dict]:
    try:
        return call(st, {"op": "ping"}, 3.0)
    except (OSError, ValueError, LspError):
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def stop_daemon(key: str, st: dict, wait: float = 15.0) -> bool:
    """Ask a daemon to stop and wait until its process is gone. One that doesn't answer is already dead: its state is
    forgotten, and nothing is killed by its old pids — they may belong to other processes by now."""
    try:
        call(st, {"op": "stop"}, 5.0)
    except (OSError, ValueError, LspError):
        cleanup(key, st.get("pid"))
        return False
    deadline = time.time() + wait
    while time.time() < deadline and pid_alive(st.get("pid")):
        time.sleep(0.1)
    if pid_alive(st.get("pid")):  # it answered a moment ago: these pids are still its own
        for pid in (st.get("server_pid"), st.get("pid")):
            if pid:
                kill_tree(int(pid))
    cleanup(key, st.get("pid"))
    return True


def sock_path(key: str) -> str:
    """Where a daemon listens: next to its state, or in /tmp when that path is too long for a Unix socket."""
    path = state_path(key, ".sock")
    if len(path.encode("utf-8")) <= 100:
        return path
    folder = os.path.join("/tmp", f"agp-lsp-{os.getuid()}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return os.path.join(folder, key + ".sock")


def cleanup(key: str, pid=None) -> None:
    """Forget a daemon's state file and socket — but only while they are still `pid`'s: a successor started for the
    same root in the meantime keeps its own."""
    st = read_state(key)
    if st is None or (pid is not None and st.get("pid") != pid):
        return
    for path in (state_path(key), st.get("address") if st.get("transport") == "unix" else ""):
        try:
            if path:
                os.remove(str(path))
        except OSError:
            pass


class Daemon:
    def __init__(self, key: str, sid: str, lang: str, root: str, first: str):
        self.key, self.sid, self.lang, self.root, self.first = key, sid, lang, root, first
        self.lock = threading.Lock()
        self.used = time.time()
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = ""
        os.makedirs(STATE, exist_ok=True)
        log_path = state_path(key, ".log")
        try:
            if os.path.getsize(log_path) > 2 * 1024 * 1024:
                os.remove(log_path)
        except OSError:
            pass
        self.log = open(log_path, "a", encoding="utf-8", errors="replace")
        self.sess = Session(sid, lang, root, log=self.log, first=first)
        self.token = hashlib.sha1(os.urandom(16)).hexdigest()

    def listen(self) -> socket.socket:
        if hasattr(socket, "AF_UNIX") and not WINDOWS:
            addr = sock_path(self.key)
            try:
                os.remove(addr)
            except OSError:
                pass
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o077)
            try:
                srv.bind(addr)
            finally:
                os.umask(old)
            transport = "unix"
        else:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            addr, transport = srv.getsockname()[1], "tcp"
        srv.listen(16)
        srv.settimeout(1.0)
        self.state = {"pid": os.getpid(), "server": self.sid, "lang": self.lang, "root": self.root,
                      "transport": transport, "address": addr, "token": self.token, "version": VERSION,
                      "started": time.time(), "used": time.time(), "cmd": self.sess.cmd}
        write_state(self.key, self.state)
        return srv

    def boot(self) -> None:
        try:
            self.sess.start()
            self.state["server_pid"] = self.sess.proc.pid if self.sess.proc else None
            write_state(self.key, self.state)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.sess.note(f"start failed: {e}")
        self.ready.set()

    def serve(self) -> None:
        srv = self.listen()
        threading.Thread(target=self.boot, daemon=True).start()
        try:
            while not self.stopping.is_set():
                if time.time() - self.used > IDLE and not self.lock.locked():
                    self.sess.note(f"idle for {int(IDLE)} s — stopping")
                    break
                if self.ready.is_set() and (self.error or not self.sess.alive):
                    self.sess.note("the server is gone — stopping" + (f": {self.error}" if self.error else ""))
                    time.sleep(2)  # let a waiting client read the error first
                    break
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            cleanup(self.key, os.getpid())  # first: a command arriving now starts a fresh daemon instead of waiting
            self.sess.stop()
            self.log.close()

    def handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.decode("utf-8", errors="replace"))
            if req.get("token") != self.token:
                return
            conn.settimeout(None)
            reply = self.answer(req)
            conn.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        except (OSError, ValueError) as e:
            self.sess.note(f"client: {e!r}")
        finally:
            conn.close()

    def answer(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "alive": self.sess.alive, "ready": self.sess.ready_once, "busy": self.sess.busy(),
                    "docs": len(self.sess.docs), "used": self.used, "error": self.error}
        if op == "stop":
            self.stopping.set()
            return {"ok": True}
        self.used = time.time()
        self.state["used"] = self.used
        try:
            write_state(self.key, self.state)
        except OSError:
            pass
        startup = float(self.sess.spec.get("startup") or 60)
        if not self.ready.wait(startup + 30) or self.error:
            return {"code": 1, "out": f"{self.sid} didn't start: {self.error or 'timed out'}"
                                      f" (log: {state_path(self.key, '.log')})"}
        if op == "edit-check":  # "problems": None = couldn't say this time (busy, not ready) — never «no errors»
            budget = float(req.get("budget") or 8)
            if not self.lock.acquire(timeout=max(0.5, budget / 3)):
                return {"problems": None}
            try:
                if not self.sess.ready_once and not self.sess.wait_ready(0.1):
                    return {"problems": None}
                self.sess.sync()
                return {"problems": edit_check(self.sess, str(req.get("path") or ""), budget * 2 / 3)}
            except (LspError, OSError, UserError) as e:
                self.sess.note(f"edit-check: {e}")
                return {"problems": None}
            finally:
                self.lock.release()
        with self.lock:
            try:
                code, out = execute(self.sess, list(req.get("argv") or []), str(req.get("cwd") or self.root))
            except Exception as e:  # noqa: BLE001 — answer the client whatever happens
                code, out = 1, f"lsp.py: {e!r}"
            self.used = time.time()
            return {"code": code, "out": out}


def spawn(key: str, sid: str, lang: str, root: str, first: str) -> None:
    os.makedirs(STATE, exist_ok=True)
    cmd = [sys.executable, os.path.abspath(__file__), "daemon", "--key", key, "--server", sid, "--lang", lang,
           "--root", root, "--first", first]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": root, "close_fds": True}
    if WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, own group, no window
    else:
        kw["start_new_session"] = True  # outlives the agent's command and its terminal
    subprocess.Popen(cmd, **kw)


def make_room() -> None:
    alive = [(k, s) for k, s in all_states() if ping(s)]
    alive.sort(key=lambda ks: ks[1].get("used") or 0)
    while len(alive) >= MAX_DAEMONS:
        key, st = alive.pop(0)
        stop_daemon(key, st)


def ensure_daemon(sid: str, lang: str, root: str, first: str) -> dict:
    """The running daemon for (server, root), started when there is none. Raises OSError when it can't start."""
    key = daemon_key(sid, root)
    os.makedirs(STATE, exist_ok=True)
    with open(state_path(key, ".lock"), "w") as fh:
        compat.lock(fh)
        try:
            st = read_state(key)
            if st and ping(st):
                if st.get("version") == VERSION:
                    return st
                stop_daemon(key, st)  # older code: restart it
            elif st:
                cleanup(key, st.get("pid"))
            make_room()
            spawn(key, sid, lang, root, first)
            deadline = time.time() + 15
            while time.time() < deadline:
                st = read_state(key)
                if st and st.get("pid") and ping(st):
                    return st
                time.sleep(0.05)
            raise OSError("the background server didn't come up (log: " + state_path(key, ".log") + ")")
        finally:
            compat.unlock(fh)


# ----------------------------------------------------------------------------------------------- for the hooks

def edit_problems(path: str, budget: float = 8.0) -> Optional[List[dict]]:
    """Errors the agent's edit of `path` introduced, from a server that is ALREADY running for it (never starts
    one: a cold server in a hook would cost seconds to minutes and gigabytes). None = no server could say this
    time — not the same as [] (checked: nothing new)."""
    apply_config()
    path = os.path.realpath(path)
    lang = lang_of(path)
    if not lang:
        return None
    root, _why = find_root(path, lang)
    for sid in LANGS[lang]["servers"]:
        key = daemon_key(sid, root)
        st = read_state(key)
        if not st or st.get("version") != VERSION:
            continue
        try:
            reply = call(st, {"op": "edit-check", "path": path, "budget": budget}, budget + 2, connect_timeout=0.5)
        except (ConnectionRefusedError, FileNotFoundError):
            cleanup(key, st.get("pid"))  # a dead daemon's leftovers: don't knock on its door at every edit
            return None
        except (OSError, ValueError, LspError):
            return None
        found = reply.get("problems")
        return [p for p in found if isinstance(p, dict)] if isinstance(found, list) else None
    return None


def swift_note(ws: str, root: str, why: str) -> str:
    """sourcekit-lsp sees an Xcode app's own code only through a build server (xcode-build-server)."""
    if why == "buildServer.json":
        return " (Xcode index via buildServer.json — as fresh as the last build)"
    xcodeproj = glob.glob(os.path.join(ws, "*.xcodeproj")) + glob.glob(os.path.join(ws, "*", "*.xcodeproj")) \
        + glob.glob(os.path.join(ws, "*", "*", "*.xcodeproj"))
    if xcodeproj and why != "Package.swift":
        return (" (an Xcode app without buildServer.json: the server sees Swift packages only — for the app's code "
                "the user can install xcode-build-server and run `xcode-build-server config -project <X>.xcodeproj "
                "-scheme <S>` once, then build)")
    return ""


def session_lines(ws: str) -> List[str]:
    """For the session context (hooks/session_start.py): the languages here that a language server covers, and the
    exact commands. Reads the project's file list only: nothing is started."""
    apply_config()
    langs = [(lang, n) for lang, n in languages_in(ws) if n >= 3][:5]
    have, missing = [], []
    for lang, n in langs:
        sid = pick_server(lang)
        if sid:
            note = ""
            if lang == "swift":
                sample = next((p for p in project_files(ws, LANGS["swift"]["ext"], limit=200)), "")
                root, why = find_root(os.path.realpath(sample), lang) if sample else (ws, "")
                note = swift_note(ws, root, why)
            have.append(f"{lang} → {sid}{note}")
        elif n >= 10:
            missing.append(f"{lang} ({install_hint(lang)})")
    if not have and not missing:
        return []
    out = []
    if have:
        out.append("Code navigation through language servers (skill /lsp): " + "; ".join(have) + ". For a symbol in "
                   "code, ask the server instead of rg — it knows calls from comments and same-named symbols:")
        out.append(f"- `{me()} refs <file>:<line> <name>` — every use, each with the function it is in; `def`, "
                   "`calls` (who calls it; `--out`: what it calls), `hover` (type, signature), `impl` take the same "
                   "position; `find <name>` locates a symbol, `symbols <file>` outlines a file before you read it, "
                   "`check <file>` lists type errors.")
        out.append("- Before renaming, deleting or changing a signature: `refs`/`calls` first, and update every place "
                   "they list. rg stays the tool for strings, config keys, comments and file names.")
    if missing:
        out.append("No language server here for: " + "; ".join(missing) + " — if a task needs one, tell the user the "
                   "install command; don't install it yourself.")
    return out


def search_hint(ws: str, names: List[str]) -> Optional[str]:
    """For hooks/instructions.py after the agent looked for symbol names with rg: the language server's commands."""
    apply_config()
    have = [f"{lang} → {sid}" for lang, n in languages_in(ws) if n >= 3 for sid in [pick_server(lang)] if sid]
    if not have:
        return None
    shown = ", ".join(f"`{n}`" for n in names[:3])
    return ('<code_navigation source="Antigravity Plus">\n'
            f"You looked for symbols with rg ({shown}). For a symbol in code, ask the language server — it tells a call "
            "from a comment, a string or another symbol with the same name, and lists each use with the function it is "
            f"in: `{me()} refs <file>:<line> <name>` (or just `refs <name>`), `calls` — who calls it, `def` — where it "
            f"is defined, `find <name>` — where it is. Servers here: {', '.join(have)}. rg stays right for text: "
            "strings, config keys, comments, file names.\n</code_navigation>")


# ----------------------------------------------------------------------------------------------- the CLI

COMMANDS = {"definition": "def", "goto": "def", "references": "refs", "ref": "refs", "usages": "refs",
            "callers": "calls", "incoming": "calls", "callees": "calls", "outgoing": "calls", "outline": "symbols",
            "symbol": "find", "search": "find", "workspace-symbol": "find", "diagnostics": "check", "errors": "check",
            "implementation": "impl", "implementations": "impl", "typedef": "type", "type-definition": "type",
            "info": "hover", "signature": "hover"}


def parse_args(argv: List[str]):
    p = argparse.ArgumentParser(prog="lsp.py", add_help=True, description="Code navigation through language servers.")
    p.add_argument("command")
    p.add_argument("rest", nargs="*")
    p.add_argument("--lang", help="language when no file says it (python, swift, kotlin, typescript, …)")
    p.add_argument("--path", help="a folder: the project (servers), the scope of refs/find")
    p.add_argument("--limit", type=int, help="reference lines to print (0 = all)")
    p.add_argument("--depth", type=int, help="calls: levels (1–5); symbols: nesting shown (0 = all)")
    p.add_argument("--out", action="store_true", help="calls: what the function calls instead of its callers")
    p.add_argument("--kind", help="find: only this kind (function, class, method, …)")
    p.add_argument("--to", help="rename: the new name")
    p.add_argument("--apply", action="store_true", help="rename: write the edits (default: preview)")
    p.add_argument("--no-decl", action="store_true", help="refs: leave the declaration out")
    p.add_argument("--all", action="store_true", help="symbols: also locals; stop: every server")
    p.add_argument("--errors", action="store_true", help="check: errors only")
    p.add_argument("--timeout", type=float, help="check: seconds to wait per file")
    p.add_argument("--wait", type=float, help="seconds to wait for a starting server")
    p.add_argument("--no-daemon", action="store_true", help="run the server just for this command")
    # internal: the daemon
    p.add_argument("--key", help=argparse.SUPPRESS)
    p.add_argument("--server", help=argparse.SUPPRESS)
    p.add_argument("--root", help=argparse.SUPPRESS)
    p.add_argument("--first", help=argparse.SUPPRESS)
    args = p.parse_intermixed_args(argv)
    if args.command in ("callees", "outgoing"):
        args.out = True
    return args


def target_file(args, cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """(the file this command is about, its language) — or (None, language from --lang / the project)."""
    cmd = COMMANDS.get(args.command, args.command)
    if args.lang:
        lang = {"py": "python", "ts": "typescript", "js": "typescript", "javascript": "typescript", "kt": "kotlin", "rs": "rust",
                "cpp": "c", "c++": "c", "objc": "c", "golang": "go", "cs": "csharp", "c#": "csharp"}.get(args.lang.lower(), args.lang.lower())
        if lang not in LANGS:
            raise UserError(f"unknown language {args.lang}: one of {', '.join(LANGS)}")
    else:
        lang = None
    if args.rest and cmd != "find":
        path, _l, _c = parse_target(args.rest[0], cwd)
        if path:
            return path, lang or lang_of(path)
    if lang:
        return None, lang
    scope = real(os.path.join(cwd, args.path)) if args.path else cwd
    present = [(lg, n) for lg, n in languages_in(scope) if pick_server(lg)]
    if len(present) == 1:
        return None, present[0][0]
    running = {s.get("lang") for _k, s in all_states() if under(scope, str(s.get("root") or "/-"))
               or under(str(s.get("root") or "/-"), scope)} & {lg for lg, _n in present}
    if len(running) == 1:
        return None, running.pop()
    if len(present) > 1 and present[0][1] >= 5 * present[1][1]:
        return None, present[0][0]
    if not present:
        raise UserError(f"no language with a language server in {scope} — `{me()} servers` shows what's missing")
    raise UserError("which language? This project has " + ", ".join(f"{lg} ({n} files)" for lg, n in present)
                    + " — add --lang <one of them>, or give a file: <file>:<line> <name>")


def run_command(args, argv: List[str], cwd: str) -> Tuple[int, str]:
    path, lang = target_file(args, cwd)
    if path and not lang:
        raise UserError(f"{os.path.basename(path)}: no language server knows this kind of file")
    sid = pick_server(lang)
    if not sid:
        return 2, (f"No {lang} language server on this computer. Install: {install_hint(lang)} — that's the user's "
                   f"call: tell them the command (rg still works meanwhile).")
    root, why = find_root(path or cwd, lang)
    if not path and why in ("the git repository", "the folder"):
        below = find_root_below(cwd, lang)
        if below:
            root = below[0]
    use_daemon = not args.no_daemon and os.environ.get("AGP_LSP_DAEMON", "1") != "0"
    if use_daemon:
        try:
            st = ensure_daemon(sid, lang, root, path or root)
            if not ping(st) or (not (read_state(daemon_key(sid, root)) or {}).get("server_pid")):
                print(f"(starting {sid} for {root} — the first command waits for it)", file=sys.stderr, flush=True)
            startup = float(SERVERS[sid].get("startup") or 60)
            reply = call(st, {"op": "run", "argv": argv, "cwd": cwd}, startup + 600)
            return int(reply.get("code") or 0), str(reply.get("out") or "")
        except (OSError, ValueError, LspError) as e:
            print(f"(no background server: {e} — running {sid} just for this command)", file=sys.stderr, flush=True)
    sess = Session(sid, lang, root, first=path or root)
    try:
        sess.start()
        return execute(sess, argv, cwd)
    finally:
        sess.stop()


def cmd_servers(args, cwd: str) -> Tuple[int, str]:
    folder = real(os.path.join(cwd, args.path)) if args.path else cwd
    langs = languages_in(folder)
    if not langs:
        return 0, f"No source files of a known language in {folder}."
    running = {(s.get("server"), s.get("root")): s for _k, s in all_states()}
    out = [f"Language servers for {folder}:"]
    for lang, n in langs:
        sid = pick_server(lang)
        if not sid:
            out.append(f"- {lang} ({n} files): no server — install: {install_hint(lang)}")
            continue
        files = project_files(folder, LANGS[lang]["ext"], limit=400)
        roots: Dict[str, str] = {}
        for p in files[:200]:
            r, why = find_root(os.path.realpath(p), lang)
            roots.setdefault(r, why)
        cmd = server_command(sid) or []
        parts = []
        for r, why in list(roots.items())[:3]:
            st = running.get((sid, r))
            state = f", running (pid {st.get('server_pid') or st.get('pid')})" if st and ping(st) else ""
            note = swift_note(folder, r, why) if lang == "swift" else ""
            parts.append(f"root {r} ({why}){state}{note}")
        if len(roots) > 3:
            parts.append(f"+{len(roots) - 3} more roots")
        out.append(f"- {lang} ({n} files): {sid} — {cmd[0] if cmd else '?'}; " + "; ".join(parts))
    out.append(f"Servers start on the first command and stop after {int(IDLE // 60)} idle minutes; `status` lists them.")
    return 0, "\n".join(out)


def memory_of(pid: Optional[int]) -> str:
    if not pid or WINDOWS:
        return ""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,pgid=,rss="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    kb = 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == str(pid):
            kb += int(parts[2])
    return f", {kb / 1024 / 1024:.1f} GB" if kb > 900 * 1024 else f", {kb // 1024} MB" if kb else ""


def cmd_status() -> Tuple[int, str]:
    rows = []
    for key, st in all_states():
        info = ping(st)
        if not info:
            cleanup(key, st.get("pid"))
            continue
        idle = int(time.time() - float(info.get("used") or st.get("used") or time.time()))
        up = int(time.time() - float(st.get("started") or time.time()))
        state = "ready" if info.get("ready") else f"starting: {info.get('busy')}"
        if info.get("error"):
            state = f"failed: {info['error']}"
        rows.append(f"- {st['server']} · {st['root']} · {state} · up {up // 60} min, idle {idle // 60} min"
                    f"{memory_of(st.get('server_pid'))}")
    if not rows:
        return 0, "No language servers running."
    return 0, "\n".join([f"Running language servers (each stops after {int(IDLE // 60)} idle minutes):"] + rows)


def cmd_stop(args, cwd: str) -> Tuple[int, str]:
    scope = real(os.path.join(cwd, args.path)) if args.path else (None if args.all else cwd)
    stopped = []
    for key, st in all_states():
        root = str(st.get("root") or "")
        if args.all or scope is None or under(root, scope) or under(scope, root):
            stop_daemon(key, st)
            stopped.append(f"{st.get('server')} ({root})")
    return 0, ("Stopped: " + ", ".join(stopped)) if stopped else "Nothing to stop."


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return int(e.code or 2)
    apply_config()
    cwd = real(os.getcwd())
    try:
        if args.command == "daemon":
            Daemon(args.key, args.server, args.lang, args.root, args.first or args.root).serve()
            return 0
        if args.command == "servers":
            code, out = cmd_servers(args, cwd)
        elif args.command == "status":
            code, out = cmd_status()
        elif args.command == "stop":
            code, out = cmd_stop(args, cwd)
        elif COMMANDS.get(args.command, args.command) in ("def", "type", "impl", "hover", "refs", "calls", "symbols",
                                                           "find", "check", "rename"):
            code, out = run_command(args, argv, cwd)
        else:
            code, out = 2, f"unknown command: {args.command} (run `{me()} help`)"
    except UserError as e:
        code, out = e.code, str(e)
    print(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
