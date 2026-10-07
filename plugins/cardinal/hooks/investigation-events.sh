#!/bin/sh
# cardinal investigation events — PostToolUse / PostToolUseFailure (every
# tool) and Stop hook.
#
# The fast path. Every tool call of every session runs this. Every connected
# session is bound to its Investigation (SessionStart bootstraps it), so a
# tool boundary must not pay for the network either: the session's
# background poller (investigation-poller.py) reads the investigation's
# events and leaves the deliverable ones in <session_id>.inbox.json. Here:
#
#   unbound (no ~/.cardinal/investigations/sessions/<session_id>.json and no
#   pending <session_id>.bootstrap.json): no Python, no network, no output.
#
#   bound or pending: touch <session_id>.active (the poller polls only while
#   the session is active), start the poller in the background if its pid
#   (<session_id>.poller.pid) is not alive (CARDINAL_INVESTIGATION_POLLER=0:
#   never), then
#     - an inbox, or a Stop (the backstop): hand the payload to
#       investigation-events.py, which delivers;
#     - otherwise: nothing more. No Python, no network, no output.
#
# The session id is found without a JSON parser, in $CLAUDE_CODE_SESSION_ID
# and in the first read of the payload (Claude Code puts session_id first):
# every "session_id":"<id>" there is a candidate (a tool's input may name
# another session), and the first one with a binding (or a pending
# bootstrap) file counts; Python, which parses the whole payload properly,
# re-checks it. The rest of a large payload is never scanned, only drained.
# Fail open: always exits 0.

dir="${HOME:-}/.cardinal/investigations/sessions"
if [ -z "${HOME:-}" ] || [ ! -d "$dir" ]; then
  cat >/dev/null 2>&1
  exit 0
fi
# One read(2) of at most 64 KiB: dd consumes exactly what it returns, so the
# rest of stdin is still there for Python.
first=$(dd bs=65536 count=1 2>/dev/null)
found=
for sid in ${CLAUDE_CODE_SESSION_ID:-} $(printf '%s' "$first" \
    | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]\{1,128\}"' 2>/dev/null \
    | head -n 8 \
    | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/'); do
  case "$sid" in
    *[!A-Za-z0-9_-]*) continue ;;
  esac
  if [ -f "$dir/$sid.json" ]; then
    found=bound
    break
  fi
  if [ -f "$dir/$sid.bootstrap.json" ]; then
    found=pending
    break
  fi
done
if [ -z "$found" ]; then
  cat >/dev/null 2>&1
  exit 0
fi
: >"$dir/$sid.active" 2>/dev/null
if [ "${CARDINAL_INVESTIGATION_POLLER:-}" != 0 ]; then
  pid=
  if [ -f "$dir/$sid.poller.pid" ]; then
    read -r pid <"$dir/$sid.poller.pid" 2>/dev/null
  fi
  case "$pid" in
    '' | *[!0-9]*) pid= ;;
  esac
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    "${0%/*}/investigation-poller.py" --session "$sid" --anchor "$PPID" </dev/null >/dev/null 2>&1 &
  fi
fi
deliver=
if [ "$found" = bound ]; then
  if [ -f "$dir/$sid.inbox.json" ]; then
    deliver=1
  else
    case "$first" in
      *'"hook_event_name":"Stop"'* | *'"hook_event_name": "Stop"'*) deliver=1 ;;
    esac
  fi
fi
if [ -z "$deliver" ]; then
  cat >/dev/null 2>&1
  exit 0
fi
{ printf '%s' "$first"; cat; } | "${0%/*}/investigation-events.py"
exit 0
