# Cardinal Claude plugin

Stream Claude Code telemetry to **[Cardinal](https://cardinalhq.io)** so your agent sessions show up in the Outcomes Dashboard — workflow classification, cost per satisfied outcome, anti-pattern detection, shared plan candidates.

This is the **v0.1 telemetry-only release.** A future release adds the unified Cardinal MCP that exposes every integration (lakerunner, github, jira, kube, …) under one connection.

## Install

```bash
claude plugin marketplace add cardinalhq/cardinal-claude-plugin
claude plugin install cardinal@cardinalhq-claude-plugin
```

## Connect

The fastest path is via the Cardinal web app — it mints a fresh ingest key and renders the env block for you:

1. Sign in to **<https://app.cardinalhq.io/settings/connect-claude-code>**.
2. Click **Generate**. Copy the four values out of the env block:
   - `OTEL_EXPORTER_OTLP_ENDPOINT` — the SaaS ingest endpoint for your region.
   - The key part of `OTEL_EXPORTER_OTLP_HEADERS` (after `x-cardinalhq-api-key=`).
   - `cardinal.org` from `OTEL_RESOURCE_ATTRIBUTES`.
   - `user.email` from `OTEL_RESOURCE_ATTRIBUTES`.
3. Inside Claude Code, run:

   ```
   /cardinal:connect \
     --ingest-endpoint https://otelhttp.intake.us-east-2.aws.cardinalhq.io \
     --key <paste-key-here> \
     --org cardinal-hq \
     --user-email you@example.com
   ```

4. **Fully quit Claude Code** (`Cmd-Q` on macOS) and start a new session. The OTel env block is read at process start — the running session won't pick it up.

5. Run `/cardinal:status` to verify.

## What it does

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

Non-secret metadata about the connection is recorded in `~/.claude/cardinal.json` so `/cardinal:status` can report key age and connection details without having to crack open the secret-bearing settings.json header.

## Privacy

Tool-details capture (`OTEL_LOG_TOOL_DETAILS=1`) is **on by default**. Without it, the Outcomes Dashboard can't derive `repo` or `service` per session — every event would show as `repo=unknown`, `service=unknown`. Bash command lines and file paths may contain PII; if your org's privacy policy forbids capturing those, pass `--no-tool-details` to `/cardinal:connect`.

`OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_TOOL_CONTENT` (the higher-PII switches that capture full prompt text and tool I/O) are **never** set by this plugin. If you want them, edit `settings.json` by hand after running connect.

## Commands

| Command | What it does |
|---|---|
| `/cardinal:connect` | Wire the env block. Re-run with `--rotate` to overwrite an existing config (e.g. with a fresh key). |
| `/cardinal:status` | Show the configured host / org / endpoint, the connection age, and run a small reachability probe against the ingest endpoint. |
| `/cardinal:disconnect` | Remove the plugin-owned env keys from `settings.json` and delete `~/.claude/cardinal.json`. **Does not yet revoke the key server-side** — do that at <https://app.cardinalhq.io/settings/api-keys>. |

## Roadmap

- **v0.2** — device-code authentication so `/cardinal:connect` opens a browser and walks the user through approval without copy-paste.
- **v0.2+** — unified Cardinal MCP: one MCP connection that exposes every integration the user's org has enabled (lakerunner, common, github, jira, slack, kube, …).
- **v0.2+** — server-side key revocation on `/cardinal:disconnect`.

## Requirements

- **Claude Code** (latest stable).
- **Python 3.9+** on PATH (used by the plugin's `bin/` executables).
- A **Cardinal account** with at least one Lakerunner integration configured — sign up at <https://cardinalhq.io>.

## License

Apache 2.0. See [LICENSE](./LICENSE).
