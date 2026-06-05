---
description: Verify the Cardinal plugin's wiring on this Claude Code install (telemetry and/or MCP).
disable-model-invocation: true
---

# /cardinal:status

Reports both sides of the plugin's wiring depending on the recorded
mode:

- `telemetry-and-mcp` (default after `/cardinal:connect`) — both sides.
- `telemetry-only` (after `/cardinal:connect --telemetry-only`) —
  OTel env block only.
- `mcp-only` (rare) — MCP entry only.

For each enabled side it shows the configured endpoint, key prefix, and
key age, and probes the endpoint for reachability.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-status
```

The script reads `~/.claude/cardinal.json` (the non-secret state file
written by `/cardinal:connect`) and reports:

- Mode, user email, org, host, plugin version, connection age.
- **Telemetry side** (when enabled): the ingest endpoint, the ingest
  key prefix, whether `OTEL_LOG_TOOL_DETAILS` is on, that the OTel env
  keys are present in `~/.claude/settings.json`, and a reachability
  probe.
- **MCP side** (when enabled): the MCP URL, the MCP key prefix, that
  `mcpServers.cardinal` is present in `~/.claude.json`, and a
  reachability probe with the key.

If `~/.claude/cardinal.json` doesn't exist, surfaces "not connected"
and suggests `/cardinal:connect`.

If state says connected but a corresponding config file is missing or
the probe returns 401/403, surfaces a clear repair hint
(`/cardinal:connect --rotate`).
