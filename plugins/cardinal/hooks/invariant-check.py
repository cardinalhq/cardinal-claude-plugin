#!/usr/bin/env python3
"""cardinal invariant_check hook — PreToolUse on Edit|Write|MultiEdit.

Advisory-only guard: before Claude edits a file, builds a synthetic
unified diff of the intended change and runs it through Invariant's
`check-pr.ts` (conductor `packages/invariant`). If the edit touches code
covered by a `.invariants.yaml` guarantee, the guarantee's Markdown
fires as `additionalContext` so the model sees it before the edit lands.

Replaces `core/git-hooks/` (this PR reverts it): a manual per-repo
install step is exactly the friction the plugin system exists to
eliminate. This hook auto-installs with the plugin and fires on the
agent-about-to-edit surface — THESIS.md's surface-priority #1.

Contract: stdin is Claude Code's PreToolUse JSON ({tool_name,
tool_input: {file_path, ...}}); stdout is hookSpecificOutput.
additionalContext on a fire, else nothing. NEVER exits non-zero, NEVER
blocks — every failure path (missing conductor checkout, subprocess
error, malformed payload/diff, anything) exits 0 silently. Sync (no
async in hooks.json): must finish before the tool runs so its output
can reach the model's context.

check-pr.ts's diff parser only starts a chunk at a `diff --git a/X b/Y`
header, and its `touched_paths` globs are exact-anchored,
repo-root-relative strings — so the synthetic diff includes that
header, and the path is relativized against the edited file's git repo
root, falling back to the raw path (`touched_symbols` rules don't care
about path shape, so a fire is still possible without it).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

FIRE_RE = re.compile(r"^### (?:⚠️|\U0001f41b) `", re.MULTILINE)
CHECK_PR_TIMEOUT_SEC = 45
GIT_TIMEOUT_SEC = 3


def _find_invariant_dir() -> Path | None:
    for candidate in (os.environ.get("INVARIANT_PKG_DIR"), str(Path.home() / "workspace/conductor/packages/invariant")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def _relativize(file_path: str) -> str:
    """Repo-root-relative path when resolvable, else the raw path."""
    result = subprocess.run(
        ["git", "-C", os.path.dirname(file_path) or ".", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
    )
    if result.returncode == 0 and result.stdout.strip():
        rel = os.path.relpath(file_path, result.stdout.strip())
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return file_path


def _hunk(old_lines: list[str], new_lines: list[str]) -> list[str]:
    return [f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@", *(f"-{l}" for l in old_lines), *(f"+{l}" for l in new_lines)]


def _build_diff(tool_name: str, tool_input: dict, rel: str) -> str | None:
    lines = [f"diff --git a/{rel} b/{rel}"]
    if tool_name == "Edit":
        old, new = tool_input.get("old_string"), tool_input.get("new_string")
        if old is None or new is None:
            return None
        lines += [f"--- a/{rel}", f"+++ b/{rel}", *_hunk(old.split("\n"), new.split("\n"))]
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        hunks: list[str] = []
        for edit in edits:
            old, new = edit.get("old_string"), edit.get("new_string")
            if old is not None and new is not None:
                hunks += _hunk(old.split("\n"), new.split("\n"))
        if not hunks:
            return None
        lines += [f"--- a/{rel}", f"+++ b/{rel}", *hunks]
    elif tool_name == "Write":
        content = tool_input.get("content")
        if content is None:
            return None
        content_lines = content.split("\n")
        lines += ["--- /dev/null", f"+++ b/{rel}", f"@@ -0,0 +1,{len(content_lines)} @@", *(f"+{l}" for l in content_lines)]
    else:
        return None
    return "\n".join(lines) + "\n"


def _run_check_pr(invariant_dir: Path, diff_text: str) -> str:
    result = subprocess.run(
        ["pnpm", "--filter", "@cardinalhq/invariant", "exec", "tsx", "scripts/check-pr.ts", "--diff", "-", "--guarantees-dir", "."],
        cwd=str(invariant_dir),
        input=diff_text,
        capture_output=True,
        text=True,
        timeout=CHECK_PR_TIMEOUT_SEC,
    )
    return (result.stdout or "") + (result.stderr or "")


def run() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not tool_name or not file_path:
        return

    invariant_dir = _find_invariant_dir()
    if invariant_dir is None:
        return

    diff_text = _build_diff(tool_name, tool_input, _relativize(file_path))
    if not diff_text:
        return

    report = _run_check_pr(invariant_dir, diff_text)
    if "self-referential" in report.lower() or not FIRE_RE.search(report):
        return

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": f"⚠️ Invariant check-pr flagged this edit:\n\n{report}",
                }
            }
        )
    )


def main() -> None:
    try:
        run()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
