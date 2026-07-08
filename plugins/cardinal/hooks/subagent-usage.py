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
attribution uses, so subtok and tok read in the same unit. The same
pass also collects the per-component split (input / output /
cache_creation — they sum exactly to total_tokens), the dominant model
by worked tokens, and a tool-name histogram, per
docs/specs/subagent-telemetry-enrichment.md §Field 1. The tool
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
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plan_cache  # noqa: E402
import _plugin_version  # noqa: E402


HOOK_TIMEOUT_SEC = 2.0

# Attribute-size bound on subagent_tool_counts (spec
# docs/specs/subagent-telemetry-enrichment.md §Field 1): keep the 32 most
# frequent tool names; if capped, subagent_tool_counts_truncated=true.
TOOL_COUNTS_CAP = 32

# Character cap on subagent_description (spec §Field 5). Hard truncate,
# no ellipsis marker — 160 chars comfortably covers the harness's
# "3-5 word" description guidance with headroom.
DESCRIPTION_CAP = 160


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


def _sum_transcript_usage(path: Path) -> dict | None:
    """Sum per-request usage records from a subagent transcript JSONL.

    Returns None when the file is missing/unreadable/contains no usage
    records; otherwise a dict with:
      worked, cache_read, request_count  — as before (one semantics per
        field: worked = input + cache_creation + output, matching the
        server-side turn-attribution definition so subtok and tok share
        a unit)
      input, output, cache_creation      — per-component sums; by
        construction they sum exactly to worked (downstream consistency
        check, spec §Field 1)
      model, model_count                 — dominant message.model by
        worked tokens (ties broken first-seen via dict insertion order);
        distinct models seen (>1 ⇒ mixed run). model is None when no
        usage record carried one.
      tool_counts                        — Counter of tool_use block
        names over assistant messages (names only, MCP-qualified names
        included; no arguments). Rides this same single pass — no extra
        file read.
    """
    try:
        input_sum = 0
        output_sum = 0
        cache_creation = 0
        cache_read = 0
        n = 0
        model_worked: dict[str, int] = {}
        tool_counts: Counter[str] = Counter()
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if msg.get("role") == "assistant" and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name")
                        if isinstance(name, str) and name:
                            tool_counts[name] += 1
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                n += 1
                rec_input = int(usage.get("input_tokens") or 0)
                rec_creation = int(usage.get("cache_creation_input_tokens") or 0)
                rec_output = int(usage.get("output_tokens") or 0)
                input_sum += rec_input
                cache_creation += rec_creation
                output_sum += rec_output
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
                model = msg.get("model")
                if isinstance(model, str) and model:
                    model_worked[model] = (
                        model_worked.get(model, 0)
                        + rec_input + rec_creation + rec_output
                    )
        if n == 0:
            return None
        # max() returns the first maximum in iteration order, and dicts
        # iterate in insertion order — ties break first-seen.
        dominant = (
            max(model_worked, key=model_worked.get) if model_worked else None
        )
        return {
            "worked": input_sum + cache_creation + output_sum,
            "cache_read": cache_read,
            "request_count": n,
            "input": input_sum,
            "output": output_sum,
            "cache_creation": cache_creation,
            "model": dominant,
            "model_count": len(model_worked),
            "tool_counts": tool_counts,
        }
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
    # PRIVACY BOUNDARY (spec §Field 5) — deliberate, consciously approved
    # widening: subagent_description is the FIRST free-text field this
    # plugin emits. It carries ONLY the orchestrator's short task label
    # for the spawn (the Agent tool's `description` argument, e.g.
    # "Release Claude plugin v0.12.0"), verbatim but hard-capped at
    # DESCRIPTION_CAP (160) chars. It is NOT tool content: prompts, tool
    # arguments, and tool results remain never-captured. Omitted when
    # absent, empty, or non-string.
    description = tool_input.get("description")
    if isinstance(description, str) and description:
        attributes.append(_kv("subagent_description", description[:DESCRIPTION_CAP]))
    if totals is not None:
        attributes += [
            _kv("total_tokens", totals["worked"]),
            _kv("subagent_cache_read_tokens", totals["cache_read"]),
            _kv("subagent_request_count", totals["request_count"]),
            # Component split (spec §Field 1): the three fields below sum
            # exactly to total_tokens — the downstream consistency check
            # and the bimodal-Explore signature both depend on it.
            _kv("subagent_input_tokens", totals["input"]),
            _kv("subagent_output_tokens", totals["output"]),
            _kv("subagent_cache_creation_tokens", totals["cache_creation"]),
        ]
        if totals["model"]:
            attributes += [
                _kv("subagent_model", totals["model"]),
                _kv("subagent_model_count", totals["model_count"]),
            ]
        tool_counts = totals["tool_counts"]
        if tool_counts:
            capped = len(tool_counts) > TOOL_COUNTS_CAP
            if capped:
                tool_counts = dict(tool_counts.most_common(TOOL_COUNTS_CAP))
            attributes.append(_kv(
                "subagent_tool_counts",
                json.dumps(dict(tool_counts), separators=(",", ":")),
            ))
            if capped:
                attributes.append(_kv("subagent_tool_counts_truncated", "true"))
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
    attributes.extend(_plan_cache.stamp_attrs())

    resource_attrs = _parse_resource_attrs(
        settings_env.get("OTEL_RESOURCE_ATTRIBUTES")
        or os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")
    # Overwrite any stale value baked into settings.json at install time —
    # the on-disk plugin.json is the source of truth on every upgrade.
    resource_attrs["cardinal.plugin_version"] = _plugin_version.plugin_version()

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
                            "version": _plugin_version.plugin_version(),
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
