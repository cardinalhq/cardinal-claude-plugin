"""Is a Cardinal MCP key set without a URL (a stray key)?

The plugin's .mcp.json is

    "url":     "${CARDINAL_MCP_URL}"
    "headers": {"X-CardinalHQ-API-Key": "${CARDINAL_MCP_API_KEY:-}"}

so without CARDINAL_MCP_URL the `cardinal` server has no URL and never
connects, whatever key is set: the key is sent nowhere. But a key on its own
(exported in a shell profile for a manual Codex config, or a self-hosted
maestro's key) makes hooks/_connection.py count this machine as connected,
while Cardinal's tools stay missing. /cardinal:connect always writes both, so
a connected install never hits this; a stray key does.

key_without_url() detects it from ~/.claude/settings.json `env` (which Claude
Code applies to its own environment) and the process environment, the same
two places Claude Code substitutes from. An empty or blank value counts as
unset.

Used by bin/cardinal-status. storyboard-session.py (SessionStart) calls
core's stray_key_warning directly. Core logic, shared with the other
runtimes: cardinal_core.storyboard_authoring (key_without_url, mcp_url,
stray_key_warning). Reads only local files; never raises.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core import storyboard_authoring as _sa  # noqa: E402

MCP_URL_ENV = _sa.MCP_URL_ENV
MCP_KEY_ENV = _sa.MCP_KEY_ENV
WARNING = _sa.stray_key_text("claude")


def _paths(home: Path | None):
    return _sa.agent_paths("claude", home)


def key_without_url(home: Path | None = None, environ: dict | None = None) -> bool:
    """True when a Cardinal MCP key is set and no CARDINAL_MCP_URL is."""
    return _sa.key_without_url(_paths(home), "claude", environ)


def mcp_url(home: Path | None = None, environ: dict | None = None) -> str | None:
    """The URL .mcp.json resolves to: CARDINAL_MCP_URL, else None (no server)."""
    return _sa.mcp_url(_paths(home), "claude", environ)
