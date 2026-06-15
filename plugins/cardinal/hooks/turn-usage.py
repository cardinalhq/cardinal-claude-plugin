#!/usr/bin/env python3
"""cardinal turn_usage hook — Stop.

Emits per-model-call telemetry for the user turn that just completed:
- cardinal.turn_usage : one record per model call with the API usage object.
- cardinal.turn_tool  : one record per tool_use block, linked by turn_seq.

Powers the Advisory section in conductor's /agent-outcomes dashboard:
- A1 (cache-cliff)     reads cache_read_input_tokens per model call.
- C3 (CLAUDE.md promo) reads target file_path on Read/Edit/Write/NotebookEdit.
- D1 (tool-loop)       reads tool_name sequence per user turn.

Why a hook at all: claude-code rolls up per-turn usage and per-tool inputs
into session-grain attributes before they leave the harness, so
server-side cannot reconstruct per-model-call deltas. The transcript JSONL
on disk has every record verbatim; this hook reads the slice belonging to
the current user turn and emits one OTLP POST.

Contract:
  - Input on stdin: Stop hook JSON {session_id, transcript_path, ...}.
  - Env: same OTLP settings as git-state.py (read from
    ~/.claude/settings.json because Claude Code does not propagate OTEL_*
    into hook subprocesses).
  - Behaviour: best-effort, exit 0 silently on any failure.
  - Async (hooks.json): never blocks the loop returning to the model.

See docs/specs/per-turn-telemetry.md for the full schema, caps, and the
privacy boundary on `target` capture.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plan_cache  # noqa: E402


HOOK_TIMEOUT_SEC = 2.0

# Per-emit caps (spec §5) — protect the hook process from pathological
# transcripts (long tool-loop sessions).
MAX_TURN_USAGES = 64
MAX_TURN_TOOLS = 256

# Privacy boundary (spec §Privacy) — only file-path-shaped inputs are
# emitted as `target`. Bash command, Grep pattern, MCP args are dropped.
# NotebookEdit's tool schema uses `notebook_path` rather than `file_path`,
# so the table also doubles as the per-tool input-key map; membership in
# this dict IS the allowlist.
TARGET_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
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
    """Source-of-truth read mirroring git-state.py / subagent-usage.py."""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env") or {}
        return {k: v for k, v in env.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def _is_real_user_message(msg: dict) -> bool:
    """A 'real' user message marks a turn boundary; a tool_result-only
    user message is loop continuation and is NOT a boundary."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        # Tool-result continuations carry only tool_result blocks.
        for block in content:
            if isinstance(block, dict) and block.get("type") != "tool_result":
                return True
        return False
    return False


def _ts_ns_from_record(rec: dict, fallback_ns: int) -> int:
    """Best-effort epoch-ns from a transcript record. Claude Code writes
    ISO8601 timestamps on records; if absent or unparseable, fall back to
    a monotonic now()-relative value (turn ordering is what matters)."""
    raw = rec.get("timestamp")
    if isinstance(raw, str):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
        except (ValueError, TypeError):
            pass
    return fallback_ns


def _extract_target(tool_name: str, tool_input) -> str | None:
    key = TARGET_KEYS.get(tool_name)
    if key is None or not isinstance(tool_input, dict):
        return None
    path = tool_input.get(key)
    return path if isinstance(path, str) and path else None


def _walk_current_turn(transcript_path: Path) -> list[dict]:
    """Return the JSONL records belonging to the user turn that just
    ended: everything after the most recent 'real' user message.

    Streaming forward — at each real-user-message boundary, drop the
    buffered prior turn. Memory is bounded by the current turn's record
    count, not by total transcript size, so long sessions don't load the
    whole transcript into the hook process. If no boundary is found
    (first turn or truncated transcript), returns everything seen.
    """
    current_turn: list[dict] = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if isinstance(msg, dict) and _is_real_user_message(msg):
                    current_turn = []  # boundary; drop the prior turn
                    continue
                current_turn.append(rec)
    except (OSError, UnicodeDecodeError):
        return []
    return current_turn


def _build_records(
    records: list[dict],
    session_id: str,
    now_ns: int,
) -> list[dict]:
    """Map current-turn records to a flat list of (event_name, attrs)
    tuples ready to render as OTLP logRecords. Enforces MAX_TURN_USAGES
    and MAX_TURN_TOOLS caps from spec §5."""
    out: list[tuple[str, list[dict]]] = []
    turn_seq = 0
    tool_count = 0
    truncated = False
    # Plan-state stamps: empty list when ~/.claude/cardinal/plan.json is
    # absent (e.g. plan-state.py never ran, or fetch failed). Caller
    # behaviour: append to every emitted record without changing existing
    # attribute order.
    plan_extras = _plan_cache.stamp_attrs()

    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue

        if turn_seq >= MAX_TURN_USAGES:
            truncated = True
            break

        ts_ns = _ts_ns_from_record(rec, now_ns)
        usage_attrs = [
            _kv("event_name", "cardinal.turn_usage"),
            _kv("session_id", session_id),
            _kv("ts", ts_ns),
            _kv("turn_seq", turn_seq),
        ]
        model = msg.get("model")
        if isinstance(model, str) and model:
            usage_attrs.append(_kv("model", model))
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            v = usage.get(key)
            if isinstance(v, (int, float)):
                usage_attrs.append(_kv(key, int(v)))
        usage_attrs.extend(plan_extras)
        out.append(("cardinal.turn_usage", usage_attrs))

        content = msg.get("content")
        hit_tool_cap = False
        if isinstance(content, list):
            tool_seq = 0
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if tool_count >= MAX_TURN_TOOLS:
                    truncated = True
                    hit_tool_cap = True
                    break
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                tool_attrs = [
                    _kv("event_name", "cardinal.turn_tool"),
                    _kv("session_id", session_id),
                    _kv("ts", ts_ns),
                    _kv("turn_seq", turn_seq),
                    _kv("tool_seq", tool_seq),
                    _kv("tool_name", tool_name),
                ]
                target = _extract_target(tool_name, block.get("input"))
                if target is not None:
                    tool_attrs.append(_kv("target", target))
                tool_attrs.extend(plan_extras)
                out.append(("cardinal.turn_tool", tool_attrs))
                tool_seq += 1
                tool_count += 1

        turn_seq += 1
        if hit_tool_cap:
            # Single truncation point — stop emitting further usage
            # records too, so `truncated=true` consistently means
            # "everything past this point dropped".
            break

    if truncated and out:
        # Flag truncation on the most recent turn_usage record so the
        # downstream consumer can fail loud rather than treat partial as
        # complete.
        for name, attrs in reversed(out):
            if name == "cardinal.turn_usage":
                attrs.append(_kv("truncated", True))
                break

    return [
        {"event_name": name, "attributes": attrs}
        for name, attrs in out
    ]


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

    transcript_path_raw = payload.get("transcript_path") or ""
    if not transcript_path_raw or not transcript_path_raw.endswith(".jsonl"):
        _silent_exit()
    transcript_path = Path(transcript_path_raw)

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

    current_turn = _walk_current_turn(transcript_path)
    if not current_turn:
        _silent_exit()

    now_ns = time.time_ns()
    payloads = _build_records(current_turn, session_id, now_ns)
    if not payloads:
        _silent_exit()

    resource_attrs = _parse_kv_csv(
        settings_env.get("OTEL_RESOURCE_ATTRIBUTES")
        or os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")

    # Per-record timeUnixNano: lakerunner's `agent_session_events` PK is
    # (organization_id, session_id, chq_tsns), and chq_tsns server-side is
    # sourced from this `timeUnixNano`. If every record in this batch shared
    # one timestamp (the original Stop-firing time), only ONE row per
    # firing would survive the ON CONFLICT DO NOTHING — N-1 records would
    # silently vanish before the C3/A1/D1 detectors could see them.
    #
    # Offsetting by index (1 ns per record) is enough: we emit a small
    # bounded number of records (≤ MAX_TURN_USAGES + MAX_TURN_TOOLS = 320),
    # so the spread stays inside the nanosecond resolution chq_tsns
    # already uses. Two consecutive Stop firings can't collide because
    # `time.time_ns()` ticks far more than 320 ns between them.
    log_records = [
        {
            "timeUnixNano": str(now_ns + i),
            "observedTimeUnixNano": str(now_ns + i),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": p["event_name"]},
            "attributes": p["attributes"],
        }
        for i, p in enumerate(payloads)
    ]

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
                            "version": "0.11.0",
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
