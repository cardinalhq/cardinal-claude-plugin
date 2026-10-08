#!/usr/bin/env python3
"""cardinal owner input — UserPromptSubmit: record what the owner typed into
the session's Investigation, before the model sees the turn.

Inert unless the Cardinal server asks for it: a prompt is recorded only
when ALL of these hold (cardinal_core.owner_input.enabled plus this hook's
own checks):
  - the session's binding (~/.cardinal/investigations/sessions/<sid>.json)
    says it authors its Investigation (is_author true; a joined
    investigation never gets the joiner's prompts);
  - the server advertised capabilities.owner_input.enabled: true (stored in
    the binding by the SessionStart bootstrap, refreshed once per session
    start); an older Cardinal, or the capability absent or off: nothing is
    captured, sent or queued;
  - the kill switch is not set (CARDINAL_OWNER_INPUT=0 / false / off / no,
    in the environment, ~/.claude/settings.json `env` or the project's
    .claude/settings(.local).json `env`; _owner_input.py);
  - the user has been told: the binding's owner_input_disclosed. If not yet
    (the capability arrived after SessionStart: a bootstrap the poller
    completed, a refresh mid-session), this prompt is NOT captured; the hook
    records the disclosure and shows it to the user as a systemMessage, and
    capture starts with the next prompt;
  - the prompt is not a task notification (_owner_input.TASK_NOTIFICATION_RE)
    and not empty, and the machine is connected (hooks/_connection.py).

Contract:
  - Input on stdin: Claude Code's UserPromptSubmit payload {session_id,
    prompt, ...}.
  - Output: nothing, except once per session the disclosure as one JSON
    object {"systemMessage": ...} (shown to the user; not model context, not
    a block). Never plain text (plain stdout of UserPromptSubmit becomes
    context).
  - Otherwise cardinal_core.owner_input.submit: the prompt scrubbed of
    credentials, NUL stripped, cut to 32 KiB, with prompt_sha256 the sha256
    of the scrubbed text (never of the original); posted to
    record-owner-input within a hard ~1.5 s wall-clock bound (the session's
    outbox of earlier transient failures first). A timeout or a transient
    failure (network, 5xx, 429) queues it in
    <sid>.owner-input-outbox.json (0600, bounded), flushed by the next
    prompt or at Stop (investigation-events.py). 404 (an older Cardinal or
    owner_input_disabled), 400, 401, 403: dropped, never queued; the code
    alone is logged.
  - The debug log (CARDINAL_HOOK_DEBUG) gets a status code only, never the
    prompt.
  - No agent-facing path writes owner input: no MCP tool and no
    cardinal-storyboard subcommand; this hook is the only writer.
  - Recorded as source user_prompt whatever produced the prompt: one the
    user typed, a machine-generated one (/loop, a scheduled task, `claude
    -p`), and one another parallel UserPromptSubmit hook blocks (hooks run
    in parallel, so this one cannot know).
  - Fail open: never blocks the prompt, always exits 0.
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
import _owner_input  # noqa: E402


def main() -> str:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return "bad_payload"
    if not isinstance(payload, dict):
        return "bad_payload"
    sid = payload.get("session_id")
    home = Path(os.environ.get("HOME") or str(Path.home()))
    sessions = home / ".cardinal" / "investigations" / "sessions"
    if not isinstance(sid, str) or not sid or "/" in sid or not (sessions / f"{sid}.json").exists():
        return "unbound"  # no Python-heavy work, no network for an unbound session
    if _owner_input.disabled(payload.get("cwd")):
        from cardinal_core import owner_input
        owner_input.drop_outbox(home, sid)
        owner_input.forget_disclosure(home, sid)   # removing the switch tells the user again first
        return "disabled"
    if not _connection.is_connected():
        return "unconnected"
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "no_prompt"
    if _owner_input.is_task_notification(prompt):
        return "task_notification"
    from cardinal_core import investigation_events as ie
    from cardinal_core import owner_input
    if not ie.valid_session(sid):
        return "invalid"
    b = ie.read_binding(home, sid)
    if not owner_input.enabled(b):
        owner_input.drop_outbox(home, sid)
        return "off"
    if not owner_input.disclosed(b):
        # Tell the user before anything is captured; this prompt is not.
        text = owner_input.mark_disclosed(home, sid)
        if not text:
            return "off"
        sys.stdout.write(json.dumps({"systemMessage": _owner_input.disclosure_message(text)}))
        sys.stdout.flush()
        return "disclosed"
    import _storyboard_discovery
    return owner_input.submit(home, sid, prompt, _storyboard_discovery.connection(),
                              _storyboard_discovery.client_header(), started=START)


if __name__ == "__main__":
    status = "error"
    try:
        status = main()
    except Exception:
        pass
    try:
        import _hook_debug
        _hook_debug.log("owner-input", START, status=status)
    except Exception:
        pass
    sys.exit(0)
