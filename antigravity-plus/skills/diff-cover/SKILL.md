---
name: diff-cover
description: >-
  Computes test coverage specifically over git-changed lines from Cobertura XML, LCOV, Go coverprofile, or Xcode
  .xcresult reports without external dependencies. Use when checking whether new or edited code is tested («покрытие
  диффа», «что не покрыто тестами», /diff-cover), before /commit or /pr, or to pass exact uncovered line ranges
  to `test-writer`. Helper: scripts/diff_cover.py.
---

# /diff-cover — test coverage on git-changed lines

Helper: `python3 "<skill_dir>/scripts/diff_cover.py"` (below: `diff_cover.py`). Pure Python stdlib, offline.

## Command

```bash
diff_cover.py [--workspace DIR] [--base REF] [--coverage PATH] [--min PCT] [--json]
```

- `--workspace DIR`: repository or project directory (default `.`).
- `--base REF`: git ref, branch, or range to diff against. When omitted:
  - if the working tree has staged/unstaged edits or untracked files vs `HEAD` (coverage output such as `.coverage`, `htmlcov/` or the report's own `coverage/` folder doesn't count), diffs `HEAD` (including untracked files);
  - otherwise, if on a feature branch, diffs against the `origin/main` or `main` merge-base;
  - otherwise diffs against `HEAD~1`.
- `--coverage PATH`: explicit report path (default: auto-discovers the newest of `coverage.xml`, `cobertura.xml`, `reports/coverage.xml`, `coverage/lcov.info`, `lcov.info`, `coverage.out`, `cover.out`, or `.xcresult` on macOS).
- `--min PCT`: fail with exit `1` if diff coverage is below `PCT`.
- `--json`: machine-readable JSON (`covered_lines`, `total_executable_changed_lines`, `percent`, `files`, `not_in_coverage`, `warning`, and grouped `uncovered_ranges` with 1-line previews).

Exit codes: `0` when computed (and `>= --min` if set), `1` when below `--min`, `2` when not a git repo, invalid `--base`, or no coverage report was found.

When changed source files exist but **none** of them is in the report (its paths don't match the repository's, or it is stale or from another checkout), it says so (`warning`, `percent: null`, `n/a`) instead of a 100% — and `--min` fails with exit `1`. Regenerate the report (or pass `--coverage`) before trusting the number. Paths with spaces, quotes or a leading dot (`.github/…`) match like any other.

## Workflow

1. **Ensure a fresh coverage report exists.** If none exists (exit `2`), run the project's existing test runner with coverage output (never install new tools unasked):
   - Python: `pytest --cov --cov-report=xml` (`coverage.xml`)
   - JS/TS: `npx vitest run --coverage` / `jest --coverage` (`coverage/lcov.info` or Cobertura XML)
   - Go: `go test -coverprofile=coverage.out ./...` (`coverage.out`)
   - Xcode (macOS): `xcodebuild test -enableCodeCoverage YES -resultBundlePath TestResults.xcresult`
2. **Run `diff_cover.py`:**
   ```bash
   python3 "<skill_dir>/scripts/diff_cover.py" --workspace "<ws>"
   ```
   Non-executable changed lines (comments, blank lines, docstrings) are excluded automatically using the report's instrumented lines. Changed files missing from the report are listed under `Not in coverage report` (`not_in_coverage`).
3. **Optionally fill coverage gaps with `test-writer`:**
   When uncovered ranges or `not_in_coverage` files need tests, invoke `test-writer` with:
   - the affected file paths,
   - the exact contiguous uncovered ranges (e.g. `42-45, 51`) and 1-line previews from `diff_cover.py` (or `--json` output),
   - the project's test command so `test-writer` adds targeted tests for those branches and verifies them.
   After `test-writer` finishes, re-run the test suite with coverage and re-run `diff_cover.py` to confirm coverage improved.
