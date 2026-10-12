---
name: checks
description: Build and run a Cardinal check (preview) — a reusable, evidence-backed decision made for every trace in a time range, with a contract the user approves, approved examples, and per-trace results with evidence. Use when answering needs a judgment about each trace that one aggregate query cannot give — "how often does X happen", "find traces where…", "flag any…", "check whether … for <time range>" — such as relationships between events inside one trace, what an agent said versus what its tools returned, or a rule the user wants to reuse. Do not use for counts, error rates, top endpoints or latency; answer those with the Cardinal query tools.
user-invocable: false
---

# checks — per-trace decisions over telemetry (preview)

A **check** decides, for every trace of one service in a time range, whether it matches a
plain-language rule, and shows the evidence for each decision. Users never see the words
"Behavioral Program", "DiagnosticVersion" or "UDF", and never type a trace ID, write Python
or call a developer tool: you do all of that with the tools named below.

**Read [CORE.md](CORE.md) in full before the first step.** It is the guided flow (route,
scope, contract, author, examples, evidence sufficiency, labels, teach, run, results,
storyboard, reuse) and the lessons that keep checks honest.

## Tools this skill drives

- Cardinal query tools (`mcp__plugin_cardinal_cardinal__lakerunner__*`, instance `prod`
  unless the user names another): services, field values, spans, logs. Use them to answer
  directly, to scope, to find example traces and to measure trace extents.
- Check tools (`mcp__plugin_cardinal_cardinal-behavior__*`), unchanged:
  `get_behavior_sdk`, `compile_behavior`, `test_behavior`, `inspect_behavior`,
  `get_behavior_compilation`, `run_behavior_regressions`, `accept_behavior`,
  `execute_behavior_range` / `continue_behavior_range` (any time range, run in the
  background; listen with Monitor on `cardinal-checks range watch <range_id>`),
  `get_behavior_execution`, `render_storyboard`. `execute_behavior` /
  `next_behavior_result` stay available for a single window of at most one hour.
- `cardinal-checks` (on PATH via this plugin's bin/), the teaching-label ledger:
  `cardinal-checks example set|approve|remove|expected|show --draft <id> …` and
  `cardinal-checks accepted [--service S]` for checks accepted earlier.

Check executions only reach the organization's production LakeRunner. A service that only
exists on another instance (for example `otel-demo`) cannot be checked: say so and stop.

## Claude Code specifics

- Turning the preview off (`cardinal-checks off`) removes this skill; if it was off you
  would not be reading this. Users turn it on with `cardinal-checks on` and a new session.
- Long tool results land in files the tools name (`receipt_file`, `summary_file`,
  `result_file`, `events_file`); read them with the Read tool or `jq` rather than asking for
  the full payload again.
- A range run is listened to, never polled: Monitor on the `watch_command`, re-armed on
  expiry, gets one notification per event while you keep working.
