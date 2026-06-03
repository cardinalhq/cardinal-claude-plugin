---
description: Verify Cardinal telemetry is wired correctly for this Claude Code install.
disable-model-invocation: true
---

# /cardinal:status

Reports whether Claude Code is set up to send telemetry to Cardinal, what the
current connection metadata looks like, and whether the ingest endpoint is
reachable.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-status
```

The script reads `~/.claude/cardinal.json` (the non-secret state file written
by `/cardinal:connect`) and reports:

- Whether `~/.claude/settings.json` has the expected `OTEL_*` keys.
- The configured host, org, user, ingest endpoint, and key age.
- A small reachability probe against the ingest endpoint.
- Whether `OTEL_LOG_TOOL_DETAILS` is on (and, if not, a note about the
  repo/service attribution limitation).

If `~/.claude/cardinal.json` doesn't exist or the env block is missing, surface
"not connected" and suggest running `/cardinal:connect`.
