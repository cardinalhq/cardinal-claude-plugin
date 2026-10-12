---
name: decision
description: Check whether Cardinal decision capture is on, or turn it on or off, with the plugin's cardinal-decision command. Use only when the user asks about Cardinal decision capture (recording the agent's decisions to Cardinal), not for making or recording a decision.
argument-hint: "[status|on|off]"
---

# decision — Cardinal decision capture status and toggle

Decision capture is opt-in and off by default. When it is on, each decision
you record becomes a `cardinal.decision` event tagged with the engineer,
session, repo, branch, PR, anchors and code clusters. It shows in the Agent
Outcomes dashboard and is also kept in a local per-session ledger.

## Run the command

Run it with the Bash tool:

```bash
cardinal-decision status
```

If `cardinal-decision` is not on `PATH`, run
`"<this skill's base directory>/../../bin/cardinal-decision"` instead.

- **"Is it on?"**, **"what is decision capture?"** or `/cardinal:decision`
  with no argument: run `status`. Do not toggle it.
- **"Turn it on"** or **"turn it off"**, or `/cardinal:decision on|off`: run
  `cardinal-decision on` or `cardinal-decision off`. Run `status` afterwards
  and report the effective state.
- Never run `on` or `off` unless the user asked for it in this
  conversation.

## Report the result

Report the output in one or two sentences.

- `Decision capture: on|off`: the effective state. A suffix such as
  `(CARDINAL_DECISIONS=1)` means that variable overrides the toggle, from
  `~/.claude/settings.json` `env` or the shell environment. While it is set,
  `on` and `off` have no effect. If it contradicts what the user asked for,
  tell them where to change it. Do not edit it yourself.
- `Cardinal telemetry: not connected`: decisions are kept only in the local
  ledger and are never sent to Cardinal. To send them, the user runs
  `/cardinal:connect`.
- To list this session's decisions, add `--session <session id>` to
  `status`. The session id is in your context.

`on` and `off` apply from the user's next prompt. When capture is on, a
prompt hook adds the exact `cardinal-decision record --session …` command to
each prompt. Follow it when you make a choice that constrains later work. You
do not need this skill to record a decision.
