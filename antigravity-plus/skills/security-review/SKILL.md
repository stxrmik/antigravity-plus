---
name: security-review
description: >-
  Focused security review of pending changes or a PR: the attack surface they touch, untrusted input reaching
  dangerous sinks, an exploitability check, real issues only, with fixes. Use when the user asks («проверь на
  уязвимости», /security-review), and on your own when your change touches auth, sessions, permissions, crypto,
  secrets, payments, file uploads, or builds SQL, shell or HTML from user input.
---

# /security-review — security audit of pending changes

> **On your own:** run it when your change touches auth, sessions, permissions, crypto, secrets, payments, uploads, or builds SQL, shell or HTML from user input. Say so in one line first («Запускаю /security-review: правки в авторизации»). Scope = your change. Fix the confirmed issues your change introduced, then report.

Read-only: do **not** modify code during the review. Offer fixes at the end.

## 1. Scope
Default: everything not yet on the base branch — branch commits **and** uncommitted changes.
- Branch part: `bash <skill_dir>/../commit/scripts/git_context.sh review branch`
- Uncommitted part: `bash <skill_dir>/../commit/scripts/git_context.sh review`
- User gave a PR number/URL → `gh pr diff <n>` and `gh pr view <n>` (network → `BypassSandbox: true`).
- User gave a ref/range or paths → `git_context.sh review <ref|a..b>` or audit those files in full.

If there's no git repo or the diff is empty, say so and stop. Read new/untracked files in full.

## 2. Map the attack surface (yourself, quickly)
From the changed files list: which entry points are touched (HTTP routes, CLI args, webhooks, message
consumers, file uploads, IPC), which sinks (SQL/ORM raw queries, shell/exec, templates/HTML, file paths,
redirects, deserialization, crypto, auth checks, secrets/config), and which trust boundaries change.
Skip pure docs/tests/formatting changes.

## 3. Delegate the deep audit
Invoke the **`security-auditor`** subagent (it may be listed as `antigravity-plus:security-auditor`) with:
the diff (or how to re-run the command above), the changed-file list, the attack-surface notes from
step 2, the project's `AGENTS.md` conventions, and the intent of the change. For large diffs
(> ~800 changed lines) split by area and run one auditor per area in parallel.
If the subagent is unavailable, do the audit yourself following
`<skill_dir>/../../agents/security-auditor.md`.

## 4. Verify every finding
For each candidate, open the code and confirm: the input is actually attacker-controlled, there is no
upstream validation / framework protection (ORM parameterization, template auto-escaping, CSRF
middleware, auth decorator), and the exploit path is reachable. Drop theoretical issues, duplicates,
and generic hardening advice that the diff didn't introduce.

## 5. Report (in the user's language, concise)
- Verdict: ✅ уязвимостей не найдено / ⚠️ есть риски / ⛔ критично — исправить до мержа.
- Findings, most severe first: `[CRITICAL|HIGH|MEDIUM|LOW]`, clickable `file:line`, attack scenario
  (who, how, with what input), impact, concrete fix (code if short).
- "Проверено": one line listing the areas you checked, so "nothing found" is meaningful.
- Never print full secrets — redacted prefix + location only.
- Offer to apply the fixes.
