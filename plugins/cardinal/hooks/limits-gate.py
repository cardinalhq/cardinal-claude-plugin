#!/usr/bin/env python3
"""cardinal limits gate — UserPromptSubmit (SYNC).

Reads the spend-limit verdict that git-state.py's async fetch cached at
~/.claude/cardinal/limits/<session>.verdict.json and turns it into hook
output. This hook NEVER touches the network — it is on the turn-critical
path, so its budget is one small file read. The verdict it acts on is at
most one turn + the server TTL stale, which the 75/90% threshold margins
absorb (conductor docs/specs/agent-spend-limits.md §Delivery).

The severity → channel mapping, anti-nag hysteresis (ack file), and
override downgrade all live in cardinal_core.limits.gate_output; this
script is just Claude Code's stdin/stdout spelling around it.

Fail open, always: missing/corrupt/stale verdict → exit 0 with no output.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core.limits import gate_output  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402

# Bound at import time (hooks are one process per invocation).
PATHS = AgentPaths(home=Path.home() / ".claude")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        sys.exit(0)

    out = gate_output(
        PATHS, session_id, hook_event_name="UserPromptSubmit"
    )
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
