#!/usr/bin/env python3
"""cardinal owner relay — UserPromptSubmit and Stop: the owner decides, in
this (supervisor) session's own terminal, for the worker it supervises
(cardinal_core.owner_relay; docs/specs/relayed-owner-decisions.md).

The forms: `decide: <text>` / `decide <name>: <text>` (the owner's own
words, to the registered worker target) and `approve [dp_…]` / `reject
[dp_…]` (a draft made with `cardinal-storyboard investigation
propose-decision`). This hook is the only caller of maestro's
relay-owner-decision: no CLI subcommand and no MCP tool relays.

Claude Code also fires UserPromptSubmit for prompts no person typed (a
scheduled task, /loop, a peer message), and the payload does not say which;
the transcript does, on the user entry whose promptId is the payload's
prompt_id, but only after this hook returns. So on UserPromptSubmit nothing
is sent: a matching prompt is recorded as pending and a detached verifier
(owner_relay.verify) confirms from the transcript that a person typed it
before acting. The owner is told it is being confirmed; the outcome is
shown when the turn ends (`owner-relay.py stop`, waiting at most ~3 s for
it) or with the next prompt, exactly once (systemMessage), and given to the
model with the next prompt (additionalContext), labelled with the prompt
it answers. A prompt the limits gate blocks is not acted on and takes no
outcome (Claude Code shows nothing from this hook on a blocked prompt).

The first prompt of a session runs before Claude Code creates the
transcript: the verifier waits for the file too. A peer session's message
reaches this hook as bare text; only its transcript entry says peer.

Independent of owner-input capture: it runs whether or not owner input is
on. Refused: a subagent's prompt (agent_id) and a task notification.

Contract:
  - Input on stdin: Claude Code's UserPromptSubmit payload {session_id,
    prompt, prompt_id, transcript_path, agent_id?, ...}, or with `stop` the
    Stop payload {session_id, ...}.
  - Output: nothing, or ONE JSON object: systemMessage (shown to the user)
    and, on UserPromptSubmit, hookSpecificOutput.additionalContext. The
    prompt is never blocked.
  - The debug log (CARDINAL_HOOK_DEBUG) gets a status code only.
  - Fail open: always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402

# A cheap pre-check before anything is imported: the core matches the
# normalized prompt exactly (owner_relay.APPROVAL_RE / DECIDE_RE). A session
# with relay state (<sid>.proposals.json) passes every prompt to the core,
# which notes it (freshness) and shows outcomes not shown yet.
_MAYBE = re.compile(r"\s*(?:approve|reject|decide)\b", re.IGNORECASE)


def _limits_blocked(sid: str) -> bool:
    """Whether the limits gate (a parallel UserPromptSubmit hook) blocks this
    prompt: then nothing is acted on."""
    try:
        from cardinal_core.limits import gate_decision
        from cardinal_core.paths import AgentPaths
        d = gate_decision(AgentPaths(home=Path.home() / ".claude"), sid)
        return d is not None and d.tier == "block"
    except Exception:
        return False


def main(stop: bool) -> str:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return "bad_payload"
    if not isinstance(payload, dict):
        return "bad_payload"
    sid = payload.get("session_id")
    prompt = payload.get("prompt")
    if not isinstance(sid, str) or not sid or "/" in sid or (not stop and not isinstance(prompt, str)):
        return "invalid"
    home = Path(os.environ.get("HOME") or str(Path.home()))
    store = home / ".cardinal" / "investigations" / "sessions" / f"{sid}.proposals.json"
    if not store.exists() and (stop or not _MAYBE.match(prompt)):
        return "no_match"   # nothing imported: not a decision, and this session supervises nothing
    from cardinal_core import owner_relay
    if stop:
        shown = owner_relay.at_stop(home, sid)
        if shown:
            sys.stdout.write(json.dumps({"systemMessage": shown}))
            sys.stdout.flush()
        return "stop:shown" if shown else "stop"
    import _storyboard_discovery
    connected = True
    if not _connection.is_connected():
        connected = False   # no network; the owner is still told why nothing was sent
    conn = _storyboard_discovery.connection() if connected else {}
    client = _storyboard_discovery.client_header()
    got = owner_relay.handle_prompt_deferred(home, sid, prompt, prompt_id=payload.get("prompt_id"),
                                             transcript_path=payload.get("transcript_path"), conn=conn,
                                             client=client, agent_id=payload.get("agent_id"),
                                             blocked=connected and _limits_blocked(sid))
    out: dict = {}
    if got.message:
        out["systemMessage"] = got.message
    if got.context:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": got.context}
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return f"{got.status}:{got.code}" if got.code else got.status


if __name__ == "__main__":
    status = "error"
    try:
        status = main(sys.argv[1:2] == ["stop"])
    except Exception:
        pass
    try:
        import _hook_debug
        _hook_debug.log("owner-relay", START, status=status)
    except Exception:
        pass
    sys.exit(0)
