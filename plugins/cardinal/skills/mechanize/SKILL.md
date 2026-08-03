---
name: mechanize
description: Compile a completed Claude Code session (a past investigation) into a candidate Sentinel DAG plus rationale — a reusable procedure that could later be executed against a similar problem. Use when the user asks to /mechanize, compile a session, or extract a reusable investigation procedure. Spike-quality; produces YAML + rationale, does not execute anything.
---

# mechanize — compile a Claude Code session into a Sentinel DAG

**Spike-quality compiler.** Produces a candidate `sentinel.yaml` + `rationale.md` from a past investigation session. Does NOT execute the Sentinel; that's a separate executor. Does NOT ship — this is exploratory work, and the rationale is where the honesty lives.

## How this skill is invoked

The user typed `/mechanize`, possibly with a session path as argument.

**Argument parsing:**
- If the user provided an argument that looks like an absolute path ending in `.jsonl` → that's `SESSION_PATH`.
- If the user provided a session ID (8+ char hex) → look for `~/.claude/projects/*/${arg}*.jsonl` and use the match.
- If the user provided nothing → **default to the current session**. Users typically don't know session UUIDs, so don't ask for one. Resolve the current session as follows:
  1. Encode the current working directory by replacing `/` with `-` (e.g. `/Users/foo/bar` → `-Users-foo-bar`).
  2. Look in `~/.claude/projects/<encoded-cwd>/` for `.jsonl` files.
  3. Pick the most recently modified one — that's the running session.
  4. Tell the user which session you resolved (short ID + path) in one line before proceeding, so they can interrupt if it's wrong.
  If no `.jsonl` exists under that directory, THEN ask the user to paste a path or ID — but only as a fallback.

**Caveat when compiling the current session:** the tail of the JSONL contains the `/mechanize` invocation itself and any earlier turns since the last checkpoint. Treat everything from the user's `/mechanize` message onward as INCIDENTAL (meta-work, not part of the investigation). Segment on the last substantive investigation conclusion BEFORE the mechanize call.

**Output location default:** `./mechanize-out/<session-id-short>/` under the current working directory. If the CWD is not writable, fall back to `~/mechanize-out/<session-id-short>/`. Tell the user where you're writing.

**Spec location:** `sentinels.md` is co-located with this SKILL.md — read it as `sentinels.md` (relative to the skill directory). Same for `FINDINGS.md`.

## Read the spec first, in this order

Do NOT skip this. The compiler design depends on knowing what a Sentinel is.

1. `sentinels.md` §8 — Sentinel schema (full example)
2. `sentinels.md` §9 — Node model
3. `sentinels.md` §10 — Tool-node contract
4. `sentinels.md` §11 — Function-node contract
5. `sentinels.md` §12 — LLM-node contract
6. `sentinels.md` §13 — Condition-node contract (governs *condition-node expressions only*; tool-argument expressions are covered in "Expression language" below)
7. `sentinels.md` §14 — Emit-node contract
8. `sentinels.md` §14a — Ask-human-node contract (governs when a judgment needs runtime operator ratification)
9. `sentinels.md` §28 + §28.1 — CaptureEvent + adapter contract (esp. attachment rules)
10. `sentinels.md` §29 — Compiler stages (this skill executes them)
11. `sentinels.md` §32 — Analytical-node selection rule (three-way function/llm/ask_human procedure; no optional analytical nodes)
12. `sentinels.md` §37 — Experiment 1 pass criteria (what "success" means)
13. `sentinels.md` §47 — Audit log (what you must produce alongside the DAG)
14. `sentinels.md` §52 — Most important design constraint (do NOT optimize for reuse percentage)

Also read: `FINDINGS.md` — empirical findings from the heuristic spike. Every rule in that document supersedes what a naive reading of the spec might suggest, EXCEPT F3, which is refined in "Spill-to-disk collapsing" below.

## What a Sentinel DAG looks like — concrete example

This is the shape you are producing. Keep it in front of you while compiling.

```yaml
apiVersion: mechanize.dev/v1alpha1
kind: Sentinel
metadata:
  name: post-deployment-error-regression
  version: 0.1.0
spec:
  purpose:
    summary: >
      Determine whether a deployment caused a material increase in
      application errors relative to its recent baseline.
    reusableQuestion: >
      Did a recent change produce a statistically and operationally
      meaningful increase in errors?
    conclusionType: regression-assessment
  inputs:
    service: { type: string, required: true }
    environment: { type: string, default: production }
    baselineWindow: { type: duration, default: 24h }
    minimumIncrease: { type: number, default: 0.25 }
  capabilities:
    required:
      - id: deployments.list
        capabilityType: tool
      - id: telemetry.query-timeseries
        capabilityType: tool
  variationPoints:
    - path: /spec/inputs/service
      operations: [bind]
    - path: /spec/nodes/query-current/config/toolRef
      operations: [replace-binding]
  nodes:
    get-deployment:
      kind: tool
      dependsOn: []
      config:
        toolRef: deployments.list
        arguments:
          service: "${inputs.service}"
          environment: "${inputs.environment}"
      output:
        schema:
          type: object
          required: [deploymentId, deployedAt]
    query-baseline:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          start: "${nodes.get-deployment.output.deployedAt - inputs.baselineWindow}"
          end: "${nodes.get-deployment.output.deployedAt}"
    query-current:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          start: "${nodes.get-deployment.output.deployedAt}"
          end: "${execution.now}"
    compare-rates:
      kind: function
      dependsOn: [query-baseline, query-current]
      config:
        runtime: python3.12
        source: functions/compare-rates.py
        entrypoint: run
        arguments:
          baseline: "${nodes.query-baseline.output}"
          current: "${nodes.query-current.output}"
      output:
        schema:
          type: object
          required: [relativeIncrease, sampleSufficient]
    regression-condition:
      kind: condition
      dependsOn: [compare-rates]
      config:
        expression: >
          nodes.compare-rates.output.sampleSufficient == true &&
          nodes.compare-rates.output.relativeIncrease >= inputs.minimumIncrease
    emit-finding:
      kind: emit
      dependsOn: [get-deployment, compare-rates, regression-condition]
      when: "${nodes.regression-condition.output == true}"
      config:
        finding:
          type: deployment-error-regression
          title: "Error regression after deployment for ${inputs.service}"
          dedupeKey: "${inputs.service}:${nodes.get-deployment.output.deploymentId}"
  outputs:
    finding:
      value: "${nodes.emit-finding.output}"
      required: false
  execution:
    concurrency: 1
    failureMode: fail-fast
    defaultTimeout: 5m
```

Note the shape:
- Stable, semantic node IDs (`get-deployment`, `query-baseline`) — never `tool-1`, `step-7`.
- Explicit `dependsOn`.
- Explicit expressions using `${inputs.x}`, `${nodes.y.output.z}`, `${execution.now}`.
- Explicit input contract with types and defaults.
- Explicit output contract per node (schema shape).
- Explicit capability contract (abstract IDs, not vendor tool names).
- Variation points declared up front.

## Compilation flow — walk these stages in order

### Stage 1 — Read and segment

Read the JSONL. Each line is a JSON object with a `type` field:

- `type: "user"` messages carry user text (from message.content text blocks) and tool_result blocks (message.content type=tool_result, linked by tool_use_id).
- `type: "assistant"` messages carry assistant text and `tool_use` blocks (message.content type=tool_use).
- Non-text content blocks — type=`image`, type=`document` — are **attachments**. Do NOT decode them. Note only their kind, mimeType (source.media_type), and size in bytes.

Produce a mental model of:
- **Objective**: first substantive user text (skip `<local-command-caveat>` prefixes and slash-command entries).
- **Tool calls**: ordered list of tool_use blocks with their ordinal, name, input, and paired tool_result content.
- **Attachments**: any image/document blocks; where they appear.
- **Conclusion**: last substantive assistant text block(s).

### Stage 1.5 — Recognize spill-to-disk pairs (refines F3)

Claude Code truncates tool_results that exceed a token budget. When it does, the tool_result body carries a marker like:

> `Output has been saved to /Users/<user>/.claude/projects/<encoded-cwd>/<session>-<uuid>.json` (or `.txt`)

The operator then recovers by running a Bash call (typically `jq`, `cat`, `head`, `tail`, or `python -c`) whose input path IS that spill path.

**These pairs are ONE logical operation, not a meta-tool call and not two separate steps.** Collapse them at segmentation time:

- The tool_use that produced the spill remains the semantic node.
- Its logical `output` for compilation purposes is the *extracted* portion of the spill — the projection the follow-on Bash call actually took. If the operator ran `jq -r '.summary'`, the node's compiled output shape retains `summary` as the load-bearing field.
- The follow-on Bash call is NOT a separate node. It is recorded in the audit log as `collapsed-into: <preceding-node-id>` with reason `spill-projection`.

**F3 REFINEMENT.** The heuristic-spike F3 rule "filter tool calls whose input references `~/.claude/`" is too broad. Apply this refined rule:

- A `bash.jq/cat/head/tail/python` call whose input path is exactly a spill path referenced by a preceding tool_result is a **spill-projection** — collapse into the preceding node, do NOT mark INCIDENTAL.
- A tool call whose input references `~/.claude/` for any *other* reason (poking the session file after conclusion, listing project directories for meta-work) IS INCIDENTAL. Preserve F3's intent for those cases.

Discrimination heuristic: if the spill path appears verbatim in an earlier tool_result within the same session, treat as spill-projection. Otherwise INCIDENTAL.

### Stage 2 — Classify each tool call (§29 stage 3)

For each tool call, assign exactly one:

- **REQUIRED** — produced evidence directly cited or used by the conclusion. Retain.
- **SUPPORTING** — needed for reproducibility or confidence but not directly cited. Retain when unclear.
- **EXPLORATORY** — hypothesis-driven probe that returned nothing useful or refuted a hypothesis. Omit from DAG; note in audit.
- **FAILED** — attempted but errored. Omit from DAG.
- **INCIDENTAL** — meta-work not part of the investigation. Includes tool calls referencing `~/.claude/projects/` UNLESS they are spill-projections (see Stage 1.5), and any call occurring after the terminal assistant conclusion. Omit.
- **LOCAL_ONLY** — depends on operator's local machine state that cannot be parameterized. Omit or convert to input.
- **COLLAPSED** — spill-projection Bash call absorbed into a preceding REQUIRED/SUPPORTING node per Stage 1.5. Do NOT double-classify.

For each Bash call, determine its **synthetic capability ID** from `argv[0]`: `bash.grep`, `bash.kubectl`, `bash.git`, `bash.gh`, `bash.jq`, `bash.find`, `bash.ls`, `bash.cat`, `bash.mv`, `bash.curl`, `bash.python`. Preserve the raw tool name; add the synthetic ID for capability binding.

### Stage 2.5 — Code-reading compression policy

Many investigations include a sub-pattern:

> Operator runs several `grep` + `Read` calls to understand what a metric, function, config value, or emission site actually means. The *semantic understanding* they derive becomes load-bearing for the final classification.

For each such cluster, the compiler must PICK ONE of these options and record its choice in the rationale:

**Option A — LLM node with directory access.**
- Add an `llm` node whose task is to read the emission-site code under `${inputs.codeRepoPath}` and return a structured claim about semantics/dimensions/tolerances/shape.
- Evidence: the grep-result summary. Model policy: analytical-small.
- **Pros:** preserves the operator's judgment mechanically; a future run against a different target re-does the analysis.
- **Cons:** LLM node in every execution; must justify per §32; output must be schema-valid and support `inconclusive`.
- **Pick this when:** the code-reading judgment is generalizable across targets and the downstream logic depends on it. Per §32, this node is REQUIRED (not gated) — `codeRepoPath` becomes a required input.

**Option B — function node fed by grep output that extracts specific values.**
- Add a `function` node that parses the grep matches for concrete constants (max-age table, level count, expected cardinality) and emits them as typed outputs.
- **Pros:** deterministic; cacheable; testable.
- **Cons:** requires the code emit values in a machine-extractable form; brittle across languages/codebases; usually needs a per-project fixture.
- **Pick this when:** the operator was extracting NUMBERS or ENUMS from code, not making qualitative judgments.

**Option C — compress into structured operator inputs.**
- Encode the operator's *conclusion* as a plain input; drop the code-reading tool calls; add an OPTIONAL grep node for reviewer context only.
- **Pros:** simplest Sentinel; no LLM cost; a specialist reviewer sees the reasoning in the input names.
- **Cons:** genuine fidelity loss. A generalist running this Sentinel against an unfamiliar target must reproduce the code reading themselves and pick the same input values without guidance.
- **Pick this when:** the reusable question is narrow enough that operators re-running the Sentinel will be domain-familiar, OR the code-reading judgment is too idiosyncratic to encode.

**Decision procedure:**
1. Was the code-reading extracting typed CONSTANTS (numbers, enums, table entries)? → Option B.
2. Was the code-reading making a QUALITATIVE JUDGMENT that depends on reading whole functions/comments? → Option A if `codeRepoPath` is a realistic runtime input, else Option C.
3. Was the code-reading a one-time SEMANTIC ORIENTATION that a future operator won't need? → Option C with the emission-locate node as OPTIONAL context.

Whichever option is chosen, the rationale MUST name it explicitly and state what fidelity is lost.

### Stage 3 — Extract procedure signature (§25)

State the vendor-independent procedure the investigation followed. Example:

```
objectiveClass: metric-anomaly-explanation
evidencePattern:
  - metric-emission-source
  - metric-time-series
  - dimensional-breakdown
transformations:
  - identify-emission-code-path
  - group-by-dimensions
  - detect-stuck-values
judgments:
  - metric-correctness-classification
outputClass: metric-integrity-finding
```

If the investigation does not have a coherent procedure — for example, if it's task execution ("do X, then Y, then Z") rather than investigation ("why is X happening") — **stop here** and produce an audit report explaining why compilation is not appropriate. Do not force a Sentinel out of a task-execution session. This is the §40 negative-reuse discipline restated.

**How to tell:** the conclusion of an investigation *classifies* or *explains* ("the metric is stuck because...", "this is caused by X"). The conclusion of a task execution *reports actions* ("Done. Rebased X. Sent Y."). If your conclusion is the second shape, refuse compilation.

**Mixed phases:** some sessions have BOTH an investigation phase and a task-execution phase (e.g., "investigate this error, then implement the fix"). Split at the boundary — compile the investigation phase, refuse the task phase, and note the split in the rationale.

### Stage 3.5 — Mechanization scan

Between Stage 3 (procedure signature) and Stage 4 (DAG synthesis), scan the investigation for operator-side work that could be a `function` node in the compiled Sentinel. Stage 4's §32 rule already handles work the operator explicitly *framed* as a judgment (three-way `function | llm | ask_human` choice). Stage 3.5 targets the work the operator did WITHOUT framing it as a judgment — glue extractions, prose aggregations, ad-hoc bash reshapes — that the default synthesis will otherwise leave un-mechanized (leaking as raw arrays into `emit.evidence`, expression-language references that reduce to hand-computed constants, or `llm` nodes that could have been deterministic).

**Where to look.** The compressed Stage 2 classification table. For each REQUIRED tool call and the assistant turn that immediately follows it, check the patterns below.

**Patterns v1 — additive as new patterns become common in the wild. Do not invent new patterns without a grounded example; drift into speculative mechanization inflates DAG size for no gain (§52).**

**M1. series-statistic-reduction.** Operator states one or more summary statistics ("peak X", "avg Y", "p95 Z", "fraction of window ≥ N", "monotonically increasing") over a raw time-series tool result, then uses those statistics — not the raw series — in a downstream tool call, `condition` expression, or `emit.evidence`. Insert a `kind: function` node between the source tool and its consumers that computes the stated statistics deterministically. Suggested signature: `def summarize_series(points: list[float], stats: list[str]) -> dict[str, float]:`. Cite in rationale which prose statement the function replaced.

**M2. cross-source-quantity-reconciliation.** Two independent REQUIRED tool calls answer the same real-world question about a count/quantity, and the operator's prose is a match/mismatch claim ("A reports 321 errors, B returns empty — mismatch"). The mismatch is often THE finding. Insert a `kind: function` node that computes `{agrees, delta, explain}` from the two outputs; wire the two source tools as its `dependsOn` and the emit/condition consumers to it. Suggested signature: `def reconcile_counts(source_a: dict, source_b: dict, subject: str) -> dict:`. Highest-criticality pattern in the sampled corpus — often the load-bearing finding.

**M3. json-field-extract-and-carry.** Operator receives a structured tool result, extracts one field (often nested and often `[0]`), and hand-carries it as an argument to the next tool call — or as a scalar in `emit.attributes`. Insert a `kind: function` node that does the extraction as a pure `pick` operation, wired between producer and consumer. Suggested signature: `def pick_field(response: dict, path: str) -> Any:`. This is the pattern that has repeatedly produced `pick-resolved-service`-style nodes in existing compiles; Stage 3.5 generalizes it to messier cases (nested paths, first-non-empty, threshold-filtered picks).

**Refuse to mechanize when:**
- The operator's judgment is not deterministic ("this looks suspicious" — no reducible criterion). Route to §32.
- The transformation needs world knowledge not present in the inputs (unit conversions that depend on a runtime FX rate; format normalization that requires a schema the compiler doesn't have).
- The tool call sits in Stage 2 EXPLORATORY class — mechanizing dead ends bakes the operator's guesses into the Sentinel.
- The prose statement summarizes across items the executor won't have at replay time (e.g. "these are the only three services in this cluster" — a claim about the world, not a deterministic function of the tool output).

**Output.** Append a `mechanization-candidates` list to Stage 3's procedure-signature output — one entry per pattern match:

```
- pattern: M1 | M2 | M3
  source_tool_call_ordinal: N          # or list of two ordinals for M2
  function_signature: "def ..."
  replaces_prose_at_message: M         # ordinal of the assistant turn where the operator did this by hand
  downstream_consumers: [<node-id-or-ordinal>, ...]  # who currently uses the hand-computed value
```

**Handoff to Stage 4.** Stage 4 consumes this list: for each candidate, add a `kind: function` node with the specified signature, wire it as `dependsOn` on the source tool(s), and rewrite every downstream consumer to reference the function's output instead of the raw upstream output or a hand-computed constant. Cite the M-pattern name in the rationale for each mechanized node.

**Boundary with §32.** Stage 3.5 is about work the operator did WITHOUT framing it as a judgment (glue, aggregation, extraction — the operator was the deterministic transformation). Stage 4's §32 rule handles work the operator DID frame as a judgment (three-way `function | llm | ask_human`). A candidate from Stage 3.5 always becomes a `function` node — that's its definition — and Stage 4 does not reclassify it.

### Stage 4 — Synthesize the DAG (§29 stages 7–8)

Now produce YAML in the shape above. Rules:

- **Function-node runtime:** for v0, function nodes MUST emit `runtime: python3.12` and `source: functions/<node-id>.py`. Node.js and other runtimes are a future concern; do NOT emit them.
- **Consume Stage 3.5's `mechanization-candidates` list.** For each entry: add a `kind: function` node with the specified signature and node-id derived from what it does (per Node-ID style guide). Wire the source tool_call ordinal(s) as `dependsOn`, and rewrite every listed downstream consumer to reference this function's output. Cite the M-pattern name in the rationale entry for the node. Stage 3.5 mechanizations are `function` — do NOT reclassify them via §32.
- **Node kinds — three-way analytical selection per §32.** For every step that produces a judgment:
  1. Can it be a deterministic transformation over declared inputs? → `function`.
  2. Otherwise, is it qualitative AND safe to delegate to an LLM without human ratification (downstream nodes only produce read-only side effects like findings)? → `llm`. Record `judgmentJustification` including `deterministicAlternativeConsidered`, `reasonRejected`, and `delegationSafetyConsidered`.
  3. Otherwise (requires human ratification before downstream nodes act) → `ask_human`. Record `judgmentJustification` explaining the ratification need.
- **No optional analytical nodes.** Do not write `when: "${inputs.X != null}"` on any `llm` or `ask_human` node. If a judgment is on the reusable procedure's critical path, its node is required. If it's sometimes needed, either the input carries the judgment's output (and the node is absent) or it's a Variation.
- **Node IDs:** semantic and stable — describe what the node does, not its order. Choose them as if this is the ONLY chance you'll get (see Stage 6's stability freeze).
- **Edges:** derive from actual data flow. If node B's input needs a value produced by node A, add A to B's dependsOn.
- **Inputs:** extract literals that look like inputs (service name, environment, time range, thresholds). Leave literals that are investigation-domain constants inline (metric names, code identifiers).
- **Attachments:** apply the Attachment chooser (Stage 4.5).
- **Variation points:** declare which input bindings, tool bindings, thresholds, and node replacements should be exposed to future Variations.

### Stage 4.5 — Attachment chooser

For every retained action whose input references an attachment (§29 stage 3 lists four allowed dispositions), the compiler MUST answer the following questions in order. Take the FIRST option that fires:

**Q1. Did the operator's INFERENCE from the attachment become a downstream Sentinel input?**
For example: the operator saw a screenshot of a metric spike and *decided* the metric to investigate is `X`. The name `X` then flows into every subsequent tool call. The attachment's role is anchoring; the operator's extracted claim is the load-bearing fact.
→ Encode the extracted claim as one or more plain-typed Sentinel inputs (`metricName: string`, `service: string`, etc.). Record the attachment in `provenance.omittedAttachments[]` with `disposition: replaced-by-plain-input` and a reason naming the specific inputs that replace it.

**Q2. Would a shipping executor genuinely need the attachment at runtime to make the same judgment?**
For example: an investigation whose central claim depends on visual comparison ("does this chart show a bimodal distribution?"). No plain-typed input can honestly stand in.
→ Emit the attachment as a Sentinel input of type `image`, `pdf`, or `binary`. Downstream nodes reference it via `${inputs.<name>}`. Note: v0 has no multimodal function-node runtime; only an `llm` node may reference it, and even then only when a multimodal capability is declared as required.

**Q3. Is the attachment nice-to-have CONTEXT for a human reviewer of the finding but not needed for the DAG to reach its conclusion?**
For example: a screenshot pasted alongside a text objective that also fully describes the situation.
→ Record `disposition: requires-manual-input` in the audit and stop generation until a reviewer confirms, OR `disposition: omit` if the operator provided sufficient text context in the same message. Prefer `omit` when the text alone suffices; prefer `requires-manual-input` only when the attachment carries information not present in text.

**Q4 (always). NEVER describe attachment content as evidence.**
Do not generate prose that purports to summarize what a screenshot shows and use that prose as a fact in the Sentinel. This is a hallucination cloaked as compilation.

Order matters: Q1 fires first because it is the most common outcome for text-based agent sessions. Q2 fires only when the modality itself is load-bearing. Q3 is the "in doubt" branch.

Record the answer in `rationale.md` under a subsection `Attachment handling` naming the disposition and the question that fired.

### Stage 5 — Validate (§49 layers 1–4)

Check the emitted DAG for:

1. **Schema validity** — matches §8 shape.
2. **Referential validity** — every `dependsOn` target exists; every `${nodes.x.output.y}` reference resolves.
3. **Graph validity** — acyclic; declared outputs reachable.
4. **Type validity** — arguments look plausibly typed. Every expression uses only functions declared in "Expression language" below.

Plus semantic quality checks:

5. **Semantic drift** — does the DAG actually represent the original investigation? Or has the synthesis stage drifted into producing something plausible-looking but different?
6. **Honest LLM nodes** — is anything marked `kind: llm` that could plausibly be deterministic? If yes, prefer function. Is anything marked `kind: llm` that should not be autonomously delegated? If yes, prefer ask_human.
7. **No-optional-analytical discipline** — is any `llm` or `ask_human` node gated by `when:`? If yes, restructure per §32.
8. **Attachment discipline** — did any attachment content get inlined as evidence text? If yes, remove it. Was the Stage 4.5 chooser applied? If not, apply it now.
9. **Spill-projection collapse discipline** — is any spill-projection Bash call still present as its own tool node? If yes, collapse into its preceding node.

### Stage 5.5 — Cold ratification (semantic judge)

Stage 5 covers mechanical validity (schema/refs/graph/type + drift). Stage 5.5 is a semantic pass: does the DAG follow every SKILL rule that isn't structurally enforceable in Stage 5? Run this as a **cold subagent** via the Agent/Task tool — do NOT reason inline. An inline "then judge yourself" pass inherits the compiler's blind spots; the whole point is a cold read by a reviewer who doesn't know why any decision was made.

The subagent gets: absolute paths to the fresh `sentinel.yaml` and `rationale.md`, plus the ratification checklist below. It reads them cold (no compilation context), evaluates each rule literally against the YAML, and returns the verdict block. Do NOT ask the subagent to opine beyond the checklist — the point is a rules pass, not a judgment call.

**Ratification checklist v1 — additive over time. Every new failure mode observed in the wild becomes a numbered rule here.**

**R1. Variation-point completeness.** For every input under `spec.inputs` that has a `default:` AND is templated anywhere in the YAML as `${inputs.<name>}` (in a tool argument, function argument, or expression), there MUST be an entry in `spec.variationPoints[]` with `path: /spec/inputs/<name>/default` and a non-empty `operations:` list (typically `[replace]`, or `[bind]` for identity-shaped inputs like a service selector). Missing entries FAIL — cite the offending input name(s) and the tool/function node(s) that reference them.

**R2. Capability-ID abstraction.** Every `spec.capabilities.required[].id` MUST use an abstract prefix listed in the "Known capability registry" section below (`observability.*`, `code.*`). Vendor-shaped IDs (`lakerunner.*`, `datadog.*`, `prometheus.*`, `grafana.*`, etc.) FAIL. If a needed abstract capability isn't in the registry, the ID must appear in a `capability-registry-extension-needed` note in `rationale.md`; a vendor-shaped ID with no such rationale note FAILS.

**R3. Function-vs-LLM discipline.** Every `kind: llm` node MUST have a rationale paragraph in `rationale.md` explicitly justifying why it isn't `kind: function` per §32. An `llm` node without that justification FAILS.

**R4. Node existence.** Every node id cited in `rationale.md` (Stage 2 classification table, invariants, judgment-call flags, unresolved-issues section, etc.) MUST exist under that exact name in `sentinel.yaml`. Hallucinated names FAIL — cite the offending name and where in the rationale it appeared.

**R5. Emit dedupeKey decomposability.** Every `emit` node's `dedupeKey` MUST be a stable string composed only of `${inputs.*}`, `${nodes.*.output.*}` references, and literal separators. No `${execution.now}`, no `${uuid()}`, no free text that could vary between runs. FAILS on any time-varying or free-text component.

**R6. toolRef ↔ capability referential integrity.** Every `spec.nodes[].config.toolRef` value MUST appear as an `id` in `spec.capabilities.required[]`. Every entry in `spec.capabilities.required[]` MUST be referenced by at least one node's `toolRef`. Orphan capabilities (declared but unused) and dangling toolRefs (referenced but undeclared) both FAIL — cite the offending id and side (orphan | dangling). This catches the class of inconsistency that a naive rename can introduce: renaming a capability id under `capabilities.required` without also renaming its toolRef in the node using it (or vice versa).

**Verdict format the subagent MUST return — report ONLY this block, no preamble:**

```
VERDICT: RATIFIED | REVISE

R1 [PASS|FAIL]: <one line — cite offending input name(s) + referencing node id(s) if FAIL>
R2 [PASS|FAIL]: <cite offending capability id(s) if FAIL>
R3 [PASS|FAIL]: <cite offending llm node id(s) if FAIL>
R4 [PASS|FAIL]: <cite hallucinated name(s) + where in rationale if FAIL>
R5 [PASS|FAIL]: <cite offending emit node id(s) + dedupeKey substring if FAIL>
R6 [PASS|FAIL]: <cite offending id + side (orphan | dangling) if FAIL>

If REVISE — fix list (mandatory, one entry per FAIL rule, phrased as a concrete YAML edit):
  - <Rn>: <e.g. "add `- path: /spec/inputs/aggregationBucket/default\n  operations: [replace]` to spec.variationPoints">
  - ...
```

`VERDICT: RATIFIED` requires ALL rules PASS. Any FAIL → `REVISE` and the fix list is mandatory.

### Stage 6 — Iterate (max 3 rounds) with node-ID stability freeze

If Stage 5 OR Stage 5.5 reports issues:
- **Round 1:** fix all Stage 5 errors + apply every Stage 5.5 fix-list item, refine node IDs freely, re-run Stage 5 AND re-invoke Stage 5.5's cold subagent. **At the end of Round 1, node IDs are FROZEN.**
- **Round 2:** fix remaining Stage 5 errors + remaining Stage 5.5 fix items + as many warnings as reasonable. NODE IDs MAY NOT CHANGE. If a rename would materially improve the graph, that is a Stage 4 decision — either accept the current name or restart from Stage 4 (which resets the freeze).
- **Round 3:** emit the best DAG you can plus an unresolved-issues section in the rationale. NODE IDs STILL FROZEN.

If after 3 rounds Stage 5 is still invalid OR Stage 5.5 verdict is still `REVISE`, emit what you have plus a **failure report** listing every unresolved Stage 5 error AND every unresolved Stage 5.5 fix item. Also add a `metadata.ratification` block to `sentinel.yaml`:

```yaml
metadata:
  name: <sentinel name>
  version: <version>
  ratification:
    status: revise
    unresolved:
      - <Rn>: <one-line reason>
```

so downstream tools (executor, PR review, matcher) know not to silently trust the artifact. **Do not** hide failures.

**Rationale for the freeze rule:** §9 requires stable node IDs because Variations patch nodes by path. If iteration renames nodes freely, no Variation authored between rounds would resolve. Freezing after Round 1 gives one editorial pass to pick meaningful IDs, then locks them.

**Rationale for the cold subagent in Stage 5.5:** the ratification pass targets exactly the class of failure the compiler is prone to (VP omission for duration-in-selector inputs, vendor-shape leak from the source session's tool names). An inline judge running in the same context as the compiler inherits the same reasoning that produced the failure. A cold read catches these because the reviewer doesn't know why any decision was made.

### Stage 7 — Emit outputs

Write these files to `OUT_DIR`:

1. `sentinel.yaml` — the final Sentinel candidate.
2. `rationale.md` — for each retained node: which tool_use ordinal(s) it derived from, why this node kind, what was preserved verbatim, what was generalized, what was guessed. Also list every tool call NOT retained with its classification and rationale. Include an `Attachment handling` subsection per Stage 4.5 and a `Code-reading option chosen` subsection per Stage 2.5.
3. `audit.jsonl` — per §47, one entry per capture event with the compiler's decision. Skip if too expensive — but the rationale.md is mandatory.

At the end, tell the user (a) where you wrote the files, (b) the top-line summary (node count by kind, procedure signature), (c) what you refused to compile and why (task-execution phases, mixed-phase splits), (d) the largest single judgment call you had to make.

## Known capability registry

Emit only these capability IDs (per §10 abstract-capability rule). Do NOT emit vendor-shaped IDs (`lakerunner.*`, `datadog.*`, etc.).

- `observability.list-services` — enumerate services matching a filter
- `observability.error-overview` — per-service error rollup
- `observability.query-metrics` — time-series metric query
- `observability.query-logs` — LogQL-shaped log query
- `code.grep` — recursive grep over a filesystem path
- `code.read` — read a file

Rule: when the SKILL wants to emit a capability whose ID isn't in this table, it must either (a) map to an existing compatible one, or (b) declare it and flag `capability-registry-extension-needed` in the rationale.

## Expression language

Sentinel expressions appear in **three** places with **different** allowed subsets:

**A. Condition-node expressions** (`nodes.<id>.config.expression` for `kind: condition`) — governed by §13.
- Boolean operators, numeric comparisons, string equality, null checks, array length.
- References to `inputs.*` and `nodes.*.output.*`.
- Pure functions: `abs`, `min`, `max`, `contains`.
- No I/O, no string manipulation beyond equality.

**B. Tool-argument and finding-body expressions** (`${...}` interpolations in `arguments:`, `title:`, `dedupeKey:`, `attributes:`, `severityExpression:`) — §13 does NOT govern these. The pragmatic ruling, pending spec clarification:
- Everything condition-node expressions allow.
- String concatenation via interpolation: `"${inputs.x}:${inputs.y}"`.
- Nested `${...}` inside a string is permitted; the executor evaluates innermost expressions first and substitutes.
- Pure functions: `join(array, separator)`, `format(template, ...args)`.
- Arithmetic on time and numeric inputs: `${execution.now - inputs.window}`.
- Ternary expressions in `severityExpression` per §8 example: `cond ? "critical" : "warning"`.

**C. `when:` gates on nodes** — treat as condition-node expressions (subset A). Renders a boolean; no string manipulation needed.

**Spec-question flag:** the spec (§13) does not explicitly enumerate a tool-argument expression grammar. Subset B above is what the skill assumes; flag as `spec-clarification-needed` in the rationale's "Unresolved" section if the compilation depends on it. Do NOT invent new functions beyond subset B without flagging.

## Node-ID style guide

Node IDs must describe **what the node does** in a way another operator, reading the Sentinel cold, understands without context.

**Meaningful (good):**
- `query-metric-timeseries` — says what capability, what shape of output.
- `detect-degeneracy` — says what the function decides.
- `emit-degeneracy-finding` — says what side effect and about what.

**Pretty but not meaningful (bad):**
- `main-query` — pretty; tells you nothing about the query.
- `analyze` — tells you nothing about what's being analyzed or how.
- `step-primary` — ordinal masquerading as semantic.

**Ordinals (worst):**
- `tool-4`, `step-7`, `node-1` — carry zero semantic information; forbidden per §9.

Test: read the node ID out loud without any surrounding context. If it doesn't answer "what does this node do?", pick a different ID before Round 1's freeze.

## What you MUST NOT do (§52 restated)

- Do NOT invent tool outputs. If a tool_result was empty or errored, mark that node's classification honestly.
- Do NOT describe an image's content as evidence. Ever.
- Do NOT produce a Sentinel from a task-execution session (see stage 3). Refuse cleanly with an audit report.
- Do NOT optimize for a large DAG. A 3-node Sentinel from a real investigation beats a 15-node Sentinel that pretends the operator's exploratory dead ends were required.
- Do NOT emit `tool-4` / `step-7` style IDs. Meaningful IDs are required, not preferred.
- Do NOT rename node IDs after Round 1 iteration (Stage 6 freeze).
- Do NOT treat spill-projection Bash calls as INCIDENTAL — collapse them per Stage 1.5.
- Do NOT invent expression-language functions beyond those enumerated above.
- Do NOT emit vendor-shaped capability IDs (`lakerunner.*`, `datadog.*`) — use the abstract registry above.
- Do NOT emit function-node runtimes other than `python3.12` for v0.
- Do NOT gate `llm` or `ask_human` nodes with `when:` on their own inputs — §32 prohibits this.
- Do NOT read prior compilation outputs of the same session (if any exist in the OUT_DIR from a previous run) before writing your own.
- Do NOT skip the rationale.md. Without it, no reviewer can tell whether the compilation was honest.

## Success criterion

You produced:
- A `sentinel.yaml` that is structurally valid, OR a `refusal-report.md` that honestly explains why compilation didn't complete.
- A `rationale.md` that a human reader could use to audit every compilation decision, including the code-reading option chosen (Stage 2.5), the attachment disposition (Stage 4.5), and the analytical-node kind selection per §32 (Stage 4).

If a human reads the rationale and says "yes, this is what the investigation was, and here's why these nodes exist" — the compilation succeeded. If they say "this is a plausible-looking DAG but doesn't match what I did" — the compiler surfaced a weakness worth flagging.
