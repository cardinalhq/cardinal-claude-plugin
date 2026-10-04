#!/usr/bin/env python3
"""cardinal storyboard edit lookup — PreToolUse hook on Edit, Write,
MultiEdit and NotebookEdit.

Before the agent first edits a file, puts the Cardinal storyboards ABOUT
that file (or about a PR that last changed it) in its context, so a change
starts from what the org already established about the code. Core logic:
cardinal_core.storyboard_discovery.lookup_for_path.

A separate process from invariant-check (same matcher entry in hooks.json):
a slow lookup must never delay or fail the invariant check.

Contract:
  - Input on stdin: Claude Code's PreToolUse payload {session_id, cwd,
    tool_name, tool_input: {file_path | notebook_path, ...}}.
  - Fast path, standard library only: a directory this session already
    looked up, or EDIT_MAX_LOOKUPS (6) lookups already made, exits before
    any other import (~/.claude/cardinal/storyboard-edit-lookup/<session>.json).
  - No network unless connected with an MCP key and the cached server
    capability (server-caps.json, written by storyboard discovery and by
    this hook's own find answers) is >= 1. Unconnected or capability < 1 /
    unknown leaves a per-session marker (<session>.skip) so later edits
    exit on the fast path too, until it is SKIP_TTL_S old or settings.json,
    cardinal.json or server-caps.json changes after it.
  - One find {refs: {repo, paths: [file], prs: PRs that last changed it}},
    within 1.5 s of process start; only about path / about pr matches not
    already shown to this session (discovery's block or an earlier edit).
  - Output: hookSpecificOutput {hookEventName: "PreToolUse",
    additionalContext} of at most 1 KB, framed as DATA. Never a
    permissionDecision: the edit always proceeds.
  - Opt-out: CARDINAL_STORYBOARD_DISCOVERY=0 (the same switch as session
    discovery). Fail open: never blocks, never prints an error, exits 0.
"""

from __future__ import annotations

import time

STARTED = time.monotonic()

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DISABLE_ENV = "CARDINAL_STORYBOARD_DISCOVERY"
OFF_VALUES = ("0", "false", "off", "no")
# Mirrors storyboard_discovery.EDIT_MAX_LOOKUPS / EDIT_BUDGET_S (the fast
# path cannot import it).
MAX_LOOKUPS = 6
BUDGET_S = 1.5
SKIP_TTL_S = 600
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def home_dir() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def state_dir() -> str:
    return os.path.join(home_dir(), ".claude", "cardinal", "storyboard-edit-lookup")


def disabled() -> bool:
    value = os.environ.get(DISABLE_ENV)
    if value is None:
        try:
            with open(os.path.join(home_dir(), ".claude", "settings.json"), encoding="utf-8") as f:
                env = json.load(f).get("env", {})
            value = env.get(DISABLE_ENV) if isinstance(env, dict) else None
        except Exception:
            value = None
    return isinstance(value, str) and value.strip().lower() in OFF_VALUES


def skip_marker(session_id: str) -> str:
    return os.path.join(state_dir(), _SAFE_RE.sub("_", session_id)[:128] + ".skip")


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def ineligible(session_id: str) -> bool:
    """A recent 'not connected / capability < 1' marker that nothing it
    depends on has changed since."""
    marked = _mtime(skip_marker(session_id))
    if not marked or time.time_ns() - marked > SKIP_TTL_S * 1_000_000_000:
        return False
    claude = os.path.join(home_dir(), ".claude")
    deps = (os.path.join(claude, "settings.json"), os.path.join(claude, "cardinal.json"),
            os.path.join(claude, "cardinal", "server-caps.json"))
    return all(_mtime(p) < marked for p in deps)


def mark_ineligible(session_id: str) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(skip_marker(session_id), "w", encoding="utf-8"):
            pass
        os.utime(skip_marker(session_id), None)
    except Exception:
        pass


def already_done(session_id: str, directory: str) -> bool:
    """The fast-path dedupe (storyboard_discovery.read_edit_state / dir_key)."""
    path = os.path.join(state_dir(), _SAFE_RE.sub("_", session_id)[:128] + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    key = hashlib.sha256(directory.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    count = state.get("count")
    dirs = state.get("dirs")
    return (isinstance(dirs, list) and key in dirs) or (isinstance(count, int) and count >= MAX_LOOKUPS)


def debug(**fields) -> None:
    try:
        sys.path.insert(0, HOOKS_DIR)
        import _hook_debug
        _hook_debug.log("storyboard-edit-lookup", STARTED, **fields)
    except Exception:
        pass


def main() -> None:
    if disabled():
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict) or payload.get("tool_name") not in EDIT_TOOLS:
        return
    sid = payload.get("session_id")
    tool_input = payload.get("tool_input")
    if not isinstance(sid, str) or not sid or not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(file_path, str) or not file_path or "\0" in file_path:
        return
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd and os.path.isdir(cwd) else os.getcwd()
    full = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    directory = os.path.dirname(os.path.realpath(full))
    if already_done(sid, directory):
        debug(result="dedupe")
        return
    if ineligible(sid):
        debug(result="ineligible")
        return

    sys.path.insert(0, HOOKS_DIR)
    import _connection
    if not _connection.is_connected():
        mark_ineligible(sid)
        return
    import _storyboard_discovery
    from pathlib import Path
    from cardinal_core import storyboard_discovery

    conn = _storyboard_discovery.connection()
    if not conn.get("key") or not conn.get("org"):
        mark_ineligible(sid)
        return
    caps = _storyboard_discovery.server_caps(conn)
    if caps is None or caps < 1:
        mark_ineligible(sid)
        return
    block = storyboard_discovery.lookup_for_path(
        file_path,
        cwd=cwd,
        conn=conn,
        session_id=sid,
        state_dir=Path(state_dir()),
        caps=caps,
        client=_storyboard_discovery.client_header(),
        discovery_state_dir=_storyboard_discovery.state_dir(),
        caps_path=_storyboard_discovery.caps_path(),
        deadline=STARTED + BUDGET_S,
    )
    debug(result="block" if block else "none")
    if not block:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": block,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    # An abandoned request thread (the deadline passed) must not hold the
    # process open.
    os._exit(0)
