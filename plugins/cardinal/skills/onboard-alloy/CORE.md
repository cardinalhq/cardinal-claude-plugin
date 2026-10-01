# onboard-alloy — shared core

**This file is the shared core of the onboard-alloy skill.** It is copied verbatim,
with `scripts/` and `references/`, into each adapter's `skills/onboard-alloy/` by
`build/sync_skills.py`. Do not edit the copies — edit the canonical files under
`common/onboard-alloy/` and re-run the sync.

The adapter's `SKILL.md` (read it first) supplies what differs per agent: how to
locate `$SCRIPTS`, how to connect to Cardinal, and what to say at the end.

Gets a customer's **live** logs, metrics and traces flowing into Cardinal Data Lake
from the Grafana Alloy they already run, **alongside Grafana**. Alloy keeps sending
everything to Grafana exactly as before; a Cardinal branch sends the same data to
Cardinal. Two targets, set by `TARGET` in the values file:

| Target | Who | Cardinal branch ends in |
|---|---|---|
| `s3` | Self-hosted Cardinal Data Lake (Lakerunner on the customer's bucket) | `otelcol.exporter.awss3` writing to `s3://<bucket>/otel-raw/<org-uuid>/<cluster>/` |
| `saas` | Cardinal SaaS (app.cardinalhq.io) | `otelcol.exporter.otlphttp` to Cardinal's OTLP/HTTP intake, with an `x-cardinalhq-api-key` header read from an env var |

Ask which one applies. If unsure: a customer who signs in at app.cardinalhq.io and
has no Lakerunner of their own is `saas`.

Not this skill: dashboards and alert rules (`migrate-from-grafana`, which needs this
done first), historical data, and customers without Alloy (Cardinal's own collectors).

## How it works for the user

**Nothing starts automatically.** The skill reads the Alloy config, proposes a plan,
and produces a patched config plus an IAM policy (`s3`) or the env vars to set from a
Secret (`saas`). **The customer's team deploys it**
through their normal process (Helm, Argo CD, Flux). Data starts flowing then, and
keeps flowing with nobody running anything.

## Rules for the agent

1. **Never break Grafana.** Anything that changes data — delta conversion, attributes,
   batching — lives only in the Cardinal branch. `lint_config.py` enforces this;
   never hand-edit `out/config.alloy` to get past a lint error.
2. **Never apply.** No `kubectl apply`, `helm upgrade`, `aws iam …` writes, or Fleet
   Management pushes. Read-only `kubectl` (get, list, port-forward, logs) is fine
   when the user allows it. The deliverable is files, or a PR in the customer's repo
   if they ask for one.
3. **Scripts write the config, not you.** If a config needs something the scripts
   don't support, stop and say what's missing; don't hand-write Alloy config into
   the output.
4. **Secrets.** Alloy configs often hold Grafana Cloud passwords inline. Show only
   `changes.review.diff` (masked) or `cardinal.alloy`, never `out/config.alloy` or
   the original config in full. Mask values if you must quote a line.
5. **One cluster at a time.** Keep everything under `onboard/<cluster>/`.

## What you need from the user

| Item | Target | Why | Notes |
|---|---|---|---|
| Cardinal connection | both | org, and checking data arrives (step 6) | See SKILL.md. **Always ask which org.** |
| `s3` or `saas` | both | which exporter is rendered | See the table above |
| The Alloy config, **from its source of truth** | both | what gets patched | See step 1 |
| Cluster name | both | `k8s.cluster.name` (and the S3 prefix) | Permanent — see step 0 |
| Organization UUID | `s3` | S3 prefix, IAM scope | From the Cardinal data lake install. Optional for `saas` |
| Data lake bucket and region (and endpoint for MinIO/R2) | `s3` | where Alloy writes | Confirm with the user; don't guess |
| KMS key ARN, if the bucket uses SSE-KMS | `s3` | IAM policy | Ask; a missing KMS grant shows up as AccessDenied |
| Ingest endpoint | `saas` | where Alloy sends | Cardinal's OTLP/HTTP intake for their region, e.g. `https://otelhttp.intake.us-east-2.aws.cardinalhq.io` (base URL, no `/v1/...`). Confirm with the user; don't guess the region |
| A Cardinal API key, **outside chat and outside the values file** | `saas` | authenticates Alloy | The customer creates it in Cardinal and stores it in a Kubernetes Secret. The values file holds only the env var name (`CARDINAL_API_KEY_ENV`, default `CARDINAL_API_KEY`) |

All of these except the Cardinal connection go in **one values file the user fills
in**, `.env.onboard-alloy` (per cluster, in the working directory). Don't collect them
in chat:

```bash
python3 $SCRIPTS/onboard_env.py --init .env.onboard-alloy   # template, chmod 600, never overwrites
```

Then open it for the user in their editor (SKILL.md says how) and say: *"Fill in the
values in `.env.onboard-alloy` (each one is explained in the file), save it, and tell
me when you're done."* The file holds `TARGET`, `ALLOY_CONFIG` (path to the config),
`CLUSTER_NAME` and `VALUES_MODE`, then per target:
- `s3`: `CARDINAL_ORG_ID`, `S3_BUCKET`, `AWS_REGION`, and optionally `S3_ENDPOINT`, `KMS_KEY_ARN`.
- `saas`: `CARDINAL_INGEST_ENDPOINT` and `CARDINAL_API_KEY_ENV` (optionally `CARDINAL_ORG_ID`).

None of them is a secret: Alloy reaches S3 through its IAM role, and the SaaS API key
reaches Alloy as an env var from a Secret. Never add AWS keys or a Cardinal API key to
the file, and never ask for the key in chat. If the user pastes a key anyway, don't
repeat it, tell them to rotate it, and continue with the env var name.

When they say done:

```bash
python3 $SCRIPTS/onboard_env.py --check .env.onboard-alloy
```

`RESULT: OK` → continue. `INCOMPLETE` (exit 3) → show the `FIX` lines, ask them to
correct the file and save, then check again. `alloy_inventory.py` and `render.py` read
the same file (`--env-file`) and stop with exit 3 if it becomes incomplete. To change a
value later (e.g. the bucket), edit the file and re-run render; the file wins over
`plan.json` for these fields.

## Workflow

### 0. Scope and values file

- Ask which clusters. Do them one at a time.
- Ask whether they're on Cardinal SaaS or their own Cardinal Data Lake (`TARGET`).
- Create and open `.env.onboard-alloy` (above), and discuss the two values that need
  thought while the user fills it in:
  - The **cluster name** (`CLUSTER_NAME`): lowercase, digits, `-`, max 63. It becomes a label
    every dashboard filters on (and, for `s3`, part of the S3 path), so **it can't change later**
    without breaking dashboards. Use the name the customer already uses for the
    cluster in Grafana (often the `cluster` label).
  - Where `ALLOY_CONFIG` comes from: step 1 below. Settle that before they fill it in.
- Run `--check` once they say done.
- Ask whether this cluster already sends anything to Cardinal (Cardinal's own
  collectors, or an earlier run). Sending twice doubles ingestion.

### 1. Find the config's source of truth

Editing the wrong layer gets silently reverted. Ask, and confirm from the files:

| Setup | Sign | What to patch |
|---|---|---|
| Plain `grafana/alloy` Helm chart | values with `alloy.configMap.content` | that content (save it to a `.alloy` file for the scripts, then put the result back) |
| Raw ConfigMap in a GitOps repo | Argo CD / Flux manage it | the `.alloy` file in their repo |
| **Grafana k8s-monitoring Helm chart** | release `k8s-monitoring`, pods `alloy-metrics` / `alloy-logs` / `alloy-receiver` | **Not supported yet.** The chart generates the config and runs several Alloy instances. Stop and say so. |
| **Fleet Management** | a `remotecfg` block | **Not supported yet.** Local edits don't stick. Stop and say so. |
| Grafana Agent | `grafana-agent` image | Stop: recommend migrating to Alloy first. |

With read-only cluster access you can confirm what's running
(`kubectl get configmap -n <ns> <name> -o jsonpath='{.data.config\.alloy}'`), but
**patch the source of truth**, not the live ConfigMap.

### 2. Inventory

```bash
python3 $SCRIPTS/alloy_inventory.py --env-file .env.onboard-alloy --out onboard/<cluster>
```

Prints each exporter and each **tap**: the list that feeds a Grafana-bound exporter.
Cardinal gets exactly what that list sends to Grafana, after the customer's own
relabeling, filtering and sampling, so labels match what their Grafana dashboards
use. Prometheus and Loki pipelines get a bridge (`otelcol.receiver.prometheus` /
`otelcol.receiver.loki`). OTLP data converted to Prometheus or Loki format is tapped
before the conversion.

Go through every `WARN` with the user. The important ones:
- **modules / `import.*` / `declare`**: the file doesn't show the whole pipeline. Stop
  unless the user can give you the module files too.
- **several exporters per signal**: if they carry the same data (e.g. two Grafana
  stacks), keep only one tap or Cardinal gets duplicates.
- **tail sampling**: Cardinal gets the same sample Grafana gets. Say so.
- **profiles (`pyroscope.write`)**: not ingested by Cardinal; not sent.

### 3. Plan

The target, org, cluster, bucket, region, endpoint, KMS key, ingest endpoint, API key
env var and values mode come from `.env.onboard-alloy`; change those in that file, not
in the plan. Decide the rest of
`onboard/<cluster>/plan.json` with the user:

| Field | Decide |
|---|---|
| `taps[].enabled` | which pipelines go to Cardinal. Turn one off to leave a signal out. |
| `k8sattributes` | `true` when their pipeline doesn't already add Kubernetes metadata (inventory decides; keep it) |
| `batch` | keep the defaults (10000 / 30000 / 10s). **Each batch is one S3 PUT** (`s3`) or one request (`saas`). |

Metrics are converted to delta in the Cardinal branch for both targets, so Grafana
keeps getting cumulative data.

Also discuss, even though the scripts don't handle them yet (`s3` only):
- **Size.** Every Alloy pod writes its own S3 objects. A DaemonSet makes roughly
  nodes × signals × 6 PUTs a minute when idle (200 nodes ≈ 3,600/min). Above about
  50 nodes, recommend two tiers (thin DaemonSet forwarding to a 2–4-replica
  Deployment that writes to S3). **Two-tier isn't rendered yet.** Delta conversion
  must then run where each series always lands on the same pod.
- **Network.** S3 traffic through a NAT gateway pays per-GB NAT charges. Recommend an
  S3 gateway VPC endpoint if they don't have one.
- **Service graph metrics.** Built only by Cardinal's gateway from traces; Alloy
  writing directly to S3 doesn't produce them.
- **Bucket notifications.** Lakerunner ingests a file when the bucket notifies it.
  Some small self-hosted installs (for example a POC Lakerunner with an in-cluster
  object store) send no bucket notifications. Only the site's gateway collector tells
  Lakerunner about new files, so objects Alloy writes land but are **never ingested**.
  Ask how their bucket notifies Lakerunner. If it doesn't, use `TARGET=saas` with
  `CARDINAL_INGEST_ENDPOINT` set to their gateway collector's OTLP/HTTP endpoint.

🚦 Get explicit approval of the plan before rendering.

### 4. Render and lint

```bash
python3 $SCRIPTS/render.py --env-file .env.onboard-alloy --plan onboard/<cluster>/plan.json \
    --out onboard/<cluster>/out
```

It lints its own output and writes nothing if a rule fails (`RESULT: FAIL`). Rules
are listed in `lint_config.py`. The ones that protect Grafana: no Cardinal component
feeds anything outside the branch (C001), no new delta conversion upstream of their
exporters (C002), and their components are unchanged except for the added taps
(C009). If it fails, report the finding. Don't work around it.

Re-running on an already-patched config updates the Cardinal block in place, so the
same command works after a plan change.

Show the user `out/changes.review.diff` (secrets masked) and walk through it:
the taps added to their lists, then the Cardinal block at the end.

### 5. Hand off the rollout

Give the user, for their platform team:
0. **Alloy stability flag first, as its own change.** render.py prints the level it
   needs (`REQUIRES`). In Alloy v1.20.1, `otelcol.exporter.awss3` is *experimental*
   and `cumulativetodelta` is *public-preview*, so `s3` needs
   `--stability.level=experimental`; `saas` (`otlphttp` is stable) needs
   `public-preview` when metrics are sent, and no flag otherwise. Without the flag,
   Alloy **rejects the whole config**: new or restarted pods crash-loop and **Grafana
   stops receiving data too**. Roll the flag out first. It doesn't change the existing config. Once
   those pods are healthy, roll out the config. (In the `grafana/alloy` Helm chart
   this is a chart value; check the name for their chart version.) The flag allows
   every component at that level, not just these, so say this plainly. Some
   customers have a policy against experimental features, and that is their call.
1. **Credentials.**
   - `s3`: `out/iam-policy.json` allows `s3:PutObject` only under this cluster's
     prefix (plus KMS if set). Attach it to the role Alloy's ServiceAccount assumes
     (IRSA or EKS Pod Identity). Static keys only as a last resort.
   - `saas`: put the Cardinal API key in a Kubernetes Secret and expose it to the
     Alloy pods as the env var `out/env.json` names (`secretKeyRef`), **before** the
     config. Without it, Alloy sends with an empty key and Cardinal rejects every
     request. Only the Cardinal branch fails; Grafana is unaffected. No IAM change.
2. **RBAC.** When `k8sattributes` is on, Alloy's ServiceAccount needs get/list/watch on
   pods, namespaces and replicasets (the Alloy chart usually grants this already).
3. **Env vars.** Set `out/env.json` on the Alloy pods (with `"values": "env"`; for
   `saas` the API key entry is a placeholder for the Secret, in either mode).
4. **Config.** Replace their config with `out/config.alloy` through their normal
   process.
5. **Order.** Non-prod cluster first, then one prod cluster, then the rest.
6. **Rollback.** Revert the change. The Cardinal branch is additive, so reverting
   removes it cleanly. Data already sent stays in Cardinal.

Wait for the user to say it's deployed.

### 6. Check it's working (manual for now)

`verify.py` isn't built yet. Walk the user through these checks, and use the Cardinal
tools where you have them:
1. **Alloy healthy:** pods ready, no restarts or OOM kills. **Failed exports are
   logged at `level=info`** ("Exporting failed. Will retry…" with
   `component_id=otelcol.exporter.awss3.cardinal_onboard`, or
   `otelcol.exporter.otlphttp.cardinal_onboard` for `saas`), so a `level=error` search
   misses them. Grep for that message, and tell the customer to alert on Alloy's
   exporter-failure metrics, not on error logs. For `saas`, a 401/403 means the API key
   env var is missing or wrong. When Cardinal (S3 or the intake) is unreachable, the
   Cardinal queue retries, then drops data **for Cardinal only**. Grafana isn't affected
   (tested for both targets: Cardinal down, senders not slowed, Grafana received
   everything).
2. **Grafana unaffected:** their dashboards still show data, especially `rate()` panels.
3. **Files landing (`s3`):** new objects under `otel-raw/<org>/<cluster>/` with `logs_`,
   `metrics_`, `traces_` names. Lots of tiny objects means batching isn't working.
   Files that land but never show up in Cardinal usually mean the bucket doesn't notify
   Lakerunner (see step 3). For `saas` there are no files: go to check 4.
4. **Cardinal sees it:** metrics, log streams and services for this cluster appear in
   Cardinal (lakerunner discovery tools, filtered by `k8s.cluster.name`).
5. **Nothing missing:** each Grafana `job` / namespace / service shows up in Cardinal.
   A whole job missing usually means a pipeline wasn't tapped.

Check again the next day: delta resets after restarts and S3 throttling at daily peaks
only show up later.

### 7. Report

Tell the user what's flowing per signal, what was left out and why, and the
follow-ups (two-tier for big clusters, VPC endpoint, service graph). When every
cluster is done, the next step is `migrate-from-grafana` for dashboards and alerts.

## Known limits (v1)

- No k8s-monitoring chart, Fleet Management, modules, or Grafana Agent.
- No two-tier rendering.
- The Docker end-to-end test (`tests/onboard_alloy/e2e/run_e2e.py [--target saas]`:
  real Alloy, an S3-compatible store or a Cardinal intake stub that checks the API key
  header, a Grafana stub, fault injection) covers the rendered *template*, not each
  customer's full config. Still watch Grafana ingestion closely
  in the first non-prod rollout.
- No `verify.py`. Step 6 is manual.
