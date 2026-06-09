#!/usr/bin/env bash
#
# export_clean_repo.sh — produce a shareable, credential-free archive of the repo.
#
# WHY: A manual `zip -r` of the project root has previously leaked `.env`
# (with live DB credentials), `.git/`, and bulky generated artifacts. This
# script instead uses `git archive HEAD`, which by construction includes ONLY
# files tracked at HEAD — gitignored files (`.env`, `backtest_artifacts/`, most
# of `reports/`, caches) and untracked files are inherently excluded. A
# post-build self-check then asserts that no forbidden path slipped through.
#
# OUTPUT: dist/bond-factor-lab-clean.tar.gz
#
# NOTE: `dist/` is NOT listed in .gitignore. This is harmless here because the
# tarball is an untracked file and `git archive` never includes untracked files
# (so the archive cannot contain itself). Editing .gitignore is out of scope for
# this script; consider adding `dist/` to .gitignore separately.
#
# Idempotent: safe to re-run; the output tarball is overwritten each time.

set -euo pipefail

# Resolve the repo root from this script's location so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

DIST_DIR="${REPO_ROOT}/dist"
OUT="${DIST_DIR}/bond-factor-lab-clean.tar.gz"

mkdir -p "${DIST_DIR}"

echo "Building clean archive from HEAD..."
# git archive includes ONLY files tracked at HEAD; gitignored + untracked
# files are inherently excluded.
git archive --format=tar.gz -o "${OUT}" HEAD

echo "Running self-check on archive contents..."
contents="$(tar tzf "${OUT}")"

# Each pattern is an extended-regex matched against every archive entry path.
# A match means a forbidden file leaked into the archive -> fail.
violations=""
check() {
  local label="$1" regex="$2" allow="${3:-}"
  local hits
  hits="$(printf '%s\n' "${contents}" | grep -E "${regex}" || true)"
  # Optional allow-list: drop entries matching this exact ERE before judging.
  if [[ -n "${allow}" && -n "${hits}" ]]; then
    hits="$(printf '%s\n' "${hits}" | grep -Ev "${allow}" || true)"
  fi
  if [[ -n "${hits}" ]]; then
    violations+=$'\n'"[${label}]"$'\n'"${hits}"
  fi
}

# A `.env` path component, but allow `.env.example`.
#   - matches: `.env`, `foo/.env`, `.env/...`
#   - allows:  `.env.example` (the trailing `.example` makes it not match)
check ".env"               '(^|/)\.env($|/)'
check ".git directory"     '(^|/)\.git/'
check "*.pem"              '\.pem$'
check "*.key"              '\.key$'
# Any top-level reports/ entry EXCEPT the deliberately-tracked reports/README.md.
# Everything else under reports/ is gitignored generated output (csv/json/etc.)
# and must never appear in the archive.
check "top-level reports/" '^reports/' '^reports/$|^reports/README\.md$'
check "backtest_artifacts" '(^|/)backtest_artifacts/'
check "__pycache__"        '(^|/)__pycache__/'
check ".DS_Store"          '(^|/)\.DS_Store$'

if [[ -n "${violations}" ]]; then
  echo "ERROR: archive self-check FAILED. Forbidden entries found:" >&2
  echo "${violations}" >&2
  echo >&2
  echo "Removing the unsafe archive: ${OUT}" >&2
  rm -f "${OUT}"
  exit 1
fi

# Portable size lookup (BSD/macOS `stat -f%z`, GNU `stat -c%s`).
if size_bytes="$(stat -f%z "${OUT}" 2>/dev/null)"; then
  :
else
  size_bytes="$(stat -c%s "${OUT}")"
fi
# Human-readable if available, else raw bytes.
if size_human="$(du -h "${OUT}" 2>/dev/null | cut -f1)"; then
  size_str="${size_human} (${size_bytes} bytes)"
else
  size_str="${size_bytes} bytes"
fi

echo "OK: clean archive self-check passed."
echo "Tarball: ${OUT}"
echo "Size:    ${size_str}"
