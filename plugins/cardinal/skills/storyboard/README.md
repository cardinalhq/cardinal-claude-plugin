# storyboard

Every Claude Code session connected to Cardinal has an **Investigation** and a live
**Investigation Storyboard** from its first moment: the plugin creates them at session
start (no command, no skill) and gives Claude the private storyboard URL. A storyboard is
a scene-by-scene explanation of the investigation, private to your org members while it
is live, and shareable by link once you publish a reviewed version. Every number in it
traces to a receipt from the tools Claude used, and each scene has its own interactive
visual. This skill is how Claude improves that storyboard; see SKILL.md for the flow.
Drawing and previewing the visuals is the [canvas](../canvas/README.md) skill.

## Before you start

- **Claude Code** with the **Cardinal plugin**, connected to your org (`/cardinal:connect`).
  Publishing needs an API key; there is no OAuth sign-in. New to Cardinal? Sign up at
  `https://app.cardinalhq.io` (you get a personal workspace), then run `/cardinal:connect`
  and approve it in the browser; it stores an API key for this machine. You need the **Member** role (or Owner) in that org to author
  storyboards. Readers need no account when the org allows public links.
- Evidence from Cardinal's tools in this session, or within the last 14 days. Claude cites
  the *receipts* those tool calls produced. Unpublished receipts expire after 14 days.
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

You never need to start a storyboard. Start Claude, ask your question and work normally;
Claude's tool results are captured on your machine as you go. Ask *"What's the
storyboard?"* or *"Give me the storyboard link"* at any time and Claude gives you the
private live URL (`cardinal-storyboard investigation link` prints it too). Asked *"Do I
invoke the storyboard skill before I ask my question?"*, the answer is no: work normally.

On a Cardinal that keeps the storyboard up to date from the investigation's checkpoints
(Claude's session-start context says so), you never ask for it to be written. Otherwise,
or to change what it shows, say so in plain words: *"Storyboard this for the team."* or
*"Show how we found the checkout regression, with visuals."*
Claude drafts the scenes in the session's live storyboard and validates them with
`storyboard__preview`. The plugin renders each scene locally and hands Claude the
pictures, which it critiques before it revises. Publishing (only when you ask) freezes a
reviewed version you can share; nothing is published or shared automatically. Published
acts are immutable, and they keep every receipt they cite.

To update another storyboard, ask Claude to add to it (*"Add the rollback to the checkout
storyboard."*). A plugin hook stamps where each act is written from (repo, path in the
repo, branch, PR, commit, the files this session edited, a hashed directory id and your
Cardinal account email; never an absolute path; `cardinal-storyboard context` is the
fallback), and Claude says what the storyboard is about (the PRs, issues, files or links it
explains) when your Cardinal supports it. Claude asks Cardinal for storyboards about the
same work or written from the same session, PR, branch or repo. Org members see these
labels; public links never show them. Claude continues a draft without asking only when it
is this session's own and about what it is writing. Otherwise, if something matches, it
asks before adding to it, saying whether a match is about this work or only written from
the same checkout: up to three close matches, or one looser match
updated in the last 7 days, always with *Start a new storyboard* as an option. Without
anyone to ask (`claude -p`) it starts a new storyboard and names the match. An update adds
an act to the same storyboard, so the id and link stay the same. Public links keep showing
the acts they showed until you say to extend them to the new act. If a link shares raw
evidence the new act binds, Claude always asks you first, even if you said to update what
you shared. A Cardinal without
`storyboard__add_act` gets a new storyboard instead.

Storyboards also find you. When a session starts in a repo, and again when the branch or
commit changes, the plugin asks Cardinal for storyboards of the same PR, branch or
directory and gives Claude up to three of them with their published findings, marked as
data your org wrote rather than instructions. Asked to review or debug that work, Claude
reads the full storyboard (`storyboard__get`) first. Turn it off with
`CARDINAL_STORYBOARD_DISCOVERY=0`.

## What is (and isn't) checked

Cardinal's publish check is **deterministic**. It verifies that every binding resolves to a
receipt, that derived values are computed by Cardinal, that claims are typed honestly and
causal claims are supported, and that statement numbers match the bound evidence. It does
not look at pixels. A picture can suggest a hypothesis. Only a receipted measurement can
state it as fact.
