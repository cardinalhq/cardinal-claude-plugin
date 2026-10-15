---
name: supervise
description: Supervise another agent session through its Cardinal Investigation — watch everything it does (its checkpoints and its captured tool activity), validate every finding, question every hypothesis, challenge what is wrong or unreported, and review its artifacts when it is done, posting verdicts the owner sees on the Storyboard under a Supervisor tag. Use only when the user asks you to supervise, watch, review or check another session (by session id, inv_ id or Storyboard link); not for reviewing your own work.
argument-hint: "<worker session id | inv_… | Storyboard link>"
---

# supervise — watch another session, check it independently

Follow [CORE.md](CORE.md), co-located in this directory, for the whole workflow: set up,
the review loop, verdicts, rulings, the artifact review and the bounds. This file adds what
is specific to Claude Code.

## Commands and state

- `cardinal-storyboard` and `cardinal-evidence` are on `PATH` through this plugin's `bin/`;
  if not, run `"<this skill's base directory>/../../bin/cardinal-storyboard"` (and
  `cardinal-evidence`) instead. Run each Cardinal command in its own Bash call.
- `<dir>` (CORE.md) is `~/.cardinal/supervise/<inv>/`: create it with `mkdir -p -m 700`.
- Your own session keeps its own Investigation. Do not checkpoint your verdicts into it as
  if they were the worker's; your review lives in the worker's Investigation as posts.

## Waking: listen, never poll

Run one watch per supervised Investigation with **Monitor**, timeout at the maximum:

```
cardinal-storyboard investigation events <inv> --credential-file <dir>/grant.json --cursor-file <dir>/cursor.json --follow --timeout 600
```

Starting from your cursor file, the watch signals only events you have not committed, so
re-arming it never replays the history; it never commits anything itself.

Without `--json` it prints one line per new event (your own posts excluded) and nothing
while the worker is quiet, so each notification means there is something to review. On a
notification, run one pass of CORE.md's review loop (it reads through your cursor file;
the watch's own lines are only the signal). When the watch exits on its timeout, sweep the
worker's new activity anyway (a worker can act for a long time without checkpointing),
then re-arm it. Never write sleep-and-reread loops.

## Doing your own checks

Reproduce claims with read-only tools: Read, Grep, `git log` / `git diff`, Cardinal query
tools (their results carry witnessed `rcpt_…` receipts you can cite). For anything that
needs a build or tests, use your own worktree (`git worktree add` under your scratchpad),
never the worker's checkout, and run the targeted test, not the full suite. Delegating a
bounded check to a subagent is fine; the verdict is still yours.
