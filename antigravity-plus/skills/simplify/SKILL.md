---
name: simplify
description: >-
  Reviews recently changed code for reuse, simplification and efficiency, then applies behavior-preserving
  cleanups and verifies them. Quality only, not a bug hunt. Use when the user asks («упрости», «почисти код»,
  «отрефакторь», /simplify). Not on your own unless the user granted full access, because it changes code beyond the request; after a large change you
  may offer it in one line.
---

# /simplify — clean up the changed code

> **Only on the user's request, or under full access:** the command, the same request in their own words, or full access the user granted (rules: "Full access"). Otherwise, if you got here on your own, don't run it; offer it to the user in one line instead.

Invoking this skill is permission to **edit** the changed code (not unrelated code). Add `--dry` /
"только покажи" → report the proposals without editing.

## 1. Scope
Default: uncommitted changes (`bash <skill_dir>/../commit/scripts/git_context.sh review`); "branch" →
`… review branch`; explicit paths → those files. Empty diff → say so and stop.

## 2. Look for (in the changed hunks only)
- **Reuse:** logic that duplicates an existing helper/util/component in the repo — search for it
  (`rg -n '<distinctive name or call>'`, or delegate a broad search to the `explorer` subagent) and use
  the existing one. Duplication *within* the diff → extract only if it appears 3+ times.
- **Simplification:** needless indirection, wrappers that only forward, flags/params nobody passes,
  deep nesting that early returns flatten, re-implemented stdlib/framework features, dead branches,
  commented-out code, unused imports/vars, over-general abstractions with one caller.
- **Efficiency:** repeated work in loops, N+1 queries, quadratic searches over lists that should be
  sets/maps, re-reading files or re-computing values that are already available.
- **Altitude:** code doing work at the wrong layer (e.g. formatting in data access, business rules
  in UI handlers) when the fix is small and local.

Skip: style nits a formatter would handle, renames for taste, anything that changes behavior or a
public API, and code outside the diff.

## 3. Apply
Make each change minimal and behavior-preserving; match the surrounding style. Keep a list of what you
changed and why.

## 4. Verify
Run the project's relevant checks (tests for touched modules, type-check, lint — find the real commands
in `AGENTS.md` / manifests). If something fails because of a cleanup, fix or revert that cleanup.

## 5. Report (in the user's language)
Bullets grouped by Reuse / Simplification / Efficiency, each with a `file:line` link and one-line
reason; then how it was verified; then proposals you deliberately skipped (risky or out of scope).
