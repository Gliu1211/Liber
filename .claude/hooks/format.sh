#!/usr/bin/env bash
# PostToolUse hook (ARCHITECTURE.md §15): auto-format edited Python files. Never blocks.
INPUT="$(cat)"
FILE="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)"
case "$FILE" in
  *.py) (cd "${CLAUDE_PROJECT_DIR:-.}" && uv run --quiet ruff format "$FILE" >/dev/null 2>&1) ;;
esac
exit 0
