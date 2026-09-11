#!/usr/bin/env python3
"""cardinal decision_prompt hook — UserPromptSubmit.

When decision capture is on (`cardinal-decision on`), tells the agent to
record meaningful decisions with bin/cardinal-decision and lists this
session's decisions so far, so new ones can link to them (follows /
refines / supersedes). Silent when capture is off.

Contract: stdin is Claude Code's UserPromptSubmit JSON ({session_id,
prompt, ...}); stdout is hookSpecificOutput.additionalContext when
capture is on, else nothing. Sync (its output must reach this turn's
context) and cheap: two small JSON reads. NEVER blocks — every failure
exits 0 silently.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
from cardinal_core import decisions  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402

PATHS = AgentPaths(home=Path.home() / ".claude")
CLI = Path(__file__).resolve().parent.parent / "bin" / "cardinal-decision"

# Claude Code re-enters UserPromptSubmit with a synthetic
# "task-notification …" body when a background task finishes; that is
# not a turn the human took.
TASK_NOTIFICATION_RE = re.compile(r"^\s*task-notification\s", re.IGNORECASE)


def build_context(cli: str, session_id: str, entries: list[dict]) -> str:
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one Bash call:\n"
        f'"{cli}" record --session {session_id} --choice "<the option chosen, 2-7 words>" '
        '--question "<what had to be settled>" --why "<one sentence>" '
        '[--alt "<rejected option>"]... [--by user] [--anchor <path>[::Symbol]]... '
        "[--follows|--refines|--supersedes <id>]\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and TASK_NOTIFICATION_RE.match(prompt):
        return

    settings_env = _otel_settings.load_otel_settings()
    override = settings_env.get(decisions.ENABLE_ENV) or os.environ.get(decisions.ENABLE_ENV)
    if not decisions.is_enabled(PATHS.runtime_dir, override):
        return

    entries = decisions.read_ledger(PATHS.runtime_dir, session_id)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": build_context(str(CLI), session_id, entries),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
