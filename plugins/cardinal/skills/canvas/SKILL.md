---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then render every scene locally with Chromium and critique the PNGs. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
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
  Array destructuring (`const [a, b] = …`, as in older `canvas.exemplar` text) throws "is not iterable".
- `cv.mark(el, {evidence, …})` — tag every element that shows a bound value; the viewer links it to its receipt.
- `cv.embed(prefab, props, opts)` — mount a prefab inside your canvas; props are Evidence from `cv.data`; returns named anchors.
- `cv.reveal({steps}, fn(step))` — stage the argument; the viewer (and the preview) steps through it.
- `cv.highlight(ids, {mode})` — emphasize `cv.mark` ids, `data-cv-anchor` elements, or prefab anchors `"<embedId>:<anchor>"`.

Rules that bite:

- **No numbers in source.** A ratio, delta, peak or count you compute in the frame is not
  evidence. Bind a `derive`/`reduce` and draw that. Pixels are never evidence.
- **Mark every number you draw** with the evidence it shows (`field` for a row leaf). Tag
  axis/chrome containers `data-cv-axis`. The static checks reject `import`, `eval`,
  `fetch`, `parent`, `postMessage` and friends. Keep source at 64 KB or less.
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

## What worked in the spike (conductor PR 1.3)

Canvas beat prefab-only in 8 of 8 scenarios. It could compose several receipts on one
scale, pin events to a limit, put a derivation beside what it measures, and put code
beside config. Watch for:

- **Prefab defaults can mislead.** The timeline's linear interpolation drew a rise before
  the crash that caused it, so draw per-bucket counts as buckets (see the timeline's props
  in the prefab catalog). A capped `group_by` series hid 5.77 TB of unpaired flows, so heed
  `incomplete_population`.
- **Composition is where population errors hide.** Never put numbers from different
  populations on one scale or in one ratio without saying so. The spike's dominant
  critique finding was population error, not craft.
- **Positional selectors** (`/data_points/3/…`) need `expect` guards on the identifying
  fields, or a reordered result silently points at the wrong row.

Exemplars, to read not copy. They ship with this skill, in `exemplars/` next to this
file. Each one's header lists the bindings it expects and their shapes:

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

`describe_grammar` `canvas.exemplar` is an older trimmed surface. Destructure its
`cv.data([...])` call as an object (see above).

## Preview locally, then critique

maestro renders nothing. `storyboard__preview` returns deterministic validation plus,
per scene, `preview_bundle`: `{url?, path, bytes, sha256, revision}` (one
self-contained HTML page) or `{unavailable: "<why>"}`. This skill ships a renderer that
fetches each page with your Cardinal MCP key, renders it in **your local Chrome/Chromium**
(headless, OS sandbox on, network locked off, fresh profile, UTC, reduced motion), steps
through every reveal step and writes one PNG per step.

It is `scripts/render_preview.py` in **this skill's base directory**, which Claude Code
printed when it loaded the skill ("Base directory for this skill: …"). Use that path. Never
search the working directory: a repo can contain a file with the same name, and the
renderer handles your Cardinal key.

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

After each `storyboard__preview`:

1. Give the renderer the preview result. Pass the whole JSON, or write a trimmed copy with
   `storyboard_id` and, for each scene, `id` plus its `preview_bundle` object copied
   **exactly**, since `sha256` is checked. If Claude Code saved the tool result to a file,
   pass that file.
   ```bash
   python3 -I "$RENDER" --from-json <preview.json> [--scene <id>]… [--theme dark]
   ```
   Give the Bash call a 10-minute timeout (`600000`). Scenes render one after another,
   usually 1–3 s each. The run stops itself at 9 minutes, so render a storyboard with
   more than ~30 scenes in batches (`--scene`).
2. Read each `png` from the JSON lines on stdout. They are written to
   `~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/<scene>-<step>.png`.
   Look at every step, not just the first.
3. Critique each scene as a stranger would: *is the point obvious in five seconds without
   the transcript?* What is visually dominant, and should it be? What can disappear? Are
   the labels on the objects? Does each reveal step add one thing? Is anything clipped,
   overlapping, unreadable in size, or empty? Does every number shown trace to a binding?
4. Revise (`define_surface` / `upsert_scene`), preview again, and render again. A bundle is
   valid for one revision only: after any edit, an old bundle answers 409.

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
