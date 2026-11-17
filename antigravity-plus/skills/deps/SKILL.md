---
name: deps
description: >-
  Audits project dependencies for known vulnerabilities and outdated packages across Python, JS/TS, Rust, Go, Ruby,
  PHP, Swift, and Gradle using only already-installed tools (plus osv-scanner fallback). Run on your own (read-only
  `scan` / `audit`) before `/deploy` when dependencies changed or when `security-auditor` flags a package, and when
  the user asks («проверь зависимости», «есть ли уязвимости», «что устарело», /deps). Never install scanners or
  upgrade packages unasked, and never run `npm audit fix --force`.
---

# /deps — dependency vulnerabilities and outdated packages

Helper: `python3 "<skill_dir>/scripts/deps.py"` (below: `deps.py`).
- `scan` is local and offline.
- `audit`, `outdated`, and `all` query registries/advisory databases over the network → run them with `BypassSandbox: true`.

## Commands

```bash
deps.py scan     [--path DIR] [--json]                  # ecosystems, manifests, lockfiles, installed tools
deps.py audit    [--path DIR] [--json] [--timeout 120]  # known vulnerabilities (native tools + osv-scanner fallback)
deps.py outdated [--path DIR] [--json] [--timeout 120]  # outdated packages classified major / minor / patch
deps.py all      [--path DIR] [--json] [--timeout 120]  # audit + outdated
```

Exit codes:
- `0` — clean: nothing vulnerable or outdated found, and every check ran.
- `1` — findings: vulnerable or outdated packages found.
- `2` — incomplete: at least one check couldn't run (`missing`, `failed`, `timeout`, or `unsupported`) and nothing was found elsewhere (or `--path` does not exist).

## How to run and read results

1. **Before `/deploy` or during a security check:** run `deps.py audit --path "<ws>"` (or `deps.py scan` first if you only want to see ecosystems/lockfiles). When the user asks for a full dependency check («проверь зависимости», «что устарело»), run `deps.py all --path "<ws>"`.
2. **Reading vulnerabilities:**
   - Ordered by severity (`critical` → `high` → `moderate` → `low`) then package name; check `id`, `aliases`, `fixed`, and `fix`.
   - For Go (`govulncheck`), distinguish `your code calls it` (reachable, urgent) from `imported, not called`.
   - Use `--json` when more than 15 findings are truncated or when you need structured fields.
3. **Reading outdated packages:**
   - Classified `major`, `minor`, `patch` (in `0.x`, a minor bump counts as `major` per semver).
   - Prioritize `patch`/`minor` security fixes first; major bumps may break APIs (review changelogs and upgrade one at a time, only if requested).
4. **Missing or failing tools (`⚠️ ... not checked`):**
   - `deps.py` never installs tools itself. Report what couldn't run and include the exact install command printed by `deps.py` (`install` / `fallback_install`), or note why it was skipped (e.g. missing lockfile or `.venv`).
   - If `osv-scanner` is installed, `deps.py` automatically falls back to it for any ecosystem with a lockfile whose native scanner is missing, unsupported, or failed.

## Rules

- **Read-only by default:** never install scanners (`pip-audit`, `osv-scanner`, `cargo-audit`, etc.), create `.venv`, or generate lockfiles without asking the user first.
- **Never upgrade packages unasked:** present the upgrade plan (`What to do` in the report) and ask before modifying manifests or lockfiles.
- **Never run `npm audit fix --force`:** it silently applies breaking major upgrades.
- **After any user-approved upgrade:** run the project's tests and build, then re-run `deps.py audit` to verify the fix.
