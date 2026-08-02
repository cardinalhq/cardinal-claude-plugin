---
description: Install a Cardinal site (perch + optional POC Lakerunner) into a Kubernetes cluster you have access to, driven by the Cardinal control-plane API.
disable-model-invocation: true
---

# /cardinal:install-site

Stands up a Cardinal **site**: creates it in Maestro, installs the **perch**
operator into your Kubernetes cluster, optionally adds a POC **Lakerunner**, and
verifies both came up — all from this session.

Everything talks to the Cardinal API with your user-scoped `maestro:act` token
(minted by `/cardinal:connect`). The one step that touches your
cluster is a single `helm install`, and you approve the exact command first.

## Prerequisites

- You ran `/cardinal:connect` (this skill fails with
  `no_act_token` otherwise).
- `helm` and `kubectl` are on PATH, and your current kube-context points at the
  cluster you want to install into (`kubectl config current-context`).
- You are an **owner** of the Cardinal org you're installing for. The API
  enforces this — a non-owner token is refused server-side, not by this skill.

## How you (Claude) should run this

Drive `cardinal-install-site <subcommand>` via the Bash tool. **Every
subcommand prints one JSON object**; parse it and branch on `ok`. On `ok:false`,
read `error` + `hint` and tell the user in plain language — never dump raw JSON.

Do the steps in order, pausing for the user where noted. **Do not skip the
confirm gate before `install-perch`**, and **never print the install key** — the
bin handles it and redacts it; if you ever see a `psk_...` value, something is
wrong.

### 1–2. Pick the org

```
cardinal-install-site whoami
```

Lists `owner_orgs`. If empty, stop: the user isn't an owner of any org and can't
install a site (say so; mention `non_owner_count` if > 0). If one, use it. If
several, ask which.

### 3. Name the site

```
cardinal-install-site list-sites --org <orgId>
```

Show existing site names. Ask for a new name. If the name matches an existing
site, offer to **resume** it (skip create, go to step 6/7 against that siteId) or
pick another — don't just retry into a `name_taken` error.

### 4–5. Cluster + namespace

- Confirm the target cluster: run `kubectl config current-context` and show it.
  If it's not the intended cluster, have the user switch context and re-confirm.
- Namespace: suggest `cardinal`. Check it's free with
  `kubectl get ns cardinal` and re-prompt if it already exists. This one
  namespace is used for **both** the `helm -n` install and the site's workload
  namespace — the skill keeps them identical on purpose.

### 6. Create the site, then install perch (with a confirm gate)

```
cardinal-install-site create-site --org <orgId> --name <name> --namespace <ns>
```

Then show the exact command that will run against the cluster:

```
cardinal-install-site install-perch --org <orgId> --site <siteId> --namespace <ns> --dry-run
```

Print the `command` it returns and **ask the user to confirm** before executing.
On approval:

```
cardinal-install-site install-perch --org <orgId> --site <siteId> --namespace <ns>
```

If this fails (`helm_failed`), the site row still exists and the install key is
still recoverable — relay the `stderr`, let the user fix the cluster issue, and
re-run the same command. If they want to abandon, `delete-site`.

### 7. Wait for perch to check in

```
cardinal-install-site wait-perch --org <orgId> --site <siteId>
```

Blocks until perch phones home (up to 5 min). This is a **hard gate** for step 8
— don't proceed on a timeout; help debug the perch pod instead (the `hint` has
the kubectl commands).

### 8. Optionally add a POC Lakerunner

Ask if they want a POC Lakerunner (managed object store + Postgres, provisioned
in-cluster by perch — no external credentials).

```
cardinal-install-site add-lakerunner --org <orgId> --site <siteId> --name <name> --namespace <ns>
```

Common stops: `trial_not_eligible` / `contact_sales` (no license — relay it,
don't retry); `operator_not_phoned_home` (step 7 didn't actually complete). After
it returns, run `wait-perch` again so perch reconciles the new workload.

### 9. Verify + hand off

```
cardinal-install-site verify --org <orgId> --site <siteId>
cardinal-install-site connect-info --org <orgId> --site <siteId>
```

Show the user the site is up, then print the connect block from `connect-info`:
the login email, the `port_forward` command, and the `read_password` command.
Say plainly that Maestro never had the password — perch mints it in-cluster, and
`read_password` reads it from the Secret. Point them at the site in the Cardinal
UI to take it from there.

## Notes

- The token authenticates as `X-CardinalHQ-API-Key`; the bin adds `X-Org-Id`
  itself. You never construct HTTP calls directly — always go through the bin.
- If any call returns `unauthorized`, the token was revoked/expired: tell the
  user to re-run `/cardinal:connect`.
- This skill only ever mutates the cluster once (the approved `helm install`).
  Every verification is Maestro-side — perch pushes status back — so it works
  even if this session can't reach the cluster after the install.
