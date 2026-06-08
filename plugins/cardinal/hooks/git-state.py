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
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){0,3}$")
# Conventional-commit subject: `<type>(<scope>)[!]: <description>`.
# `<type>` is letters; scope is required for our extraction (no scope →
# no signal). Allows the breaking-change `!` marker.
_CONVENTIONAL_RE = re.compile(r"^[a-zA-Z]+\((?P<scope>[^)]+)\)!?:\s")


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


def _resolve_initiative(
    cwd: str, settings_env: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve (name, description) for cardinal.initiative.* attributes.

    Priority chain per docs/specs/cardinal-initiative.md §"Plugin behavior":
      1. CARDINAL_INITIATIVE env var (or settings.json env block)
      2. .cardinal-initiative JSON at repo root — {name, description}
      3. Branch-name prefix: feat/foo-bar → foo
      4. Conventional-commit scope of HEAD: feat(baz): … → baz

    Description is only sourced from (2). The other sources yield a name
    only — caller emits the description attribute only when non-None.
    Returns (None, None) when no signal matches.
    """
    # 1. Shell env var override. Read both surfaces — settings.json env
    #    block (the OTEL pattern in this file) and the process env — so
    #    devs can set it either way regardless of Claude Code's env
    #    stripping behavior on hook subprocesses.
    env_name = (
        settings_env.get("CARDINAL_INITIATIVE")
        or os.environ.get("CARDINAL_INITIATIVE", "")
    ).strip()
    if env_name:
        return env_name, None

    # 2. .cardinal-initiative at repo root.
    repo_root = _git(["rev-parse", "--show-toplevel"], cwd)
    if repo_root:
        f = Path(repo_root) / ".cardinal-initiative"
        if f.exists():
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Malformed → log to stderr and fall through to next signal.
                print(
                    "cardinal: .cardinal-initiative is not valid JSON; "
                    "ignoring",
                    file=sys.stderr,
                )
                doc = None
            if isinstance(doc, dict):
                name = doc.get("name")
                desc = doc.get("description")
                if isinstance(name, str) and _KEBAB_RE.match(name):
                    description = (
                        desc.strip()
                        if isinstance(desc, str) and desc.strip()
                        else None
                    )
                    return name, description

    # 3. Branch-name prefix.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch and "/" in branch:
        tail = branch.split("/", 1)[1]
        first_segment = tail.split("-", 1)[0].strip()
        if first_segment:
            return first_segment, None

    # 4. Conventional-commit scope of HEAD subject.
    subject = _git(["log", "-1", "--format=%s", "HEAD"], cwd)
    if subject:
        m = _CONVENTIONAL_RE.match(subject)
        if m:
            scope = m.group("scope").strip()
            if scope:
                return scope, None

    return None, None


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

    initiative_name, initiative_desc = _resolve_initiative(cwd, settings_env)

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
            *(
                [_kv("cardinal.initiative.description", initiative_desc)]
                if initiative_desc
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
                            "version": "0.5.0",
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
