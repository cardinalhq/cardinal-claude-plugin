#!/usr/bin/env python3
"""cardinal subagent_usage hook — PostToolUse on Agent|Task.

Emits one OTLP/HTTP log event with event_name='cardinal.subagent_usage'
per completed subagent spawn, carrying the spawn's EXACT cumulative
token spend so the lakerunner agent-sessions processor can fold it into
agents_used[type].subtok (conductor
docs/specs/agent-outcomes-toolkit-metering.md §7).

Why a hook at all: claude-code reports all subagent activity inline
under the parent session_id with no per-request marker, so server-side
attribution cannot isolate a spawn's spend (background spawns interleave
with the main loop). The harness, however, writes the subagent's own
transcript to <transcript_dir>/<session_id>/subagents/agent-<id>.jsonl
with per-request usage records — this hook sums them:

    total_tokens = Σ (input + cache_creation + output)   per request

which matches the "worked tokens" definition the server-side turn
attribution uses, so subtok and tok read in the same unit. The tool
response's own totalTokens is NOT that number — it is the final
request's context footprint (verified 2026-06-12: equals the last
usage record's component sum on 7/7 samples) — so it is emitted as
final_context_tokens, a separate, honestly-named field.

Contract:
  - Input on stdin: PostToolUse hook JSON {session_id, transcript_path,
    cwd, tool_name, tool_input, tool_response, ...}.
  - Env: same OTLP settings as git-state.py (read from
    ~/.claude/settings.json because Claude Code does not propagate
    OTEL_* into hook subprocesses).
  - Behaviour: best-effort, exit 0 silently on any failure. If the
    subagent transcript is missing/unreadable, the event is emitted
    WITHOUT total_tokens — the processor then skips subtok entirely
    rather than recording a wrong number (one semantics per field).
  - Async (hooks.json): never blocks the loop returning to the model.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


HOOK_TIMEOUT_SEC = 2.0


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": str(value)}}


def _parse_resource_attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def _load_otel_settings() -> dict[str, str]:
    """Same source-of-truth read as git-state.py: Claude Code does not
    propagate OTEL_* into hook subprocess environments."""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env") or {}
        return {k: v for k, v in env.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def _sum_transcript_usage(path: Path) -> tuple[int, int, int] | None:
    """Sum per-request usage records from a subagent transcript JSONL.

    Returns (worked_tokens, cache_read_tokens, request_count) or None
    when the file is missing/unreadable/contains no usage records.
    worked = input + cache_creation + output, matching the server-side
    turn-attribution definition so subtok and tok share a unit.
    """
    try:
        worked = 0
        cache_read = 0
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = (rec.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                n += 1
                worked += (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("output_tokens") or 0)
                )
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
        if n == 0:
            return None
        return worked, cache_read, n
    except OSError:
        return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    if payload.get("tool_name") not in ("Agent", "Task"):
        # hooks.json matcher already filters; belt-and-braces for direct
        # invocation.
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

    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Type sourcing mirrors lakerunner's toolkitKey defaulting chain so
    # the subtok lands on the same agents_used key the tool_result's n
    # landed on.
    subagent_type = (
        tool_response.get("agentType")
        or tool_input.get("subagent_type")
        or "general-purpose"
    )
    agent_id = tool_response.get("agentId")

    # Exact cumulative spend from the subagent's own transcript:
    # <transcript_dir>/<session_id>/subagents/agent-<agentId>.jsonl
    totals = None
    transcript_path = payload.get("transcript_path") or ""
    if agent_id and transcript_path.endswith(".jsonl"):
        sub = Path(transcript_path[: -len(".jsonl")]) / "subagents" / f"agent-{agent_id}.jsonl"
        totals = _sum_transcript_usage(sub)

    attributes = [
        _kv("event_name", "cardinal.subagent_usage"),
        _kv("session_id", session_id),
        _kv("subagent_type", subagent_type),
        *([_kv("agent_id", agent_id)] if agent_id else []),
    ]
    if totals is not None:
        worked, cache_read, request_count = totals
        attributes += [
            _kv("total_tokens", worked),
            _kv("subagent_cache_read_tokens", cache_read),
            _kv("subagent_request_count", request_count),
        ]
    # Footprint fields from the harness result — informational; the
    # processor's subtok reads ONLY total_tokens (cumulative spend).
    for src, dst in (
        ("totalTokens", "final_context_tokens"),
        ("totalToolUseCount", "subagent_tool_use_count"),
        ("totalDurationMs", "subagent_duration_ms"),
    ):
        v = tool_response.get(src)
        if isinstance(v, (int, float)):
            attributes.append(_kv(dst, int(v)))

    resource_attrs = _parse_resource_attrs(
        settings_env.get("OTEL_RESOURCE_ATTRIBUTES")
        or os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")

    now_ns = time.time_ns()
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
                            "version": "0.10.0",
                        },
                        "logRecords": [
                            {
                                "timeUnixNano": str(now_ns),
                                "observedTimeUnixNano": str(now_ns),
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": "cardinal.subagent_usage"},
                                "attributes": attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    url = endpoint.rstrip("/") + "/v1/logs"
    headers = {"Content-Type": "application/json"}
    headers.update(_parse_otlp_headers(headers_raw))
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
