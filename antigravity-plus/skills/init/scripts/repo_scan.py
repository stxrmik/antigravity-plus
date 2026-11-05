#!/usr/bin/env python3
"""Antigravity Plus — /init helper: collect repository facts for writing AGENTS.md.

Prints a compact markdown digest: layout, languages, manifests & scripts, CI, existing
agent-instruction files, README head and git conventions. Stdlib only.

Usage: repo_scan.py [repo_root]   (default: current directory)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter

IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "out", "target",
    ".next", ".nuxt", ".turbo", ".cache", "coverage", ".idea", ".vscode", "vendor", "Pods", ".gradle",
    ".dart_tool", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "bin", "obj", ".terraform",
    "DerivedData", ".expo", ".svelte-kit", ".parcel-cache", "site-packages",
}
LANG = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript/React", ".js": "JavaScript", ".jsx": "JavaScript/React",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".kts": "Kotlin", ".swift": "Swift", ".m": "Objective-C", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
    ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++ header", ".hpp": "C++", ".scala": "Scala",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".vue": "Vue", ".svelte": "Svelte", ".lua": "Lua",
    ".sql": "SQL", ".sh": "Shell", ".zig": "Zig", ".clj": "Clojure", ".hs": "Haskell", ".tf": "Terraform",
    ".proto": "Protobuf", ".graphql": "GraphQL", ".css": "CSS", ".scss": "SCSS", ".html": "HTML",
}
AGENT_FILES = [
    "AGENTS.md", "GEMINI.md", "CLAUDE.md", "CLAUDE.local.md", ".cursorrules", ".windsurfrules", ".clinerules",
    ".github/copilot-instructions.md", "CONTRIBUTING.md", ".editorconfig",
]
AGENT_DIRS = [".cursor/rules", ".agents/rules", ".agent/rules", ".claude/commands", ".claude/agents", ".windsurf/rules"]
MANIFESTS = [
    "package.json", "pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json", "pyproject.toml", "setup.py",
    "setup.cfg", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle.kts", "Gemfile", "composer.json", "mix.exs",
    "pubspec.yaml", "Package.swift", "Podfile", "CMakeLists.txt", "Makefile", "justfile", "Taskfile.yml",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "deno.json", "bunfig.toml",
    "tsconfig.json", "vite.config.ts", "next.config.js", "next.config.mjs", "next.config.ts", "jest.config.js",
    "jest.config.ts", "vitest.config.ts", "playwright.config.ts", "pytest.ini", "tox.ini", "noxfile.py",
    ".pre-commit-config.yaml", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs", ".prettierrc",
    "biome.json", "ruff.toml", ".golangci.yml", "rustfmt.toml", ".nvmrc", ".python-version", ".tool-versions",
]
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "package-lock.json": "npm", "bun.lockb": "bun",
             "bun.lock": "bun", "poetry.lock": "poetry", "uv.lock": "uv", "Pipfile.lock": "pipenv"}


def sh(cmd: list, cwd: str) -> str:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def read(path: str, limit: int = 4000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def walk(root: str, max_files: int = 20000):
    count = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in IGNORE_DIRS and not d.startswith(".") or d in (".github", ".agents", ".cursor", ".claude")]
        for fn in fns:
            yield os.path.join(dp, fn)
            count += 1
            if count >= max_files:
                return


def tree(root: str, depth: int = 2, max_entries: int = 25) -> list:
    lines = []

    def rec(path: str, prefix: str, d: int):
        try:
            entries = sorted(os.listdir(path))
        except OSError:
            return
        entries = [e for e in entries if e not in IGNORE_DIRS and not (e.startswith(".") and e not in (".github", ".agents"))]
        dirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(path, e))]
        shown = dirs + files
        for e in shown[:max_entries]:
            full = os.path.join(path, e)
            is_dir = os.path.isdir(full)
            lines.append(f"{prefix}{e}{'/' if is_dir else ''}")
            if is_dir and d < depth:
                rec(full, prefix + "  ", d + 1)
        if len(shown) > max_entries:
            lines.append(f"{prefix}… (+{len(shown) - max_entries} more)")

    rec(root, "", 1)
    return lines


def main() -> int:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    git_root = sh(["git", "rev-parse", "--show-toplevel"], root)
    if git_root:
        root = git_root
    out = [f"# Repo scan: `{root}`", ""]

    # Languages
    exts = Counter()
    test_files = 0
    total = 0
    for p in walk(root):
        total += 1
        ext = os.path.splitext(p)[1].lower()
        if ext in LANG:
            exts[LANG[ext]] += 1
        b = os.path.basename(p).lower()
        if re.search(r"(^test_|_test\.|\.test\.|\.spec\.|tests?\.py$)", b):
            test_files += 1
    out.append("## Languages (file counts)")
    out.append(", ".join(f"{l}: {c}" for l, c in exts.most_common(10)) or "(none detected)")
    out.append(f"Total files scanned: {total} · test-like files: {test_files}")
    out.append("")

    # Layout
    out.append("## Layout (depth 2)")
    out.append("```")
    out.extend(tree(root))
    out.append("```")
    out.append("")

    # Manifests
    found = [m for m in MANIFESTS if os.path.exists(os.path.join(root, m))]
    pms = sorted({v for k_, v in LOCKFILES.items() if os.path.exists(os.path.join(root, k_))})
    out.append("## Manifests & tooling")
    out.append(", ".join(f"`{m}`" for m in found) or "(none at root)")
    if pms:
        out.append(f"Package manager(s) by lockfile: {', '.join(pms)}")
    out.append("")

    pj = os.path.join(root, "package.json")
    if os.path.exists(pj):
        try:
            data = json.loads(read(pj, 200000))
            out.append("### package.json")
            for key in ("name", "packageManager", "type"):
                if key in data:
                    out.append(f"- {key}: `{data[key]}`")
            if data.get("workspaces"):
                out.append(f"- workspaces: `{data['workspaces']}`")
            if data.get("engines"):
                out.append(f"- engines: `{data['engines']}`")
            scripts = data.get("scripts") or {}
            if scripts:
                out.append("- scripts:")
                for k_, v in list(scripts.items())[:40]:
                    out.append(f"  - `{k_}`: `{v}`")
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            key_deps = [d for d in deps if re.match(
                r"^(react|next|vue|nuxt|svelte|@sveltejs/kit|angular|@angular/core|express|fastify|nestjs|@nestjs/core|"
                r"vite|webpack|jest|vitest|mocha|playwright|@playwright/test|cypress|typescript|eslint|prettier|biome|"
                r"@biomejs/biome|prisma|drizzle-orm|typeorm|mongoose|tailwindcss|electron|react-native|expo|trpc|"
                r"@trpc/server|graphql|zod|turbo|nx)$", d)]
            if key_deps:
                out.append(f"- key deps: {', '.join(f'`{d}@{deps[d]}`' for d in key_deps)}")
            out.append("")
        except (ValueError, TypeError):
            pass

    for name, pat in (("pyproject.toml", r"^\[(tool\.[\w.-]+|project(\.scripts)?|build-system)\]"),):
        p = os.path.join(root, name)
        if os.path.exists(p):
            txt = read(p, 30000)
            sections = re.findall(pat, txt, re.M)
            out.append(f"### {name}")
            out.append(f"- sections: {', '.join(sorted(set(sections)))}")
            m = re.search(r"requires-python\s*=\s*\"([^\"]+)\"", txt)
            if m:
                out.append(f"- requires-python: `{m.group(1)}`")
            out.append("")

    mk = os.path.join(root, "Makefile")
    if os.path.exists(mk):
        targets = re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", read(mk, 50000), re.M)
        targets = [t for t in targets if not t.startswith(".")]
        out.append("### Makefile targets")
        out.append(", ".join(f"`{t}`" for t in dict.fromkeys(targets)) or "(none)")
        out.append("")
    jf = os.path.join(root, "justfile")
    if os.path.exists(jf):
        recipes = re.findall(r"^([A-Za-z0-9_-]+)(?:\s+[^:]*)?:", read(jf, 50000), re.M)
        out.append("### justfile recipes")
        out.append(", ".join(f"`{r}`" for r in dict.fromkeys(recipes)))
        out.append("")

    # CI
    ci = []
    wf = os.path.join(root, ".github", "workflows")
    if os.path.isdir(wf):
        for fn in sorted(os.listdir(wf)):
            txt = read(os.path.join(wf, fn), 20000)
            runs = re.findall(r"^\s*(?:-\s*)?run:\s*(.+)$", txt, re.M)
            ci.append(f"- `.github/workflows/{fn}`: " + "; ".join(f"`{r.strip()[:80]}`" for r in runs[:8]))
    for f in (".gitlab-ci.yml", ".circleci/config.yml", "azure-pipelines.yml", "Jenkinsfile", "bitbucket-pipelines.yml"):
        if os.path.exists(os.path.join(root, f)):
            ci.append(f"- `{f}`")
    out.append("## CI")
    out.extend(ci or ["(none found)"])
    out.append("")

    # Existing agent instructions
    out.append("## Existing agent / contributor instructions")
    any_found = False
    for f in AGENT_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            any_found = True
            txt = read(p, 3000)
            out.append(f"### `{f}` ({os.path.getsize(p)} bytes)")
            out.append("```")
            out.append(txt.rstrip())
            out.append("```")
    for d in AGENT_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            any_found = True
            out.append(f"### `{d}/`: " + ", ".join(sorted(os.listdir(p))[:30]))
    # nested AGENTS.md / CLAUDE.md
    nested = [os.path.relpath(p, root) for p in walk(root)
              if os.path.basename(p) in ("AGENTS.md", "CLAUDE.md", "GEMINI.md") and os.path.dirname(p) != root]
    if nested:
        any_found = True
        out.append("Nested instruction files: " + ", ".join(f"`{n}`" for n in nested[:20]))
    if not any_found:
        out.append("(none)")
    out.append("")

    # README
    for f in ("README.md", "README.rst", "README.txt", "README"):
        p = os.path.join(root, f)
        if os.path.isfile(p):
            out.append(f"## {f} (first 60 lines)")
            out.append("```")
            out.extend(read(p, 8000).splitlines()[:60])
            out.append("```")
            out.append("")
            break

    # Git
    if git_root:
        out.append("## Git")
        out.append(f"- branch: `{sh(['git', 'branch', '--show-current'], root)}`")
        head = sh(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
        if head:
            out.append(f"- default remote branch: `{head}`")
        log = sh(["git", "log", "-n", "15", "--pretty=format:%s"], root)
        if log:
            out.append("- recent commit subjects (style reference):")
            out.extend(f"  - {l}" for l in log.splitlines())
        out.append("")
    return print("\n".join(out)) or 0


if __name__ == "__main__":
    sys.exit(main())
