---
name: semantic-dag
description: Paint a live typed semantic DAG of the current Claude session in a browser tab. Use when the user wants to watch work evolve as a graph rather than read a scrolling transcript.
---

# Semantic DAG

Drive a live graph at `http://localhost:8765/t/<thread>`. It is a compact semantic memory of the task, not a tool-call trace.

The helper is `emit.py`, resolved relative to this file. Replace `<emit>` below with its absolute path.

## Turn boundary

Activation is opt-in once per Claude session, not once per prompt. The user may
invoke `/semantic-dag` by itself or include it with the first work request.
Run `start` on that invocation so watch mode is bound to the session; every
later prompt then repaints the same viewer automatically without another skill
mention. Do not start a DAG for unrelated sessions where the user never opted in.

On the first active turn run:

```bash
python3 <emit> start "<2–6 word topic>"
```

`start` enables persistent watch mode. The installed `UserPromptSubmit` bridge repaints the same thread and reactivates this skill on later prompts, including when `/semantic-dag` was invoked in a separate turn. On each later user turn run `reset "<new topic>"`; the bridge may already have done the repaint, but the reset remains safe. At the end of every turn, including blocked or failed turns, run `finish "<factual one-line outcome>"` immediately before the final answer. `finish` leaves watch mode enabled; the user can submit `semantic-dag off` or run `watch off` to disable it. Keep terminal commentary to one sentence at meaningful transitions.

## Semantic ontology

Each node has one `type`, independent of its `status`. Use only:

- `GOAL` — desired end state.
- `QUESTION` — unresolved question that affects the work.
- `HYPOTHESIS` — candidate explanation that can be confirmed or rejected.
- `DECISION` — meaningful choice that constrains future work.
- `WORK` — substantial investigation, implementation, analysis, or verification phase.
- `EVIDENCE` — durable observation that supports or refutes something.
- `OUTCOME` — meaningful result, resolution, or completed state.

Use only: `decomposes_into`, `raises`, `tested_by`, `supported_by`, `refuted_by`, `resolved_by`, `based_on`, `leads_to`, `depends_on`, `produces`, `implements`, `validates`, `supersedes`.

Read relationships left to right, for example `hypothesis refuted_by evidence` and `work produces outcome`.

Statuses are separate lifecycle or disposition values: `pending`, `active`, `paused`, `completed`, `confirmed`, `rejected`, `superseded`, `resolved`, `error`. Keep distinctions such as `HYPOTHESIS(status=rejected)`, `DECISION(status=superseded)`, and `WORK(status=active)`.

## Node-worthiness and granularity

Before adding a node, ask:

> Would a future agent want to retrieve this item independently and understand how it relates to the rest of the work?

If not, keep it as metadata. Never make semantic nodes for individual tool calls, commands, files, glossary concepts, narration, or subagents. Attach those with `tool`, `file`, `concept`, `note`, or agent provenance. One `WORK` node may contain dozens of tool calls.

Reuse a stable ID when revisiting an item. `add` updates an existing node's type, label, and supplied description while preserving status and provenance; do not create near-duplicates.

## Commands

```bash
python3 <emit> add <id> <TYPE> "<label>" [--parent <id> | --root] [--relation <relationship>] [--description "<terse description>"]
python3 <emit> link <from-id> <relationship> <to-id>
python3 <emit> activate <id>
python3 <emit> status <id> <status> ["<reason>"]
python3 <emit> done <id>
python3 <emit> error <id> "<reason>"
python3 <emit> describe <id> "<updated description>"
python3 <emit> note <id> "<one-line narration>"
python3 <emit> tool <id> "<tool-name>" "<summary>"
python3 <emit> file <id> <read|updated> "<path>"
python3 <emit> concept <id> "<term>" "<definition>"
python3 <emit> define "<term>" "<definition>"
python3 <emit> undefine "<term>"
python3 <emit> watch <on|off>
```

`done` aliases `status <id> completed`. Use `status` for confirmed, rejected, resolved, and superseded items.

Emit and activate a node before doing its work. Labels must be concrete 2–7 word domain phrases that remain meaningful without the drawer. Never use placeholders such as `Phase 5`, `Stacking Phase 5`, or `Next Step`.

An explicit `--parent` defaults to `decomposes_into`; automatic chaining defaults to `leads_to`. Use `--relation` when another typed relationship is more accurate. A new node without `--parent` or `--root` chains to the most recent node from the same agent. Use `--root` for an independent semantic root.

Descriptions explain the live item. `activate` automatically seeds the node's first narration entry from its description when it has no notes. Immediately before sending a progress commentary update, mirror that same user-facing sentence with `note` on the active node; do not defer narration until the end. Add 1–3 further notes only for facts or changes worth reading. File events record every materially read or changed file and never become nodes. `concept` adds a contextual drawer tab and dictionary entry; `define` is for important turn-wide terms, not ordinary verbs, commands, filenames, or tool names.

## Subagent provenance

Subagents are metadata, not semantic nodes. To aggregate one into the parent graph, start it with:

```bash
SEMANTIC_DAG_THREAD=<parent-thread> SEMANTIC_DAG_AGENT=<agent-id> \
  python3 <emit> start "<delegated task>" --parent <owning-node>
```

The start event registers agent metadata only. Its first typed `add` attaches to the owning semantic node if no other parent is supplied. Storage keys are `<agent>::<id>`, each agent may have one active node, and parallel agents may pulse concurrently. A subagent `finish` completes only its own nodes; only the parent finishes the whole graph.

## Controls

```bash
python3 <emit> reset "<new topic>"
python3 <emit> topic "<new topic>"
```

Set `SEMANTIC_DAG_PORT` to change the port, `SEMANTIC_DAG_NO_OPEN=1` to suppress opening a tab, or `SEMANTIC_DAG_NO_SERVER=1` for headless testing.
