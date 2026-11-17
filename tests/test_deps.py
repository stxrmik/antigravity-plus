"""/deps: ecosystem scan, monorepo folding/skipping, audit & outdated parsers, osv-scanner fallback, missing tools, timeouts, exit codes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

PLUGIN, SP = sys.argv[1], sys.argv[2]
DEPS = os.path.join(PLUGIN, "skills", "deps", "scripts", "deps.py")
WIN = os.name == "nt"

sys.path.insert(0, os.path.join(PLUGIN, "skills", "deps", "scripts"))
import deps as deptool  # type: ignore[import-not-found]  # noqa: E402

ok = True


def check(name: str, cond: object, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1200]}" if not cond and detail != "" else ""))


def write(root, rel, content):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def make_exec(path, target_script, tool_name):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if WIN:
        cmd_path = path if path.endswith(".cmd") else path + ".cmd"
        with open(cmd_path, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{target_script}" {tool_name} %*\r\n')
        return cmd_path
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\nimport runpy, sys\nsys.argv = [sys.argv[0], {tool_name!r}, *sys.argv[1:]]\nrunpy.run_path({target_script!r}, run_name='__main__')\n")
    os.chmod(path, 0o755)
    return path


# ── Cross-platform fake CLI dispatcher ──
FAKE_CLI = os.path.join(SP, "fake_deps_cli.py")
FAKE_CLI_SRC = r'''import json, os, sys, time

tool = sys.argv[1]
argv = sys.argv[2:]
state_file = os.environ.get("AGP_FAKE_DEPS_STATE", "")
try:
    with open(state_file, encoding="utf-8") as f:
        st = json.load(f)
except (OSError, ValueError):
    st = {}

if st.get("sleep"):
    time.sleep(float(st["sleep"]))

if tool == "venv-python":
    if argv[:1] == ["-c"]:
        sys.stdout.write(json.dumps(st.get("venv_pkgs", [])))
        sys.exit(0)
    if argv[:3] == ["-m", "pip", "list"]:
        if st.get("no_pip"):
            sys.stderr.write("No module named pip\n")
            sys.exit(1)
        sys.stdout.write(json.dumps(st.get("pip_outdated", [])))
        sys.exit(0)
    sys.exit(2)

if tool == "uv":
    if argv[:3] == ["pip", "list", "--outdated"]:
        sys.stdout.write(json.dumps(st.get("uv_outdated", [])))
        sys.exit(0)
    sys.exit(2)

if tool == "pip-audit":
    req_pkgs = []
    if "-r" in argv:
        for i, a in enumerate(argv):
            if a == "-r" and i + 1 < len(argv):
                with open(argv[i + 1], encoding="utf-8") as rf:
                    req_pkgs.extend(ln.strip() for ln in rf if ln.strip())
    mode = st.get("pip_audit", "vuln")
    if mode == "clean":
        sys.stdout.write(json.dumps({"dependencies": []}))
        sys.exit(0)
    deps = []
    for spec in req_pkgs or ["requests==2.31.0"]:
        name, _, ver = spec.partition("==")
        vulns = []
        if name == "requests":
            vulns = [{
                "id": "PYSEC-2023-74",
                "fix_versions": ["2.31.1", "2.32.0"],
                "aliases": ["CVE-2023-32681", "GHSA-j8r2-6x86-q33q"],
                "description": "Unintended leak of Proxy-Authorization header.",
            }]
        deps.append({"name": name, "version": ver, "vulns": vulns})
    sys.stdout.write(json.dumps({"dependencies": deps}))
    sys.exit(1 if any(d["vulns"] for d in deps) else 0)

if tool == "npm":
    if argv[:1] == ["audit"]:
        if st.get("npm_audit_error"):
            sys.stdout.write(json.dumps({"error": {"code": "ENOTFOUND", "summary": "network unreachable"}}))
            sys.exit(1)
        if st.get("npm_clean"):
            sys.stdout.write(json.dumps({"auditReportVersion": 2, "vulnerabilities": {}}))
            sys.exit(0)
        sys.stdout.write(json.dumps({
            "auditReportVersion": 2,
            "vulnerabilities": {
                "lodash": {
                    "name": "lodash",
                    "severity": "critical",
                    "range": "<4.17.21",
                    "fixAvailable": True,
                    "via": [{
                        "source": 1001,
                        "name": "lodash",
                        "title": "Prototype Pollution in lodash",
                        "url": "https://github.com/advisories/GHSA-jf85-cpcp-j695",
                        "severity": "critical",
                        "range": "<4.17.21",
                    }],
                },
                "wrapper": {
                    "name": "wrapper",
                    "severity": "critical",
                    "via": ["lodash"],
                    "fixAvailable": False,
                },
                "mini": {
                    "name": "mini",
                    "severity": "moderate",
                    "range": "<1.2.6",
                    "fixAvailable": {"name": "mini", "version": "2.0.0", "isSemVerMajor": True},
                    "via": [{
                        "source": 1002,
                        "name": "mini",
                        "title": "ReDoS in mini",
                        "url": "https://github.com/advisories/GHSA-1111-2222-3333",
                        "severity": "moderate",
                        "range": "<1.2.6",
                    }],
                },
            },
        }))
        sys.exit(1)
    if argv[:1] == ["outdated"]:
        if st.get("npm_clean"):
            sys.stdout.write("{}")
            sys.exit(0)
        sys.stdout.write(json.dumps({
            "alpha": {"current": "1.0.0", "wanted": "1.2.0", "latest": "2.0.0"},
            "beta": [{"current": "1.1.0", "wanted": "1.2.0", "latest": "1.3.0"}],
            "gamma": {"current": "2.0.0", "wanted": "2.0.1", "latest": "2.0.1"},
            "delta": {"current": "3.0.0", "wanted": "3.0.0", "latest": "3.0.0"},
        }))
        sys.exit(1)

if tool == "pnpm":
    if argv[:1] == ["audit"]:
        sys.stdout.write(json.dumps({
            "advisories": {
                "42": {
                    "id": 42,
                    "module_name": "ws",
                    "severity": "high",
                    "title": "DoS in ws",
                    "url": "https://github.com/advisories/GHSA-3h5v-q93c-6h6q",
                    "cves": ["CVE-2024-37890"],
                    "vulnerable_versions": "<8.17.1",
                    "patched_versions": "<0.0.0",
                    "findings": [{"version": "8.17.0"}],
                }
            }
        }))
        sys.exit(1)
    if argv[:1] == ["outdated"]:
        sys.stdout.write(json.dumps([
            {"name": "ws", "current": "8.17.0", "wanted": "8.17.1", "latest": "8.18.0"},
        ]))
        sys.exit(1)

if tool == "cargo-audit":
    if argv[:2] != ["audit", "--json"]:
        sys.stderr.write(f"unexpected cargo-audit args: {argv}\n")
        sys.exit(2)
    if st.get("cargo_clean"):
        sys.stdout.write(json.dumps({"vulnerabilities": {"found": False, "count": 0, "list": []}}))
        sys.exit(0)
    sys.stdout.write(json.dumps({
        "vulnerabilities": {
            "found": True,
            "count": 1,
            "list": [{
                "advisory": {
                    "id": "RUSTSEC-2023-0071",
                    "package": "rsa",
                    "title": "Timing sidechannel in rsa crate",
                    "url": "https://rustsec.org/advisories/RUSTSEC-2023-0071",
                    "aliases": ["CVE-2023-49092"],
                },
                "versions": {"patched": [">=0.9.6"]},
                "package": {"name": "rsa", "version": "0.9.2"},
            }],
        }
    }))
    sys.exit(1)

if tool == "cargo-outdated":
    if st.get("cargo_clean"):
        sys.stdout.write(json.dumps({"dependencies": []}))
        sys.exit(0)
    sys.stdout.write(json.dumps({
        "dependencies": [
            {"name": "serde", "project": "1.0.100", "compat": "1.0.210", "latest": "1.0.210", "kind": "Normal"},
            {"name": "tokio", "project": "1.0.0", "compat": "---", "latest": "Removed", "kind": "Normal"},
        ]
    }))
    sys.exit(0)

if tool == "govulncheck":
    lines = [
        {"config": {"protocol_version": "v1.0.0"}},
        {"osv": {"id": "GO-2024-0001", "aliases": ["CVE-2024-0001"], "summary": "Imported issue."}},
        {"osv": {"id": "GO-2024-0002", "aliases": ["CVE-2024-0002"], "summary": "Called issue."}},
        {"finding": {"osv": "GO-2024-0001", "fixed_version": "v1.2.3",
                     "trace": [{"module": "example.com/liba", "version": "v1.2.0", "package": "example.com/liba"}]}},
        {"finding": {"osv": "GO-2024-0002", "fixed_version": "v2.0.1",
                     "trace": [{"module": "example.com/libb", "version": "v2.0.0", "function": "Parse"}]}},
    ]
    sys.stdout.write("\n".join(json.dumps(x) for x in lines) + "\n")
    sys.exit(3)

if tool == "go":
    objs = [
        {"Path": "example.com/app", "Main": True},
        {"Path": "example.com/libb", "Version": "v2.0.0", "Update": {"Version": "v2.0.1"}, "Indirect": True},
    ]
    sys.stdout.write("\n".join(json.dumps(x) for x in objs) + "\n")
    sys.exit(0)

if tool == "osv-scanner":
    if st.get("osv_empty"):
        sys.stdout.write(json.dumps({"results": []}))
        sys.exit(0)
    src = st.get("osv_source", "Cargo.lock")
    eco = st.get("osv_eco", "crates.io")
    pkg = st.get("osv_pkg", "rsa")
    ver = st.get("osv_ver", "0.9.2")
    fixed = st.get("osv_fixed", "0.9.6")
    sys.stdout.write(json.dumps({
        "results": [{
            "source": {"path": src, "type": "lockfile"},
            "packages": [{
                "package": {"name": pkg, "version": ver, "ecosystem": eco},
                "groups": [{"ids": ["GHSA-c38w-74pg-36hr", "RUSTSEC-2023-0071"], "max_severity": "7.5"}],
                "vulnerabilities": [{
                    "id": "GHSA-c38w-74pg-36hr",
                    "aliases": ["CVE-2023-49092"],
                    "summary": "Marvin Attack timing vulnerability.",
                    "affected": [{
                        "package": {"name": pkg.upper() if eco == "PyPI" else pkg},
                        "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": fixed}, {"fixed": "2.0.0"}]}],
                    }],
                }],
            }],
        }]
    }))
    sys.exit(1)

sys.exit(2)
'''
with open(FAKE_CLI, "w", encoding="utf-8") as _f:
    _f.write(FAKE_CLI_SRC)

SYS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32") if WIN else ""
EMPTY_BIN = os.path.join(SP, "empty_bin")
os.makedirs(EMPTY_BIN, exist_ok=True)
STATE_FILE = os.path.join(SP, "fake_deps_state.json")


def make_env(bindir, **state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    path = bindir + (os.pathsep + SYS_PATH if WIN else "")
    return dict(os.environ, PATH=path, AGP_FAKE_DEPS_STATE=STATE_FILE)


def run_deps(args: List[str], env: Dict[str, str]) -> Tuple[int, str, str]:
    r = subprocess.run([sys.executable, DEPS] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout, r.stderr


def run_json(args: List[str], env: Dict[str, str]) -> Tuple[int, Dict[str, Any], str]:
    code, out, err = run_deps(list(args) + ["--json"], env)
    try:
        parsed = json.loads(out)
        return code, (parsed if isinstance(parsed, dict) else {"_val": parsed}), err
    except ValueError:
        return code, {"_raw": out, "_err": err}, err


# ── 1. Ecosystem detection: scan, monorepo folding, skipping node_modules/.venv/vendor, Swift Xcode ──
mono = os.path.join(SP, "monorepo")
write(mono, "package.json", '{"name": "root"}\n')
write(mono, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
write(mono, "packages/web/package.json", '{"name": "@demo/web"}\n')
write(mono, "packages/api/package.json", '{"name": "@demo/api"}\n')
write(mono, "packages/api/package-lock.json", '{"lockfileVersion": 3}\n')
write(mono, "node_modules/hidden-pkg/package.json", '{"name": "ignored"}\n')

write(mono, "services/py/pyproject.toml", '[project]\nname = "svc"\nversion = "0.1.0"\n')
write(mono, "services/py/.venv/pyvenv.cfg", "home = /usr/bin\n")
py_rel = "services/py/.venv/Scripts/python.cmd" if WIN else "services/py/.venv/bin/python3"
make_exec(os.path.join(mono, *py_rel.split("/")), FAKE_CLI, "venv-python")
write(mono, "services/py/.venv/lib/site-packages/dep/pyproject.toml", '[project]\nname = "ignored"\n')

write(mono, "services/py2/requirements/prod.txt", "requests==2.31.0\n")
write(mono, "crates/core/Cargo.toml", '[package]\nname = "core"\nversion = "0.1.0"\n')
write(mono, "crates/core/Cargo.lock", "# lock\n")
write(mono, "crates/core/sub/Cargo.toml", '[package]\nname = "sub"\nversion = "0.1.0"\n')
write(mono, "apps/Demo.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved", '{"pins":[]}\n')
write(mono, "vendor/third/go.mod", "module third\n")

BIN_SCAN = os.path.join(SP, "bin_scan")
for t in ("pnpm", "npm", "cargo-audit"):
    make_exec(os.path.join(BIN_SCAN, t), FAKE_CLI, t)

code, data, err = run_json(["scan", "--path", mono], make_env(BIN_SCAN))
ecos = data.get("ecosystems", [])
by_eco_dir = {(e["ecosystem"], e["dir"]): e for e in ecos}
check("scan exits 0", code == 0, (code, data, err))
check("scan skips node_modules, .venv site-packages, and vendor",
      not any("node_modules" in e["dir"] or ".venv" in e["dir"] or "vendor" in e["dir"] for e in ecos), ecos)
check("scan folds workspace member without own lockfile into root pnpm entry",
      ("js", ".") in by_eco_dir and by_eco_dir[("js", ".")]["manager"] == "pnpm"
      and by_eco_dir[("js", ".")]["members"] == ["packages/web"], by_eco_dir)
check("scan keeps subpackage with own lockfile as separate npm entry",
      ("js", "packages/api") in by_eco_dir and by_eco_dir[("js", "packages/api")]["manager"] == "npm", by_eco_dir)
check("scan detects Python .venv, requirements/*.txt lock, Rust folding, Swift Xcode owner",
      ("python", "services/py") in by_eco_dir and by_eco_dir[("python", "services/py")]["venv"] == "services/py/.venv"
      and "requirements/prod.txt" in by_eco_dir.get(("python", "services/py2"), {}).get("locks", [])
      and by_eco_dir.get(("rust", "crates/core"), {}).get("members") == ["sub"]
      and ("swift", "apps") in by_eco_dir, by_eco_dir)
check("scan JSON keeps internal abs/python hidden and reports tools",
      all("abs" not in e and "python" not in e for e in ecos)
      and by_eco_dir[("js", ".")]["tools"] == {"pnpm": True}
      and data.get("osv_scanner") is False, data)

# ── 2. Python: venv pip-audit, requirements/*.txt, pip outdated + 0.x major + uv fallback ──
py_proj = os.path.join(SP, "py_proj")
write(py_proj, "pyproject.toml", '[project]\nname = "pyproj"\nversion = "1.0.0"\n')
write(py_proj, ".venv/pyvenv.cfg", "home = /usr/bin\n")
make_exec(os.path.join(py_proj, ".venv", "Scripts" if WIN else "bin", "python.cmd" if WIN else "python3"),
          FAKE_CLI, "venv-python")
make_exec(os.path.join(py_proj, ".venv", "Scripts" if WIN else "bin", "pip-audit"), FAKE_CLI, "pip-audit")

env_py = make_env(EMPTY_BIN,
                  venv_pkgs=[["requests", "2.31.0"], ["urllib3", "1.26.5"]],
                  pip_outdated=[
                      {"name": "django", "version": "4.2.0", "latest_version": "5.0.1"},
                      {"name": "httpx", "version": "0.25.0", "latest_version": "0.27.0"},
                      {"name": "requests", "version": "2.31.0", "latest_version": "2.32.3"},
                      {"name": "urllib3", "version": "2.2.0", "latest_version": "2.2.1"},
                  ])
code, data, err = run_json(["audit", "--path", py_proj], env_py)
py_aud = (data.get("ecosystems") or [{}])[0].get("audit", {})
check("pip-audit in .venv finds PYSEC vuln and exits 1",
      code == 1 and data["summary"]["vulnerabilities"] == 1
      and py_aud.get("vulns", [{}])[0].get("id") == "PYSEC-2023-74"
      and "2.31.1" in py_aud.get("vulns", [{}])[0].get("fixed", ""), (code, data, err))

code, data, err = run_json(["outdated", "--path", py_proj], env_py)
pkgs = {p["package"]: p["kind"] for p in (data.get("ecosystems") or [{}])[0].get("outdated", {}).get("packages", [])}
check("pip outdated classifies major / 0.x-minor-as-major / minor / patch and exits 1",
      code == 1 and pkgs == {"django": "major", "httpx": "major", "requests": "minor", "urllib3": "patch"}
      and data["summary"]["outdated_major"] == 2, (code, data, err))

# uv fallback when pip is missing in venv
BIN_UV = os.path.join(SP, "bin_uv")
make_exec(os.path.join(BIN_UV, "uv"), FAKE_CLI, "uv")
env_uv = make_env(BIN_UV, no_pip=True, uv_outdated=[{"name": "ruff", "version": "0.6.0", "latest_version": "0.6.2"}])
code, data, err = run_json(["outdated", "--path", py_proj], env_uv)
out_uv = (data.get("ecosystems") or [{}])[0].get("outdated", {})
check("uv fallback runs when venv has no pip",
      code == 1 and out_uv.get("tool") == "uv pip list" and out_uv.get("packages", [{}])[0].get("kind") == "patch",
      (code, data, err))

# pinned requirements/prod.txt without .venv
py_req = os.path.join(SP, "py_req")
write(py_req, "requirements/prod.txt", "requests==2.31.0\n# comment\n-e ./local\n" if False else "requests==2.31.0\n# comment\n")
BIN_PA = os.path.join(SP, "bin_pa")
make_exec(os.path.join(BIN_PA, "pip-audit"), FAKE_CLI, "pip-audit")
code, data, err = run_json(["audit", "--path", py_req], make_env(BIN_PA))
check("pinned requirements/*.txt audited without .venv",
      code == 1 and data["summary"]["vulnerabilities"] == 1, (code, data, err))

# ── 3. JS npm / pnpm audit & outdated + clean exit 0 ──
BIN_ALL = os.path.join(SP, "bin_all")
for t in ("npm", "pnpm", "cargo-audit", "cargo-outdated", "govulncheck", "go", "osv-scanner"):
    make_exec(os.path.join(BIN_ALL, t), FAKE_CLI, t)

js_npm = os.path.join(SP, "js_npm")
write(js_npm, "package.json", '{"name": "js-demo", "version": "1.0.0"}\n')
write(js_npm, "package-lock.json", '{"lockfileVersion": 3}\n')

code, data, err = run_json(["all", "--path", js_npm], make_env(BIN_ALL))
js_e = (data.get("ecosystems") or [{}])[0]
vulns = js_e.get("audit", {}).get("vulns", [])
out_pkgs = {p["package"]: p["kind"] for p in js_e.get("outdated", {}).get("packages", [])}
check("npm audit parses v2 JSON, extracts GHSA, avoids string-via dup, orders critical first",
      code == 1 and len(vulns) == 2 and vulns[0]["package"] == "lodash" and vulns[0]["severity"] == "critical"
      and vulns[0]["id"] == "GHSA-jf85-cpcp-j695" and vulns[1]["package"] == "mini"
      and "(major)" in vulns[1]["fix"], (code, data, err))
check("npm outdated parses dict + list rows, classifies major/minor/patch, and drops up-to-date delta",
      out_pkgs == {"alpha": "major", "beta": "minor", "gamma": "patch"}, js_e.get("outdated"))

code, data, err = run_json(["all", "--path", js_npm], make_env(BIN_ALL, npm_clean=True))
check("clean npm audit + outdated exits 0",
      code == 0 and data["summary"]["vulnerabilities"] == 0 and data["summary"]["outdated"] == 0, (code, data, err))

js_pnpm = os.path.join(SP, "js_pnpm")
write(js_pnpm, "package.json", '{"name": "pnpm-demo"}\n')
write(js_pnpm, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
code, data, err = run_json(["all", "--path", js_pnpm], make_env(BIN_ALL))
pnpm_e = (data.get("ecosystems") or [{}])[0]
check("pnpm audit (v6 advisories) & outdated (list JSON) parsed cleanly",
      code == 1 and pnpm_e["audit"]["vulns"][0]["package"] == "ws"
      and pnpm_e["audit"]["vulns"][0]["fix"] == "no fixed version yet"
      and pnpm_e["outdated"]["packages"][0]["kind"] == "minor", (code, data, err))

# ── 4. Rust cargo-audit & cargo-outdated ──
rust_proj = os.path.join(SP, "rust_proj")
write(rust_proj, "Cargo.toml", '[package]\nname = "rs"\nversion = "0.1.0"\n')
write(rust_proj, "Cargo.lock", "# lock\n")
code, data, err = run_json(["all", "--path", rust_proj], make_env(BIN_ALL))
rs_e = (data.get("ecosystems") or [{}])[0]
check("cargo-audit & cargo-outdated parsed and exit 1",
      code == 1 and rs_e["audit"]["vulns"][0]["id"] == "RUSTSEC-2023-0071"
      and rs_e["outdated"]["packages"] == [
          {"package": "serde", "current": "1.0.100", "wanted": "1.0.210", "latest": "1.0.210", "kind": "patch", "note": ""}
      ], (code, data, err))
code, data, err = run_json(["all", "--path", rust_proj], make_env(BIN_ALL, cargo_clean=True))
check("clean cargo-audit & cargo-outdated exits 0", code == 0, (code, data, err))

# ── 5. Go govulncheck & go list -m -u ──
go_proj = os.path.join(SP, "go_proj")
write(go_proj, "go.mod", "module example.com/app\ngo 1.22\n")
code, data, err = run_json(["all", "--path", go_proj], make_env(BIN_ALL))
go_e = (data.get("ecosystems") or [{}])[0]
go_vulns = go_e.get("audit", {}).get("vulns", [])
check("govulncheck puts called vulns before imported-only and parses go outdated",
      code == 1 and len(go_vulns) == 2 and go_vulns[0]["note"] == "your code calls it"
      and go_vulns[1]["note"] == "imported, not called"
      and go_e["outdated"]["packages"][0]["note"] == "indirect", (code, data, err))

# ── 6. osv-scanner fallback (with relative source.path) + Swift rescue ──
BIN_OSV = os.path.join(SP, "bin_osv")
make_exec(os.path.join(BIN_OSV, "osv-scanner"), FAKE_CLI, "osv-scanner")
code, data, err = run_json(["audit", "--path", rust_proj], make_env(BIN_OSV))
osv_aud = (data.get("ecosystems") or [{}])[0].get("audit", {})
check("osv-scanner fallback resolves relative source.path against scan root and picks nearest fixed version",
      code == 1 and osv_aud.get("tool") == "osv-scanner"
      and osv_aud.get("vulns", [{}])[0].get("fixed") == "0.9.6"
      and osv_aud.get("vulns", [{}])[0].get("severity") == "high", (code, data, err))

swift_proj = os.path.join(SP, "swift_proj")
write(swift_proj, "Package.swift", "// swift-tools-version:5.9\n")
write(swift_proj, "Package.resolved", '{"pins":[]}\n')
code, data, err = run_json(["audit", "--path", swift_proj], make_env(BIN_OSV, osv_empty=True))
check("osv-scanner rescues Swift native-unsupported audit to exit 0",
      code == 0 and data["ecosystems"][0]["audit"]["tool"] == "osv-scanner", (code, data, err))

# ── 7. Missing tool message & exit code 2, missing lockfile, empty dir, bad path ──
code, text_out, err = run_deps(["audit", "--path", rust_proj], make_env(EMPTY_BIN))
check("missing cargo-audit and osv-scanner prints install hints and exits 2",
      code == 2 and "not checked (missing)" in text_out and "cargo install cargo-audit" in text_out
      and "osv-scanner" in text_out, (code, text_out, err))

code, data, err = run_json(["all", "--path", rust_proj], make_env(EMPTY_BIN))
check("all --json with all tools missing exits 2 and counts not_checked",
      code == 2 and data["summary"]["not_checked"] == 2 and data["exit"] == 2, (code, data, err))

no_lock = os.path.join(SP, "no_lock")
write(no_lock, "package.json", '{"name": "nolock"}\n')
code, data, err = run_json(["audit", "--path", no_lock], make_env(BIN_ALL))
check("missing lockfile refuses audit with failed and exits 2",
      code == 2 and data["ecosystems"][0]["audit"]["status"] == "failed"
      and "no lockfile" in data["ecosystems"][0]["audit"]["message"], (code, data, err))

empty_proj = os.path.join(SP, "empty_proj")
os.makedirs(empty_proj, exist_ok=True)
code, data, err = run_json(["all", "--path", empty_proj], make_env(EMPTY_BIN))
check("empty directory exits 0", code == 0 and data["summary"]["ecosystems"] == 0, (code, data, err))

code, _, _ = run_deps(["scan", "--path", os.path.join(SP, "does_not_exist")], make_env(EMPTY_BIN))
check("nonexistent --path exits 2", code == 2, code)

# ── 8. Timeout handling kills process and exits 2 ──
BIN_ONLY_CARGO = os.path.join(SP, "bin_cargo")
make_exec(os.path.join(BIN_ONLY_CARGO, "cargo-audit"), FAKE_CLI, "cargo-audit")
t0 = time.monotonic()
code, data, err = run_json(["audit", "--path", rust_proj, "--timeout", "0.4"], make_env(BIN_ONLY_CARGO, sleep=5))
elapsed = time.monotonic() - t0
aud_to = (data.get("ecosystems") or [{}])[0].get("audit", {})
check("timeout kills child process fast, sets status=timeout, exits 2",
      code == 2 and aud_to.get("status") == "timeout" and "didn't finish" in aud_to.get("message", "")
      and elapsed < 4.0, (code, elapsed, data, err))

# ── 9. Direct unit checks for yarn/bun/ruby/php parsers ──
yarn_ndjson = '\n'.join([
    json.dumps({"type": "auditAdvisory", "data": {"advisory": {
        "module_name": "minimist", "severity": "moderate", "github_advisory_id": "GHSA-xvch-5gv4-984h",
        "title": "Prototype Pollution", "patched_versions": ">=1.2.6", "findings": [{"version": "1.2.5"}]
    }}}),
    json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {"moderate": 1}}}),
])
check("parse_npm_family handles yarn classic NDJSON",
      len(deptool.parse_npm_family(yarn_ndjson) or []) == 1)
check("parse_bun_outdated parses box table",
      [p["package"] for p in (deptool.parse_bun_outdated(
          "| Package | Current | Update | Latest |\n|---|---|---|---|\n| zod (dev) | 3.22.0 | 3.22.4 | 4.0.0 |\n"
      ) or [])] == ["zod"])
check("parse_bundle_audit & parse_composer_audit parse advisories",
      len(deptool.parse_bundle_audit(json.dumps({
          "results": [{"type": "unpatched_gem", "gem": {"name": "rack", "version": "2.2.0"},
                       "advisory": {"cve": "2024-25126", "criticality": "medium", "patched_versions": [">=2.2.8.1"]}}]
      })) or []) == 1
      and len(deptool.parse_composer_audit(json.dumps({
          "advisories": {"symfony/http-kernel": [{"advisoryId": "PKSA-1", "cve": "CVE-2024-1", "severity": "high"}]}
      })) or []) == 1)

# ── 10. Untrusted repo guards: cwd sys.path hijacking, tracked .venv, unsafe .yarnrc* ──
poison_py = os.path.join(SP, "poison_py")
write(poison_py, "pyproject.toml", '[project]\nname = "poison"\nversion = "0.1.0"\n')
write(poison_py, "json.py", "raise RuntimeError('poisonous json.py imported')\n")
write(poison_py, "pip.py", "raise RuntimeError('poisonous pip.py imported')\n")
write(poison_py, ".venv/pyvenv.cfg", "home = /usr/bin\n")
venv_bin = os.path.join(poison_py, ".venv", "Scripts" if WIN else "bin", "python.cmd" if WIN else "python3")
os.makedirs(os.path.dirname(venv_bin), exist_ok=True)
if WIN:
    with open(venv_bin, "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\n"{sys.executable}" %*\r\n')
else:
    with open(venv_bin, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\nexec {json.dumps(sys.executable)} \"$@\"\n")
    os.chmod(venv_bin, 0o755)
code, data, err = run_json(["audit", "--path", poison_py], make_env(EMPTY_BIN))
aud_py = (data.get("ecosystems") or [{}])[0].get("audit", {})
check("audit_python isolates cwd and sys.path from repo json.py",
      aud_py.get("tool") == "pip-audit" and "poisonous" not in aud_py.get("message", "")
      and aud_py.get("status") != "failed", (code, aud_py, err))
code, data, err = run_json(["outdated", "--path", poison_py], make_env(EMPTY_BIN))
out_py = (data.get("ecosystems") or [{}])[0].get("outdated", {})
check("outdated_python runs pip from temp cwd instead of repo pip.py",
      "poisonous" not in out_py.get("message", ""), (code, out_py, err))

tracked_py = os.path.join(SP, "tracked_py")
write(tracked_py, "pyproject.toml", '[project]\nname = "tracked"\nversion = "0.1.0"\n')
write(tracked_py, ".venv/pyvenv.cfg", "home = /usr/bin\n")
tracked_bin = os.path.join(tracked_py, ".venv", "Scripts" if WIN else "bin", "python.cmd" if WIN else "python3")
make_exec(tracked_bin, FAKE_CLI, "venv-python")
subprocess.run(["git", "init", "-q"], cwd=tracked_py, check=True)
subprocess.run(["git", "add", "-f", "pyproject.toml", ".venv"], cwd=tracked_py, check=True)
subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "init"],
               cwd=tracked_py, check=True)
code, data, err = run_json(["scan", "--path", tracked_py], make_env(EMPTY_BIN))
tr_entry = (data.get("ecosystems") or [{}])[0]
check("find_venv skips .venv committed to git", not tr_entry.get("venv"), (code, tr_entry, err))

unsafe_yarn = os.path.join(SP, "unsafe_yarn")
yarn_ran = os.path.join(SP, "yarn_ran.txt")
write(unsafe_yarn, "package.json", '{"name": "y"}\n')
write(unsafe_yarn, "yarn.lock", "# yarn lockfile v1\n")
write(unsafe_yarn, ".yarnrc.yml", "yarnPath: ./.yarn/releases/custom.cjs\n")
BIN_YARN = os.path.join(SP, "bin_yarn")
make_exec(os.path.join(BIN_YARN, "yarn"),
          f"import os, sys\nopen({yarn_ran!r}, 'w').write('ran')\nsys.exit(1)\n", "yarn")
code, data, err = run_json(["all", "--path", unsafe_yarn], make_env(BIN_YARN))
y_entry = (data.get("ecosystems") or [{}])[0]
check("audit_js skips executing yarn when .yarnrc.yml sets yarnPath",
      not os.path.exists(yarn_ran) and y_entry["audit"]["status"] == "failed"
      and "yarnPath" in y_entry["audit"]["message"], (code, y_entry, err))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
