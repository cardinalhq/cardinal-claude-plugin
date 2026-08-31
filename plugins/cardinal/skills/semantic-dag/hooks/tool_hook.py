#!/usr/bin/env python3
"""Claude tool and file attribution hook for the Semantic DAG viewer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _load_file_event_classifier():
    source = Path(__file__).resolve()
    candidates = [parent / "core" for parent in source.parents]
    candidates.extend(parent / "hooks" for parent in source.parents)
    for candidate in candidates:
        if (candidate / "cardinal_core" / "file_events.py").is_file():
            sys.path.insert(0, str(candidate))
            from cardinal_core.file_events import file_events_from_hook

            return file_events_from_hook
    return lambda _payload: []


file_events_from_hook = _load_file_event_classifier()

STATE_DIR = Path(
    os.path.expanduser(
        os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.claude/state/semantic-dag")
    )
)
EMIT = Path(__file__).resolve().parents[1] / "emit.py"


def _summary(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "cmd", "command", "url", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:60]
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    session = str(payload.get("session_id") or "").strip()
    binding = {}
    if session:
        try:
            value = json.loads((STATE_DIR / "bindings" / f"{session}.json").read_text())
            if isinstance(value, dict):
                binding = value
        except (OSError, ValueError):
            pass
    thread = str(
        os.environ.get("SEMANTIC_DAG_ROOT_THREAD")
        or binding.get("thread")
        or session
    ).strip()
    if not thread or not (STATE_DIR / "threads" / thread / "dag.json").exists():
        return 0
    try:
        dag = json.loads((STATE_DIR / "threads" / thread / "dag.json").read_text())
    except (OSError, ValueError):
        return 0
    agent = str(
        os.environ.get("SEMANTIC_DAG_AGENT") or binding.get("agent") or "root"
    ).strip()
    active = (dag.get("active_by_agent") or {}).get(agent)
    if not active and agent == "root":
        active = dag.get("active")
    if not active:
        return 0

    tool = str(payload.get("tool_name") or "tool")
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("cmd") if isinstance(tool_input, dict) else ""
    if isinstance(command, str) and "semantic-dag" in command and "emit.py" in command:
        return 0

    common = ["--thread", thread, "--agent", agent]

    def spawn(*arguments: str) -> None:
        subprocess.Popen(
            [sys.executable, str(EMIT), *arguments, *common],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )

    if str(payload.get("hook_event_name") or "") == "PreToolUse":
        spawn("tool", str(active), tool, _summary(tool_input))
    for kind, path in file_events_from_hook(payload):
        spawn("file", str(active), kind, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
