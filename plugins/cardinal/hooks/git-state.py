#!/usr/bin/env python3
"""cardinal git_state hook — UserPromptSubmit.

Reads git state for the current cwd and POSTs one OTLP/HTTP log event
with event_name='cardinal.git_state' so the lakerunner agent-sessions
processor can LWW {repo, branch, head_sha, cwd} onto the session row.

Contract (see ~/workspace/conductor/docs/specs/agent-sessions.md §Plugin
hook contract):
  - Input on stdin: Claude Code's UserPromptSubmit hook JSON
    {session_id, cwd, hook_event_name, prompt, ...}.
  - Env (set by cardinal-connect in ~/.claude/settings.json):
      OTEL_EXPORTER_OTLP_ENDPOINT  e.g. https://otelhttp.intake...
      OTEL_EXPORTER_OTLP_HEADERS   "x-cardinalhq-api-key=<key>"
      OTEL_RESOURCE_ATTRIBUTES     comma-separated key=value pairs
                                   (carries user.email, cardinal.org)
  - Behaviour: best-effort. Any failure (not in git, no env, network
    blip) → exit 0 silently. Never block the prompt.
  - Async: declared with async=true in hooks/hooks.json, so Claude
    Code already spawns this off the prompt-submit critical path.
    The POST itself uses a short timeout for belt-and-braces.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


HOOK_TIMEOUT_SEC = 2.0
_REMOTE_URL_RE = re.compile(
    r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$"
)

# Branches that are NOT initiatives — trunk lines where many concurrent
# pieces of work share the ref. Sessions here get type=research (the
# honest semantic match for un-branched scoping work) and no name (so
# the rollup doesn't collapse them into a single fake initiative).
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})

# Branch-prefix → initiative type mapping. Branches like `feat/foo-bar`
# carry the type in their prefix; we extract it. Aliases are folded
# in (feat/feature, fix/bugfix, chore/infra, spike/research) so common
# conventions all map cleanly.
_PREFIX_TO_TYPE: dict[str, str] = {
    "feat":     "feature",
    "feature":  "feature",
    "fix":      "bugfix",
    "bugfix":   "bugfix",
    "refactor": "refactor",
    "infra":    "infra",
    "chore":    "infra",
    "research": "research",
    "spike":    "research",
}

# Closed vocabulary downstream (lakerunner, conductor dashboard) treats
# as canonical. Kept here as the authoritative list so a typo in
# _PREFIX_TO_TYPE is a contained bug.
_INITIATIVE_TYPES = frozenset({
    "feature", "bugfix", "refactor", "infra", "research",
})

# Default type for branches that don't match a recognized prefix.
# "feature" is the modal piece of work in practice — least misleading
# fallback. Belt-and-suspenders: lakerunner column will also default
# to 'feature' if a session ever arrives without the attribute set.
_DEFAULT_TYPE = "feature"

# Slash-command detection (docs/skill-command-telemetry.md). User-typed
# skill invocations (`/code-review --fix`) never produce a Skill
# tool_result event, so this hook stamps the command NAME (never args —
# they can carry sensitive free text) onto the cardinal.git_state event.
# Two accepted shapes, because the UserPromptSubmit payload may carry the
# raw typed text or Claude Code's expanded <command-name> form:
#   raw:  "/code-review --fix"          → "code-review"
#   tag:  "<command-name>/foo</command-name>…" → "foo"
# Anchored at start (raw form) so a prompt that merely *mentions* a
# command mid-sentence does not match. Built-in CLI commands (/model,
# /clear, …) match too by design — the skill-vs-builtin distinction is
# a downstream (maestro) concern; a denylist here would rot.
_COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")
_COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")


def _detect_command(prompt: str | None) -> str | None:
    """'/code-review --fix' → 'code-review'; non-command prompts → None."""
    if not prompt:
        return None
    m = _COMMAND_RE.match(prompt)
    if m:
        return m.group(1)
    m = _COMMAND_TAG_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def _silent_exit() -> None:
    sys.exit(0)


def _git(args: list[str], cwd: str) -> str | None:
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


def _canonical_repo(remote_url: str) -> str | None:
    """git@github.com:org/name.git → 'org/name' (host-agnostic)."""
    m = _REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    _host, owner, name = m.group(1), m.group(2), m.group(3)
    name = re.sub(r"\.git$", "", name)
    return f"{owner}/{name}" if owner and name else None


def _parse_resource_attrs(raw: str) -> dict[str, str]:
    """Parse OTEL_RESOURCE_ATTRIBUTES (comma-separated k=v pairs)."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS (RFC 3986-ish, comma-separated)."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _kv(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _resolve_initiative(branch: str | None) -> tuple[str | None, str]:
    """Derive (name, type) from the current branch.

    The branch is the unit of an initiative — one branch, one piece of
    intended work. There is no priority chain, no file lookup, no env
    var, no conventional-commit fallback. Branch in, name + type out.

    Returns (name, type). `type` is ALWAYS one of `_INITIATIVE_TYPES`
    so downstream never has to handle null. `name` is None for
    protected/trunk branches (where many concurrent sessions share the
    ref) so the rollup doesn't fake an initiative out of unrelated
    work; otherwise it's the branch (or branch-tail after a known
    prefix) verbatim.

    Resolution:
      - None / "HEAD" / empty       → (None, "research")
      - Branch in protected set     → (None, "research")
      - `<prefix>/<rest>` w/ known
        prefix in `_PREFIX_TO_TYPE` → (rest, mapped type)
      - anything else               → (branch, "feature")
    """
    if not branch or branch == "HEAD":
        return None, "research"
    if branch in _PROTECTED_BRANCHES:
        return None, "research"
    if "/" in branch:
        prefix, _, rest = branch.partition("/")
        mapped = _PREFIX_TO_TYPE.get(prefix.lower())
        if mapped and rest:
            return rest, mapped
    return branch, _DEFAULT_TYPE


def _load_otel_settings() -> dict[str, str]:
    """Read the OTel env that cardinal-connect wrote into
    ~/.claude/settings.json. Claude Code itself uses these to configure its
    native exporter, but it does NOT propagate them into hook subprocess
    environments — empirically validated 2026-06-06 — so hook scripts must
    read the source of truth directly. See conductor docs/specs/agent-
    sessions.md §Plugin hook contract.
    """
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env") or {}
        return {k: v for k, v in env.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    # settings.json wins over env, because Claude Code strips OTEL_* and
    # CLAUDE_PROJECT_DIR from hook subprocess envs in practice.
    settings_env = _load_otel_settings()
    cwd = (
        payload.get("cwd")
        or settings_env.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    # Session id sourcing in priority order:
    #   1. stdin JSON `session_id`   (the canonical Claude Code hook payload)
    #   2. CLAUDE_CODE_SESSION_ID env (set by Claude Code on the parent)
    #   3. CLAUDE_SESSION_ID env      (legacy variant)
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        _silent_exit()

    endpoint = (
        settings_env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    headers_raw = (
        settings_env.get("OTEL_EXPORTER_OTLP_HEADERS")
        or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    )
    if not endpoint:
        _silent_exit()

    head_sha = _git(["rev-parse", "HEAD"], cwd)
    if head_sha is None:
        # Not a git repo (or git not installed). Nothing useful to send.
        _silent_exit()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote_url = _git(["remote", "get-url", "origin"], cwd)
    repo = _canonical_repo(remote_url) if remote_url else None

    resource_attrs = _parse_resource_attrs(
        settings_env.get("OTEL_RESOURCE_ATTRIBUTES")
        or os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")

    initiative_name, initiative_type = _resolve_initiative(branch)
    command = _detect_command(payload.get("prompt"))

    now_ns = time.time_ns()
    log_record = {
        "timeUnixNano": str(now_ns),
        "observedTimeUnixNano": str(now_ns),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": "cardinal.git_state"},
        "attributes": [
            _kv("event_name", "cardinal.git_state"),
            _kv("session_id", session_id),
            _kv("cardinal.cwd", cwd),
            _kv("cardinal.head_sha", head_sha),
            *([_kv("cardinal.branch", branch)] if branch else []),
            *([_kv("cardinal.repo", repo)] if repo else []),
            *(
                [_kv("cardinal.remote_url", remote_url)]
                if remote_url
                else []
            ),
            *(
                [_kv("cardinal.initiative.name", initiative_name)]
                if initiative_name
                else []
            ),
            # type is ALWAYS emitted — _resolve_initiative guarantees a
            # non-null value from the closed enum, so the lakerunner
            # column receives a real classification on every event.
            _kv("cardinal.initiative.type", initiative_type),
            # Slash-command name (never args) when this turn invoked one —
            # closes the user-typed-skill gap in the native telemetry.
            # Consumer accumulates per session (commands_used), not LWW.
            *([_kv("cardinal.command", command)] if command else []),
        ],
    }

    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        _kv(k, v) for k, v in resource_attrs.items()
                    ],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-claude-plugin",
                            "version": "0.8.0",
                        },
                        "logRecords": [log_record],
                    }
                ],
            }
        ]
    }

    url = endpoint.rstrip("/") + "/v1/logs"
    headers = {"Content-Type": "application/json"}
    headers.update(_parse_otlp_headers(headers_raw))

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    # Spend-limits verdict refresh (conductor docs/specs/agent-spend-limits.md
    # §Delivery). This hook is the async half: it re-fetches the verdict from
    # maestro when the server-assigned TTL has lapsed and rewrites the local
    # verdict file that the sync limits-gate.py hook reads. Runs AFTER the
    # OTLP post and stays best-effort — limits must never cost telemetry.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _limits_common

        _limits_common.maybe_refresh_verdict(
            session_id=session_id,
            repo=repo,
            branch=branch,
            settings_env=settings_env,
        )
    except Exception:
        pass

    _silent_exit()


if __name__ == "__main__":
    main()
