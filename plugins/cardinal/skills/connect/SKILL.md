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

**You MUST run `cardinal-connect` in the background.** The script
blocks for up to 10 minutes waiting for the user to approve in their
browser; the Bash tool's stdout is buffered until the call returns, so
if you don't background it the user never sees the verification URL.

Invoke via the Bash tool with `run_in_background: true`:

```
cardinal-connect
```

Then surface the URL via the pending side-channel file:

1. After kicking off the background bash call, poll
   `~/.claude/cardinal-pending.json` — the script writes it within
   1–2 seconds of starting. Read up to 5 times with 1-second gaps.
2. Parse the JSON. Shape:
   ```json
   {
     "verification_uri": "https://app.cardinalhq.io/connect?code=ABCD-EFGH",
     "user_code": "ABCD-EFGH",
     "expires_in": 600,
     "written_at": "2026-06-05T05:40:59Z",
     "plugin_version": "0.3.1"
   }
   ```
3. **Show `verification_uri` to the user prominently** — wrap it in
   a code fence (a real markdown link is fine too) and say something
   like "Open this in your browser, log in if needed, pick your org,
   and click Approve. I'm watching for it." Do NOT block on it
   yourself; the background bash call is doing that.
4. Wait for the background bash call to complete. Claude Code will
   notify you when it finishes; until then you can answer side
   questions, but don't run another long-blocking command in the
   same conversation.
5. When the background call returns, read its final stdout for the
   success summary or the error. Surface it to the user verbatim.

The pending file is deleted automatically when `cardinal-connect`
exits — success, denied, expired, or error.

### What the script actually does

1. POST to `/api/auth/device/code` to start the flow.
2. Writes the verification URL to `~/.claude/cardinal-pending.json`
   (this is what step 1 above reads).
3. Polls `/api/auth/device/token` until approval lands (or the user
   denies / the 10-minute TTL expires).
4. Writes two files on success:
   - **`~/.claude/settings.json`** — OTel env keys + the two
     `CARDINAL_MCP_*` env vars (atomic; preserves any unrelated env
     keys).
   - **`~/.claude/cardinal.json`** — non-secret state + key ids for
     `/cardinal:status` and `/cardinal:disconnect`.
5. If `~/.claude.json` already had a v0.2-era `mcpServers.cardinal`
   entry, or legacy per-driver `cardinal-*` entries, **prunes them**
   (with a backup) so the plugin-declared `cardinal` server doesn't
   collide with stale user-config copies.
6. Probes both endpoints to confirm the keys actually authenticate.
7. Deletes `~/.claude/cardinal-pending.json` on exit.

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
