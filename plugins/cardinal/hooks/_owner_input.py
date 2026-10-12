"""Shared by the owner-input hooks: Claude's names for the core owner-input
gate (cardinal_core.owner_input), which every runtime shares.

is_task_notification: Claude Code re-enters UserPromptSubmit with a
synthetic body when a background task finishes ("<task-notification>…" or
"task-notification …"); that is not a turn the owner took, so it is never
recorded as owner input (owner_input.MACHINE_PROMPTS["claude"]).
decision-prompt.py keeps its own, narrower check (unchanged).

Kill switch (disabled; owner_input.kill_switch for runtime "claude"):
CARDINAL_OWNER_INPUT set to 0 / false / off / no (any JSON type: `false` and
`0` count) in ANY of: the hook's environment, ~/.cardinal/owner-input.json
{"claude": "off"}, ~/.claude/settings.json `env`, or the project's
.claude/settings.json / .claude/settings.local.json `env` (the project is
the hook payload's cwd, and $CLAUDE_PROJECT_DIR when Claude Code passes it).
Any one of them turns owner input off on this machine whatever the server
advertises: nothing is captured, the outbox is deleted instead of flushed at
Stop, and nothing says prompts are recorded. The reliable place is
~/.claude/settings.json `env`: a repo's settings `env` can override a shell
export. Never raises.

Disclosure (disclosure_message): the user-visible systemMessage that must
precede capture (cardinal_core.owner_input.mark_disclosed). Claude Code
parses a hook's stdout as JSON when it is one JSON object and the hook
exits 0: `systemMessage` is shown to the user, is not added to the model's
context and does not block the prompt (only `decision: "block"` would).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RUNTIME = "claude"
# cardinal_core is imported only when a function is called: owner-input.py
# imports this module before it knows the session is bound, and an unbound
# session's prompt does no Python-heavy work.


def home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def is_task_notification(prompt) -> bool:
    from cardinal_core import owner_input
    return owner_input.is_machine_prompt(RUNTIME, prompt)


def disabled(cwd=None) -> bool:
    """The kill switch is set (any place counts; cannot tell: True)."""
    try:
        from cardinal_core import owner_input
    except Exception:
        return True   # cannot tell: off, never capture
    return owner_input.kill_switch(RUNTIME, home(), os.environ, cwd=cwd if isinstance(cwd, str) else None)


def disclosure_message(text: str) -> str:
    """The user-visible disclosure, with how to turn it off."""
    from cardinal_core import owner_input
    return owner_input.disclosure_message(RUNTIME, text)
