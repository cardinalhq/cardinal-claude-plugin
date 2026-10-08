"""Shared by the owner-input hooks: what is not a turn the owner took, the
owner-input kill switch, and the user-visible disclosure.

TASK_NOTIFICATION_RE: Claude Code re-enters UserPromptSubmit with a
synthetic body when a background task finishes ("<task-notification>…" or
"task-notification …"); that is not a turn the owner took, so it is never
recorded as owner input. decision-prompt.py keeps its own, narrower check
(unchanged).

Kill switch: CARDINAL_OWNER_INPUT set to 0 / false / off / no (any JSON
type: `false` and `0` count) in ANY of: the hook's environment,
~/.claude/settings.json `env`, or the project's .claude/settings.json /
.claude/settings.local.json `env` (the project is the hook payload's cwd,
and $CLAUDE_PROJECT_DIR when Claude Code passes it). Any one of them turns
owner input off on this machine whatever the server advertises: nothing is
captured, the outbox is deleted instead of flushed at Stop, and nothing
says prompts are recorded. The reliable place is ~/.claude/settings.json
`env`: a repo's settings `env` can override a shell export. Never raises.

Disclosure (disclosure_message): the user-visible systemMessage that must
precede capture (cardinal_core.owner_input.mark_disclosed). Claude Code
parses a hook's stdout as JSON when it is one JSON object and the hook
exits 0: `systemMessage` is shown to the user, is not added to the model's
context and does not block the prompt (only `decision: "block"` would).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TASK_NOTIFICATION_RE = re.compile(r"^\s*<?task-notification[\s>]", re.IGNORECASE)

KILL_ENV = "CARDINAL_OWNER_INPUT"
OFF_VALUES = ("0", "false", "off", "no")
OPT_OUT = "Set CARDINAL_OWNER_INPUT=0 to turn this off on this machine."


def is_task_notification(prompt) -> bool:
    return isinstance(prompt, str) and bool(TASK_NOTIFICATION_RE.match(prompt))


def _off(v) -> bool:
    return v is not None and not isinstance(v, (dict, list)) and str(v).strip().lower() in OFF_VALUES


def _settings_env(path: Path) -> dict:
    try:
        env = json.loads(path.read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def disabled(cwd=None) -> bool:
    """The kill switch is set (any place counts)."""
    try:
        if _off(os.environ.get(KILL_ENV)):
            return True
        home = Path(os.environ.get("HOME") or str(Path.home()))
        files = [home / ".claude" / "settings.json"]
        for d in {str(x) for x in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")) if isinstance(x, str) and x}:
            files += [Path(d) / ".claude" / "settings.json", Path(d) / ".claude" / "settings.local.json"]
        return any(_off(_settings_env(f).get(KILL_ENV)) for f in files)
    except Exception:
        return True   # cannot tell: off, never capture


def disclosure_message(text: str) -> str:
    """The user-visible disclosure, with how to turn it off."""
    return f"Cardinal: {text} {OPT_OUT}"
