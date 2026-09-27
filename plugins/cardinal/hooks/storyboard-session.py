#!/usr/bin/env python3
"""cardinal storyboard session id — SessionStart hook.

Puts this Claude Code session's id in Claude's context so the storyboard
skill can pass it to `storyboard__create` as `session_id` (conductor
docs/specs/investigation-storyboards.md §15). maestro stores it on the
storyboard row only; receipts do not carry it.

Contract:
  - Input on stdin: Claude Code's SessionStart payload {session_id, ...}.
    Falls back to $CLAUDE_CODE_SESSION_ID / $CLAUDE_SESSION_ID.
  - Output: hookSpecificOutput.additionalContext with one sentence, in any
    directory (a storyboard does not need a git repo). SessionStart also
    fires on resume / clear / compact, so the id survives compaction.
  - Silent when there is no id, or the id is not one maestro accepts
    (^[A-Za-z0-9_-]{1,128}$, routes/storyboards-mcp-tools.ts CreateSchema).
  - Fail open: never blocks or delays session start, never prints an error.
"""

from __future__ import annotations

import json
import os
import re
import sys

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def session_id(payload: dict) -> str | None:
    for value in (
        payload.get("session_id"),
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ):
        if isinstance(value, str) and SESSION_ID_RE.match(value):
            return value
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    sid = session_id(payload)
    if not sid:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"Cardinal session id for this session: {sid}. "
                "Pass it as session_id to storyboard__create."
            ),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
