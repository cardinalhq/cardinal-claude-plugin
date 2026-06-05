# Cardinal Claude plugin

Connect Claude Code to **[Cardinal](https://cardinalhq.io)** in a single browser-approved consent:

- **Telemetry** — agent sessions stream to the Cardinal Outcomes Dashboard (workflow classification, cost per satisfied outcome, anti-pattern detection, shared plan candidates).
- **MCP** — the unified `cardinal` MCP server appears in your Claude Code session, exposing whichever observability and integration tools your org has configured (lakerunner, common, github, jira, kube, …).

Both are minted by maestro's device-code flow and committed to your local config atomically. Use `--telemetry-only` if you want the Outcomes Dashboard side but no Cardinal tools in your Claude Code palette.

## Install

```bash
claude plugin marketplace add cardinalhq/cardinal-claude-plugin
claude plugin install cardinal@cardinalhq-claude-plugin
```

## Connect

```
/cardinal:connect
```

That's it. The plugin prints a `https://app.cardinalhq.io/connect?code=ABCD-EFGH` URL — open it in your browser, log in (if you're not already), pick the org to connect, and click **Approve**. The plugin's poller picks up your consent within a few seconds and writes:

| File | What gets written |
|---|---|
| `~/.claude/settings.json` | OTel env block (telemetry side) + `CARDINAL_MCP_URL` / `CARDINAL_MCP_API_KEY` env vars (MCP side) |
| `~/.claude/cardinal.json` | Non-secret state + key ids for `/cardinal:status` and `/cardinal:disconnect` |

Then **fully quit Claude Code** (`Cmd-Q` on macOS) and start a new session — `settings.json` env is read at process start.

Run `/cardinal:status` from the new session to verify both sides.

### Variants

```
/cardinal:connect --telemetry-only      # OTel env only; skip the MCP env vars
/cardinal:connect --rotate              # Mint fresh keys, overwrite existing config
/cardinal:connect --host https://...    # Point at dogfood / customer in-VPC install
/cardinal:connect --no-tool-details     # Privacy-conscious opt-out (see warning below)
/cardinal:connect --dry-run             # Run the device-code flow, print what would be written
```

## What it does

### Telemetry side

The plugin owns these keys in `~/.claude/settings.json` `env`:

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=<your region's intake host>
OTEL_EXPORTER_OTLP_HEADERS=x-cardinalhq-api-key=<your key>
OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code,agent.runtime=claude-code,deployment.environment=<env>,user.email=<email>,cardinal.org=<slug>,cardinal.plugin_version=<semver>
OTEL_LOG_TOOL_DETAILS=1
```

Any other keys you have in `env` (theme, enabledPlugins, etc.) are left alone. `/cardinal:disconnect` removes only the keys above and leaves the rest.

### MCP side

The plugin **declares the `cardinal` MCP server natively** in `plugins/cardinal/.mcp.json`:

```json
{
  "cardinal": {
    "type": "http",
    "url": "${CARDINAL_MCP_URL}",
    "headers": { "X-CardinalHQ-API-Key": "${CARDINAL_MCP_API_KEY}" }
  }
}
```

Claude Code substitutes those `${VAR}` references from `~/.claude/settings.json` env at MCP server connect time. `/cardinal:connect` writes the live values:

```
CARDINAL_MCP_URL=https://<host>/api/orgs/<org-uuid>/mcp
CARDINAL_MCP_API_KEY=<your minted MCP key>
```

The URL points at the **aggregator** — a single durable endpoint that exposes whatever tools your org has integrations for. As your admin enables more integrations on the Cardinal side, the same URL surfaces more tools on the next `tools/list`. **You don't need to re-run `/cardinal:connect` to "see" new tools.**

## Migrating from v0.2 → v0.3

v0.2 wrote `mcpServers.cardinal` directly into `~/.claude.json`. v0.3 lets Claude Code register the MCP server from the plugin's manifest instead — cleaner, no global-config side effects.

On first run after upgrade, `/cardinal:connect` automatically prunes the v0.2 `mcpServers.cardinal` stanza (and any legacy `cardinal-*` per-driver entries) from `~/.claude.json` so they don't collide with the plugin-declared server. A backup is written to `~/.claude.json.bak.<timestamp>` first. Pass `--skip-legacy-cleanup` to opt out of the prune.

## Privacy

Tool-details capture (`OTEL_LOG_TOOL_DETAILS=1`) is **on by default**. Without it, the Outcomes Dashboard can't derive `repo` or `service` per session — every event would show as `repo=unknown`, `service=unknown`. Bash command lines and file paths may contain PII; if your org's privacy policy forbids capturing those, pass `--no-tool-details` to `/cardinal:connect`.

`OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_TOOL_CONTENT` (the higher-PII switches that capture full prompt text and tool I/O) are **never** set by this plugin. If you want them, edit `settings.json` by hand after running connect.

## Commands

| Command | What it does |
|---|---|
| `/cardinal:connect` | Runs the device-code flow and wires up both telemetry and MCP. Use `--telemetry-only` to skip the MCP side, `--rotate` to overwrite an existing config. Prunes v0.2-era `~/.claude.json` entries on upgrade. |
| `/cardinal:status` | Show the configured mode, host, org, both endpoints, key prefixes, connection age, and a reachability probe against each enabled side. |
| `/cardinal:disconnect` | Best-effort revoke the MCP key server-side (via `/api/maestro-keys/<id>/revoke`), strip the plugin-owned env keys from `~/.claude/settings.json`, and delete `~/.claude/cardinal.json`. The ingest-key revoke endpoint isn't shipped yet; the script points at the admin UI. Use `--keep-telemetry` to disconnect only the MCP side. |

## Requirements

- **Claude Code** (latest stable; needs plugin-declared MCP server support with `${VAR}` substitution).
- **Python 3.9+** on PATH (used by the plugin's `bin/` executables).
- A **Cardinal account** — sign up at <https://cardinalhq.io>. The MCP side is empty until your org has at least one integration configured on the Cardinal side; the built-in `common-mcp` tools are available either way.

## License

Apache 2.0. See [LICENSE](./LICENSE).
