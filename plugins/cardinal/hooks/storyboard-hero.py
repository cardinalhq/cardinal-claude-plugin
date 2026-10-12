#!/usr/bin/env python3
"""cardinal storyboard hero upload — PostToolUse hook on storyboard__publish.

maestro never renders a Canvas, so a link preview can show a scene only when
the plugin uploads its own local render. When storyboard__publish answers
with card.hero_upload (rich unfurls design §4.2), this hook picks the render
storyboard-preview.py wrote for that revision and PUTs it to the path publish
named. Core logic, shared with the other runtimes:
cardinal_core.storyboard_authoring.on_publish (over storyboard_hero).

Contract:
  - Input on stdin: Claude Code's PostToolUse payload. Acts only on Cardinal's
    own storyboard__publish (server `cardinal` or `plugin_cardinal_cardinal`)
    whose result has published: true, an integer revision and
    card.hero_upload. The result is unwrapped as storyboard-preview.py does
    (structuredContent, text content, spill notice;
    storyboard_authoring.unwrap).
  - revision is the PRE-publish revision R: the r<R>/ directory the preview
    rendered into. The file is r<R>/<scene>-cover.png when the scene is the
    designated cover and the file exists, else the scene's last reveal step
    r<R>/<scene>-<N>.png. hero_upload.path (already carrying ?revision=R)
    is used unchanged and must be the connected org's hero route.
  - The PUT carries the MCP key (X-CardinalHQ-API-Key), X-Cardinal-Client:
    claude-plugin/<version> and Content-Type: image/png, and never follows a
    redirect.
  - Output: one additionalContext line saying which image link previews use
    (the cover render, the rendered scene, or Cardinal's summary card and
    why). An HTTP error (409 stale_revision, 413, 422, 429, ...) is reported,
    never raised.
  - Silent no-op: CARDINAL_STORYBOARD_HERO=0 (environment or settings.json
    env), not connected, another server's tool, an unpublished or card-less
    result, unreadable input. Bounded by UPLOAD_TIMEOUT_S under hooks.json's
    10 s; never exits non-zero, never blocks the tool.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
try:
    from cardinal_core import storyboard_authoring as sa  # noqa: E402
except Exception:  # not vendored: fail open
    if __name__ == "__main__":
        sys.exit(0)
    raise

RUNTIME = "claude"
DISABLE_ENV = sa.HERO_DISABLE_ENV
UPLOAD_TIMEOUT_S = sa.UPLOAD_TIMEOUT_S
SUMMARY = sa.SUMMARY


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def client_string() -> str:
    try:
        import _plugin_version

        return "claude-plugin/" + _plugin_version.plugin_version()
    except Exception:
        return "claude-plugin/unknown"


def message(result: dict, home: Path, conn: dict, opener=None) -> str | None:
    """The hero upload for a published result (storyboard_authoring.publish_message)."""
    return sa.publish_message(sa.agent_paths(RUNTIME, home), result, conn, client_string(), opener=opener,
                              timeout=UPLOAD_TIMEOUT_S)


def connection() -> dict:
    import _storyboard_discovery

    return _storyboard_discovery.connection()


def main() -> None:
    paths = sa.agent_paths(RUNTIME, home_dir())
    if sa.hero_disabled(paths, RUNTIME):
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict) or not sa.is_cardinal_tool(RUNTIME, payload.get("tool_name"),
                                                                "storyboard__publish"):
        return
    text = sa.on_publish(paths, RUNTIME, payload.get("tool_response"), connection=connection,
                         client=client_string())
    if not text:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text},
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
