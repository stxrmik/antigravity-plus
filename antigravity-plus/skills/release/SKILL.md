---
name: release
description: >-
  Cuts a project release safely: finds all version sources and mismatches, suggests the next bump from Conventional
  Commits, generates release notes, bumps versions with minimal formatting-preserving edits, updates CHANGELOG.md,
  creates an annotated tag, and publishes a GitHub release with explicit confirmation. Runs ONLY when the user asks
  (/release, «выпусти релиз», «сделай релиз») or under full access (read-only `plan` / `notes` may inspect what a
  release would look like when asked).
---

# /release — safe version bumps, tags, and GitHub releases

> **Only on the user's request, or under full access** for making a release (`bump`, `tag`, `publish`). Never push tags
> or publish a GitHub release without explicit user go-ahead (or full access without a «не пушь» / «только локально» ban).

Helper: `python3 "<skill_dir>/scripts/release.py"` (below: `release.py`).
- `plan` (with `--no-ci`) and `notes` are local; `plan` (with CI check / `--fetch`) and `publish` talk to GitHub over the network → run those with `BypassSandbox: true`.

## Commands

```bash
release.py plan    [--path DIR] [--fetch] [--no-ci] [--json]
release.py bump    <major|minor|patch|X.Y.Z> [--path DIR] [--dry-run] [--no-changelog]
release.py notes   [--since TAG] [--until REF] [--out FILE] [--path DIR]
release.py tag     [--version X.Y.Z] [--message TEXT] [--path DIR] [--dry-run]
release.py publish [--version X.Y.Z] [--remote NAME] [--title TEXT] [--notes-file FILE] [--draft] [--prerelease] [--path DIR] --yes
```

Exit codes: `0` done, `1` refused or failed, `2` not a git repository (where required), `3` `publish` previewed without `--yes`.

## Workflow

1. **Inspect the state (`plan`):**
   ```bash
   python3 "<skill_dir>/scripts/release.py" plan --path "<ws>" --fetch
   ```
   (Use `--no-ci` if working offline.) Review:
   - All detected version sources (`package.json`, `package-lock.json`, `pyproject.toml`, `setup.cfg`, `Cargo.toml`,
     `plugin.json` / `manifest.json`, `VERSION*`, `__version__`, Gradle `versionName`, Xcode `MARKETING_VERSION`) and any mismatches.
   - Last release tag, unmerged newer tags, commits since the tag, suggested bump, and draft notes.
   - Tree cleanliness, branch (default branch vs feature branch), upstream sync, and CI status of `HEAD`.

2. **Agree on the version with the user:**
   Present the current version (and any mismatches), the suggested bump + reasoning, and the draft notes. If sources disagree, a relative bump (`major`/`minor`/`patch`) refuses — agree on an explicit `X.Y.Z` to align every source.

3. **Bump the version:**
   Run `release.py bump <major|minor|patch|X.Y.Z> --dry-run` to preview edits, then run `release.py bump <target>`.
   - `bump` uses minimal textual edits so JSONC comments, trailing commas, TOML comments, indentation, CRLF/LF, and `v` prefixes are preserved.
   - If `CHANGELOG.md` exists, `bump` turns `[Unreleased]` into the release section (keeping any manual entries under `[Unreleased]`) or inserts a new section matching the file's existing heading style. Pass `--no-changelog` if the user wants to leave `CHANGELOG.md` untouched.

4. **Run the project's tests and build:**
   Review `git diff` and run the project's test suite / build. Fix any issues before committing or tagging.

5. **Commit, push, and check CI:**
   - Commit the bump via `/commit` (e.g. `chore(release): vX.Y.Z`).
   - If the repository releases via PR or you're on a release branch, push and check CI via `/pr` / `/ci` (`ci.py watch`). If on the default branch, push and run `ci.py watch` until CI is green.
   - Respect user bans («не пушь» / «только локально»): stop after local commit or local tag if pushing is disallowed.

6. **Create the local tag:**
   ```bash
   python3 "<skill_dir>/scripts/release.py" tag --path "<ws>"
   ```
   Requires a clean working tree and matching version sources (`git tag -d <tag>` undoes a local tag).

7. **Publish (outward-facing — explicit go-ahead only):**
   First preview without `--yes` (exits `3`, shows the exact `git push` and `gh release create` commands):
   ```bash
   python3 "<skill_dir>/scripts/release.py" publish --path "<ws>"
   ```
   Only after the user explicitly says to publish (or under full access), run with `--yes` (`BypassSandbox: true`, add `--draft` or `--prerelease` if requested). `publish` refuses if the tagged commit is not on the remote branch, CI is red, or `gh` is missing / not authenticated (tell the user to run `gh auth login`; never work around it).
