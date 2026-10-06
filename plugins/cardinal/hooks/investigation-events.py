#!/usr/bin/env python3
"""cardinal investigation events — delivery for a session bound to an
Investigation. Started only by investigation-events.sh (the PostToolUse,
PostToolUseFailure and Stop hook), and only when this session has a binding
file.

At a tool boundary (PostToolUse, or PostToolUseFailure so a run of failing
tools still delivers): reads the investigation's events after
this session's cursor (read-investigation-events, to_session_id = this
session; one request normally, 1.5 s timeout), keeps the deliverable ones
(cue / question / challenge addressed to this session or to everyone, not
its own, not acknowledged) and returns them as
hookSpecificOutput.additionalContext, rendered by
cardinal_core.investigation_events (producer, provenance, authority
ADVISORY, the text as one JSON string, the ack command). Then the cursor
advances past everything fetched. Nothing deliverable: no output at all.
Throttled to one check per second (parallel tool calls).

A subagent's tool call (the payload carries a non-empty agent_id; its
session_id is the parent's) does nothing: no output and the cursor is not
touched, so the event waits for the main thread's next tool boundary or
Stop instead of landing in the subagent's context.

At Stop (the backstop for events that land during the final turn): the
same check; deliverable events block the stop ({"decision": "block",
"reason": rendered}), at most 3 consecutive times (stop_hook_active).

Fail open: any error (not connected, network, server, lock busy) means no
output, exit 0, cursor unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    sid = payload.get("session_id")
    event = payload.get("hook_event_name")
    if event not in ("PostToolUse", "PostToolUseFailure", "Stop"):
        return
    if payload.get("agent_id"):
        return  # a subagent's tool call: leave the event for the main thread
    from cardinal_core import investigation_events as ie
    home = Path(os.environ.get("HOME") or str(Path.home()))
    if not ie.valid_session(sid) or ie.read_binding(home, sid) is None:
        return
    if not _connection.is_connected():
        return
    import _storyboard_discovery
    conn = _storyboard_discovery.connection()
    client = _storyboard_discovery.client_header()

    def emit(text: str) -> None:
        if event == "Stop":
            out = {"decision": "block", "reason": text}
        else:
            out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()

    ie.check(home, sid, conn, client, emit, stop=event == "Stop",
             stop_hook_active=payload.get("stop_hook_active") is True,
             budget=max(0.5, ie.CHECK_BUDGET - (time.monotonic() - START)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
