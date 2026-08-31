#!/usr/bin/env python3
"""Claude Code entrypoint for the shared Cardinal Semantic DAG emitter."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for candidate in (PLUGIN_ROOT / "hooks", REPO_ROOT / "core"):
    if (candidate / "cardinal_core" / "semantic_dag.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from cardinal_core.semantic_dag import RuntimeConfig, main  # noqa: E402


CONFIG = RuntimeConfig(
    runtime="claude",
    default_state_dir="~/.claude/state/semantic-dag",
    default_port=8765,
    viewer_dir=Path(__file__).resolve().parent / "viewer",
    native_thread_env=("CLAUDE_SESSION_ID",),
    project_dir_env="CLAUDE_PROJECT_DIR",
)


if __name__ == "__main__":
    raise SystemExit(main(CONFIG))
