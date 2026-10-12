---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs the plugin renders locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

Follow [CORE.md](CORE.md), co-located in this directory, for the Canvas workflow: fetch the
`canvas` guide first, the exemplars, and the preview-then-critique loop. This file adds what
is specific to Claude Code.

## The hook renders for you

**The plugin renders the bundles for you.** After `storyboard__preview` returns, the
Cardinal plugin's PostToolUse hook passes the result to the storyboard skill's renderer,
which renders every scene and reveal step. The hook then reports in your context the PNG
directory (`~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/`), each scene's
files (`<scene>-<step>.png`, step 0 first) and any per-scene render error, then the cover
and the link-preview mock when the preview carried `card`. You do not build the renderer's
input. Read every PNG it names, as CORE.md says.

## Rendering by hand (fallback)

Run the renderer yourself when the hook timed out or left scenes unrendered, for the dark
theme, or to re-render a subset. It is `render_preview.py` in the **storyboard** skill's
`scripts/`, next to this skill's base directory, which Claude Code printed when it loaded
the skill ("Base directory for this skill: …"). Use that path. Never search the working
directory: a repo can contain a file with the same name, and the renderer handles your
Cardinal key.

```bash
RENDER="<this skill's base directory>/../storyboard/scripts/render_preview.py"
```

If you do not have the base directory, take the newest installed Cardinal plugin's copy
(chosen by version, not mtime), and fall back to a personal-skill install. Keep the `-I`:
without it Python imports `glob`/`os`/`re` from the working directory first, so a repo's
own `glob.py` would run here:

```bash
RENDER=$(python3 -I -c 'import glob, os, re
h = os.path.expanduser("~/.claude")
ver = lambda p: [int(n) for n in re.findall(r"\d+", p.split(os.sep)[-5])]
c = sorted(glob.glob(h + "/plugins/cache/*/cardinal/*/skills/storyboard/scripts/render_preview.py"), key=ver)
c = c[-1:] or glob.glob(h + "/skills/storyboard/scripts/render_preview.py")
print(c[0] if c else "")')
[ -f "$RENDER" ] || echo "storyboard render_preview.py not found"
```

Give it the preview result (CORE.md, "The renderer's output"). If Claude Code saved the
tool result to a file, pass that file. `--runtime claude` uses this Claude Code's Cardinal
connection and writes to `~/.claude/cardinal/storyboards/`.

```bash
python3 -I "$RENDER" --from-json <preview.json> [--scene <id>]… [--theme dark] --runtime claude
```

Give the Bash call a 10-minute timeout (`600000`). The run stops itself at 9 minutes.
