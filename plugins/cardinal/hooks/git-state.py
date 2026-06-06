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


HOOK_TIMEOUT_SEC = 2.0
_REMOTE_URL_RE = re.compile(
    r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$"
)


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


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        _silent_exit()

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers_raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
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
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs.setdefault("service.name", "claude-code")
    resource_attrs.setdefault("agent.runtime", "claude-code")

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
                            "version": "0.4.0",
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

    _silent_exit()


if __name__ == "__main__":
    main()
