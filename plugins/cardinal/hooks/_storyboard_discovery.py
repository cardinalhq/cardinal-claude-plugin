"""Claude Code's wiring for cardinal_core.storyboard_discovery.

Shared by hooks/storyboard-discovery.py (SessionStart, UserPromptSubmit,
SubagentStart) and
bin/cardinal-storyboard discover. Supplies what the harness-neutral core
needs from this adapter:

  - the connection: CARDINAL_MCP_URL + CARDINAL_MCP_API_KEY from
    ~/.claude/settings.json `env` (where /cardinal:connect writes them; Claude
    Code does not reliably export them to hooks), then the environment — the
    same lookup as bin/cardinal-evidence. A telemetry-only connection has no
    MCP key: nothing is sent.
  - the PR: the decisions gh cache only (cache_only_pr_resolver), never `gh`.
  - the session cache: ~/.claude/cardinal/storyboard-discovery/<session>.json
    (the last look's branch/HEAD and the block it rendered, which
    SubagentStart re-emits).
  - the opt-out: CARDINAL_STORYBOARD_DISCOVERY=0, in the environment or in
    settings.json `env` (plugin-side only; maestro is unaffected).

Reads only local files until discover() makes its two bounded requests;
never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

MCP_URL_ENV = "CARDINAL_MCP_URL"
MCP_KEY_ENV = "CARDINAL_MCP_API_KEY"
DISABLE_ENV = "CARDINAL_STORYBOARD_DISCOVERY"
OFF_VALUES = ("0", "false", "off", "no")


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def _settings_env(home: Path) -> dict:
    try:
        env = json.loads((home / ".claude" / "settings.json").read_text()).get("env", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def is_disabled(home: Optional[Path] = None, environ: Optional[dict] = None) -> bool:
    """CARDINAL_STORYBOARD_DISCOVERY=0 (false / off / no) in the environment
    or in ~/.claude/settings.json `env`."""
    environ = os.environ if environ is None else environ
    for source in (environ, _settings_env(home or home_dir())):
        value = source.get(DISABLE_ENV)
        if isinstance(value, str) and value.strip().lower() in OFF_VALUES:
            return True
    return False


def connection(home: Optional[Path] = None, environ: Optional[dict] = None) -> dict:
    """{origin, org, key} or {} (no usable URL)."""
    from cardinal_core.evidence_promote import connection_for

    home = home or home_dir()
    environ = os.environ if environ is None else environ
    env = _settings_env(home)
    url, key = env.get(MCP_URL_ENV), env.get(MCP_KEY_ENV)
    if not (isinstance(url, str) and url):
        url, key = environ.get(MCP_URL_ENV), environ.get(MCP_KEY_ENV)
    return connection_for(url, key)


def runtime_dir(home: Optional[Path] = None) -> Path:
    from cardinal_core.paths import AgentPaths

    return AgentPaths(home=(home or home_dir()) / ".claude").runtime_dir


def state_dir(home: Optional[Path] = None) -> Path:
    return runtime_dir(home) / "storyboard-discovery"


def discover(cwd: str, *, session_id: Optional[str], event: str, use_cache: bool = True,
             home: Optional[Path] = None, environ: Optional[dict] = None, opener=None,
             deadline: Optional[float] = None, deliver_by: Optional[float] = None) -> Optional[str]:
    """The block for cwd, or None. use_cache False (the CLI): always runs and
    records nothing. deadline / deliver_by: absolute time.monotonic() values
    (the hook measures them from its process start; see
    storyboard_discovery.discover)."""
    try:
        from cardinal_core import decisions, storyboard_context, storyboard_discovery

        conn = connection(home, environ)
        if not conn.get("key") or not conn.get("org"):
            return None
        return storyboard_discovery.discover(
            cwd,
            conn=conn,
            session_id=session_id,
            state_dir=state_dir(home) if use_cache else None,
            event=event,
            pr_resolver=storyboard_context.cache_only_pr_resolver(decisions.cache_dir(runtime_dir(home))),
            opener=opener,
            deadline=deadline,
            deliver_by=deliver_by,
        )
    except Exception:
        return None
