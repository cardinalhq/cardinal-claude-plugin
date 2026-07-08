# Subagent telemetry enrichment (latent-subagent mining, plugin side)

Add four targeted fields to the telemetry the plugin already emits so
the Outcomes Dashboard can mine **latent subagents** — recurring,
self-contained work-clusters that run inline on an expensive model and
should be extracted into a model-pinned subagent. This spec covers only
the plugin-side signal; the mining pipeline and product surface live in
conductor (`docs/specs/latent-subagent-harvester.md`,
`docs/specs/subagent-recommendation-cards.md` there).

## Why

A prototype harvester (2026-07-08, read-only SQL over
`agent_sessions` + `agent_session_events`, window Jun 7 – Jul 8)
validated the thesis with zero telemetry changes:

- Subagents appear in only ~19% of rjha's 169 sessions and ~22% of
  mgraff's 839 — the other ~80% of sessions do everything inline on
  the main model (mostly opus).
- The **explorer archetype** (read-only navigation) is the flagship:
  mgraff had 18.8M inline-opus tokens undelegated across 80 sessions
  (~$497; ~$398 saved at a sonnet pin, ≈$4k/yr for one engineer).
- The **runner archetype** (Bash-heavy loops) is larger by volume
  (mgraff 21.6M tokens) but read/write-ambiguous, forcing a ~50%
  discount.
- The **observability archetype** (inline MCP investigation loops,
  rjha ~$610 across 11 sessions) was *invisible* in the session
  aggregate — MCP calls never reach `tool_counts`.

The v0 numbers are upper-ish bounds. The four fields below are the
entire gap between that supervised proxy and a trustworthy harvester.
This is targeted enrichment — not "fix all telemetry."

A second motivation is the **Explore decomposition**. "Explore" fuses
two jobs: mechanical retrieval (grep/read/symbol lookup — wants a cheap
tier or the code-mcp tools) and inference/synthesis (wants a
reasoning-strong tier). A flat model pin is wrong in both directions.
The per-subagent **input/output token split** is the signature that
proves and sizes that boundary: a bimodal profile (huge input+cache /
tiny output retrieval phase, then a large-output synthesis tail) means
the right recommendation is a two-stage pipeline, not a single pin.
Today `subagent-usage.py` collapses the components into one
`total_tokens`, so the decomposition is unmeasurable.

## Design constraints

1. **No content capture.** Same privacy boundary as
   `per-turn-telemetry.md`: no prompts, no command lines, no tool
   arguments beyond the existing file-path `target` allowlist. Every
   new field is either a token count, a model id, a tool *name*, or a
   closed enum.
2. **Additive only.** Existing fields keep their names and semantics
   (`total_tokens` remains the subtok source; one semantics per
   field). Downstream processors that ignore the new attributes keep
   working unchanged.
3. **Best-effort, fail-open.** Same hook contract as today: silent
   exit on any failure, async, never blocks the loop. If a component
   can't be computed, omit the field rather than emit a guess.
4. **Bounded work.** Hooks already stream the relevant transcripts;
   no new file reads are introduced except the subagent tool-name
   scan, which rides the same pass `_sum_transcript_usage` already
   makes.

## Field 1 — subagent token components + model + tool histogram (keystone)

**Hook:** `subagent-usage.py`. `_sum_transcript_usage()` already reads
every per-request usage record and `message.model` from the subagent
transcript — it sums into one number and discards the rest. Extend the
same single pass to collect and emit:

| New attribute | Type | Source |
|---|---|---|
| `subagent_input_tokens` | int | Σ `usage.input_tokens` |
| `subagent_output_tokens` | int | Σ `usage.output_tokens` |
| `subagent_cache_creation_tokens` | int | Σ `usage.cache_creation_input_tokens` |
| `subagent_model` | string | dominant `message.model` by worked tokens |
| `subagent_model_count` | int | distinct models seen (>1 ⇒ mixed run) |
| `subagent_tool_counts` | string (JSON) | `{tool_name: count}` over `tool_use` blocks in assistant messages |

Notes:
- `subagent_cache_read_tokens` and `total_tokens` already exist;
  `total_tokens` must remain exactly
  `input + cache_creation + output` (invariant: the three new
  component fields sum to it — a downstream consistency check).
- `subagent_tool_counts` carries tool *names only* (including
  fully-qualified MCP names, e.g. `mcp__cardinal__lakerunner__…`).
  Cap at the 32 most frequent names to bound attribute size; if
  capped, add `subagent_tool_counts_truncated=true`.
- Dominant-model tie-break: most worked tokens wins; ties broken by
  first-seen.

This field alone unblocks: (a) grounded savings math per subagent
(model is known, not inferred from the session default), and (b) the
bimodal Explore signature via the input/output split.

## Field 2 — stop dropping tool calls; get MCP into the session aggregate

**Hook:** `turn-usage.py`. Two changes:

1. **Chunked emission instead of a hard drop.** Today
   `MAX_TURN_USAGES=64` / `MAX_TURN_TOOLS=256` truncate long turns
   (flagged `truncated=true`, but the data is gone — the prototype
   saw only 164 MCP calls total, far too few to size the
   observability archetype). Replace the single-POST cap with
   chunked emission: build all records, emit in batches of ≤256
   logRecords per POST (same 1-ns index offsets, continued across
   batches so the `chq_tsns` PK stays unique). Keep an absolute
   safety ceiling (e.g. 4,096 records per firing) with the existing
   `truncated=true` flag as the fallback for genuinely pathological
   transcripts.
2. **Server-side dependency (cross-repo, lakerunner):** fold
   `cardinal.turn_tool` MCP tool names into the `agent_sessions`
   `tool_counts` / `mcp_servers_used` aggregates so the harvester can
   size MCP work from the session grain without scanning raw events.
   Plugin emits nothing new for this; the spec records the
   dependency so the two changes land together.

## Field 3 — session-monotonic turn counter (`user_turn_seq`)

**Hook:** `turn-usage.py`. Contiguous-run segmentation needs a stable
global ordering of tool calls within a session. Today `turn_seq` /
`tool_seq` order records *within one Stop firing* and reset on the
next; cross-firing order leans on wall-clock `ts`, which is fragile
(clock granularity, retried emits).

Add `user_turn_seq` (int) to every `cardinal.turn_usage` and
`cardinal.turn_tool` record: the ordinal of the current user turn
within the session. `_walk_current_turn()` already streams the whole
transcript forward and detects real-user-message boundaries — count
them in the same pass instead of discarding the count. The triple
`(user_turn_seq, turn_seq, tool_seq)` then totally orders the
session's tool stream.

Note: per-tool→model-call *cost linkage already exists* — a
`turn_tool` record's `turn_seq` joins to the `turn_usage` record
carrying that model call's `model` and usage. No new field needed for
cost; `user_turn_seq` is what's missing for boundaries.

## Field 4 — privacy-safe Bash verb class

**Hook:** `turn-usage.py`. The runner archetype (largest pool by
volume) is blocked on Bash being read/write-ambiguous. For
`tool_name == "Bash"` records only, classify the command string
in-process and emit **only a closed enum** — never the command line,
never a fragment of it:

| `bash_class` | Examples (never emitted) |
|---|---|
| `git-read` | `git status`, `git log`, `git diff`, `git show` |
| `git-write` | `git commit`, `git push`, `git rebase`, `git checkout -b` |
| `test` | `pytest`, `go test`, `npm test`, `cargo test` |
| `build` | `make`, `go build`, `npm run build`, `tsc` |
| `pkg` | `pip install`, `npm i`, `brew`, `cargo add` |
| `file-read` | `ls`, `cat`, `find`, `grep`, `head`, `wc` |
| `file-write` | `rm`, `mv`, `cp`, `mkdir`, `chmod`, `sed -i` |
| `network` | `curl`, `wget`, `gh`, `ssh` |
| `other` | anything unmatched |

Classifier rules:
- Tokenize on shell separators (`&&`, `;`, `|`); classify each
  segment by its leading command word (after stripping env-var
  prefixes and `sudo`); emit the **most write-risky** class present
  (ordering: `file-write` > `git-write` > `pkg` > `network` > `build`
  > `test` > `git-read` > `file-read` > `other`) plus
  `bash_multi=true` when segments span classes.
- The rule table is a static dict of command-word → class. Unknown
  words → `other`. No regex over arguments; the command word is the
  only input to the lookup, and it is not emitted.
- This is the one field with a privacy judgment in it. The enum is
  deliberately coarse; if a future consumer wants finer classes, that
  is a new spec, not a widened emit.

## Field 5 — subagent task label (`subagent_description`, v0.12.1)

**Hook:** `subagent-usage.py`. The Agent tool's `description`
argument — the orchestrator's short (3-5 word) task label for the
spawn, e.g. "Release Claude plugin v0.12.0" — arrives in the
PostToolUse payload's `tool_input`. Emit it verbatim as
`subagent_description`, hard-capped at 160 characters (truncate, no
ellipsis marker); omit when absent, empty, or non-string.

**Why:** with the label, spawn clustering flips from shape-inference
(guessing intent from token components and tool histograms) to direct
label-grouping, and minted agents can name themselves from label
clusters instead of receiving synthetic names.

**Privacy note (explicit):** this is the first free-text field the
plugin emits — every prior field is an enum, count, identifier, or
model name. The widening is deliberate and was consciously approved:
the value is a model-authored *task label*, capped at 160 chars, not
user or tool content. Prompts, tool arguments, and tool results
remain never-captured; this field does not open the door to them.

## Shipped assumed-agent catalog (counterpart, zero new telemetry)

The reasoning archetype is handled by assumption rather than
detection (conductor `latent-subagent-harvester.md`, §Assumed agent
catalog): the plugin ships a small catalog of universal agents under
`plugins/cardinal/agents/`, starting with **`brainstorm.md`** pinned
to the fable-class tier via `model:` frontmatter. Scope notes:

- The shipped agent covers the *delegable* slice (self-contained
  divergent generation). Interactive strategizing stays on the main
  thread by nature — that grain is addressed by a dashboard nudge
  (conductor `subagent-recommendation-cards.md`,
  §session-model nudge), not by anything in this plugin.
- **No new telemetry fields.** Catalog usage self-labels through the
  events this spec already covers: `subagent_type='brainstorm'` on
  `cardinal.subagent_usage` (with `subagent_model` proving the pin
  held) and `agents_used` server-side. This self-labeling is the
  point — it removes the reasoning-work classifier from the
  harvester entirely.
- Catalog agents are plugin components, versioned and updated via
  ordinary plugin releases — distinct from beamed per-user
  recommendations, which arrive via the `agents` sync channel and
  carry the managed-file header.

## Testing

Extend `tests/test_subagent_usage.py` and `tests/test_turn_usage.py`:

- Component-sum invariant: emitted components sum to `total_tokens`
  on synthetic transcripts, including records with missing usage
  keys.
- Mixed-model subagent transcript → correct dominant model +
  `subagent_model_count=2`.
- Tool histogram: MCP-qualified names counted; >32 distinct names →
  capped + truncation flag.
- Chunked emission: 300-tool synthetic turn → two POSTs, contiguous
  1-ns offsets, no `truncated` flag; 5,000-tool turn → ceiling +
  `truncated=true`.
- `user_turn_seq`: multi-turn transcript → counter matches
  real-user-message count; tool_result-only user messages do not
  increment it.
- Bash classifier: table-driven cases per class; compound command →
  most-write-risky class + `bash_multi`; assert the emitted
  attributes never contain the command string (scan the OTLP body).

## Rollout & measurement

- Ships as a minor release (target v0.12.x); bump the scope version
  string in both hooks.
- **Validation gate before any conductor build:** once field 1 is
  live, re-run the prototype harvester queries and test for the
  bimodal Explore signature (retrieval-phase vs synthesis-tail
  input/output split). That single result decides whether the
  recommender must support two-stage pipelines before any
  recommender or beam-down work starts.
- Kill criteria: if 30 days of enriched data does not materially
  tighten the v0 pool estimates (explorer pool confidence interval
  still spans >2× after dedup of the per-turn double-count), stop
  and re-scope before building the harvester.
