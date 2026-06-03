---
description: Connect Claude Code to Cardinal — write the OpenTelemetry env block into ~/.claude/settings.json so sessions stream to the Cardinal Outcomes Dashboard.
disable-model-invocation: true
---

# /cardinal:connect

This skill wires Claude Code's OpenTelemetry up to a Cardinal workspace. It writes
the right keys into the `env` block of `~/.claude/settings.json` (preserving any
unrelated keys), and records non-secret metadata in `~/.claude/cardinal.json` so
`/cardinal:status` and `/cardinal:disconnect` know what was wired.

## How you (Claude) should run this

The plugin ships an executable `cardinal-connect` on the Bash tool's PATH. Invoke
it via the Bash tool with the user's arguments:

```
cardinal-connect [flags]
```

### Required flags

- `--key <ingest-key>` — the Cardinal ingest API key (begins with hex bytes).
- `--org <slug>` — the org slug, e.g. `cardinal-hq`.
- `--ingest-endpoint <url>` — the OTLP/HTTP endpoint, e.g.
  `https://otelhttp.intake.us-east-2.aws.cardinalhq.io`.
- `--user-email <email>` — the user's email for resource attribution.

### Optional flags

- `--host <url>` — Cardinal app host. Defaults to `https://app.cardinalhq.io`.
- `--deployment-env <name>` — derived from host when omitted (`prod` /
  `dogfood` / `cardinal` / `customer` / `unknown`).
- `--no-tool-details` — opt out of capturing bash commands and file paths.
  On by default — required for per-repo and per-team attribution in the
  Outcomes Dashboard. See the warning below.
- `--rotate` — skip the "already connected" confirmation when re-connecting.
- `--dry-run` — print the env block that would be written, don't touch files.
- `--no-color` — disable color output (for non-TTY).

### When the user runs `/cardinal:connect` with no args

Walk them through the values:

1. Tell them the easiest source is the Cardinal web app at
   **`https://app.cardinalhq.io/settings/connect-claude-code`**. That page
   renders a complete env block with a freshly-minted key already inlined.
2. Ask them to copy: the key (from `OTEL_EXPORTER_OTLP_HEADERS`), the endpoint
   (from `OTEL_EXPORTER_OTLP_ENDPOINT`), and their org slug + email (from
   `OTEL_RESOURCE_ATTRIBUTES`).
3. Re-invoke this command with those values as flags.

### A note about `--no-tool-details`

Tool-details capture is **on by default** because without it, the Outcomes
Dashboard cannot derive `repo` or `service` from per-step events — every
session would show as `repo=unknown` and `service=unknown`. The trade-off is
that bash command lines and file paths may contain PII, which some orgs'
privacy policies forbid. If the user's org has such a policy, pass
`--no-tool-details`.

### After success

Tell the user:

1. The key is now in `~/.claude/settings.json` `env`.
2. **Fully quit Claude Code (Cmd-Q on macOS)** and start a new session.
   The env block is read once at process start; the current session won't
   pick it up.
3. After restarting, they can run `/cardinal:status` to verify.

### Errors

If `cardinal-connect` exits non-zero, surface the error message and don't
claim success. Common cases:

- `settings.json is malformed JSON` — tell the user to fix or back up
  `~/.claude/settings.json`; this command refuses to write into an
  unparseable file.
- `missing required flag` — re-prompt with the specific flag name.
- `endpoint unreachable` — the verify-reachability sanity check failed;
  the values may be wrong or the user is offline.
