#!/usr/bin/env python3
"""cardinal initiative convention — SessionStart hook.

Tells Claude the branch-naming convention Cardinal uses to attribute
agent spend to initiatives. Every session in a git repo sees this
prompt so that when Claude cuts a new branch during the conversation,
the branch name produces a clean (initiative_name, initiative_type)
classification in the Outcomes Dashboard.

Contract:
  - Input on stdin: Claude Code's SessionStart hook JSON payload
    {session_id, cwd, hook_event_name, source, ...}.
  - Output: JSON on stdout with hookSpecificOutput.additionalContext
    when cwd is inside a git repo. Otherwise exits silently with no
    output (there's no branch to advise on).
  - Best-effort: any failure exits 0 silently. Never blocks session
    start.

Why a hook (not just README): Claude only sees what's in its context.
A README in the plugin repo doesn't reach the session running in a
different repo. SessionStart additionalContext is the surface Claude
Code provides for "tell the model this on every session" — short,
authoritative, in-context.

The convention text and the session-start budget standing both come
from cardinal_core (session.convention_prompt / session.budget_standing);
the ingest key lives in Claude's OTel settings, not cardinal-secrets.json,
and core 0.2.0 takes it as an argument.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
from cardinal_core.initiative import is_git_repo  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402
from cardinal_core.session import budget_standing, convention_prompt  # noqa: E402

PROMPT = convention_prompt("Claude Code")

# Bound at import time (hooks are one process per invocation).
PATHS = AgentPaths(home=Path.home() / ".claude")


def _budget_standing(payload: dict, cwd: str) -> str | None:
    """core session.budget_standing (one synchronous fetch at session
    start, fail open, warm-writes the verdict file the per-turn gate
    reads) with Claude's payload/env session-id sourcing and OTel-settings
    key sourcing. Spec: conductor docs/specs/agent-spend-limits.md
    §Delivery."""
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    return budget_standing(
        PATHS, session_id, cwd, api_key=_otel_settings.ingest_api_key()
    )


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

    if not is_git_repo(cwd):
        # Outside a git repo there's no branch to advise on; suppress
        # the prompt to avoid wasted context.
        sys.exit(0)

    context = PROMPT
    try:
        standing = _budget_standing(payload, cwd)
        if standing:
            context = f"{PROMPT}\n\n{standing}"
    except Exception:
        # Budget standing is additive — never let it cost the convention
        # prompt (or session start).
        pass

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
