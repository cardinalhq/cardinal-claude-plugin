# migrate-from-grafana

Moves your Grafana dashboards and alert rules into Cardinal. Claude does the
work; you provide credentials and approve each step. See SKILL.md for the flow
Claude follows.

## Before you start

- **Your services already send telemetry to Cardinal.** This migrates
  *dashboards and alert rules*, not historical data. A dashboard for a service
  that isn't sending data to Cardinal will be empty.
- **Claude Code** is installed, with the **Cardinal plugin** (install
  instructions: [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)).
- **Python 3.9+** is available (`python3 --version`). Nothing else to install.

## 1. Get your tokens ready

**Cardinal:** usually nothing to copy. Claude connects you with
`/cardinal:connect dashboards:write alerts:write telemetry:query` (you approve
one link in the browser), and that connection does the whole migration. You
need the **Member** (or Owner) role in the target org.

**Cardinal login token**, only if Claude asks for one (no Cardinal plugin, or an
older Cardinal that doesn't offer those scopes):

1. Sign in to Cardinal, switch to the org you're migrating into, and reload the page.
2. Open browser dev tools → **Network** and click **Dashboards**.
3. **Right-click** any request to `/api/orgs/…` → **Copy → Copy as cURL**.
4. Paste it anywhere and copy everything after `Bearer ` up to the closing quote.
   (Don't copy it from the Headers pane; it cuts long tokens off.)

**The token only lasts about 5 minutes**, so get it when Claude asks for it, and
expect Claude to ask for a fresh one a few times during the migration. Claude
tells you when, and carries on from the same step. (An org API key with
`admin:all` scope doesn't expire; only a Cardinal superadmin can create one.)

**Grafana token**

(read-only): Administration → Users and access → Service accounts →
**Add service account** (role: **Viewer**) → **Add token**. Copy the `glsa_…` value.

Also decide **what** to migrate: a Grafana folder, a tag, specific dashboards,
or everything.

## 2. Start the migration

```bash
mkdir grafana-migration && cd grafana-migration
claude
```

Then type `/cardinal:migrate-from-grafana`, or just ask:
*"Migrate my Grafana dashboards and alerts in the 'Production' folder to Cardinal."*

## 3. Approve the Cardinal connection (first time only)

If Claude Code isn't connected to Cardinal yet, Claude shows you an
`app.cardinalhq.io` link. Open it, log in, pick an org, and click **Approve**.
Claude carries on by itself once you do. Claude uses this connection to list
your Cardinal orgs, check that your data is arriving, and switch migrated alert
rules on or off. It also sends your Claude Code usage telemetry to that org;
`/cardinal:disconnect` undoes it.

Claude then shows your Cardinal orgs and **asks which one to migrate into**. It
doesn't have to be the one you approved.

## 4. Fill in the files Claude creates

Claude creates two files and opens them in your editor:

```
.env.grafana-migrate   → GRAFANA_URL, GRAFANA_TOKEN
.env.cardinal          → CARDINAL_TOKEN
```

Fill them in and save. Don't paste tokens into the chat.

## 5. Answer Claude's questions as it works

| Step | Claude does | You do |
|---|---|---|
| Org | Lists your Cardinal orgs | Pick the one to migrate into |
| Data check | Confirms Cardinal is receiving your data in that org | — |
| Export | Pulls dashboards and alerts from Grafana, lists them | Confirm it's the right set |
| Name mapping | Matches Grafana metric names to Cardinal's (they often differ, e.g. `_total` suffixes) | Confirm or correct names it isn't sure about |
| Convert | Converts offline; reports what was migrated as-is, adapted, or skipped | Review changes, e.g. p95/p99 panels that become averages |
| Dry run | Shows exactly what it will create or update in Cardinal (same names as in Grafana) | Say **"go ahead"**, and whether alert rules should be **enabled** or **disabled**. Nothing is written until you do. |
| Migrate + validate | Goes through your dashboards one at a time, then your alert rules | Watch; nothing to do |

During that last step you'll see each dashboard migrated and then checked,
before Claude moves on to the next one:

```
Dashboard 1/3 — "API Overview": migrating…
  ✓ created dashboard 1/3 "API Overview" (8 panels) → https://app.cardinalhq.io/dashboards/…
Dashboard 1/3 — "API Overview": validating…
  ✓ PASS — 8/8 panels have data
Dashboard 2/3 — "Payments": migrating…
  ✓ created dashboard 2/3 "Payments" (5 panels) → https://app.cardinalhq.io/dashboards/…
Dashboard 2/3 — "Payments": validating…
  ⚠ WARN — 4/5 panels have data ("Refund p99": no data)
…
```

**PASS** means the dashboard is in Cardinal and every panel shows data. **WARN**
means some panels have no data; Claude explains why at the end and offers
fixes. **FAIL** means the dashboard didn't make it into Cardinal, or none of its
panels have data.

## 6. Get your report

Claude gives you links to each new Cardinal dashboard, the status of each alert
rule, everything skipped and why, and follow-ups. It can save this as
`migration-report.md`.

## 7. After the migration

- **Set up notifications.** Grafana contact points and notification policies
  don't carry over. Create notification groups in Cardinal and attach them to
  the migrated alert rules.
- **Delete the Grafana service account token** you created for the migration.
- Run Grafana and Cardinal side by side for a while and compare before
  switching Grafana off.

## Troubleshooting

| You see | Meaning |
|---|---|
| The connect link expired or you clicked Deny | Tell Claude to try again; it shows a fresh link |
| "the login token expired" / "is cut off" | Normal: tokens last ~5 minutes. Copy a fresh one (Copy as cURL) into `.env.cardinal` and tell Claude to continue; it picks up where it stopped. |
| `403` on dashboards | Your Cardinal role in that org is Viewer |
| `403` on alerts | Your Cardinal role in that org is Viewer (older Cardinal versions also required **Owner**) |
| "Alerting is not connected…" | Alerting is switched off on that Cardinal data lake. Claude offers another data lake in your org if one has the same data; otherwise ask your admin to switch alerting on. Dashboards are unaffected. |
| Panels show "No data" | The metric isn't reaching Cardinal yet, or its name was mapped wrong |

### Don't want to connect Claude Code to Cardinal?

Tell Claude when it shows the link. It then asks you to add
`CARDINAL_URL` and `CARDINAL_ORG_ID` (the org ID is in the same `/api/orgs/<org-id>/…`
request URL) to `.env.cardinal` next to your token, and runs the data check
with those instead.
