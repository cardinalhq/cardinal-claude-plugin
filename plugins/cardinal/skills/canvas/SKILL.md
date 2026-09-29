---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs the plugin renders locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

A Canvas is the visual of one storyboard scene: a surface's JavaScript source runs in a
sandboxed iframe (`sandbox="allow-scripts"` only, no network) and receives **only** the
evidence bindings maestro resolved. You own every pixel; you own none of the numbers.

## Fetch the guide first

Before you write or revise a surface, call `storyboard__describe_grammar {section:
"canvas"}`. It is the design guide (`canvas.design`: the doctrine, visualize the finding,
the reveal-step rules, the five cv operations, the rules that bite, composition pitfalls)
and the reference (`canvas.api`, `canvas.static_checks`, `canvas.design_tokens`,
`canvas.exemplar`). Fetch `{section: "prefabs"}`, `{section: "libraries"}` and
`{section: "bindings"}` (source · select · ref · derive · reduce · extract) as you need
them. Follow them; do not work from memory. This skill adds only what is specific to
Claude Code with the Cardinal plugin: the exemplar files and the local preview loop.

## Exemplars: technique, not shape

They ship in `exemplars/` next to this file; each header lists the bindings it expects.
They teach binding, marking, revealing and embedding, not what a scene should look like.
Read them; do not copy them.

- `exemplars/scalar-and-series.js`: the smallest complete surface. Marked numbers, a
  line, two reveal steps, and the object-destructured batch `cv.data`.
- `exemplars/roof-and-timeline.js`: draw the physical thing, `cv.embed` the timeline
  prefab under it, and a callout to its `window:outage` anchor. Also `cv.highlight` on
  marks and prefab anchors, and one surface shared by two scenes (`cv.onUpdate`).
- `exemplars/cohort-rows.js`: a population drawn one row per member, with one shared
  scale and counts derived over whole receipts. For thousands of rows, draw on a
  `<canvas>` instead.

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
2. Critique each PNG against `canvas.design` and the authoring guide's critique, as a
   stranger would: *is the point obvious in five seconds without the transcript?* Would a
   reader who stops at this step understand it? Do the legend and annotations match what
   is drawn at this step? Is anything clipped, overlapping, unreadable in size, or empty?
   Judge the rendered result, not the source code.
3. Revise (`define_surface` with `edits` / `upsert_scene`), then `storyboard__preview`
   again. The hook renders the new revision. A bundle is valid for one revision only:
   after any edit, an old bundle answers 409.

A scene the hook reports with an `ERROR` or `frame errors` needs a fix in its source or
spec, not a re-render: `frame errors` are exceptions thrown by your source, including a
prefab's `prefab "<name>" (embed "<id>"): <reason>`. A scene reported as not rendered
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
protocol_errors}`; the last line is `{"summary": {...}}`. An `error` needs a fix, not a
re-render; `{unavailable}` scenes echo the reason preview reported.

**Exit codes:**

| Exit | Meaning | Do |
|---|---|---|
| 0 | Ran. Some scenes may still carry errors or be unavailable. | Read the PNGs, fix what the lines report. |
| 2 | Nothing rendered: input, connection or fetch problem (not connected, wrong org, viewer role, stale revision, busy, "upgrade Cardinal"). | Relay the `summary.message`. For stale revision or missing dataset, run `storyboard__preview` again and re-render. |
| 3 | No usable local Chrome/Chromium (112+), Windows, or Chromium could not start with its sandbox on (e.g. Linux without user namespaces). | **Skip the preview, tell the user once, keep authoring.** Never treat it as a publish blocker. |

Chromium is found via `$CARDINAL_CHROMIUM` (authoritative), `$PUPPETEER_EXECUTABLE_PATH`,
`$CHROME_PATH`, the usual Chrome/Chromium/Canary/Brave/Edge install locations, then the
Playwright and Puppeteer caches. Canvas code is hostile and this runs on the user's
machine, so the renderer keeps Chromium's sandbox on, launches it with the Canvas network
lock, attaches the out-of-process Canvas frame, fails every request that is not `data:`,
`blob:`, `about:` or the page itself, and closes a Canvas that creates child frames,
starts workers or navigates.
