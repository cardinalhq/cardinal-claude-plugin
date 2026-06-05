---
description: Disconnect this Claude Code install from Cardinal — revoke the MCP key, remove the MCP entry and OTel env block, delete local state.
disable-model-invocation: true
---

# /cardinal:disconnect

Reverses what `/cardinal:connect` did:

1. Best-effort POST to `/api/maestro-keys/<mcp_key_id>/revoke` so the
   MCP key stops authenticating immediately (R11 §1 "self" path — the
   plugin holds the plaintext so it can revoke its own key without an
   admin session).
2. Removes `mcpServers.cardinal` from `~/.claude.json` (with a backup).
3. Removes the OTel env block this plugin wrote from
   `~/.claude/settings.json` (with a backup; unrelated env keys are
   preserved).
4. Deletes `~/.claude/cardinal.json` (the local state file).

The ingest API key is **not** revoked server-side — the matching
maestro endpoint hasn't shipped yet (tracked alongside the device-code
work). The script prints a pointer to the admin UI so the user can
revoke it there.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-disconnect
```

### Flags

- `--force` — proceed even if `~/.claude/cardinal.json` is missing.
- `--keep-telemetry` — only remove the MCP side. Keeps the OTel env
  block and the telemetry section of the state file in place. Useful
  for going from `telemetry-and-mcp` back to `telemetry-only` without
  re-running `/cardinal:connect`.

## After success

Tell the user:

1. The MCP key has been revoked server-side (if the revoke call
   succeeded — the script reports either way).
2. The ingest key is still active server-side; revoke it via
   `https://<host>/settings/api-keys` for a clean disconnect.
3. Restart Claude Code so it picks up the removed `mcpServers` entry
   and the missing OTel env block.

If the state file was missing and `--force` wasn't passed, surface
"not connected" and exit without modifying files.
