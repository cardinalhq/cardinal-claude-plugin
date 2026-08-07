---
name: deploy-sentinel
description: "Author a Sentinel Custom Resource next to a compiled Sentinel directory and guide the user through the git-push → ArgoCD → controller reconciliation flow."
---

# deploy-sentinel (Claude Code) — author a Sentinel CR next to a compiled Sentinel directory

**What this skill does.** Given a Sentinel directory (produced by `/mechanize`), author a `sentinel-cr.yaml` next to it that references the current git repo + path, prompts the user for the deploy-time bindings (namespace, inputs, schedule, capability providers, sinks), and prints the git-push + verify steps.

**What this skill does NOT do.** It never `git push`es. It never `git commit`s. It never creates k8s namespaces, secrets, or the controller. Those are one-time platform/human actions.

The load-bearing facts (repo URL, branch, path-in-repo) come from `git` commands, not from guesses. The user is prompted for everything else.

## Stage 1 — Argument parsing

The user typed `/cardinal:deploy-sentinel <sentinel-dir>`.

- If the argument is **absent**: ask the user for the path. Do NOT guess (there may be many Sentinel dirs under `mechanize-out/` or `sentinels/`).
- If the argument **exists but does not contain `sentinel.yaml`**: refuse. Print exactly why (e.g. "no `sentinel.yaml` at `<dir>` — did you mean the parent?"). Do not proceed.
- If the argument is a **relative path**: resolve it to an absolute path against the current working directory before continuing.

Call the resolved absolute path `SENTINEL_DIR` for the rest of this skill.

## Stage 2 — Discover repo context

Run these Bash commands from inside `SENTINEL_DIR`. Fail loudly (stop, print the error, do not write a CR) if any of them fails.

```
cd "<SENTINEL_DIR>"
REPO_ROOT=$(git rev-parse --show-toplevel)
REPO_URL_RAW=$(git remote get-url origin)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
```

Normalize `REPO_URL_RAW`:

- `git@github.com:ORG/REPO.git` → `https://github.com/ORG/REPO`
- `https://github.com/ORG/REPO.git` → `https://github.com/ORG/REPO`
- Leave any other shape unchanged (but tell the user, so they can hand-edit).

Compute `PATH_IN_REPO` as `SENTINEL_DIR` relative to `REPO_ROOT`. If `SENTINEL_DIR` is not inside `REPO_ROOT` (e.g. absolute path outside the tree), refuse.

Print a one-line summary before continuing so the user can interrupt if any of this is wrong:

```
Repo:   <REPO_URL> @ <BRANCH>
Path:   <PATH_IN_REPO>
Root:   <REPO_ROOT>
```

## Stage 3 — Read the Sentinel dir

Read three files from `SENTINEL_DIR`:

1. **`sentinel.yaml`** (required). Capture:
   - `metadata.name` → will become the CR's default `metadata.name`.
   - `spec.inputs` → the free-form input schema. Note which keys have declared defaults and which are required.
   - `spec.capabilities.required[]` → the list of abstract capability ids the executor will need bound.

   If `sentinel.yaml` is missing or unparseable, stop and print why.

2. **`inputs.json`** (optional but common). Capture the current key/value defaults. These become the suggested defaults in Stage 4 for anything the user is prompted to fill in.

3. **`deployment.yaml`** (**required — not optional**). If present, capture:
   - `capabilityBindings` — the per-capability provider hints already recorded. Use them as suggested defaults in 4.5.
   - `findingsRouting` — the rules that decide which sink actually fires (see 4.6).
   - `execution.allowFixtures` — whether fixture bindings are permitted in remote mode.

   **If it is missing, stop and author it before continuing — the deploy cannot work without it.** `/mechanize` does not emit one (it writes `sentinel.yaml`, `rationale.md` and `functions/` only), so for a freshly mechanized directory this file is *always* missing and authoring it is a real step of this skill, not an edge case.

   Why it is mandatory: the controller projects `/config/deployment.yaml` by merging the CR's `spec.capabilities` / `spec.sinks` **into this file**. `schemaVersion`, `kind` and `runtime` come from the directory side and **nowhere else** (`k8s/controller/projections.py`, `project_deployment`), and all three are in `required:` in `common/deployment-schema.yaml`. With no `deployment.yaml` in the directory, the controller projects a file with none of them, the executor's `load_deployment` fails schema validation at pod start, and the Job CrashLoops with an error naming `runtime`. `findingsRouting` lives here too, so without this file no finding is delivered even if the DAG runs.

   Show the user this template, fill in the input keys from `sentinel.yaml`'s `spec.inputs` and the function ids from its function nodes, and ask before writing `<SENTINEL_DIR>/deployment.yaml`:

   ```yaml
   schemaVersion: mechanize.dev/v1alpha1
   kind: SentinelDeployment
   # Registered runtime ids: k8s-controller | ci-plugin | daemon | manual.
   # A CR reconciled by the sentinel-controller is k8s-controller; lint rule
   # R15 FAILs any value not in common/integrations.yaml.
   runtime: k8s-controller

   # No capabilityBindings — the CR supplies them (4.5), and a CR binding
   # replaces the directory's binding for the same id wholesale. Add them
   # here only for capabilities the CR will NOT name.

   inputBindings:
     instance: {source: dispatch}
     # ...one entry per key in sentinel.yaml's spec.inputs

   findingsRouting:
     - match: {"*": true}
       sink: stdout

   functions:
     # ...one entry per function node id in sentinel.yaml
     example-function-id: {network: disabled, filesystem: none}
   ```

   Do **not** add `execution.allowFixtures: true` unless the user is deliberately choosing the fixture path in 4.5.1 — its absence is what makes a stray `provider: fixture` fail remote lint instead of quietly producing a synthetic finding in production.

   This file must be committed and pushed alongside `sentinel-cr.yaml` (Stage 6.1) — the executor pod git-clones the Sentinel directory and gets only what is in the repo.

Also check for a **`fixtures/` directory** (`ls <SENTINEL_DIR>/fixtures/`). Its presence tells you the Sentinel was authored for fixture-backed replay; note which capability ids already have a file, because those are the ids for which `provider: fixture` will actually work in 4.5.

## Stage 4 — Author the CR interactively

Prompt the user for each field below **in order**. For each prompt, offer a concrete default the user can accept with a single word. Do not batch the prompts — one question at a time so the user can course-correct.

### 4.1 `metadata.namespace`

Ask: "Which k8s namespace on the target cluster should this Sentinel run in?"

Suggest the most likely value in this order:
- If the Sentinel's `metadata.name` looks like `<something>-<service>`, suggest `<service>`.
- Else suggest the repo name (last segment of `REPO_URL`).
- Else suggest `default`.

Tell the user: "This namespace must already exist on the target cluster. This skill does not create it."

### 4.2 `metadata.name`

Ask: "CR name?" Default: the Sentinel's `metadata.name`. Accept anything DNS-1123 compliant.

### 4.3 `spec.inputs`

For every key in the Sentinel's `spec.inputs` schema:

- If the key has a default in the schema OR a value in `inputs.json`, present that as the suggested default.
- Otherwise, prompt with no default and require an answer.

Only include keys the Sentinel actually declares. Do not invent inputs.

### 4.4 `spec.schedule`

Ask: "One-shot or recurring?"

- One-shot → omit `spec.schedule` in the output.
- Recurring → ask for a cron expression. Default: `*/5 * * * *` (every 5 minutes, the standard polling cadence). Validate it looks like a 5-field cron string; if not, re-ask.

### 4.5 `spec.capabilities[]`

**Two concrete providers are registered in the executor today** (`spike/executor/capabilities.py`, `spike/executor/providers/mcp.py`). Neither is a placeholder; the choice between them is the choice between a live run and a replayed one.

| Provider | What it does | Registered for | Needs Secrets? |
|---|---|---|---|
| `mcp` | one stateless JSON-RPC `tools/call` POST to the Cardinal MCP gateway; returns live telemetry | `observability.list-services`, `observability.error-overview`, `observability.query-logs`, `observability.query-metrics` | yes — endpoint + token |
| `fixture` | reads a checked-in JSON file from the Sentinel dir and returns it verbatim; no network | all six ids above plus `code.grep`, `code.read` | no |

**How the operator picks.** Ask, per required `id`: "Live telemetry or a pinned fixture?"

- **Live telemetry → `mcp`.** This is the answer for anything that will run on a schedule against prod. Only the four `observability.*` ids can use it — if the Sentinel requires `code.grep` or `code.read`, tell the user plainly that no live provider exists for those yet and they must use `fixture` (see 4.5.1).
- **Pinned fixture → `fixture`.** For a demo of DAG topology, a deterministic CI replay, or a capability with no live provider. Findings are synthetic; say so out loud.

Do not offer any other provider name. `resolve_provider` raises `UnknownProviderError` at DAG time for anything unregistered, and the run fails at the first tool node.

#### 4.5.1 If the answer is `fixture`

`fixture` needs **no Secrets** — omit `endpointSecretRef` and `tokenSecretRef` from that entry. It needs files instead, and this is the part that has been undocumented:

- Create `<SENTINEL_DIR>/fixtures/` and add one JSON file per capability, named `<capability-id>.json` — e.g. `fixtures/observability.list-services.json`. An underscored spelling (`observability_list-services.json`) is also accepted, as is a per-node override `fixtures/<node-id>.json`, which wins over the per-capability file.
- The file holds the tool response verbatim. Optionally it may hold `{"_byArgs": {"<canonical-json-args>": <result>}, "_default": <result>}` to vary the response by call arguments; a `_byArgs` miss with no `_default` fails the node.
- **Missing file = hard failure.** The provider raises `FileNotFoundError` naming the directory and the three filenames it tried. There is no fallback.
- These files must be committed — the executor pod git-clones the Sentinel directory and gets only what is in the repo.
- Remote-mode `sentinel-lint` **fails** a `fixture` binding unless the Sentinel directory's own `deployment.yaml` sets `execution.allowFixtures: true`. Tell the user to add it, and tell them why it is a gate: it exists so fixtures cannot reach production unnoticed.

#### 4.5.2 If the answer is `mcp`

- Ask: "Which Secret holds the endpoint URL?" Default: `cardinal-mcp-endpoint`. The controller reads key **`endpoint`** from it.
- Ask: "Which Secret holds the auth token?" Default: `cardinal-mcp-token`. The controller reads key **`token`** from it.
- The endpoint value should be the **full gateway URL ending in `/mcp`** (e.g. `http://maestro-maestro.maestro.svc.cluster.local:4200/api/orgs/<orgId>/mcp`). If the user only has a base URL, the provider can build the suffix instead, but then the binding needs an org id, which this skill does not author — tell them to seed the full URL.
- **Every lakerunner tool requires an `instance` argument** (the integration slug, e.g. `prod`). If the Sentinel's tool nodes reference `${inputs.instance}`, make sure `instance` is collected in 4.3. If they do not, the run fails at the first tool node with an explicit "no 'instance' argument" error — flag this to the user before writing the CR rather than letting them discover it in pod logs.

Tell the user: "These Secrets must already exist in the target namespace. This skill does not create them."

Emit one entry in `spec.capabilities[]` per required id. Include `endpointSecretRef` / `tokenSecretRef` only for `mcp` entries.

The `mcp` provider serves any capability id — the four legacy `observability.*` ids via its alias map, and transcript-derived ids by passthrough (the id IS the gateway tool name) — so a binding with `provider: mcp` passes remote-mode lint rule R10. There is no capability registry; R10 checks provider resolvability against the runtime's registrations.

### 4.6 `spec.sinks`

Use `{ id: stdout }`. **`stdout` is the only sink registered in the executor today** (`spike/executor/sinks/` contains `stdout.py` and nothing else) — findings go to pod logs, readable via the `kubectl logs` command in 6.3. `resolve_sink` raises for any other id and the run fails when it tries to deliver.

Do NOT offer a Slack sink. It does not exist yet. If the user asks for one, say so directly rather than emitting an entry that will fail at delivery time — after the DAG has already done its work.

Two things to tell the user, because both are surprising:

- Which sink actually fires is decided by `findingsRouting` in the Sentinel directory's own `deployment.yaml`, not by this list. The directory needs a rule such as `findingsRouting: [{ match: {"*": true}, sink: stdout }]` or no finding is delivered at all. That file is mandatory and Stage 3 item 3 has you author it if `/mechanize` did not — if you skipped that step, go back to it now.
- **`spec.sinks` on the CR is currently projected into `deployment.yaml` as a top-level `sinks:` key that the executor does not read.** Only `findingsRouting` is consulted. Setting `spec.sinks` therefore has no runtime effect today — emit it because the CRD models it and the seam is expected to be fixed, but do not tell the user it changes delivery.

If `deployment.yaml` already listed `findingsRouting` entries, report which sinks they name so the user can see what will actually fire.

### 4.7 `spec.runtime.image`

Ask: "Executor image?" Default: `ghcr.io/cardinalhq/sentinel-executor:v0.2.0` — the current release. `v0.1.3` was the first tag both multi-arch (amd64 + arm64) **and** carrying the `mcp` provider. `v0.1.4` fixed a nested `?:` in `severityExpression` and the fixture provider's capability whitelist. **`v0.2.0` is breaking twice:** `functions.<id>.network` is now enforced — a function node whose policy is not `enabled` (including one with no `functions:` entry, which defaults to denied) raises `NetworkAccessDenied` rather than silently reaching the network — and the capability registry is gone, so remote lint R10 checks whether the runtime can resolve (capability, provider) instead of consulting a YAML allowlist. Deployments binding unimplemented providers (`lakerunner`, `prometheus`, `github-checkout`) now FAIL lint.

Two warnings worth giving unprompted:

- **Do not use `v0.1.0`.** It is amd64-only and fails to pull on arm64 (Graviton) nodes.
- **`mcp` requires `v0.1.3` or newer.** The provider ships only in an image built from a commit containing `spike/executor/providers/mcp.py`; `v0.1.2` and every earlier tag predate it. Binding a capability to `provider: mcp` on `v0.1.2` fails with `UnknownProviderError` at the first tool node, on every scheduled run. Never resolve doubt by picking "the newest tag you happen to know about" — check `spike/executor/VERSION` in the repo, which is exactly what `.github/workflows/executor-image.yml` publishes as `v<VERSION>`, and confirm the tag exists in GHCR before pinning it.

If `spike/executor/VERSION` is ahead of every tag published in GHCR (i.e. the branch adding the provider has not merged yet), say so plainly: the CR cannot run until that merge builds the image. Do not silently downgrade to an older published tag — that trades a clear "image not found" for an `UnknownProviderError` on every run.

Do NOT prompt for `spec.runtime.resources`, `timeoutSeconds`, `activeDeadlineSeconds`. The controller applies sane defaults; the CR omits them unless the user specifically asks to override.

### 4.8 Private-repo handling

If `REPO_URL` is an SSH URL or the user tells you the repo is private, ask: "Which Secret holds the deploy key for this repo?" and emit `spec.source.git.credentialsSecretRef: <name>`.

If the repo is public (the common case for Cardinal-owned repos), **explicitly emit `credentialsSecretRef: null`** in the output so it is obvious the omission was intentional.

## Stage 5 — Write `sentinel-cr.yaml`

Write the collected fields to `<SENTINEL_DIR>/sentinel-cr.yaml`. Structure exactly as follows; **omit any field the user did not set** (except the intentional `credentialsSecretRef: null` from 4.8):

```yaml
apiVersion: sentinels.cardinalhq.io/v1alpha1
kind: Sentinel
metadata:
  name: <4.2>
  namespace: <4.1>
spec:
  source:
    git:
      url: <REPO_URL>
      ref: <BRANCH>
      path: <PATH_IN_REPO>
      credentialsSecretRef: <4.8 or null>
  inputs:
    <4.3 collected key/value pairs>
  schedule: <4.4 if recurring>
  runtime:
    image: <4.7>
  capabilities:
    - id: <id>
      provider: <provider>
      endpointSecretRef: <name>
      tokenSecretRef: <name>
    # ...one entry per 4.5 collection
  sinks:
    - id: stdout
    # ...plus 4.6 additions
```

Do NOT write null or empty maps/arrays for sections the user did not populate (e.g. drop `schedule:` entirely for one-shot; drop `inputs:` entirely if the Sentinel declares none).

After writing, print the full CR back to the user for a final sanity check and ask "Write this to `<SENTINEL_DIR>/sentinel-cr.yaml`? (y/n)". Only write on `y`.

## Stage 6 — Print next steps

Once the file is written, print three blocks.

### 6.1 Git commands

Print the exact commands, ready to copy-paste. Do NOT run them.

```
cd <REPO_ROOT>
git add <PATH_IN_REPO>/sentinel-cr.yaml <PATH_IN_REPO>/deployment.yaml
git commit -m "Add Sentinel CR for <metadata.name>"
git push
```

Include `deployment.yaml` (and `fixtures/`, if 4.5.1 applied) in the `git add` — the executor pod clones the Sentinel directory from the repo, so an uncommitted `deployment.yaml` does not exist to the run and the pod dies at start with a schema error naming `runtime`.

### 6.2 Deploy-glue check

Detect whether the owning repo's deploy pipeline already picks up `sentinels/*/sentinel-cr.yaml`. Use Bash (from `REPO_ROOT`) to check for these markers, in order:

- **Kustomize:** `find . -maxdepth 4 -name kustomization.yaml -not -path '*/node_modules/*'`, then grep the matches for the string `sentinels/` or `sentinel-cr.yaml`.
- **Helm:** `find charts -maxdepth 3 -name 'Chart.yaml' 2>/dev/null` and, if any chart exists, grep its `templates/` for `sentinel-cr`.
- **ArgoCD ApplicationSet:** `grep -r 'kind: ApplicationSet' . --include='*.yaml' -l 2>/dev/null`, then grep those files for `sentinels/`.
- **Nothing found:** the repo has no deploy glue for Sentinel CRs yet.

Report which shape you found (or "none"). Then print the matching one-time addition:

- **Kustomize repo, glue missing:** "Add `sentinels/*/sentinel-cr.yaml` to `resources:` in the top-level `kustomization.yaml` (use a glob if the tool supports it, else list the paths explicitly)."
- **Helm chart repo, glue missing:** "Add a `templates/sentinels.yaml` that includes each `sentinels/*/sentinel-cr.yaml` as a raw manifest (via `{{ .Files.Get }}` + `range`)."
- **ApplicationSet repo, glue missing:** "Add a matrix generator whose second dimension globs `sentinels/*/`, or add a per-Sentinel Application resource."
- **No pipeline detected at all:** "This repo appears not to have an obvious deploy pipeline. If deploys happen by hand or by a script, add `kubectl apply -f sentinels/*/sentinel-cr.yaml` to that path. Talk to whoever owns this service's deploy."

If deploy glue **was** found matching this Sentinel, skip the one-time-addition text and just say: "Glue detected in `<file>` — the next `git push` should pick this up automatically."

### 6.3 Verify

Print exactly:

```
kubectl get sentinel <metadata.name> -n <metadata.namespace>
kubectl describe sentinel <metadata.name> -n <metadata.namespace>
kubectl logs -l 'sentinels.cardinalhq.io/sentinel=<metadata.name>' -n <metadata.namespace> --tail=200
```

Tell the user: "Give ArgoCD (or your deploy pipeline) a minute to sync, then the controller a few seconds to reconcile. `status.phase` will move Pending → Reconciling → Running. Findings appear in pod logs via the `stdout` sink — grep the logs for `[dag]` to watch node-by-node execution. If `status.conditions` shows `CapabilitiesBound=False` with reason `MissingCapabilityProvider`, a capability the Sentinel directory declares under `spec.capabilities.required[]` has no matching entry in the CR's `spec.capabilities[]`; the message names the first missing id."

## What NOT to do

- **Do NOT `git push`.** Only print the command.
- **Do NOT `git commit`.** Only print the command.
- **Do NOT create the k8s namespace.** Only tell the user it must exist.
- **Do NOT create the Secrets** referenced in `spec.capabilities[]`. Only tell the user they must exist, with the key names the controller reads (`endpoint`, `token`).
- **Do NOT write fixture files.** If the user picks `provider: fixture`, tell them which files `fixtures/` needs (4.5.1) and let them supply the contents. Inventing a tool response produces a Sentinel that "passes" against fiction.
- **Do NOT offer a provider or sink that is not registered.** Today that means providers `mcp` and `fixture`, and sink `stdout`. Anything else fails at run time, after the DAG has already burned its wall clock.
- **Do NOT install the sentinel-controller.** That is a one-time platform action, not a per-Sentinel step.
- **Do NOT add the deploy-glue programmatically.** Only print the matching instructions from Stage 6.2 and let the user decide. Deploy pipelines vary too much repo-to-repo.
- **Do NOT invent inputs, capabilities, or sinks the Sentinel didn't declare.** The CR is a binding layer, not a redesign.

## Success criterion

A `sentinel-cr.yaml` exists next to the Sentinel directory, references the correct repo URL / ref / path (verified via `git`), and binds **every** id in the Sentinel's `spec.capabilities.required[]` to a registered provider — `mcp` with an endpoint + token Secret pair, or `fixture` with the fixture files named and the `execution.allowFixtures` requirement stated. The directory also has a `deployment.yaml` carrying `schemaVersion`, `kind`, `runtime` and a `findingsRouting` rule — authored in Stage 3 if `/mechanize` did not emit one — without which the pod dies at start with a schema error naming `runtime`. It lists the `stdout` sink and pins an executor image that is both multi-arch and new enough to contain every provider it binds (`v0.1.3`+ for `mcp`). The user knows which of their findings will be live and which will be replayed. They have the exact three commands they need to ship it (`add`, `commit`, `push`), and — if the repo needs deploy-glue — the exact addition they need to make once, matched to the deploy shape their repo actually uses.
