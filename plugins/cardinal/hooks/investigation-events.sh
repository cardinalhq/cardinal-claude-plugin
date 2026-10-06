#!/bin/sh
# cardinal investigation events — PostToolUse / PostToolUseFailure (every
# tool) and Stop hook.
#
# The fast path. Every tool call of every session runs this, so an UNBOUND
# session (no ~/.cardinal/investigations/sessions/<session_id>.json) must
# cost nothing: no Python, no network, no output, exit 0. Only a session
# bound to an investigation (CARDINAL_INVESTIGATION_ID at launch, or
# `cardinal-storyboard investigation bind|create`) hands the payload to
# investigation-events.py, which reads the investigation's new advisory
# events and delivers them (see that file).
#
# The session id is found without a JSON parser, in $CLAUDE_CODE_SESSION_ID
# and in the first read of the payload (Claude Code puts session_id first):
# every "session_id":"<id>" there is a candidate (a tool's input may name
# another session), and Python, which parses the whole payload properly,
# starts only when one of them has a binding file. The rest of a large
# payload is never scanned, only drained. Fail open: always exits 0.

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
    found=1
    break
  fi
done
if [ -z "$found" ]; then
  cat >/dev/null 2>&1
  exit 0
fi
{ printf '%s' "$first"; cat; } | "$(dirname "$0")/investigation-events.py"
exit 0
