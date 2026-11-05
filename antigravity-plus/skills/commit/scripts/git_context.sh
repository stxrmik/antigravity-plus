#!/usr/bin/env bash
# Antigravity Plus — compact git context for /commit, /pr and /review.
#
#   git_context.sh commit                 # working tree: status, staged/unstaged diffs, style, secret scan
#   git_context.sh pr [base]              # current branch vs base: commits, diffstat, diff
#   git_context.sh review [target]        # target: (empty)=uncommitted | staged | branch | <ref> | <a>..<b>
#
# Env: MAX_DIFF_LINES (default 2000) truncates diffs to keep context small.
set -uo pipefail

MODE="${1:-commit}"
ARG="${2:-}"
MAX="${MAX_DIFF_LINES:-2000}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: not inside a git repository"; exit 2
fi
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 2

section() { printf '\n## %s\n' "$1"; }
clip() {  # clip <label>  (reads stdin)
  local tmp; tmp="$(mktemp)"; cat >"$tmp"
  local n; n=$(wc -l <"$tmp" | tr -d ' ')
  if [ "$n" -gt "$MAX" ]; then
    head -n "$MAX" "$tmp"
    echo "… [truncated: $n lines total, showing $MAX — run 'git diff' on specific files for the rest]"
  else
    cat "$tmp"
  fi
  rm -f "$tmp"
}

detect_base() {
  local b="$1"
  if [ -n "$b" ]; then echo "$b"; return; fi
  b="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
  if [ -n "$b" ]; then echo "$b"; return; fi
  for c in origin/main origin/master main master origin/develop develop; do
    if git rev-parse --verify --quiet "$c" >/dev/null; then echo "$c"; return; fi
  done
  echo ""
}

SECRET_RE='AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'[:space:]]{8,}'

secret_scan() {  # secret_scan <diff-args...>   (scans added lines only)
  local hits
  hits="$(git diff "$@" -U0 2>/dev/null | grep -E '^\+[^+]' | grep -niE "$SECRET_RE" | cut -c1-160 | head -n 20)"
  if [ -n "$hits" ]; then
    echo "⚠️  POSSIBLE SECRETS in added lines:"; echo "$hits"
  else
    echo "none detected in diff"
  fi
}

secret_scan_untracked() {
  local hits=""
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    [ "$(wc -c <"$f" | tr -d ' ')" -gt 1048576 ] && continue
    h="$(grep -niIE "$SECRET_RE" "$f" 2>/dev/null | cut -c1-140 | head -n 5)"
    [ -n "$h" ] && hits="$hits$f:\n$h\n"
  done < <(git ls-files --others --exclude-standard)
  if [ -n "$hits" ]; then printf '⚠️  POSSIBLE SECRETS in untracked files:\n%b' "$hits"; else echo "none detected in untracked files"; fi
}

risky_files() {  # list risky paths among given file list (stdin)
  grep -E '(^|/)(\.env(\..*)?|.*\.pem|.*\.p12|.*\.key|id_rsa.*|id_ed25519.*|credentials.*\.json|.*\.keystore|secrets?\.(ya?ml|json))$' || true
}

style() {
  section "Recent commit subjects (match this style)"
  git log -n 15 --pretty=format:'%s' 2>/dev/null || echo "(no commits yet)"
  echo
}

case "$MODE" in
  commit)
    section "Branch"
    echo "$(git branch --show-current 2>/dev/null || echo '(detached)')  $(git status -sb | head -n1)"
    section "Status (short)"
    git status --short | head -n 200
    section "Staged diffstat"
    git diff --cached --stat | tail -n 60
    section "Unstaged diffstat"
    git diff --stat | tail -n 60
    section "Untracked files"
    git ls-files --others --exclude-standard | head -n 100
    section "Risky files (staged, unstaged or untracked)"
    r="$( { git diff --cached --name-only; git diff --name-only; git ls-files --others --exclude-standard; } | sort -u | risky_files)"
    echo "${r:-none}"
    section "Large files (>1MB) among changes"
    big=""
    while IFS= read -r f; do
      [ -f "$f" ] && [ "$(wc -c <"$f" | tr -d ' ')" -gt 1048576 ] && big="$big$f\n"
    done < <( { git diff --cached --name-only; git ls-files --others --exclude-standard; } | sort -u)
    printf '%b' "${big:-none\n}"
    section "Secret scan (staged + unstaged)"
    if git rev-parse -q --verify HEAD >/dev/null; then secret_scan HEAD; else secret_scan --cached; fi
    secret_scan_untracked
    style
    section "Staged diff"
    git diff --cached | clip
    section "Unstaged diff"
    git diff | clip
    ;;

  pr)
    BASE="$(detect_base "$ARG")"
    BR="$(git branch --show-current)"
    section "Branch"
    echo "head: $BR"
    echo "base: ${BASE:-'(not found — pass it explicitly)'}"
    UP="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    echo "upstream: ${UP:-none (branch not pushed)}"
    [ -n "$UP" ] && echo "ahead/behind upstream: $(git rev-list --left-right --count "$UP"...HEAD 2>/dev/null | awk '{print "behind " $1 ", ahead " $2}')"
    if [ -n "$(git status --porcelain)" ]; then echo "⚠️  uncommitted changes present (not part of the PR)"; fi
    section "Tooling"
    if command -v gh >/dev/null 2>&1; then echo "gh: installed"; else echo "gh: not installed"; fi
    [ -f .github/pull_request_template.md ] && echo "PR template: .github/pull_request_template.md"
    [ -f .github/PULL_REQUEST_TEMPLATE.md ] && echo "PR template: .github/PULL_REQUEST_TEMPLATE.md"
    [ -z "$BASE" ] && exit 0
    MB="$(git merge-base "$BASE" HEAD 2>/dev/null)"
    section "Commits ($BASE..HEAD)"
    git log --reverse --pretty=format:'- %h %s%n%w(0,4,4)%b' "$MB"..HEAD | sed '/^ *$/d' | head -n 200
    echo
    section "Diffstat"
    git diff --stat "$MB"...HEAD | tail -n 80
    section "Secret scan"
    secret_scan "$MB"...HEAD
    section "Diff"
    git diff "$MB"...HEAD | clip
    ;;

  review)
    case "$ARG" in
      ""|uncommitted)
        section "Scope: uncommitted changes (staged + unstaged + untracked)"
        git diff HEAD --stat 2>/dev/null | tail -n 80 || git diff --stat
        UNTR="$(git ls-files --others --exclude-standard)"
        section "Changed files"
        { git diff HEAD --name-only 2>/dev/null; echo "$UNTR"; } | sed '/^$/d' | sort -u
        section "Diff"
        { git diff HEAD 2>/dev/null || git diff; } | clip
        if [ -n "$UNTR" ]; then
          section "Untracked files (read them in full)"
          echo "$UNTR"
        fi
        ;;
      staged)
        section "Scope: staged changes"
        git diff --cached --stat | tail -n 80
        section "Changed files"; git diff --cached --name-only
        section "Diff"; git diff --cached | clip
        ;;
      branch)
        BASE="$(detect_base "")"
        MB="$(git merge-base "$BASE" HEAD)"
        section "Scope: branch $(git branch --show-current) vs $BASE"
        git log --oneline "$MB"..HEAD | head -n 50
        section "Diffstat"; git diff --stat "$MB"...HEAD | tail -n 80
        section "Changed files"; git diff --name-only "$MB"...HEAD
        section "Diff"; git diff "$MB"...HEAD | clip
        ;;
      *..*)
        section "Scope: range $ARG"
        git log --oneline "$ARG" | head -n 50
        section "Diffstat"; git diff --stat "$ARG" | tail -n 80
        section "Changed files"; git diff --name-only "$ARG"
        section "Diff"; git diff "$ARG" | clip
        ;;
      *)
        section "Scope: commit/ref $ARG"
        git show --stat --format='%h %s%n%n%b' "$ARG" | head -n 100
        section "Diff"; git show --format= "$ARG" | clip
        ;;
    esac
    ;;

  *)
    echo "usage: git_context.sh commit | pr [base] | review [uncommitted|staged|branch|<ref>|<a>..<b>]"; exit 2
    ;;
esac
