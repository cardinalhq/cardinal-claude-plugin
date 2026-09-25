---
name: migrate-from-grafana
description: Migrate Grafana dashboards and alert rules into Cardinal (Maestro dashboards + lakerunner alert rules). Use whenever someone wants to move, copy, import, port or switch their Grafana (Cloud, OSS or Enterprise) dashboards or alerts to Cardinal / CardinalHQ / lakerunner / Maestro, bring existing Grafana monitoring over when adopting Cardinal, or check what of their Grafana setup would carry over — even if they only say "move my grafana stuff to cardinal". Not for teams starting fresh on Cardinal with nothing in Grafana, and not for moving raw telemetry data.
---

# /cardinal:migrate-from-grafana — migrate Grafana to Cardinal

Moves **dashboards and alert rules** from a Grafana instance into a Cardinal org.
It does not move historical telemetry — Cardinal only shows data that is sent to it,
so the services must already be shipping OTLP to Cardinal (the migration is about
the *views and rules*, which is why metric names get checked against Cardinal's
catalog before anything is converted).

Migration is optional. If the user has no Grafana to migrate from (a fresh start on
Cardinal), say there's nothing to migrate and stop — Cardinal's own dashboard
authoring and `manage_alert_rules` MCP tool are the right path for them.

## What you need from the user

| Item | Why | Notes |
|---|---|---|
| Cardinal connection (`/cardinal:connect`) | lists their orgs; "is Cardinal receiving data?" check; switches alert rules on/off | Step 0 starts it for the user if they aren't connected; they only approve an `app.cardinalhq.io` link in the browser. Connected with `dashboards:write alerts:write telemetry:query` (step 0 asks for them), its token also **reads the catalog, creates and updates dashboards and alert rules, and runs validation** — every script uses it when no login token is set, for any org the user belongs to. |
| Which Cardinal org | where everything goes | **Always ask** (step 0) — many users have several orgs, and the connected org is not necessarily the target. |
| Cardinal login token (fallback) | only when `/cardinal:connect` isn't available, or is connected without the three scopes (older Cardinal) | `CARDINAL_TOKEN` in `.env.cardinal`. Carries the user's org role: **Member** (or Owner) can create dashboards and alert rules. **Lives only ~5 minutes** — see below. Alternative that doesn't expire: an org API key with `admin:all` scope (`CARDINAL_API_KEY`; only a Cardinal superadmin can mint one). |
| Grafana URL + service account token | read dashboards, alert rules, datasources | **Viewer** role is enough. Administration → Users and access → Service accounts → Add → Viewer → Add token (`glsa_…`). |
| Which dashboards/alerts | scope | a folder, a tag, specific dashboard UIDs, or "everything" |
| Alert rules on or off | whether migrated rules evaluate immediately | ask at the dry run (step 4) |

Credentials are secrets: have the user put them in env files (below) instead of
pasting them into chat. Create the files for them with empty values, `chmod 600`,
and open them in an editor (`open -e <file>` on macOS). Never echo token values
back — when checking a file, mask them (e.g. `sed -E 's/(TOKEN|KEY)=(.{6}).*/\1=\2…/'`).
Never print `~/.claude/settings.json` or `~/.claude/cardinal*.json` either; the
scripts read what they need from them.

```
# .env.grafana-migrate
GRAFANA_URL=https://<stack>.grafana.net
GRAFANA_TOKEN=

# .env.cardinal
CARDINAL_ORG_ID=<the org chosen in step 0 — fill this in yourself>
CARDINAL_TOKEN=
CARDINAL_API_KEY=
# only when not using /cardinal:connect:
# CARDINAL_URL=https://app.cardinalhq.io
```

**Skip the login token when step 0 connected with all three scopes** — `/cardinal:status`
lists them under Actions, and the scripts print `using the /cardinal:connect token`.
Only fall back to it when a script exits asking for `CARDINAL_TOKEN`.

**The login token lives about 5 minutes**, and a migration takes longer, so expect to
ask for a fresh one several times. How to copy it so it isn't cut off (the Headers
pane in dev tools truncates long values): sign in to Cardinal, switch to the target
org, reload, open dev tools → Network, click Dashboards, **right-click** any
`/api/orgs/...` request → **Copy → Copy as cURL**, paste that anywhere and take
everything after `Bearer ` up to the closing quote. If the header says `CardinalDemo`
instead of `Bearer`, they're in Cardinal's public demo, which is read-only — they need
a real login.

Every script checks the token before sending anything and **exits 5** when it is
expired or cut off (it prints the reason and how long a valid one has left). On exit
5: say which step you're on, ask the user to save a fresh token and reply, then re-run
the same command — every step is safe to re-run. Ask for the token right before
step 2, and once they're in, keep moving between steps without waiting on the user.

Keep all migration files in one working directory (e.g. `./grafana-migration/`).
The scripts ship with this skill and need only Python 3.9+ (standard library).
Locate them once and reuse `$SCRIPTS` in every step — portable across plugin and
personal-skill installs:

```bash
SCRIPTS=$(dirname "$(find ~/.claude/plugins ~/.claude/skills . -name convert.py \
  -path '*migrate-from-grafana/scripts*' 2>/dev/null | head -1)")
[ -f "$SCRIPTS/convert.py" ] || { echo "migrate-from-grafana scripts not found"; exit 1; }
```

## Workflow

### 0. Connect, pick the org, confirm Cardinal is receiving data

Do this first, before asking for anything Grafana-side.

**a. Connected?** Run `python3 $SCRIPTS/cardinal_catalog.py --orgs`. It lists the
user's Cardinal orgs and marks the connected one. If it exits 3 (not connected) or 4
(needs renewing), connect for the user — don't make them run `/cardinal:connect`
separately:

1. Find the connect script — `CONNECT=$(command -v cardinal-connect || find
   ~/.claude/plugins -path '*/bin/cardinal-connect' 2>/dev/null | head -1)`. If there
   is none (the Cardinal plugin isn't installed), fall back to the `.env.cardinal`
   route at the end of this step.
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

The migration scripts read the new connection straight from disk, so there is no
need to restart Claude Code now. The connect script's own "restart Claude Code"
advice only concerns the Cardinal MCP tools in chat — pass it on in the report.

**b. Which org?** Show the `--orgs` list and **ask which org to migrate into**, even
when there is only one (then just confirm it). Never assume the connected org. Write
the chosen org's id into `.env.cardinal` as `CARDINAL_ORG_ID` when you create it; all
scripts use it.

**c. Is it receiving data?**

```bash
python3 $SCRIPTS/cardinal_catalog.py --env-file .env.cardinal --check
```

With a `telemetry:query` connection this works for any of the user's orgs (no login
token). Connected without it, only the connected org can be checked; for another org it
exits 6 — either reconnect with the three scopes (`--rotate`) or run it again right
after the user adds the login token (before step 2). If it says Cardinal isn't receiving
data, stop: the user's services need to send OTLP to Cardinal first (a
data-onboarding step, not a migration one); migrated dashboards would all be empty.

If the user can't or won't connect: ask for the org ID (it's in the
`/api/orgs/<org-id>/...` request URL) and `CARDINAL_URL`, put both in `.env.cardinal`
with the token, and run the `--check` above.

### 1. Export from Grafana

```bash
python3 $SCRIPTS/grafana_export.py --env-file .env.grafana-migrate --out export \
    [--folder "Team X"] [--tag prod] [--uid <dash-uid>]
```

Writes `export/dashboards/*.json`, `export/alerts.json`, `export/datasources.json`.
Show the user the list of dashboards and the alert count and confirm the scope
before continuing — migrating the wrong folder is the most common mistake. If alert
export fails (older Grafana, or no permission), continue with dashboards and say so.

### 2. Read Cardinal's catalog and build the name mapping

```bash
python3 $SCRIPTS/cardinal_catalog.py --env-file .env.cardinal --export export --out catalog \
    [--instance <slug>]
```

If the org has more than one data lake, the script says so and uses the first; ask
the user which one holds the data (names are in `catalog/instance.json`) and re-run
with `--instance <slug>` if it's another. The alert rules are created on this data
lake too.

This lists Cardinal's real metric and label names and writes
`catalog/mapping.suggested.json`: for every metric the Grafana queries use, the
Cardinal name it most likely corresponds to (Prometheus-style names in Grafana
carry `_total` / unit suffixes that lakerunner may not). It also probes whether
this Cardinal instance supports `histogram_quantile`.

Then **review the mapping** — this is where migrations go wrong silently, because a
wrong name produces a dashboard that renders but shows "No data":

- Look at every entry in `_review`. For `no Cardinal metric matched`, check
  `similar_in_cardinal` and `catalog/metrics.json`; set the right name, or `null` if
  the metric genuinely isn't sent to Cardinal (those panels/rules get skipped and
  reported, which is better than an empty panel).
- For renamed metrics, sanity-check they're the same thing (same service, same unit).
- If a metric is missing only because the service isn't sending to Cardinal yet,
  tell the user — that's a data-onboarding gap, not a migration problem.
- Copy the reviewed result to `mapping.json` (drop the `_review` key).

Also check `native_histograms` (Cardinal stores these histograms under the base name
only — no `_bucket/_sum/_count`; the converter rewrites the queries) and `drop_labels`
(labels or exact filter values the Grafana queries use that don't exist in Cardinal,
e.g. an environment label whose value differs between the two pipelines; those
filters are removed). Confirm with the user that removing a filter is right — it
widens what the panel shows.

If the catalog comes back empty, the org isn't receiving data yet; stop and say so.

### 3. Convert (offline)

```bash
python3 $SCRIPTS/convert.py --export export --mapping mapping.json --out plan
```

Produces `plan/dashboards/*.json` (Cardinal dashboard specs), `plan/alerts.json`
(lakerunner rule specs) and `plan/report.json` (every panel and rule marked
`migrated`, `adapted` or `skipped`, with the reason). See
`references/translation-rules.md` for exactly what gets converted, adapted or
dropped — read it when a result looks surprising or the user asks why something
changed.

Summarise the report for the user before writing anything: counts per status, and
every **adapted** item whose meaning changed (the important one: when Cardinal doesn't
support `histogram_quantile`, p50/p95/p99 panels and alerts use the histogram's own
value instead of a true percentile — the title still says "p95", so call these out and
offer to rename them) and every **skipped** item with its reason.

### 4. Dry run

```bash
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog
```

Shows every dashboard and alert rule that would be created or updated (same-name
objects are updated in place, so re-running is safe). Migrated objects keep their
Grafana names — **don't add a name prefix** by default. Only if the dry run shows an
*update* of something that isn't from an earlier run of this migration (it would be
overwritten), ask the user whether to overwrite it or use `--name-prefix "<prefix>"`
(then on every apply/verify call below).

Before step 5, get an explicit go-ahead, and in the same question ask whether the
alert rules should be created **enabled** (they start evaluating immediately, as in
Grafana) or **disabled** (created and validated, switched on later in Cardinal's
Alerts page). Rules that were paused in Grafana are always created disabled. It
writes into the user's Cardinal org, so don't start without the answer.

### 5. Migrate and validate, one item at a time

The user should watch the migration happen item by item: migrate dashboard 1,
validate dashboard 1, migrate dashboard 2, validate dashboard 2, … then the alert
rules the same way. Each step is its own command, so each shows up in the session
as it happens. Get the order first:

```bash
python3 $SCRIPTS/cardinal_apply.py --plan plan --list
```

Then, for each dashboard `i` of `N` in that order (`<uid>` from the list):

1. Say one line: **Dashboard i/N — "<name>": migrating…**
2. Migrate it (Bash description: `Migrate dashboard i/N: <name>`):
   ```bash
   python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog --apply --dashboard <uid>
   ```
3. Say one line: **Dashboard i/N — "<name>": validating…**
4. Validate it (Bash description: `Validate dashboard i/N: <name>`):
   ```bash
   python3 $SCRIPTS/cardinal_verify.py --env-file .env.cardinal --plan plan --catalog catalog --dashboard <uid>
   ```
   It checks the dashboard exists in Cardinal and runs every panel query (variables
   set to "All"), ending with `RESULT: PASS | WARN | FAIL`.
5. Say one line with the outcome and the link, e.g.
   `✓ PASS — 8/8 panels have data → <link>` or
   `⚠ WARN — 4/5 panels have data ("Refund p99": no data) → <link>`,
   then go straight on to the next dashboard.

Then the alert rules, the same way, with `--alert <number>` (from `--list`) and
"Alert rule i/N" in the lines and descriptions — adding `--disable-alerts` to the
apply command if the user chose disabled:

```bash
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog --apply --alert <n> [--disable-alerts]
python3 $SCRIPTS/cardinal_verify.py --env-file .env.cardinal --plan plan --catalog catalog --alert <n>
```

Validation reads the rule back from Cardinal: it must exist, be enabled/disabled as
chosen, and its query must return data. Switching a rule off goes through the
`/cardinal:connect` MCP key (a plain REST update is accepted but doesn't take effect),
so when the target org isn't the connected one the apply line says the switch
failed — tell the user to switch those rules off in Cardinal's Alerts page.

Keep the between-step lines to one line each; save explanations for the report.
Rules for the loop:

- **WARN / validation FAIL:** note it and keep going. Fix these after the loop (below).
- **Exit 5 (token expired or cut off):** ask for a fresh token, then re-run the same
  command and carry on from that item. Expected several times per migration.
- **Apply error (403/422):** stop the loop — it will fail for every remaining item.
  403 on dashboards: the token can't write (Viewer role, or an API key without
  `admin:all`). 403 on alerts: the user is a Viewer (or the Cardinal host predates Member-level alert writes, where only Owners can). `insufficient_scope`: the connect token lacks a scope this step needs — reconnect with `--rotate dashboards:write alerts:write telemetry:query`. 422 *"Alerting is not
  connected for this integration (status='disabled')"*: alerting is switched off on
  that data lake. The error lists the org's other data lakes: if one of them holds the
  same data (check with the user), continue the alerts there with `--instance <slug>`
  on both apply and verify; otherwise an org admin has to switch alerting on, and the
  alerts wait. Report these messages verbatim and don't retry blindly.

After the loop, for items with problems: a query **ERROR** is a translation problem —
fix the mapping or query, re-run `convert.py`, then migrate + validate just that item
again (it updates in place). **No data** while Grafana has data usually means a wrong
metric/label mapping or data that isn't flowing to Cardinal; say which.

Note: Cardinal's query API is not the Prometheus HTTP API. Queries go to
`POST /api/lakerunner/<instance>/query/{metrics|logs}/query` with `{q, s, e, step}`
(times in ms, as strings) and stream back as SSE; `cardinal_catalog.NativeQuery`
wraps this if you need to test a query by hand.

### 6. Report

Give the user a short migration report, built from `plan/applied.json`,
`plan/verify.json` and `plan/report.json`:

- Dashboards: name → Cardinal link (`{CARDINAL_URL}/dashboards/{id}`), panels migrated/adapted/skipped
- Alerts: name → created/updated, enabled or disabled, which data lake, and any meaning changes
- Everything skipped, and why
- Validation result per item (PASS / WARN / FAIL) and the panels that returned no data
- Follow-ups: rotate/delete the Grafana token if it was created for this; alert
  notification routing (Grafana contact points / policies do **not** carry over —
  the user sets up Cardinal notification groups, then attaches them to the rules)
- If step 0 connected them just now: restart Claude Code to get the Cardinal MCP
  tools (querying data, managing alerts) in chat; `/cardinal:disconnect` undoes the
  connection

Offer to write the report into a file (`migration-report.md`) in the working directory.
