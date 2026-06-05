---
description: Connect Claude Code to Cardinal — runs the device-code flow to enable telemetry to the Outcomes Dashboard AND the unified Cardinal MCP tools, in one consent.
disable-model-invocation: true
---

# /cardinal:connect

Wires Claude Code up to a Cardinal workspace. **Enables both sides at
once by default**:

- **Telemetry** — Claude Code's OpenTelemetry stream goes to Cardinal's
  Outcomes Dashboard. Configured via the `env` block in
  `~/.claude/settings.json`.
- **MCP** — the unified `cardinal` MCP server appears in this Claude
  Code session, exposing whichever tools the org has integrations
  configured for. The plugin **declares the MCP server natively** via
  `plugins/cardinal/.mcp.json` with `${CARDINAL_MCP_URL}` and
  `${CARDINAL_MCP_API_KEY}` substitution; the connect script just sets
  those env vars in the same `settings.json` env block. **No
  `~/.claude.json` write.**

Both are minted in one browser-approved consent via the maestro
device-code flow (`cardinal-mcp-aggregator.md` R5b). The MCP URL is a
single durable endpoint per org (`https://<host>/api/orgs/<uuid>/mcp`)
whose aggregator fans out to whatever integrations are configured —
adding / removing integrations on the Cardinal side never requires
re-running this command.

## How you (Claude) should run this

The plugin ships a `cardinal-connect` executable on the Bash tool's
PATH. With no flags, run:

```
cardinal-connect
```

The script will:

1. POST to `/api/auth/device/code` to start the flow.
2. Print a `verification_uri` like
   `https://app.cardinalhq.io/connect?code=ABCD-EFGH`.
3. Tell the user to open it in their browser. They'll log in (if not
   already), pick the org to connect, and click Approve.
4. Poll `/api/auth/device/token` until approval lands (or the user
   denies / the 10-minute TTL expires).
5. Write two files:
   - **`~/.claude/settings.json`** — OTel env keys + the two
     `CARDINAL_MCP_*` env vars (atomic; preserves any unrelated env
     keys).
   - **`~/.claude/cardinal.json`** — non-secret state + key ids for
     `/cardinal:status` and `/cardinal:disconnect`.
6. If `~/.claude.json` already had a v0.2-era `mcpServers.cardinal`
   entry, or legacy per-driver `cardinal-*` entries, **prune them**
   (with a backup) so the plugin-declared `cardinal` server doesn't
   collide with stale user-config copies.
7. Probe both endpoints to confirm the keys actually authenticate.

When you (Claude) run this:

- Surface the verification URL **prominently** — wrap it in a code
  fence and make it copy-pastable. The Bash tool can't open a browser.
- The script blocks for up to 10 minutes waiting for approval.
- On success, pass the summary through verbatim.

## Flags

- `--telemetry-only` — request only the ingest scope. The two
  `CARDINAL_MCP_*` env vars are NOT written; the plugin's `.mcp.json`
  is still loaded by Claude Code but with the env vars unset the
  `cardinal` server entry resolves to empty and silently doesn't
  connect.
- `--rotate` — proceed even when state shows we're already connected.
  Mints fresh keys; the previous ones stay alive until their TTL or
  until `/cardinal:disconnect` revokes them.
- `--host <url>` — Cardinal host (default `https://app.cardinalhq.io`).
- `--no-tool-details` — opt out of OTel tool-details capture.
- `--skip-legacy-cleanup` — don't prune the v0.2 `mcpServers.cardinal`
  entry or `cardinal-*` entries from `~/.claude.json`. Default behavior
  is to prune them.
- `--deployment-env <name>` — override the derived
  `deployment.environment` label.
- `--dry-run` — run the device-code flow, print what would be written.

## How the MCP side actually wires up (for the curious)

The plugin's `plugins/cardinal/.mcp.json`:

```json
{
  "cardinal": {
    "type": "http",
    "url": "${CARDINAL_MCP_URL}",
    "headers": { "X-CardinalHQ-API-Key": "${CARDINAL_MCP_API_KEY}" }
  }
}
```

Claude Code reads `~/.claude/settings.json` `env` at process start and
substitutes `${VAR}` references in plugin-declared `.mcp.json` files at
MCP server connect time. So setting `CARDINAL_MCP_URL` and
`CARDINAL_MCP_API_KEY` in the env block is all that's needed to bring
the server online — no `~/.claude.json` ownership required.

## A note about `--no-tool-details`

Tool-details capture is **on by default** because without it the
Outcomes Dashboard can't derive `repo` or `service` from per-step
events — every session shows as `repo=unknown` and `service=unknown`.
Bash command lines and file paths may contain PII some orgs' privacy
policies forbid. If the user's org has such a policy, suggest
`--no-tool-details`.

## After success

Tell the user:

1. `~/.claude/settings.json` env has been updated; any v0.2-era
   `~/.claude.json` cardinal entries were pruned.
2. **Fully quit Claude Code** (Cmd-Q on macOS) and start a new
   session. `settings.json` env is read at process start, and Claude
   Code substitutes the env vars into the plugin's `.mcp.json` when it
   loads the MCP servers.
3. Run `/cardinal:status` from the new session to verify both sides.

## Errors

Surface the script's stderr verbatim and don't claim success. Common
cases:

- `Cardinal is already connected as ...` — exit 2 from the
  already-connected guard. Re-run with `--rotate` to overwrite.
- `Consent request expired before approval` — the 10-minute TTL
  elapsed; re-run.
- `Request was denied in the browser` — the user clicked Deny.
- `settings.json is not valid JSON` — the script refuses to write into
  an unparseable file. Tell the user to fix or back up the file.
- `ingest reachability failed` / `MCP reachability failed` — the
  newly-minted keys don't authenticate at the endpoint. Usually means a
  maestro misconfig (org has no active lakerunner integration for the
  ingest side, gateway not running for the MCP side).
