#!/usr/bin/env python3
"""cardinal storyboard hero upload — PostToolUse hook on storyboard__publish.

maestro never renders a Canvas, so a link preview can show a scene only when
the plugin uploads its own local render. When storyboard__publish answers
with card.hero_upload (rich unfurls design §4.2), this hook picks the render
storyboard-preview.py wrote for that revision and PUTs it to the path publish
named. Core logic: cardinal_core.storyboard_hero.

Contract:
  - Input on stdin: Claude Code's PostToolUse payload. Acts only on Cardinal's
    own storyboard__publish (server `cardinal` or `plugin_cardinal_cardinal`)
    whose result has published: true, an integer revision and
    card.hero_upload. The result is unwrapped as storyboard-preview.py does
    (structuredContent, text content, spill notice).
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

import importlib.util
import json
import os
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

CARDINAL_SERVERS = ("cardinal", "plugin_cardinal_cardinal")
DISABLE_ENV = "CARDINAL_STORYBOARD_HERO"
OFF_VALUES = ("0", "false", "off", "no")
UPLOAD_TIMEOUT_S = 7.0
MAX_MESSAGE_CHARS = 200


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def is_cardinal_publish(name) -> bool:
    if not (isinstance(name, str) and name.startswith("mcp__")):
        return False
    server, sep, tool = name[len("mcp__"):].partition("__")
    return bool(sep) and server in CARDINAL_SERVERS and tool == "storyboard__publish"


def is_disabled() -> bool:
    try:
        env = json.loads((home_dir() / ".claude" / "settings.json").read_text()).get("env", {})
    except (OSError, ValueError, AttributeError):
        env = {}
    for source in (os.environ, env if isinstance(env, dict) else {}):
        value = source.get(DISABLE_ENV)
        if isinstance(value, str) and value.strip().lower() in OFF_VALUES:
            return True
    return False


def _unwrap_publish(tool_response):
    """The publish result, unwrapped by storyboard-preview.py's _unwrap."""
    spec = importlib.util.spec_from_file_location("_cardinal_storyboard_preview", HOOK_DIR / "storyboard-preview.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._unwrap(tool_response, want=lambda o: isinstance(o.get("published"), bool))


def _clip(s) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= MAX_MESSAGE_CHARS else s[: MAX_MESSAGE_CHARS - 1] + "…"


def client_string() -> str:
    try:
        import _plugin_version

        return "claude-plugin/" + _plugin_version.plugin_version()
    except Exception:
        return "claude-plugin/unknown"


SUMMARY = "link previews use Cardinal's summary card"


def message(result: dict, home: Path, conn: dict, opener=None) -> str | None:
    from cardinal_core import storyboard_hero

    card = result.get("card")
    if not isinstance(card, dict):
        return None
    upload = card.get("hero_upload")
    rev = result.get("revision")
    if not (isinstance(upload, dict) and isinstance(upload.get("path"), str)):
        return None
    if not (isinstance(rev, int) and not isinstance(rev, bool)):
        return None
    scene = card.get("hero_scene_id")
    designated = card.get("designated_cover") is True
    sb = result.get("storyboard_id")
    if not isinstance(scene, str):
        return None
    cap = upload.get("max_bytes")
    cap = min(cap, storyboard_hero.MAX_UPLOAD_BYTES) if isinstance(cap, int) and cap > 0 else \
        storyboard_hero.MAX_UPLOAD_BYTES
    pick = storyboard_hero.hero_png(home / ".claude" / "cardinal" / "storyboards", sb, rev, scene, designated, cap)
    if pick.status == "missing":
        return f"Cardinal: no local render of {scene} at revision {rev}; {SUMMARY}."
    if pick.status == "over_cap":
        return (f"Cardinal: your render of {scene} ({pick.path.name}) is {pick.size} bytes, over the {cap}-byte "
                f"upload cap (install Pillow to downscale it); {SUMMARY}.")
    try:
        res = storyboard_hero.upload_hero(conn, upload["path"], pick.png, UPLOAD_TIMEOUT_S, client_string(),
                                          opener=opener)
    except ValueError:
        return None
    if res.status is None:
        return f"Cardinal: the link-preview image upload failed ({res.error or 'no answer'}); {SUMMARY}."
    if not (200 <= res.status < 300) or res.body.get("stored") is False:
        code = res.body.get("error")
        detail = f"HTTP {res.status}" + (f" {code}" if isinstance(code, str) and code else "")
        msg = res.body.get("message")
        detail += (": " + _clip(msg)) if isinstance(msg, str) and msg else ""
        return f"Cardinal: the link-preview image was not uploaded ({detail}); {SUMMARY}."
    if pick.cover:
        return f"Cardinal: link previews use your cover render of {scene} (revision {rev})."
    if designated:
        return (f"Cardinal: link previews use your rendered {scene} (revision {rev}, its last reveal step; preview "
                f"with card to render the 1200x630 cover).")
    return (f"Cardinal: public links use your rendered {scene} (revision {rev}); member link previews use "
            f"Cardinal's summary card (no cover_scene designated).")


def main() -> None:
    if is_disabled():
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict) or not is_cardinal_publish(payload.get("tool_name")):
        return
    result = _unwrap_publish(payload.get("tool_response"))
    if not isinstance(result, dict) or result.get("published") is not True:
        return
    card = result.get("card")
    if not (isinstance(card, dict) and isinstance(card.get("hero_upload"), dict)):
        return
    import _storyboard_discovery

    conn = _storyboard_discovery.connection()
    if not (conn.get("key") and conn.get("org") and conn.get("origin")):
        return
    text = message(result, home_dir(), conn)
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
