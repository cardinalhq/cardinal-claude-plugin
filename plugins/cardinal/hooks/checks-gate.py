#!/usr/bin/env python3
"""cardinal checks gate — the Checks preview opt-in, enforced.

Two events, one script (dispatch on hook_event_name):

  SessionStart: makes skills/checks/ match `cardinal-checks on|off`
    (_checks.reconcile: a plugin update installs a fresh version directory
    without the copy; `off` in another session leaves a copy here). When it
    changed anything, returns reloadSkills so this session lists (or stops
    listing) the skill. While the preview is on and Cardinal is connected,
    adds the routing rule: answer with queries first, propose a check only for
    per-trace judgments. Off: no routing text at all.

  PreToolUse (matcher Skill): while the preview is off, denies the Skill tool
    for the checks skill. Plugin skill directories are not watched, so a
    session that listed the skill before `off` would otherwise still be able
    to invoke it. Any other skill, or the preview on: no output.

Fail open for everything except the deny: an exception prints nothing.
Always exits 0.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _checks  # noqa: E402

ROUTING_RULE = (
    "Cardinal Checks preview is on. For telemetry questions, answer with the Cardinal query tools first. "
    "Use the checks skill only when the answer needs a judgment about every trace: relationships between "
    "events inside one trace, reading what was said, or a rule the user wants to reuse. Never build a "
    "check for something one aggregate query answers (counts, rates, top endpoints, latency); answer it."
)
DENY_REASON = (
    "The Cardinal Checks preview is off on this machine, so the checks skill is unavailable. Answer with the "
    "ordinary Cardinal query tools, or tell the user that per-trace checks are not enabled "
    "(`cardinal-checks on` turns the preview on)."
)


def session_start(payload: dict) -> dict | None:
    try:
        changed = _checks.reconcile()
    except OSError:
        changed = False
    out: dict = {"hookEventName": "SessionStart"}
    if changed:
        out["reloadSkills"] = True
    if _checks.is_enabled():
        try:
            import _connection
            connected = _connection.is_connected()
        except Exception:
            connected = False
        if connected:
            out["additionalContext"] = ROUTING_RULE
    return {"hookSpecificOutput": out} if len(out) > 1 else None


def pre_tool_use(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Skill":
        return None
    tool_input = payload.get("tool_input")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    if not isinstance(tool_input, dict) or not _checks.is_checks_skill(tool_input.get("skill"), cwd):
        return None
    if _checks.is_enabled():
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": DENY_REASON}}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    out = session_start(payload) if event == "SessionStart" else pre_tool_use(payload) if event == "PreToolUse" else None
    if out:
        sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
