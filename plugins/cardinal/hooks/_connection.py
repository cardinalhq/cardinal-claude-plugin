"""Is this Claude Code connected to a Cardinal org (/cardinal:connect)?

The plugin works in two modes:

  not connected   Local-only. CARDINAL_MCP_URL is unset, so the bundled
                  `cardinal` MCP server has no URL and never connects (no
                  network, no sign-in; /mcp lists it as missing
                  CARDINAL_MCP_URL). Evidence capture into the local spool
                  and local rendering work. Every telemetry, spend-limit,
                  initiative, plan, decision, git-state and usage hook is a
                  silent no-op: no context, no network. storyboard-session.py
                  gives one line on how to connect (connect_hint).
  connected       /cardinal:connect wrote the org's MCP URL + key and the OTel
                  ingest settings into ~/.claude/settings.json `env` (and its
                  state file ~/.claude/cardinal.json). Everything runs.

Write access (publishing storyboards, Cardinal's tools) needs an API key:
sign up at https://app.cardinalhq.io, then run /cardinal:connect, whose
device-code approval creates and stores one. There is no OAuth sign-in.

Connected means any Cardinal credential or connect state is configured:

  - CARDINAL_MCP_API_KEY, non-empty, in ~/.claude/settings.json `env` or in
    the environment (the MCP side of /cardinal:connect);
  - a Cardinal ingest key (x-cardinalhq-api-key=<key>) in
    OTEL_EXPORTER_OTLP_HEADERS, same two places (the telemetry side, which
    `--telemetry-only` writes without an MCP key);
  - the connect state file ~/.claude/cardinal.json (written by every
    /cardinal:connect, removed by /cardinal:disconnect).

After /cardinal:disconnect the environment no longer counts (the marker
~/.claude/cardinal-disconnected, see _local_state.py): a running Claude Code
keeps the old CARDINAL_MCP_API_KEY in its process environment until it
restarts, and that must not keep the hooks connected.

Every gated hook calls is_connected() before doing anything else and exits 0
with no output when it is False. Reads only local files; never raises.
HOME is resolved per call (tests point it at a temp dir).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _local_state  # noqa: E402

MCP_KEY_ENV = "CARDINAL_MCP_API_KEY"
OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
INGEST_KEY_HEADER = "x-cardinalhq-api-key"


def _home(home: Path | None) -> Path:
    if home is not None:
        return home
    return Path(os.environ.get("HOME") or str(Path.home()))


def _settings_env(claude_dir: Path) -> dict:
    try:
        with open(claude_dir / "settings.json", encoding="utf-8") as f:
            env = json.load(f).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def _has_ingest_key(headers) -> bool:
    if not isinstance(headers, str):
        return False
    for pair in headers.split(","):
        k, sep, v = pair.partition("=")
        if sep and k.strip().lower() == INGEST_KEY_HEADER and v.strip():
            return True
    return False


def is_connected(home: Path | None = None, environ: dict | None = None) -> bool:
    """True when /cardinal:connect (or an equivalent env) configured Cardinal."""
    try:
        claude_dir = _home(home) / ".claude"
        env = os.environ if environ is None else environ
        sources = [_settings_env(claude_dir)]
        if not _local_state.is_marked_disconnected(_home(home)):
            sources.append(env)
        for source in sources:
            key = source.get(MCP_KEY_ENV)
            if isinstance(key, str) and key.strip():
                return True
            if _has_ingest_key(source.get(OTLP_HEADERS_ENV)):
                return True
        return (claude_dir / "cardinal.json").is_file()
    except Exception:
        return False


SIGNUP_URL = "https://app.cardinalhq.io"
CONNECT_STEPS = (
    f"sign up at {SIGNUP_URL}, then run /cardinal:connect and approve it in the "
    "browser, which stores an API key for this machine (self-hosted Cardinal: "
    "/cardinal:connect --host <your maestro URL>)"
)
