#!/usr/bin/env bash
# Stop hook (ARCHITECTURE.md §15): runs `make check` when the agent tries to finish.
#   checks pass      -> allow the stop (exit 0) and reset the counter
#   checks fail      -> block the stop (exit 2); stderr is fed back to the agent
#   3 blocks in a row-> allow the stop anyway (exit 1, warning to the user),
#                       so the agent can never loop forever
set -uo pipefail

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id","default"))' 2>/dev/null || echo default)"
COUNTER_FILE="${TMPDIR:-/tmp}/liber-stop-${SESSION_ID}"
MAX_BLOCKS=3

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Nothing to check yet (before the scaffold exists): don't block.
[ -f Makefile ] && [ -f pyproject.toml ] || exit 0

OUTPUT="$(make check 2>&1)"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  rm -f "$COUNTER_FILE"
  exit 0
fi

COUNT="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

if [ "$COUNT" -gt "$MAX_BLOCKS" ]; then
  echo "make check still fails after ${MAX_BLOCKS} fix attempts; stopping anyway. Say so clearly in docs/PROGRESS.md and the PR." >&2
  rm -f "$COUNTER_FILE"
  exit 1
fi

{
  echo "make check failed (attempt ${COUNT}/${MAX_BLOCKS}). Diagnose from the output below, fix, and run make check again before stopping."
  echo "----- last 60 lines -----"
  printf '%s\n' "$OUTPUT" | tail -n 60
} >&2
exit 2
