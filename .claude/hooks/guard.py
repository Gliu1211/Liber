"""PreToolUse guard (ARCHITECTURE.md §15, principle 9: least privilege).

Blocks (exit code 2, reason fed back to the agent):
- edits to protected files: the eval baseline, env files, the harness itself, the lockfile;
- destructive shell commands: force pushes, hard resets, rm -rf outside the repo,
  and reading env files from the shell.

Why a hook instead of a rule in CLAUDE.md: a hook is deterministic and can't be forgotten.
"""

import json
import re
import sys
from pathlib import PurePath

PROTECTED_FILES = [
    re.compile(r"(^|/)evals/baseline\.json$"),  # baseline changes are a separate, human-approved PR
    re.compile(r"(^|/)\.env(\..+)?$"),          # secrets
    re.compile(r"(^|/)\.claude/"),              # the harness can't edit its own guardrails
    re.compile(r"(^|/)uv\.lock$"),              # change deps with `uv add`, not by hand
    re.compile(r"(^|/)evals/budgets\.json$"),  # reserved for absolute limits; owner-only
]
ALLOWED_ENV_FILES = re.compile(r"(^|/)\.env\.example$")

DANGEROUS_COMMANDS = [
    (re.compile(r"git\s+push\b.*(--force\b|-f\b|--force-with-lease)"), "force push"),
    (re.compile(r"git\s+reset\s+--hard"), "hard reset"),
    (re.compile(r"git\s+clean\s+-[a-z]*f"), "git clean -f"),
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\.\.)"), "recursive delete outside the repo"),
    (re.compile(r"(cat|less|more|head|tail|grep|source|\.)\s+[^|;&]*\.env(\s|$|\.(?!example))"), "reading an env file"),
    (re.compile(r"make\s+eval-baseline"), "rewriting the eval baseline (owner-only)"),
]


def block(reason: str) -> None:
    print(f"Blocked by guard.py: {reason}. See ARCHITECTURE.md §15.", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)  # never block on a malformed event; fail open for non-security parsing errors

    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    if tool in {"Edit", "Write", "MultiEdit"}:
        path = PurePath(str(tool_input.get("file_path", ""))).as_posix()
        if ALLOWED_ENV_FILES.search(path):
            sys.exit(0)
        for pattern in PROTECTED_FILES:
            if pattern.search(path):
                block(f"{path} is a protected file")

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for pattern, label in DANGEROUS_COMMANDS:
            if pattern.search(command):
                block(label)

    sys.exit(0)


if __name__ == "__main__":
    main()
