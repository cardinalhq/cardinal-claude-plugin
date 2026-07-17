---
description: Mine this engineer's own session telemetry for capability-fit recommendations across agents, skills, commands, and MCP tools (extract a new sub-agent or command, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree.
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
  e.g. "231 Explore spawns / 32.6M subtok in the last 30 days" — from
  whichever `my_toolkit_adoption` surface has the biggest signal this
  window: `.agents`, `.skills`, `.commands`, `.mcp_servers`, or
  `.tool_counts`. Built-in agents (`Explore`, `Plan`, `general-purpose`,
  `Task`) count on equal footing with namespaced plugin agents here —
  see step 5.
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
enriched_spawn_count, avg_tokens_per_enriched_spawn, tools_seen,
unique_labels, top_labels[:8], tokens_total, session_count,
burst_count, sample_top_by_tokens}] }`. Rows come sorted by
`tokens_total` descending. `avg_tokens_per_enriched_spawn` and
`tools_seen` are what step 5's counterfactual-ratio and extract-vs-gap
mechanics read directly — use them rather than re-deriving the same
arithmetic from `sample_top_by_tokens`.

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

- If `my_toolkit_adoption` shows an agent/skill/command whose name
  looks like it might cover the meta-cluster's dominant verb buckets +
  `tool_shape`, `Read` the matching file under `.claude/agents/`,
  `.claude/skills/*/SKILL.md`, or `.claude/commands/` (including
  plugin-installed paths under `~/.claude/plugins/`) to confirm the
  fit before recommending `adopt`. Bad `adopt` recommendations happen
  when the name matches but the actual scope doesn't. If the candidate
  is a **built-in** (`Explore`, `Plan`, `general-purpose`, `Task`)
  there's no file to `Read` — use the description string from your own
  tool listing instead; that's expected, not a reason to skip it (see
  step 5's contrast-pair mechanic, fixed in v5 to stop excluding
  built-ins).
- If `my_toolkit_adoption.mcp_servers` shows an entry with
  `err > 0.1 * n`, that's evidence for a `swap`/`pin` flag on the MCP
  side — check `.mcp.json` for how that server is configured before
  saying anything actionable about it.
- If you're considering `extract` (mint new), `Grep` under
  `.claude/agents/` and `.claude/commands/` for the meta-cluster's
  `tool_shape` — a similar-shape agent or command may already exist
  under a name your `my_toolkit_adoption` scan didn't surface.
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
contrast pairs — across every capability surface, not just agents.**
A contrast pair sets a named capability's aggregate usage (from
`my_toolkit_adoption`, the "routed" side) against a reducer sub-cluster
whose labels credibly describe the same domain but never name that
capability (the "bypassed" side). Do **not** look for the contrast
inside a single spawn's `tool_signature` — an `Agent`/`Skill` call
there reflects the *parent* turn's routing decision, not the child
subagent's own trace, and `tool_shape` (top-2 tools by frequency)
usually buries a lone `Agent`/`Skill` call under `Bash`/`Read` noise
anyway. Cross-data-source contrast is the mechanic that actually
surfaces evidence:

1. **Build the routed side from every `my_toolkit_adoption` surface,
   not just `agents`.** (v5 fix — v4 restricted this to namespaced
   agent keys, which silenced the single biggest signal in real data:
   a built-in like `Explore` clearing 231 spawns / 32.6M `subtok`.)
   Keep any entry — agent, skill, command, or MCP server — clearing
   **≥10 spawns/invocations OR ≥1M `subtok`/`tok`**. This explicitly
   *includes* built-ins: `Explore`, `Plan`, `general-purpose`,
   `Task` all qualify on the same bar as a namespaced plugin agent.
   "Built-in" is not a reason to exclude something the user's own data
   shows them reaching for constantly — it just means the domain
   vocabulary in step 2 has to come from the tool's actual behavior
   description (see `Explore`'s own agent-listing description, e.g.
   "fast read-only search agent for locating code... find files...
   grep for symbols... where is X defined") rather than a hand-authored
   frontmatter file, since built-ins don't have one on disk to `Read`.
   Also check `my_toolkit_adoption.skills` (≥5 invocations OR ≥100k
   `tok`) and `.commands` (≥5 invocations) the same way — a skill or
   slash command is just as valid a routed side as an agent.
2. For each kept capability, build a small domain-vocabulary set from
   its real behavior: for a file-backed agent/skill, `Read` the
   `description:` frontmatter you already located in step 4 — the
   content words in the "use this when..." sentence, not just the
   slug (a name like `pr-test-analyzer` undersells that its domain
   also covers "coverage" and "regression"). For a built-in with no
   file on disk, use the description string your own tool listing
   carries for it. For a slash command, infer domain from its name
   plus whatever `.claude/commands/<name>.md` says.
3. Walk the reducer's `reduced_rows` — **including rows where
   `zero_signal_count == spawn_count`** (the reducer retains these;
   they carry no tokens/model but their `top_labels` are real
   evidence, and labels like "General code review" / "Silent failure
   hunt" / "Explore ui-web routing" are often the *strongest* signal
   precisely because they're bespoke, self-narrated Task descriptions
   rather than toolkit boilerplate). Flag a row as a bypass candidate
   when its `top_labels` share ≥2 content words with a kept
   capability's domain vocabulary. Also check `my_toolkit_adoption
   .tool_counts` for recurring native-tool patterns that echo a
   sub-cluster's `tool_shape` (e.g. heavy direct `Bash`+`Read` calls
   matching what a command already automates).
4. Require the candidate to clear the same bar a one-off wouldn't:
   **≥2 spawns**, same session or sessions within the window you're
   already scanning. Two or more label-only reviews fired back-to-back
   in one session (check `at` timestamps — a burst under a few minutes
   is a giveaway) is a much stronger tell than a single spawn.
   Bonus confidence: if the same verb-phrase recurs across sessions
   with only the trailing subject varying ("Code review Phase A" /
   "Code review site-config-v2" / "General code review") — that's a
   hand-rolled loop the user re-invents each time, not a one-off.
5. **Compute the counterfactual ratio — say a magnitude number, not
   just a shape argument.** (v5 fix — v4 pitched adopt on shape/label
   overlap alone with no quantified size comparison.)
   - Routed-side avg = the capability's own `subtok / n` (agents) or
     `tok / n` (skills/commands) from `my_toolkit_adoption` — tokens
     spent per invocation when the work actually goes through it.
   - Bypassed-side avg = the sub-cluster's
     `avg_tokens_per_enriched_spawn` (reduce_spawns.py v5 emits this
     directly — tokens_total divided by non-zero-signal spawns only,
     so zero-signal rows don't silently deflate the average).
   - Ratio = bypassed avg / routed avg. **≥3x is a real magnitude
     signal** worth stating out loud even with no cohort/$ pricing
     ("your inline pattern runs ~13x the tokens per spawn that
     `Explore` averages for the same shape of work") — this is
     evidence a plain shape-match pitch doesn't carry.
6. State the caveat honestly when you present this: `cluster_spawns`
   never exposes `subagent_type` on a member (only
   `session_id`/`at`/`subagent_description`/`subagent_model`/
   `tokens_total`), so you cannot prove the bypassed spawns did NOT
   route through the named capability — only that their labels don't
   mention it and their shape/cadence reads as ad hoc. Pitch it as
   "these don't reference `<capability>` and look hand-rolled" rather
   than "you didn't use `<capability>`."

Once you have a pair, the play is `adopt` — the user has the
capability, they're inconsistently reaching for it.

**Extract vs gap — where the bar actually sits.** (v5 fix — v4 let
`gap` absorb any sub-cluster without a clean adopt target, including
ones with real recurring evidence; that's too easy an out.) Walk each
top-K meta-cluster's member sub-cluster rows that had no adopt match
in the loop above:

- If a single row clears **≥2 spawns** with a tool_shape that isn't
  `<empty>`, that's `extract` — mint a new sub-agent, no further
  argument needed.
- If sibling rows share the same **verb** but split across different
  `tool_shape` buckets purely from top-2 noise (check `tools_seen` —
  the reducer's v5 field listing the *full* tool set per row, not just
  the top-2 that make `tool_shape`; two rows with identical or
  near-identical `tools_seen` are the same underlying job, just
  weighted differently between calls), **combine their spawn_count**
  before applying the ≥2 bar. Two `migrate` rows at spawn_count=1 each
  — one bucketed "Read+Write", one "Bash+Read", both with
  `tools_seen: [Bash, Edit, Read, Write]` — are one recurring
  "migrate X to KB" pattern (2 spawns, same session, ~4M tokens each),
  not two singleton gaps. This is exactly the failure mode that
  silenced the Migration/Implementation meta-cluster in the v4 run.
- Only fall to `gap` when, after that combining step, the surviving
  `tools_seen` sets for same-verb rows still share fewer than 2 tool
  names in common (genuinely heterogeneous — no coherent subagent
  could be authored from them), or no row/combination clears 2 spawns
  even after combining. State which of the two you checked when you
  call it `gap` — "combined across sibling tool_shapes, still only 1
  spawn" reads very differently from "combined, and the shapes still
  don't overlap."

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
justify. **None of these kinds are agent-only** (v5 fix — v4's kind
list read as if the target was always a sub-agent; every kind below
can target an agent, a skill, a command, or an MCP server, whichever
`my_toolkit_adoption` surface the evidence actually points at). The
five buildable kinds and one signal-only kind:

- **`adopt`** — the cluster's `tool_signature` and label overlap an
  existing capability you saw in step 3 (agent, skill, or command).
  Recommend the user reach for the existing thing consistently, no new
  file. Softer signal: the user's own `my_toolkit_adoption` shows the
  target with meaningful usage already (they know it exists; the miss
  is inconsistency, not awareness).
- **`swap`** — cluster overlaps an existing capability, but that
  existing capability is the wrong shape or wrong model for this work
  — or, for an MCP server, the wrong integration for the job. Recommend
  replacing it. Harder to justify than `adopt`; state the reason
  ("existing agent is pinned to opus but the tool_signature is
  mechanical — swap it for a cheap-tier variant"). MCP-server swap
  candidates surface from `my_toolkit_adoption.mcp_servers`: an entry
  with `err > 0.1 * n` is worth flagging, but a swap needs a cheaper/
  more-reliable alternative to point at — absent a cohort comparison
  across other users/orgs, you usually can't name one. When you can't,
  don't force a `swap`; say plainly this is signal-only ("`<server>`
  errors on ~N% of calls; no alternative to recommend without
  cross-org comparison data") and skip the artifact.
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
  capability to point at. Mint a new sub-agent (default) or, when the
  pattern is a recurring shell/git sequence rather than open-ended
  reasoning (Bash-only or Bash+Read `tool_shape` on git/build/release
  work), mint a new slash command instead — a command is the more
  honest artifact for "always run these same 4 git commands," a
  sub-agent is for "always reason the same way over varying inputs."
  This is the one kind where you author a full new file from scratch:
  `.claude/agents/<name>.md` or `.claude/commands/<name>.md`. Requires
  you to name the capability and derive its `tools:` allowlist (agent)
  or command body (command) from the cluster's `tool_signature`/
  `tools_seen`. See the "Extract vs gap" bar above for when this
  applies instead of `gap`.
- **`consolidate`** — two clusters, or two existing capabilities
  (skills, commands, or agents), look like the same underlying job
  under two different labels/names — e.g. two commands both invoked
  regularly (`my_toolkit_adoption.commands` shows real usage on both)
  whose names read like a pre/post-rename pair. No new file; recommend
  merging — name which should absorb the other and why. Present
  conversationally, don't auto-locate or auto-merge the files.
- **`gap`** (signal only, no artifact) — cluster is real recurring
  work, but you cannot pick a kind honestly even after the combining
  step in "Extract vs gap" above (tool shapes too heterogeneous to
  author a coherent subagent or command, and no adopt target exists).
  Say so plainly, no artifact, no confirmation question, no
  `estimate_savings` call for this candidate. Not every capability
  surface will produce a play from a given 30-day window — a
  window that doesn't turn up a skill/command/MCP-level `gap`-free
  recommendation isn't a mechanic failure, just a quiet window for
  that surface.

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

**`extract` — new file at `.claude/agents/<name>.md`** (default) **or
`.claude/commands/<name>.md`** (when the pattern is a fixed shell/git
sequence rather than open-ended reasoning — see step 5's `extract`
definition for the split). You author:

- `name`: kebab-case, `^[a-z][a-z0-9-]*$`, derived from the cluster's
  `representative_label`. Short enough to type. If a same-name file
  already exists in `.claude/agents/` (or `.claude/commands/` for a
  command extract), disambiguate rather than overwrite.
- `description`: one sentence stating what work this handles and when
  Claude should delegate to it. Ground the sentence in the cluster's
  actual observed work — reference the tool_signature's (or, when you
  combined sibling rows per step 5's "Extract vs gap," the combined
  `tools_seen`) dominant tools and the label's phrasing. Not a
  template; you write it.
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
already handles it — I saw N sessions where it would have applied,"
plus the counterfactual ratio from step 5 ("~13x the tokens per spawn
`<capability>` averages for this shape"). No confirmation-to-write
question; the actionable part is the user's future behavior, not a
diff. **Never offer a CLAUDE.md line, or any other prose-nudge
artifact, as a fallback** — not on request, not "if the user asks for
a durable reminder." A recommendation with no clean capability-level
artifact is not an `adopt` with a CLAUDE.md consolation prize; it's
signal you present honestly and move on (see `gap`, or the closing
note below).

**`swap` — edit or replace an existing file** (`.claude/agents/*.md`,
`.claude/commands/*.md`, or `.mcp.json` for an MCP-server swap).
Author the dry-run as the specific edit you'd propose: which file,
which field(s) change, what the new content is. Do not mint a
brand-new file under `swap` without the user explicitly asking for
one.

**`consolidate` — no automated file work.** Present the two-candidate
overlap (two agents, two skills, or two commands) and ask the user
which capability should absorb the other — name the file each lives
in (`.claude/agents/*.md`, `.claude/skills/*/SKILL.md`, or
`.claude/commands/*.md`) so the ask is concrete. Do not attempt
auto-locate or auto-merge.

**No clean capability-level intervention exists.** Some real
patterns just don't map onto any of the four file types this skill can
write (`.claude/agents/*.md`, `.claude/commands/*.md`,
`.claude/skills/*/SKILL.md`, `.mcp.json`) — an MCP-server error-rate
flag with no alternative to name is the clearest recurring example.
Say so plainly: "this pattern doesn't fit any capability-level
intervention I can make cleanly; noting it as signal only." That is
the *only* honest fallback. Do not reach for CLAUDE.md as a substitute
artifact under any kind, including this one.

**`gap` — no artifact.** State the pattern, name it as a signal.
Skip step 7 (Mark) — actually, do call `mark` with `status:
"presented"` and `proposed_kind: "gap"` for the ledger's sake, but
skip the write/confirmation loop.

**Present the recommendation like a colleague on Slack, not a
monitoring dashboard.** The mechanic below (§5's five-step algorithm,
`my_toolkit_adoption` cross-reference, sub-cluster reduction) is
scaffolding — it stays in your head, not on the user's screen. What
the user sees should read like "someone smart looked at my week and
noticed X."

**Ban list — never appear in user-facing prose.** Do not use these
internal-mechanic terms in the pitch:

- `sub-cluster`, `verb-bucket`, `meta-cluster`
- `routed side`, `bypassed side`, `contrast pair`
- `cluster_spawns`, `my_toolkit_adoption`, `subtok`,
  `zero_signal_count`, `tool_shape`, `tools_seen`
- `signal-only`, `<X>-shaped signal`
- Kind names as headings or prefixes ("Candidate 1 — adopt / agent:
  Explore" is the mechanic's structure, not the user's mental model)
- Tabular metric dumps as the lead (small tables INSIDE prose are
  fine when they carry evidence)

**Voice.** Short paragraphs. Concrete session dates and IDs where
they add credibility. Ratios, not percentages, when the point is
magnitude ("17× cheaper per spawn" reads sharper than "94% cheaper").
The recommendation goes FIRST; the evidence justifies it, doesn't
lead it.

**Example — bad framing (dashboard):**

> ### Candidate 1 — adopt Explore for code-locating spawns
> Routed side (my_toolkit_adoption.agents.Explore): 14 spawns,
> 1.75M subtok → ~125k subtok / spawn. Description: "fast read-only
> search agent for locating code..."
> Bypassed side (Research & Investigation sub-clusters whose labels +
> Bash+Read shape read as pure code-locating, not routed through
> Explore):
> [table of metrics]

**Example — good framing (peer):**

> **Past week, one thing worth changing:**
>
> You reached for generic subagents to do code-exploration work 9
> times, burning ~19M tokens. Two clear giveaways:
>
> - **Jul 15, session 9f21ee76**: "Trace polly sub-agent cwd flow"
>   then "Find sub-agent detection signals" — back-to-back, both
>   on opus-4-7, 8.4M combined
> - **Jul 15, session 2bbbd04f**: three "Read <adapter> telemetry"
>   spawns fired in 4 minutes at 09:29 (sonnet-5, 2.3M combined) —
>   a hand-rolled fan-out
>
> Your Explore agent is designed for exactly this shape of work
> (locating code, finding files, grepping symbols). You used it 14
> times last week at ~125k tokens each — **17× cheaper per spawn
> than the generic ones.**
>
> Next time you're about to spawn a Task starting with "Trace X,"
> "Find X in code," "Read X telemetry," "Survey path," or
> "Inventory constants for X" — reach for Explore. It can fan out
> inside itself; that three-adapter read could have been one
> "Read identity fields across omnigent/gemini/cursor" call.
>
> Can't quote dollars — your organization only prices Haiku and
> Sonnet 4.6, and you mostly run on opus. The 17× is a magnitude
> signal, not a receipt.

**Present order per recommendation:**

1. Lead with what to change. One-sentence recommendation, in the
   user's own vocabulary (agent names, session labels).
2. Show 1–3 concrete pieces of evidence — session IDs, dates, actual
   labels, tokens. Bursts and cross-session recurrence carry the
   most weight; solo spawns rarely justify a pitch.
3. State the counterfactual as a ratio or a burst-collapse ("three
   spawns in four minutes → one call would have covered it"), never
   as a percentage. If dollars are unquotable, say so once and move
   on — don't apologize twice.
4. Give the concrete next action. If it writes a file, name the
   file. If it's behavioral (`adopt`), say what to type/reach-for
   next time. If it's `consolidate`, ask which absorbs which.
5. Confirmation question ONLY when a file will be written.
   `adopt` / `gap` / signal-only observations don't ask "yes/no?" —
   they leave the reader with the observation and move on.

**Signal-only observations** (patterns you noticed but won't
recommend action on — MCP error rates without a cohort target,
single-session extract candidates, etc.) go at the end under a plain
"**Two things I noticed but won't recommend yet:**" heading. Never
call them "signal-only." Explain in one sentence why you're holding
off ("Only one session's worth so far — if it recurs, worth
minting..."). No taxonomy prefix.

**One candidate at a time — don't stack.** If a file write needs a
yes/no, wait for it before showing the next candidate. Behavioral
adopts / gaps flow together at the end; they don't need
confirmation.

**Confirmation format when a file will be written.** A plain
question, target path visible ("write this to
`.claude/agents/<name>.md`? yes/no"), no ceremony.

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
- Don't reach for `gap` just because the routed side isn't a
  namespaced agent, or just because a sub-cluster's exact `tool_shape`
  row only has one spawn — check whether it's a built-in (step 5's
  contrast-pair mechanic) and whether combining same-verb sibling rows
  by `tools_seen` overlap changes the count (step 5's "Extract vs
  gap") before you call it a gap.
- Don't offer a CLAUDE.md line, or any other prose-nudge, as a
  fallback artifact — for any kind, on request or not. The only honest
  fallback when no capability-level file applies is saying so plainly
  and stopping (see step 7's "No clean capability-level intervention
  exists").
- Don't exceed the ~10-call budget on `outcomes__*` tools; if you're
  reaching for more, stop and say the pipeline needs more than a
  thin skill can responsibly do here.
