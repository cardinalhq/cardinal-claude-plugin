#!/usr/bin/env python3
"""cardinal code routing — SessionStart hook.

Tells Claude to prefer the cardinal-code MCP server's symbolic
navigation tools (Serena-backed: find_symbol, find_referencing_symbols,
get_symbols_overview, find_implementations, search_for_pattern) over
the Explore agent / large file reads for symbol-shaped questions.

Why this exists: the Outcomes Dashboard shows Explore burns the
overwhelming majority of agent-attributed tokens, and a big slice of
that is structural navigation — "where is X / what calls Y / what
implements Z" — which LSP-style tools answer in tens of tokens instead
of the hundreds it takes to grep+read whole files. Routing only
matters if Claude actually reaches for the cheap tool first; without
this hook the trained prior toward Explore wins.

Contract:
  - Input on stdin: SessionStart hook JSON payload.
  - Output: hookSpecificOutput.additionalContext when cwd is inside a
    git repo. Outside of source trees there's nothing to navigate, so
    we suppress the prompt to avoid wasted context.
  - Best-effort: any failure exits 0 silently. Never blocks session
    start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


PROMPT = (
    "The `cardinal-code` MCP server is connected. It exposes "
    "read-only LSP-backed symbolic navigation tools (find_symbol, "
    "find_referencing_symbols, get_symbols_overview, "
    "find_implementations, search_for_pattern). For 'where is X "
    "defined / what calls Y / what does this module export / what "
    "implements Z'-shaped questions, PREFER these tools before "
    "spawning the Explore agent or reading whole files. They return "
    "compact, precise results and are dramatically cheaper in "
    "tokens. Fall back to Explore or direct file reads when the "
    "symbol can't be resolved or you genuinely need to read "
    "prose-shaped logic across a region of code."
)


def _is_git_repo(cwd: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    cwd = (
        payload.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    if not _is_git_repo(cwd):
        sys.exit(0)

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": PROMPT,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
