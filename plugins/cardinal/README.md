# cardinal (Claude Code plugin)

Cardinal's Claude Code plugin: the `cardinal` MCP server, Investigation
Storyboards with local preview and captured evidence, and (once connected)
session telemetry, spend limits and initiative attribution.

## Coordination gate (opt-in preview)

Version 0.60.0 adds a synchronous coordination gate for the validated Claude
Code `-p` transports: `Bash`, `Write`, `Edit` and `NotebookEdit`. It checks fresh
collaborator contributions before declared actions, retains pending content
durably and requires explicit handling before the agent chooses its next action
again. Installing the plugin leaves the gate off.

The server must advertise `event_snapshot` version 1. Enablement requires a
session-specific policy, `CARDINAL_COORDINATION_MODE=direct-cli` and actual
transport probes in that session. Existing sessions retain their delivery
behavior until activation. See the
[runtime and operator contract](https://github.com/cardinalhq/cardinal-agent-plugins/blob/main/docs/specs/coordination-gate-runtime.md)
for setup, `gate-*` commands, rollback and the exact coverage limits. MultiEdit,
interactive/desktop use and unlisted command equivalents remain outside
coverage. Actual hook crashes and timeouts can fail open. This preview does not
claim Phase 4 performance/fleet acceptance.

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
- **Delegated supervision.** A challenge asks for a position (`accepted`
  or `declined`, with a note; `noted` is refused by Cardinal). A supervisor
  holding a `supervise` grant (`investigation grant --scope supervise`)
  rules once on its own challenge after the session took a position
  (`investigation rule <seq> --uphold|--withdraw --text TEXT`); an upheld
  ruling arrives as a DIRECTIVE (authority DELEGATED SUPERVISION): binding,
  answered `accepted` or `conflicts --conflicts-with <owner.decided seq>`.
  The owner's decisions outrank it, and it never widens the session's
  permissions. With a Cardinal that advertises delegated supervision, a
  challenge the session accepted and a directive are re-shown every 5
  main-thread tool boundaries (every 20 after 10 showings) until a
  checkpoint cites them (`refs: ["#<seq>"]`) or the owner overrules them
  (`<sid>.supervision` makes the fast path start Python while anything is
  owed); an open directive blocks Stop once.
  `investigation events` shows each challenge's status.
- **Owner input (connected, author sessions, only when Cardinal asks).**
  `hooks/owner-input.py` (UserPromptSubmit) records each prompt the owner
  types into the session's Investigation (`record-owner-input`, type
  `owner_input.recorded`, authority `owner_input_client_attested`) only
  when the session authors it, the server advertised
  `capabilities.owner_input.enabled: true`, the user has been shown the
  disclosure, the prompt is not a task notification, and
  `CARDINAL_OWNER_INPUT` is not `0` / `false` / `off` / `no`. That kill
  switch is read from the environment, `~/.claude/settings.json` `env`
  and the project's `.claude/settings.json` / `.claude/settings.local.json`
  `env`; any one of them turns it off. The reliable switch is
  `CARDINAL_OWNER_INPUT=0` in `~/.claude/settings.json` `env`: a repo's
  settings `env` can override a shell export of the same variable. Setting
  it also forgets the disclosure, so removing it later discloses again
  before capture resumes. An older Cardinal, or the capability absent or
  off: nothing is captured, sent or queued.
  - Disclosure first: the user is shown a `systemMessage` (user-visible,
    not model context) saying the session's prompts are recorded, at
    session start, or, when the capability arrives later, on the first
    prompt that would be captured (that prompt itself is not). Capture
    starts only after it (`owner_input_disclosed` in the binding, recorded
    only once the message has been written). `claude -p` in text mode never
    displays a `systemMessage`, so a non-interactive session's user does not
    see it; its prompts are still recorded once disclosed.
  - The text is scrubbed of credentials, NUL-stripped and cut to 32 KiB.
    `prompt_sha256` is the sha256 of the scrubbed text before that cut,
    never of the original (no redacted secret can be brute-forced from it);
    re-scrubbing the transcript's prompt reproduces it.
  - Posting is bounded to ~1.5 s of wall-clock time in total; a timeout or
    a transient failure (network, 5xx, 429) waits in
    `<sid>.owner-input-outbox.json` (0600, bounded) for the next prompt or
    Stop; a 404, 400, 401 or 403 drops it (the code alone is logged). The
    outbox is deleted when the capability turns off. After a lost binding,
    turns are renumbered past the server's highest. Owner input is never
    delivered to the model, and nothing the agent can call writes it.
  - Known limits: hooks run in parallel, so a prompt another
    UserPromptSubmit hook blocks may still be recorded; and a
    machine-generated prompt (`/loop`, a scheduled task, `claude -p`) is
    recorded as `user_prompt` like one the user typed.
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
- `cardinal-storyboard investigation grant [--scope read,advise]
  [--owner-input] [--ttl 4h] [--label NAME]`: give someone else (another
  agent acting as a supervisor) scoped access to this session's
  Investigation; prints `export CARDINAL_INVESTIGATION_TOKEN=…
  CARDINAL_INVESTIGATION_ORIGIN=…` once. `investigation grants` lists them
  (never tokens), `investigation revoke <grant_id>` revokes one. With
  `CARDINAL_INVESTIGATION_TOKEN` set, `investigation events|post|show` act
  as that grantee (`Authorization: CardinalInvestigation`, never a key) and
  the author-only commands refuse to run.
- `cardinal-decision on|off|status`: decision capture, off by default. When
  it is on, `hooks/decision-prompt.py` (UserPromptSubmit, connected only)
  gives Claude the exact `cardinal-decision record --session …` command for
  the choices it makes; each one is saved to a local per-session ledger and
  sent as a `cardinal.decision` event when telemetry is connected.
  `CARDINAL_DECISIONS=1|0` (environment or settings `env`) overrides the
  toggle. The model-invoked `decision` skill (`skills/decision/`,
  `/cardinal:decision [status|on|off]`) routes "is decision capture on?" to
  `status` and "turn it on/off" to `on` / `off`; Claude runs the command and
  reports the result, and never toggles it unasked.
- `cardinal-checks on|off|status`: the Checks preview (per-trace,
  evidence-backed decisions over telemetry, built on the `cardinal-behavior`
  tools). Off by default. `on` installs the model-invoked `checks` skill
  from `checks/skill/` into this plugin's `skills/checks/` (listed as
  `cardinal:checks`, hidden from the `/` menu); `off` removes it. The
  `checks-gate.py` hook reconciles that copy at session start (asking Claude
  Code to reload skills when it changes), injects the routing rule (queries
  first, a check only for per-trace judgments) only while the preview is on
  and connected, and denies the Skill tool for it while the preview is off.
  The skill records teaching labels and finds accepted checks through
  `cardinal-behavior` MCP tools (`set_check_example`, `approve_check_example`,
  `remove_check_example`, `get_check_draft`, `lock_check_examples`,
  `list_accepted_checks`), the same ledger (`lib/behavior/checks_ledger.py`)
  that `cardinal-checks example …` / `accepted` run for people. Its guided
  flow is the shared `common/checks/CORE.md`; Codex, Cursor and Gemini CLI
  have the preview too. The raw `cardinal-behavior` tools stay registered for
  everyone; `execute_behavior_range` starts a detached
  worker (`common/behavior/range_runner.py`) that runs an accepted check over
  any range in one-hour windows with window-edge canonicalization and writes
  `ranges/<id>/events.jsonl`, `summary.json` and `result.json`;
  `cardinal-checks range watch <id>` turns the events into one line each for
  Claude Code's Monitor, `range status <id>` prints a local snapshot, and
  `continue_behavior_range` supplies edge extents or restarts a stopped worker.
- `cardinal-install-site`: see the install-site skill.

## Reaching a session from elsewhere

**A running session: Remote Control.** Start Claude Code with
`claude --remote-control` to continue that session from another device
(claude.ai or the Claude app). Your messages there are your own turns: they
reach the session at once, with the permissions you already gave it, and a
decision you post on the Investigation page reaches it at its next tool
boundary as usual. This is the Claude Code path for answering an
Investigation's open question while you are away from the terminal.

**A stopped session.** The storyboard's Sessions panel in the Cardinal web
app gives each session's resume command and a prompt to continue it with.

**Removed: `cardinal-wake`** (plugin 0.53.0). The macOS notifier and its
`cardinal-wake resume` are gone; owner decisions addressed to a session are
still delivered by this plugin's hooks at its next tool boundary. If you ran
`cardinal-wake enable`, remove what it installed:

```sh
launchctl bootout gui/$(id -u)/io.cardinalhq.wake
rm ~/Library/LaunchAgents/io.cardinalhq.wake.plist
rm -rf ~/.cardinal/wake
```

## Limits and parity

Claude Code is the reference runtime: every feature in the cross-agent matrix
is present here except Cursor's connect-time owner-input consent (Claude shows
the per-session disclosure instead, so it needs none). Two things are Claude-only by design: the invariant check (an internal
developer gate, `hooks/invariant-check.py`) and following a Checks range run
with the Monitor tool (Codex, Cursor and Gemini CLI poll `get_behavior_range`
instead). What the Codex, Cursor and Gemini CLI plugins can and can't do next
to this one, feature by feature, with a citation for every limit:
[`docs/parity.md`](../../docs/parity.md). A new hook in `hooks/hooks.json`
fails `tests/test_parity_matrix.py` until it is mapped to a feature there.

## Repository associations across a session

Repository provenance is shared across Claude Code, Codex, Cursor and Gemini.
Starting in a parent workspace does not prevent later repository attachment:
explicit tool working directories, file arguments and successful edits add
repository labels locally. The background poller submits them to Cardinal.
The original checkout context remains unchanged; later repos are additive
`written_from` provenance, never automatically asserted as the storyboard's
subject. Labels contain canonical repository names, never local paths.

To backfill an existing session from its saved edited-file ledger, run:

```sh
cardinal-storyboard investigation repos --session SESSION_ID
```

Supply `--cwd /path/to/checkout` (repeatable) to attach additional checkouts.
This command uses the session's existing investigation and storyboard. It does
not publish or share anything. Automatic collection respects
`CARDINAL_STORYBOARD_CONTEXT=0`, and only an author's own session may sync.
At most 32 repositories are retained. Shell command text is not parsed: use
explicit tool working directories or the CLI for shell-only checkout changes.
Historical repair requires a retained local edit ledger or explicit checkouts.

Multiple labels and browser grouping require Cardinal's
`repository_provenance` capability (the companion Conductor migration/server
change). Older servers continue to accept the existing context request, and
the CLI reports when full multi-repo support is unavailable. Every adapter's
next patch release includes this same shared implementation; the parity matrix
and packaged-artifact tests guard against adapter drift.
