#!/usr/bin/env python3
"""cardinal storyboard discovery — SessionStart + UserPromptSubmit + SubagentStart hook.

Puts the Cardinal storyboards this work already has (same PR, same branch,
same directory below the repo root) in Claude's context, so a review,
a debugging session or resumed work starts from what the org already
established. Core logic: cardinal_core.storyboard_discovery (harness-neutral);
this adapter's wiring: hooks/_storyboard_discovery.py.

Contract:
  - Input on stdin: Claude Code's hook payload {session_id, cwd,
    hook_event_name, ...}.
  - SessionStart (startup, resume, clear, compact) always looks.
    UserPromptSubmit looks again only when the branch or HEAD changed since
    this session's last look (one git call otherwise, no network). Every
    look is remembered, failures included, so an unreachable maestro costs
    nothing on later prompts.
  - SubagentStart: a subagent (a forked skill, an Agent/Task call) starts
    without the session's SessionStart context, so the block the session's
    last look rendered (stored next to its branch/HEAD) is emitted again for
    the subagent. No git call, no network. Nothing when no block is stored
    (no look yet, nothing matched last time, no session id).
  - Output: hookSpecificOutput {hookEventName, additionalContext} with at
    most 3 storyboards and 2 KB, framed as DATA written by org members, not
    instructions, and pointing at storyboard__get. Draft acts' statements are
    inlined marked "[draft, not yet checked]". Nothing when nothing matches.
  - Silent no-op (exit 0, no output, no network) when not connected, when
    the connection has no MCP key (telemetry-only), outside a git repo with
    an origin, or with CARDINAL_STORYBOARD_DISCOVERY=0.
  - Bounded, from INTERPRETER START (imports and settings reads count: when
    several sessions start at once they alone can take most of a second): the
    network work ends by STARTED + BUDGET_S (at least MIN_NETWORK_S after the
    imports, never past STARTED + HARD_CAP_S). hooks.json's timeout (6) is
    the backstop, well clear of HARD_CAP_S. A block ready after DELIVER_BY_S
    is not printed (Claude Code may have stopped listening): it is stored as
    pending and the next prompt emits it, so the session and its subagents
    never disagree. Never blocks the prompt, never prints an error.
"""

from __future__ import annotations

import time

# Before any other import: the budget is measured from here.
STARTED = time.monotonic()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402
import _storyboard_discovery  # noqa: E402

EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart")

# Seconds from process start. hooks.json's timeout for this hook is 6.
BUDGET_S = 2.1
MIN_NETWORK_S = 1.0
HARD_CAP_S = 4.0
# Before hooks.json's 6 with room for what the clock cannot see: the
# interpreter's own startup before STARTED, and record_run + print after the
# late check.
DELIVER_BY_S = 4.5


def deadlines(now: float, started: float = STARTED) -> tuple:
    """(deadline, deliver_by) as absolute time.monotonic() values: the
    network work ends by started + BUDGET_S, or now + MIN_NETWORK_S when the
    startup ran late, but never after started + HARD_CAP_S."""
    deadline = min(max(started + BUDGET_S, now + MIN_NETWORK_S), started + HARD_CAP_S)
    return deadline, started + DELIVER_BY_S


def main() -> None:
    # Env and local-file checks first: no subprocess, no network.
    if _storyboard_discovery.is_disabled():
        return
    if not _connection.is_connected():
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return
    cwd = payload.get("cwd")
    if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
        cwd = os.getcwd()
    sid = payload.get("session_id")
    sid = sid if isinstance(sid, str) and sid else None

    deadline, deliver_by = deadlines(time.monotonic())
    block = _storyboard_discovery.discover(cwd, session_id=sid, event=event, deadline=deadline,
                                           deliver_by=deliver_by)
    if not block:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": block,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    # An abandoned request thread (the deadline passed) must not hold the
    # process open.
    os._exit(0)
