#!/usr/bin/env bash
# Publish this directory only to buluu97/evo-agent-platform branch latest-1124.
# Prereq: git credential helper or `gh auth login` so HTTPS push works.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_URL="${REMOTE_URL:-https://github.com/buluu97/evo-agent-platform.git}"
BRANCH="${BRANCH:-latest-1124}"
MSG="${1:-Update crewai-midtest-experiment snapshot on ${BRANCH}.}"

REMOTE_SHA="$(git ls-remote "$REMOTE_URL" "refs/heads/${BRANCH}" | awk '{print $1}')"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/crewai-push-XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

rsync -a --delete \
  --exclude '.venv/' --exclude 'venv/' --exclude '.platform/' --exclude '.runtime/' \
  --exclude 'config-deepseek/.runtime/' --exclude '.env' --exclude '.env.*' \
  --exclude '__pycache__/' --exclude '*.py[cod]' --exclude '.DS_Store' \
  --exclude 'evidence/' --exclude 'runs/' --exclude 'deliverables/' --exclude '*-deliverables/' \
  --exclude '*.zip' --exclude '*.log' --exclude '*.sqlite*' --exclude '*.db' \
  "$SRC/" "$TMP/"

cd "$TMP"
git init -q
git add -A
if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi
git commit -m "$MSG"
git branch -M "$BRANCH"

if [[ -n "$REMOTE_SHA" ]]; then
  git push "$REMOTE_URL" \
    --force-with-lease="refs/heads/${BRANCH}:${REMOTE_SHA}" \
    "${BRANCH}:${BRANCH}"
else
  git push "$REMOTE_URL" "${BRANCH}:${BRANCH}"
fi

echo "Pushed ${BRANCH} at $(git rev-parse --short HEAD) -> ${REMOTE_URL}"