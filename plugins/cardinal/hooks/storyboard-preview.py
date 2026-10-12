#!/usr/bin/env python3
"""cardinal storyboard auto-preview — PostToolUse hook on storyboard__preview.

When `storyboard__preview` returns, this hook hands the result to the
storyboard skill's local renderer (skills/storyboard/scripts/render_preview.py
--runtime claude), which fetches each scene's preview bundle with the
Cardinal MCP key, renders it in the user's own sandboxed, network-locked
Chromium and writes one PNG per reveal step to
~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/. The hook then puts
the PNG paths (and any per-scene render errors) in Claude's context, so Claude
Reads them instead of rebuilding the renderer input by hand (first real e2e
run, friction 7). render_preview.py stays the manual fallback (the canvas
skill says how). Core logic, shared with the other runtimes:
cardinal_core.storyboard_authoring.on_preview.

Contract:
  - Input on stdin: Claude Code's PostToolUse payload {tool_name, tool_input,
    tool_response, session_id, ...}. For an MCP tool, Claude Code 2.1.283
    sends the same object the transcript stores as tool_use_result:
    {content: "<result text>", structuredContent: {...result}} (every preview
    in the first real e2e run). Also accepted: `content` as a string without
    structuredContent, the bare result text, a list of {type: "text", text}
    blocks, or {content: [blocks]}. An oversized result may arrive as Claude
    Code's "... Output has been saved to <file>." notice; the file is read
    only when it resolves under ~/.claude/projects/.
  - Output: hookSpecificOutput.additionalContext (a few KB at most) naming the
    PNG directory, each scene's PNG files in step order or its render error,
    and one instruction to Read every step. Frame and renderer error text
    comes from the scene's own (untrusted) code and is labelled as quoted
    data, not instructions.
  - Silent when the tool is not Cardinal's storyboard__preview (server
    "cardinal" or the plugin's "plugin_cardinal_cardinal",
    storyboard_authoring.is_cardinal_tool: the result names the pages to
    fetch with the Cardinal key, so another server's tool of the same name
    never reaches the renderer), the result is an error or has no scenes,
    or the payload is unreadable.
  - No local Chromium (renderer exit 3): says so once per session (a marker
    keyed by session_id; `claude --resume` keeps the id, so a resumed session
    stays quiet), then stays silent.
  - The renderer crashes with no output: one line saying so, with the
    exception's class name only (never its message, which could carry the
    key), and the manual fallback.
  - The link preview (rich unfurls design §5.2): when the result carries a
    `card` block, after the scene render (a) card.cover_render renders that
    scene's last step at 1200x630 (render_preview.py --cover, r<rev>/<scene>-
    cover.png); (b) with card.summary_svg or a cover render, r<rev>/unfurl-
    mock.html (storyboard_authoring.mock_html: a Slack-like attachment, every
    string escaped, the image a data: URI) is rendered to unfurl-mock.png
    (--static-html: scripts off, same network lock); (c) the context names the
    mock and what to judge in it. Each step runs only with MIN_COVER_S /
    MIN_MOCK_S of the budget left; no Chromium means no mock and the
    session's one notice.
  - Bounded: the renderer gets --timeout RENDER_TIMEOUT_S and the hook ends it
    after HOOK_BUDGET_S, both below the hooks.json timeout, so Claude always
    gets an answer. Fail open: never exits non-zero, never blocks the tool.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cardinal_core import evidence  # noqa: E402,F401  (the spill-file follower on_preview uses)
    from cardinal_core import storyboard_authoring as sa  # noqa: E402
except Exception:  # not vendored: fail open
    if __name__ == "__main__":
        sys.exit(0)
    raise

RUNTIME = "claude"
HOOK_DIR = Path(__file__).resolve().parent
RENDERER = HOOK_DIR.parent / "skills" / "storyboard" / "scripts" / "render_preview.py"

RENDER_TIMEOUT_S = sa.RENDER_TIMEOUT_S
HOOK_BUDGET_S = sa.PREVIEW_BUDGET_S
KILL_GRACE_S = sa.KILL_GRACE_S
MIN_COVER_S = sa.MIN_COVER_S
MIN_MOCK_S = sa.MIN_MOCK_S
MAX_CONTEXT_CHARS = sa.MAX_CONTEXT_CHARS
READ_INSTRUCTION = sa.READ_INSTRUCTION
crash_context = sa.crash_context
scene_lines = sa.scene_lines


def paths() -> sa.AgentPaths:
    return sa.agent_paths(RUNTIME)


def build_context(result: dict, tool_input: dict, records: list, summary, code, timed_out: bool,
                  session_id, reserve: int = 0):
    """The scene-render context (storyboard_authoring.build_context) for Claude's paths."""
    return sa.build_context(paths(), result, tool_input, records, summary, code, timed_out, session_id,
                            reserve=reserve, budget=sa.preview_budget())


def main() -> None:
    started = time.monotonic()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if not sa.is_cardinal_tool(RUNTIME, payload.get("tool_name"), "storyboard__preview"):
        return
    ctx = sa.on_preview(paths(), RUNTIME, payload.get("tool_response"), session_id=payload.get("session_id"),
                        tool_input=payload.get("tool_input"), renderer=RENDERER, started=started)
    if not ctx:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx},
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
