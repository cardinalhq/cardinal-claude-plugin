#!/usr/bin/env python3
"""cardinal storyboard discovery — SessionStart + UserPromptSubmit hook.

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
  - Output: hookSpecificOutput.additionalContext with at most 3 storyboards
    and 2 KB, framed as DATA written by org members, not instructions, and
    pointing at storyboard__get. Nothing when nothing matches.
  - Silent no-op (exit 0, no output, no network) when not connected, when
    the connection has no MCP key (telemetry-only), outside a git repo with
    an origin, or with CARDINAL_STORYBOARD_DISCOVERY=0.
  - Bounded: the network work has a hard 2 s deadline (hooks.json timeout 3
    is the backstop). Never blocks the prompt, never prints an error.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402
import _storyboard_discovery  # noqa: E402

EVENTS = ("SessionStart", "UserPromptSubmit")


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

    block = _storyboard_discovery.discover(cwd, session_id=sid, event=event)
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
