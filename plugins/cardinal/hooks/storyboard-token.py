#!/usr/bin/env python3
"""cardinal storyboard evidence token — PostToolUse hook on storyboard__create,
storyboard__add_act and storyboard__preview.

storyboard__create returns an `evidence_token`: a 24 h token that can upload
evidence for that one storyboard and nothing else (conductor
storyboard/scoped-tokens.ts; `Authorization: CardinalEvidence <token>` on
POST /api/orgs/<org>/storyboards/<id>/evidence). storyboard__add_act, which
opens the next act of a published storyboard, returns the same token block
for the same storyboard id. storyboard__preview returns a fresh one while an
act is open (a draft storyboard is its open act 1). This hook keeps it, so
`cardinal-evidence promote` can upload captured evidence without the org
API key.

Contract:
  - Input on stdin: Claude Code's PostToolUse payload {tool_name,
    tool_response, session_id, ...}. Only Cardinal's own server counts
    (mcp__cardinal__* or mcp__plugin_cardinal_cardinal__*: servers "cardinal"
    and "plugin_cardinal_cardinal", storyboard_authoring.is_cardinal_tool): a
    token another MCP server hands back is never stored, so no other server
    can point a later upload at its own storyboard.
  - tool_response: {content: "<result JSON>", structuredContent: {...}}, the
    bare result text, a list of text blocks, or Claude Code's spill notice
    (followed only under ~/.claude/projects/).
  - Reads storyboard_id, evidence_token, evidence_token_expires_at and the
    org: evidence_upload.path (/api/orgs/<org>/storyboards/<id>/evidence)
    on create and add_act; on preview a scene's preview_bundle.path, or the org already
    stored for that storyboard.
  - Writes ~/.cardinal/evidence/<session_id>/token.json (0600, directories
    0700, atomic), keyed by storyboard id (cardinal_core.evidence
    store_token). No output. No network. Nothing is stored while evidence
    capture is off (CARDINAL_EVIDENCE_CAPTURE=0 or the disabled flag file).
    Core logic, shared with the other runtimes:
    cardinal_core.storyboard_authoring.observe_cardinal_result.
  - Fail open: always exits 0, never blocks the tool, never prints an error.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RUNTIME = "claude"
# The tools whose result carries the token (hooks.json's matcher; the same
# set as storyboard_authoring.TOKEN_TOOLS).
TOOLS = ("storyboard__create", "storyboard__preview", "storyboard__add_act")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    from cardinal_core import storyboard_authoring as sa

    if not sa.is_cardinal_tool(RUNTIME, payload.get("tool_name"), TOOLS):
        return
    sa.observe_cardinal_result(sa.agent_paths(RUNTIME), RUNTIME, payload.get("tool_name"),
                               payload.get("tool_response"), session_id=payload.get("session_id"))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
