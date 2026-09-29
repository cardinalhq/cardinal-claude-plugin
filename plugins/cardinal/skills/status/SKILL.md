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
- `mcp-only` (rare) — MCP env vars only.

For each enabled side it shows the configured endpoint, key prefix, and
key age, and probes the endpoint for reachability.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-status
```

The script reads `~/.claude/cardinal.json` (the non-secret state file)
and reports:

- Mode, user email, org, host, plugin version, connection age.
- **Telemetry side** (when enabled): the ingest endpoint, key prefix,
  whether `OTEL_LOG_TOOL_DETAILS` is on, that the OTel env keys are
  present in `~/.claude/settings.json`, and a reachability probe.
- **MCP side** (when enabled): the MCP URL, key prefix, that
  `CARDINAL_MCP_URL` and `CARDINAL_MCP_API_KEY` are present in
  `settings.json` env (these are what the plugin's `.mcp.json`
  substitutes at MCP server connect time), and a reachability probe.

If `~/.claude/cardinal.json` doesn't exist, surfaces "not connected",
says the plugin is local-only (the `cardinal` server is off; `/mcp`
lists it as missing `CARDINAL_MCP_URL`) and how to get write access:
sign up at `https://app.cardinalhq.io`, then run `/cardinal:connect`
(approving it in the browser stores an API key for this machine). In either state it warns when
`CARDINAL_MCP_API_KEY` is set but `CARDINAL_MCP_URL` is not — the
`cardinal` server then has no URL, so Cardinal's tools are missing —
and names the fixes (`/cardinal:connect [--host <url>]`, or unset the key). If state says connected but the
matching env vars are absent or a probe returns 401/403, surfaces a
clear repair hint (`/cardinal:connect --rotate`).
