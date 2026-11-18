---
name: perf
description: >-
  Honest statistical before/after performance and bundle-size benchmarking: median, p25/p75 IQR, noise detection,
  interleaved ABBA A/B comparisons, temporary git worktree comparisons against HEAD, child max RSS, and raw + gzip-9
  artifact sizes. Use when measuring or comparing speed, memory, or bundle size («измерь скорость», «стало быстрее?»,
  «сравни производительность», «размер бандла», /perf), and before claiming an optimization improved performance.
---

# /perf — statistical before/after performance and size benchmarking

Helper: `python3 "<skill_dir>/scripts/perf.py"` (below: `perf.py`).

## Commands

```bash
perf.py run     --cmd "<command>" [--runs 7] [--warmup 2] [--cwd DIR] [--timeout 60] [--allow-fail] [--json]
perf.py compare --base-cmd "<cmd1>" --new-cmd "<cmd2>" [--runs 10] [--warmup 2] [--cwd DIR] [--timeout 60] [--allow-fail] [--json]
perf.py git     --cmd "<command>" [--ref HEAD] [--runs 10] [--warmup 1] [--workspace DIR] [--timeout 60] [--allow-fail] [--json]
perf.py size    PATH [PATH ...] [--json]
```

- `run` — executes `--warmup` unrecorded runs, then `--runs` timed runs (`time.perf_counter()`, `stdin=DEVNULL`), reporting `min`, `p25`, `median`, `p75`, `iqr`, `mean`, `stdev`, `max`, exit codes (fails with exit `1` on non-zero exit unless `--allow-fail`), and POSIX child max RSS (`None` on Windows). Flags noisy runs when `iqr / median > 0.25`. `--timeout` stops a run together with everything it started (process group on POSIX, `taskkill /T /F` on Windows).
- `compare` — warms up both commands, then runs them in counterbalanced interleaved order (`A, B, B, A, A, B, ...`) so CPU frequency scaling, caching, or background load does not skew one side. Reports medians, percentage delta, speedup (`1.42x faster` / `1.20x slower`), and `[p25, p75]` overlap. The default `--runs 10` per side is the fewest that can give a verdict.
- `git` — checks out `--ref` (default `HEAD`) into a temporary detached `git worktree`, runs the interleaved A/B benchmark between `--ref` and the current working tree, and always removes the temporary worktree in cleanup. Note: the `--ref` worktree does not contain untracked directories like `node_modules` or `.venv`; point the command at shared dependencies or use `compare` if needed.
- `size` — reports raw byte size and `gzip` level-9 compressed size (`KiB`/`bytes` + `--json`) for files or directories (walked recursively).

## How to interpret results

1. **Rely on `median` and `[p25, p75]`, not single runs or `mean`:** single runs and arithmetic means are easily distorted by cold caches, GC pauses, or scheduler spikes.
2. **Respect the verdict:**
   - `clear improvement` / `clear regression` — at least 10 runs per side and the `[p25, p75]` interquartile ranges do **not** overlap.
   - `inconclusive (only N runs per side: a verdict needs at least 10)` — too few runs to tell noise from a change (with 5 runs, identical commands miss each other's IQR about 1 time in 5); rerun with `--runs 10` or more.
   - `inconclusive (IQR overlap / within noise)` — the `[p25, p75]` ranges overlap; you cannot honestly claim a speedup or regression. Increase `--runs`, test a larger workload (so process startup does not dominate), or reduce background load.
3. **Check `noisy` warnings (`IQR / median > 25%`):** if a run is noisy, report the spread and treat small differences cautiously.

## Rules

- **Never claim a performance, memory, or size improvement in your final report without an actual measurement** in this conversation (verified by `facts.py`): run `perf.py` after your last change and cite the observed medians, `[p25, p75]` / IQR, run count, or byte counts.
- **If the comparison is `inconclusive (…)`** (too few runs or IQR overlap), report that plainly instead of claiming the code is faster or slower.
- **Never fake a benchmark** or extrapolate timings from reading code alone.
