"""/perf skill: statistics, noisy detection, interleaved ABBA compare, git worktree cleanup, size, and JSON output."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List

PLUGIN, SP = sys.argv[1], sys.argv[2]
PERF_PY = os.path.join(PLUGIN, "skills", "perf", "scripts", "perf.py")
WIN = os.name == "nt"

sys.path.insert(0, os.path.join(PLUGIN, "skills", "perf", "scripts"))
import perf as perftool  # type: ignore[import-not-found]  # noqa: E402

ok = True


def check(name: str, cond: object, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1000]}" if not cond and detail != "" else ""))


def run_cli(args: List[str], cwd: str = SP) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, PERF_PY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def run_json(args: List[str], cwd: str = SP) -> tuple:
    r = run_cli([*args, "--json"], cwd=cwd)
    try:
        data: Dict[str, Any] = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        data = {}
    return r.returncode, data, r.stdout, r.stderr


def qcmd(code: str) -> str:
    """Build a shell-safe Python one-liner command across POSIX and Windows."""
    return f'"{sys.executable}" -c "{code}"'


# ── 1. Unit tests: statistics, percentiles, noise flag, and compare_sides ──
s_clean = perftool.compute_stats([10.0, 20.0, 30.0, 40.0, 50.0])
check(
    "compute_stats computes exact min/p25/median/p75/iqr/mean/max",
    s_clean["min_ms"] == 10.0
    and s_clean["p25_ms"] == 20.0
    and s_clean["median_ms"] == 30.0
    and s_clean["p75_ms"] == 40.0
    and s_clean["iqr_ms"] == 20.0
    and s_clean["mean_ms"] == 30.0
    and s_clean["max_ms"] == 50.0
    and s_clean["stdev_ms"] > 0,
    s_clean,
)
check("compute_stats flags noisy when iqr/median > 0.25", s_clean["noisy"] is True, s_clean)

s_tight = perftool.compute_stats([100.0, 102.0, 104.0, 106.0, 108.0])
check("compute_stats does not flag noisy when iqr/median <= 0.25", s_tight["noisy"] is False, s_tight)

s_single = perftool.compute_stats([42.5])
check(
    "compute_stats handles single-sample input without StatisticsError",
    s_single["median_ms"] == 42.5 and s_single["iqr_ms"] == 0.0 and s_single["stdev_ms"] == 0.0,
    s_single,
)

cmp_reg = perftool.compare_sides(
    perftool.compute_stats([10.0 + i * 0.5 for i in range(10)]),
    perftool.compute_stats([20.0 + i for i in range(10)]),
)
check(
    "compare_sides reports clear regression when new IQR > base IQR",
    cmp_reg["verdict"] == "clear regression"
    and cmp_reg["iqr_overlap"] is False
    and cmp_reg["speedup_text"] == "2.00x slower"
    and cmp_reg["pct_change"] == 100.0,
    cmp_reg,
)

cmp_ovl = perftool.compare_sides(
    perftool.compute_stats([10.0 + i * 2.5 for i in range(10)]),
    perftool.compute_stats([18.0 + i * 2.0 for i in range(10)]),
)
check(
    "compare_sides reports inconclusive on IQR overlap",
    cmp_ovl["iqr_overlap"] is True and cmp_ovl["verdict"] == "inconclusive (IQR overlap / within noise)",
    cmp_ovl,
)

cmp_few = perftool.compare_sides(
    perftool.compute_stats([10.0, 11.0, 12.0, 13.0, 14.0]),
    perftool.compute_stats([20.0, 22.0, 24.0, 26.0, 28.0]),
)
check(
    "compare_sides gives no clear verdict below 10 runs per side, even without IQR overlap",
    cmp_few["iqr_overlap"] is False
    and cmp_few["verdict"] == "inconclusive (only 5 runs per side: a verdict needs at least 10)"
    and cmp_few["speedup_text"] == "2.00x slower",
    cmp_few,
)

# ── 2. CLI `run`: timings, RSS on POSIX / None on Windows, nonzero exit & --allow-fail ──
rc, data, out, err = run_json([
    "run", "--cmd", qcmd("import time; time.sleep(0.015)"), "--runs", "3", "--warmup", "1",
])
rss = data.get("rss_bytes")
check(
    "run --json records 3 runs, valid stats, and platform-appropriate RSS",
    rc == 0
    and data.get("ok") is True
    and len(data.get("times_ms", [])) == 3
    and data.get("exit_codes") == [0, 0, 0]
    and data.get("stats", {}).get("median_ms", 0) >= 10.0
    and ((rss is None) if WIN else (isinstance(rss, dict) and rss.get("max", 0) > 0)),
    (rc, data, err),
)

rc_fail, data_fail, _, err_fail = run_json([
    "run", "--cmd", qcmd("import sys; sys.exit(7)"), "--runs", "2", "--warmup", "0",
])
check(
    "run fails cleanly with exit 1 on non-zero command exit",
    rc_fail == 1 and data_fail.get("ok") is False and "7" in data_fail.get("error", ""),
    (rc_fail, data_fail, err_fail),
)

rc_allow, data_allow, _, err_allow = run_json([
    "run", "--cmd", qcmd("import sys; sys.exit(7)"), "--runs", "2", "--warmup", "1", "--allow-fail",
])
check(
    "run --allow-fail succeeds and records exit codes",
    rc_allow == 0 and data_allow.get("exit_codes") == [7, 7] and data_allow.get("failed_runs") == 2,
    (rc_allow, data_allow, err_allow),
)

# ── 3. CLI `compare`: faster vs slower + ABBA interleaving + identical/noisy overlap ──
rc_cmp, data_cmp, _, err_cmp = run_json([
    "compare",
    "--base-cmd", qcmd("import time; time.sleep(0.085)"),
    "--new-cmd", qcmd("import time; time.sleep(0.002)"),
    "--runs", "10",
    "--warmup", "1",
])
cmp_block = data_cmp.get("comparison", {})
check(
    "compare distinguishes slower base vs faster new with ABBA schedule",
    rc_cmp == 0
    and data_cmp.get("schedule") == ["A", "B", "B", "A"] * 5
    and cmp_block.get("verdict") == "clear improvement"
    and cmp_block.get("iqr_overlap") is False
    and "faster" in cmp_block.get("speedup_text", "")
    and cmp_block.get("pct_change", 0) < -30.0,
    (rc_cmp, data_cmp, err_cmp),
)

# Identical noisy script: alternates 5ms / 35ms across consecutive invocations.
# ABBA interleaving balances periodic noise across both identical sides so IQRs overlap & noisy=True.
noisy_script = os.path.join(SP, "noisy_toggle.py")
toggle_state = os.path.join(SP, "noisy_toggle.txt")
with open(noisy_script, "w", encoding="utf-8") as fh:
    fh.write(
        "import os, sys, time\n"
        f"p = {toggle_state!r}\n"
        "n = int(open(p, encoding='utf-8').read()) if os.path.exists(p) else 0\n"
        "with open(p, 'w', encoding='utf-8') as f:\n"
        "    f.write(str(n + 1))\n"
        "time.sleep(0.035 if n % 2 else 0.004)\n"
    )
same_cmd = f'"{sys.executable}" "{noisy_script}"'
rc_same, data_same, _, err_same = run_json([
    "compare", "--base-cmd", same_cmd, "--new-cmd", same_cmd, "--runs", "10", "--warmup", "0",
])
cmp_same = data_same.get("comparison", {})
check(
    "compare detects noisy runs and IQR overlap on identical commands",
    rc_same == 0
    and cmp_same.get("iqr_overlap") is True
    and cmp_same.get("verdict") == "inconclusive (IQR overlap / within noise)"
    and data_same.get("base", {}).get("stats", {}).get("noisy") is True
    and data_same.get("new", {}).get("stats", {}).get("noisy") is True,
    (rc_same, data_same, err_same),
)

# ── 3b. The Windows runner (also POSIX without os.wait4): no pipes, so nothing it started can hold it past --timeout ──
started = time.perf_counter()
res_to = perftool._run_once_subprocess(qcmd("import time; time.sleep(5)"), SP, 0.5)
took_to = time.perf_counter() - started
check(
    "fallback runner stops a command at --timeout",
    res_to["timed_out"] is True and res_to["exit_code"] != 0 and took_to < 4.0,
    (res_to, took_to),
)
# The command exits at once but leaves a process behind that still holds its stdout/stderr (with pipes, waiting
# for their end would last as long as that process does).
leave_behind = qcmd("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])")
started = time.perf_counter()
res_bg = perftool._run_once_subprocess(leave_behind, SP, 20.0)
took_bg = time.perf_counter() - started
check(
    "fallback runner returns when the command exits, not when what it started closes the output",
    res_bg["timed_out"] is False and res_bg["exit_code"] == 0 and took_bg < 2.0,
    (res_bg, took_bg),
)

# ── 4. CLI `git`: real temp git repo (HEAD 80ms vs working tree 5ms) + worktree cleanup ──
git_repo = os.path.join(SP, "perf_git_repo")
os.makedirs(os.path.join(git_repo, "sub"), exist_ok=True)
work_py = os.path.join(git_repo, "sub", "work.py")
with open(work_py, "w", encoding="utf-8") as fh:
    fh.write("import time\ntime.sleep(0.080)\n")
subprocess.run(["git", "init", "-q"], cwd=git_repo, check=True)
subprocess.run(["git", "add", "sub/work.py"], cwd=git_repo, check=True)
subprocess.run(
    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "slow baseline"],
    cwd=git_repo,
    check=True,
)
with open(work_py, "w", encoding="utf-8") as fh:
    fh.write("import time\ntime.sleep(0.005)\n")

rc_git, data_git, _, err_git = run_json([
    "git",
    "--cmd", f'"{sys.executable}" sub/work.py',
    "--ref", "HEAD",
    "--warmup", "1",
    "--workspace", git_repo,
])
wt_list = subprocess.run(
    ["git", "worktree", "list", "--porcelain"],
    cwd=git_repo,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    check=True,
).stdout
wt_count = sum(1 for ln in wt_list.splitlines() if ln.startswith("worktree "))
wt_path = data_git.get("worktree_path", "")
check(
    "git compares HEAD (80ms) vs working tree (5ms) with the default 10 runs per side and detects speedup",
    rc_git == 0
    and data_git.get("runs") == 10
    and data_git.get("comparison", {}).get("verdict") == "clear improvement"
    and "faster" in data_git.get("comparison", {}).get("speedup_text", "")
    and data_git.get("base", {}).get("stats", {}).get("median_ms", 0)
    > data_git.get("new", {}).get("stats", {}).get("median_ms", 0),
    (rc_git, data_git, err_git),
)
check(
    "git removes temporary worktree completely after run",
    bool(wt_path)
    and not os.path.exists(wt_path)
    and not os.path.exists(os.path.dirname(wt_path))
    and wt_count == 1,
    (wt_path, wt_count, wt_list),
)

# Verify subdirectory workspace + worktree cleanup even when benchmark command fails
rc_git_sub, data_git_sub, _, err_git_sub = run_json([
    "git",
    "--cmd", f'"{sys.executable}" work.py',
    "--ref", "HEAD",
    "--runs", "10",
    "--warmup", "1",
    "--workspace", os.path.join(git_repo, "sub"),
])
check(
    "git works from repo subdirectory",
    rc_git_sub == 0 and data_git_sub.get("comparison", {}).get("verdict") == "clear improvement",
    (rc_git_sub, data_git_sub, err_git_sub),
)

rc_git_fail, _, _, _ = run_json([
    "git",
    "--cmd", qcmd("import sys; sys.exit(3)"),
    "--workspace", git_repo,
    "--runs", "2",
    "--warmup", "0",
])
wt_after_fail = subprocess.run(
    ["git", "worktree", "list", "--porcelain"],
    cwd=git_repo,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    check=True,
).stdout
check(
    "git removes temporary worktree even when benchmark fails",
    rc_git_fail == 1 and sum(1 for ln in wt_after_fail.splitlines() if ln.startswith("worktree ")) == 1,
    (rc_git_fail, wt_after_fail),
)

# ── 5. CLI `size`: raw + gzip -9 for file and directory, text + --json ──
bundle_dir = os.path.join(SP, "bundle")
nested_dir = os.path.join(bundle_dir, "nested")
os.makedirs(nested_dir, exist_ok=True)
blob_a = b"console.log('bundle');\n" * 120
blob_b = b"body { margin: 0; padding: 0; }\n" * 80
file_a = os.path.join(bundle_dir, "app.js")
file_b = os.path.join(nested_dir, "style.css")
with open(file_a, "wb") as fh:
    fh.write(blob_a)
with open(file_b, "wb") as fh:
    fh.write(blob_b)

expected_gz_a = len(gzip.compress(blob_a, compresslevel=9))
expected_gz_b = len(gzip.compress(blob_b, compresslevel=9))

rc_sz, data_sz, _, err_sz = run_json(["size", file_a, bundle_dir])
entries_sz = data_sz.get("entries", [])
check(
    "size --json reports exact raw and gzip-9 bytes for file and directory",
    rc_sz == 0
    and len(entries_sz) == 2
    and entries_sz[0]["raw_bytes"] == len(blob_a)
    and entries_sz[0]["gzip_bytes"] == expected_gz_a
    and entries_sz[1]["files"] == 2
    and entries_sz[1]["raw_bytes"] == len(blob_a) + len(blob_b)
    and entries_sz[1]["gzip_bytes"] == expected_gz_a + expected_gz_b,
    (rc_sz, data_sz, err_sz),
)

r_sz_txt = run_cli(["size", file_a])
check(
    "size text output includes human-readable KiB and raw/gzip byte counts",
    r_sz_txt.returncode == 0
    and "KiB" in r_sz_txt.stdout
    and f"{len(blob_a)} B" in r_sz_txt.stdout
    and f"{expected_gz_a} B" in r_sz_txt.stdout,
    (r_sz_txt.returncode, r_sz_txt.stdout, r_sz_txt.stderr),
)

rc_sz_missing, data_sz_missing, _, _ = run_json(["size", os.path.join(SP, "missing_file.bin")])
check("size exits 1 cleanly on missing path", rc_sz_missing == 1 and data_sz_missing.get("ok") is False, data_sz_missing)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
