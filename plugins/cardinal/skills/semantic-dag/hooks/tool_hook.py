#!/usr/bin/env python3
"""Claude tool and file attribution hook for the Semantic DAG viewer."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
GENERIC_LABEL_RE = re.compile(r"\b(?:phase|stage|step|part|task)\s*[-:#]?\s*\d+\b", re.IGNORECASE)


def _work_label(topic: str) -> str:
    words = WORD_RE.findall(topic)
    label = " ".join(words[:6])
    if len(WORD_RE.findall(label)) < 2 or GENERIC_LABEL_RE.search(label):
        return "Investigate and act"
    return label


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
        os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.cardinal/state/semantic-dag")
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

    tool = str(payload.get("tool_name") or "tool")
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("cmd") if isinstance(tool_input, dict) else ""
    if isinstance(command, str) and "semantic-dag" in command and "emit.py" in command:
        return 0

    common = ["--thread", thread, "--agent", agent]

    def emit_sync(*arguments: str) -> None:
        environment = os.environ.copy()
        environment["SEMANTIC_DAG_NO_SERVER"] = "1"
        environment["SEMANTIC_DAG_NO_OPEN"] = "1"
        subprocess.run(
            [sys.executable, str(EMIT), *arguments, *common],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )

    if not active:
        try:
            turn_n = int(dag.get("turn", 1))
        except (TypeError, ValueError):
            turn_n = 1
        turns_list = dag.get("turns") or []
        topic = ""
        if turns_list and isinstance(turns_list[-1], dict):
            topic = str(turns_list[-1].get("topic") or "").strip()
        topic = topic or str(dag.get("topic") or "").strip()
        work_id = f"turn-{turn_n}-work"
        add_args = ["add", work_id, "WORK", _work_label(topic)]
        goal_id = f"turn-{turn_n}-goal"
        if agent == "root" and goal_id in (dag.get("nodes") or {}):
            add_args += ["--parent", goal_id]
        elif agent == "root":
            add_args += ["--root"]
        emit_sync(*add_args)
        emit_sync("activate", work_id)
        active = work_id

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
