---
description: Remove Cardinal telemetry configuration from Claude Code's settings.json.
disable-model-invocation: true
---

# /cardinal:disconnect

Removes the OpenTelemetry env keys this plugin wrote, removes
`~/.claude/cardinal.json`, and reminds the user to restart Claude Code.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-disconnect
```

The script:

1. Reads `~/.claude/cardinal.json` to know which keys were plugin-written.
2. Backs up `~/.claude/settings.json` to `~/.claude/settings.json.bak.<timestamp>`.
3. Removes ONLY the OTel keys this plugin wrote. Preserves any other env
   keys (theme, enabledPlugins, etc.) untouched.
4. Deletes `~/.claude/cardinal.json`.
5. Reminds the user to restart Claude Code and to revoke the ingest key in
   the Cardinal admin UI when convenient — this v0.1 does not call a remote
   revoke endpoint (planned for v0.2).

If `~/.claude/cardinal.json` doesn't exist, surface "not connected" and exit
without modifying files.

After success, tell the user:

1. Local config is cleaned.
2. The ingest key is still active server-side — revoke it via
   `https://app.cardinalhq.io/settings/api-keys` to fully disconnect.
3. Restart Claude Code so it stops sending telemetry to Cardinal.
