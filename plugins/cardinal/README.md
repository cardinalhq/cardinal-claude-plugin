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
storyboards         no (no Cardinal tools)            yes
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
  `cardinal-evidence find <text>` / `show ev_…` look entries up.
- **Storyboard discovery (connected).** `hooks/storyboard-discovery.py`
  (SessionStart, and UserPromptSubmit when the branch or HEAD moved) asks
  Cardinal for storyboards of the same PR, branch (not `main`/`master`) or
  directory below the repo root (never the whole repo) and puts at most 3,
  with their published scene statements (2 KB in all), in Claude's context,
  marked as data written by org members, not instructions, so a review or a
  debugging session starts from them and reads them in full with
  `storyboard__get`. It sends only repo, path, branch and PR (the PR from the
  `gh` cache; it never runs `gh`), gives up after 2 s, remembers every look
  per session (failures too), and is silent when not connected, without an
  MCP key, outside a repo or when nothing matches. Opt out with
  `CARDINAL_STORYBOARD_DISCOVERY=0` (environment or settings `env`).
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
- `cardinal-storyboard context`: prints `{"context": {…}}`, where a
  storyboard is being written (repo, path in the repo, branch, PR, commit, a
  hashed directory id, the client and the Cardinal account email; never an
  absolute path). The storyboard skill passes it to `storyboard__find`,
  `storyboard__create` and `storyboard__add_act`, so an update adds an act to
  the storyboard it finds instead of starting a duplicate. Labels only, never
  authorization; members see them, public links never do.
- `cardinal-storyboard discover [--cwd DIR] [--json]`: prints the block the
  discovery hook injects (`{"block": …}` with `--json`); for other harnesses
  and for debugging. Always exits 0.
- `cardinal-decision`: decision capture.
- `cardinal-install-site`: see the install-site skill.
