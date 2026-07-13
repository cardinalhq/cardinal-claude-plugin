#!/usr/bin/env python3
"""cardinal subagent_usage hook — PostToolUse on Agent|Task.

Emits one OTLP/HTTP log event with event_name='cardinal.subagent_usage'
per completed subagent spawn, carrying the spawn's EXACT cumulative
token spend so the lakerunner agent-sessions processor can fold it into
agents_used[type].subtok (conductor
docs/specs/agent-outcomes-toolkit-metering.md §7).

Why a hook at all: claude-code reports all subagent activity inline
under the parent session_id with no per-request marker. The harness,
however, writes the subagent's own transcript to
<transcript_dir>/<session_id>/subagents/agent-<id>.jsonl with
per-request usage records — this hook sums them:

    total_tokens = Σ (input + cache_creation + output)   per request

matching the "worked tokens" definition server-side turn attribution
uses. The same pass collects the per-component split, the dominant
model by worked tokens, and a tool-name histogram
(docs/specs/subagent-telemetry-enrichment.md §Field 1). The tool
response's own totalTokens is the final request's context footprint and
is emitted as final_context_tokens, a separate, honestly-named field.

Contract:
  - Input on stdin: PostToolUse hook JSON {session_id, transcript_path,
    cwd, tool_name, tool_input, tool_response, ...}.
  - Env: same OTLP settings as git-state.py.
  - Behaviour: best-effort, exit 0 silently on any failure. If the
    subagent transcript is missing/unreadable, the event is emitted
    WITHOUT total_tokens — one semantics per field.
  - Async (hooks.json): never blocks the loop returning to the model.

The transcript summing is Claude-specific and stays here. Every
attribute is emitted as an OTLP stringValue (ints included) — the wire
contract this event shipped with — hence the str() coercion around
core's kv.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _otel_settings  # noqa: E402
import _plan_cache  # noqa: E402
import _plugin_version  # noqa: E402
from cardinal_core.otlp import emit_records, kv  # noqa: E402

HOOK_TIMEOUT_SEC = 2.0

# Attribute-size bound on subagent_tool_counts (spec §Field 1): keep the
# 32 most frequent tool names; if capped, subagent_tool_counts_truncated.
TOOL_COUNTS_CAP = 32

# Character cap on subagent_description (spec §Field 5). Hard truncate,
# no ellipsis marker — 160 chars comfortably covers the harness's
# "3-5 word" description guidance with headroom.
DESCRIPTION_CAP = 160


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value) -> dict:
    """This event's wire contract: every value is a stringValue."""
    return kv(key, str(value))


def _sum_transcript_usage(path: Path) -> dict | None:
    """Sum per-request usage records from a subagent transcript JSONL.

    Returns None when the file is missing/unreadable/contains no usage
    records; otherwise a dict with:
      worked, cache_read, request_count  — worked = input + cache_creation
        + output, matching the server-side turn-attribution definition so
        subtok and tok share a unit
      input, output, cache_creation      — per-component sums; they sum
        exactly to worked (downstream consistency check, spec §Field 1)
      model, model_count                 — dominant message.model by
        worked tokens (ties broken first-seen); distinct models seen
      tool_counts                        — Counter of tool_use block
        names over assistant messages (names only; no arguments).
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

    settings_env = _otel_settings.load_otel_settings()
    connection = _otel_settings.ingest_connection(settings_env)
    if connection is None:
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
    # widening: subagent_description carries ONLY the orchestrator's short
    # task label for the spawn (the Agent tool's `description` argument),
    # verbatim but hard-capped at DESCRIPTION_CAP chars. It is NOT tool
    # content: prompts, tool arguments, and tool results remain
    # never-captured. Omitted when absent, empty, or non-string.
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

    now_ns = time.time_ns()
    emit_records(
        [
            {
                "timeUnixNano": str(now_ns),
                "observedTimeUnixNano": str(now_ns),
                "severityNumber": 9,
                "severityText": "INFO",
                "body": {"stringValue": "cardinal.subagent_usage"},
                "attributes": attributes,
            }
        ],
        connection,
        _otel_settings.resource_attrs(settings_env),
        scope_name="cardinal-claude-plugin",
        scope_version=_plugin_version.plugin_version(),
        timeout=HOOK_TIMEOUT_SEC,
    )

    _silent_exit()


if __name__ == "__main__":
    main()
