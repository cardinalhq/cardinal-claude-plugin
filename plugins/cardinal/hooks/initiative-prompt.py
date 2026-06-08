#!/usr/bin/env python3
"""cardinal initiative prompt — SessionStart hook.

Adds a one-shot system-prompt extension instructing Claude to author a
.cardinal-initiative file at the repo root when none exists, so that
the git-state hook's resolution chain can pick it up on subsequent
turns. Silent when the file is already present (Claude doesn't need a
nudge for repos that already declared their initiative).

Contract (see conductor docs/specs/cardinal-initiative.md §"System-prompt
extension"):
  - Input on stdin: Claude Code's SessionStart hook JSON payload
    {session_id, cwd, hook_event_name, source, ...}.
  - Output: JSON on stdout with hookSpecificOutput.additionalContext
    when (and only when) cwd is a git repo with no .cardinal-initiative
    at the root. Otherwise exits silently with no output.
  - Best-effort: any failure exits 0 silently. Never blocks session
    start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# The system-prompt extension itself. Phrased so Claude treats the file
# like source: author only when intent is clear, do not edit an existing
# one without an explicit ask.
PROMPT = (
    "You are running inside a Cardinal-instrumented Claude Code session. "
    "Cardinal attributes agent spend to \"initiatives\" — coherent "
    "bodies of work that may span multiple PRs and contributors. The "
    "initiative for this session is determined by .cardinal-initiative "
    "at the repo root.\n\n"
    "On the FIRST turn of a session, after you understand the user's "
    "intent, check whether the repo has .cardinal-initiative. If it "
    "does not, and the conversation makes the intent clear, use the "
    "Write tool to create it as JSON:\n\n"
    "  {\n"
    "    \"name\": \"<kebab-case 1-4 segments>\",\n"
    "    \"description\": \"<1-3 sentences, present tense — why this "
    "matters for the org>\"\n"
    "  }\n\n"
    "Do not create the file speculatively. Do not modify an existing "
    "file without an explicit ask. The file is committed to the repo "
    "and reviewed like source code."
)


def _git_toplevel(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    cwd = (
        payload.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    root = _git_toplevel(cwd)
    if not root:
        # Not in a git repo — there's no "repo root" to author the file
        # at, so the nudge wouldn't help.
        sys.exit(0)

    if (Path(root) / ".cardinal-initiative").exists():
        # Already authored. Don't ask Claude to touch it.
        sys.exit(0)

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": PROMPT,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
