---
name: migrate-from-grafana
description: Migrate Grafana dashboards and alert rules into Cardinal (Maestro dashboards + lakerunner alert rules). Use whenever someone wants to move, copy, import, port or switch their Grafana (Cloud, OSS or Enterprise) dashboards or alerts to Cardinal / CardinalHQ / lakerunner / Maestro, bring existing Grafana monitoring over when adopting Cardinal, or check what of their Grafana setup would carry over — even if they only say "move my grafana stuff to cardinal". Not for teams starting fresh on Cardinal with nothing in Grafana, and not for moving raw telemetry data.
---

# /cardinal:migrate-from-grafana — migrate Grafana to Cardinal

This SKILL.md is the **Claude-Code-specific** part of the skill: where the scripts
live, how to connect, and what to say about restarting. The workflow itself — what
to ask for, credentials, steps 0–6 — lives in `CORE.md`, co-located in this
directory. **Read `CORE.md` in full before starting**, then use the sections below
wherever it points to SKILL.md.

## Scripts and agent home

Locate the scripts once and reuse `$SCRIPTS` in every step — portable across plugin
and personal-skill installs. Export the agent home so the scripts read Claude Code's
connect state:

```bash
export CARDINAL_AGENT_HOME=~/.claude
SCRIPTS=$(dirname "$(find ~/.claude/plugins ~/.claude/skills . -name convert.py \
  -path '*migrate-from-grafana/scripts*' 2>/dev/null | head -1)")
[ -f "$SCRIPTS/convert.py" ] || { echo "migrate-from-grafana scripts not found"; exit 1; }
```

Claude Code keeps the MCP key in `~/.claude/settings.json` — never print that file.

## Connect (step 0a)

Claude Code's `cardinal-connect` grants the three extra scopes
(`dashboards:write alerts:write telemetry:query`), so with it the whole migration
runs without a login token, for any org the user belongs to, and `--orgs` lists them.
When `--orgs` exits 3 or 4, connect for the user — don't make them run
`/cardinal:connect` separately:

1. Find the connect script — `CONNECT=$(command -v cardinal-connect || find
   ~/.claude/plugins -path '*/bin/cardinal-connect' 2>/dev/null | head -1)`. If there
   is none (the Cardinal plugin isn't installed), fall back to the `.env.cardinal`
   route at the end of CORE.md step 0.
2. `rm -f ~/.claude/cardinal-pending.json`, then run
   `"$CONNECT" dashboards:write alerts:write telemetry:query` (exit 3) or
   `"$CONNECT" --rotate dashboards:write alerts:write telemetry:query` (exit 4) with the Bash tool's **`run_in_background: true`** —
   it blocks for up to 10 minutes waiting for approval, and its stdout only arrives
   when it exits. Add `--host <their Cardinal URL>` for a self-hosted Cardinal.
3. Within a few seconds it writes `~/.claude/cardinal-pending.json`; read
   `verification_uri` from it (retry a few times, 1 s apart) and show it:
   "To connect Claude Code to Cardinal, open this link, log in, pick an org, and click
   **Approve**: `<verification_uri>`". Mention in the same message that approving also
   sends this Claude Code's usage telemetry to that Cardinal org, and that
   `/cardinal:disconnect` undoes it.
4. Wait for the background command to finish. On success, run `--orgs` again. On
   failure (denied, expired, "already connected"), show its error verbatim; for
   "already connected", run it again with `--rotate`.

`/cardinal:status` lists the granted scopes under Actions.

In step 5, give each migrate/validate Bash call the description CORE.md names
(`Migrate dashboard i/N: <name>`, …) so each item shows up in the session.

## Report

If step 0 connected them just now: tell them to restart Claude Code to get the
Cardinal MCP tools (querying data, managing alerts) in chat; `/cardinal:disconnect`
undoes the connection.
