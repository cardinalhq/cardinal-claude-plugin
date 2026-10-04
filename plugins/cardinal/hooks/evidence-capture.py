#!/usr/bin/env python3
"""cardinal evidence capture — PostToolUse and PostToolUseFailure hook on
EVERY tool (matcher ".*").

Records the result of any tool call (Bash, Read, Edit/Write, Grep, Glob,
WebFetch, WebSearch, Agent, TodoWrite, any MCP server's tool, and any tool
Claude Code adds later) in the local evidence spool, so a storyboard can later
cite it as *captured* evidence. The capture decision is the shared, tool-
neutral pipeline cardinal_core.evidence_capture.capture_call; this file only
turns Claude Code's hook payload into a ToolCall. Cardinal's own gateway tools
(mcp__cardinal__*, mcp__plugin_cardinal_cardinal__*) are skipped: the gateway
already mints a *witnessed* receipt for them.

Contract:
  - Input on stdin (read to at most 32 MiB): Claude Code's PostToolUse
    payload {tool_name, tool_input, tool_response, tool_use_id, session_id,
    cwd, ...}, or its PostToolUseFailure payload {..., error, is_interrupt}:
    the error text is captured as the result and the entry marked
    status "error"; an interrupt is skipped. A larger payload is recorded as
    a withheld stub (reason "unreadable").
  - A call whose input names something sensitive (a .env or key file,
    ~/.aws, a secret-dumping command, a credentialed URL or header; see
    cardinal_core.evidence_gate) is recorded as a withheld stub: tool,
    status and the rule that fired, no arguments, no result.
  - Writes ~/.cardinal/evidence/<session_id>/ev_<12 hex>.json: directories
    0700, file 0600, written atomically. Arguments and result are scrubbed
    (the gateway's receipt scrub plus plain-text key=value rules, base64
    blobs and local paths) and capped (64 KiB / 256 KiB). The spool is kept
    under 14 days, 256 MiB and 10,000 entries per session.
  - Output: hookSpecificOutput.additionalContext, one line: the first
    capture of a session explains how to cite; later ones are just
    "[evidence:ev_…]" (or "[evidence:ev_… withheld: <reason>]").
    CARDINAL_EVIDENCE_CONTEXT=0 keeps capturing but prints nothing.
  - Opt-out: CARDINAL_EVIDENCE_CAPTURE=0 or the flag file
    ~/.cardinal/evidence/disabled.
  - Edited files: after a successful Edit, Write, MultiEdit or NotebookEdit
    (PostToolUse), the file's repo-relative path is also recorded for the
    session (cardinal_core.storyboard_files, ~/.claude/cardinal/
    storyboard-files/<session>.json, 0600): storyboard__create / add_act /
    publish stamp them as the act's written_from `context.paths`. Recorded
    even with evidence capture off; no extra hook process.
  - No network. Fail open: always exits 0, never blocks the tool, never
    prints an error; gives up after 1.4 s (hooks.json gives it 2 s). A
    call the pipeline could not finish in time (or a payload nested too
    deep to parse) is still recorded, as a withheld stub (reason
    "unreadable"), so the agent gets an id and a reason, never silence.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RUNTIME = "claude-code"
# Cardinal's own MCP servers, as Claude Code names them (a user-scope
# `cardinal` server, or the plugin's bundled one). The gateway mints
# witnessed receipts for these.
CARDINAL_SERVERS = ("cardinal", "plugin_cardinal_cardinal")


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def client() -> str:
    from cardinal_core import evidence
    try:
        from _plugin_version import plugin_version
        version = plugin_version()
    except Exception:
        version = None
    return evidence.client_string(RUNTIME, version)


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# The edited-file record must finish before this (the hook's timeout is 2 s).
RECORD_BY_S = 1.8


def record_edited_file(payload, home: Path) -> None:
    """A successful file edit's repo-relative path, for the session's
    storyboard context (storyboard_files.record). Never raises."""
    try:
        if not isinstance(payload, dict) or (payload.get("hook_event_name") or "PostToolUse") != "PostToolUse":
            return
        if payload.get("tool_name") not in EDIT_TOOLS:
            return
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        cwd = payload.get("cwd")
        from cardinal_core import storyboard_files
        from cardinal_core.paths import AgentPaths
        storyboard_files.record(AgentPaths(home=home / ".claude").runtime_dir / "storyboard-files",
                                payload.get("session_id"), file_path, cwd if isinstance(cwd, str) else None)
    except Exception:
        pass


def emit(event: str, line) -> None:
    if not line:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": line,
        }
    }))
    sys.stdout.flush()


def main() -> None:
    from cardinal_core import evidence
    from cardinal_core import evidence_capture as cap

    home = home_dir()
    root = evidence.default_root(home)
    if evidence.capture_disabled(root):
        # Capture is off; the edited-files record for storyboards is not.
        raw, complete = cap.read_stdin_bounded()
        if complete and raw.strip():
            try:
                record_edited_file(json.loads(raw.decode("utf-8", errors="replace"), strict=False), home)
            except (ValueError, RecursionError):
                pass
        return
    spill_root = str(home / ".claude" / "projects")
    stub = None
    with cap.time_guard(cap.TIME_BUDGET_S):
        raw, complete = cap.read_stdin_bounded()
        if not complete:
            stub = ("size", f"> {cap.MAX_STDIN_BYTES >> 20} MiB")
        else:
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"), strict=False) if raw.strip() else {}
            except RecursionError:
                # Valid JSON nested deeper than the parser goes (it happens
                # in MCP results): a stub from the payload's head.
                stub = ("depth", "nested too deep to read")
            except ValueError:
                return
    if stub is not None:
        entry = cap.unreadable_record(RUNTIME, client(), raw, spill_root, rule=stub[0], hint=stub[1])
        if entry is not None:
            evidence.write_entry(root, entry)
            if cap.context_enabled():
                emit(cap.head_field(raw, "hook_event_name") or "PostToolUse", cap.context_line(entry, False))
        return
    if not isinstance(payload, dict):
        return
    try:
        capture(payload, spill_root, home)
    finally:
        # After the capture, so its git probes never eat the capture's
        # budget, and bounded by what is left of the hook's timeout.
        with cap.time_guard(max(0.05, RECORD_BY_S - (time.monotonic() - START))):
            record_edited_file(payload, home)


def capture(payload: dict, spill_root: str, home: Path) -> None:
    from cardinal_core import evidence_capture as cap

    event = payload.get("hook_event_name") or "PostToolUse"
    error = None
    response = None
    if event == "PostToolUseFailure":
        # A failed call reaches hooks only here, as the error text. A user
        # interrupt is not the tool's answer.
        error = payload.get("error")
        if payload.get("is_interrupt") is True or not isinstance(error, str) or not error.strip():
            return
    elif event == "PostToolUse":
        response = payload.get("tool_response")
    else:
        return
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return
    source, tool = cap.classify_mcp_name(tool_name, RUNTIME, CARDINAL_SERVERS)
    cwd = payload.get("cwd")
    call = cap.ToolCall(
        runtime=RUNTIME,
        tool_name=tool_name,
        source=source,
        tool=tool,
        tool_input=payload.get("tool_input"),
        response=response,
        error=error,
        session_id=payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
        tool_use_id=payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        spill_root=spill_root,
        client=client(),
    )
    budget = max(0.2, cap.TIME_BUDGET_S - (time.monotonic() - START))
    got = cap.capture_call_guarded(call, home, budget_s=budget, promote_cmd="cardinal-evidence")
    if got is not None:
        emit(event, got.line)


if __name__ == "__main__":
    try:
        # Reading and parsing the payload runs under the hook's budget;
        # capture_call_guarded then spends what is left of it, and keeps a
        # withheld stub when the pipeline cannot finish.
        main()
    except BaseException:
        pass
    sys.exit(0)
