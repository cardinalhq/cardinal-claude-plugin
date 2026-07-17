---
description: Mine this engineer's own session telemetry for capability-fit recommendations (extract a new sub-agent, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree.
disable-model-invocation: true
---

# /cardinal:optimize-toolkit

A **per-user, past-informed, future-effective** optimizer — not a
session-local one. It looks at your own last 30 days of sessions,
picks the top few recurring inline-work patterns worth acting on,
authors the artifact itself grounded in your actual toolkit, and — only
on your explicit confirmation — writes the accepted artifact to this
working tree. **Anything written takes effect next session**, not this
one: agents load at session start, so accepting a candidate here will
not change what happens for the rest of this conversation.

This burns your own session tokens to run. The server (conductor's
maestro, via the `cardinal` MCP server) provides evidence — clusters,
model mix, toolkit adoption, tier pricing, priced-savings math. **You
(Claude) do the authorship** from first principles: pick the
recommendation kind by reasoning about the evidence, then write the
artifact grounded in this user's real capabilities, real tool
signatures, real cluster labels. There is no server-side artifact
template — the artifact is composed here, per invocation, and passed
back to the ledger verbatim on `mark`.

## Before you start

This skill orchestrates six `outcomes__*` tools served by the
`cardinal` MCP server (see `/cardinal:connect` — same server, same
consent). They are documented in conductor's
`docs/specs/optimize-toolkit-mcp-tools.md`:

1. `outcomes__my_turn_pattern`
2. `outcomes__my_toolkit_adoption`
3. `outcomes__cluster_spawns`
4. `outcomes__org_offered_tiers`
5. `outcomes__estimate_savings`
6. `outcomes__mark`

`outcomes__my_recent_spawns` also exists on the same server — it's a
raw spawn history for ad-hoc debugging, not part of this flow. Reach
for it only if the user explicitly asks "what did I spawn recently?".

**Check your available tools before doing anything else.** If none of
the `outcomes__*` tools appear in your tool list (they'd show under the
`cardinal` MCP server, alongside whatever other integrations this org
has configured), say so plainly and stop:

> "The optimize-toolkit tools aren't wired into this org's Cardinal MCP
> server yet — that's a known rollout gap, not a problem with your
> connection. Nothing to do here yet."

Do not attempt a substitute analysis (no reading transcripts, no
grepping local logs, no falling back to "let me look at your repo
instead"). MCP unreachable or empty candidates → one line, stop, no
retries.

## How you (Claude) should run this

Budget: usually 5–7 tool calls total, ≤10 in the worst case. You'll
also read a handful of files from the working tree to ground kind
picking and authorship — that's fine, keep it targeted.

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
  enough session history yet" and stop.
- **Coverage caveat.** Use `my_toolkit_adoption.coverage.sessions_with_tier_attribution
  / coverage.sessions_scanned` as the enrichment-coverage proxy. When
  that ratio is under 0.5, prepend a one-line caveat: "evidence from
  N/M of your sessions in the last 30 days — coverage will climb as
  you keep working on a current plugin version." Also check
  `my_turn_pattern.coverage.plugin_versions_seen` — if it shows a stale
  version for recent turns, mention the plugin-version-drift
  possibility and suggest a Claude Code restart.

### 2. Discover — pull raw spawns (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"`, **`min_jaccard: 0.99`**,
**`min_cluster_size: 1`**. Those thresholds effectively disable the server's
built-in token-Jaccard clustering — each spawn returns as its own single-
member "cluster", giving you the raw spawn population (label + session_id +
tool_signature + tokens + model). You do the actual clustering client-side
in the next step, because the server's token-Jaccard is too coarse for
semantically-similar-but-token-diverse labels ("Trace polly cwd flow" and
"Map maestro sites/org API" are both investigation-shape but share zero
content tokens).

Note the `coverage.with_description_share` — if it's under 0.5, mention it
in the caveat you already added in step 1 (older sessions from pre-v0.12
plugin versions don't emit `subagent_description`; it's legacy drift, not
a per-agent gap).

### 3. Reduce + semantically cluster (Bash + LLM pass)

**Stage 3a — mechanical reduction (Bash).** Save the cluster_spawns
response to a temp file and pipe it through the reducer that ships
alongside this SKILL.md. The reducer collapses N-identical labels,
groups by first content-word (verb), sub-groups by dominant-tool
signature, and collapses same-session bursts. It turns 100+ raw spawn
records into ~15–30 sub-clusters — a small enough set for the semantic
pass in 3b to reason over without paging in the whole raw population.

```bash
# Find the reducer that shipped with this skill. Portable across
# adapters + install locations.
REDUCER=$(find ~/.claude/plugins ~/.claude/skills . \
  -name reduce_spawns.py -path '*optimize-toolkit*' 2>/dev/null | head -1)
[ -z "$REDUCER" ] && { echo "reduce_spawns.py not found"; exit 1; }

# Feed cluster_spawns response as JSON on stdin; get verb-bucketed JSON out.
python3 "$REDUCER" < /tmp/spawns_raw.json > /tmp/spawns_reduced.json
cat /tmp/spawns_reduced.json
```

Output shape: `{ input_spawns_raw, zero_signal_spawns, sub_cluster_count,
reduced_rows: [{verb, tool_shape, spawn_count, zero_signal_count,
unique_labels, top_labels[:8], tokens_total, session_count,
burst_count, sample_top_by_tokens}] }`. Rows come sorted by
`tokens_total` descending.

**Zero-signal rows are retained, not dropped.** A row where
`zero_signal_count == spawn_count` has no tokens/model on any member
(pre-enrichment sessions or tracker entries) — `tokens_total` reads 0
and `tool_shape` reads `<empty>` for these, so don't use them for
cost/tier math (adopt-savings, downgrade share, pin thresholds). But
their `top_labels` are real, often bespoke, self-narrated Task
descriptions — exactly the evidence the contrast-pair mechanic below
needs. Don't filter these rows out of your own reasoning just because
`tokens_total` is 0.

**Stage 3b — semantic cluster (your reasoning, in-context).** Read the
reducer's output and group verb buckets into meta-clusters by intent, not
by surface tokens. Verb buckets like `code`/`silent-failure`/`silent`/
`test`/`comment`/`type`/`review`/`independent`/`fresh-eyes`/`general` all
fold into one **Code Review** meta-cluster; `trace`/`research`/`investigate`
/`find`/`explore`/`map`/`inventory`/`reconcile` fold into one **Research
& Investigation** meta-cluster. Expected meta-clusters for a typical
engineer include: **Code Review**, **Research & Investigation**,
**Migration / Implementation**, **Testing & Validation**, **Planning /
Organization**. Not every meta-cluster surfaces for every user; only
name the ones that clear a real token/recurrence floor from the data
in front of you.

The semantic pass is **your judgment** — no server-side taxonomy. Use
the reducer's rich per-bucket evidence (top_labels, session_count,
tool_shape) to justify each grouping. A verb bucket that
doesn't cleanly fit any meta-cluster stays as its own single-bucket
meta-cluster; don't force-fit for symmetry.

Take the top **K = 3** meta-clusters by `tokens_total`. If fewer than 3
clear meta-clusters exist, present fewer — do not pad.

**Known coverage gap.** `cluster_spawns` only covers Task-tool subagent
spawns. Slash commands (visible in `my_toolkit_adoption.commands`)
never show up here — a heavy user of `/make-release` won't see a
"Releases" cluster no matter how the reducer runs. If the user asks
about command patterns, say so plainly and point at
`my_toolkit_adoption.commands` for the raw counts; don't fabricate a
cluster from spawn data alone.

### 4. Ground yourself in the user's real toolkit (0–2 file reads per meta-cluster)

Before picking a kind for a meta-cluster, look at what the user actually
has. `my_toolkit_adoption` lists capabilities by name and usage, but
"a name in a usage map" is not the same as "a file on disk that will
still be there next session." For each of the top-K meta-clusters:

- If `my_toolkit_adoption` shows an agent/skill whose name looks like
  it might cover the meta-cluster's dominant verb buckets +
  `tool_shape`, `Read` the matching file under
  `.claude/agents/` or `.claude/skills/` (including plugin-installed
  paths under `~/.claude/plugins/`) to confirm the fit before
  recommending `adopt`. Bad `adopt` recommendations happen when the
  name matches but the actual scope doesn't.
- If you're considering `extract` (mint new), `Grep` under
  `.claude/agents/` for the meta-cluster's `tool_shape` — a
  similar-shape agent may already exist under a name your
  `my_toolkit_adoption` scan didn't surface.
- If you're considering `pin`/`downgrade`, locate the existing agent
  file so you can propose a **minimal edit** (change one `model:`
  line) rather than overwriting the whole file.

Keep this bounded — one or two focused `Read`/`Grep` calls per
meta-cluster, not a full-repo sweep. If the file isn't obviously
there, that's information ("adopt target's name matched but the file
isn't under `.claude/agents/` — treat as evidence to soften the adopt
pitch, or reframe as `gap`").

### 5. Pick the kind, from first principles

Call `outcomes__org_offered_tiers` once — this tells you the org's
actual `cheap` and `reasoning` model tiers. **Never suggest a model
the org isn't offered**; if a tier is `null`, that door is closed for
this org.

**Drill into sub-clusters — meta-clusters are framing, not the
recommendation unit.** The meta-cluster tells you the organizing shape
of an engineer's work (Research is heavy, Code Review is heavy); the
actionable play lives one level deeper, at the sub-cluster level: the
individual verb-bucket from the reducer with its specific
`top_labels`, `tool_shape`, `sample_top_by_tokens[].model`,
`session_count`, and `burst_count`. Pitching at the meta-cluster level
alone produces truisms ("mechanical work should run on Haiku") — the
non-obvious plays only appear when you compare a sub-cluster's shape
against `my_toolkit_adoption`.

**Strongest pattern (empirically): toolkit-consistency adopt, found via
contrast pairs.** A contrast pair sets a named agent's aggregate usage
(from `my_toolkit_adoption`, the "routed" side) against a reducer
sub-cluster whose labels credibly describe the same domain but never
name that agent (the "bypassed" side). Do **not** look for the
contrast inside a single spawn's `tool_signature` — an `Agent`/`Skill`
call there reflects the *parent* turn's routing decision, not the
child subagent's own trace, and `tool_shape` (top-2 tools by
frequency) usually buries a lone `Agent`/`Skill` call under `Bash`/
`Read` noise anyway. Cross-data-source contrast is the mechanic that
actually surfaces evidence:

1. From `my_toolkit_adoption.agents`, keep entries whose key is
   namespaced (contains `:`, e.g. `pr-review-toolkit:code-reviewer` —
   built-ins like `Explore`/`general-purpose`/`Plan` aren't a "domain"
   to bypass, they're the generic fallback) and that clear **≥10
   spawns OR ≥1M `subtok`**. This is the routed side.
2. For each kept agent, build a small domain-vocabulary set from its
   `description:` frontmatter (`Read` the file you already located in
   step 4) — the content words in the "use this agent when..."
   sentence, not just the slug. Reading the real description beats
   guessing from the hyphenated name; a name like `pr-test-analyzer`
   undersells that its domain also covers "coverage" and "regression."
3. Walk the reducer's `reduced_rows` — **including rows where
   `zero_signal_count == spawn_count`** (v4 of `reduce_spawns.py`
   retains these; they carry no tokens/model but their `top_labels`
   are real evidence, and labels like "General code review" / "Silent
   failure hunt" / "Test coverage review" / "Type design review" are
   often the *strongest* signal precisely because they're bespoke,
   self-narrated Task descriptions rather than toolkit boilerplate).
   Flag a row as a bypass candidate when its `top_labels` share ≥2
   content words with a kept agent's domain vocabulary.
4. Require the candidate to clear the same bar a one-off wouldn't:
   **≥2 spawns**, same session or sessions within the window you're
   already scanning. Two or more label-only reviews fired back-to-back
   in one session (check `at` timestamps — a burst under a few minutes
   is a giveaway) is a much stronger tell than a single spawn.
   Bonus confidence: if the same verb-phrase recurs across sessions
   with only the trailing subject varying ("Code review Phase A" /
   "Code review site-config-v2" / "General code review") — that's a
   hand-rolled loop the user re-invents each time, not a one-off.
5. State the caveat honestly when you present this: `cluster_spawns`
   never exposes `subagent_type` on a member (only
   `session_id`/`at`/`subagent_description`/`subagent_model`/
   `tokens_total`), so you cannot prove the bypassed spawns did NOT
   route through the named agent — only that their labels don't
   mention it and their shape/cadence reads as ad hoc. Pitch it as
   "these don't reference `<agent>` and look hand-rolled" rather than
   "you didn't use `<agent>`."

Once you have a pair, the play is `adopt` — the user has the tool,
they're inconsistently reaching for it.

**Tie-break — adopt beats downgrade when both fire.** If a sub-cluster
is both `adopt`-covered (an existing agent handles this shape) AND
downgrade-shaped (running on reasoning tier with a mechanical
tool_signature), pitch `adopt` alone. The existing agent's own model
config is a separate concern; routing the work to the agent captures
the primary win. Do not double-recommend.

For each of the top-K meta-clusters, reason about kind from the
evidence in front of you (the semantic cluster label, its member verb
buckets, its dominant tool shapes, its token magnitude, and the
`my_toolkit_adoption` match you just grounded). There is no server-side kind gate — you pick, you
justify. The five buildable kinds and one signal-only kind:

- **`adopt`** — the cluster's `tool_signature` and label overlap an
  existing capability you saw in step 3. Recommend the user reach for
  the existing thing consistently, no new file. Softer signal: the
  user's own `my_toolkit_adoption` shows the target with meaningful
  usage already (they know it exists; the miss is inconsistency, not
  awareness).
- **`swap`** — cluster overlaps an existing capability, but that
  existing capability is the wrong shape or wrong model for this work.
  Recommend replacing it. Harder to justify than `adopt`; state the
  reason ("existing agent is pinned to opus but the tool_signature is
  mechanical — swap it for a cheap-tier variant").
- **`pin`** — cluster runs on a mix of tiers with the org's cheap tier
  already carrying meaningful share (say, ≥30% of the cluster's
  `subagent_model` occurrences) and no evidence the reasoning tier is
  load-bearing. Recommend pinning the existing capability to the
  cheap tier. Requires you to have located an existing file to edit.
- **`downgrade`** — cluster runs predominantly on the reasoning tier
  (say, ≥50% share) but the `tool_signature` looks mechanical
  (`jaccard_within` ≥ 0.6, small tool set, no reasoning-heavy tools
  like WebSearch/deep-analysis). Recommend re-tiering the existing
  capability down. Same file-locate requirement as `pin`.
- **`extract`** — a recurring inline pattern with no existing named
  capability to point at. Mint a new sub-agent. This is the one kind
  where you author a full new `.claude/agents/<name>.md` file from
  scratch. Requires you to name the capability and derive its
  `tools:` allowlist from the cluster's `tool_signature`.
- **`consolidate`** — two clusters look like the same underlying job
  under two different labels (or two existing capabilities do). No
  new file; recommend merging. Present conversationally, don't
  auto-locate the files.
- **`gap`** (signal only, no artifact) — cluster is real recurring
  work, but you cannot pick a kind honestly (no adopt target, extract
  would be premature, `tool_signature` too diffuse to justify a
  minted agent). Say so plainly, no artifact, no confirmation
  question, no `estimate_savings` call for this candidate.

Thresholds above (30% cheap share, 50% reasoning share, ≥0.6
jaccard_within) are rules of thumb, not gates. Adjust when the
cluster's specifics clearly warrant — the point is to reason from the
evidence, not clear a fixed bar.

### 6. Estimate savings (1 call per non-`gap` candidate)

For each candidate that isn't `gap`, call `outcomes__estimate_savings`
with the cluster's `total_tokens`, per-component tokens if you can
derive them from members' fields, `current_model` (the dominant
`subagent_model` across cluster members), and `target_tier`
(`"cheap"` for `pin`/`downgrade`/`extract`; `"cheap"` also for
`adopt`/`swap` if the pointed-to capability runs cheaper). Read
`estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below before you say a dollar
figure out loud.

### 7. Author + Present (no tool call; you write the artifact)

For each candidate, author the artifact yourself from first principles
before asking for confirmation. The user will see the full artifact,
not a description of one. Per kind:

**`extract` — new file at `.claude/agents/<name>.md`.** You author:

- `name`: kebab-case, `^[a-z][a-z0-9-]*$`, derived from the cluster's
  `representative_label`. Short enough to type. If a same-name file
  already exists in `.claude/agents/`, disambiguate rather than
  overwrite.
- `description`: one sentence stating what work this handles and when
  Claude should delegate to it. Ground the sentence in the cluster's
  actual observed work — reference the tool_signature's dominant
  tools and the label's phrasing. Not a template; you write it.
- `tools`: derived from the cluster's `tool_signature`. Include the
  tools that account for the mass of calls, drop the long tail. If
  the tool_signature is empty (no per-call breakdown), omit the
  `tools:` line entirely (the subagent inherits the main thread's
  toolset).
- `model`: the `target_model_id` from `org_offered_tiers` for the
  tier you picked in step 5.
- Body: a short (≤10 line) system prompt describing the role.
  Grounded in this cluster's specifics — what request shape the
  subagent handles, what to report back, when to escalate.
  Absolutely not a template like "handles recurring work" — write it
  for this specific pattern.

**`pin`/`downgrade` — edit an existing file at `.claude/agents/<name>.md`.**
You already located the file in step 3. The meaningful change is one
frontmatter line: `model: <target_model_id>`. Author the dry-run as a
one-field edit, not a full-file replacement. If you couldn't locate
the existing file, say so and reframe as a conversational nudge
("your `<capability>` agent shows heavy reasoning-tier usage; consider
pinning it to cheap — I couldn't find its file to edit; where should
this go?").

**`adopt` — usually no file.** Author a plain-language recommendation:
"stop spawning this inline pattern; your existing `<capability>`
already handles it — I saw N sessions where it would have applied."
No confirmation-to-write question; the actionable part is the user's
future behavior, not a diff. If the user asks for a durable
reminder, offer to write a short note somewhere they'd see it
(a CLAUDE.md line, for instance) — only on explicit ask.

**`swap` — edit or replace an existing file.** Author the dry-run as
the specific edit you'd propose: which file, which field(s) change,
what the new content is. Do not mint a brand-new file under `swap`
without the user explicitly asking for one.

**`consolidate` — no automated file work.** Present the two-candidate
overlap and ask the user which capability should absorb the other.
Do not attempt auto-locate or auto-merge.

**`gap` — no artifact.** State the pattern, name it as a signal.
Skip step 7 (Mark) — actually, do call `mark` with `status:
"presented"` and `proposed_kind: "gap"` for the ledger's sake, but
skip the write/confirmation loop.

**Present** the authored artifact (or the plain-language
recommendation) with:

- The evidence summary in plain language.
- The `matching_sessions` slice from the cluster if present,
  referenced inline ("this would have covered your session on Jul 6").
- The savings figure, honestly caveated per placeholder rules.
- A plain confirmation question ("write this to `.claude/agents/<name>.md`?
  yes/no"). One candidate at a time — don't stack.

### 8. Write (only on explicit confirmation)

**Validate the target-file basename before anything else.** Names are
used as path segments — reject if the name (a) contains `/` or `\`,
(b) contains `..`, or (c) doesn't match kebab-case
`^[a-z][a-z0-9-]*$`. On rejection, surface the value verbatim to the
user with the specific reason and stop.

Only write after an explicit "yes"-shaped answer in the conversation.
No write on silence, hedging, or topic change. After writing, tell
the user this **takes effect next session** (agents load at session
start; there is no live-registration channel for claude-code) — never
imply the current conversation just changed. Consent, revert, and
distribution are git: the artifact lands in the working tree like any
other change, reviewed in the diff, reverted with `git checkout --`,
shared via the repo.

### 9. Mark (1 call per candidate you presented)

**Exactly one `mark` call per candidate you showed, carrying its
terminal status and — when there is one — the artifact body you
authored.** Not one call per state transition, not a stream of
"presented → accepted" updates. Pick from:

- `status: "accepted"` — confirmed and written.
- `status: "dismissed"` — **explicit refusal only** ("no," "don't
  want this"). Ask one short follow-up ("what didn't fit?") and
  forward the answer verbatim as `reason` (cap ~200 chars).
- `status: "presented"` — shown, no decision either way ("not now,"
  topic change, session ends without an answer). **Never
  auto-dismiss on non-confirmation.** Dismissals are sticky
  server-side (2× pooled-cost reopen); a false dismissal is worse
  than a missed mark.

Use `action: { kind: "cluster", cluster_id, proposed_kind }`. When
the candidate is `extract`/`pin`/`downgrade`/`swap`/`consolidate` and
you authored an artifact, pass `agent_spec_md` (your authored body,
verbatim) and `est_savings_low_usd` / `est_savings_high_usd` (from
`estimate_savings`) so the ledger row isn't lossy. For `adopt` with
no file written, and for `gap`, omit those fields.

Mark is best-effort: if the call fails, don't error the conversation
over it — the artifact write (or its absence) is the real outcome;
the ledger is measurement, not the source of truth.

## Failure handling for non-`mark` tools

`mark` is the one tool that follows the silent-log rule above — every
other tool is on the hard-stop rule. If any of `my_turn_pattern`,
`my_toolkit_adoption`, `cluster_spawns`, `org_offered_tiers`, or
`estimate_savings` returns `503` (lakerunner-not-configured), `400`
(invalid body), or an empty result set where the flow depends on at
least one row, **surface the error verbatim to the user, stop the
flow, do not retry**. An empty `cluster_spawns` result means "no
clusters cleared the recurrence floor — nothing to pitch," not "try
again with looser thresholds."

## Placeholder savings, honestly

Every `outcomes__estimate_savings` response carries fields that exist
specifically so you don't overstate a number:

- `estimate: "no_cohort_catalog_only"` means there's no cohort of
  other engineers/orgs to compare against yet — the figure is
  **catalog pricing math only**, not validated against how the tier
  actually performs for this kind of work. Say this out loud: "this
  is a catalog-only estimate — I don't have cohort data yet to
  confirm the cheaper tier holds up for this pattern; treat it as a
  ceiling, not a promise." Do not drop the caveat just because the
  number is attractive.
- `assumptions.placeholder_output_ratio` / `placeholder_cache_ratio`
  set to `true` mean the estimate fell back to typical ratios because
  the cluster didn't carry per-component token data. Say "estimated
  within a wide band" rather than quoting a bare point figure when
  either flag is set.
- **`current_cost_usd: null` — dominant model not priced in this
  org's catalog.** If the sub-cluster's dominant model (e.g.,
  `claude-opus-4-8`) isn't in `org_offered_tiers.all`,
  `estimate_savings` returns `current_cost_usd: null` and
  `savings_high/low_usd: 0`. Do not quote a $ figure. Pitch the play
  on consistency or shape grounds ("mechanical tool-use pattern, better
  routed through <existing agent>") — the savings surface just isn't
  usable in this org for this model.

None of this blocks presenting the candidate — it changes how
confidently you say the number, not whether you say it.

## What not to do

- Don't spawn a sub-agent to do independent investigation — you
  already have the evidence you need from the six tools plus a
  couple of targeted local file reads.
- Don't invent a cohort comparison when a tool response says there
  isn't one.
- Don't write anything without an explicit "yes" in this
  conversation.
- Don't auto-invoke yourself — this skill is command-only
  (`disable-model-invocation: true`); only run it when the user types
  `/cardinal:optimize-toolkit`.
- Don't paper over a bad pick with a plausibly-worded artifact.
  Authoring from first principles means the artifact should read as
  specific to the cluster's actual pattern. If you find yourself
  writing template-shaped prose ("handles recurring work of this
  kind"), that's a signal the evidence isn't strong enough to
  recommend — reclassify as `gap` and say so.
- Don't exceed the ~10-call budget on `outcomes__*` tools; if you're
  reaching for more, stop and say the pipeline needs more than a
  thin skill can responsibly do here.
