# storyboard

Turns a Cardinal investigation into an **Investigation Storyboard**. A storyboard is a
scene-by-scene explanation of what was found, published in Cardinal, and shareable by
link. Every number in it traces to a receipt from the tools Claude used, and each scene
has its own interactive visual. See SKILL.md for the flow Claude follows. Drawing and
previewing the visuals is the [canvas](../canvas/README.md) skill.

## Before you start

- **Claude Code** with the **Cardinal plugin**, connected to your org (`/cardinal:connect`).
  You need the **Member** role (or Owner) in that org to author storyboards.
- An investigation done with Cardinal's tools in this session, or within the last 14 days.
  Claude cites the *receipts* those tool calls produced. Unpublished receipts expire
  after 14 days.
- For visual previews: **Google Chrome or Chromium** (version 112+) on macOS or Linux.
  Without it Claude still authors and publishes, but can't look at the scenes first.
- Cardinal (maestro) v1.97.14 or newer. On an older install, row lookups by identity
  (`select`) and binding reuse (`ref`) are rejected as unknown binding keys, and large
  receipts cannot be navigated.

## Use it

After investigating, ask for it in plain words:

*"Storyboard this investigation for the team."* or *"Write up how we found the checkout
regression, with visuals."*

Claude drafts the scenes and validates them with `storyboard__preview`. The plugin then
renders each scene locally and hands Claude the pictures, which it critiques before it
revises and publishes. You get a `view_url` to open or
share. Published storyboards are immutable, and they keep every receipt they cite. To
change one, ask for a new storyboard.

## What is (and isn't) checked

Cardinal's publish check is **deterministic**. It verifies that every binding resolves to a
receipt, that derived values are computed by Cardinal, that claims are typed honestly and
causal claims are supported, and that statement numbers match the bound evidence. It does
not look at pixels. A picture can suggest a hypothesis. Only a receipted measurement can
state it as fact.
