#!/usr/bin/env bash
# Harbor verifier entry point for a JETO-Bench task. Runs inside the task image.
#
# The agent edits /app/original_repo in place. This script reconstructs the
# untouched baseline next to it, runs the modified modules' test suite
# 11 times per version (alternating, to spread machine noise across
# both), then hands the logs to verifier.py for the reward.
set -uo pipefail

CANDIDATE_REPO="/app/original_repo"
BASELINE_REPO="/app/baseline_repo"
MODULES="dubbo-common"
EXEC_TIMES=11
LOG_DIR="/logs/harbor"
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$LOG_DIR"

# The image captured a pristine baseline at build time (see environment/
# Dockerfile). Comparing against it is the same comparison PatchEvaluator makes:
# original vs. candidate. Fall back to reverting the agent's git changes only if
# that copy is missing.
if [ ! -d "$BASELINE_REPO" ]; then
  echo "NOTE: $BASELINE_REPO absent — reconstructing it from git" >&2
  cp -r "$CANDIDATE_REPO" "$BASELINE_REPO"
  if [ -d "$BASELINE_REPO/.git" ]; then
    git -C "$BASELINE_REPO" checkout -- . 2>/dev/null || true
    git -C "$BASELINE_REPO" clean -fd 2>/dev/null || true
  else
    echo "WARNING: no .git either — baseline is NOT pristine, timings are meaningless" >&2
  fi
fi

run_suite() {  # <repo> <log path>
  local repo="$1" log="$2"
  ( cd "$repo" && ./mvnw -pl "$MODULES" -am test \
      -Dsurefire.runOrder=alphabetical -DfailIfNoTests=false ) > "$log" 2>&1
}

for i in $(seq 0 $((EXEC_TIMES - 1))); do
  if [ $((i % 2)) -eq 0 ]; then
    run_suite "$BASELINE_REPO"  "$LOG_DIR/baseline_$i.log"
    run_suite "$CANDIDATE_REPO" "$LOG_DIR/candidate_$i.log"
  else
    run_suite "$CANDIDATE_REPO" "$LOG_DIR/candidate_$i.log"
    run_suite "$BASELINE_REPO"  "$LOG_DIR/baseline_$i.log"
  fi
done

exec python3 "$TESTS_DIR/verifier.py"
