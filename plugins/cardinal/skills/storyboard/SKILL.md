---
name: storyboard
description: Turn a finished (or stalled) Cardinal investigation into an Investigation Storyboard — an evidence-bound, scene-by-scene explanation with its own interactive visuals, published in Cardinal and shareable by link. Use whenever the user wants to explain, write up, present, share, hand off, post-mortem or "storyboard" what an investigation found (an incident, a regression, a cost jump, a canary verdict), or asks for a visual walkthrough of the evidence — even if they only say "write this up for the team" or "show me how we got here" after using Cardinal tools. Covers the storyboard__* tools (describe_grammar, find, create, add_act, define_surface, upsert_scene, preview, publish, get_receipt), receipts and captured evidence, and the Claude Code preview → critique → publish loop. Not for dashboards or ongoing monitoring.
---

# storyboard — explain the investigation

Every investigation gets its own interface. You author the view and the argument; Cardinal
owns the runtime and the evidence. This skill is the storytelling half; the **canvas** skill
is the visual half and covers the local previews.

> Rendered pixels may suggest a hypothesis but never establish a factual claim. Any quantitative or population-level statement must resolve to evidence from a receipt or deterministic derivation over receipts.

> Preview every scene before publishing. Inspect whether the intended point is visually obvious without reading the investigation transcript. Revise the presentation when it is not.

Publish trust is deterministic: maestro checks specs, bindings, receipts, derived values and
the numbers in each statement. It never renders anything. A skipped preview is a quality
problem, not a trust violation.

## Fetch the guides first

Before the first storyboard tool call, call `storyboard__describe_grammar` with
`{section: "authoring"}` and `{section: "evidence"}`, and `{section: "canvas"}` before
drawing (its `canvas.design` is the design guide). Fetch the reference (no section, or
`schema` / `bindings` / `prefabs` / `libraries` / `rules`) as you need it. They are the
only source for scene titles, claims, units, bindings, warnings, the pre-publish critique
and handover. Follow them; do not work from memory. This skill adds only what is specific
to Claude Code with the Cardinal plugin.

Needs Cardinal (maestro) newer than v1.97.16. An older one rejects `section: "authoring"`
as invalid: ask the user to upgrade.

No `storyboard__*` tools means the plugin is not connected (writes need an API key; there
is no sign-in flow). Tell the user once: sign up at https://app.cardinalhq.io, run
`/cardinal:connect` and approve it in the browser (it stores an API key; self-hosted:
`--host <url>`), then restart Claude Code.
Captured evidence stays on this machine meanwhile and can be cited after connecting.

## Evidence in Claude Code

- **Witnessed.** Every read-only Cardinal tool result ends with `[receipt:rcpt_<24 hex>]`.
  Note receipt ids as you investigate.
- **Captured.** Any tool result from this session can be cited: shell commands (tests,
  `git`, `make`), file reads and edits, searches, web fetches, subagents, other MCP
  servers, any tool. The plugin's hook keeps each result on this machine and prints
  `[evidence:ev_…]`. Nothing is uploaded until a storyboard cites it. Lost an id? Run
  `cardinal-evidence find <text>`. After `create`, promote the ones you cite before binding
  them, only those, as you write the scene that cites them:
  `cardinal-evidence promote --storyboard <id> ev_… [ev_…]` prints `ev_… -> rcpt_…` per
  entry (or its error); bind that receipt. Exit 1 means some entries failed: read the errors.
  An entry promoted before prints its existing receipt (`already promoted`): reuse that id.
- **Withheld.** `[evidence:ev_… withheld: …]` means the call touched something sensitive
  (a `.env`, a key, a credential command). Nothing was kept and it cannot be cited: say so
  plainly instead of paraphrasing its result.
- **Redacted is not withheld.** A captured result showing `[redacted]` was kept with its
  secrets masked: cite it and call it redacted, never withheld; claim nothing about masked values.
- **Never claim more than the output shows.** A captured result is the client's record,
  labeled "reported by <client>". Exit 0 shows a command succeeded, not that the feature
  works. `cardinal-evidence show ev_…` prints what you would cite; bind the exact field
  (`/exit_code`, a `stdout` line) that carries the claim.
- **Reported** (`storyboard__record_evidence`) is for a result with no `ev_…` id, for
  example when the user turned capture off (`cardinal-evidence status`). Prefer captured.

## Update, don't duplicate

If `storyboard__find` is listed, on every storyboard request, before `storyboard__create`:
1. A plugin hook adds `session_id` and `context` (where you write from) when you leave them
   out. A result without context: pass `cardinal-storyboard context`'s `{"context": {…}}`.
2. Call `storyboard__find {session_id, context}` (plus `refs`, if listed, for PRs, commits,
   files or issues named) and follow its `rule`: continue without asking only on
   `may_continue` true (older Cardinal: continue silently only an `open_act` with
   `same_session` and `yours` both true).
3. Otherwise ask (below). All acts published: `storyboard__add_act {storyboard_id, title,
   session_id, context}`; an open act you can write: continue it. New, or no
   `storyboard__add_act` listed: `storyboard__create` with `context`.
4. If storyboard__create lists `about`: `{checkout: true}` only when it explains this
   branch's or PR's change (ask if unsure; never for an incident); else the PRs, commits,
   issues, paths and links it explains. After opening or merging that PR:
   `storyboard__link {storyboard_id, add: {prs: [N]}}` (or `{commits: [<merge sha>]}`).
5. Publish answers `public_links_decision_required`: ask the person if public links should
   show this act, unless they already said to update what they shared (that covers only
   this choice); publish again with `public_links: "extend"` or `"keep"`.
6. `raw_evidence_confirmation_required`: always ask the person, listing the bindings it names;
   never set `confirm_raw_evidence` yourself, even if they said to update what they shared.
   Only their yes sends `confirm_raw_evidence: true`.

No `storyboard__find` (an older Cardinal): create without `context`.

### Ask before adding to an existing storyboard

Skip it if the person named a target (an `sb_` id, a link, "the storyboard for PR …").
Offer strong matches (`match` session, or `match_role` about), then written_from ones, at
most 3, in find's order; with none, at most 1 weak match (repo, workdir, actor) updated in
the last 7 days. None: create without asking. Ask with AskUserQuestion (else a numbered
question; wait):
- label `Add to "<question, first 40 chars>"`, or for someone else's open act
  `Continue open act <n> of "<question>"`
- description `<why> · <act_count> acts · updated <relative time>`; `<why>` names the role:
  `same session`, `about <kind> <value>`, `written from branch <branch>`, `written from
  the checkout of PR <repo>#<pr_number>`, `same repo <repo>`, `your storyboard`. Never
  call a written_from match the same PR.

Always add `Start a new storyboard`. Can't ask (`claude -p`): create; name the best match
in your final message ("say: add this to <storyboard>").

## Claude Code specifics

- **session_id:** a SessionStart hook puts this session's id in your context ("Cardinal
  session id for this session: …"). Pass it to `storyboard__create`, `storyboard__find` and
  `storyboard__add_act` (the context hook adds it if you forget).
- **The preview loop is local.** After every `storyboard__preview`, a plugin hook renders
  each scene with your local Chromium and reports the PNG paths. Read every PNG (every
  reveal step, the first and last included) and critique it against the authoring guide
  before you revise. You do not run the renderer; the canvas skill covers the critique
  and the manual fallback. If the hook says there is no local Chrome, say once that
  previews were skipped, then continue: it is never a publish blocker.

```
investigation (note rcpt_ / ev_ ids)
  → describe_grammar {authoring, evidence} → find (context stamped) → ask before adding
  → storyboard__create or add_act → promote
  → define_surface / upsert_scene → storyboard__preview
    ↳ plugin hook: local Chromium → PNG per scene per reveal step
  → Read every PNG → critique → revise → preview … → critique the whole → storyboard__publish
```

Publishing is final: published acts are immutable; storyboard__add_act adds the next act to
the same storyboard and link. Hand over the `view_url`; if it is app-relative (a
self-hosted install without `MAESTRO_BASE_URL`), prefix the Cardinal host.

## The card

The card is an act's link preview: what a posted link shows. When `storyboard__publish`
lists `card` (an older Cardinal rejects it), pass it once the scenes are clean:
- `headline`: the conclusion, one sentence, at most 140 characters. Every number in it
  must be bound in this act, or publish refuses.
- `headline_figure`: one bound number. `cover_scene`: the scene that shows the conclusion;
  avoid scenes that bind raw rows.
- Preview with `card`, Read the cover and the link-preview mock the hook reports, then
  publish with the same `card`. After publish a hook uploads your render of the cover (or
  hero) scene and says which image link previews use.

Member link previews are on by default. Tell the person what a posted link shows
(question, headline, counts, verdict, image). If they don't want it, pass
`link_preview: false` when `storyboard__publish` lists `link_preview`, or use
`storyboard__share` when its action lists `link_preview`.
