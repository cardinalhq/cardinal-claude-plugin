# cardinal (Claude Code plugin)

Cardinal's Claude Code plugin: the `cardinal` MCP server, Investigation
Storyboards with local preview and captured evidence, and (once connected)
session telemetry, spend limits and initiative attribution.

## Two modes

```
                    before /cardinal:connect          after /cardinal:connect
                    ------------------------          -----------------------
MCP server          none: no URL, never connects      the org's URL
                    (/mcp: missing CARDINAL_MCP_URL)  (CARDINAL_MCP_URL)
credential          none                              the org's API key
                                                      (CARDINAL_MCP_API_KEY)
storyboards         no (no Cardinal tools)            yes: every session has an
                                                      Investigation and a live,
                                                      private Storyboard
evidence capture    yes, local only                   yes; cited results
                                                      uploaded on promote
telemetry, spend    off: those hooks exit at once,    on
limits, initiative, with no context and no network
plan, decision,
git-state, usage
```

- **Not connected (local-only).** `.mcp.json` takes its URL from
  `${CARDINAL_MCP_URL}`, so with it unset the `cardinal` server has no URL.
  Claude Code contacts nothing and lists the server in `/mcp` as failed with
  "Missing environment variables: CARDINAL_MCP_URL". That entry is expected:
  Claude Code has a quiet "not configured" state only for a literally empty
  URL, which a plugin whose URL comes from an env var cannot declare. The
  session-start hook says once how to connect and keeps one line of context
  for later sessions; `/cardinal:status` says the same.
- **Getting write access.** Writes (Cardinal's tools, publishing
  storyboards) need an API key; there is no OAuth sign-in. Sign up at
  `https://app.cardinalhq.io` (a personal workspace is created), then run
  `/cardinal:connect` and approve it in the browser: that creates an API
  key for this machine and stores it. Self-hosted Cardinal:
  `/cardinal:connect --host <url>`. Published storyboards are read without
  an account through a public link when the org allows them.
- **Evidence capture.** In either mode, `hooks/evidence-capture.py`
  (PostToolUse and PostToolUseFailure, matcher `.*`) stores the result of
  every tool call except Cardinal's own (Bash, Read, Edit/Write, Grep,
  WebFetch, Agent, other MCP servers, any tool) locally, under
  `~/.cardinal/evidence/<session_id>/` (credentials scrubbed, removed after
  14 days). A call that touches something sensitive (`.env`, keys, a
  credential command) is kept as a withheld stub only. Nothing is uploaded
  automatically: only a result a storyboard cites is uploaded, by
  `cardinal-evidence promote`, which needs a connection.
  `cardinal-evidence find <text>` / `show ev_…` look entries up. The same
  run records the repo-relative path of every file a successful Edit,
  Write, MultiEdit or NotebookEdit changed, per session
  (`~/.claude/cardinal/storyboard-files/<session>.json`, 0600, at most 200):
  the storyboard context hook stamps them as where an act was written from.
- **Storyboard discovery (connected).** `hooks/storyboard-discovery.py`
  (SessionStart, and UserPromptSubmit when the branch or HEAD moved) asks
  Cardinal for storyboards about this work (a PR, commit, branch, file,
  issue or link they declared they explain) or written from it (the same
  PR, branch, commit or edited file; never just the repo or a directory),
  and puts at most 3, about-matches first, each labelled honestly
  ("about PR o/r#N", or "written from branch b (subject not confirmed)"),
  with their scene statements (2 KB in all; a draft act's statements marked
  `[draft, not yet checked]`, published ones first when space runs short),
  in Claude's context, marked as data written by org members, not
  instructions, so a review or a debugging session starts from them and
  reads them in full with `storyboard__get`. Off `main`/`master`/`develop`/
  `trunk` it sends repo, branch, PR (from the `gh` cache; it never runs
  `gh`), HEAD and the tracker keys in the branch name (ENG-12). On one of
  those branches (after a merge) it sends the repo, the PR numbers and merge
  commits of the last 50 first-parent commits (at most 20) and the last 5
  branches this checkout was on (`git reflog`), so a storyboard about a PR
  you just merged is labelled "… — merged as 1a2b3c4". These go to your
  org's own Cardinal only. Every request carries `X-Cardinal-Client:
  claude-plugin/<version>`; find's capability version is cached for 24 h in
  `~/.claude/cardinal/server-caps.json`, and an older Cardinal gets the
  previous request shape. It gives up after 2 s, remembers every look per session (failures too), and is silent
  when not connected, without an MCP key, outside a repo or when nothing
  matches. On SubagentStart it gives a subagent (a forked skill such as
  code review, an Agent/Task call) the block the session's last look
  rendered, from that per-session record: no request. Opt out with
  `CARDINAL_STORYBOARD_DISCOVERY=0` (environment or settings `env`).
- **Storyboards about the file being edited (connected).**
  `hooks/storyboard-edit-lookup.py` (PreToolUse Edit, Write, MultiEdit,
  NotebookEdit; a separate process from invariant-check) asks Cardinal, at
  most once per directory and 6 times per session, within 1.5 s, for
  storyboards about the file or about a PR that last changed it (`git log
  -n 10 -- <file>`), and shows the ones this session has not seen yet (1 KB:
  "about file p", "about PR o/r#N, which last changed p"). Only against a
  Cardinal whose cached capability says it supports it; same opt-out as
  discovery.
- **Storyboard context (connected; Claude Code 2.1.0 or newer).**
  `hooks/storyboard-context.py` (PreToolUse on `storyboard__create`,
  `add_act`, `publish`, `find`, `link`) adds this session's id and, when the
  call has none, the checkout context (`cardinal-storyboard context`'s
  fields plus the files this session edited) through `updatedInput`, so
  Claude never pastes it. It keeps every argument Claude passed, never
  changes a context Claude set and never writes `about` (what a storyboard
  explains). `publish` is stamped only on a Cardinal that accepts it. It
  needs a Claude Code that applies a PreToolUse `updatedInput` to MCP tool
  calls (2.1.0 or newer); on an older one the call runs unchanged and the
  skill's `cardinal-storyboard context` fallback applies. Opt out with
  `CARDINAL_STORYBOARD_CONTEXT=0`. `CARDINAL_HOOK_DEBUG=1` logs each
  storyboard hook's timing and decision to
  `~/.claude/cardinal/hook-debug.log` (local only).
- **Live Investigation (connected).** Every session gets an Investigation
  and its live Storyboard automatically: the SessionStart hook
  (`hooks/storyboard-session.py`) asks Cardinal once
  (`ensure-session-investigation`, idempotent per session) and binds the
  session (`~/.cardinal/investigations/sessions/<id>.json`: investigation and
  storyboard ids, the private viewer and investigation URLs, the event
  cursor). Restart, resume and compaction reuse the binding without a
  request. Claude's context says which investigation and storyboard this is
  and to give the URL when asked; nobody starts a storyboard. A failed
  request never blocks the session: it adds one short clause, is retried in
  the background with back-off, and `cardinal-storyboard investigation link`
  retries at once. `CARDINAL_INVESTIGATION_ID=inv_…` at launch joins an
  existing investigation instead; joining someone else's, the session is told
  it is not the author (it gets the links and the advisory events, but cannot
  edit or publish that storyboard or acknowledge events). Nothing is uploaded
  by this: captured evidence stays local until a storyboard cites it. The
  control log is never evidence: `cardinal-storyboard investigation …` calls
  are never captured.
- **Investigation events (connected).** Advisory events posted to the
  session's investigation (cue, question, challenge from other principals)
  reach it at its next main-thread tool boundary. A per-session background
  poller (`hooks/investigation-poller.py`, started at session start and
  restarted by the fast path when it is gone) reads them every 5 s while
  the session is active (a tool call in the last 2 minutes; none while
  idle) and leaves the deliverable ones in the session's inbox;
  `hooks/investigation-events.sh` (PostToolUse and PostToolUseFailure on
  every tool call, Stop as a backstop, at most 3 consecutive blocks) is a
  POSIX sh check for that inbox, so a tool call starts no Python and touches
  no network unless something is waiting. A subagent's call leaves the event
  for the main thread. Each is marked authority ADVISORY with its producer,
  its text as one JSON string and the `cardinal-storyboard investigation
  ack` command; the cursor advances only after it was delivered. The poller
  exits with the Claude Code process. `CARDINAL_INVESTIGATION_POLLER=0`
  turns it off (events then arrive only at Stop).
- **Semantic checkpoints (connected, author sessions).** The session-start
  context asks the investigating session to checkpoint material changes in
  its understanding (a hypothesis relied on or resolved, an experiment
  started or finished, a finding proposed / revised / retracted, a decision
  proposed / revised, a material question opened / resolved) with
  `cardinal-storyboard investigation checkpoint`: a JSON array of up to 20
  events on stdin, appended to the same ordered stream as one atomic batch
  (`checkpoint-investigation`), one short line of output. Each is the
  agent's claim (authority `producer_claim`), never a fact, owner authority
  or InvestigationState, and never chain of thought; tool calls are not
  checkpointed. They are never delivered back as advisory events. A cited
  `ev_` id is uploaded first as a receipt of the Investigation
  (`upload-investigation-evidence`, the item `cardinal-evidence promote`
  sends; never through a storyboard, so a published storyboard does not
  block it). Only the cited ones; a withheld or unknown one refuses the
  whole checkpoint, nothing sent; `rcpt_` ids pass as they are. The
  default key comes from the events as cited, so a retry is deduplicated
  even after local records are lost. Ids get their family's prefix
  (`fnd_x` -> `finding_x`; a bare id -> its family's). A joined non-author
  session is not asked to and cannot.
  `CARDINAL_CONNECTION=env` makes the hooks and `cardinal-storyboard` use
  `CARDINAL_MCP_URL` / `CARDINAL_MCP_API_KEY` from the environment only (a
  development session against another Maestro); after `/cardinal:disconnect`
  nothing is sent in that mode either.
- **Connected.** `/cardinal:connect` picks the org, writes its MCP URL and API
  key and the OTel ingest settings into `~/.claude/settings.json` `env`, and
  turns on telemetry (Outcomes Dashboard), spend limits, initiative, plan,
  decision and usage hooks. `/cardinal:disconnect` goes back to local-only.

**A key without a URL (stray key).** If `CARDINAL_MCP_API_KEY` is set but
`CARDINAL_MCP_URL` is not — say you exported the key in a shell profile for a
manual Codex config — the `cardinal` server still has no URL (the key is sent
nowhere), but the hooks count the machine as connected and Cardinal's tools
are missing. `/cardinal:connect` writes both (except `--telemetry-only`,
which writes neither). The plugin warns when it sees this (once per session,
at session start, and in `/cardinal:status`); fix it by running
`/cardinal:connect` (`--host <your maestro URL>` for self-hosted) or unsetting
the key.

The switch is `hooks/_connection.py`: connected means a `CARDINAL_MCP_API_KEY`,
a Cardinal ingest key in `OTEL_EXPORTER_OTLP_HEADERS` (settings `env` or the
environment), or the connect state file `~/.claude/cardinal.json`.

## Commands (`bin/`)

- `cardinal-connect`, `cardinal-disconnect`, `cardinal-status`: the connection.
- `cardinal-evidence`: captured evidence (`promote`, `list`, `find`, `show`,
  `off` / `on`, `status`).
- `cardinal-storyboard context [--session-id ID]`: prints `{"context": {…}}`,
  where a storyboard is being written (repo, path in the repo, branch (not
  a default branch), PR, commit, a hashed directory id, the client, the
  Cardinal account email and, with a session id, the files that session
  edited; never an absolute path). The storyboard-context hook stamps the
  same object automatically; this is the fallback. Labels only, never
  authorization; members see them, public links never do.
- `cardinal-storyboard discover [--cwd DIR] [--json]`: prints the block the
  discovery hook injects (`{"block": …}` with `--json`); for other harnesses
  and for debugging. Always exits 0.
- `cardinal-storyboard investigation link`: this session's private live
  Storyboard URL and Investigation URL (from the binding; no network).
- `cardinal-storyboard investigation question "<text>"`: record what the
  investigation asks (the agent's statement, not owner authority).
- `cardinal-storyboard investigation checkpoint [--session SID]` (events on
  stdin): record material changes in this session's understanding as
  semantic events (`--help` lists the 11 types and their fields).
- `cardinal-storyboard investigation ack|events|post|show|attach`: the
  investigation's advisory event stream and state (`--help`);
  `investigation create|bind` are optional, for joining or starting another
  investigation explicitly.
- `cardinal-decision`: decision capture.
- `cardinal-install-site`: see the install-site skill.
