# Behavioral Programs in the Cardinal Claude plugin

The installed plugin registers `cardinal-behavior` alongside the existing Cardinal
MCP connection. The default Query API is
`https://lakerunner-query-api.global.aws.cardinalhq.io`; override it with
`CARDINAL_BEHAVIOR_API_URL` for another deployment. The bridge uses the existing
`CARDINAL_MCP_API_KEY` and does not infer the Query API from the MCP service URL. No model credentials enter the plugin or UDF.
The deployed runtime injects the authorized JEV backend.

The normal Claude workflow is:

1. `get_behavior_sdk`: read the deployed executable SDK contract, example Python
   UDF and CompilePlan schema.
2. Author a candidate from the user's natural-language request, then call
   `compile_behavior` with `description`, `service_name`, `udf_source`,
   `compile_plan`, and one to eight authored `teaching_examples`. Each example
   contains a normalized `trace` with `trace_id` and an optional `expected_verdict`.
   These examples are compiler checks, not production evidence or a benchmark.
3. Inspect the returned Behavior Contract, Python source, immutable version and
   compile receipt, or call `inspect_behavior` again. A failed compile returns its
   diagnostics and compile receipt so Claude can revise the candidate explicitly.
   The service mechanically validates the submitted Claude-authored candidate; it
   does not claim that a separate model generated it.
4. `accept_behavior` explicitly accepts the inspected immutable DiagnosticVersion.
5. `execute_behavior` submits the accepted version, `population` (exact service
   name), and RFC3339 `start` and `end` to the authenticated deployed API.
6. Call `next_behavior_result` until a completed receipt appears. Findings expose
   trace ID, verdict, bounded reason, witness references and coverage gaps. A
   running count is progress, not the final population denominator.
7. `get_behavior_execution` inspects durable shard references and bounded JEV
   metadata for a selected trace without advancing the result cursor.
8. `render_storyboard` fetches committed detailed receipts and renders expandable
   evidence from a completed execution. Raw model inputs stay in private host
   artifacts and the Storyboard, outside the tool's compact findings.

Compiled versions, acceptances and execution receipts are retained under
`~/.cardinal/behavior-executions` by default. `CARDINAL_BEHAVIOR_OUTPUT_DIR` can
select another private directory. The bridge checks source hashes, result version,
profile, UDF and adapter identities, duplicate trace IDs and completed counts.
Multiple compiled versions can be executed and inspected after a plugin restart.

For explicit deployments, `CARDINAL_BEHAVIOR_CONFIG` or `--config` can name a JSON
configuration; see `config.example.json`. `headers_file`, if used, must be private
(mode 0600) and contain only supported API authentication headers. Authenticated
normal API calls derive the organization from the existing Cardinal key.

The release build copies canonical `common/behavior` into `lib/behavior`; the
launcher supports both installed plugins and source checkouts. Legacy explicitly
configured `diagnostic_version`/`artifacts` selection remains available for old
receipts, but the normal workflow does not load frozen default artifacts or
require manual registration.

Run focused checks with:

```sh
python3 -m unittest discover -s common/behavior/tests -v
python3 -m unittest discover -s tests -p test_behavior_release.py -v
```
