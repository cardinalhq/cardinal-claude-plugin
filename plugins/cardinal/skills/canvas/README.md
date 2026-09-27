# canvas

The visual half of [Investigation Storyboards](../storyboard/README.md). Claude draws each
scene's Canvas: free-form HTML/SVG/d3 or Cardinal's prefabs, in a sandboxed frame that
only ever receives evidence Cardinal resolved. Claude then **renders every scene on your
machine** to check that the point is obvious before publishing.

`exemplars/` holds three worked Canvas surfaces from Cardinal's own spike. Each one's
header lists the bindings it expects. Claude reads them for technique and does not copy them.

## Local preview

Every time Claude calls `storyboard__preview`, the plugin's `PostToolUse` hook
(`hooks/storyboard-preview.py`) runs `scripts/render_preview.py` on the result and tells
Claude where the PNGs are. The hook is bounded (the renderer stops at 2 minutes, the hook
answers within 150 s) and never fails the tool call; without a usable Chromium it says
so once per session.

`scripts/render_preview.py` (Python 3.9+, standard library only) takes the result of
`storyboard__preview` and does the following:

1. Downloads each scene's self-contained preview page from your Cardinal, using the key
   `/cardinal:connect` stored. It only ever sends that key to the Cardinal you are
   connected to, and it checks every page's size and SHA-256 against the preview result.
2. Opens the page in **your local Chrome/Chromium**, headless, with its OS sandbox **on**,
   networking locked off (no DNS, all connections to a dead proxy, every request refused),
   a throwaway profile, UTC and reduced motion.
3. Steps through the scene's reveal steps and writes one PNG per step to
   `~/.claude/cardinal/storyboards/<storyboard>/r<revision>/` (files `0600`). Claude reads
   the PNGs and critiques them. The temporary page copies and the browser profile are
   deleted afterwards.

Nothing is rendered on Cardinal's servers, and publishing does not depend on a preview.

| Platform | Status |
|---|---|
| macOS | Supported: Google Chrome, Chromium, Chrome Canary, Brave, Edge, or a Playwright/Puppeteer download. |
| Linux | Supported where Chromium's sandbox can start (unprivileged user namespaces). Many containers and locked-down images can't. The renderer then says so and Claude authors without previews. It never turns the sandbox off. |
| Windows | Not supported yet: Claude authors without previews. |

Point it at a specific browser with `CARDINAL_CHROMIUM=/path/to/chrome`.

Run by hand (the hook normally does this; Claude falls back to it for the dark theme, a
subset of scenes, or a run that timed out):

```bash
python3 scripts/render_preview.py --from-json preview.json [--scene <id>] [--theme dark]
python3 scripts/render_preview.py --html page.html        # a bundle page saved locally
```

Exit codes: `0` ran, with per-scene results on stdout; `2` connection, auth or fetch
problem, with the reason in the summary; `3` no usable local Chromium, so the preview is
skipped.
