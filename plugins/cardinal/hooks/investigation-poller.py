#!/usr/bin/env python3
"""cardinal investigation poller — the detached per-session process that
keeps a bound session's inbox of advisory events current
(cardinal_core.investigation_poller), so the PostToolUse fast path
(investigation-events.sh) never waits on the network.

Not a hook in hooks.json: started in the background by storyboard-session.py
(SessionStart) and by investigation-events.sh when the session's poller is
not running. Detaches into its own session, never writes to stdout/stderr,
exits when the Claude Code process it watches (--anchor, resolved to the
first non-shell ancestor) is gone, after 24 h, or when the session has no
binding and no pending bootstrap.

  investigation-poller.py --session SID [--anchor PID] [--once]

--once: one poll now, in the foreground (tests, debugging).

Env: CARDINAL_INVESTIGATION_POLL_INTERVAL (seconds, at least 1; default 5).
Only when connected (hooks/_connection.py); fails open, always exits 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402

INTERVAL_ENV = "CARDINAL_INVESTIGATION_POLL_INTERVAL"


def interval() -> float:
    from cardinal_core import investigation_poller as ip
    try:
        v = float(os.environ.get(INTERVAL_ENV) or ip.POLL_INTERVAL)
    except ValueError:
        v = ip.POLL_INTERVAL
    return max(ip.MIN_INTERVAL, v)


def main() -> None:
    parser = argparse.ArgumentParser(prog="investigation-poller.py", add_help=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--anchor", type=int)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not _connection.is_connected():
        return
    from cardinal_core import investigation_events as ie
    from cardinal_core import investigation_poller as ip
    import _storyboard_discovery
    if not ie.valid_session(args.session):
        return
    if not args.once:
        try:
            os.setsid()  # outlive the hook's process group
        except OSError:
            pass
    anchor = ip.resolve_anchor(args.anchor) if args.anchor and args.anchor > 1 else None
    home = Path(os.environ.get("HOME") or str(Path.home()))
    ip.run(home, args.session, connection=_storyboard_discovery.connection,
           client=_storyboard_discovery.client_header(), anchor=anchor,
           connected=_connection.is_connected, interval=interval(), once=args.once)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
