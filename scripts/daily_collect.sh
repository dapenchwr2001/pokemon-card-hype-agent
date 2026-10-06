#!/usr/bin/env bash
# Daily collection run, used by the scheduled Routine.
# History (hype.db) and the watchlist live on the DATA_BRANCH branch, not with the code. Each run
# restores them, collects, and pushes them back as a single fresh commit so binary DB copies
# don't pile up in git history (the DB itself holds the full history).
set -euo pipefail

DATA_BRANCH="${DATA_BRANCH:-hype-data}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
DATA_DIR="$(mktemp -d)"

git -C "$REPO_ROOT" fetch origin "$DATA_BRANCH"
git -C "$REPO_ROOT" worktree add --detach "$DATA_DIR" "origin/$DATA_BRANCH"

HYPE_DB_PATH="$DATA_DIR/hype.db" HYPE_WATCHLIST_PATH="$DATA_DIR/watchlist.json" \
  "${PYTHON:-python}" -m hype_agent.collect "$@"

cd "$DATA_DIR"
git checkout --quiet --orphan "data-$(date -u +%Y%m%d%H%M%S)"
git add -A
git commit --quiet -m "Hype data as of $(date -u +'%Y-%m-%d %H:%M UTC')"
for delay in 2 4 8 16 0; do
  if git push --force origin "HEAD:refs/heads/$DATA_BRANCH"; then break; fi
  [ "$delay" = 0 ] && { echo "push failed" >&2; exit 1; }
  sleep "$delay"
done
cd "$REPO_ROOT" && git worktree remove --force "$DATA_DIR"
echo "Saved data to branch $DATA_BRANCH"
