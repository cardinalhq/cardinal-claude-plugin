#!/usr/bin/env python3
"""cardinal storyboard session — SessionStart hook: this session's id, its
automatic Investigation and live Storyboard, and the connect hint.

Puts this Claude Code session's id in Claude's context, makes sure the
session has its Investigation and live Storyboard on Cardinal (connected
only; nobody starts one), and tells Claude the private storyboard URL and to
give it whenever the user asks for the storyboard. The id is what the
storyboard skill passes as `session_id` to `storyboard__find` and
`storyboard__add_act` (conductor docs/specs/investigation-storyboards.md
§15).

Contract:
  - Input on stdin: Claude Code's SessionStart payload {session_id, ...}.
    Falls back to $CLAUDE_CODE_SESSION_ID / $CLAUDE_SESSION_ID.
  - Output: hookSpecificOutput.additionalContext with one sentence, in any
    directory (a storyboard does not need a git repo). SessionStart also
    fires on resume / clear / compact, so the id survives compaction.
  - No session-id sentence when there is no id, or the id is not one maestro accepts
    (^[A-Za-z0-9_-]{1,128}$, routes/storyboards-mcp-tools.ts CreateSchema),
    or when not connected (no storyboard__create to pass it to).
  - Not connected (hooks/_connection.py: no Cardinal key, ingest key or
    connect state): one line on how to get write access (sign up at
    app.cardinalhq.io, then /cardinal:connect, which stores an API key) and why /mcp
    lists `cardinal` as missing CARDINAL_MCP_URL. The first unconnected
    startup on this machine phrases it as "tell the user once" (marker
    ~/.cardinal/connect-hint); later sessions keep it as context only, for
    Claude to use if the user asks about Cardinal or storyboards. The text
    and marker are core's (cardinal_core.storyboard_authoring.connect_hint,
    runtime "claude"); the connected test stays _connection.is_connected.
  - Stray-key warning (storyboard_authoring.stray_key_warning, the rule
    bin/cardinal-status also reports): when CARDINAL_MCP_API_KEY is set but
    CARDINAL_MCP_URL is not, the cardinal server has no URL although the
    hooks count the machine as connected. Adds a short "tell the user" note
    to the context — at most once per session id (marker under
    ~/.cardinal/key-warning/; without an id, only on startup).
  - Automatic Investigation (connected, valid session id only): every
    session gets an Investigation and its live Storyboard with no command
    (cardinal_core.investigation_bootstrap.ensure). A binding that already
    bootstrapped (restart, resume, compaction) is reused with its cursor and
    no request (its capabilities are refreshed afterwards by the background
    poller, for the next start); otherwise one ensure-session-investigation request (3 s;
    idempotent per session, so concurrent hooks and resumes get the same
    pair). CARDINAL_INVESTIGATION_ID=inv_... at launch is an explicit join
    of that investigation instead (never creates one). The binding:
    ~/.cardinal/investigations/sessions/<session_id>.json. Then the
    session's background poller (investigation-poller.py) is started,
    unless CARDINAL_INVESTIGATION_POLLER=0. The context (the shared session
    guidance, cardinal_core.investigation_text.session_text) gets the
    investigation id, the labeled links (the Storyboard, a private draft
    until published, and the Investigation timeline, the one to share before
    any publish) and how to answer "what's the storyboard link?" (give the
    URLs; the user never starts a storyboard), session-owned visualization
    guidance, and the advisory-events line. A failure gets at most one
    short clause (a fixed phrase, never server text) and is retried in the
    background with back-off; an older Cardinal without the route: the
    session id sentence as before.
  - Semantic checkpoints (investigation_text.CHECKPOINT_LINE): only a session that authors its
    bootstrapped Investigation is told to checkpoint material changes in
    its understanding (`cardinal-storyboard investigation checkpoint`) right
    then, for a reader who sees only the record: sparse claims with their
    why, not facts, never reasoning or routine tool use. A joined
    non-author session cannot checkpoint and is not told to; nothing else
    is said when unconnected or unbound.
  - Owner input transparency: only when this session's prompts are recorded
    (owner-input.py): it authors its Investigation, the server advertised
    capabilities.owner_input.enabled: true, and the kill switch is not set
    (CARDINAL_OWNER_INPUT=0, environment, user or project settings;
    _owner_input.py). Then the user is shown the disclosure as a top-level
    `systemMessage` (user-visible, not model context) and the model's
    context gets the same sentence (owner_input.DISCLOSURE), informational, not an
    instruction. Only as the very last step, after that output was written
    and flushed, does the binding record it (owner_input_disclosed: capture
    starts only once it is set; a failed write records nothing). The kill
    switch makes the binding forget it, so removing the switch discloses
    again before capture resumes. Never for a joined investigation, an older
    Cardinal, or the capability absent or off.
  - Fail open: never blocks or delays session start, never prints an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402
from cardinal_core import storyboard_authoring  # noqa: E402
from cardinal_core.investigation_text import ADVISORY_LINE, SESSION_ID_LINE  # noqa: E402

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PATHS = storyboard_authoring.agent_paths("claude")


def session_id(payload: dict) -> str | None:
    for value in (
        payload.get("session_id"),
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ):
        if isinstance(value, str) and SESSION_ID_RE.match(value):
            return value
    return None


def key_warning(sid: str | None, source) -> str | None:
    """The "tell the user" note when CARDINAL_MCP_API_KEY is set without
    CARDINAL_MCP_URL, once per session id (core: storyboard_authoring.stray_key_warning)."""
    return storyboard_authoring.stray_key_warning(PATHS, "claude", sid=sid, source=source)


def connect_hint(source, connected: bool) -> str | None:
    """One line for an unconnected machine, else None (core:
    storyboard_authoring.connect_hint; Claude's connection rule is
    hooks/_connection.is_connected, passed in)."""
    return storyboard_authoring.connect_hint(PATHS, "claude", source=source, connected=connected)


INVESTIGATION_ENV = "CARDINAL_INVESTIGATION_ID"
POLLER_ENV = "CARDINAL_INVESTIGATION_POLLER"

PAYLOAD_CWD = None   # the SessionStart payload's cwd (project settings for the kill switch)


def owner_input_recorded(b) -> bool:
    """Whether owner-input.py records this session's prompts
    (owner_input.enabled and no kill switch). Never raises."""
    try:
        import _owner_input
        from cardinal_core import owner_input
        return owner_input.enabled(b) and not _owner_input.disabled(PAYLOAD_CWD)
    except Exception:
        return False


def live_line(sid: str, b: dict) -> str:
    """The context for a session whose Investigation and live Storyboard
    exist: the shared session guidance (cardinal_core.investigation_text),
    Claude's commands (`cardinal-storyboard investigation …`, the session
    from $CLAUDE_CODE_SESSION_ID)."""
    from cardinal_core import investigation_text
    return investigation_text.session_text(sid, b, runtime="claude", owner_input_recorded=owner_input_recorded(b))


def spawn_poller(sid: str) -> None:
    """Start this session's background poller unless it runs already."""
    if (os.environ.get(POLLER_ENV) or "").strip() == "0":
        return
    from cardinal_core import investigation_poller as ip
    home = Path(os.environ.get("HOME") or str(Path.home()))
    if ip.running(home, sid):
        return
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "investigation-poller.py")
    subprocess.Popen([sys.executable, script, "--session", sid, "--anchor", str(os.getppid())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def investigation_line(sid: str | None, source=None) -> str | None:
    """Bootstrap this session's Investigation (or join $CARDINAL_INVESTIGATION_ID,
    or reuse the existing binding) and describe it; None for "say nothing
    new" (the caller then adds the plain session-id sentence)."""
    if not sid:
        return None
    from cardinal_core import investigation_bootstrap as boot
    from cardinal_core import investigation_events as ie
    import _storyboard_discovery
    home = Path(os.environ.get("HOME") or str(Path.home()))
    wanted = (os.environ.get(INVESTIGATION_ENV) or "").strip()
    wanted = wanted if ie.valid_investigation(wanted) else None
    conn = _storyboard_discovery.connection()
    if not (conn.get("origin") and conn.get("org") and conn.get("key")):
        # Connected without a usable MCP connection (telemetry only, a stray
        # key): no request; an explicit join still binds locally, as before.
        b = ie.bind(home, sid, wanted, "env")[0] if wanted else ie.read_binding(home, sid)
        if not b:
            return None
        return (SESSION_ID_LINE.format(sid=sid) + f" This session is bound to Cardinal investigation "
                f"{b['investigation_id']}. " + ADVISORY_LINE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_session_storyboard_context", Path(__file__).with_name("storyboard-context.py"))
    context_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context_module)
    context = {} if context_module.disabled() else context_module.stamp_context("find", PAYLOAD_CWD or os.getcwd(), sid)
    res = boot.ensure(home, sid, conn, _storyboard_discovery.client_header(), wanted=wanted, context=context,
                      started_at=boot.started_now() if source in (None, "startup") else None)
    from cardinal_core import session_repos
    session_repos.replay(home, sid, "claude", _storyboard_discovery.client_header())
    try:
        ie.prune(home)  # once a day: the files of sessions idle for 30 days
    except Exception:
        pass
    b = res.get("binding")
    if res["status"] in ("ok", "reused", "backoff", "failed", "busy") and (b or boot.read_pending(home, sid)):
        ie.touch_activity(home, sid)
        try:
            spawn_poller(sid)
        except Exception:
            pass
    if b and isinstance(b.get("bootstrap"), dict) and b["bootstrap"].get("status") == "ok":
        return live_line(sid, b)
    line = SESSION_ID_LINE.format(sid=sid)
    if b:
        line += f" This session is bound to Cardinal investigation {b['investigation_id']}. " + ADVISORY_LINE
    if res["status"] in ("failed", "backoff"):
        why = res.get("reason") or "Cardinal could not be reached"
        line += (f" Cardinal could not set up this session's live Storyboard yet ({why}); it retries in the "
                 "background, and `cardinal-storyboard investigation link` retries now if the user asks for it.")
    return line


def owner_input_disclosure(sid: str | None) -> str | None:
    """The user-visible disclosure (a systemMessage) when this session's
    prompts are recorded, else None. Nothing is recorded here: the binding
    says it was disclosed only once the message has been written
    (mark_owner_input_disclosed, after the output is flushed), so a prompt
    submitted while this hook still runs is never captured unannounced.
    The kill switch makes the binding forget an earlier disclosure."""
    if not sid:
        return None
    try:
        import _owner_input
        from cardinal_core import investigation_events as ie
        from cardinal_core import owner_input
        home = Path(os.environ.get("HOME") or str(Path.home()))
        if not ie.valid_session(sid):
            return None
        if _owner_input.disabled(PAYLOAD_CWD):
            owner_input.forget_disclosure(home, sid)
            return None
        b = ie.read_binding(home, sid)
        if not owner_input_recorded(b):
            return None
        return _owner_input.disclosure_message(owner_input.DISCLOSURE.format(inv=b["investigation_id"]))
    except Exception:
        return None


def mark_owner_input_disclosed(sid: str) -> None:
    """The disclosure reached Claude Code: from now on prompts are captured."""
    try:
        from cardinal_core import owner_input
        owner_input.mark_disclosed(Path(os.environ.get("HOME") or str(Path.home())), sid)
    except Exception:
        pass


def main() -> None:
    global PAYLOAD_CWD
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    PAYLOAD_CWD = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    sid = session_id(payload)
    try:
        connected = _connection.is_connected()
    except Exception:
        connected = False
    parts = []
    # Unconnected, the storyboard__* tools do not exist (the cardinal server
    # has no URL), so the id would only point Claude at a missing tool.
    if sid and connected:
        try:
            line = investigation_line(sid, payload.get("source"))
        except Exception:
            line = None
        parts.append(line or SESSION_ID_LINE.format(sid=sid))
    try:
        warning = key_warning(sid, payload.get("source"))
    except Exception:
        warning = None
    if warning:
        parts.append(warning)
    try:
        hint = None if connected else connect_hint(payload.get("source"), connected)
    except Exception:
        hint = None
    if hint:
        parts.append(hint)
    if not parts:
        return
    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": " ".join(parts),
        }
    }
    told = owner_input_disclosure(sid) if connected else None
    if told:
        out["systemMessage"] = told
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()   # a failed write raises here: the disclosure is then not recorded
    if told:
        # Last step, after the message is out: only now may prompts be captured.
        mark_owner_input_disclosed(sid)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
