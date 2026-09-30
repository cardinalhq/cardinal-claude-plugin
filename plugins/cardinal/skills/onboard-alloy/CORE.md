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
`s3://<bucket>/otel-raw/<org-uuid>/<cluster>/`, where Cardinal ingests it.

Not this skill: dashboards and alert rules (`migrate-from-grafana`, which needs this
done first), historical data, and customers without Alloy (Cardinal's own collectors).

## How it works for the user

**Nothing starts automatically.** The skill reads the Alloy config, proposes a plan,
and produces a patched config plus an IAM policy. **The customer's team deploys it**
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

| Item | Why | Notes |
|---|---|---|
| Cardinal connection | org, and checking data arrives (step 6) | See SKILL.md. **Always ask which org.** |
| Organization UUID | S3 prefix, IAM scope | From the Cardinal data lake install |
| Data lake bucket and region (and endpoint for MinIO/R2) | where Alloy writes | Confirm with the user; don't guess |
| The Alloy config, **from its source of truth** | what gets patched | See step 1 |
| Cluster name | S3 prefix + `k8s.cluster.name` | Permanent — see step 0 |
| KMS key ARN, if the bucket uses SSE-KMS | IAM policy | Ask; a missing KMS grant shows up as AccessDenied |

All of these except the Cardinal connection go in **one values file the user fills
in**, `.env.onboard-alloy` (per cluster, in the working directory). Don't collect them
in chat:

```bash
python3 $SCRIPTS/onboard_env.py --init .env.onboard-alloy   # template, chmod 600, never overwrites
```

Then open it for the user in their editor (SKILL.md says how) and say: *"Fill in the
values in `.env.onboard-alloy` (each one is explained in the file), save it, and tell
me when you're done."* The file holds `ALLOY_CONFIG` (path to the config), `CARDINAL_ORG_ID`,
`CLUSTER_NAME`, `S3_BUCKET`, `AWS_REGION`, and optionally `S3_ENDPOINT`, `KMS_KEY_ARN`
and `VALUES_MODE`. None of them is a secret, since Alloy reaches S3 through its IAM
role. Never add AWS keys to it.

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
- Create and open `.env.onboard-alloy` (above), and discuss the two values that need
  thought while the user fills it in:
  - The **cluster name** (`CLUSTER_NAME`): lowercase, digits, `-`, max 63. It becomes part of the
    S3 path and a label every dashboard filters on, so **it can't change later**
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

The org, cluster, bucket, region, endpoint, KMS key and values mode come from
`.env.onboard-alloy`; change those in that file, not in the plan. Decide the rest of
`onboard/<cluster>/plan.json` with the user:

| Field | Decide |
|---|---|
| `taps[].enabled` | which pipelines go to Cardinal. Turn one off to leave a signal out. |
| `k8sattributes` | `true` when their pipeline doesn't already add Kubernetes metadata (inventory decides; keep it) |
| `batch` | keep the defaults (10000 / 30000 / 10s). **Each batch is one S3 PUT.** |

Also discuss, even though the scripts don't handle them yet:
- **Size.** Every Alloy pod writes its own S3 objects. A DaemonSet makes roughly
  nodes × signals × 6 PUTs a minute when idle (200 nodes ≈ 3,600/min). Above about
  50 nodes, recommend two tiers (thin DaemonSet forwarding to a 2–4-replica
  Deployment that writes to S3). **Two-tier isn't rendered yet.** Delta conversion
  must then run where each series always lands on the same pod.
- **Network.** S3 traffic through a NAT gateway pays per-GB NAT charges. Recommend an
  S3 gateway VPC endpoint if they don't have one.
- **Service graph metrics.** Built only by Cardinal's gateway from traces; Alloy
  writing directly to S3 doesn't produce them.

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
0. **Alloy stability flag first, as its own change.** In Alloy v1.20.1,
   `otelcol.exporter.awss3` is *experimental* and `cumulativetodelta` is
   *public-preview*. Without `--stability.level=experimental`, Alloy **rejects the
   whole config**: new or restarted pods crash-loop and **Grafana stops receiving
   data too**. Roll the flag out first. It doesn't change the existing config. Once
   those pods are healthy, roll out the config. (In the `grafana/alloy` Helm chart
   this is a chart value; check the name for their chart version.) The flag allows
   every experimental component, not just these two, so say this plainly. Some
   customers have a policy against experimental features, and that is their call.
1. **IAM.** `out/iam-policy.json` allows `s3:PutObject` only under this cluster's
   prefix (plus KMS if set). Attach it to the role Alloy's ServiceAccount assumes
   (IRSA or EKS Pod Identity). Static keys only as a last resort.
2. **RBAC.** When `k8sattributes` is on, Alloy's ServiceAccount needs get/list/watch on
   pods, namespaces and replicasets (the Alloy chart usually grants this already).
3. **Env vars.** With `"values": "env"`, set `out/env.json` on the Alloy pods.
4. **Config.** Replace their config with `out/config.alloy` through their normal
   process.
5. **Order.** Non-prod cluster first, then one prod cluster, then the rest.
6. **Rollback.** Revert the change. The Cardinal branch is additive, so reverting
   removes it cleanly. Objects already written to S3 stay (already ingested).

Wait for the user to say it's deployed.

### 6. Check it's working (manual for now)

`verify.py` isn't built yet. Walk the user through these checks, and use the Cardinal
tools where you have them:
1. **Alloy healthy:** pods ready, no restarts or OOM kills. **Failed S3 uploads are
   logged at `level=info`** ("Exporting failed. Will retry…" with
   `component_id=otelcol.exporter.awss3.cardinal_onboard`), so a `level=error` search misses
   them. Grep for that message, and tell the customer to alert on Alloy's
   exporter-failure metrics, not on error logs. When S3 is unreachable the Cardinal
   queue retries, then drops data **for Cardinal only**. Grafana isn't affected
   (tested: S3 down, senders not slowed, Grafana received everything).
2. **Grafana unaffected:** their dashboards still show data, especially `rate()` panels.
3. **Files landing:** new objects under `otel-raw/<org>/<cluster>/` with `logs_`,
   `metrics_`, `traces_` names. Lots of tiny objects means batching isn't working.
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
- The Docker end-to-end test (`tests/onboard_alloy/e2e/run_e2e.py`: real Alloy,
  S3-compatible store, a Grafana stub, S3 fault injection) covers the rendered
  *template*, not each customer's full config. Still watch Grafana ingestion closely
  in the first non-prod rollout.
- No `verify.py`. Step 6 is manual.
