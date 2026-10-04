"""Opt-in local debug log for the storyboard hooks.

CARDINAL_HOOK_DEBUG=1 (true / on / yes), in the environment or in
~/.claude/settings.json `env`, appends one JSON line per hook run to
~/.claude/cardinal/hook-debug.log (0600): the hook, its event, how long it
took and what it decided (a dedupe hit, a request, whether a PreToolUse
payload carried agent_id). Never the tool input, a key or a path outside
the repo. Off by default; never raises; stdlib only (the edit-lookup hook
calls it on its fast path).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEBUG_ENV = "CARDINAL_HOOK_DEBUG"
ON_VALUES = ("1", "true", "on", "yes")
MAX_LOG_BYTES = 1 << 20


def _home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def enabled() -> bool:
    try:
        value = os.environ.get(DEBUG_ENV)
        if value is None:
            env = json.loads((_home() / ".claude" / "settings.json").read_text()).get("env", {})
            value = env.get(DEBUG_ENV) if isinstance(env, dict) else None
        return isinstance(value, str) and value.strip().lower() in ON_VALUES
    except Exception:
        return False


def log(hook: str, started: float, **fields) -> None:
    """Append {at, hook, ms, **fields} when enabled. `started` is the hook's
    time.monotonic() at process start."""
    try:
        if not enabled():
            return
        path = _home() / ".claude" / "cardinal" / "hook-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_suffix(".log.1"))
        line = json.dumps({"at": round(time.time(), 3), "hook": hook,
                           "ms": round((time.monotonic() - started) * 1000, 1), **fields}, default=str)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
