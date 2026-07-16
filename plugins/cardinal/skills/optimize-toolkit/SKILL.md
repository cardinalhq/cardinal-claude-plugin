---
description: Mine this engineer's own session telemetry for capability-fit recommendations (extract a new sub-agent, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree.
disable-model-invocation: true
---

# /cardinal:optimize-toolkit

A **per-user, past-informed, future-effective** optimizer — not a
session-local one. It looks at your own last 30 days of sessions
(recurrence across ≥5 is the admission bar the server applies before a
candidate ever reaches this skill), pitches the top few candidates with
their evidence, and — only on your explicit confirmation — writes the
accepted artifact to this working tree. **Anything written takes effect
next session**, not this one: agents load at session start, so accepting
a candidate here will not change what happens for the rest of this
conversation.

This burns your own session tokens to run. It is built to be cheap by
construction: the server (conductor's maestro, via the `cardinal` MCP
server) authors every number and every artifact body — this skill never
re-analyzes your repo, never invents a savings figure, and never spawns
a sub-agent to do its own investigating. If a candidate needs more than
cosmetic adaptation (renaming, trimming a tool allowlist), that is a
server-side gap to report, not something to improvise here.

## Before you start

This skill orchestrates eight `outcomes__*` tools served by the
`cardinal` MCP server (see `/cardinal:connect` — same server, same
consent). They are documented in conductor's
`docs/specs/optimize-toolkit-mcp-tools.md`:

1. `outcomes__my_turn_pattern`
2. `outcomes__my_toolkit_adoption`
3. `outcomes__cluster_spawns`
4. `outcomes__org_offered_tiers`
5. `outcomes__estimate_savings`
6. `outcomes__generate_agent_spec`
7. `outcomes__mark`

`outcomes__my_recent_spawns` also exists on the same server — it's a
raw spawn history for ad-hoc debugging, not part of this flow. Reach
for it only if the user explicitly asks "what did I spawn recently?".

**Check your available tools before doing anything else.** As of this
writing, the maestro routes behind these tools are live, but the
mcp-gateway `outcomes/` package that exposes them as *callable MCP
tools* is a separate, not-yet-shipped follow-up. If none of the
`outcomes__*` tools appear in your tool list (they'd show under the
`cardinal` MCP server, alongside whatever other integrations this org
has configured), say so plainly and stop:

> "The optimize-toolkit tools aren't wired into this org's Cardinal MCP
> server yet — that's a known rollout gap, not a problem with your
> connection. Nothing to do here yet."

Do not attempt a substitute analysis (no reading transcripts, no
grepping local logs, no falling back to "let me look at your repo
instead"). This mirrors the spec's silent-failure rule: MCP unreachable
or empty candidates → one line, stop, no retries.

## How you (Claude) should run this

Budget: usually 5–7 tool calls total, ≤10 in the worst case. Stay inside
it — this is a "thin skill" by design; the intelligence is server-side.

### 1. Open — situational-awareness bundle (2 calls)

Call `outcomes__my_turn_pattern` and `outcomes__my_toolkit_adoption`,
both with `window: "30d"`. From the two responses, compose a short
(≤6 line) opening paragraph before mentioning any candidate:

- State the evidence window explicitly ("based on your last 30 days of
  sessions") — never let the opening imply "this session."
- Name the caller's top 1–2 model-mix rows by cost from
  `my_turn_pattern.models`.
- Name the toolkit-adoption headline the first candidate will target,
  e.g. "42 Explore spawns / 480k tokens on opus in the last 30 days"
  (from `my_toolkit_adoption.agents` or `.skills`).
- **No usable evidence, stop before any ratio math.** If
  `my_toolkit_adoption.coverage.sessions_scanned == 0`, or
  `my_turn_pattern.turns_total` is 0, there is nothing to compute a
  coverage ratio from — say "no optimization candidates right now, not
  enough session history yet" and stop. Never divide by
  `sessions_scanned` before this check passes.
- **Coverage caveat.** The 8-tool contract doesn't ship one unified
  `enriched_share` field (an earlier draft of the skill spec assumed a
  single bundle response with that shape; the shipped tool contract
  splits it per-tool instead — treat the per-tool `coverage` objects as
  authoritative). Use `my_toolkit_adoption.coverage.sessions_with_tier_attribution
  / coverage.sessions_scanned` as the enrichment-coverage proxy —
  `sessions_with_tier_attribution` is the subset where `tok_by_model`
  is actually populated (v0.12.x fields present), which is what this
  skill's savings math depends on; it's the closest available stand-in
  for the `enriched_share` the original spec draft described (see
  `optimize-toolkit-mcp-tools.md:214-218`). Treat
  `sessions_with_toolkit_data / sessions_scanned` as a **separate,
  secondary** check — "does the plugin see this user at all" — worth
  mentioning if it diverges sharply from the tier-attribution ratio
  (e.g. a very-online-but-pre-v0.12 user), but don't gate the caveat on
  it: a user can score 1.0 on toolkit-data presence while still being
  at 0 on tier attribution, and it's the latter that governs whether a
  savings number is grounded. When the tier-attribution ratio is under
  0.5, prepend a one-line caveat: "evidence from N/M of your sessions
  in the last 30 days — coverage will climb as you keep working on a
  current plugin version." Also check
  `my_turn_pattern.coverage.plugin_versions_seen` — if it shows a stale
  version for recent turns, mention the plugin-version-drift possibility
  and suggest a Claude Code restart.

If `my_turn_pattern.turns_total` or `my_toolkit_adoption` shows nothing
usable (near-zero coverage), say so and stop — do not manufacture a
pitch from thin data.

### 2. Discover (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"` (defaults:
`min_jaccard: 0.4`, `min_cluster_size: 3` — leave as-is unless the
conversation gives you a reason to tune them). This tool does not judge
which clusters are worth pitching — **that's your job, adaptively, in
this conversation.** Rank by `total_cost_usd` (fall back to
`total_tokens` when cost is null) and recurrence; drop anything with
`recurrence < 3` even if it slipped through, and prefer clusters with a
higher `with_description_share` in `coverage` (clustering is weaker
without descriptions).

Take the top **K = 3** clusters forward. If fewer than 3 clear clusters
exist, present fewer — do not pad with weak candidates to hit the
number.

### 3. Score (1 + up to K calls)

Call `outcomes__org_offered_tiers` once — this tells you the org's
actual `cheap` and `reasoning` model tiers. **Never suggest a model the
org isn't offered**; if a tier is `null`, that door is closed for this
org.

For each of the top-K clusters, call `outcomes__estimate_savings` with
the cluster's token/model data and a `target_tier`. Choosing
`target_tier` and the eventual `kind` (next step) is this skill's
judgment call — the tools score a proposal, they don't propose one for
you:

- Cluster's `current_model` already matches the org's `cheap` tier →
  there's no tiering headroom; this cluster is a candidate for
  `extract` (mint a reusable capability) or `consolidate`
  (near-duplicate of something that already exists), not `pin`/
  `downgrade`. **Still call `estimate_savings` with `target_tier:
  "cheap"`** — the savings will be ~0 honestly, but the call keeps
  `target_tier` grounded for the compose step that follows, and
  confirms there's truly no delta rather than assuming it. Never skip
  the call.
- Cluster runs on a `reasoning`-tier or unresolved model and the work
  looks mechanical (tight `tool_signature` — use `jaccard_within` as
  the proxy, `≥ 0.6` is a defensible starting threshold;
  `TODO(reviewer)` on the exact value) → `target_tier: "cheap"`, kind
  candidate `pin` or `downgrade`.
- Cluster's `tool_signature` looks like it's duplicating an existing
  named capability you can see in `my_toolkit_adoption` → kind
  candidate `adopt` (stop minting the inline pattern, use the existing
  one) or `swap` (existing capability is the wrong shape/model, needs
  replacing).
- Two or more clusters look like near-duplicates of each other →
  `consolidate`.
- **D5 outcome gate**: `adopt`/`swap`/`pin`/`downgrade` require the
  cohort outcome signal to be present for this candidate. Pick your
  best-guess `kind` here from the signals above — the gate itself is
  validated authoritatively after `outcomes__generate_agent_spec` in
  step 5 (Compose), which returns `outcome_backed` / `kind_supported`
  directly (cardinalhq/conductor#1322). No inference from errors or
  body shape needed.
- If nothing in the cluster fits any artifact-bearing kind, it's a
  `gap` — a signal worth naming in conversation ("you keep doing X by
  hand; there's no fitting capability for it yet") with **no artifact
  and no `generate_agent_spec` call**.

Read `estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below before you say a dollar
figure out loud.

### 4. Present (no tool call)

One candidate at a time, top-K by headline savings, each with:

- The evidence summary in plain language (not a raw JSON dump).
- The `matching_sessions` slice if present, referenced inline ("this
  would have covered your session on Jul 6"), not as a bare count.
- The full artifact body you'd write (from step 5 — call
  `outcomes__generate_agent_spec` before presenting, not after
  confirming, since "full artifact before any confirmation question" is
  the contract).
- The savings figure, honestly caveated per placeholder rules.
- An explicit confirmation question. Do not proceed to writing on
  silence, "not now," or a topic change — see **Marking honestly**.

### 5. Compose (1 call per candidate you present)

Call `outcomes__generate_agent_spec` with the cluster's id, the chosen
`target_tier`, and the chosen `kind` (one of `extract`, `pin`,
`downgrade`, `adopt`, `swap`, `consolidate` — never `gap`, which has no
artifact). This is a **server-authored** artifact — you do not compose
the markdown; you present it and, on confirmation, write it verbatim
(cosmetic adaptation only: renaming to fit repo conventions, trimming an
obviously-irrelevant tool from the allowlist).

**D5 outcome gate — read directly.** The response carries
`outcome_backed: boolean` and `kind_supported: boolean`
(cardinalhq/conductor#1322). Read both before presenting the candidate:

- If `kind_supported === false`, fall back to kind `extract` and tell
  the user plainly why: "this recommendation kind needs cohort outcome
  data your sessions don't have populated yet — falling back to
  `extract`."
- Never present or write a `pin`/`downgrade`/`adopt`/`swap` artifact
  when `outcome_backed === false` — this is a hard gate.
- This is independent of the `warning: "artifact_kind_not_yet_specialized"`
  field described below in **Known gap** — that flags body
  specialization (FU-1), not D5 outcome validity. Both can appear on
  the same response.

**Known gap — be honest about it.** `outcomes__generate_agent_spec`
today emits the same shape of markdown body regardless of `kind`
(flagged in the harvester review as FU-1, not yet closed as of this
writing). That means a `pin` or `adopt` recommendation may come back
reading like a freshly-minted agent even though nothing about the role
is actually new. **Say what kind you're rendering and where the target
file would go even when the body itself is generic** — do not let a
generic body imply the recommendation is less grounded than it is (the
evidence and savings numbers are still real; only the prose body is
currently kind-blind). Per kind:

| kind | what it means | target file | what to do given the generic-body gap |
|---|---|---|---|
| `extract` | mint a genuinely new capability from a recurring inline cluster | new file: `.claude/agents/<suggested_name>.md` | Body is expected to be generic-shaped here — this is the one kind `generate_agent_spec` was designed for. Write as-is. |
| `pin` | keep the existing capability, change only its model tier | existing `.claude/agents/<name>.md` if you can identify it from the conversation/repo; otherwise `.claude/agents/<suggested_name>.md` as a fallback | Tell the user plainly: "this is a `pin` — the meaningful change is the model line (`model: <target_model_id>`), not a new role description. I'd normally just edit that one field on your existing agent file rather than replace it with this generic body; let me know which existing file this should target." Prefer a minimal edit over a full-body overwrite when you can locate the existing file. |
| `downgrade` | same as `pin` but framed as re-tiering an over-qualified capability down | same as `pin` | Same honesty note as `pin`. |
| `adopt` | stop minting this pattern inline; an existing capability already covers it | usually **no new file** | Say so directly: "this is an `adopt` — no new file is needed, `<existing capability>` already covers this. I'll skip writing anything; the actionable part is reaching for it next time." Only write something (e.g., a short note) if the user asks for a durable reminder. |
| `swap` | replace a capability with a better-fit existing one | the **existing** capability's file, if identifiable | Same posture as `adopt` — this is a pointer to something that already exists, not new content. Don't write a new agent file under this kind without the user explicitly asking for one. |
| `consolidate` | merge near-duplicate capabilities | the files being merged, once the user identifies them | `generate_agent_spec` doesn't return which files are duplicates — it only scores the cluster. Present the opportunity conversationally; don't attempt to auto-locate or auto-merge files. Only write once the user tells you which files are involved. |
| `gap` | no fitting capability exists | none — no artifact | Never call `generate_agent_spec` for this kind. Present as a signal only. |

`TODO(reviewer)`: this table is this skill's interpretation of how to
stay honest around the FU-1 generic-body gap — confirm it against
product intent once `generate_agent_spec` becomes kind-aware, and
simplify accordingly.

### 6. Write (only on explicit confirmation)

**Validate the target-file basename before anything else.** The
`suggested_name` field is server-authored so this is defence in depth,
but names are used as path segments — reject if `suggested_name` (a)
contains `/` or `\`, (b) contains `..`, or (c) doesn't match
kebab-case `^[a-z][a-z0-9-]*$`. On rejection, surface the value
verbatim to the user with the specific reason and stop — do not
attempt a rewrite or a slugification pass.

**Dry-run first, always.** Before writing anything, show:

- The exact target file path (from the table above).
- Whether it's a new file or an edit to an existing one, and if an
  edit, which fields change (ideally just the model line for
  `pin`/`downgrade`).
- The full artifact body that would land.
- A plain confirmation question ("write this to `.claude/agents/
  <name>.md`? yes/no").

Only write after an explicit "yes"-shaped answer in the conversation.
No write on silence, hedging, or topic change. After writing, tell the
user this **takes effect next session** (agents load at session start;
there is no live-registration channel for claude-code) — never imply
the current conversation just changed. Consent, revert, and
distribution are git: the artifact lands in the working tree like any
other change, reviewed in the diff, reverted with `git checkout --`,
shared via the repo. There is no separate revoke/sync mechanism to
explain.

### 7. Mark (1 call per candidate you presented)

**Exactly one `mark` call per candidate you showed, carrying its
terminal status.** Not one call per state transition, not a stream of
"presented → accepted" updates — the ledger reads the status as the
single terminal outcome. Don't double-mark. Pick from:

- `status: "accepted"` — confirmed and written.
- `status: "dismissed"` — **explicit refusal only** ("no," "don't want
  this"). Ask one short follow-up ("what didn't fit?") and forward the
  answer verbatim as `reason` (cap ~200 chars; do not paraphrase or
  classify it yourself — the raw text is the learning signal).
- `status: "presented"` — shown, no decision either way ("not now,"
  topic change, session ends without an answer). **Never auto-dismiss
  on non-confirmation** — hesitation must not read as a refusal;
  dismissals are sticky server-side (2× pooled-cost reopen) and
  poisoning that with a false dismissal is worse than a missed mark.

Use `action: { kind: "cluster", cluster_id, proposed_kind }` — these are
live cluster-derived decisions, not legacy ledger rows. Mark is
best-effort: if the call fails, don't error the conversation over it —
the artifact write (or its absence) is the real outcome; the ledger is
measurement, not the source of truth.

## Failure handling for non-`mark` tools

`mark` is the one tool that follows the silent-log rule above — every
other tool is on the hard-stop rule. If any of `my_turn_pattern`,
`my_toolkit_adoption`, `cluster_spawns`, `org_offered_tiers`,
`estimate_savings`, or `generate_agent_spec` returns `503`
(lakerunner-not-configured), `400` (invalid body), or an empty result
set where the flow depends on at least one row, **surface the error
verbatim to the user, stop the flow, do not retry**. An empty
`cluster_spawns` result means "no clusters cleared the recurrence
floor — nothing to pitch," not "try again with looser thresholds."

## Placeholder savings, honestly

Every `outcomes__estimate_savings` response carries fields that exist
specifically so you don't overstate a number:

- `estimate: "no_cohort_catalog_only"` means there's no cohort of other
  engineers/orgs to compare against yet — the figure is **catalog
  pricing math only**, not validated against how the tier actually
  performs for this kind of work. Say this out loud: "this is a
  catalog-only estimate — I don't have cohort data yet to confirm the
  cheaper tier holds up for this pattern; treat it as a ceiling, not a
  promise." Do not drop the caveat just because the number is
  attractive.
- `assumptions.placeholder_output_ratio` / `placeholder_cache_ratio` set
  to `true` mean the estimate fell back to typical ratios because the
  cluster didn't carry per-component token data. Say "estimated within
  a wide band" rather than quoting a bare point figure when either flag
  is set.
- When `current_cost_usd` is `null` (current model unpriced), don't
  imply a before/after delta — state the projected cost alone.

None of this blocks presenting the candidate — it changes how
confidently you say the number, not whether you say it.

## What not to do

- Don't re-analyze the repo beyond confirming a target file path exists
  or locating the existing file a `pin`/`downgrade`/`swap` targets.
- Don't spawn a sub-agent to do independent investigation — the server
  already computed everything you need.
- Don't invent a cohort comparison when a tool response says there
  isn't one.
- Don't write anything without an explicit "yes" in this conversation.
- Don't auto-invoke yourself — this skill is command-only
  (`disable-model-invocation: true`); only run it when the user types
  `/cardinal:optimize-toolkit`.
- Don't exceed the ~10-call budget; if you're reaching for more calls
  than that, stop and say the pipeline needs more than a thin skill can
  responsibly do here.
