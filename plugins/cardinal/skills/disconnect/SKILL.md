---
description: Disconnect this Claude Code install from Cardinal — revoke the MCP key, strip the plugin's env block, delete local state.
disable-model-invocation: true
---

# /cardinal:disconnect

Reverses what `/cardinal:connect` did:

1. Best-effort POST to `/api/maestro-keys/<mcp_key_id>/revoke`. The
   plugin reads the plaintext from `~/.claude/settings.json` env
   (`CARDINAL_MCP_API_KEY`) and authenticates as the key itself (R11 §1
   "self" path).
2. Strips the plugin-owned env keys from `~/.claude/settings.json` —
   both OTel side and the two `CARDINAL_MCP_*` vars. Unrelated env keys
   stay (with a backup before mutating).
3. Deletes `~/.claude/cardinal.json`.

The plugin does **not** touch `~/.claude.json` here — v0.3 doesn't
write to it on connect either, so there's nothing to undo. (Users
upgrading from v0.2 should re-run `/cardinal:connect` once; the
connect-time legacy cleanup prunes the v0.2 stanza.)

The ingest key is not revoked server-side — the maestro endpoint
hasn't shipped yet. The script prints a pointer to the admin UI.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-disconnect
```

### Flags

- `--force` — proceed even if `~/.claude/cardinal.json` is missing.
- `--keep-telemetry` — only remove the MCP side. Keeps the OTel env
  keys in place. Useful for going from `telemetry-and-mcp` back to
  `telemetry-only` without re-running connect.

## After success

Tell the user:

1. The MCP key has been revoked server-side (if the revoke call
   succeeded — the script reports either way).
2. The ingest key is still active server-side; revoke it via
   `https://<host>/settings/api-keys` for a clean disconnect.
3. Restart Claude Code so it picks up the env-block change. Without
   the `CARDINAL_MCP_*` env vars the plugin's `.mcp.json` resolves to
   an empty URL and the `cardinal` server won't connect — effectively
   off on the next launch.
