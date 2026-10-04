#!/usr/bin/env python3
"""cardinal storyboard context — PreToolUse hook on Cardinal's
storyboard__create, storyboard__add_act, storyboard__publish,
storyboard__find and storyboard__link.

Stamps where a storyboard act is written from, so the agent never has to
run `cardinal-storyboard context` and paste it (that CLI stays the
fallback). The fields are provenance (written_from), never what the
storyboard is about: this hook never writes `about`.

Contract:
  - Input on stdin: Claude Code's PreToolUse payload {tool_name,
    tool_input, session_id, cwd, ...}.
  - Output, only when something was added: hookSpecificOutput
    {hookEventName: "PreToolUse", updatedInput} where updatedInput is a
    copy of EVERY tool_input key plus what was added. No
    permissionDecision: the permission prompt (if any) shows the stamped
    input. Requires a Claude Code that applies updatedInput to MCP tools
    (README: minimum version).
  - session_id (create, add_act, find): this session's id when absent and
    valid (^[A-Za-z0-9_-]{1,128}$).
  - context (create, add_act, publish, find): only when absent or {}, from
    cardinal_core.storyboard_context.collect (client claude-code/<ver>,
    actor_email, the files this session edited as `paths`); the branch's PR
    from `gh` within GH_TIMEOUT_S on create / add_act / publish, from the gh
    cache only on find. A context the model set is never changed.
    publish is stamped only when the cached server capability
    (associations_api, server-caps.json) is >= 1: an older gateway's
    publish rejects an unknown `context` argument.
  - find with refs.commits, at capability >= 1: the PRs those commits name
    locally (`git log -1 --format=%s%n%b <sha>`: "… (#N)", "Merge pull
    request #N"; at most MAX_COMMITS) are added to refs.prs.
  - storyboard__link: nothing to stamp (its about refs are the model's).
  - agent_id (a subagent's call) is noted in the opt-in debug log only
    (_hook_debug); nothing is sent.
  - Opt-out: CARDINAL_STORYBOARD_CONTEXT=0 (false / off / no), in the
    environment or settings.json `env`.
  - Fail open: any exception prints nothing, so the call runs unchanged.
    Always exits 0. hooks.json gives it 3 s.
"""

from __future__ import annotations

import time

STARTED = time.monotonic()

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DISABLE_ENV = "CARDINAL_STORYBOARD_CONTEXT"
OFF_VALUES = ("0", "false", "off", "no")
TOOL_RE = re.compile(r"^mcp__(?:plugin_cardinal_)?cardinal__storyboard__(create|add_act|publish|find|link)$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
SESSION_TOOLS = ("create", "add_act", "find")
CONTEXT_TOOLS = ("create", "add_act", "publish", "find")
GH_TOOLS = ("create", "add_act", "publish")
GH_TIMEOUT_S = 2.0
MAX_COMMITS = 10
MAX_PRS = 50


def disabled() -> bool:
    import _storyboard_discovery

    for source in (os.environ, _storyboard_discovery._settings_env(_storyboard_discovery.home_dir())):
        value = source.get(DISABLE_ENV)
        if isinstance(value, str) and value.strip().lower() in OFF_VALUES:
            return True
    return False


def plugin_client() -> str:
    try:
        from cardinal_core import evidence
        from _plugin_version import plugin_version
        return evidence.client_string("claude-code", plugin_version())
    except Exception:
        return "claude-code"


def actor_email():
    try:
        import _otel_settings
        return _otel_settings.resource_attrs(_otel_settings.load_otel_settings()).get("user.email")
    except Exception:
        return None


def pr_resolver(tool: str):
    """gh within GH_TIMEOUT_S on create / add_act / publish; the gh cache
    only on find (it runs on every lookup)."""
    from cardinal_core import decisions, storyboard_context
    import _storyboard_discovery

    cache_dir = decisions.cache_dir(_storyboard_discovery.runtime_dir())
    if tool not in GH_TOOLS:
        return storyboard_context.cache_only_pr_resolver(cache_dir)

    def resolve(cwd: str, repo: str, branch: str):
        return decisions.resolve_pr(cwd, repo, branch, cache_dir, timeout=GH_TIMEOUT_S)

    return resolve


def stamp_context(tool: str, cwd: str, session_id) -> dict:
    from cardinal_core import storyboard_context, storyboard_files
    import _storyboard_discovery

    files = _storyboard_discovery.files_dir()
    edited = (lambda repo: storyboard_files.for_repo(files, session_id, repo)) if session_id else None
    return storyboard_context.collect(cwd, client=plugin_client(), actor_email=actor_email(),
                                      pr_resolver=pr_resolver(tool), edited_paths=edited)


def prs_of_commits(cwd: str, commits) -> list:
    """The PRs the given commits' local messages name (subjects first)."""
    from cardinal_core.initiative import git
    from cardinal_core.storyboard_discovery import pr_from_subject

    out: list = []
    for sha in [c for c in commits if isinstance(c, str) and COMMIT_RE.match(c.strip().lower())][:MAX_COMMITS]:
        msg = git(["log", "-1", "--format=%s%n%b", sha.strip().lower(), "--"], cwd)
        if not msg:
            continue
        found = pr_from_subject(msg.split("\n", 1)[0])
        if found is None:
            for line in msg.split("\n")[1:]:
                if line.startswith("Merge pull request #"):
                    found = pr_from_subject(line)
                    break
        if found is not None and found not in out:
            out.append(found)
    return out


def add_prs(refs: dict, prs: list) -> bool:
    existing = refs.get("prs")
    current = list(existing) if isinstance(existing, list) else ([] if existing is None else [existing])
    seen = {str(p).lstrip("#") for p in current if isinstance(p, (int, str)) and not isinstance(p, bool)}
    added = [p for p in prs if str(p) not in seen]
    if not added:
        return False
    refs["prs"] = (current + added)[:MAX_PRS]
    return True


def updated_input(payload: dict):
    """The stamped copy of tool_input, or None when nothing was added."""
    m = TOOL_RE.match(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not m or not isinstance(tool_input, dict):
        return None
    tool = m.group(1)
    if tool == "link":
        return None
    out = dict(tool_input)  # every original key, unchanged
    changed = False
    cwd = payload.get("cwd")
    if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
        cwd = os.getcwd()
    sid = payload.get("session_id")
    sid = sid if isinstance(sid, str) and SESSION_RE.match(sid) else None

    if tool in SESSION_TOOLS and out.get("session_id") in (None, "") and sid:
        out["session_id"] = sid
        changed = True

    caps = None
    if tool in ("publish", "find"):
        import _storyboard_discovery
        conn = _storyboard_discovery.connection()
        caps = _storyboard_discovery.server_caps(conn) if conn.get("key") else None

    ctx_missing = "context" not in out or out.get("context") is None or out.get("context") == {}
    if tool in CONTEXT_TOOLS and ctx_missing and (tool != "publish" or (caps or 0) >= 1):
        ctx = stamp_context(tool, cwd, sid)
        if ctx:
            out["context"] = ctx
            changed = True

    refs = out.get("refs")
    if tool == "find" and (caps or 0) >= 1 and isinstance(refs, dict):
        commits = refs.get("commits")
        commits = commits if isinstance(commits, list) else [commits]
        prs = prs_of_commits(cwd, commits)
        if prs:
            refs = dict(refs)
            if add_prs(refs, prs):
                out["refs"] = refs
                changed = True
    return out if changed else None


def main() -> None:
    if disabled():
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict) or payload.get("hook_event_name", "PreToolUse") != "PreToolUse":
        return
    out = updated_input(payload)
    try:
        import _hook_debug
        _hook_debug.log("storyboard-context", STARTED, tool=payload.get("tool_name"),
                        agent_id_present="agent_id" in payload, stamped=out is not None)
    except Exception:
        pass
    if out is None:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": out,
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
    os._exit(0)
