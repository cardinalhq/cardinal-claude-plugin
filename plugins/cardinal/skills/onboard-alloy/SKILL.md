---
name: onboard-alloy
description: Get a customer's live logs, metrics and traces flowing into Cardinal Data Lake from the Grafana Alloy they already run, alongside Grafana, without changing what Grafana receives. Use when someone wants to send telemetry from Alloy to Cardinal, is moving from Grafana to Cardinal and has no data in Cardinal yet, or when /cardinal:migrate-from-grafana stops because Cardinal isn't receiving data. Not for dashboards or alert rules (migrate-from-grafana), historical data, or setups without Alloy (Cardinal's own collectors).
---

# /cardinal:onboard-alloy — send live telemetry from Alloy to Cardinal

This SKILL.md is the **Claude-Code-specific** part of the skill. The workflow lives
in `CORE.md`, co-located in this directory. **Read `CORE.md` in full before
starting**, then use the sections below wherever it points to SKILL.md.

## Scripts

Locate the scripts once and reuse `$SCRIPTS` in every step:

```bash
SCRIPTS=$(dirname "$(find ~/.claude/plugins ~/.claude/skills . -name render.py \
  -path '*onboard-alloy/scripts*' 2>/dev/null | head -1)")
[ -f "$SCRIPTS/render.py" ] || { echo "onboard-alloy scripts not found"; exit 1; }
```

They need only Python 3.9+ and are offline: they read files and write files under
`onboard/<cluster>/`. Nothing is sent anywhere.

## Opening the values file

After `onboard_env.py --init`, open the file for the user: `open -e .env.onboard-alloy`
on macOS, `xdg-open .env.onboard-alloy` on Linux (never a terminal editor like nano:
there is no terminal here for it). If you can't open an editor
from here, give them the full path and ask them to open it. Don't read the values
back into chat beyond what `--check` prints.

## Connect

Only step 6 (checking data arrives) needs Cardinal. If the Cardinal MCP tools are
available in this session, use the lakerunner discovery tools for it. If not,
suggest `/cardinal:connect`, then restarting Claude Code to load the tools.

## Report

End with the next command: once every cluster is flowing, `/cardinal:migrate-from-grafana`
moves the dashboards and alert rules.
