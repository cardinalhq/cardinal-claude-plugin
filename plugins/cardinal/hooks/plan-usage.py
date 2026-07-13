#!/usr/bin/env python3
"""cardinal plan_usage hook — Stop, throttled.

Emits one OTLP log event with event_name='cardinal.plan_usage' when the
cache's usage half is older than 10 minutes (spec
docs/specs/plan-state-telemetry.md §`cardinal.plan_usage`). On cache-fresh
Stops we are silent — heavy users emit ≤ ~7 usage events/day rather than
one per Stop.

Contract:
  - Input on stdin: Stop hook JSON {session_id, transcript_path, ...}.
  - Env: same OTLP settings as turn-usage.py.
  - Behaviour: best-effort, exit 0 silently on every error.
  - Async (hooks.json): never blocks the loop returning to the model.

This hook does NOT bypass plan-state.py: if the cache is absent (i.e.
plan-state has not yet populated it, or it failed), this hook is a no-op.
The first usage snapshot of a session is always written by plan-state.py
at SessionStart.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
import _plan_cache  # noqa: E402
from cardinal_core.otlp import emit_records, kv  # noqa: E402

HOOK_TIMEOUT_SEC = 2.0
# Wire-frozen scope version this event family shipped with (matches
# plan-state.py).
SCOPE_VERSION = "0.11.1"
_USAGE_REFRESH_TTL_SEC = 10 * 60


def _silent_exit() -> None:
    sys.exit(0)


def _parse_iso(s: str | None) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_usage_attrs(usage: dict, session_id: str, ts_ns: int) -> list[dict] | None:
    attrs = [
        kv("event_name", "cardinal.plan_usage"),
        kv("session_id", session_id),
        kv("ts", ts_ns),
    ]
    any_field = False
    for window in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        bucket = usage.get(window)
        if not isinstance(bucket, dict):
            continue
        util = bucket.get("utilization")
        if isinstance(util, (int, float)):
            attrs.append(kv(f"{window}_utilization", float(util)))
            any_field = True
        resets = bucket.get("resets_at")
        if isinstance(resets, str) and resets:
            attrs.append(kv(f"{window}_resets_at", resets))
            any_field = True
    return attrs if any_field else None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        _silent_exit()

    cached = _plan_cache.read()
    if not isinstance(cached, dict):
        # plan-state.py has not yet populated the cache (e.g. first
        # session ever, or fetch failed). plan-state owns the bootstrap.
        _silent_exit()

    # Throttle: don't refetch unless 10 min has passed since the last
    # usage fetch.
    last = _parse_iso(cached.get("usage_fetched_at"))
    if last is not None:
        if (datetime.now(timezone.utc) - last).total_seconds() < _USAGE_REFRESH_TTL_SEC:
            _silent_exit()

    settings_env = _otel_settings.load_otel_settings()
    connection = _otel_settings.ingest_connection(settings_env)
    if connection is None:
        _silent_exit()

    blob = _plan_cache.refresh_usage_only()
    if not isinstance(blob, dict):
        _silent_exit()

    usage = blob.get("usage")
    if not isinstance(usage, dict):
        _silent_exit()

    now_ns = time.time_ns()
    attrs = _build_usage_attrs(usage, session_id, now_ns)
    if attrs is None:
        _silent_exit()

    emit_records(
        [
            {
                "timeUnixNano": str(now_ns),
                "observedTimeUnixNano": str(now_ns),
                "severityNumber": 9,
                "severityText": "INFO",
                "body": {"stringValue": "cardinal.plan_usage"},
                "attributes": attrs,
            }
        ],
        connection,
        _otel_settings.resource_attrs(settings_env),
        scope_name="cardinal-claude-plugin",
        scope_version=SCOPE_VERSION,
        timeout=HOOK_TIMEOUT_SEC,
    )

    _silent_exit()


if __name__ == "__main__":
    main()
