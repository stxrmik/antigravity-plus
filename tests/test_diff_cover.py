"""/diff-cover: Cobertura XML, LCOV, Go coverprofile, .xcresult, diff modes, --min, --json, exit 2."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

PLUGIN, SP = sys.argv[1], sys.argv[2]
DIFF_COVER = os.path.join(PLUGIN, "skills", "diff-cover", "scripts", "diff_cover.py")
WIN = os.name == "nt"
MAC = sys.platform == "darwin"

ok = True


def check(name: str, cond: object, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1200]}" if not cond and detail != "" else ""))


def write(root: str, rel: str, content: str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return path


def git(args: List[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def init_repo(path: str, branch: str = "main") -> None:
    os.makedirs(path, exist_ok=True)
    git(["init", "-q", "-b", branch], path)


def run_dc(args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, DIFF_COVER] + list(args),
        cwd=cwd or SP,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return r.returncode, r.stdout, r.stderr


def run_dc_json(args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], str]:
    code, out, err = run_dc(list(args) + ["--json"], cwd=cwd, env=env)
    try:
        data = json.loads(out)
        return code, (data if isinstance(data, dict) else {"_raw": data}), err
    except ValueError:
        return code, {"_invalid_json": out}, err


# ── 1. Cobertura XML: <source> normalization, comments/docstrings excluded, contiguous ranges, not_in_coverage ──
repo_cob = os.path.join(SP, "repo_cob")
init_repo(repo_cob)
write(
    repo_cob,
    "src/calc.py",
    "\n".join(
        [
            "def add(a, b):",
            "    return a + b",
            "",
            "def legacy():",
            "    return 1",
            "",
        ]
    ),
)
write(
    repo_cob,
    "src/notes.py",
    "\n".join(
        [
            "x = 1",
            "y = 2",
            "z = 3",
            "",
        ]
    ),
)
git(["add", "."], repo_cob)
git(["commit", "-qm", "base"], repo_cob)

# Modify src/calc.py: add comment, docstring, covered lines, and two uncovered ranges (13-15, 18)
# Plus deletion-only edit in src/notes.py and an untracked file src/untracked_mod.py
write(
    repo_cob,
    "src/calc.py",
    "\n".join(
        [
            "def add(a, b):",  # 1
            "    return a + b",  # 2
            "",  # 3
            "def legacy():",  # 4
            "    return 1",  # 5
            "",  # 6
            "# New discount helper",  # 7 (comment - NOT instrumented)
            "def discount(price, pct):",  # 8 (instrumented, hit=1)
            '    """Compute discounted price."""',  # 9 (docstring - NOT instrumented)
            "",  # 10 (blank - NOT instrumented)
            "    if pct <= 0:",  # 11 (instrumented, hit=1)
            "        return price",  # 12 (instrumented, hit=1)
            "    if pct > 100:",  # 13 (instrumented, hit=0)
            "        msg = 'too high'",  # 14 (instrumented, hit=0)
            "        raise ValueError(msg)",  # 15 (instrumented, hit=0)
            "    rate = pct / 100.0",  # 16 (instrumented, hit=1)
            "    if price < 0:",  # 17 (instrumented, hit=1 -> wait make line 18 uncovered)
            "        return 0",  # 18 (instrumented, hit=0)
            "    return price * (1.0 - rate)",  # 19 (instrumented, hit=1)
            "",
        ]
    ),
)
# 6 covered (8, 11, 12, 16, 17, 19) + 4 uncovered (13, 14, 15, 18) = 10 executable changed lines -> 60.0%
# (--min 80 / --min 95 are checked on the 90% LCOV case below)

write(repo_cob, "src/notes.py", "x = 1\n")  # deletion-only change (deleted lines 2-3)
write(repo_cob, "src/untracked_mod.py", "def helper():\n    return 42\n")

# Cobertura XML placed in reports/coverage.xml with <source> pointing to <repo_cob>/src
write(
    repo_cob,
    "reports/coverage.xml",
    f"""<?xml version="1.0" ?>
<coverage version="7.5.0" line-rate="0.6">
  <sources>
    <source>{os.path.join(repo_cob, "src")}</source>
  </sources>
  <packages>
    <package name="src">
      <classes>
        <class name="calc.py" filename="calc.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
            <line number="4" hits="0"/>
            <line number="5" hits="0"/>
            <line number="8" hits="1"/>
            <line number="11" hits="1"/>
            <line number="12" hits="1"/>
            <line number="13" hits="0"/>
            <line number="14" hits="0"/>
            <line number="15" hits="0"/>
            <line number="16" hits="1"/>
            <line number="17" hits="1"/>
            <line number="18" hits="0"/>
            <line number="19" hits="1"/>
          </lines>
        </class>
        <class name="notes.py" filename="notes.py">
          <lines>
            <line number="1" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
)

code, data, err = run_dc_json(["--workspace", repo_cob])
calc_entry = next((f for f in data.get("files", []) if f["path"] == "src/calc.py"), {})
check(
    "cobertura XML: auto-discovered reports/coverage.xml, normalized via <source>, comments excluded",
    code == 0
    and data.get("coverage_format") == "cobertura"
    and data.get("coverage_file") == "reports/coverage.xml"
    and data.get("base") == "HEAD"
    and data.get("covered_lines") == 6
    and data.get("total_executable_changed_lines") == 10
    and data.get("percent") == 60.0
    and calc_entry.get("executable_changed_lines") == [8, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    and calc_entry.get("uncovered_lines") == [13, 14, 15, 18],
    (code, data, err),
)
check(
    "cobertura XML: contiguous uncovered ranges grouped with 1-line previews",
    calc_entry.get("uncovered_summary") == "13-15, 18"
    and len(calc_entry.get("uncovered_ranges", [])) == 2
    and calc_entry["uncovered_ranges"][0]["lines"] == "13-15"
    and "pct > 100" in calc_entry["uncovered_ranges"][0]["preview"]
    and calc_entry["uncovered_ranges"][1]["lines"] == "18"
    and "return 0" in calc_entry["uncovered_ranges"][1]["preview"],
    calc_entry,
)
check(
    "deletion-only file skipped and untracked file reported in not_in_coverage",
    data.get("not_in_coverage") == ["src/untracked_mod.py"]
    and all(f["path"] != "src/notes.py" for f in data.get("files", [])),
    data,
)

# Also verify CI foreign <source> prefix falls back cleanly via suffix matching
write(
    repo_cob,
    "reports/ci_coverage.xml",
    """<?xml version="1.0" ?>
<coverage version="7.5.0">
  <sources>
    <source>/runner/build/project/src</source>
  </sources>
  <packages>
    <package name="src">
      <classes>
        <class name="calc.py" filename="calc.py">
          <lines>
            <line number="8" hits="1"/>
            <line number="11" hits="1"/>
            <line number="12" hits="1"/>
            <line number="13" hits="0"/>
            <line number="14" hits="0"/>
            <line number="15" hits="0"/>
            <line number="16" hits="1"/>
            <line number="17" hits="1"/>
            <line number="18" hits="0"/>
            <line number="19" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
)
code_ci, data_ci, err_ci = run_dc_json(["--workspace", repo_cob, "--coverage", "reports/ci_coverage.xml"])
check(
    "cobertura XML: foreign CI <source> prefix normalized via workspace suffix matching",
    code_ci == 0 and data_ci.get("covered_lines") == 6 and data_ci.get("total_executable_changed_lines") == 10,
    (code_ci, data_ci, err_ci),
)

# ── 2. LCOV + auto-discovery picks newest file + feature branch merge-base + --min 80 vs --min 95 ──
repo_lcov = os.path.join(SP, "repo_lcov")
init_repo(repo_lcov, branch="main")
write(repo_lcov, "lib/app.js", "export function base() {\n  return 1;\n}\n")
write(repo_lcov, "lib/other.js", "export const x = 1;\n")
git(["add", "."], repo_lcov)
git(["commit", "-qm", "initial"], repo_lcov)

git(["checkout", "-qb", "feature/math"], repo_lcov)
# Add 10 executable lines on feature branch (9 covered, 1 uncovered -> 90.0%)
write(
    repo_lcov,
    "lib/app.js",
    "\n".join(
        [
            "export function base() {",  # 1
            "  return 1;",  # 2
            "}",  # 3
            "",  # 4
            "export function compute(n) {",  # 5
            "  // leading comment",  # 6 (not in DA)
            "  let acc = 0;",  # 7 (hit)
            "  if (n < 0) {",  # 8 (hit)
            "    return -1;",  # 9 (uncovered: 0)
            "  }",  # 10
            "  acc += 1;",  # 11 (hit)
            "  acc += 2;",  # 12 (hit)
            "  acc += 3;",  # 13 (hit)
            "  acc += 4;",  # 14 (hit)
            "  acc += 5;",  # 15 (hit)
            "  acc += 6;",  # 16 (hit)
            "  return acc;",  # 17 (hit)
            "}",  # 18
            "",
        ]
    ),
)
git(["add", "lib/app.js"], repo_lcov)
git(["commit", "-qm", "feat: compute"], repo_lcov)

# Advance main independently so diff must use merge-base, NOT main's tip
git(["checkout", "-q", "main"], repo_lcov)
write(repo_lcov, "lib/other.js", "export const x = 99;\nexport const y = 100;\n")
git(["commit", "-qam", "main moves ahead"], repo_lcov)
git(["checkout", "-q", "feature/math"], repo_lcov)

# Create stale coverage.xml and newer coverage/lcov.info
stale_cov = write(repo_lcov, "coverage.xml", "<coverage><packages/></coverage>\n")
old_ts = time.time() - 120
os.utime(stale_cov, (old_ts, old_ts))

write(
    repo_lcov,
    "coverage/lcov.info",
    "\n".join(
        [
            "TN:vitest",
            f"SF:{os.path.join(repo_lcov, 'lib', 'app.js')}",
            "DA:2,1",
            "DA:7,3",
            "DA:8,3",
            "DA:9,0",
            "DA:11,3,deadbeef",
            "DA:12,3",
            "DA:13,3",
            "DA:14,3",
            "DA:15,3",
            "DA:16,3",
            "DA:17,3",
            "end_of_record",
            "",
        ]
    ),
)

code, data, err = run_dc_json(["--workspace", repo_lcov, "--min", "80"])
check(
    "LCOV: picks newest coverage/lcov.info, uses main merge-base on clean feature branch, --min 80 passes",
    code == 0
    and data.get("coverage_format") == "lcov"
    and "main (merge-base" in data.get("base", "")
    and data.get("covered_lines") == 9
    and data.get("total_executable_changed_lines") == 10
    and data.get("percent") == 90.0
    and data.get("passed") is True
    and data.get("not_in_coverage") == []
    and len(data.get("files", [])) == 1
    and data["files"][0]["uncovered_lines"] == [9],
    (code, data, err),
)

code95, txt95, err95 = run_dc(["--workspace", repo_lcov, "--min", "95"])
check(
    "--min 95 returns exit 1 when diff coverage is 90.0%",
    code95 == 1 and "90.0%" in txt95 and "FAILED:" in txt95 and "L9:" in txt95,
    (code95, txt95, err95),
)

# ── 3. Go coverprofile: partial line spans, hit=0 vs hit>0, go.mod module path normalization ──
repo_go = os.path.join(SP, "repo_go")
init_repo(repo_go)
write(repo_go, "go.mod", "module example.com/myservice\n\ngo 1.22\n")
write(
    repo_go,
    "pkg/math.go",
    "\n".join(
        [
            "package pkg",
            "",
            "func Add(a, b int) int {",
            "    return a + b",
            "}",
            "",
        ]
    ),
)
git(["add", "."], repo_go)
git(["commit", "-qm", "base go"], repo_go)

write(
    repo_go,
    "pkg/math.go",
    "\n".join(
        [
            "package pkg",  # 1
            "",  # 2
            "func Add(a, b int) int {",  # 3
            "    return a + b",  # 4
            "}",  # 5
            "",  # 6
            "func Clamp(x int) int {",  # 7
            "    if x < 0 {",  # 8
            "        return 0",  # 9
            "    } else {",  # 10
            "        return x",  # 11
            "    }",  # 12
            "}",  # 13
            "",
        ]
    ),
)
git(["commit", "-qam", "add Clamp"], repo_go)

# Line 10 (`} else {`) has two partial spans: end of uncovered if-body (count=0) and start of covered else-body (count=1)
write(
    repo_go,
    "coverage.out",
    "\n".join(
        [
            "mode: count",
            "example.com/myservice/pkg/math.go:3.24,5.2 1 2",
            "example.com/myservice/pkg/math.go:7.24,8.12 1 1",
            "example.com/myservice/pkg/math.go:8.12,10.3 1 0",
            "example.com/myservice/pkg/math.go:10.10,12.3 1 1",
            "",
        ]
    ),
)

# Clean repo on main -> falls back to HEAD~1 automatically
code_go, data_go, err_go = run_dc_json(["--workspace", repo_go])
go_file = (data_go.get("files") or [{}])[0]
check(
    "Go coverage.out: clean main falls back to HEAD~1, go.mod normalized, partial spans (line 10 max=1, line 9=0)",
    code_go == 0
    and data_go.get("coverage_format") == "gocover"
    and data_go.get("base") == "HEAD~1"
    and go_file.get("path") == "pkg/math.go"
    and go_file.get("uncovered_lines") == [9]
    and data_go.get("covered_lines") == 5
    and data_go.get("total_executable_changed_lines") == 6,
    (code_go, data_go, err_go),
)

# ── 4. Diff edge case: added code lines containing `+++ b/...` or `diff --git` don't confuse parser ──
repo_edge = os.path.join(SP, "repo_edge")
init_repo(repo_edge)
write(repo_edge, "app.py", "def f():\n    return ''\n")
git(["add", "."], repo_edge)
git(["commit", "-qm", "base"], repo_edge)
write(
    repo_edge,
    "app.py",
    "\n".join(
        [
            "def f():",
            '    s = "++ b/spoof"',  # 2: in unified diff this renders as `+++ b/spoof`!
            '    d = "diff --git a/x b/x"',  # 3
            "    return s + d",  # 4
            "",
        ]
    ),
)
write(
    repo_edge,
    "lcov.info",
    "SF:app.py\nDA:2,1\nDA:3,0\nDA:4,1\nend_of_record\n",
)
code_ed, data_ed, err_ed = run_dc_json(["--workspace", repo_edge, "--base", "HEAD"])
check(
    "diff parser ignores `+++ b/...` inside added hunk body",
    code_ed == 0
    and data_ed.get("covered_lines") == 2
    and data_ed.get("total_executable_changed_lines") == 3
    and (data_ed.get("files") or [{}])[0].get("uncovered_lines") == [3],
    (code_ed, data_ed, err_ed),
)

# ── 5. Exit 2 cases: missing coverage report (text + --json), not a git repo, invalid --base ──
repo_nocov = os.path.join(SP, "repo_nocov")
init_repo(repo_nocov)
write(repo_nocov, "a.py", "x = 1\n")
git(["add", "."], repo_nocov)
git(["commit", "-qm", "init"], repo_nocov)

code_nc, out_nc, err_nc = run_dc(["--workspace", repo_nocov])
combined_nc = out_nc + err_nc
check(
    "missing coverage report returns exit 2 with generation instructions",
    code_nc == 2
    and "coverage.xml" in combined_nc
    and "lcov.info" in combined_nc
    and "coverage.out" in combined_nc,
    (code_nc, out_nc, err_nc),
)

code_ncj, data_ncj, err_ncj = run_dc_json(["--workspace", repo_nocov])
check(
    "missing coverage report with --json returns exit 2 and valid JSON error",
    code_ncj == 2 and "error" in data_ncj and "coverage.xml" in data_ncj.get("hint", ""),
    (code_ncj, data_ncj, err_ncj),
)

non_git = os.path.join(SP, "non_git")
os.makedirs(non_git, exist_ok=True)
code_ng, out_ng, err_ng = run_dc(["--workspace", non_git])
check(
    "non-git workspace returns exit 2 cleanly",
    code_ng == 2 and "Not a git repository" in (out_ng + err_ng),
    (code_ng, out_ng, err_ng),
)

code_ref, out_ref, err_ref = run_dc(["--workspace", repo_edge, "--base", "no-such-branch"])
check(
    "unknown --base ref returns exit 2 cleanly",
    code_ref == 2 and "Unknown git ref" in (out_ref + err_ref),
    (code_ref, out_ref, err_ref),
)

# ── 6. Path edge cases: spaces (git's trailing TAB), C-quoted names, dot-directories, untracked .coverage ──
repo_paths = os.path.join(SP, "repo paths")
init_repo(repo_paths)
two_funcs = "def a():\n    return 1\n\ndef b():\n    return 2\n"  # adds lines 3-5; 4 and 5 are executable
quoted_name = None if WIN else 'odd "name".py'  # a quote makes git C-quote the path (not a valid name on Windows)
edited = ["my pkg/mod.py", ".ci/tool.py"] + ([quoted_name] if quoted_name else [])
for name in edited:
    write(repo_paths, name, "def a():\n    return 1\n")
git(["add", "."], repo_paths)
git(["commit", "-qm", "base"], repo_paths)
git(["checkout", "-qb", "feature/paths"], repo_paths)
for name in edited:
    write(repo_paths, name, two_funcs)
write(repo_paths, ".ci/new_tool.py", "def c():\n    return 3\n")  # untracked file in a dot-directory
cov_lines = '<line number="1" hits="1"/><line number="2" hits="1"/><line number="4" hits="1"/><line number="5" hits="0"/>'
classes = "".join(
    f'<class name="m" filename="{name.replace(chr(34), "&quot;")}"><lines>{cov_lines}</lines></class>' for name in edited
)
classes += '<class name="n" filename=".ci/new_tool.py"><lines><line number="1" hits="1"/><line number="2" hits="0"/></lines></class>'
write(
    repo_paths,
    "coverage.xml",
    f'<?xml version="1.0" ?>\n<coverage version="7.5.0"><sources><source>{repo_paths}</source></sources>'
    f'<packages><package name="p"><classes>{classes}</classes></package></packages></coverage>\n',
)
expected_p = sorted(edited + [".ci/new_tool.py"])
total_p = 2 * len(edited) + 2
code_p, data_p, err_p = run_dc_json(["--workspace", repo_paths])
check(
    "paths with spaces, C-quoted names and dot-directories (edited and untracked) all match the report",
    code_p == 0
    and data_p.get("base") == "HEAD"
    and sorted(f["path"] for f in data_p.get("files", [])) == expected_p
    and data_p.get("total_executable_changed_lines") == total_p
    and data_p.get("not_in_coverage") == []
    and data_p.get("warning") is None,
    (code_p, data_p, err_p),
)
# Commit the work; pytest-cov's untracked .coverage (and friends) must not turn the base into HEAD and hide it
git(["add", "--"] + expected_p, repo_paths)
git(["commit", "-qm", "feature"], repo_paths)
write(repo_paths, ".coverage", "SQLite format 3\n")
write(repo_paths, ".coverage.host.123.456", "SQLite format 3\n")
write(repo_paths, "htmlcov/index.html", "<html></html>\n")
code_c, data_c, err_c = run_dc_json(["--workspace", repo_paths, "--min", "10"])
check(
    "untracked .coverage / .coverage.* / htmlcov/ are report artifacts: a clean branch diffs against the main merge-base",
    code_c == 0
    and "main (merge-base" in data_c.get("base", "")
    and sorted(f["path"] for f in data_c.get("files", [])) == expected_p
    and data_c.get("total_executable_changed_lines") == total_p
    and data_c.get("not_in_coverage") == [],
    (code_c, data_c, err_c),
)

# ── 7. A report that matches none of the changed source files: no silent 100%, --min fails ──
repo_stale = os.path.join(SP, "repo_stale")
init_repo(repo_stale)
write(repo_stale, "app/core.py", "def a():\n    return 1\n")
write(repo_stale, "README.md", "# Demo\n")
git(["add", "."], repo_stale)
git(["commit", "-qm", "base"], repo_stale)
write(repo_stale, "app/core.py", two_funcs)
write(repo_stale, "README.md", "# Demo\n\nMore docs.\n")
write(repo_stale, "lcov.info", "SF:/elsewhere/checkout/lib/other.py\nDA:1,1\nDA:2,0\nend_of_record\n")
code_s, data_s, err_s = run_dc_json(["--workspace", repo_stale, "--min", "80"])
code_s_txt, out_s_txt, _ = run_dc(["--workspace", repo_stale, "--min", "80"])
code_s_nomin, data_s_nomin, _ = run_dc_json(["--workspace", repo_stale])
check(
    "no changed source file in the report: percent null + warning, --min fails (exit 1), text says why",
    code_s == 1
    and data_s.get("percent") is None
    and data_s.get("passed") is False
    and "None of the 1 changed source file(s)" in (data_s.get("warning") or "")
    and data_s.get("not_in_coverage") == ["README.md", "app/core.py"]
    and code_s_txt == 1
    and "n/a" in out_s_txt
    and "FAILED: no diff coverage could be computed" in out_s_txt
    and code_s_nomin == 0
    and data_s_nomin.get("warning"),
    (code_s, data_s, err_s, out_s_txt),
)
# A docs-only change is not a mismatch: nothing to cover, --min passes
git(["checkout", "-q", "--", "app/core.py"], repo_stale)
write(repo_stale, "lcov.info", "SF:app/core.py\nDA:1,1\nDA:2,1\nend_of_record\n")
code_d, data_d, err_d = run_dc_json(["--workspace", repo_stale, "--min", "80"])
check(
    "docs-only change with a matching report: no warning, --min passes",
    code_d == 0 and data_d.get("warning") is None and data_d.get("not_in_coverage") == ["README.md"],
    (code_d, data_d, err_d),
)

# ── 8. macOS Xcode .xcresult via mocked xcrun on PATH ──
if MAC:
    repo_xc = os.path.join(SP, "repo_xc")
    init_repo(repo_xc)
    write(repo_xc, "Sources/Greeter.swift", "func hi() -> String {\n    return \"hi\"\n}\n")
    git(["add", "."], repo_xc)
    git(["commit", "-qm", "init"], repo_xc)
    write(
        repo_xc,
        "Sources/Greeter.swift",
        "\n".join(
            [
                "func hi(loud: Bool) -> String {",
                "    if loud {",
                '        return "HI"',
                "    }",
                '    return "hello"',
                "}",
                "",
            ]
        ),
    )
    os.makedirs(os.path.join(repo_xc, "TestResults.xcresult"), exist_ok=True)
    fake_bin = os.path.join(SP, "fake_xcrun_bin")
    os.makedirs(fake_bin, exist_ok=True)
    swift_abs = os.path.join(repo_xc, "Sources", "Greeter.swift")
    fake_xcrun = os.path.join(fake_bin, "xcrun")
    write(
        fake_bin,
        "xcrun",
        f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
if args[:4] == ["xccov", "view", "--report", "--json"]:
    print(json.dumps({{"targets": [{{"name": "App", "files": [{{"path": {swift_abs!r}}}]}}]}}))
    sys.exit(0)
if args[:4] == ["xccov", "view", "--archive", "--file"]:
    print(json.dumps({{{swift_abs!r}: [
        {{"line": 1, "isExecutable": True, "executionCount": 1}},
        {{"line": 2, "isExecutable": True, "executionCount": 1}},
        {{"line": 3, "isExecutable": True, "executionCount": 0}},
        {{"line": 4, "isExecutable": False}},
        {{"line": 5, "isExecutable": True, "executionCount": 1}},
    ]}}))
    sys.exit(0)
sys.exit(2)
""",
    )
    os.chmod(fake_xcrun, 0o755)
    env_xc = dict(os.environ, PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""))
    code_xc, data_xc, err_xc = run_dc_json(["--workspace", repo_xc], env=env_xc)
    check(
        "macOS .xcresult auto-discovered and parsed via xcrun xccov",
        code_xc == 0
        and data_xc.get("coverage_format") == "xcresult"
        and data_xc.get("covered_lines") == 3
        and data_xc.get("total_executable_changed_lines") == 4
        and (data_xc.get("files") or [{}])[0].get("uncovered_lines") == [3],
        (code_xc, data_xc, err_xc),
    )

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
