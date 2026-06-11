# Skill / command usage telemetry — plugin change (v0.8.0)

**Status**: draft (2026-06-11)
**Companion spec**: `conductor/docs/specs/agent-outcomes-skills.md`
(the consumer side — lakerunner aggregation + the /outcomes Skills
panel). This doc covers only what changes **in this plugin**.

---

## Why the plugin needs to change at all

Almost everything the Skills-distribution feature needs is already on
the wire, emitted by Claude Code's **native** OTel exporter (which
`cardinal-connect` configures). Validated against prod lakerunner on
2026-06-11: every tool execution lands as a `tool_result` event whose
tags identify the thing invoked —

| Invocation | `tool_name` | Where the name lives |
|---|---|---|
| Skill (model-invoked) | `Skill` | `tool_parameters` → `{"skill_name": "commit-commands:commit-push-pr"}` |
| MCP tool | `mcp_tool` | `tool_parameters` → `{"mcp_server_name": "plugin_cardinal_cardinal", "mcp_tool_name": "lakerunner__execute_logs_query"}` |
| Subagent spawn | `Agent` | `tool_parameters` → `{"subagent_type": "Explore"}` |
| Built-in tool | `Bash`, `Read`, … | `tool_name` itself |

Subagents need no plugin assist either: their internal tool calls
(validated live, 2026-06-11) emit `tool_result` events under the
**parent** `session_id`, so skills/MCP/tools used inside a subagent
are already counted.

All of these carry `session_id` / `user_email` / `event_sequence` as
indexed dimensions, so they join to `agent_sessions` rows for free.
**No plugin work is needed for any of that.**

The one genuine gap: **skills the user invokes by typing a slash
command** (`/code-review`, `/verify`, …). Claude Code expands those
directly into context — no `Skill` tool call happens, so no
`tool_result` event fires. Without a plugin assist, user-invoked
skills are invisible and the adoption numbers undercount exactly the
invocations that show deliberate human intent.

The fix is small because the plugin already has a hook in the right
place: `git-state.py` runs on every `UserPromptSubmit` and already
POSTs one `cardinal.git_state` event per turn. The hook's stdin
payload includes the raw `prompt` — for slash-command turns, that is
the command text. We parse the command name out and stamp it on the
event we already send.

## The change — `hooks/git-state.py`

### Detection

After parsing the hook payload, inspect `payload["prompt"]`:

```python
_COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")

def _detect_command(prompt: str | None) -> str | None:
    """'/code-review --fix' → 'code-review'; non-command prompts → None."""
    if not prompt:
        return None
    m = _COMMAND_RE.match(prompt)
    return m.group(1) if m else None
```

Rules:

- **Name only, never arguments.** Slash-command args are free text
  (`/deep-research <anything>`) and can carry sensitive content. The
  attribute is the command name, full stop.
- Namespaced commands (`plugin:command`) pass through verbatim — the
  `:` is part of the name and downstream grouping wants it.
- A prompt that merely *mentions* a slash command mid-sentence does
  not match (anchored at start).
- Built-in CLI commands (`/model`, `/clear`, …) will match too. That
  is fine — filtering builtins from *skills* is a downstream concern
  (lakerunner knows the skill vocabulary; the plugin does not, and
  must not hardcode a denylist that rots).

### Emission

One new optional attribute on the existing `cardinal.git_state` log
record, same emission style as `cardinal.initiative.name`:

```
log.attributes:
  ...
  cardinal.command = "code-review"        (only when the turn is a slash command)
```

Stored form in lakerunner (dots → underscores): `cardinal_command`.

Why piggyback rather than a second event: the command is a per-turn
fact, `cardinal.git_state` is the per-turn event, and one POST per
turn keeps the hook's latency budget and failure surface unchanged.
The consumer routes on `event_name='cardinal.git_state'` already;
it just gains one more LWW-style field to read (consumer treats it as
append-to-set per session, not LWW — see the conductor spec).

### Open validation item

The exact `prompt` shape Claude Code hands to `UserPromptSubmit` for
slash-command turns needs one empirical check before release: it is
expected to be the raw typed text (`"/code-review --fix"`), but if it
arrives pre-expanded (`<command-name>/code-review</command-name>…`),
the regex must also accept that form:

```python
_COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")
```

Cheap to validate the same way the OTEL-env propagation gap was
validated for v0.4 (log the payload from a live hook once, then
delete the logging). Ship whichever branch matches reality; keeping
both costs four lines and is robust to Claude Code changing its mind.

### Non-functional invariants (unchanged)

- Best-effort: any failure → `exit 0`, never block the prompt.
- No new POST, no new timeout, no new dependency.
- Tests: extend `tests/test_cardinal_plugin.py` with `_detect_command`
  table cases (plain command, args, namespaced, mid-sentence mention,
  empty, tag-wrapped) and one end-to-end payload assertion that
  `cardinal.command` appears when expected and is absent otherwise.

## Versioning

- Bump plugin to **0.8.0** (`plugin.json`, and the `scope.version` in
  `git-state.py`'s OTLP body).
- Graceful absence holds in both directions: old plugins simply never
  send `cardinal.command` (sessions show no user-invoked skills, the
  panel degrades to model-invoked data); new plugins talking to an
  older lakerunner send an attribute nobody reads.

## Explicitly out of scope for 0.8.0 (fast-follow candidate)

**Installed-skills inventory** — the /outcomes "unused skills"
feature ("you shipped 9 skills, 4 have never fired") needs to know
what is *installed*, not just what *fired*. That would be a new
`SessionStart`-hook event (`cardinal.skills_installed`, names only)
enumerating skills visible to the session. Deferred: it is a new
event type and a new payload contract, and the distribution panel
ships without it. Specced in the conductor doc as Phase 2.
