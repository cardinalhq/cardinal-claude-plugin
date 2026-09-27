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

## How trust works (2026-09-27 decisions)

- **Publish trust is deterministic.** maestro checks the scene spec, evidence bindings,
  receipts, derived values, libraries, static source checks, and numbers in the statement
  against the resolved bindings. It never renders anything and never inspects pixels.
- **Rendering is authoring feedback.** Previews render locally on this machine: after
  every `storyboard__preview`, the plugin's hook renders the scenes with the canvas
  skill's renderer and reports the PNG paths. Skipping a preview (for example, when there
  is no local Chrome) is a quality problem, not a trust violation. Say so and keep going.
- Storyboards and the viewer are on for every org. There is no flag to ask about.
- This skill targets Cardinal (maestro) **v1.97.12 or newer**: surface `edits`,
  `value_preview`, numeric-string reconciliation, the `timestamp_mismatch` and
  `sparse_groups_at_timestamp` warnings, prefab frame errors and the queries'
  `latest_only`. On an older Cardinal, `define_surface` rejects `edits` with a 400:
  resend the whole surface and ask the user to upgrade.

## Receipts: collect them while you investigate

Every successful **read-only** Cardinal tool call (queries, lookups, kube reads) mints a
receipt. It shows as `[receipt:rcpt_<24 hex>]`, the last text block of the result (also
`_receipt` in structured content). Writes, failed calls and kube Secret reads get no
receipt. Credential fields are redacted and can never be bound.

- Note receipt ids as you go, before you start authoring. A number you cannot point at a
  receipt cannot go in a storyboard.
- `storyboard__get_receipt {receipt_id}` returns what a receipt recorded (args, window,
  result).
- Receipts expire after **14 days** unless a *published* storyboard cites them. A draft that
  cites an expired one fails with `receipt_not_found`: re-run the query and cite the new
  receipt.
- If a preview shows you a pattern you never measured, measure it with a tool call, then
  cite that receipt.

## Tools and shapes

Fetch the grammar once per session: `storyboard__describe_grammar` (~12 KB, or
`{section}` with one of overview, schema, bindings, canvas, prefabs, libraries, rules).
It is the only reference for scene and binding schemas, derive and reduce ops, and caps.
Do not work from memory.

| Tool | Input | Returns |
|---|---|---|
| `storyboard__create` | `{question, window: {start, end} (RFC3339 with zone), canvas_allowed?, session_id?}` | `{storyboard_id, view_url, next}` |
| `storyboard__define_surface` | `{storyboard_id, name, surface: {source, libraries?, bindings?}}`, or to revise it `{storyboard_id, name, edits: [{old, new}]}` | revision, warnings |
| `storyboard__upsert_scene` | `{storyboard_id, scene, ordinal? \| after?}` or `{storyboard_id, remove: <scene id>}` | revision, warnings |
| `storyboard__preview` | `{storyboard_id, scene_ids?}` | `{ok, errors, warnings, scenes[].{ok, errors, warnings, bindings[k].{kind, provenance, value_preview}, preview_bundle}, value_preview_rule, materialization, local_preview, view_url}` |
| `storyboard__publish` | `{storyboard_id}` | `{published, view_url, warnings}`; 422 with the full report when not clean; 409 `published` when already published |

- **session_id:** a SessionStart hook puts this session's id in your context ("Cardinal
  session id for this session: …"). Pass it to `create`. It labels the storyboard row
  only; receipts don't carry it.
- The same scene id replaces a scene in place. Upserts are cheap, so batch several scene
  edits between previews.
- **Revise a surface with `edits`; never resend the whole source.** `edits` is up to 50
  `{old, new}` pairs, applied in order to the **stored** source (each one sees the source
  as the previous edits left it). Each `old` must match exactly once, or the whole call
  fails with `edit_no_match` / `edit_ambiguous` naming the edit and nothing is written.
  Libraries and bindings are kept. The result is re-checked like a full replace and bumps
  the revision. Make `old` long enough to be unique. To change libraries or bindings, send
  the full `surface`.
- **Preview shows every binding's resolved value** as `value_preview` (bounded; see
  `value_preview_rule`). Check that each binding holds what you meant, a regex `extract`
  included, before you look at the pictures. Publish's report omits it.

## Writing the argument

Before any tool call, answer three questions. *What is the point? What should be visually
dominant? What can disappear?*

- **Minimum scene sequence.** Keep only the scenes the argument needs. Keep a wrong turn
  only when it is material (a hypothesis a reader would otherwise raise), shown as
  `ruled_out`.
- **One point per scene.** The `statement` must stand on its own, without the visual. Every
  number in it must be a value the scene binds. The prose-number check warns otherwise.
- **Type claims honestly.** `precedes` is not `causes`. Causal kinds (`causes`,
  `contributes_to`) need an evidence ref with role `supports`. Say how far a claim reaches
  with `scope` (the population the evidence could see).
- **End on `open` when warranted.** An unresolved question is a valid final state. Use
  `openQuestions` rather than overclaiming.
- **Numbers come from bindings, never from the spec.** Bind them from receipts (JSON
  Pointer selectors) or compute them with `derive` / `reduce` / `extract`. For a whole
  population the model never loaded, bind the dataset (`representation: "dataset"`) and
  cite a `reduce`/`derive` over it.
- **Numeric strings.** A bound string that is exactly a number literal (a label value
  `"0.05"`, a regex `extract` result `"5"`) reconciles with the same number in the
  statement, compared as written: `"0.05"` matches 0.05 and `"5"` matches 5, but there is
  no unit or % scaling, so `"5k"` or `"5%"` never match. `derive` still refuses strings
  ("left operand is string"). To compute with one, turn it into a number first with
  `extract {parse: "yaml" | "json", pointer: ""}` (or a pointer into the parsed document).
- **Positional selectors need `expect` guards** on the fields that identify the row, or a
  reordered result silently points at the wrong one.
- **Mind populations.** The spike's most common critique finding was population error, not
  craft. The failures were numbers from different populations composed on one canvas,
  prose claiming more than the receipts measured, and a capped series drawn as if it were
  complete. Take the warnings below seriously.

## The loop

```
investigation (collect receipt ids)
  → describe_grammar → create (session_id) → define_surface / upsert_scene   (draft)
  → storyboard__preview        deterministic validation + one preview_bundle ref per scene
    ↳ plugin hook              local Chromium → PNG per scene per reveal step; reports the paths
  → Read every PNG → critique: is the point obvious in five seconds, without the transcript?
  → revise (define_surface edits / upsert_scene) → preview → Read … → storyboard__publish
```

You do not run the renderer: call `storyboard__preview`, then Read the PNG paths the hook
reports (every step, first and last included). The canvas skill covers the critique and
the manual `render_preview.py` fallback.

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
  - `timestamp_mismatch`: a derive combines points at different timestamps from
    different groups of one receipt (or from two receipts over overlapping windows).
  - `sparse_groups_at_timestamp`: a scene picks several groups of a grouped series at one
    timestamp each, and a group sits off the timestamp most of them share. In the first
    real run a rolling `[24h]` count had no 07:40 point for KittyCard, so the scene mixed
    its 07:35 point with the others' 07:40. Prefer a `latest_only` query (below), which
    names stale groups outright, and disclose any group that stays off the shared time. Ignore it for deliberate per-group moments, such as each group's peak.
- A `preview_bundle` is valid for **one revision**: after any edit, preview again.
- If the hook says there is no local Chrome, say once that previews were skipped, then
  continue.
- **Publishing is final.** A published storyboard is immutable, and it retains every
  receipt it cites. To revise one, create a new storyboard.

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
- `detect_anomalies` in logs mode over 24h returns data again (it used to answer "No data
  in either window").

## Handing it over

Give the user the `view_url`. It is absolute when the Cardinal install sets
`MAESTRO_BASE_URL` (app.cardinalhq.io does). A self-hosted install may return an
app-relative path; prefix it with the Cardinal host the user is connected to. Draft
storyboards are visible to org members at the same link. Say which scenes stayed `open`
and why.
