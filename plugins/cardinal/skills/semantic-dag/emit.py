#!/usr/bin/env python3
"""Claude Code entrypoint for the shared Cardinal Semantic DAG emitter."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for candidate in (REPO_ROOT / "core", PLUGIN_ROOT / "hooks"):
    if (candidate / "cardinal_core" / "semantic_dag.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from cardinal_core.semantic_dag import RuntimeConfig, main  # noqa: E402


CONFIG = RuntimeConfig(
    runtime="claude",
    default_state_dir="~/.cardinal/state/semantic-dag",
    default_port=8766,
    viewer_dir=(
        REPO_ROOT / "common" / "semantic-dag" / "viewer"
        if (REPO_ROOT / "common" / "semantic-dag" / "viewer").is_dir()
        else Path(__file__).resolve().parent / "viewer"
    ),
    plugin_root=PLUGIN_ROOT,
    emit_path=Path(__file__).resolve(),
    # Same precedence every other Claude hook uses (see hooks/git-state.py):
    # Claude Code exports CLAUDE_CODE_SESSION_ID; CLAUDE_SESSION_ID is only a
    # legacy variant. Naming just the legacy one left `_native_session_id()`
    # permanently None, which silently disabled session-keyed bindings, wrote
    # `session_id: null` pointers, and stopped the prompt hook from ever
    # adopting its own thread — so watch mode never engaged on Claude.
    native_thread_env=("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
    project_dir_env="CLAUDE_PROJECT_DIR",
)


if __name__ == "__main__":
    raise SystemExit(main(CONFIG))
