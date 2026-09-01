#!/usr/bin/env python3
"""Surface Claude subagent lifecycle in the Semantic DAG.

Claude Code fires PreToolUse and PostToolUse on the Task tool for each
subagent spawn. This hook emits agent_begin at PreToolUse (so the
subagent shows up as active in the agents view while it runs) and
agent_finish at PostToolUse (so the count settles). The agent id is
derived from subagent_type + a short hash of the description so
concurrent same-type spawns don't collide but repeat same-type/desc
launches within a session collapse onto one entry.

Best-effort: any parse error, missing binding, or subprocess failure is
silently swallowed. Never blocks the loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(
    os.path.expanduser(
        os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.cardinal/state/semantic-dag")
    )
)
EMITTER = Path(__file__).parents[1] / "emit.py"
SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(value: str) -> str:
    return SAFE_RE.sub("_", value)[:64] or "subagent"


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:8]


def _binding_thread(session: str) -> str | None:
    if not session:
        return None
    try:
        binding = json.loads(
            (STATE_DIR / "bindings" / f"{SAFE_RE.sub('_', session)[:128]}.json").read_text()
        )
        thread = binding.get("thread")
        return thread if isinstance(thread, str) and thread else None
    except (OSError, ValueError):
        return None


def _agent_identity(payload: dict) -> tuple[str, str, str]:
    """Return (agent_id, label, task) derived from the Task payload.

    Same subagent_type + description reproduces the same id in Pre/Post so
    finish lands on the entry begin created.
    """
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(tool_response, dict):
        tool_response = {}
    subagent_type = str(
        tool_response.get("agentType")
        or tool_input.get("subagent_type")
        or "subagent"
    ).strip() or "subagent"
    description = str(tool_input.get("description") or "").strip()
    label = subagent_type.replace("_", " ").replace("-", " ").title() or "Subagent"
    agent_id = _safe(subagent_type)
    if description:
        agent_id = f"{agent_id}-{_short_hash(description)}"
    return agent_id, label, description or subagent_type


def _run_emit(thread: str, agent: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_NO_SERVER"] = "1"
    environment["SEMANTIC_DAG_NO_OPEN"] = "1"
    environment["SEMANTIC_DAG_ROOT_THREAD"] = thread
    environment["SEMANTIC_DAG_AGENT"] = agent
    try:
        subprocess.Popen(
            [sys.executable, str(EMITTER), *arguments],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in ("Task", "Agent"):
        return 0
    session = str(payload.get("session_id") or "").strip()
    thread = _binding_thread(session)
    if not thread:
        return 0
    agent_id, label, task = _agent_identity(payload)
    event_name = str(payload.get("hook_event_name") or "")
    if event_name == "PreToolUse":
        # begin under this agent id — CLI creates the agents[agent_id] entry.
        _run_emit(
            thread,
            agent_id,
            "begin",
            task[:120] or "Subagent",
            "--agent-label",
            label,
        )
    elif event_name in ("PostToolUse", "SubagentStop"):
        tool_response = payload.get("tool_response") or {}
        summary = ""
        if isinstance(tool_response, dict):
            summary_value = tool_response.get("summary") or tool_response.get(
                "result"
            )
            if isinstance(summary_value, str):
                summary = summary_value.strip().splitlines()[0][:160]
        _run_emit(thread, agent_id, "finish", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
