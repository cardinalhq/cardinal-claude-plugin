#!/usr/bin/env python3
"""cardinal limits gate — UserPromptSubmit (SYNC).

Reads the spend-limit verdict that git-state.py's async fetch cached at
~/.claude/cardinal/limits/<session>.verdict.json and turns it into hook
output. This hook NEVER touches the network — it is on the turn-critical
path, so its budget is one small file read. The verdict it acts on is at
most one turn + the server TTL stale, which the 75/90% threshold margins
absorb (conductor docs/specs/agent-spend-limits.md §Delivery).

Severity → channel mapping (the server decides severity; we route it):

  decision=allow, band>0 (notify) → additionalContext only — the model
                                    quietly economizes, no user noise.
  decision=warn                   → additionalContext + systemMessage —
                                    recommendations like /clear or "split
                                    this PR" are HUMAN actions; the
                                    engineer must see them directly.
  decision=block                  → {"decision": "block", reason} — the
                                    turn is stopped; the reason names the
                                    setter and the override path.

Anti-nag hysteresis: warn/notify surface only when the threshold band
RISES (75 → 90 → 100); the last surfaced band lives in <session>.ack.json
(owned by this hook — single-writer, no races with the fetch side). A
block is enforced every turn while in force.

Fail open, always: missing/corrupt/stale verdict → exit 0 with no output.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _limits_common as lc  # noqa: E402


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        sys.exit(0)

    verdict = lc.read_verdict(session_id)
    if not verdict:
        sys.exit(0)

    decision = verdict.get("decision")
    try:
        band = int(verdict.get("band") or 0)
    except (TypeError, ValueError):
        band = 0

    fetched_at = verdict.get("fetched_at")
    age = (
        time.time() - fetched_at
        if isinstance(fetched_at, (int, float))
        else float("inf")
    )

    # Block: enforced every turn while in force and fresh enough. An
    # override file (written by /cardinal:override after the server logs
    # the override) downgrades it to warn-tier surfacing below.
    if decision == "block" and age <= lc.BLOCK_MAX_AGE_SEC:
        if not lc.override_path(session_id).exists():
            reason = (
                verdict.get("block_reason")
                or verdict.get("user_message")
                or "A Cardinal spend limit for this work has been reached."
            )
            _emit({"decision": "block", "reason": reason})
            sys.exit(0)
        decision = "warn"  # overridden: keep the human-visible standing

    # Warn / notify tiers fail open past the staleness window.
    if band <= 0 or age > lc.WARN_MAX_AGE_SEC:
        sys.exit(0)

    # Hysteresis: only speak when the band has risen since we last did.
    ack = lc._read_json_file(lc.ack_path(session_id))
    try:
        last_band = int(ack.get("band") or 0)
    except (TypeError, ValueError):
        last_band = 0
    if band <= last_band:
        sys.exit(0)

    out: dict = {}
    agent_context = verdict.get("agent_context")
    if isinstance(agent_context, str) and agent_context:
        out["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": agent_context,
        }
    user_message = verdict.get("user_message")
    if decision == "warn" and isinstance(user_message, str) and user_message:
        out["systemMessage"] = user_message

    if out:
        _emit(out)
        lc.atomic_write_json(
            lc.ack_path(session_id), {"band": band, "surfaced_at": time.time()}
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
