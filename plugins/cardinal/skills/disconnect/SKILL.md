---
description: Disconnect this Claude Code install from Cardinal — revoke the ingest, MCP and control-plane keys, strip the plugin's env block, delete local state and caches.
disable-model-invocation: true
---

# /cardinal:disconnect

Reverses what `/cardinal:connect` did. Every credential is read into memory
first, the local teardown runs next, and the server-side revokes run last,
so a failed revoke never leaves a half-disconnected machine:

1. Writes `~/.claude/cardinal-disconnected`. A running Claude Code keeps the
   old `CARDINAL_MCP_*` values in its process environment until it restarts;
   while this marker exists the hooks ignore that stale environment.
   `/cardinal:connect` removes it.
2. Strips the plugin-owned env keys (OTel side and `CARDINAL_MCP_*`) from
   `~/.claude/settings.json`, atomically. Unrelated env keys stay. No backup
   copy is made: it would hold the keys being removed. The Cardinal keys are
   also scrubbed from `settings.json.bak.<timestamp>` files older versions
   left behind.
3. Removes the control-plane token from `~/.claude/cardinal-secrets.json`,
   and deletes `~/.claude/cardinal.json`.
4. Deletes the per-identity caches: `~/.claude/cardinal/` (spend-limit
   verdicts, decision ledgers, plan stamp, the org's storyboard-discovery
   blocks, preview renders) and the evidence tokens/receipt ledgers under
   `~/.cardinal/evidence/`. Captured evidence itself stays on this machine.
5. Revokes the **MCP key** and the **control-plane token** server-side
   (`POST /api/maestro-keys/<id>/revoke`, each authenticating as itself).
   It can **not** revoke the **ingest key**: maestro stores ingest keys
   separately, and only a signed-in org owner can revoke one. The script
   names the key and where to revoke it.

The plugin does **not** touch `~/.claude.json` here — v0.3 doesn't write to
it on connect either, so there's nothing to undo.

## How you (Claude) should run this

Invoke via the Bash tool:

```
cardinal-disconnect
```

### Flags

- `--force` — proceed even if `~/.claude/cardinal.json` is missing (cleans
  up leftover env keys, caches and old backups; keys whose id is unknown
  can't be revoked and are reported).
- `--keep-telemetry` — only remove the MCP side (MCP env keys, MCP key, the
  storyboard-discovery cache). Keeps the OTel env keys in place. Useful for
  going from `telemetry-and-mcp` back to `telemetry-only` without
  re-running connect. Refused (exit 2) on an MCP-only connection, which has
  no telemetry to keep.

Exit status: 0 when the local teardown completed; 1 when something could not
be removed locally (the output names it).

## After it runs

Tell the user:

1. Which keys were revoked server-side, as the script reports them, with the
   `<host>/settings/api-keys` link for any it could not revoke. The ingest
   key always stays active: an org owner revokes it at
   `<host>/integrations` → Lakerunner → Ingest API keys (the script prints
   its prefix). Until then, sessions started before the disconnect keep
   sending telemetry under the old identity.
2. Anything the script marked `✗` (exit 1) still needs doing by hand.
3. Quit **every** Claude Code session on this machine — other terminals, IDE
   extensions, the desktop app — not just this one. Each keeps the settings
   it started with until it restarts. After restart the plugin
   falls back to local-only: the `cardinal` MCP server has no URL and never connects
   (`/mcp` lists it as missing `CARDINAL_MCP_URL`), the telemetry,
   spend-limit and usage hooks go silent (full disconnect), and evidence
   capture stays on this machine. `/cardinal:connect` turns it back on.
4. Connecting as someone else next: the browser approval uses whichever
   Cardinal account is signed in at the Cardinal app, so sign out there
   first, and check the "Connected as" line `/cardinal:connect` prints.
