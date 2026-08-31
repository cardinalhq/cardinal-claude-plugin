# semantic-dag

A unified Cardinal session window for Claude Code and Codex, with launched
agents shown separately from each session's typed semantic workflow DAG.

```
┌────────────────────────────────────────┬───────────────────────┐
│ Refactor login flow      t-a3f1  ● live│  PHASE DETAILS        │
├────────────────────────────────────────┤                       │
│         [ Plan the change ]            │  Overview  Router     │
│           /            \               │                       │
│      [ Read ]    [ Edit ]•Edit         │  Replacing the legacy │
│                                        │  redirect while tests │
│              ✓ DONE                    │  run in real time.    │
└────────────────────────────────────────┴───────────────────────┘
   live SVG · SSE stream                    clickable drawer
```

## Unified sessions

- Each Claude Code or Codex session gets its own thread and URL. A single left
  navigation lists every session and identifies its runtime with a crown.
- Launched agents appear in an Agent team roster with their real names and
  assignments. The semantic workflow remains a separate DAG below it.
- The page header shows the **thread topic** — a short paraphrase of the
  user's last question.
- Every new user question **repaints** the DAG (via `emit.py reset
  "<new-topic>"`).
- Invoking `/semantic-dag` once enables persistent watch mode. The
  `UserPromptSubmit` bridge repaints the same browser thread and supplies a
  compact continuation protocol on later questions; submit `semantic-dag off`
  to stop it.
- Each turn ends with a quiet green status dot and the factual outcome directly
  under the page title; the graph stays unobscured.
- The skill **launches the browser tab automatically** on the first
  turn and reopens it when no viewer is connected.
- Active nodes and their incident edges breathe subtly; the viewer
  reloads itself after implementation updates.
- Nodes show semantic type separately from lifecycle status, and edges
  display typed relationships such as `tested by`, `produces`, and `validates`.
- Re-emitting a stable node ID updates it in place, preserving its status
  and provenance instead of creating a duplicate.
- Terms appear in a dedicated, alphabetical dictionary-style Glossary
  tab, while node-specific concepts remain definition tabs in the drawer.
- Every node drawer shows semantic type and separates files read from files updated.

## Setup

Nothing to install — pure stdlib Python + one static HTML page. Nothing
to start manually.

Semantic DAG is opt-in once per session. Invoke `/semantic-dag` alone or add it
to the first work request; after that, the prompt bridge refreshes the same DAG
for every later question until `semantic-dag off`.

1. **Activate the skill in Claude Code:**
   ```
   /semantic-dag
   ```
   On the next user prompt, the skill runs `emit.py start "<topic>"`,
   which spawns the shared viewer on port 8766 (detached, if it isn't already
   running) and opens a browser tab pointed at that thread's URL.
   http://127.0.0.1:8766 opens the unified session window.

2. **Later-turn persistence is included.** The Cardinal plugin registers
   `hooks/prompt_hook.py` as a `UserPromptSubmit` hook. It is silent unless
   the current DAG has watch mode enabled, and avoids reloading the full skill
   on ordinary continuation turns.

3. **Tool and file attribution is included.** The plugin's quiet `PreToolUse`
   bridge attaches tool metadata only when a Semantic DAG is active. Its
   `PostToolUse` bridge records successful file reads and updates. Direct file
   tools and patch targets are exact; shell attribution is conservative and
   best effort.

## Node drawer

Click a node to see its semantic type, terse current purpose, recent
timestamped notes, agent provenance, tool activity, files read, files
updated, and definition tabs. Subagents, files, commands, concepts, and
tool calls are provenance rather than standalone semantic nodes. There is
no observer chat or sidecar model invocation.

## Layout

- `SKILL.md` — instructions loaded into the main session when active
- `emit.py` — thin Claude entrypoint into `cardinal_core.semantic_dag`
- `viewer/server.py` — stdlib HTTP + SSE, presence, and build-version endpoints
- `viewer/index.html` — hand-rolled SVG typed DAG + node drawer + glossary + title outcome
- `hooks/tool_hook.py` — optional Claude Code hook for auto-attribution
- `hooks/prompt_hook.py` — prompt bridge for persistent cross-turn watch mode

State: `~/.cardinal/state/semantic-dag/threads/<thread>/` (events.jsonl +
dag.json). Per-cwd "current thread" pointer: `current-<sha1-of-cwd>`.

## Reset / finish / rename manually

- Click **reset** in the viewer → clears nodes but keeps thread + topic.
- Run `emit.py reset "<new-topic>"`, `finish "<summary>"`, `topic
  "<new-topic>"`, or `watch off` through the skill-relative helper.

## Change the port

```
SEMANTIC_DAG_PORT=9000 python3 <skill-root>/viewer/server.py
```

## Don't open the browser

```
SEMANTIC_DAG_NO_OPEN=1 python3 <skill-root>/emit.py start "topic"
```
