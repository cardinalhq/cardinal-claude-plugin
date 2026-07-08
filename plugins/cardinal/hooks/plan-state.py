#!/usr/bin/env python3
"""cardinal plan_state hook — SessionStart.

Emits one OTLP log event with event_name='cardinal.plan_state' per
SessionStart so the lakerunner processor can LWW the subscription tier +
billing mode onto the agent_sessions row. The fetch + cache is owned by
the sibling _plan_cache module; this hook is just the SessionStart
emitter.

Contract:
  - Input on stdin: SessionStart hook JSON {session_id, transcript_path,
    ...}.
  - Env: same OTLP settings as turn-usage.py (read from
    ~/.claude/settings.json because Claude Code does not propagate
    OTEL_* into hook subprocesses).
  - Behaviour: best-effort, exit 0 silently on any failure.
  - Async (hooks.json): never blocks Claude Code's session start.

See docs/specs/plan-state-telemetry.md for the contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plan_cache  # noqa: E402
import _plugin_version  # noqa: E402


HOOK_TIMEOUT_SEC = 2.0
SCOPE_VERSION = "0.11.1"


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _parse_kv_csv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def _load_otel_settings() -> dict[str, str]:
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env") or {}
        return {k: v for k, v in env.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def _build_plan_state_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict]:
    attrs = [
        _kv("event_name", "cardinal.plan_state"),
        _kv("session_id", session_id),
        _kv("ts", ts_ns),
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
            attrs.append(_kv(key, v))
    has_extra = blob.get("has_extra_usage_enabled")
    if isinstance(has_extra, bool):
        attrs.append(_kv("has_extra_usage_enabled", has_extra))
    return attrs


def _build_plan_usage_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict] | None:
    usage = blob.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    attrs = [
        _kv("event_name", "cardinal.plan_usage"),
        _kv("session_id", session_id),
        _kv("ts", ts_ns),
    ]
    any_field = False
    for window in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        bucket = usage.get(window)
        if not isinstance(bucket, dict):
            continue
        util = bucket.get("utilization")
        if isinstance(util, (int, float)):
            attrs.append(_kv(f"{window}_utilization", float(util)))
            any_field = True
        resets = bucket.get("resets_at")
        if isinstance(resets, str) and resets:
            attrs.append(_kv(f"{window}_resets_at", resets))
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

    settings_env = _load_otel_settings()
    endpoint = (
        settings_env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    headers_raw = (
        settings_env.get("OTEL_EXPORTER_OTLP_HEADERS")
        or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    )
    if not endpoint:
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

    resource_attrs = _parse_kv_csv(
        settings_env.get("OTEL_RESOURCE_ATTRIBUTES")
        or os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")
    # Overwrite any stale value baked into settings.json at install time —
    # the on-disk plugin.json is the source of truth on every upgrade.
    resource_attrs["cardinal.plugin_version"] = _plugin_version.plugin_version()

    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [_kv(k, v) for k, v in resource_attrs.items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-claude-plugin",
                            "version": SCOPE_VERSION,
                        },
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }

    url = endpoint.rstrip("/") + "/v1/logs"
    headers = {"Content-Type": "application/json"}
    headers.update(_parse_kv_csv(headers_raw))
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    _silent_exit()


if __name__ == "__main__":
    main()
