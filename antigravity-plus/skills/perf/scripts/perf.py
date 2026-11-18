#!/usr/bin/env python3
"""Antigravity Plus — /perf: honest statistical before/after performance and artifact-size benchmarking.

  perf.py run     --cmd "<cmd>" [--runs 7] [--warmup 2] [--cwd DIR] [--timeout 60] [--allow-fail] [--json]
  perf.py compare --base-cmd "<cmd1>" --new-cmd "<cmd2>" [--runs 10] [--warmup 2] [--cwd DIR] [--timeout 60] [--allow-fail] [--json]
  perf.py git     --cmd "<cmd>" [--ref HEAD] [--runs 10] [--warmup 1] [--workspace DIR] [--timeout 60] [--allow-fail] [--json]
  perf.py size    PATH [PATH ...] [--json]

Measures wall-clock latency (min, p25, median, p75, IQR, mean, stdev, max), child max RSS on POSIX, interleaved
ABBA A/B comparisons with [p25, p75] overlap checks (a "clear" verdict needs at least 10 runs per side), git
worktree comparisons against a ref, and raw + gzip-9 size. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

WINDOWS = os.name == "nt"
# Below this many runs per side, two identical commands' [p25, p75] ranges miss each other too often (about 1 in 5
# at 5 runs) for a "clear" verdict to mean anything.
MIN_VERDICT_RUNS = 10
QUIET_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_EDITOR": "true",
    "LC_ALL": "C",
    "LANG": "C",
}


class PerfError(Exception):
    """Clean, user-facing benchmark or validation failure."""


def _rss_to_bytes(raw_rss: int) -> int:
    """Convert rusage.ru_maxrss to bytes (bytes on macOS, KiB on Linux/BSD)."""
    return int(raw_rss) if sys.platform == "darwin" else int(raw_rss) * 1024


def _read_tail(fh: Any, limit: int = 2048) -> str:
    """Read the last `limit` bytes from a binary temporary file as UTF-8."""
    try:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - limit), os.SEEK_SET)
        data = fh.read()
        return data.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _status_to_exitcode(status: int) -> int:
    """Python 3.8-compatible wait status decoder."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 1


def _run_once(cmd: str, cwd: str, timeout: float) -> Dict[str, Any]:
    """Execute `cmd` once via shell, capturing wall time (ms), exit code, and POSIX child max RSS."""
    if not WINDOWS and resource is not None and hasattr(os, "wait4"):
        return _run_once_wait4(cmd, cwd, timeout)
    return _run_once_subprocess(cmd, cwd, timeout)


def _run_once_wait4(cmd: str, cwd: str, timeout: float) -> Dict[str, Any]:
    """Run one shell command on POSIX using os.wait4 for accurate per-run child max RSS."""
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        timed_out = [False]
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
        )

        def _kill_group() -> None:
            timed_out[0] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass

        timer = threading.Timer(timeout, _kill_group)
        timer.daemon = True
        timer.start()
        try:
            _, status, ru = os.wait4(proc.pid, 0)
            t1 = time.perf_counter()
        except BaseException:
            timer.cancel()
            _kill_group()
            try:
                os.wait4(proc.pid, 0)
            except OSError:
                pass
            raise
        finally:
            timer.cancel()

        code = _status_to_exitcode(status)
        proc.returncode = code
        rss_bytes = _rss_to_bytes(ru.ru_maxrss) if ru.ru_maxrss > 0 else 0
        output_tail = ""
        if timed_out[0] or code != 0:
            output_tail = _read_tail(err_f) or _read_tail(out_f)
        return {
            "wall_ms": (t1 - t0) * 1000.0,
            "exit_code": code,
            "max_rss_bytes": rss_bytes,
            "timed_out": timed_out[0],
            "output_tail": output_tail,
        }


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    """Kill a timed-out command together with everything it started: `taskkill /T /F` on Windows (cmd.exe /c runs
    the real program as its child), the process group elsewhere."""
    if WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_once_subprocess(cmd: str, cwd: str, timeout: float) -> Dict[str, Any]:
    """Single run on Windows (or POSIX without os.wait4). Output goes to temporary files, not pipes: a pipe that a
    process started by the command keeps open would hold the wait for EOF past the timeout."""
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            start_new_session=not WINDOWS,
        )
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
            t1 = time.perf_counter()
        except subprocess.TimeoutExpired:
            t1 = time.perf_counter()
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            code = -1
        except BaseException:
            _kill_tree(proc)
            raise
        output_tail = (_read_tail(err_f) or _read_tail(out_f)) if (timed_out or code != 0) else ""

    rss_bytes: Optional[int] = None
    if not WINDOWS and resource is not None:
        raw_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss_bytes = _rss_to_bytes(raw_rss) if raw_rss > 0 else 0

    return {
        "wall_ms": (t1 - t0) * 1000.0,
        "exit_code": code,
        "max_rss_bytes": rss_bytes,
        "timed_out": timed_out,
        "output_tail": output_tail,
    }


def _check_run(res: Dict[str, Any], label: str, phase: str, allow_fail: bool, timeout: float) -> None:
    if res["timed_out"]:
        raise PerfError(f"{label} timed out after {timeout:.1f}s during {phase}.")
    if not allow_fail and res["exit_code"] != 0:
        detail = f": {res['output_tail']}" if res["output_tail"] else ""
        raise PerfError(f"{label} exited with code {res['exit_code']} during {phase}{detail}")


def percentile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear-interpolation quantile (R-7) compatible with Python 3.8+ including single-item inputs."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = p * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo]))


def compute_stats(times_ms: Sequence[float]) -> Dict[str, Any]:
    """Compute summary statistics and noise flag for wall-clock timings (ms)."""
    ordered = sorted(float(x) for x in times_ms)
    if not ordered:
        raise PerfError("No timed runs recorded.")
    p25 = percentile(ordered, 0.25)
    median = percentile(ordered, 0.50)
    p75 = percentile(ordered, 0.75)
    iqr = p75 - p25
    mean = statistics.fmean(ordered)
    stdev = statistics.stdev(ordered) if len(ordered) >= 2 else 0.0
    iqr_ratio = (iqr / median) if median > 0 else 0.0
    noisy = bool(median > 0 and iqr_ratio > 0.25)
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 4),
        "p25_ms": round(p25, 4),
        "median_ms": round(median, 4),
        "p75_ms": round(p75, 4),
        "iqr_ms": round(iqr, 4),
        "mean_ms": round(mean, 4),
        "stdev_ms": round(stdev, 4),
        "max_ms": round(ordered[-1], 4),
        "iqr_ratio": round(iqr_ratio, 4),
        "noisy": noisy,
    }


def summarize_rss(samples: Sequence[Optional[int]]) -> Optional[Dict[str, Any]]:
    """Summarize POSIX max RSS samples in bytes, or None when unavailable (Windows)."""
    vals = [int(v) for v in samples if v is not None]
    if not vals:
        return None
    ordered = sorted(vals)
    return {
        "max": max(ordered),
        "median": int(round(percentile([float(v) for v in ordered], 0.50))),
        "samples": vals,
    }


def fmt_ms(val: float) -> str:
    if val >= 1000.0:
        return f"{val:.1f} ms"
    if val >= 10.0:
        return f"{val:.2f} ms"
    return f"{val:.3f} ms"


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.2f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MiB"
    return f"{n / (1024 * 1024 * 1024):.2f} GiB"


def _validate_run_params(runs: int, warmup: int, timeout: float, cwd: str) -> str:
    if runs < 1:
        raise PerfError(f"--runs must be >= 1 (got {runs}).")
    if warmup < 0:
        raise PerfError(f"--warmup must be >= 0 (got {warmup}).")
    if timeout <= 0:
        raise PerfError(f"--timeout must be > 0 (got {timeout}).")
    resolved = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(resolved):
        raise PerfError(f"Directory does not exist: {resolved}")
    return resolved


def execute_run(
    cmd: str,
    runs: int,
    warmup: int,
    cwd: str,
    timeout: float,
    allow_fail: bool,
) -> Dict[str, Any]:
    """Run warmups + timed runs for a single command and return structured stats."""
    work_dir = _validate_run_params(runs, warmup, timeout, cwd)
    for w in range(warmup):
        res = _run_once(cmd, work_dir, timeout)
        _check_run(res, "Command", f"warmup {w + 1}/{warmup}", allow_fail, timeout)

    times_ms: List[float] = []
    exit_codes: List[int] = []
    rss_samples: List[Optional[int]] = []
    for i in range(runs):
        res = _run_once(cmd, work_dir, timeout)
        _check_run(res, "Command", f"run {i + 1}/{runs}", allow_fail, timeout)
        times_ms.append(round(res["wall_ms"], 4))
        exit_codes.append(res["exit_code"])
        rss_samples.append(res["max_rss_bytes"])

    return {
        "ok": True,
        "command": cmd,
        "cwd": work_dir,
        "runs": runs,
        "warmup": warmup,
        "times_ms": times_ms,
        "exit_codes": exit_codes,
        "failed_runs": sum(1 for c in exit_codes if c != 0),
        "stats": compute_stats(times_ms),
        "rss_bytes": summarize_rss(rss_samples),
    }


def _build_side_payload(
    label: str,
    cmd: str,
    cwd: str,
    times_ms: List[float],
    exit_codes: List[int],
    rss_samples: List[Optional[int]],
) -> Dict[str, Any]:
    return {
        "label": label,
        "command": cmd,
        "cwd": cwd,
        "times_ms": [round(t, 4) for t in times_ms],
        "exit_codes": exit_codes,
        "failed_runs": sum(1 for c in exit_codes if c != 0),
        "stats": compute_stats(times_ms),
        "rss_bytes": summarize_rss(rss_samples),
    }


def compare_sides(base_stats: Dict[str, Any], new_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Compare baseline vs new stats and report speedup, IQR overlap, and honest verdict."""
    base_med = base_stats["median_ms"]
    new_med = new_stats["median_ms"]
    pct_change = ((new_med - base_med) / base_med * 100.0) if base_med > 0 else 0.0

    ratio_base_over_new: Optional[float] = None
    if base_med > 0 and new_med > 0:
        ratio_base_over_new = round(base_med / new_med, 4)
        if new_med < base_med:
            speedup_text = f"{base_med / new_med:.2f}x faster"
        elif new_med > base_med:
            speedup_text = f"{new_med / base_med:.2f}x slower"
        else:
            speedup_text = "1.00x (same)"
    else:
        speedup_text = "n/a"

    iqr_overlap = max(base_stats["p25_ms"], new_stats["p25_ms"]) <= min(base_stats["p75_ms"], new_stats["p75_ms"])
    runs = min(int(base_stats.get("n") or 0), int(new_stats.get("n") or 0))
    if runs < MIN_VERDICT_RUNS:
        verdict = (f"inconclusive (only {runs} run{'s' if runs != 1 else ''} per side: "
                   f"a verdict needs at least {MIN_VERDICT_RUNS})")
    elif iqr_overlap:
        verdict = "inconclusive (IQR overlap / within noise)"
    elif new_med < base_med:
        verdict = "clear improvement"
    elif new_med > base_med:
        verdict = "clear regression"
    else:
        verdict = "inconclusive (IQR overlap / within noise)"

    return {
        "base_median_ms": base_med,
        "new_median_ms": new_med,
        "delta_ms": round(new_med - base_med, 4),
        "pct_change": round(pct_change, 2),
        "speedup_ratio": ratio_base_over_new,
        "speedup_text": speedup_text,
        "iqr_overlap": iqr_overlap,
        "verdict": verdict,
    }


def execute_ab(
    cmd_a: str,
    cwd_a: str,
    label_a: str,
    cmd_b: str,
    cwd_b: str,
    label_b: str,
    runs: int,
    warmup: int,
    timeout: float,
    allow_fail: bool,
) -> Dict[str, Any]:
    """Run interleaved A/B benchmark (A, B, B, A, A, B, ...) after warmups."""
    dir_a = _validate_run_params(runs, warmup, timeout, cwd_a)
    dir_b = _validate_run_params(runs, warmup, timeout, cwd_b)

    for w in range(warmup):
        seq = (("A", cmd_a, dir_a, label_a), ("B", cmd_b, dir_b, label_b))
        if w % 2 == 1:
            seq = (seq[1], seq[0])
        for _, c, d, lbl in seq:
            res = _run_once(c, d, timeout)
            _check_run(res, lbl, f"warmup {w + 1}/{warmup}", allow_fail, timeout)

    a_times: List[float] = []
    a_codes: List[int] = []
    a_rss: List[Optional[int]] = []
    b_times: List[float] = []
    b_codes: List[int] = []
    b_rss: List[Optional[int]] = []
    order: List[str] = []

    for i in range(runs):
        pair = (("A", cmd_a, dir_a, label_a), ("B", cmd_b, dir_b, label_b))
        if i % 2 == 1:
            pair = (pair[1], pair[0])
        for tag, c, d, lbl in pair:
            res = _run_once(c, d, timeout)
            _check_run(res, lbl, f"run {i + 1}/{runs}", allow_fail, timeout)
            order.append(tag)
            if tag == "A":
                a_times.append(res["wall_ms"])
                a_codes.append(res["exit_code"])
                a_rss.append(res["max_rss_bytes"])
            else:
                b_times.append(res["wall_ms"])
                b_codes.append(res["exit_code"])
                b_rss.append(res["max_rss_bytes"])

    base = _build_side_payload(label_a, cmd_a, dir_a, a_times, a_codes, a_rss)
    new = _build_side_payload(label_b, cmd_b, dir_b, b_times, b_codes, b_rss)
    comparison = compare_sides(base["stats"], new["stats"])
    return {
        "ok": True,
        "runs": runs,
        "warmup": warmup,
        "schedule": order,
        "base": base,
        "new": new,
        "comparison": comparison,
    }


def _git_run(args: Sequence[str], cwd: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=dict(os.environ, **QUIET_GIT_ENV),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)


def execute_git(
    cmd: str,
    ref: str,
    runs: int,
    warmup: int,
    workspace: str,
    timeout: float,
    allow_fail: bool,
) -> Dict[str, Any]:
    """Compare `cmd` in a temporary detached worktree at `ref` against current `workspace`."""
    ws = _validate_run_params(runs, warmup, timeout, workspace)
    code, top_out, top_err = _git_run(["rev-parse", "--show-toplevel"], ws)
    if code != 0 or not top_out.strip():
        raise PerfError(f"Not a git repository: {ws}" + (f" ({top_err.strip()})" if top_err.strip() else ""))
    repo_root = os.path.realpath(top_out.strip())

    code, sha_out, sha_err = _git_run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_root)
    if code != 0 or not sha_out.strip():
        raise PerfError(f"Git ref {ref!r} could not be resolved to a commit"
                        + (f": {sha_err.strip()}" if sha_err.strip() else "."))
    full_sha = sha_out.strip()
    _, short_out, _ = _git_run(["rev-parse", "--short", full_sha], repo_root)
    short_sha = short_out.strip() or full_sha[:7]

    rel = os.path.relpath(ws, repo_root)
    parent_tmp = tempfile.mkdtemp(prefix="agp-perf-wt-")
    wt_dir = os.path.join(parent_tmp, "wt")
    try:
        code, _, add_err = _git_run(["worktree", "add", "--detach", wt_dir, full_sha], repo_root, timeout=60.0)
        if code != 0:
            raise PerfError(f"Failed to create git worktree for {ref}: {add_err.strip() or f'exit {code}'}")
        wt_cwd = wt_dir if rel == "." else os.path.join(wt_dir, rel)
        if not os.path.isdir(wt_cwd):
            raise PerfError(f"Directory {rel!r} does not exist at {ref} ({short_sha}).")

        result = execute_ab(
            cmd_a=cmd,
            cwd_a=wt_cwd,
            label_a=f"{ref} ({short_sha})",
            cmd_b=cmd,
            cwd_b=ws,
            label_b="working tree",
            runs=runs,
            warmup=warmup,
            timeout=timeout,
            allow_fail=allow_fail,
        )
        result["ref"] = ref
        result["ref_sha"] = short_sha
        result["workspace"] = ws
        result["worktree_path"] = wt_dir
        return result
    finally:
        _git_run(["worktree", "remove", "--force", wt_dir], repo_root, timeout=30.0)
        shutil.rmtree(parent_tmp, ignore_errors=True)
        _git_run(["worktree", "prune"], repo_root, timeout=30.0)


def _measure_file(path: str) -> Tuple[int, int]:
    with open(path, "rb") as fh:
        data = fh.read()
    return len(data), len(gzip.compress(data, compresslevel=9))


def execute_size(paths: Sequence[str]) -> Dict[str, Any]:
    """Measure raw and gzip-9 compressed byte sizes for files or directories recursively."""
    entries: List[Dict[str, Any]] = []
    total_raw = 0
    total_gzip = 0
    total_files = 0

    for raw_path in paths:
        target = os.path.expanduser(raw_path)
        if os.path.isfile(target):
            try:
                raw_b, gz_b = _measure_file(target)
            except OSError as exc:
                raise PerfError(f"Failed reading {raw_path}: {exc}") from exc
            kind = "file"
            file_count = 1
        elif os.path.isdir(target):
            kind = "dir"
            raw_b = 0
            gz_b = 0
            file_count = 0
            for root, dirs, filenames in os.walk(target, followlinks=False):
                dirs.sort()
                for name in sorted(filenames):
                    full = os.path.join(root, name)
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    try:
                        f_raw, f_gz = _measure_file(full)
                    except OSError as exc:
                        raise PerfError(f"Failed reading {full}: {exc}") from exc
                    raw_b += f_raw
                    gz_b += f_gz
                    file_count += 1
        else:
            raise PerfError(f"Path does not exist (or is not a file/directory): {raw_path}")

        ratio = (gz_b / raw_b) if raw_b > 0 else 0.0
        entries.append({
            "path": raw_path,
            "kind": kind,
            "files": file_count,
            "raw_bytes": raw_b,
            "gzip_bytes": gz_b,
            "raw_human": fmt_bytes(raw_b),
            "gzip_human": fmt_bytes(gz_b),
            "ratio": round(ratio, 4),
        })
        total_raw += raw_b
        total_gzip += gz_b
        total_files += file_count

    total_ratio = (total_gzip / total_raw) if total_raw > 0 else 0.0
    return {
        "ok": True,
        "entries": entries,
        "total": {
            "files": total_files,
            "raw_bytes": total_raw,
            "gzip_bytes": total_gzip,
            "raw_human": fmt_bytes(total_raw),
            "gzip_human": fmt_bytes(total_gzip),
            "ratio": round(total_ratio, 4),
        },
    }


def _rss_line(rss: Optional[Dict[str, Any]]) -> str:
    if rss is None:
        return "n/a (Windows)"
    return f"max {fmt_bytes(rss['max'])} (median {fmt_bytes(rss['median'])})"


def render_run(res: Dict[str, Any]) -> str:
    s = res["stats"]
    lines = [
        f"# perf run ({res['runs']} runs, {res['warmup']} warmup)",
        f"Command: `{res['command']}`",
        f"CWD: `{res['cwd']}`",
        "",
        (f"Wall time: median {fmt_ms(s['median_ms'])} | "
         f"IQR [{fmt_ms(s['p25_ms'])}, {fmt_ms(s['p75_ms'])}] (width {fmt_ms(s['iqr_ms'])})"),
        (f"Range: min {fmt_ms(s['min_ms'])}, mean {fmt_ms(s['mean_ms'])}, "
         f"stdev {fmt_ms(s['stdev_ms'])}, max {fmt_ms(s['max_ms'])}"),
        f"Child max RSS: {_rss_line(res['rss_bytes'])}",
    ]
    if res["failed_runs"]:
        lines.append(f"Exit codes: {res['failed_runs']}/{res['runs']} non-zero ({res['exit_codes']})")
    if s["noisy"]:
        lines.append(f"⚠️ Noisy measurement: IQR/median = {s['iqr_ratio'] * 100:.1f}% (> 25%).")
    return "\n".join(lines)


def _render_side(name: str, side: Dict[str, Any]) -> List[str]:
    s = side["stats"]
    lines = [
        f"## {name}: {side['label']}",
        f"Command: `{side['command']}` (cwd: `{side['cwd']}`)",
        (f"  median {fmt_ms(s['median_ms'])} | IQR [{fmt_ms(s['p25_ms'])}, {fmt_ms(s['p75_ms'])}] "
         f"(width {fmt_ms(s['iqr_ms'])}) | min {fmt_ms(s['min_ms'])} .. max {fmt_ms(s['max_ms'])}"),
        f"  mean {fmt_ms(s['mean_ms'])}, stdev {fmt_ms(s['stdev_ms'])} | Child max RSS: {_rss_line(side['rss_bytes'])}",
    ]
    if s["noisy"]:
        lines.append(f"  ⚠️ Noisy: IQR/median = {s['iqr_ratio'] * 100:.1f}% (> 25%).")
    return lines


def render_ab(res: Dict[str, Any], title: str) -> str:
    c = res["comparison"]
    sign = "+" if c["pct_change"] > 0 else ""
    lines = [
        f"# {title} ({res['runs']} interleaved runs per side, {res['warmup']} warmup; ABBA order)",
        "",
        *_render_side("Base", res["base"]),
        "",
        *_render_side("New", res["new"]),
        "",
        "## Comparison",
        (f"Median change: {sign}{c['pct_change']:.2f}% ({sign}{fmt_ms(c['delta_ms'])}) — "
         f"{c['speedup_text']}"),
        f"IQR overlap: {'yes' if c['iqr_overlap'] else 'no'} -> Verdict: {c['verdict']}",
    ]
    return "\n".join(lines)


def render_size(res: Dict[str, Any]) -> str:
    lines = ["# Artifact sizes (raw + gzip -9)", ""]
    for e in res["entries"]:
        pct = e["ratio"] * 100.0
        unit_word = "file" if e["files"] == 1 else "files"
        lines.append(
            f"- {e['path']} ({e['kind']}, {e['files']} {unit_word}): "
            f"raw {e['raw_human']} ({e['raw_bytes']} B), "
            f"gzip {e['gzip_human']} ({e['gzip_bytes']} B, {pct:.1f}%)"
        )
    if len(res["entries"]) > 1:
        t = res["total"]
        pct = t["ratio"] * 100.0
        lines.append("")
        lines.append(
            f"Total ({t['files']} files): raw {t['raw_human']} ({t['raw_bytes']} B), "
            f"gzip {t['gzip_human']} ({t['gzip_bytes']} B, {pct:.1f}%)"
        )
    return "\n".join(lines)


def _emit_error(message: str, use_json: bool) -> int:
    if use_json:
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2))
    print(f"perf.py: {message}", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="perf.py",
        description="Honest statistical performance & bundle-size benchmarking.",
    )
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p_run = sub.add_parser("run", help="benchmark a single command")
    p_run.add_argument("--cmd", required=True, help="shell command to benchmark")
    p_run.add_argument("--runs", type=int, default=7, help="recorded runs (default: 7)")
    p_run.add_argument("--warmup", type=int, default=2, help="unrecorded warmup runs (default: 2)")
    p_run.add_argument("--cwd", default=".", help="working directory (default: .)")
    p_run.add_argument("--timeout", type=float, default=60.0, help="per-run timeout in seconds (default: 60)")
    p_run.add_argument("--allow-fail", action="store_true", help="do not fail on non-zero exit codes")
    p_run.add_argument("--json", action="store_true", help="machine-readable JSON output")

    p_cmp = sub.add_parser("compare", help="interleaved A/B comparison of two commands")
    p_cmp.add_argument("--base-cmd", required=True, help="baseline command (A)")
    p_cmp.add_argument("--new-cmd", required=True, help="new/candidate command (B)")
    p_cmp.add_argument("--runs", type=int, default=MIN_VERDICT_RUNS,
                       help=f"recorded runs per side (default: {MIN_VERDICT_RUNS}, the fewest that can give a verdict)")
    p_cmp.add_argument("--warmup", type=int, default=2, help="warmup runs per side (default: 2)")
    p_cmp.add_argument("--cwd", default=".", help="working directory (default: .)")
    p_cmp.add_argument("--timeout", type=float, default=60.0, help="per-run timeout in seconds (default: 60)")
    p_cmp.add_argument("--allow-fail", action="store_true", help="do not fail on non-zero exit codes")
    p_cmp.add_argument("--json", action="store_true", help="machine-readable JSON output")

    p_git = sub.add_parser("git", help="interleaved A/B comparison of --ref worktree vs working tree")
    p_git.add_argument("--cmd", required=True, help="shell command to benchmark in both trees")
    p_git.add_argument("--ref", default="HEAD", help="git ref for baseline worktree (default: HEAD)")
    p_git.add_argument("--runs", type=int, default=MIN_VERDICT_RUNS,
                       help=f"recorded runs per side (default: {MIN_VERDICT_RUNS}, the fewest that can give a verdict)")
    p_git.add_argument("--warmup", type=int, default=1, help="warmup runs per side (default: 1)")
    p_git.add_argument("--workspace", default=".", help="git workspace directory (default: .)")
    p_git.add_argument("--timeout", type=float, default=60.0, help="per-run timeout in seconds (default: 60)")
    p_git.add_argument("--allow-fail", action="store_true", help="do not fail on non-zero exit codes")
    p_git.add_argument("--json", action="store_true", help="machine-readable JSON output")

    p_size = sub.add_parser("size", help="measure raw and gzip-9 size of files or directories")
    p_size.add_argument("paths", nargs="+", metavar="PATH", help="file or directory path(s)")
    p_size.add_argument("--json", action="store_true", help="machine-readable JSON output")

    args = ap.parse_args(argv)
    try:
        if args.subcmd == "run":
            res = execute_run(args.cmd, args.runs, args.warmup, args.cwd, args.timeout, args.allow_fail)
            print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render_run(res))
            return 0
        if args.subcmd == "compare":
            res = execute_ab(
                args.base_cmd, args.cwd, "base",
                args.new_cmd, args.cwd, "new",
                args.runs, args.warmup, args.timeout, args.allow_fail,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render_ab(res, "perf compare"))
            return 0
        if args.subcmd == "git":
            res = execute_git(args.cmd, args.ref, args.runs, args.warmup, args.workspace, args.timeout, args.allow_fail)
            print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render_ab(res, f"perf git vs {args.ref}"))
            return 0
        if args.subcmd == "size":
            res = execute_size(args.paths)
            print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render_size(res))
            return 0
    except PerfError as exc:
        return _emit_error(str(exc), bool(getattr(args, "json", False)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
