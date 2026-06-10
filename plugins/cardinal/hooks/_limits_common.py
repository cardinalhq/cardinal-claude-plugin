"""Shared spend-limits delivery helpers for the cardinal hooks.

The limits feature is split across two hooks so the turn-critical path
never touches the network (conductor docs/specs/agent-spend-limits.md
§Delivery):

  git-state.py (async, per turn)   — calls maybe_refresh_verdict() after
                                     its OTLP post: GETs the maestro
                                     status endpoint when the cached
                                     verdict's TTL has lapsed, and
                                     atomically rewrites the verdict file.
  limits-gate.py (sync, per turn)  — reads the verdict file (file I/O
                                     only) and emits hook JSON.
  initiative-convention.py         — SessionStart: one synchronous fetch
                                     (short timeout, fail open) to inject
                                     budget standing from turn one.

File layout under ~/.claude/cardinal/limits/ — single-writer ownership,
no merge races:

  <session>.verdict.json   written by the async fetch (this module);
                           the server response plus a fetched_at stamp.
  <session>.ack.json       written by the sync gate; the last band it
                           surfaced (hysteresis state).
  <session>.override.json  written by /cardinal:override (phase 3); its
                           presence downgrades a block to warn-tier.

Everything here is best-effort: any failure returns None / does nothing.
A missing verdict means "allow" — fail open is the contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"
STATE_PATH = CLAUDE_DIR / "cardinal.json"
LIMITS_DIR = CLAUDE_DIR / "cardinal" / "limits"

FETCH_TIMEOUT_SEC = 2.0
# Default refresh cadence when the server response carried no ttl_seconds.
DEFAULT_TTL_SEC = 120
# Gate-side staleness: a warn/notify verdict older than this is ignored
# (fail open). Block verdicts stay honored longer — spend only grows, and
# the async hook refreshes every turn anyway.
WARN_MAX_AGE_SEC = 10 * 60
BLOCK_MAX_AGE_SEC = 60 * 60

_SESSION_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _read_json_file(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings_env() -> dict[str, str]:
    """The OTel env block cardinal-connect wrote into ~/.claude/settings.json.
    Claude Code does not propagate OTEL_* into hook subprocess envs, so the
    file is the source of truth (same as git-state.py)."""
    env = _read_json_file(SETTINGS_PATH).get("env") or {}
    return {k: v for k, v in env.items() if isinstance(v, str)}


def limits_config() -> dict | None:
    """The limits block cardinal-connect persisted from the device-flow
    bundle. None = server doesn't speak the protocol / not connected —
    every limits path is a no-op then (zero overhead for older backends)."""
    limits = _read_json_file(STATE_PATH).get("limits")
    if not isinstance(limits, dict):
        return None
    url = limits.get("status_url")
    if not url or not limits.get("enabled", True):
        return None
    return {"status_url": url}


def ingest_api_key(settings_env: dict[str, str] | None = None) -> str | None:
    """The plugin's ingest key, from OTEL_EXPORTER_OTLP_HEADERS — the same
    credential the status endpoint authenticates (and derives engineer
    identity from, server-side)."""
    env = settings_env if settings_env is not None else load_settings_env()
    raw = env.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip().lower() == "x-cardinalhq-api-key" and v.strip():
            return v.strip()
    return None


# ---------------------------------------------------------------------------
# Verdict / ack / override files
# ---------------------------------------------------------------------------

def _safe_session(session_id: str) -> str:
    return _SESSION_ID_SAFE.sub("_", session_id)[:128]


def verdict_path(session_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_session(session_id)}.verdict.json"


def ack_path(session_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_session(session_id)}.ack.json"


def override_path(session_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_session(session_id)}.override.json"


def read_verdict(session_id: str) -> dict | None:
    v = _read_json_file(verdict_path(session_id))
    return v or None


def atomic_write_json(path: Path, obj: dict) -> None:
    """tmp + rename so the sync gate never reads a half-written verdict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fetch + refresh
# ---------------------------------------------------------------------------

def fetch_status(
    status_url: str,
    api_key: str,
    session_id: str,
    repo: str | None,
    branch: str | None,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """One GET against maestro's /api/agent-limits/status. The server
    derives initiative + engineer identity itself; the client only ships
    raw git facts. Returns the parsed verdict or None on any failure."""
    params = {"session_id": session_id}
    if repo:
        params["repo"] = repo
    if branch:
        params["branch"] = branch
    url = status_url + ("&" if "?" in status_url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"x-cardinalhq-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def maybe_refresh_verdict(
    session_id: str,
    repo: str | None,
    branch: str | None,
    settings_env: dict[str, str] | None = None,
    force: bool = False,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """Refresh the session's verdict file if its server-assigned TTL has
    lapsed (or force=True). Returns the current verdict (fresh or cached),
    or None when limits aren't configured / everything failed."""
    cfg = limits_config()
    if not cfg:
        return None

    existing = read_verdict(session_id)
    if existing and not force:
        fetched_at = existing.get("fetched_at")
        ttl = existing.get("ttl_seconds") or DEFAULT_TTL_SEC
        if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < float(ttl):
            return existing

    api_key = ingest_api_key(settings_env)
    if not api_key:
        return existing

    verdict = fetch_status(cfg["status_url"], api_key, session_id, repo, branch, timeout=timeout)
    if verdict is None:
        return existing
    verdict["fetched_at"] = time.time()
    atomic_write_json(verdict_path(session_id), verdict)
    return verdict


# ---------------------------------------------------------------------------
# Git facts (used by the SessionStart standing fetch, which doesn't have
# git-state.py's locals in scope)
# ---------------------------------------------------------------------------

def git_facts(cwd: str) -> tuple[str | None, str | None]:
    """(repo 'org/name', branch) for cwd — best-effort, 1s per command."""

    def _git(args: list[str]) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    remote = _git(["remote", "get-url", "origin"])
    repo = None
    if remote:
        m = _REMOTE_URL_RE.match(remote.strip())
        if m:
            name = re.sub(r"\.git$", "", m.group(3))
            if m.group(2) and name:
                repo = f"{m.group(2)}/{name}"
    return repo, branch


# ---------------------------------------------------------------------------
# Standing summary (SessionStart + cardinal-status rendering)
# ---------------------------------------------------------------------------

def standing_lines(verdict: dict) -> list[str]:
    """Render the evaluations into short standing lines. This is data
    formatting only — all policy COPY (headlines, recommendations, block
    reasons) is server-authored and passed through verbatim."""
    evaluations = verdict.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        return []
    lines: list[str] = []
    for e in evaluations:
        if not isinstance(e, dict):
            continue
        try:
            scope = e.get("scope", "?")
            window = e.get("window")
            spent = float(e.get("spent_usd", 0))
            limit = float(e.get("limit_usd", 0))
            pct = int(round(float(e.get("fraction", 0)) * 100))
            set_by = e.get("set_by") or {}
            who = "you" if set_by.get("self") else set_by.get("display_name") or set_by.get("email") or "?"
            scope_label = f"{scope} ({window})" if scope == "engineer" and window else scope
            lines.append(
                f"- {scope_label}: ${spent:.2f} of ${limit:.2f} ({pct}%) — set by {who}"
            )
        except (TypeError, ValueError):
            continue
    return lines
