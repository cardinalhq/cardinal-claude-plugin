---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs the plugin renders locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

A Canvas is the visual of one storyboard scene: a surface's JavaScript source runs
in a sandboxed iframe (`sandbox="allow-scripts"` only, hash-only CSP, no network,
no storage, no parent access) and receives **only** the evidence bindings maestro
resolved. You own every pixel; you own none of the numbers.

The **storyboard** skill decides what each scene argues. This skill decides how the
argument looks and makes sure you have seen it rendered.

## Canvas design doctrine

> **Canvas**
>
> Canvas is a blank, interactive visual surface. When stock components are insufficient, use it freely.
>
> Act like an exceptional UI and information designer. Do not default to charts, boxes, tables, or generic node graphs
> just because they are easy to generate.
>
> Ask: *If I had complete control over the pixels, what would make this finding obvious in five seconds?*
>
> Build that.
>
> You may draw physical objects, architecture, topology, timelines, flows, code, infrastructure, annotations, overlays,
> animations, or entirely novel visualizations. Combine representations when useful.
>
> Prefer direct visual explanation over legends and prose. Put information where it belongs: status on the object,
> latency on the path, failure on the component, change at the point it occurred.
>
> Use hierarchy aggressively. Make the important thing visually dominant and supporting context quiet. Remove anything
> that does not help establish the scene's statement.
>
> Preserve useful spatial context across scenes and progressively reveal, highlight, annotate, or transform it when that
> makes the story easier to follow.
>
> The Canvas receives evidence-backed data. You have complete freedom over its presentation, but no freedom to invent or
> alter the underlying evidence.
>
> Always preview the Canvas. Judge the rendered result, not the source code. Iterate until a viewer can understand the
> scene's point without reading the investigation transcript.

### Visualize what was learned, not only the telemetry

A dashboard shows telemetry; a scene shows what was learned from it. Prefer visualizing
the conclusion or reasoning over merely visualizing the raw telemetry. The underlying
telemetry should remain inspectable as evidence. The five-seconds question above stays the
test.

Charts are right when the shape of the data is the argument (a step, a trend, a
distribution, a gap between two lines); use them freely then. When the finding is a
relationship, a composition, a mechanism or a chain of reasoning with an unsettled link,
draw that, and keep the series it rests on marked or embedded where a reader can inspect
it. Of every visual ask: *is this chart the argument, or a convenience?* A scene no
dashboard would have had is what Canvas is for.

### Every reveal step stands on its own

The viewer opens each scene on its last step with a Replay control, but a reader who
replays, or stops early, sees every step. Do not rely on the last step.

- **Every reveal step is a complete, correct picture.** Step 1 already carries the
  scene's point. Later steps add emphasis or detail; they never hold the point back.
  (Failure seen: step 1 drew only "attempted" bars, and the offender's rejections and
  highlight arrived at step 2.)
- **Legends, axis labels and annotations describe only marks already drawn at that
  step.** (Failure seen: a step-1 legend listed red and blue "rejected" bars that were
  drawn at steps 2–3.)
- **Never `cv.highlight` with `dim-others` or `isolate` on a step where the other marks
  are evidence the statement cites.** Use `highlight`. (Failure seen: `dim-others` on one
  path faded the hourly bars that proved "flat all day" to 22% opacity.)
- **Keep reveal steps to 1–3.** Prefer 1 when the point is a single comparison.
- **Review every step PNG, the first and the last explicitly**, and ask of each: *would a
  reader who stops here understand the scene's point?*

## The runtime (fetch the details, don't guess them)

`storyboard__describe_grammar` is the reference, built from the frozen sources so it
cannot drift. Fetch the section you need rather than recalling it:

| Need | Call |
|---|---|
| `cv` API signatures, frame rules, static-check list, `--cv-*` design tokens, a working exemplar | `describe_grammar {section: "canvas"}` (`canvas.api`, `canvas.static_checks`, `canvas.design_tokens`, `canvas.exemplar`) |
| Prefab catalog: props, config keys, anchor ids | `{section: "prefabs"}` |
| Approved libraries (d3, Plot, dagre, elk, icons…) and their globals | `{section: "libraries"}` — to use `cv.embed` the surface must list the `prefabs` pseudo-library |
| Binding shapes (source · derive · reduce · extract), selectors, `expect` | `{section: "bindings"}` |
| Authoring rules, caps, the local preview contract | `{section: "rules"}` |

The five operations, in one line each (signatures in `canvas.api`):

- `cv.data(key | [keys])` — async; the only way data enters the frame (Evidence objects; datasets page in lazily).
  The array form resolves to an **object keyed by binding key**: `const {p99, series} = await cv.data(["p99", "series"])`.
- `cv.mark(el, {evidence, …})` — tag every element that shows a bound value; the viewer links it to its receipt.
- `cv.embed(prefab, props, opts)` — synchronous; mount a prefab inside your canvas; props are Evidence from `cv.data`;
  returns a handle whose `el` you must append, and named anchors.
- `cv.reveal({steps}, fn(step))` — stage the argument; the preview renders every step, and the viewer opens on the
  last one with a Replay control. Every step must stand on its own (above).
- `cv.highlight(ids, {mode})` — emphasize `cv.mark` ids, `data-cv-anchor` elements, or prefab anchors `"<embedId>:<anchor>"`.

Rules that bite:

- **No numbers in source.** Arithmetic that combines values (a ratio, delta, share, peak or
  count) done in the frame is not evidence: bind a `derive`/`reduce` and draw that.
  Formatting one bound value for display (scaling ms to minutes, rounding, separators, a
  unit label) is presentation: do it in the frame and `cv.mark` that element with the
  value's evidence, so the exact measurement stays inspectable. Pixels are never evidence.
- **Mark every number you draw** with the evidence it shows (`field` for a row leaf). Tag
  axis/chrome containers `data-cv-axis`. The static checks reject `import`, `eval`,
  `fetch`, `parent`, `postMessage` and friends. Keep source at 64 KB or less.
- **`cv.embed` is synchronous, and you must append its element.** `const h = cv.embed(…);
  cv.root.append(h.el)` (or append it into your own layout) before the first settle. To
  stage an embed across reveal steps, hide it with `visibility` or `opacity`; never append
  it at a later step and never use `display: none`. A detached embed, a mount or render
  that throws, props the prefab cannot draw, or no layout within 2 s all show up as the
  frame error `prefab "<name>" (embed "<id>"): <reason>` in that scene's `frame errors`
  (the render still finishes). Fix the source; re-rendering will not help.
- A prefab scene (`presentation.kind: "prefab"`) is one `cv.embed`. Its config is
  presentational only: a y-domain or threshold is data, so bind it.
- Consecutive scenes that name the same surface share one frame. Use `cv.onUpdate` to
  transform the world rather than redraw it.
- **Past a few thousand marks, draw on a `<canvas>`, not in SVG.** A dataset binding can
  hold up to 100k rows. One SVG node (and one `cv.mark`) per row blows the frame's ready
  and per-step settle budgets, so the preview reports an error and no PNG, and the viewer
  then lays out and hit-tests every node. Paint the population onto a `<canvas>` (d3
  scales work unchanged) and `cv.mark` the canvas element once with the dataset Evidence,
  or bind a `reduce`/`derive` and draw the summary. Keep per-element SVG and `cv.mark` for
  the few marks the argument points at.

## Where compositions go wrong

- **Prefab defaults can mislead.** The timeline's linear interpolation drew a rise before
  the crash that caused it, so draw per-bucket counts as buckets: pass the timeline
  `bucketed: true` with its `step` (see `{section: "prefabs"}`). A capped `group_by` series
  hid 5.77 TB of unpaired flows, so heed `incomplete_population`.
- **Composition is where population errors hide.** Never put numbers from different
  populations on one scale or in one ratio without saying so.

## Exemplars: technique, not shape

They, and the grammar's `canvas.exemplar`, teach binding, marking, revealing and
embedding, not what a scene should look like. Read them; do not copy them. They ship in
`exemplars/` next to this file, and each header lists the bindings it expects:

- `exemplars/scalar-and-series.js`: the smallest complete surface. Marked numbers, a
  line, two reveal steps, and the object-destructured batch `cv.data`.
- `exemplars/roof-and-timeline.js`: draw the physical thing (a roof of panels coloured
  by production), `cv.embed` the timeline prefab under it, and a callout to its
  `window:outage` anchor. Also covers `cv.highlight` on marks and prefab anchors, and
  one surface shared by two scenes (`cv.onUpdate`).
- `exemplars/cohort-rows.js`: a population drawn one row per member, with probing for
  numbered bindings, one shared scale, and a sentence whose counts are derived over
  whole receipts. One SVG row per member suits tens of rows; for thousands, use a
  `<canvas>` (see above).

## Preview, then critique

maestro renders nothing. `storyboard__preview` returns deterministic validation plus,
per scene, `preview_bundle`: `{url?, path, bytes, sha256, revision}` (one
self-contained HTML page) or `{unavailable: "<why>"}`.

**The plugin renders the bundles for you.** After `storyboard__preview` returns, the
Cardinal plugin's PostToolUse hook passes the result to this skill's renderer, which
fetches each page with your Cardinal MCP key, renders it in **your local Chrome/Chromium**
(headless, OS sandbox on, network locked off, fresh profile, UTC, reduced motion), steps
through every reveal step and writes one PNG per step. The hook then reports in your
context the PNG directory
(`~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/`), each scene's files
(`<scene>-<step>.png`, step 0 first) and any per-scene render error. You do not build
the renderer's input.

After each `storyboard__preview`:

1. **Read every PNG the hook reported**, every step of every scene, the first and the
   last explicitly. A preview scoped with `scene_ids` renders only those scenes; the
   other scenes' PNGs stay in the directory of the revision they were last rendered at.
2. Critique each scene as a stranger would: *is the point obvious in five seconds without
   the transcript?* Would a reader who stops at this step understand it? Does the visual
   explain the finding, or display telemetry the reader must interpret? What is visually
   dominant, and should it be? What can disappear? Are the labels on the objects? Does
   each reveal step add one thing, without withholding the point? Do the legend and
   annotations match what is drawn at this step? Is anything clipped, overlapping,
   unreadable in size, or empty? Does every number shown trace to a binding?
3. Revise (`define_surface` with `edits` / `upsert_scene`), then `storyboard__preview`
   again. The hook renders the new revision. A bundle is valid for one revision only:
   after any edit, an old bundle answers 409.

A scene the hook reports with an `ERROR` or `frame errors` needs a fix in its source or
spec, not a re-render: `frame errors` are exceptions thrown by your source, including the
`prefab "<name>" (embed "<id>"): <reason>` errors above. A scene reported as not rendered
(`unavailable: …`) has errors in `storyboard__preview`'s own result: fix those.

If the hook says there is no usable local Chrome/Chromium, it says so once per session.
**Skip the preview, tell the user once, keep authoring.** Never treat it as a publish
blocker. If it says the Cardinal connection or fetch failed, relay its message.

### Rendering by hand (fallback)

Run the renderer yourself when the hook timed out or left scenes unrendered, for the dark
theme, or to re-render a subset. It is `scripts/render_preview.py` in **this skill's base
directory**, which Claude Code printed when it loaded the skill ("Base directory for this
skill: …"). Use that path. Never search the working directory: a repo can contain a file
with the same name, and the renderer handles your Cardinal key.

```bash
RENDER="<this skill's base directory>/scripts/render_preview.py"
```

If you do not have the base directory, take the newest installed Cardinal plugin's copy
(chosen by version, not mtime), and fall back to a personal-skill install. Keep the `-I`:
without it Python imports `glob`/`os`/`re` from the working directory first, so a repo's
own `glob.py` would run here:

```bash
RENDER=$(python3 -I -c 'import glob, os, re
h = os.path.expanduser("~/.claude")
ver = lambda p: [int(n) for n in re.findall(r"\d+", p.split(os.sep)[-5])]
c = sorted(glob.glob(h + "/plugins/cache/*/cardinal/*/skills/canvas/scripts/render_preview.py"), key=ver)
c = c[-1:] or glob.glob(h + "/skills/canvas/scripts/render_preview.py")
print(c[0] if c else "")')
[ -f "$RENDER" ] || echo "canvas render_preview.py not found"
```

Give it the preview result: the whole JSON, or a trimmed copy with `storyboard_id` and,
for each scene, `id` plus its `preview_bundle` object copied **exactly**, since `sha256`
is checked. If Claude Code saved the tool result to a file, pass that file.

```bash
python3 -I "$RENDER" --from-json <preview.json> [--scene <id>]… [--theme dark]
```

Give the Bash call a 10-minute timeout (`600000`). Scenes render one after another,
usually 1–3 s each. The run stops itself at 9 minutes, so render a storyboard with more
than ~30 scenes in batches (`--scene`). Read each `png` from the JSON lines on stdout.

Per-line output: `{scene_id, step, steps, png, state, height, error, frame_errors,
protocol_errors}`. The last line is `{"summary": {...}}`. A scene with an `error`
(`frame_errors` are exceptions thrown by your source) needs a fix, not a re-render.
`{unavailable}` scenes are echoed with the reason: fix the errors preview reported for
them.

**Exit codes:**

| Exit | Meaning | Do |
|---|---|---|
| 0 | Ran. Some scenes may still carry errors or be unavailable. | Read the PNGs, fix what the lines report. |
| 2 | Nothing rendered: input, connection or fetch problem (not connected, wrong org, viewer role, stale revision, busy, "upgrade Cardinal"). | Relay the `summary.message`. For stale revision or missing dataset, run `storyboard__preview` again and re-render. |
| 3 | No usable local Chrome/Chromium (112+), Windows, or Chromium could not start with its sandbox on (e.g. Linux without user namespaces). | **Skip the preview, tell the user once, keep authoring.** Never treat it as a publish blocker. |

Chromium is found via `$CARDINAL_CHROMIUM` (authoritative), `$PUPPETEER_EXECUTABLE_PATH`,
`$CHROME_PATH`, the usual Chrome/Chromium/Canary/Brave/Edge install locations, then the
Playwright and Puppeteer caches. The renderer never disables Chromium's sandbox and
never passes `--allow-file-access-from-files`. It launches Chromium with the Canvas
network lock (`--host-resolver-rules="MAP * ~NOTFOUND" --proxy-server=127.0.0.1:9
--proxy-bypass-list="<-loopback>"`). It auto-attaches the Canvas frame, which current
Chrome runs as its own out-of-process target, and fails every request from the page or
the frame that is not `data:`, `blob:`, `about:` or the page itself. It closes a Canvas
that creates child frames, starts workers or navigates (`about:blank` included). Chrome
runs the frame's first parse before the renderer can attach, so for that brief window
the network lock and the frame's hash CSP block requests, and the frame tree catches
frames and navigations still present when the renderer attaches. Canvas code is
hostile, and this runs on the user's machine.

Skipping the preview is a quality problem, not a trust problem: publish validation is
deterministic and does not look at pixels.
