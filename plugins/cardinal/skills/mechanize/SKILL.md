---
name: mechanize
description: Compile a completed Claude Code session (a past investigation) into a candidate Sentinel DAG plus rationale — a reusable procedure that could later be executed against a similar problem. Use when the user asks to /mechanize, compile a session, or extract a reusable investigation procedure. Spike-quality; produces YAML + rationale, does not execute anything.
---

# mechanize (Claude Code) — compile a Claude Code session into a Sentinel DAG

**Spike-quality compiler.** Produces a candidate `sentinel.yaml` + `rationale.md` from a past investigation session. Does NOT execute the Sentinel; that's a separate executor. Does NOT ship — this is exploratory work, and the rationale is where the honesty lives.

This SKILL.md is the **Claude-Code-specific** part of the mechanize skill: how to find the session, how to read Claude Code's JSONL transcript, how to collapse Claude Code's spill-to-disk pattern, how to decode Claude's message-content blocks, and how to spawn a cold subagent via the Agent/Task tool for Stage 5.5. The shared compilation algorithm — Stages 2 through 7, the Sentinel example, the ratification checklist, the expression language, the capability registry, the rules — lives in `CORE.md`, co-located in this directory.

**You MUST read `CORE.md` in full after finishing the Claude-specific stages below.**

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

## Then, before anything else — read the spec

Read `sentinels.md` §§ 8, 9, 10, 11, 12, 13, 14, 14a, 28, 28.1, 29, 32, 37, 47, 52 (co-located in this directory), and `FINDINGS.md` in full. The complete reading list with rationale is at the top of `CORE.md`. Do NOT skip this.

## Stage 1 — Read and segment (Claude-Code-specific)

Read the JSONL. Each line is a JSON object with a `type` field:

- `type: "user"` messages carry user text (from `message.content` text blocks) and `tool_result` blocks (`message.content` type=`tool_result`, linked by `tool_use_id`).
- `type: "assistant"` messages carry assistant text and `tool_use` blocks (`message.content` type=`tool_use`).
- Non-text content blocks — type=`image`, type=`document` — are **attachments**. Do NOT decode them. Note only their kind, `mimeType` (`source.media_type`), and size in bytes.

Produce a mental model of:
- **Objective**: first substantive user text (skip `<local-command-caveat>` prefixes and slash-command entries).
- **Tool calls**: ordered list of `tool_use` blocks with their ordinal, name, input, and paired `tool_result` content.
- **Attachments**: any image/document blocks; where they appear.
- **Conclusion**: last substantive assistant text block(s).

## Stage 1.5 — Recognize spill-to-disk pairs (Claude-Code-specific; refines F3)

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

## Stage 2 addendum — shell-shaped tool in Claude Code

The Bash tool is Claude Code's shell-shaped tool. Apply CORE.md Stage 2's synthetic-capability-ID rule (`bash.<argv[0]>`) to every Bash tool call. Preserve the raw tool name `Bash` and add the synthetic ID for capability binding.

## Stage 4.5 addendum — attachment vocabulary in Claude Code transcripts

An attachment in a Claude Code session appears as a content block with `type: "image"` or `type: "document"` inside a user message's `message.content` array. The relevant fields are `source.media_type` (mime) and `source.data` size. **Do not decode `source.data`.** Apply CORE.md Stage 4.5's Q1–Q4 chooser using this recognition rule.

## Stage 5.5 addendum — cold-subagent mechanism in Claude Code

To run Stage 5.5's ratification pass as a cold subagent, use the `Agent` tool (subagent_type `general-purpose` is fine, or `Explore` if you want a read-only reader). Pass the subagent absolute paths to the fresh `sentinel.yaml` and `rationale.md` plus the ratification checklist from CORE.md Stage 5.5. Instruct the subagent to return ONLY the verdict block — no preamble. Do NOT run the checklist inline.

## Stage 8 addendum — presenting `preview.html` in Claude Code

After the shared renderer writes `<OUT_DIR>/preview.html`, hand the file to Claude Code's `Artifact` tool so the user sees the DAG rendered inline:

```
Artifact(file_path="<OUT_DIR>/preview.html", favicon="🕵️",
         description="Sentinel preview for <name> — DAG + node bodies + rationale")
```

The tool returns a URL. Include the URL in your final Stage 7 message so the user can revisit the preview later. Mermaid diagrams render natively inside Artifact — no extra script needed.

If the renderer command fails (non-zero exit), surface the stderr to the user and continue — the compile itself still succeeded.

## Stage 9 addendum — spawning warm and cold subagents in Claude Code

Both stages use Claude Code's `Agent` tool. The distinction between "warm" and "cold" is **what you put in the prompt**, not the subagent tier.

**Stage 9a — warm.** Run `python3 <repo-root>/common/mechanize/review.py rubric-gen-instructions <OUT_DIR>` and pass the printed prompt verbatim as the `Agent` task. The prompt is self-contained (base rubric + node inventory + directory paths); the subagent will read the sentinel directory and write `<OUT_DIR>/rubric.md`. If you have material extra context from the compile that would sharpen the appendix — a specific judgment call you made in Stage 5, an omitted candidate from Stage 3.5, an attachment disposition question — append it under a `### Additional compile context you may consider:` header before invoking. Otherwise, invoke the prompt as-is. Use `subagent_type: general-purpose`.

**Stage 9b — cold.** Run `python3 <repo-root>/common/mechanize/review.py grade-instructions <OUT_DIR>` and pass the printed prompt verbatim. **Do NOT append compile context** — the whole point is a cold read of `rubric.md` against the directory alone. Any warmth you leak here defeats the split. Use `subagent_type: general-purpose`.

Both subagents return a single line with the path to the file they wrote. That is the expected shape — do not ask them for prose commentary.

## Now continue with CORE.md

At this point you should have:
- A resolved session file path.
- A segmented mental model of the session (objective, tool calls, attachments, conclusion).
- Any spill-to-disk pairs collapsed per Stage 1.5.

Continue at **CORE.md Stage 2** and follow through Stage 9. When CORE.md instructs you to apply Stage 4.5's chooser, use the Claude-Code attachment vocabulary above to recognize attachments. When CORE.md instructs you to spawn a Stage 5.5 cold subagent, use the Agent tool as described above. For Stage 8, use the `Artifact` tool per the Stage 8 addendum. For Stages 9a and 9b, spawn subagents via the `Agent` tool per the Stage 9 addendum, being careful to preserve the warm/cold split (warm-only compile context in 9a; cold in 9b).

Do NOT skip any of Stages 2 through 7. Do NOT hallucinate rules that aren't in CORE.md.

## Success criterion

See CORE.md's "Success criterion" section. A `sentinel.yaml` + `rationale.md` that a human reader can audit is the bar — nothing less.
