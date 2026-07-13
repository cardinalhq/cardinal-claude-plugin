#!/usr/bin/env python3
"""cardinal turn_usage hook — Stop.

Emits per-model-call telemetry for the user turn that just completed:
- cardinal.turn_usage : one record per model call with the API usage object.
- cardinal.turn_tool  : one record per tool_use block, linked by turn_seq.

Why a hook at all: claude-code rolls up per-turn usage and per-tool inputs
into session-grain attributes before they leave the harness, so
server-side cannot reconstruct per-model-call deltas. The transcript JSONL
on disk has every record verbatim; this hook reads the slice belonging to
the current user turn and emits chunked OTLP POSTs.

Contract:
  - Input on stdin: Stop hook JSON {session_id, transcript_path, ...}.
  - Env: same OTLP settings as git-state.py (read from
    ~/.claude/settings.json because Claude Code does not propagate OTEL_*
    into hook subprocesses).
  - Behaviour: best-effort, exit 0 silently on any failure.
  - Async (hooks.json): never blocks the loop returning to the model.

Transcript walking is Claude-specific and stays here; Bash verb
classification and timestamp parsing come from the vendored
cardinal_core. See docs/specs/per-turn-telemetry.md for the schema and
the privacy boundary on `target` capture, and
docs/specs/subagent-telemetry-enrichment.md for chunked emission,
user_turn_seq, and the bash_class closed enum.
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
from cardinal_core.bashclass import classify_bash_command  # noqa: E402
from cardinal_core.otlp import emit_records, kv, parse_ts_ns  # noqa: E402

HOOK_TIMEOUT_SEC = 2.0

# Emission bounds (docs/specs/subagent-telemetry-enrichment.md §Field 2).
# Long turns are chunked into POSTs of ≤BATCH_MAX_RECORDS logRecords
# rather than dropped. The absolute ceiling protects the hook process
# from genuinely pathological transcripts; past it, truncated=true.
BATCH_MAX_RECORDS = 256
MAX_RECORDS_PER_FIRING = 4096

# Privacy boundary (spec §Privacy) — only file-path-shaped inputs are
# emitted as `target`. Bash command, Grep pattern, MCP args are dropped.
# Membership in this dict IS the allowlist.
TARGET_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


def _silent_exit() -> None:
    sys.exit(0)


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


def _extract_target(tool_name: str, tool_input) -> str | None:
    key = TARGET_KEYS.get(tool_name)
    if key is None or not isinstance(tool_input, dict):
        return None
    path = tool_input.get(key)
    return path if isinstance(path, str) and path else None


def _walk_current_turn(transcript_path: Path) -> tuple[list[dict], int]:
    """Return (records, user_turn_seq) for the user turn that just
    ended: everything after the most recent 'real' user message, plus
    the 1-based ordinal of that turn within the session (spec §Field 3;
    tool_result-only continuations do not increment it).

    Streaming forward — memory is bounded by the current turn's record
    count, not total transcript size. If no boundary is found (first
    turn or truncated transcript), returns everything seen with
    user_turn_seq=0 (ordinal unknown)."""
    current_turn: list[dict] = []
    user_turn_seq = 0
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
                    user_turn_seq += 1
                    continue
                current_turn.append(rec)
    except (OSError, UnicodeDecodeError):
        return [], 0
    return current_turn, user_turn_seq


def _build_records(
    records: list[dict],
    session_id: str,
    now_ns: int,
    user_turn_seq: int,
) -> list[dict]:
    """Map current-turn records to a flat list of (event_name, attrs)
    payloads ready to render as OTLP logRecords. Enforces the
    MAX_RECORDS_PER_FIRING ceiling (batching handles the ≤256-per-POST
    bound)."""
    out: list[tuple[str, list[dict]]] = []
    turn_seq = 0
    truncated = False
    # Plan-state stamps: empty list when ~/.claude/cardinal/plan.json is
    # absent. Appended to every emitted record without changing existing
    # attribute order.
    plan_extras = _plan_cache.stamp_attrs()

    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue

        if len(out) >= MAX_RECORDS_PER_FIRING:
            truncated = True
            break

        ts_ns = parse_ts_ns(rec.get("timestamp"), now_ns)
        usage_attrs = [
            kv("event_name", "cardinal.turn_usage"),
            kv("session_id", session_id),
            kv("ts", ts_ns),
            # user_turn_seq=0 means the boundary was never seen (e.g.
            # truncated transcript) — omit rather than guess an ordinal.
            *([kv("user_turn_seq", user_turn_seq)] if user_turn_seq else []),
            kv("turn_seq", turn_seq),
        ]
        model = msg.get("model")
        if isinstance(model, str) and model:
            usage_attrs.append(kv("model", model))
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            v = usage.get(key)
            if isinstance(v, (int, float)):
                usage_attrs.append(kv(key, int(v)))
        usage_attrs.extend(plan_extras)
        out.append(("cardinal.turn_usage", usage_attrs))

        content = msg.get("content")
        hit_ceiling = False
        if isinstance(content, list):
            tool_seq = 0
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if len(out) >= MAX_RECORDS_PER_FIRING:
                    truncated = True
                    hit_ceiling = True
                    break
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                tool_attrs = [
                    kv("event_name", "cardinal.turn_tool"),
                    kv("session_id", session_id),
                    kv("ts", ts_ns),
                    *([kv("user_turn_seq", user_turn_seq)] if user_turn_seq else []),
                    kv("turn_seq", turn_seq),
                    kv("tool_seq", tool_seq),
                    kv("tool_name", tool_name),
                ]
                target = _extract_target(tool_name, block.get("input"))
                if target is not None:
                    tool_attrs.append(kv("target", target))
                if tool_name == "Bash":
                    # Closed-enum verb class only (spec §Field 4); the
                    # command string never leaves this process.
                    tool_input = block.get("input")
                    command = (
                        tool_input.get("command")
                        if isinstance(tool_input, dict) else None
                    )
                    if isinstance(command, str) and command:
                        classified = classify_bash_command(command)
                        if classified is not None:
                            bash_class, bash_multi = classified
                            tool_attrs.append(kv("bash_class", bash_class))
                            if bash_multi:
                                tool_attrs.append(kv("bash_multi", True))
                tool_attrs.extend(plan_extras)
                out.append(("cardinal.turn_tool", tool_attrs))
                tool_seq += 1

        turn_seq += 1
        if hit_ceiling:
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
                attrs.append(kv("truncated", True))
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

    settings_env = _otel_settings.load_otel_settings()
    connection = _otel_settings.ingest_connection(settings_env)
    if connection is None:
        _silent_exit()

    current_turn, user_turn_seq = _walk_current_turn(transcript_path)
    if not current_turn:
        _silent_exit()

    now_ns = time.time_ns()
    payloads = _build_records(current_turn, session_id, now_ns, user_turn_seq)
    if not payloads:
        _silent_exit()

    resource = _otel_settings.resource_attrs(settings_env)

    # Per-record timeUnixNano: lakerunner's `agent_session_events` PK is
    # (organization_id, session_id, chq_tsns), and chq_tsns server-side is
    # sourced from `timeUnixNano`. If every record in this firing shared
    # one timestamp, only ONE row per firing would survive the ON CONFLICT
    # DO NOTHING. Offsetting by GLOBAL index (1 ns per record) is enough,
    # and the index runs CONTINUOUSLY across the ≤256-record batches — a
    # per-batch restart would collide chq_tsns between batches of the
    # same firing. Total spread ≤ MAX_RECORDS_PER_FIRING = 4096 ns.
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

    # Chunked emission (spec §Field 2): one POST per ≤BATCH_MAX_RECORDS
    # slice, in order. Each POST is independently best-effort — a failed
    # batch drops only its own slice, never the ones after it.
    for start in range(0, len(log_records), BATCH_MAX_RECORDS):
        emit_records(
            log_records[start:start + BATCH_MAX_RECORDS],
            connection,
            resource,
            scope_name="cardinal-claude-plugin",
            scope_version=_plugin_version.plugin_version(),
            timeout=HOOK_TIMEOUT_SEC,
        )

    _silent_exit()


if __name__ == "__main__":
    main()
