#!/usr/bin/env python3
"""cardinal owner input — UserPromptSubmit: record what the owner typed into
the session's Investigation, before the model sees the turn.

A thin wrapper over cardinal_core.owner_input.capture, the gate every
runtime shares (runtime "claude"); this hook adds only Claude's channels:
the unbound fast path, the connection check and the systemMessage.

Inert unless the Cardinal server asks for it: a prompt is recorded only
when ALL of these hold:
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
    shows the disclosure to the user as a systemMessage and records it only
    once that write was flushed (a failed write discloses again on the next
    prompt), and capture starts with the next prompt;
  - the prompt is not a task notification (owner_input.MACHINE_PROMPTS)
    and not empty, the payload names no subagent (agent_id), and the
    machine is connected (hooks/_connection.py).

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
    import _storyboard_discovery
    from cardinal_core import owner_input
    got = owner_input.capture(home, sid, payload.get("prompt"), runtime="claude",
                              conn=_storyboard_discovery.connection(), client=_storyboard_discovery.client_header(),
                              environ=os.environ, agent_id=payload.get("agent_id"),
                              deadline=START + owner_input.BUDGET, cwd=payload.get("cwd"), mark_disclosure=False)
    if got.disclosure:
        # Tell the user before anything is captured; this prompt is not. It
        # counts as told only once the message was flushed (a failed write
        # raises here and leaves it unrecorded).
        sys.stdout.write(json.dumps({"systemMessage": got.disclosure}))
        sys.stdout.flush()
        owner_input.mark_disclosed(home, sid)
    return got.status


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
