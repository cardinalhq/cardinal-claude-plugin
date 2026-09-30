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
    (mcp__cardinal__* or mcp__plugin_cardinal_cardinal__*): a token another
    MCP server hands back is never stored, so no other server can point a
    later upload at its own storyboard.
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
  - Fail open: always exits 0, never blocks the tool, never prints an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CARDINAL_SERVERS = ("cardinal", "plugin_cardinal_cardinal")
TOOLS = ("storyboard__create", "storyboard__preview", "storyboard__add_act")

UPLOAD_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/storyboards/(sb_[0-9a-f]{24})/evidence$")
BUNDLE_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/storyboards/(sb_[0-9a-f]{24})/scenes/")


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def _unwrap(obj, evidence, depth: int = 0):
    """The tool's result object (the dict carrying storyboard_id), or None."""
    if depth > 4:
        return None
    if isinstance(obj, str):
        try:
            return _unwrap(json.loads(obj), evidence, depth + 1)
        except ValueError:
            pass
        spilled = evidence.read_spill(obj, home_dir() / ".claude" / "projects", 16 << 20)
        if spilled is None:
            return None
        try:
            return _unwrap(json.loads(spilled), evidence, depth + 1)
        except ValueError:
            return None
    if isinstance(obj, list):
        for block in obj:
            if isinstance(block, dict) and block.get("type") == "text":
                found = _unwrap(block.get("text"), evidence, depth + 1)
                if found is not None:
                    return found
        return None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("storyboard_id"), str):
        return obj
    for key in ("structuredContent", "content"):
        if key in obj:
            found = _unwrap(obj[key], evidence, depth + 1)
            if found is not None:
                return found
    return None


def _org_from(path, rx, storyboard_id: str):
    if not isinstance(path, str):
        return None
    m = rx.match(path)
    if not m or m.group(2) != storyboard_id:
        return None
    return urllib.parse.unquote(m.group(1))


def token_record(result: dict, known: dict):
    """(storyboard_id, org, token, expires_at) from a create / add_act /
    preview result, or None."""
    sb = result.get("storyboard_id")
    token = result.get("evidence_token")
    if not isinstance(sb, str) or not isinstance(token, str):
        return None
    upload = result.get("evidence_upload")
    org = _org_from(upload.get("path") if isinstance(upload, dict) else None, UPLOAD_PATH_RE, sb)
    if org is None:
        scenes = result.get("scenes")
        for scene in scenes if isinstance(scenes, list) else []:
            ref = scene.get("preview_bundle") if isinstance(scene, dict) else None
            org = _org_from(ref.get("path") if isinstance(ref, dict) else None, BUNDLE_PATH_RE, sb)
            if org:
                break
    if org is None and sb in known:
        org = known[sb].get("org")
    if not org:
        return None
    return sb, org, token, result.get("evidence_token_expires_at")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    from cardinal_core import evidence

    parts = evidence.split_mcp_tool(payload.get("tool_name"))
    if parts is None or parts[0] not in CARDINAL_SERVERS or parts[1] not in TOOLS:
        return
    result = _unwrap(payload.get("tool_response"), evidence)
    if result is None:
        return
    root = evidence.default_root(home_dir())
    if evidence.capture_disabled(root):
        return
    session = payload.get("session_id")
    rec = token_record(result, evidence.read_tokens(root, session))
    if rec is None:
        return
    sb, org, token, expires_at = rec
    evidence.store_token(root, session, storyboard_id=sb, org=org, token=token, expires_at=expires_at)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
