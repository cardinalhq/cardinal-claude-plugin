---
name: storyboard
description: Turn a finished (or stalled) Cardinal investigation into an Investigation Storyboard — an evidence-bound, scene-by-scene explanation with its own interactive visuals, published in Cardinal and shareable by link. Use whenever the user wants to explain, write up, present, share, hand off, post-mortem or "storyboard" what an investigation found (an incident, a regression, a cost jump, a canary verdict), or asks for a visual walkthrough of the evidence — even if they only say "write this up for the team" or "show me how we got here" after using Cardinal tools. Covers the storyboard__* tools (describe_grammar, create, define_surface, upsert_scene, preview, publish, get_receipt), receipts, claim typing and the preview → critique → publish loop. Not for dashboards or ongoing monitoring.
---

# storyboard — explain the investigation

Every investigation gets its own interface. You author the view and the argument; Cardinal
owns the runtime and the evidence. That gives you complete freedom over presentation and no
freedom to invent evidence. This skill is the storytelling half. The **canvas** skill is the
visual half, and it covers how to draw a scene and how to render the previews.

> Rendered pixels may suggest a hypothesis but never establish a factual claim. Any quantitative or population-level statement must resolve to evidence from a receipt or deterministic derivation over receipts.

> Preview every scene before publishing. Inspect whether the intended point is visually obvious without reading the investigation transcript. Revise the presentation when it is not.

**How trust works.** Publish trust is deterministic: maestro checks the scene spec, evidence
bindings, receipts, derived values, libraries, static source checks, and the numbers in each
statement against the resolved bindings. It never renders anything or inspects pixels.
Rendering is authoring feedback, done locally by the plugin (canvas skill); a skipped preview
is a quality problem, not a trust violation. Storyboards are on for every org. This skill
targets Cardinal (maestro) **v1.97.14 or newer**; an older one rejects `select` and `ref` as
unknown binding keys and lacks get_receipt's navigation and preview's `verbose`, so ask
the user to upgrade.

## Receipts: collect them while you investigate

Every **read-only** Cardinal tool call (queries, lookups, kube reads) mints a receipt. It
shows as `[receipt:rcpt_<24 hex>]`, the last text block of the result (also `_receipt` in
structured content). A failed read (permission denied, not found, a tool error) is evidence
too: cite it with `{receiptId, selector: "/error/message"}` (never as a dataset). Writes,
transport failures and kube Secret reads get no receipt. Credentials can never be bound.

- Note receipt ids as you go, before you start authoring. A number you cannot point at a
  receipt cannot go in a storyboard.
- `storyboard__get_receipt {receipt_id}` returns what a receipt recorded (args, window,
  result), capped at ~24 KB. Past the cap `model_result` is null and `model_result_omitted`
  names the next calls. Navigate with `outline: true` (paths, types, array lengths,
  samples), `pointer` (the value there), and `pointer` + `rows: {offset, limit ≤200}`
  (elements with their absolute pointers). A pointer from get_receipt is a binding selector
  as written. A text-only result pages by line with `rows` alone (readable, not bindable).
- Receipts expire after **14 days** unless a *published* storyboard cites them; a draft
  citing an expired one fails with `receipt_not_found`: re-run the query, cite the new one.
- If a preview shows you a pattern you never measured, measure it with a tool call, then
  cite that receipt.

## Tools and shapes

Fetch the grammar once per session: `storyboard__describe_grammar` (~25 KB, or `{section}`:
overview, schema, bindings, canvas, prefabs, libraries, rules). It is the only reference for
scene and binding schemas, derive and reduce ops, and caps. Do not work from memory.

| Tool | Input | Returns |
|---|---|---|
| `storyboard__create` | `{question, window: {start, end} (RFC3339 with zone), canvas_allowed?, session_id?}` | `{storyboard_id, view_url, next}` |
| `storyboard__define_surface` | `{storyboard_id, name, surface: {source, libraries?, bindings?}}`, or to revise it `{storyboard_id, name, edits: [{old, new}]}` | revision, warnings |
| `storyboard__upsert_scene` | `{storyboard_id, scene, ordinal? \| after?}` or `{storyboard_id, remove: <scene id>}` | revision, warnings |
| `storyboard__preview` | `{storyboard_id, scene_ids?, verbose?}` | `{ok, errors, warnings, scenes[].{ok, errors, warnings, bindings, preview_bundle}, materialization, local_preview, view_url}` |
| `storyboard__publish` | `{storyboard_id}` | `{published, view_url, warnings}`; 422 with the full report when not clean; 409 `published` when already published |

- **session_id:** a SessionStart hook puts this session's id in your context ("Cardinal
  session id for this session: …"). Pass it to `create`. It labels the storyboard row only.
- The same scene id replaces a scene in place. Upserts are cheap: batch edits between
  previews.
- **Revise a surface with `edits`; never resend the whole source.** Up to 50 `{old, new}`
  pairs, applied in order to the **stored** source; each `old` must match exactly once
  (else `edit_no_match` / `edit_ambiguous`, nothing written). Libraries and bindings are
  kept; to change them, send the full `surface`.
- **Preview shows every binding's resolved value**: `{value_preview, unit?, approx?}`, a
  surface's shared bindings once per call (`"surface:<name>"` after). `verbose: true` adds
  kind, row counts and provenance. Check that each binding holds what you meant, a regex
  `extract` included, before you look at the pictures. Publish's report omits it.

## Writing the argument

A storyboard is not a transcript, a report, a dashboard or a set of charts. It is your
edited explanation of what the investigation learned, and its interface is designed after
the question is known. Before any tool call, answer three questions. *What is the point?
What should be visually dominant? What can disappear?*

- **Minimum scene sequence.** Keep only the scenes the argument needs; a reader should miss
  each one if it were cut. Keep a wrong turn only when it is material (a hypothesis a reader
  would otherwise raise), shown as `ruled_out`.
- **Titles are findings, not topics**, in about one clause, with no fixed prefix such as
  "Answer:". The navigator lists each scene's title and state, so read down it: it is the
  argument compressed. "Cache hit rate" names a topic; "Cache hit rate fell only on the
  upgraded replicas" states a finding.
- **State is the reader's epistemology.** The viewer shows it as Supported, Ruled out, Open
  or Context. A scene's state is what the scene establishes, not how confident it sounds or
  how causal its visual looks, and its statement asserts only that part. The unresolved
  remainder goes in `openQuestions` (`{id, text, about?, next?}`, ≤12): an established
  cause with an open impact is `supported`, with open questions. Use `ruled_out` for what
  the evidence eliminated and `open` when the scene settles nothing; an open ending is
  valid. Never manufacture closure to make the story neater.
- **`transition.note` is written for the reader**: the viewer shows it as the line that says
  why this scene comes next. The viewer renders each scene's state and its `openQuestions`
  from the spec; do not draw state pills or a "still open" list in the Canvas: a copy in
  pixels drifts from the spec.
- **One point per scene.** The `statement` must stand on its own, without the visual. Every
  number in it must be a value the scene binds (its own bindings or its surface's), or the
  prose-number check warns. It skips dates, clock times, versions (1.97.13, v1.4), cron, ids
  and digits glued to letters (p99, 1h30m), and checks every other quantity, a bare 1.4
  included; the exact list is `describe_grammar {section: "rules"}` → `rules.prose_numbers`.
  A bound numeric string ("0.05") matches as written; the check never reads Canvas source,
  so bind every number a canvas draws.
- **Claims are evidence first.** Type them honestly: `precedes` is not `causes`. Causal kinds
  (`causes`, `contributes_to`) need an evidence ref with role `supports` (an evidence-ref
  role, not the claim kinds `supports` / `contradicts` / `rules_out`). Say how far a claim
  reaches with `scope` (the population the evidence could see). Name the two things a claim
  connects as noun phrases a reader recognises (a service, a query shape, an org, a
  feature flag), so it reads as from → kind → to, naming only what a receipt identifies.
  An attribution that is only `correlates_with` (the receipt shows an org and a query
  shape, not which agent sent it) stays its own claim: never inside another claim's
  from/to, and it does not decide the scene's state. For `rules_out`, from = the candidate
  cause/hypothesis being ruled out, to = the outcome it is ruled out for; reads "from —
  ruled out as a cause of — to". Role is relative to the claim sentence, so the receipts
  that establish a rules_out claim are role `supports`. Do not bend a kind, an endpoint or
  a scope to make a sentence read well.
- **Human-scale numbers are presentation; the measurement stays.** Write magnitudes as a
  person would say them ("~31 min end to end", "212× its usual rate"), and keep the exact
  value bound and visible where the magnitude matters (marked in the canvas, or beside the
  humane figure). A ratio or delta combines values, so bind it (`derive divide` / `subtract`
  / `percent_change`). Unit conversion, rounding and separators are not derive ops: the prose
  check reconciles "~31 min", "1.8M" and "1,843,200 ms" with a bound 1843200 declared
  `unit: "ms"`. Declare `unit` in a usual spelling (ms, s, min, h, d; B, KB, KiB, GB…; %)
  and confirm it in `value_preview`. k, M, G and × are written in the statement ("1.8M"),
  never declared as the unit. An unknown label (`"millis"`, `"k"`) never scales, switches
  off the fallbacks and is drawn by prefabs as the unit: worse than none. The check allows
  one step of the last written digit, so 212.4 can be "212×" but not "about 200×".
- **Numbers come from bindings, never from the spec.** Bind them from receipts (JSON
  Pointer selectors) or compute them with `derive` / `reduce` / `extract`. For a whole
  population the model never loaded, bind the dataset (`representation: "dataset"`) and
  cite a `reduce`/`derive` over it.
  - **Address a row by identity with `select`**: `{select: {receiptId, representation?,
    in, match, pointer?, unit?}}`. `match` is 1–4 row-relative pointers, equality only,
    strict types (`"200"` ≠ `200`); exactly one row must match (`select_no_match` names
    nearby values; `select_ambiguous`: add fields). It needs no `expect` guard; a positional
    selector (`/data_points/3/…`) still does. A value inside a JSON string (kube events,
    log lines) needs `extract`; derive never coerces a string, and `extract {parse: "json",
    pointer: ""}` turns `"5"` into 5.
  - **Reuse a value with `ref`**: `{ref: <binding key>, pointer?}` reads this scene's
    bindings (its surface's and its own); across scenes, bind it on the surface. Declare
    `unit` on the target, not the ref; a `pointer` reaches into receipt-read values only,
    not derive or extract results. Each dataset `select` reads the whole dataset: select
    once, then `ref` it.
  - **`reduce.where` takes a literal** (`"value": "eu-west"`), compared as written in
    `where.field`'s unit, besides a binding or a labelled author threshold `{param, label}`.
- **Mind populations.** Most critique findings so far were population errors, not craft:
  numbers from different populations composed on one canvas, prose claiming more than the
  receipts measured, a capped series drawn as if complete. Take the warnings below seriously.
- **Answer the question the storyboard was created with.** Established parts are claims
  with evidence refs; unresolved parts stay `openQuestions`, never a confident sentence.

## The loop

```
investigation (collect receipt ids)
  → describe_grammar → create (session_id) → define_surface / upsert_scene   (draft)
  → storyboard__preview        deterministic validation + one preview_bundle ref per scene
    ↳ plugin hook              local Chromium → PNG per scene per reveal step; reports the paths
  → Read every PNG → critique: is the point obvious in five seconds, without the transcript?
  → revise (define_surface edits / upsert_scene) → preview → Read … → critique the whole
  → storyboard__publish
```

You do not run the renderer: Read the PNG paths the hook reports (every step, first and
last included). The canvas skill covers the critique and the manual fallback.

- **The first preview materializes datasets.** It runs bounded re-executions of the
  receipted queries, so it can be slow. Later previews reuse them.
- Preview and publish share a **per-org limiter** (burst 10, then 6/min, plus a
  concurrency cap). A 429 `validation_busy` means wait a few seconds and retry. Batch edits
  instead of previewing after each one.
- Fix every error. Treat warnings as real problems until you can explain each one:
  `prose_number_unreconciled`, `unit_relabel`, `dataset_drift`, `dataset_incomplete`,
  `incomplete_population`, `population_mismatch`, `timestamp_mismatch`,
  `sparse_groups_at_timestamp`, `within_one_step` / `step_aware_precedes`,
  `unit_mismatch`, `epoch_guessed` (see `describe_grammar` `rules.warnings` and
  `bindings.warnings`).
  For `sparse_groups_at_timestamp`, prefer a `latest_only` query (below), which names stale
  groups outright, and disclose any group that stays off the shared time; ignore it for
  deliberate per-group moments, such as each group's peak.
- A `preview_bundle` is valid for **one revision**: after any edit, preview again.
- If the hook says there is no local Chrome, say once that previews were skipped, then
  continue.

**Before `storyboard__publish`, critique the storyboard as a whole**, as a stranger
arriving from the link would read it:

- Can each scene's finding be understood in about five seconds?
- Does each visual explain the finding, or mostly display telemetry?
- Does every scene justify its place?
- Can the reader tell what was established from what is still open?
- Read alone, do the navigator's titles and states convey how the investigation progressed?
- At the end, can the reader answer the original question?
- Can the reader get from each important claim to the evidence behind it?

A "no" means revise, not a caveat in the handover. **Publishing is final.** A published
storyboard is immutable, and it retains every receipt it cites. To revise one, create a
new storyboard.

## Queries that make good evidence

- **"Count now, per group"** (per processor, per service, over the window): call
  `execute_logs_query` / `execute_metrics_query` / `execute_spans_query` with
  `latest_only: true` over the full window and a rolling range equal to the window (e.g.
  `sum by (processor)(count_over_time({…} [24h]))` over 24h). You get one row per group
  at its last step, instead of a 70–600 KB series you would dig rows out of. Stale groups
  are listed in the result, and ddsketches are omitted. Aggregate queries only, and not
  combinable with `series_reduction`.
- **Range selectors must be a multiple of Lakerunner's step** for the window, or the
  gateway rejects the query (Lakerunner itself would return nothing): over 24h (5m step)
  use `[5m]`, `[30m]` or `[24h]`, never `[28m]`. The tool descriptions list the steps.

## Handing it over

Give the user the `view_url`. It is absolute when the Cardinal install sets
`MAESTRO_BASE_URL` (app.cardinalhq.io does). A self-hosted install may return an
app-relative path; prefix it with the Cardinal host the user is connected to. Draft
storyboards are visible to org members at the same link. Say which scenes stayed `open`
and why.
