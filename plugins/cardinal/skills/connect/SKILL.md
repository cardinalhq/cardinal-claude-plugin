---
description: Connect Claude Code to Cardinal — runs the device-code flow to enable telemetry to the Outcomes Dashboard AND the unified Cardinal MCP tools, in one consent.
disable-model-invocation: true
---

# /cardinal:connect

This skill wires Claude Code up to a Cardinal workspace. **By default it
enables both sides at once**:

- **Telemetry** — Claude Code's OpenTelemetry stream goes to Cardinal's
  Outcomes Dashboard. Configured via the `env` block in
  `~/.claude/settings.json`.
- **MCP** — the unified `cardinal` MCP server appears in this Claude
  Code session, exposing whatever observability/integration tools the
  org has configured server-side. Configured via the `mcpServers`
  block in `~/.claude.json`.

Both are minted in a single browser-approved consent via the maestro
device-code flow (`cardinal-mcp-aggregator.md` R5b). The MCP URL is a
single durable endpoint per org (`https://<host>/api/orgs/<uuid>/mcp`)
that fans out to whatever integrations are configured — so adding /
removing integrations on the Cardinal side never requires re-running
this command.

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
5. Write three files atomically:
   - OTel env keys into `~/.claude/settings.json` (preserving any
     unrelated keys you may already have there).
   - `mcpServers.cardinal` into `~/.claude.json`.
   - State + key ids into `~/.claude/cardinal.json` for
     `/cardinal:status` and `/cardinal:disconnect`.
6. Probe both endpoints to confirm the keys actually authenticate.

When you (Claude) run this:

- Surface the verification URL **prominently** — wrap it in a code
  fence and make it copy-pastable. The user must open it manually; the
  Bash tool can't open a browser.
- The script blocks for up to 10 minutes waiting for approval. That's
  expected.
- On success the script prints a clear summary. Pass that through to
  the user.

## Flags

- `--telemetry-only` — skip the MCP write. Use this when the user
  specifically only wants their Claude Code telemetry to flow to
  Cardinal (e.g. their org policy doesn't want extra tools in the
  Claude Code tool palette).
- `--rotate` — proceed even when state shows we're already connected.
  Mints fresh keys; the previous ones stay alive until their TTL or
  until `/cardinal:disconnect` revokes them.
- `--host <url>` — Cardinal host (default `https://app.cardinalhq.io`).
  Override for dogfood, customer in-VPC installs, etc.
- `--no-tool-details` — opt out of OTel tool-details capture. **Read
  the warning below before recommending this.**
- `--keep-conflicting-mcp-entries` — don't auto-remove legacy
  `cardinal-*` per-integration MCP entries from `~/.claude.json`. The
  unified entry already covers everything they did, so keeping them
  just produces duplicate tool listings. Default behavior is to remove
  them (with a backup).
- `--deployment-env <name>` — override the derived
  `deployment.environment` label.
- `--dry-run` — run the full device-code flow, then print what would
  be written. Touches no files.

## A note about `--no-tool-details`

Tool-details capture is **on by default** because without it the
Outcomes Dashboard can't derive `repo` or `service` from per-step
events — every session shows as `repo=unknown` and `service=unknown`.
The trade-off is that bash command lines and file paths may contain
PII some orgs' privacy policies forbid. If the user's org has such a
policy, suggest `--no-tool-details`.

## A note about the MCP entry

The script writes `mcpServers.cardinal` pointing at one URL:

```
"cardinal": {
  "type": "http",
  "url": "https://<host>/api/orgs/<uuid>/mcp",
  "headers": { "X-CardinalHQ-API-Key": "..." }
}
```

This is the **aggregator** URL — a single endpoint that exposes
whatever tools the org has integrations for. As the org configures
more integrations on the Cardinal side, the same URL surfaces more
tools on the next `tools/list` request; the user never needs to
re-run `/cardinal:connect` to "see" new ones.

## After success

Tell the user:

1. Their `~/.claude/settings.json` (env) and `~/.claude.json`
   (mcpServers) have been updated.
2. **Fully quit Claude Code (Cmd-Q on macOS)** and start a new
   session. Both files are read once at process start.
3. Run `/cardinal:status` from the new session to verify both sides.

## Errors

Surface the script's stderr verbatim and don't claim success. Common
cases:

- `Cardinal is already connected as ...` — exit 2 from the
  already-connected guard. Re-run with `--rotate` to overwrite.
- `Consent request expired before approval` — the 10-minute TTL
  elapsed; re-run.
- `Request was denied in the browser` — the user clicked Deny.
- `settings.json is not valid JSON` / `~/.claude.json is not valid
  JSON` — the script refuses to write into an unparseable file. Tell
  the user to fix or back up the file by hand.
- `ingest reachability failed` / `MCP reachability failed` — the
  newly-minted keys don't authenticate at the endpoint. This usually
  means a maestro misconfig (org has no active lakerunner
  integration for the ingest side, gateway not running for the MCP
  side). The bundle was returned but the connection isn't usable.
