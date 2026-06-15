# Per-turn telemetry

Extend the plugin's emit surface from session-grain to per-model-call grain so
the Advisory section in conductor's `/agent-outcomes` dashboard can detect:

- **A1** cache-cliff — `cache_read_input_tokens / (cache_read + cache_creation)`
  collapsing within a session.
- **C3** cross-session reference-doc reuse — files Read in N+ sessions for a
  user/team, recommended for promotion into `CLAUDE.md`.
- **D1** tool-loop without progress — consecutive tool calls with no
  Edit/Write/NotebookEdit before the loop exits.

None of these are derivable from the existing `cardinal.agent_session` /
`cardinal.subagent_usage` events: session-grain rollups discard per-call
token deltas and per-call tool-input arguments.

## Design constraints (non-negotiable)

1. **Async hook.** `async: true` in `hooks.json`. Claude Code does not
   await async hooks, so latency on the user-facing turn loop is structurally
   zero. This is the same property `git-state.py` and `subagent-usage.py`
   rely on; we do not introduce a new mechanism.
2. **No new sync work in any existing hook.** `limits-gate.py` (the only
   sync hook today) is not touched.
3. **No buffer, no retry, no persistence.** One Stop firing → one OTLP
   POST → silent exit on any failure, matching `subagent-usage.py:257-261`.
4. **Single transcript read per Stop.** Same file-I/O pattern as
   `subagent-usage.py:108-128`. No additional reads on PreToolUse /
   PostToolUse / UserPromptSubmit.
5. **Bounded record count per emit.** Cap at 64 model calls and 256
   tool-call records per Stop. If exceeded, truncate the tail and set
   `truncated=true` on the final usage record. Protects against pathological
   tool-loop sessions consuming unbounded memory in the hook process.
6. **Privacy-bounded `target` capture.** Only file paths from
   `Read | Edit | Write | NotebookEdit` are emitted. Other tools' inputs
   (Bash command, Grep pattern, MCP args) are NOT captured — tool name only.
   See §Privacy below.

## Trigger

`Stop` hook. Fires once when the assistant finishes responding to a user
turn (i.e. the model returned a turn without `tool_use`, or the loop
otherwise terminated).

A single user prompt can produce N model calls if the assistant invokes
tools (each tool_result loops back to a model call). The Stop hook is the
natural point to emit all of them in one batch — there is no `PostModelCall`
hook in Claude Code, and per-call inline emission from PreToolUse hooks
would multiply HTTP calls without gain.

## Event schema

Two `event_name` types, emitted in the same OTLP POST batch. Linked by
`(session_id, ts, turn_seq)`.

### `cardinal.turn_usage` — one per model call

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `event_name` | string | constant | `cardinal.turn_usage` |
| `session_id` | string | hook payload | |
| `ts` | int (ns) | transcript record `timestamp` | UTC; lakerunner orders model calls by this |
| `turn_seq` | int | 0-indexed within the Stop firing | links `cardinal.turn_tool` records |
| `model` | string | transcript `message.model` | e.g. `claude-opus-4-7` |
| `input_tokens` | int | `usage.input_tokens` | |
| `output_tokens` | int | `usage.output_tokens` | |
| `cache_creation_input_tokens` | int | `usage.cache_creation_input_tokens` | |
| `cache_read_input_tokens` | int | `usage.cache_read_input_tokens` | the **A1 critical field** |
| `truncated` | bool | constant on last record | only set if §5 truncation triggered |

### `cardinal.turn_tool` — one per `tool_use` block in the user turn

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `event_name` | string | constant | `cardinal.turn_tool` |
| `session_id` | string | hook payload | |
| `ts` | int (ns) | parent `turn_usage.ts` | identical to the linking turn_usage |
| `turn_seq` | int | parent `turn_usage.turn_seq` | |
| `tool_seq` | int | 0-indexed within `turn_seq` | preserves order |
| `tool_name` | string | content block `name` | e.g. `Read`, `Edit`, `Bash`, `mcp__foo__bar` |
| `target` | string | content block `input.file_path` | omitted unless `tool_name ∈ {Read, Edit, Write, NotebookEdit}` |

OTLP log records are flat key/value, matching `subagent-usage.py:188-211`'s
shape. Lakerunner persists each record; the C3 query is then a group-by on
`tool_name='Read'` + `target` across sessions.

## Transcript walk

Stop hook receives `{session_id, transcript_path, ...}` on stdin. To slice
"this user turn":

1. Read `transcript_path` JSONL.
2. Walk backward from EOF; find the most recent record whose
   `message.role == 'user'` AND content is a text block (not a
   `tool_result` block). That's the user-prompt boundary; everything after
   it belongs to the current Stop firing.
3. From that point forward, for each `message.role == 'assistant'` record
   with `message.usage`, emit one `cardinal.turn_usage`. For each
   `tool_use` block in `message.content`, emit one `cardinal.turn_tool`
   linked by `turn_seq`.
4. If no boundary is found (e.g. first turn, transcript truncated), treat
   the whole transcript as the current turn — A1/D1 still meaningful;
   downstream dedup keys on `(session_id, ts)`.

Edge case: if `tool_use_id`s appear in the transcript but the user has not
finished responding (tool still running), Stop will not have fired yet —
no special handling needed.

## Privacy

`target` is captured for `Read / Edit / Write / NotebookEdit` only,
carrying `file_path` as written in the tool input. This is a new exposure
surface — existing `cardinal.*` events emit branch, repo, and tool names
but not file paths.

- Path string is passed through unmodified. We do not strip the cwd
  prefix in v1 — relative-vs-absolute is the downstream consumer's choice,
  and stripping introduces a path-normalization surface we don't need yet.
- Bash `command`, Grep `pattern`, MCP tool inputs are NOT captured.
- A follow-up may add a path-allowlist (e.g. drop `/tmp`, `~/.ssh`) if
  customer review surfaces concerns. Out of scope for v1.

## Volume

Heavy user ≈ 50 user-turns/day × 5 model-calls/turn × 50 users/team =
~12.5k `turn_usage` records/day/team + ~25k `turn_tool` records/day/team.
~14M records/year/team. Trivial for lakerunner; one HTTP POST per Stop
matches the existing per-event POST cadence.

## Test plan

Mirrors `tests/test_subagent_usage.py` (stub OTLP server on port 0, hook
run as subprocess with `HOME` redirected).

| Test | Fixture | Assertion |
|---|---|---|
| `sums_usage_records_across_model_calls` | 3 assistant messages with usage, no tool_use | 3 `turn_usage` records emitted, `cache_read_input_tokens` distinct per record |
| `tool_use_records_link_to_parent_turn_seq` | 1 assistant with 2 tool_use blocks (Read + Edit) | 1 `turn_usage` + 2 `turn_tool` with `turn_seq=0`, `tool_seq=0|1`, `target` set |
| `tool_target_omitted_for_bash_and_grep` | 1 assistant with Bash + Grep tool_use | `tool_name` set, `target` absent |
| `user_boundary_excludes_prior_turn` | transcript with prior turn + new user prompt + 1 new assistant | only the new assistant emitted |
| `tool_result_user_messages_are_not_treated_as_boundary` | tool_use → tool_result → assistant | boundary stays at the real user prompt; all assistants emit |
| `truncates_above_cap` | 100 assistant messages | 64 emitted, last one carries `truncated=true` |
| `missing_transcript_silent_exit` | no transcript_path | no records, exit 0 |
| `no_endpoint_silent_exit` | empty `OTEL_*` | no records, exit 0 |
| `api_key_header_sent` | settings.json with `OTEL_EXPORTER_OTLP_HEADERS` | header present on POST |
| `chaos_lakerunner_timeout` | stub server delays 5s | hook returns 0 before timeout window |

## Plugin version

v0.10.0. Conventional commit: `feat(v0.10.0): cardinal.turn_usage / cardinal.turn_tool — per-model-call telemetry for Advisory`.

## Out of scope

- Lakerunner schema + ingest changes (separate PR in `lakerunner`).
- Conductor `/agent-outcomes` Advisory section (separate PR in `conductor`).
- `ok` / error-status field on `cardinal.turn_tool` — D1 detector doesn't
  need it; revisit if a future advisory does.
- Bash command-prefix capture, MCP arg capture — revisit when a specific
  advisory needs it.
