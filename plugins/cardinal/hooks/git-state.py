#!/usr/bin/env python3
"""cardinal git_state hook — UserPromptSubmit.

Reads git state for the current cwd and POSTs one OTLP/HTTP log event
with event_name='cardinal.git_state' so the lakerunner agent-sessions
processor can LWW {repo, branch, head_sha, cwd} onto the session row.

Contract (see ~/workspace/conductor/docs/specs/agent-sessions.md §Plugin
hook contract):
  - Input on stdin: Claude Code's UserPromptSubmit hook JSON
    {session_id, cwd, hook_event_name, prompt, ...}.
  - Env (set by cardinal-connect in ~/.claude/settings.json):
      OTEL_EXPORTER_OTLP_ENDPOINT  e.g. https://otelhttp.intake...
      OTEL_EXPORTER_OTLP_HEADERS   "x-cardinalhq-api-key=<key>"
      OTEL_RESOURCE_ATTRIBUTES     comma-separated key=value pairs
                                   (carries user.email, cardinal.org)
  - Behaviour: best-effort. Any failure (not in git, no env, network
    blip) → exit 0 silently. Never block the prompt.
  - Async: declared with async=true in hooks/hooks.json, so Claude
    Code already spawns this off the prompt-submit critical path.
    The POST itself uses a short timeout for belt-and-braces.

Algorithms (initiative resolution, worktree-noise stripping, canonical
repo, slash-command detection) live in the vendored cardinal_core —
this script owns only Claude Code's payload spelling and the OTel
settings acquisition.
"""

from __future__ import annotations

import json
import os
import sys
import time

from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
import _plan_cache  # noqa: E402
import _plugin_version  # noqa: E402
from cardinal_core.initiative import (  # noqa: E402
    canonical_repo,
    detect_command,
    git,
    resolve_initiative,
)
from cardinal_core.initiative import PREFIX_TO_TYPE, strip_worktree_noise  # noqa: E402
from cardinal_core.limits import maybe_refresh_verdict  # noqa: E402
from cardinal_core.otlp import emit_records, kv, log_record  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402

HOOK_TIMEOUT_SEC = 2.0

# Bound at import time (hooks are one process per invocation).
PATHS = AgentPaths(home=Path.home() / ".claude")

# Back-compat module surface: the pre-migration script exported these as
# underscore-prefixed locals; the algorithms now live in cardinal_core.
# Kept as aliases so importers (including the ported test suite) see the
# same API.
_resolve_initiative = resolve_initiative
_strip_worktree_noise = strip_worktree_noise
_detect_command = detect_command
_canonical_repo = canonical_repo
_kv = kv
# Closed vocabulary downstream (lakerunner, conductor dashboard) treats
# as canonical — derived from core's prefix map, whose values ARE the
# closed enum ("feature" included via the feat alias).
_INITIATIVE_TYPES = frozenset(PREFIX_TO_TYPE.values())


def _silent_exit() -> None:
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    # settings.json wins over env, because Claude Code strips OTEL_* and
    # CLAUDE_PROJECT_DIR from hook subprocess envs in practice.
    settings_env = _otel_settings.load_otel_settings()
    cwd = (
        payload.get("cwd")
        or settings_env.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    # Session id sourcing in priority order:
    #   1. stdin JSON `session_id`   (the canonical Claude Code hook payload)
    #   2. CLAUDE_CODE_SESSION_ID env (set by Claude Code on the parent)
    #   3. CLAUDE_SESSION_ID env      (legacy variant)
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        _silent_exit()

    connection = _otel_settings.ingest_connection(settings_env)
    if connection is None:
        _silent_exit()

    head_sha = git(["rev-parse", "HEAD"], cwd)
    if head_sha is None:
        # Not a git repo (or git not installed). Nothing useful to send.
        _silent_exit()
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote_url = git(["remote", "get-url", "origin"], cwd)
    repo = canonical_repo(remote_url) if remote_url else None

    initiative_name, initiative_type = resolve_initiative(branch)
    command = detect_command(payload.get("prompt"))

    # plan_type + rate_limit_tier from the SessionStart cache — absent
    # when plan-state.py hasn't populated it yet.
    stamp = {
        a["key"]: a["value"]["stringValue"] for a in _plan_cache.stamp_attrs()
    }

    now_ns = time.time_ns()
    record = log_record(
        "cardinal.git_state",
        {
            "session_id": session_id,
            "cardinal.cwd": cwd,
            "cardinal.head_sha": head_sha,
            "cardinal.branch": branch,
            "cardinal.repo": repo,
            "cardinal.remote_url": remote_url,
            "cardinal.initiative.name": initiative_name,
            # type is ALWAYS emitted — resolve_initiative guarantees a
            # non-null value from the closed enum, so the lakerunner
            # column receives a real classification on every event.
            "cardinal.initiative.type": initiative_type,
            # Slash-command name (never args) when this turn invoked one —
            # closes the user-typed-skill gap in the native telemetry.
            "cardinal.command": command,
            **stamp,
        },
        now_ns,
    )

    emit_records(
        [record],
        connection,
        _otel_settings.resource_attrs(settings_env),
        scope_name="cardinal-claude-plugin",
        scope_version=_plugin_version.plugin_version(),
        timeout=HOOK_TIMEOUT_SEC,
    )

    # Spend-limits verdict refresh (conductor docs/specs/agent-spend-limits.md
    # §Delivery). This hook is the async half: it re-fetches the verdict from
    # maestro when the server-assigned TTL has lapsed and rewrites the local
    # verdict file that the sync limits-gate.py hook reads. Runs AFTER the
    # OTLP post and stays best-effort — limits must never cost telemetry.
    # api_key: Claude's credential lives in the OTel settings, not a
    # cardinal-secrets.json — core 0.2.0 takes it as an argument.
    try:
        maybe_refresh_verdict(
            PATHS,
            session_id=session_id,
            repo=repo,
            branch=branch,
            api_key=_otel_settings.ingest_api_key(settings_env),
        )
    except Exception:
        pass

    _silent_exit()


if __name__ == "__main__":
    main()
