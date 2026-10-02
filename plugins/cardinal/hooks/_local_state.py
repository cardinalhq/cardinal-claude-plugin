"""The local state a Cardinal connection leaves on this machine, and how to
drop it. Shared by bin/cardinal-disconnect (full teardown), bin/cardinal-connect
(a reconnect as a different user or org) and hooks/_connection.py (the
disconnect marker).

Everything here is bound to the identity that was connected — the org, the
user, their keys — not to the machine, so none of it may outlive that
identity:

  ~/.claude/cardinal/                    runtime dir: spend-limit verdicts,
                                         decision ledgers + PR cache, plan
                                         stamp, storyboard-discovery blocks
                                         (the org's storyboard text), preview
                                         renders
  ~/.cardinal/evidence/<session>/token.json     24 h storyboard evidence
  ~/.cardinal/evidence/<session>/promoted.json  tokens and the receipts
                                         promoted with them (org-scoped)

The evidence spool entries themselves stay: they are this machine's own
captures, and capture works with no connection at all.

The disconnect marker (~/.claude/cardinal-disconnected): Claude Code loads
settings.json `env` into its process environment at startup, and removing a
key from the file does not unset it in a running session — hooks and Bash
keep inheriting the old CARDINAL_MCP_API_KEY until every Claude Code process
restarts. While the marker exists, the environment no longer counts as a
connection (only ~/.claude/settings.json and the state file do), so a
disconnect takes effect at once. /cardinal:connect removes it. Setups that
configure Cardinal through the environment alone never run disconnect and
never see it.

Never raises.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

DISCONNECTED_MARKER = Path(".claude") / "cardinal-disconnected"
RUNTIME_DIR = Path(".claude") / "cardinal"
EVIDENCE_ROOT = Path(".cardinal") / "evidence"
# The org-scoped files in each evidence session dir (cardinal_core.evidence
# TOKEN_FILE / PROMOTED_FILE).
EVIDENCE_IDENTITY_FILES = ("token.json", "promoted.json")


def home_dir(home: Path | None = None) -> Path:
    if home is not None:
        return home
    return Path(os.environ.get("HOME") or str(Path.home()))


def is_marked_disconnected(home: Path | None = None) -> bool:
    try:
        return (home_dir(home) / DISCONNECTED_MARKER).is_file()
    except OSError:
        return False


def mark_disconnected(home: Path | None = None) -> bool:
    """Write the marker. False when it could not be written."""
    path = home_dir(home) / DISCONNECTED_MARKER
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"disconnected_at": time.time()}) + "\n")
        return True
    except OSError:
        return False


def clear_disconnected(home: Path | None = None) -> None:
    try:
        (home_dir(home) / DISCONNECTED_MARKER).unlink()
    except OSError:
        pass


def clear_identity_caches(home: Path | None = None) -> tuple[list[str], list[str]]:
    """Remove the runtime dir and the evidence tokens/ledgers.

    Returns (removed, failed): human-readable paths that were removed, and
    paths that exist but could not be removed (so the caller never claims a
    removal that did not happen)."""
    home = home_dir(home)
    removed: list[str] = []
    failed: list[str] = []

    runtime = home / RUNTIME_DIR
    if runtime.exists():
        shutil.rmtree(runtime, ignore_errors=True)
        (failed if runtime.exists() else removed).append(str(runtime))

    root = home / EVIDENCE_ROOT
    n_removed = 0
    try:
        sessions = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    except OSError:
        sessions = []
    for session in sessions:
        for name in EVIDENCE_IDENTITY_FILES:
            path = session / name
            if not path.exists():
                continue
            try:
                path.unlink()
                n_removed += 1
            except OSError:
                failed.append(str(path))
    if n_removed:
        removed.append(f"{n_removed} evidence token/receipt file(s) under {root}")
    return removed, failed
