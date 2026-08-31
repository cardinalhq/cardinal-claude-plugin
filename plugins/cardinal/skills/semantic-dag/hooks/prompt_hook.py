#!/usr/bin/env python3
"""Keep Semantic DAG watch mode active across Claude prompt turns."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.path.expanduser(os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.claude/state/semantic-dag")))
EMITTER = Path(__file__).parents[1] / "emit.py"
SKILL = Path(__file__).parents[1] / "SKILL.md"
SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
DISABLE_RE = re.compile(r"^\s*(?:[$/])?semantic[- ]?dag\s+(?:off|stop|disable)\s*$", re.IGNORECASE)


def safe_id(value: str) -> str:
    return SAFE_RE.sub("_", value)[:128]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def short_topic(prompt: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", prompt)
    return " ".join(words[:6]) or "Continue watched task"


def discover_thread(payload: dict, session: str) -> str | None:
    binding_path = STATE_DIR / "bindings" / f"{safe_id(session)}.json"
    binding = read_json(binding_path)
    if isinstance(binding.get("thread"), str):
        return binding["thread"]
    scope = str(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    pointer = STATE_DIR / f"current-{hashlib.sha1(scope.encode()).hexdigest()[:12]}"
    try:
        thread = pointer.read_text().strip()
    except OSError:
        return None
    if thread:
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = binding_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"thread": thread}))
        temporary.replace(binding_path)
        return thread
    return None


def run_emit(thread: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_NO_SERVER"] = "1"
    environment["SEMANTIC_DAG_NO_OPEN"] = "1"
    subprocess.run(
        [sys.executable, str(EMITTER), *arguments, "--thread", thread],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    session = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
    if not session:
        return
    thread = discover_thread(payload, session)
    if not thread:
        return
    dag = read_json(STATE_DIR / "threads" / thread / "dag.json")
    if not dag.get("watch_mode"):
        return
    prompt = str(payload.get("user_prompt") or payload.get("prompt") or "").strip()
    if DISABLE_RE.fullmatch(prompt):
        run_emit(thread, "watch", "off")
        print(json.dumps({"systemMessage": "Semantic DAG watch mode disabled for this session."}))
        return
    run_emit(thread, "reset", short_topic(prompt))
    context = (
        "Persistent Semantic DAG watch mode is active for this Claude session. "
        f"Read and follow the semantic-dag skill at {SKILL} for this turn. "
        "Emit typed semantic nodes throughout the work and finish immediately before the final response. "
        "Immediately before each user-visible progress commentary, mirror the same sentence with `note` on the active node. "
        "The prompt hook has already repainted the existing viewer; do not open a separate DAG thread. "
        "Watch mode remains active on later prompts until the user submits `semantic-dag off`."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
