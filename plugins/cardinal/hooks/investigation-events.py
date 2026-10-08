#!/usr/bin/env python3
"""cardinal investigation events — delivery for a session bound to an
Investigation (every connected session is: SessionStart bootstraps it).
Started only by investigation-events.sh, and only when this session's
background poller left deliverable events in its inbox, or at Stop.

At a tool boundary (PostToolUse, or PostToolUseFailure so a run of failing
tools still delivers): renders the inbox — the deliverable events the poller
fetched (cue / question / challenge addressed to this session or to
everyone, not its own, not acknowledged) — as
hookSpecificOutput.additionalContext, rendered by
cardinal_core.investigation_events (producer, provenance, authority
ADVISORY, the text as one JSON string, the ack command). Then the cursor
advances past everything the inbox held and the inbox is removed. No
network at a tool boundary.

A subagent's tool call (the payload carries a non-empty agent_id; its
session_id is the parent's) does nothing: no output, the inbox and the
cursor are not touched, so the event waits for the main thread's next tool
boundary or Stop instead of landing in the subagent's context.

At Stop (the backstop for events that land during the final turn): the
inbox if there is one; else, unless the poller read the events successfully
within the last few seconds, one synchronous check (read-investigation-events
after the cursor, 1.5 s per request). Deliverable events block the stop
({"decision": "block", "reason": rendered}), at most 3 consecutive times
(stop_hook_active).

Also at Stop, after delivery (never at a tool boundary, and never in the
poller, which stays read-only): this session's owner input outbox
(<sid>.owner-input-outbox.json: owner prompts owner-input.py could not
record because of a transient failure) is posted, oldest first, within what
is left of a 2.6 s budget (cardinal_core.owner_input.flush), only while the
server advertises the owner_input capability and the session authors its
investigation; otherwise, or when the kill switch is set
(CARDINAL_OWNER_INPUT=0, _owner_input.py), the outbox is deleted. It writes
nothing to stdout.

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

    def emit(text: str) -> None:
        if event == "Stop":
            out = {"decision": "block", "reason": text}
        else:
            out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()

    stop = event == "Stop"
    try:
        deliver(home, sid, ie, emit, event, payload)
    finally:
        if stop:
            flush_owner_input(home, sid, payload.get("cwd"))


STOP_BUDGET = 2.6   # hooks.json gives the hook 3 s


def flush_owner_input(home: Path, sid: str, cwd=None) -> None:
    """Post the session's queued owner input with what is left of the
    budget, or delete it when owner input is off. Never raises."""
    try:
        from cardinal_core import owner_input
        if not owner_input.outbox_path(home, sid).exists():
            return
        import _owner_input
        if _owner_input.disabled(cwd):
            owner_input.drop_outbox(home, sid)
            owner_input.forget_disclosure(home, sid)
            return
        deadline = START + STOP_BUDGET
        if deadline - time.monotonic() < owner_input.MIN_REQUEST:
            return
        import _storyboard_discovery
        owner_input.flush(home, sid, _storyboard_discovery.connection(), _storyboard_discovery.client_header(),
                          deadline=deadline)
    except Exception:
        pass


def deliver(home: Path, sid: str, ie, emit, event: str, payload: dict) -> None:
    stop = event == "Stop"
    stop_hook_active = payload.get("stop_hook_active") is True
    if ie.deliver_inbox(home, sid, emit, stop=stop, stop_hook_active=stop_hook_active) is not None:
        return
    if not stop:
        return  # nothing waiting: a tool boundary never reads the network
    from cardinal_core import investigation_poller as ip
    if ip.fresh(home, sid):
        return  # the poller read the events moments ago and found nothing deliverable
    import _storyboard_discovery
    conn = _storyboard_discovery.connection()
    client = _storyboard_discovery.client_header()
    ie.check(home, sid, conn, client, emit, stop=True, stop_hook_active=stop_hook_active,
             budget=max(0.5, ie.CHECK_BUDGET - (time.monotonic() - START)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
