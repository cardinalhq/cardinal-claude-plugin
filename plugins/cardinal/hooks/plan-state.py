#!/usr/bin/env python3
"""cardinal plan_state hook — SessionStart.

Emits one OTLP log event with event_name='cardinal.plan_state' per
SessionStart so the lakerunner processor can LWW the subscription tier +
billing mode onto the agent_sessions row. The fetch + cache is owned by
the sibling _plan_cache module (adapter-only by explicit decision — the
OAuth profile surface is Claude-specific); this hook is just the
SessionStart emitter.

Contract:
  - Input on stdin: SessionStart hook JSON {session_id, transcript_path,
    ...}.
  - Env: same OTLP settings as turn-usage.py.
  - Behaviour: best-effort, exit 0 silently on any failure.
  - Async (hooks.json): never blocks Claude Code's session start.

See docs/specs/plan-state-telemetry.md for the contract.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
import _plan_cache  # noqa: E402
from cardinal_core.otlp import emit_records, kv  # noqa: E402

HOOK_TIMEOUT_SEC = 2.0
# Wire-frozen scope version this event family shipped with (predates the
# emit-time plugin_version stamping the other hooks use).
SCOPE_VERSION = "0.11.1"


def _silent_exit() -> None:
    sys.exit(0)


def _build_plan_state_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict]:
    attrs = [
        kv("event_name", "cardinal.plan_state"),
        kv("session_id", session_id),
        kv("ts", ts_ns),
    ]
    # Six profile-derived fields — each emitted only when present.
    for key in (
        "plan_type",
        "rate_limit_tier",
        "organization_type",
        "billing_type",
        "billing_mode",
    ):
        v = blob.get(key)
        if isinstance(v, str) and v:
            attrs.append(kv(key, v))
    has_extra = blob.get("has_extra_usage_enabled")
    if isinstance(has_extra, bool):
        attrs.append(kv("has_extra_usage_enabled", has_extra))
    return attrs


def _build_plan_usage_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict] | None:
    usage = blob.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
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

    settings_env = _otel_settings.load_otel_settings()
    connection = _otel_settings.ingest_connection(settings_env)
    if connection is None:
        _silent_exit()

    blob = _plan_cache.refresh_plan_state()
    if not isinstance(blob, dict):
        _silent_exit()

    now_ns = time.time_ns()
    state_attrs = _build_plan_state_attrs(blob, session_id, now_ns)
    usage_attrs = _build_plan_usage_attrs(blob, session_id, now_ns)

    # Always emit plan_state if we have any of the projected fields.
    # Emit plan_usage too if we have usage data — the SessionStart fetch
    # anchors the first snapshot of the session for the Δ math.
    log_records = []
    if len(state_attrs) > 3:  # more than event_name/session_id/ts
        log_records.append({
            "timeUnixNano": str(now_ns),
            "observedTimeUnixNano": str(now_ns),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": "cardinal.plan_state"},
            "attributes": state_attrs,
        })
    if usage_attrs is not None:
        # Offset by 1 ns so the two records share the batch but have
        # distinct chq_tsns server-side (same per-record-uniqueness reason
        # as turn-usage.py's loop offset).
        log_records.append({
            "timeUnixNano": str(now_ns + 1),
            "observedTimeUnixNano": str(now_ns + 1),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": "cardinal.plan_usage"},
            "attributes": usage_attrs,
        })

    if not log_records:
        _silent_exit()

    emit_records(
        log_records,
        connection,
        _otel_settings.resource_attrs(settings_env),
        scope_name="cardinal-claude-plugin",
        scope_version=SCOPE_VERSION,
        timeout=HOOK_TIMEOUT_SEC,
    )

    _silent_exit()


if __name__ == "__main__":
    main()
