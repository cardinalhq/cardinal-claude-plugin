"""Plan-state cache helper — plan-state.py + plan-usage.py shared backend.

Owns:
- OAuth token sourcing (macOS keychain, then ~/.claude/.credentials.json).
- Profile + usage fetch against api.anthropic.com with strict allowlist.
- Atomic cache read/write (tmp + os.replace) at ~/.claude/cardinal/plan.json.
- Cache invalidation via sha256(token)[:16] fingerprint mismatch.
- billing_mode derivation.
- stamp_attrs() helper for downstream hooks that piggyback the cached
  plan_type + rate_limit_tier on every emitted record.

Privacy contract (spec docs/specs/plan-state-telemetry.md §Privacy):
- Allowlist-only. Any field NOT on the allowlist is dropped from API
  responses before being written to the cache or returned to the caller.
- The OAuth access token never appears in the cache file, OTLP payloads,
  or log lines. Only its sha256 prefix is persisted as token_fingerprint.
- Silent failure on every error path — plan-state is enrichment, not gating.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Module-level so tests can monkeypatch with `unittest.mock.patch(
# "_plan_cache._BASE", "http://127.0.0.1:NNNN")`. Tests running the hook
# as a subprocess use CARDINAL_PLAN_OAUTH_BASE_URL instead.
_BASE = os.environ.get("CARDINAL_PLAN_OAUTH_BASE_URL") or "https://api.anthropic.com"
_PROFILE_PATH = "/api/oauth/profile"
_USAGE_PATH = "/api/oauth/usage"

_HTTP_TIMEOUT_SEC = 5.0
# One retry on transient failures (URLError/TimeoutError/OSError). /api/oauth/usage
# is consistently slower than /api/oauth/profile and the prior 2s ceiling left
# real cold-start SessionStarts without a usage half, so cardinal.plan_usage never
# emitted. 4xx/5xx are NOT retried — they don't recover from an immediate retry
# and would just double load on api.anthropic.com.
_FETCH_RETRIES = 1

# Profile cache TTL: subscription changes through Stripe propagate within
# minutes server-side; daily refresh catches them without paying for the
# call every session.
_PROFILE_TTL_SEC = 24 * 3600
# Usage cache TTL: 10 min is the cadence the Stop hook checks.
_USAGE_TTL_SEC = 10 * 60

# --- Allowlist (spec §Cache file → Schema) ---------------------------------
#
# These are the only fields we write to plan.json and the only fields any
# emit-side projection draws from. Anything appearing in an API response
# but not listed here is dropped at the projection function; there is no
# pass-through path.
_PROFILE_ALLOWED_FIELDS = {
    "plan_type",
    "rate_limit_tier",
    "organization_type",
    "billing_type",
    "has_extra_usage_enabled",
    "billing_mode",
}
_USAGE_ALLOWED_WINDOWS = {
    "five_hour",
    "seven_day",
    "seven_day_sonnet",
    "seven_day_opus",
}
_USAGE_ALLOWED_WINDOW_FIELDS = {"utilization", "resets_at"}


def _cache_path() -> Path:
    return Path.home() / ".claude" / "cardinal" / "plan.json"


# --- Token sourcing --------------------------------------------------------


def _token_from_keychain() -> str | None:
    """macOS keychain: the credentials entry is a JSON blob, the access
    token lives at claudeAiOauth.accessToken. Linux returns None here."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", "Claude Code-credentials",
                "-a", os.environ.get("USER", ""),
                "-w",
            ],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    raw = out.stdout.strip()
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(blob, dict):
        return None
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    tok = oauth.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _token_from_file() -> str | None:
    """Linux fallback (and macOS fallback when the keychain entry is
    missing): ~/.claude/.credentials.json with the same blob layout."""
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    tok = oauth.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _get_token() -> str | None:
    return _token_from_keychain() or _token_from_file()


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# --- HTTPS fetchers --------------------------------------------------------


def _fetch_json(token: str, path: str) -> dict | None:
    """GET BASE+path with the OAuth bearer. Returns the parsed JSON object
    on 2xx, or None on any failure (network, non-2xx, non-JSON, non-dict).
    Retries once on transient errors (URLError/TimeoutError/OSError); 4xx/5xx
    surface immediately. Errors are SWALLOWED — the token MUST NOT escape
    via repr/log."""
    req = urllib.request.Request(
        _BASE.rstrip("/") + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "cardinal-claude-plugin/0.11.3",
        },
        method="GET",
    )
    for attempt in range(_FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            # HTTPError is a subclass of URLError — catch first so 4xx/5xx
            # short-circuit without consuming the retry budget.
            return None
        except (urllib.error.URLError, OSError, TimeoutError):
            if attempt < _FETCH_RETRIES:
                continue
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# --- Derivations -----------------------------------------------------------


def _derive_plan_type(profile: dict) -> str | None:
    """Synthesize plan_type from raw profile fields. See spec §plan_type
    derivation. Returns None (NOT "unknown") when no rule matches — a
    sentinel would pollute cap-derivation groupings."""
    account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
    org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
    if account.get("has_claude_max") is True:
        return "max"
    if account.get("has_claude_pro") is True:
        return "pro"
    org_type = org.get("organization_type")
    if org_type == "team":
        return "team"
    if org_type == "enterprise":
        return "enterprise"
    return None


def billing_mode_of(plan_type: str | None, has_extra: bool | None) -> str | None:
    """spec §billing_mode derivation."""
    if plan_type is None:
        return None
    if plan_type == "api":
        return "usage_based"
    if has_extra is True:
        return "hybrid"
    return "plan_based"


# --- Projections (allowlist enforcement) -----------------------------------


def _project_profile(profile: dict) -> dict:
    """Build a NEW dict using only the six allowlisted fields, derived
    from the raw API response. NEVER `update()`s the raw response into
    the output — that would defeat the allowlist on future Anthropic-side
    field additions."""
    org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
    plan_type = _derive_plan_type(profile)
    has_extra = org.get("has_extra_usage_enabled")
    if not isinstance(has_extra, bool):
        has_extra = None
    projected: dict[str, Any] = {}
    if plan_type is not None:
        projected["plan_type"] = plan_type
    rt = org.get("rate_limit_tier")
    if isinstance(rt, str) and rt:
        projected["rate_limit_tier"] = rt
    ot = org.get("organization_type")
    if isinstance(ot, str) and ot:
        projected["organization_type"] = ot
    bt = org.get("billing_type")
    if isinstance(bt, str) and bt:
        projected["billing_type"] = bt
    if has_extra is not None:
        projected["has_extra_usage_enabled"] = has_extra
    bm = billing_mode_of(plan_type, has_extra)
    if bm is not None:
        projected["billing_mode"] = bm
    # Belt-and-suspenders: never let an unknown allowlist field slip in.
    return {k: v for k, v in projected.items() if k in _PROFILE_ALLOWED_FIELDS}


def _project_usage(usage: dict) -> dict:
    """Build a NEW dict containing only the four allowlisted windows, each
    with only the two allowlisted fields (utilization, resets_at). A
    window present but null in the response is OMITTED entirely from
    the projection — never represented as a "null" string later when we
    emit attributes."""
    out: dict[str, dict[str, Any]] = {}
    for window in _USAGE_ALLOWED_WINDOWS:
        bucket = usage.get(window)
        if not isinstance(bucket, dict):
            # Null at the top level (e.g. seven_day_opus often null), or
            # window absent. Skip — don't store {} which would later be
            # mistaken for "present but empty".
            continue
        cleaned: dict[str, Any] = {}
        for field in _USAGE_ALLOWED_WINDOW_FIELDS:
            v = bucket.get(field)
            if v is None:
                continue
            cleaned[field] = v
        if cleaned:
            out[window] = cleaned
    return out


# --- Cache read/write ------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read() -> dict | None:
    """Read the cached plan blob. None on absence, OSError, or unparseable."""
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def _write_cache(blob: dict) -> None:
    """Atomic write (tmp + os.replace) so two SessionStart hooks racing on
    plan.json can't produce a torn file. Last writer wins on contents,
    which is fine: profile fields are identical for the same user across
    concurrent sessions, and a slightly-later usage snapshot is no worse
    than an earlier one for delta purposes (each plan_usage event carries
    its own snapshot on the wire)."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(blob))
    os.replace(tmp, path)


# --- Public refresh API ----------------------------------------------------


def _expired(iso: str | None, ttl_sec: int) -> bool:
    when = _parse_iso(iso)
    if when is None:
        return True
    return (datetime.now(timezone.utc) - when).total_seconds() >= ttl_sec


def refresh_plan_state(force_profile: bool = False) -> dict | None:
    """Ensure profile + usage are fresh and the cache is written.

    Returns the cached blob (with the merged projection) on success;
    None on any failure or no-token branch.

    Called by plan-state.py at SessionStart. Honors:
      - sha256(token)[:16] fingerprint mismatch → full refetch.
      - profile TTL (24 h) and usage TTL (10 min) unless force_profile.
      - No-token branch: emits {plan_type: api, billing_mode: usage_based}
        but does NOT call api.anthropic.com.
    """
    token = _get_token()
    if not token:
        # API-key user / no OAuth credentials. Synthesize the minimal
        # entry without any network call so downstream hooks still get
        # a stamp. No usage data exists for API-key users.
        blob = {
            "token_fingerprint": None,
            "profile_fetched_at": _now_iso(),
            "usage_fetched_at": None,
            "plan_type": "api",
            "billing_mode": "usage_based",
            "usage": {},
        }
        try:
            _write_cache(blob)
        except OSError:
            return None
        return blob

    fp = _fingerprint(token)
    cached = read()
    if cached and cached.get("token_fingerprint") == fp and not force_profile:
        profile_stale = _expired(cached.get("profile_fetched_at"), _PROFILE_TTL_SEC)
        usage_stale = _expired(cached.get("usage_fetched_at"), _USAGE_TTL_SEC)
    else:
        # Fingerprint mismatch OR cache absent OR forced. Drop everything.
        cached = None
        profile_stale = True
        usage_stale = True

    blob: dict[str, Any] = dict(cached) if cached else {}
    blob["token_fingerprint"] = fp

    if profile_stale:
        profile_raw = _fetch_json(token, _PROFILE_PATH)
        if profile_raw is not None:
            projected = _project_profile(profile_raw)
            # Replace the six profile fields wholesale — don't merge raw.
            for k in _PROFILE_ALLOWED_FIELDS:
                blob.pop(k, None)
            blob.update(projected)
            blob["profile_fetched_at"] = _now_iso()

    if usage_stale:
        usage_raw = _fetch_json(token, _USAGE_PATH)
        if usage_raw is not None:
            blob["usage"] = _project_usage(usage_raw)
            blob["usage_fetched_at"] = _now_iso()

    # If we have nothing useful, don't persist a half-empty cache.
    if "plan_type" not in blob and "usage" not in blob:
        return None

    try:
        _write_cache(blob)
    except OSError:
        return None
    return blob


def refresh_usage_only() -> dict | None:
    """Fetch /api/oauth/usage only; merge into existing cache; return blob.
    Called by plan-usage.py at Stop after the caller has already passed
    the 10-min throttle check. This helper always fetches when called —
    throttling lives in the caller."""
    token = _get_token()
    if not token:
        return None
    cached = read()
    if not isinstance(cached, dict):
        # No prior cache (e.g. plan-state.py never ran). plan-usage.py
        # bails before reaching here, but be safe.
        return None
    usage_raw = _fetch_json(token, _USAGE_PATH)
    if usage_raw is None:
        return None
    cached["usage"] = _project_usage(usage_raw)
    cached["usage_fetched_at"] = _now_iso()
    try:
        _write_cache(cached)
    except OSError:
        return None
    return cached


# --- Stamping helper for downstream hooks ----------------------------------


def stamp_attrs() -> list[dict]:
    """Return event-level attributes to stamp onto downstream-hook records.

    Empty list when cache absent or fields missing — caller treats that
    as the no-op case (existing behavior preserved when plan-state hasn't
    run yet).
    """
    blob = read()
    if not isinstance(blob, dict):
        return []
    out: list[dict] = []
    pt = blob.get("plan_type")
    if isinstance(pt, str) and pt:
        out.append({"key": "plan_type", "value": {"stringValue": pt}})
    rt = blob.get("rate_limit_tier")
    if isinstance(rt, str) and rt:
        out.append({"key": "rate_limit_tier", "value": {"stringValue": rt}})
    return out
