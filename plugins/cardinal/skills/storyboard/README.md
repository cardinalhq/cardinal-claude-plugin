# storyboard

Turns a Cardinal investigation into an **Investigation Storyboard**. A storyboard is a
scene-by-scene explanation of what was found, published in Cardinal, and shareable by
link. Every number in it traces to a receipt from the tools Claude used, and each scene
has its own interactive visual. See SKILL.md for the flow Claude follows. Drawing and
previewing the visuals is the [canvas](../canvas/README.md) skill.

## Before you start

- **Claude Code** with the **Cardinal plugin**, connected to your org (`/cardinal:connect`).
  Publishing needs an API key; there is no OAuth sign-in. New to Cardinal? Sign up at
  `https://app.cardinalhq.io` (you get a personal workspace), then run `/cardinal:connect`
  and approve it in the browser; it stores an API key for this machine. You need the **Member** role (or Owner) in that org to author
  storyboards. Readers need no account when the org allows public links.
- An investigation done with Cardinal's tools in this session, or within the last 14 days.
  Claude cites the *receipts* those tool calls produced. Unpublished receipts expire
  after 14 days.
- Any other tool result from the session can be cited too: shell commands (tests, `git`,
  `make`), file reads and edits, web fetches, other MCP servers (Grafana, Datadog, …). The
  plugin keeps them on your machine and uploads only the ones a storyboard cites
  (`cardinal-evidence promote`); the storyboard labels them *captured*, reported by the
  client. Calls that touch secrets (`.env`, keys, credential commands) are withheld and
  cannot be cited. `cardinal-evidence off` stops the local capture.
- For visual previews: **Google Chrome or Chromium** (version 112+) on macOS or Linux.
  Without it Claude still authors and publishes, but can't look at the scenes first.
- Cardinal (maestro) newer than v1.97.16. Cardinal serves the authoring guidance itself
  (`storyboard__describe_grammar` sections `authoring`, `evidence` and `canvas`), and an
  older install rejects those sections.

Other Claude clients connected to Cardinal (claude.ai, Claude Desktop) get the same
guidance from the server. The plugin adds the local previews and captured evidence.

## Use it

After investigating, ask for it in plain words:

*"Storyboard this investigation for the team."* or *"Write up how we found the checkout
regression, with visuals."*

Claude drafts the scenes and validates them with `storyboard__preview`. The plugin then
renders each scene locally and hands Claude the pictures, which it critiques before it
revises and publishes. You get a `view_url` to open or
share. Published acts are immutable, and they keep every receipt they cite.

To update a storyboard, ask Claude to add to it (*"Add the rollback to the checkout
storyboard."*). Claude runs `cardinal-storyboard context` (repo, path in the repo, branch,
PR, commit, a hashed directory id and your Cardinal account email; never an absolute path)
and asks Cardinal for storyboards written from the same session, PR, branch or repo. Org
members see these labels; public links never show them. Claude continues a draft only
when it is this session's own. Otherwise, if something matches, it asks before adding to it:
up to three close matches (same session, PR, branch or directory), or one looser match
updated in the last 7 days, always with *Start a new storyboard* as an option. Without
anyone to ask (`claude -p`) it starts a new storyboard and names the match. An update adds
an act to the same storyboard, so the id and link stay the same. Public links keep showing
the acts they showed until you say to extend them to the new act. If a link shares raw
evidence the new act binds, Claude always asks you first, even if you said to update what
you shared. A Cardinal without
`storyboard__add_act` gets a new storyboard instead.

## What is (and isn't) checked

Cardinal's publish check is **deterministic**. It verifies that every binding resolves to a
receipt, that derived values are computed by Cardinal, that claims are typed honestly and
causal claims are supported, and that statement numbers match the bound evidence. It does
not look at pixels. A picture can suggest a hypothesis. Only a receipted measurement can
state it as fact.
