---
name: storyboard
description: Improve, frame, publish and share the live Investigation Storyboard Cardinal already keeps for this session — an evidence-bound, scene-by-scene explanation with interactive visuals. Cardinal creates every connected session's Investigation and private live Storyboard itself, so the user never starts one: they work normally and may ask for the storyboard link any time (give the URL from the session-start context; no skill needed). If that context says Cardinal keeps it current, explaining it needs no skill either. Use when the user wants the storyboard to explain, present, hand off or post-mortem what the investigation found (an incident, a regression, a cost jump, a canary verdict), or to add to another storyboard. Covers the storyboard__* tools, receipts and captured evidence, and the Claude Code preview → critique → publish loop. Not for dashboards or ongoing monitoring.
---

# storyboard — improve this session's live storyboard

Cardinal created this session's Investigation and live Storyboard at session start (the
session-start context names both, with the private URL). This skill improves how it
explains the investigation; the **canvas** skill is the visual half and covers previews.

"Do I invoke the storyboard skill before I ask my question?" No. You never need to start
Storyboards. This session already has an Investigation and Storyboard. Work normally, and
ask for the Storyboard whenever you want to see or share the investigation.

> Rendered pixels may suggest a hypothesis but never establish a factual claim. Any quantitative or population-level statement must resolve to evidence from a receipt or deterministic derivation over receipts.

> Preview every scene before publishing. Inspect whether the intended point is visually obvious without reading the investigation transcript. Revise the presentation when it is not.

## Fetch the guides first

Before the first storyboard tool call, call `storyboard__describe_grammar` with
`{section: "authoring"}` and `{section: "evidence"}`, and `{section: "canvas"}` before
drawing (its `canvas.design` is the design guide). Fetch the reference (no section, or
`schema` / `bindings` / `prefabs` / `libraries` / `rules`) as you need it. Follow them; do
not work from memory. This skill adds only what is specific to Claude Code.
Needs Cardinal (maestro) newer than v1.97.16; ask the user to upgrade an older one.

No `storyboard__*` tools: not connected. Tell the user once: sign up at
https://app.cardinalhq.io, run `/cardinal:connect`, restart Claude Code.

## Evidence in Claude Code

- **Witnessed.** Every read-only Cardinal tool result ends with `[receipt:rcpt_<24 hex>]`.
  Note receipt ids as you investigate.
- **Captured.** Any tool result from this session can be cited: shell commands (tests,
  `git`, `make`), file reads and edits, searches, web fetches, subagents, other MCP
  servers, any tool. The plugin's hook keeps each result on this machine and prints
  `[evidence:ev_…]`. Nothing is uploaded until a storyboard cites it. Lost an id? Run
  `cardinal-evidence find <text>`. Promote the ones you cite before binding them, only
  those, as you write the scene that cites them:
  `cardinal-evidence promote --storyboard <id> ev_… [ev_…]` prints `ev_… -> rcpt_…` per
  entry (or its error); bind that receipt. Exit 1 means some entries failed: read the errors.
  An entry promoted before prints its existing receipt (`already promoted`): reuse that id.
- **Withheld.** `[evidence:ev_… withheld: …]`: the call touched a secret. Nothing was kept
  and it cannot be cited: say so plainly instead of paraphrasing its result.
- **Redacted is not withheld.** A captured result showing `[redacted]` was kept with its
  secrets masked: cite it and call it redacted, never withheld; claim nothing about masked values.
- **Never claim more than the output shows.** A captured result is the client's record,
  labeled "reported by <client>". Exit 0 shows a command succeeded, not that the feature
  works. `cardinal-evidence show ev_…` prints what you would cite; bind the exact field
  (`/exit_code`, a `stdout` line) that carries the claim.
- **Reported** (`storyboard__record_evidence`): a result with no `ev_…` id (capture off).
- **The control log is never evidence** and never public: never cite, promote or record
  investigation events or `cardinal-storyboard investigation …` calls.

## Which storyboard

This session's live storyboard (`sb_…` in the session-start context, or
`cardinal-storyboard investigation link`): author it directly, no find, no question, never
`storyboard__create` a second one. It starts empty, with no stated question and an open
window: when `storyboard__set_frame` is listed, frame it (`{storyboard_id, question,
window}`) once both are clear. `cardinal-storyboard investigation question "<text>"`
records the user's question (your statement of it, not owner authority). Checkpoints
(`cardinal-storyboard investigation checkpoint`, per the session-start context) record
material changes in your understanding in the Investigation: claims, never evidence; the
storyboard is not rewritten for each one.

## Update, don't duplicate

For another storyboard (one the user names or an earlier one), or with no live one: if
`storyboard__find` is listed, before `storyboard__add_act` or `storyboard__create`:
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

- **session_id:** in the session-start context ("Cardinal session id for this session:
  …"). Pass it to `storyboard__find` and `storyboard__add_act`; the context hook adds it
  if you forget.
- **The preview loop is local.** After every `storyboard__preview`, a plugin hook renders
  each scene with your local Chromium and reports the PNG paths. Read every PNG (every
  reveal step) and critique it against the authoring guide before you revise; the canvas
  skill covers the critique. No local Chrome: say once that previews were skipped, then
  continue: it is never a publish blocker.

```
asked (note rcpt_ / ev_ ids)
  → describe_grammar → the live storyboard, or find → ask before adding → storyboard__create
  → promote → define_surface / upsert_scene → storyboard__preview → PNGs (plugin hook)
  → Read every PNG → critique → revise … → set_frame → storyboard__publish → state init
```

## Publish and share

The live storyboard is private to org members, never published or shared on its own.
Publishing freezes a reviewed version: published acts are immutable; storyboard__add_act
adds the next act to the same storyboard and link. Share only a published act, only when
asked. An app-relative `view_url` (self-hosted): prefix the Cardinal host.
- Publish answers `public_links_decision_required`: ask the person if public links should
  show this act, unless they already said to update what they shared (that covers only
  this choice); publish again with `public_links: "extend"` or `"keep"`.
- `raw_evidence_confirmation_required`: always ask the person, listing the bindings it
  names; never set `confirm_raw_evidence` yourself, even if they said to update what they
  shared. Only their yes sends `confirm_raw_evidence: true`.

After each publish, `cardinal-storyboard state init --investigation <inv_…>` (`--refresh`
after a later act) writes the InvestigationState (what an agent continues from) and prints
how to author it. `state check <path>` until `ok`, then `state publish <path>`.

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

Member link previews are on by default. Tell the person what a posted link shows. If they
don't want it, pass `link_preview: false` when `storyboard__publish` lists `link_preview`,
or use `storyboard__share` when its action lists `link_preview`.
