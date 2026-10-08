# Behavioral Programs in the Cardinal Claude plugin

The installed plugin registers `cardinal-behavior` alongside the existing Cardinal
MCP connection. It derives the authenticated organization route from
`CARDINAL_MCP_URL` and uses the existing `CARDINAL_MCP_API_KEY`. Maestro resolves
the organization's LakeRunner integration and forwards with its server-side
data-plane credential. The plugin never receives that credential. Its scoped MCP
key is not a Query API key and is never sent to a guessed Query API host. Requests
reject redirects so custom authentication headers cannot follow another host.
No model credentials enter the plugin or UDF.
The deployed runtime injects the authorized JEV backend.

The normal Claude workflow is:

1. `get_behavior_sdk`: fetch the deployed SDK artifact and read its real Python
   sources, profiles, CompilePlan schema, and executable examples. LakeRunner
   imports this exact immutable release artifact from the public
   [cardinalhq/behavior-sdk](https://github.com/cardinalhq/behavior-sdk) repository.
   The SDK version, source commit, public artifact URL, and SHA-256 appear in the
   response and are retained with compilation and acceptance. The
   plugin verifies its SHA-256 content address and caches the immutable artifact;
   it contains no independently maintained SDK declarations. The response starts
   with `sdk_directory` and `sdk_files`: exact verified public files materialized
   under the digest cache, so Claude can page through real Python source using
   its normal Read tool even when the complete MCP response is large.
2. Author a candidate from the user's contract, then call `compile_behavior`
   with `description`, `service_name`, `udf_source`, and `compile_plan`. The plugin
   pins the observed SDK, profile, and host runtime identities. The deployed
   compiler checks syntax, imports, public API, and mechanical requirements.
   Up to eight authored `teaching_examples` are optional compiler checks.
3. Call `test_behavior` with the compiled `diagnostic_version`, real `trace_ids`,
   their `service_name`, and an RFC3339 `start`/`end` window of at most one hour.
   Supply `expected_verdicts`, a map from every teaching trace ID to its expected
   MATCH, NON_MATCH, or UNKNOWN verdict. Execution failures return ERROR. The deployed API reads those
   traces through the production catalog and runs the candidate with the same
   production SDK, sandbox, and authorized JEV backend used for population runs.
   Results include actual verdicts, witnesses, Recorder output, and JEV receipts.
   No local evaluator or synthetic replacement trace is used by this tool.
4. Inspect the contract, source, compile receipt, and teaching results. Revise
   and compile again if necessary. The optional `udf_source` on `test_behavior`
   must exactly match the compiled candidate. For example, discover behavior in
   trace A, choose contrasting trace B, and confirm A MATCH/B NON_MATCH before
   acceptance. Teaching examples establish intent, not population accuracy.
5. `accept_behavior` explicitly accepts the inspected immutable DiagnosticVersion.
   Acceptance requires a real teaching receipt with expected verdicts and binds
   its trace IDs, expected/actual results, compile/JEV receipts, and SDK/profile/
   runtime identities. Identity disagreements fail closed.
6. `execute_behavior` submits the accepted version, `population` (exact service
   name), and RFC3339 `start` and `end` to the authenticated deployed API.
7. Call `next_behavior_result` until a completed receipt appears. Findings expose
   trace ID, verdict, bounded reason, witness references and coverage gaps. A
   running count is progress, not the final population denominator.
8. `get_behavior_execution` inspects durable shard references and bounded JEV
   metadata for a selected trace without advancing the result cursor.
9. `render_storyboard` fetches committed detailed receipts and renders expandable
   evidence from a completed execution. Raw model inputs stay in private host
   artifacts and the Storyboard, outside the tool's compact findings.

SDK artifacts, compiled versions, teaching results, acceptances, and execution
receipts are retained under `~/.cardinal/behavior-executions` by default.
`CARDINAL_BEHAVIOR_OUTPUT_DIR` can select another private directory. The bridge
checks SDK artifact and source hashes, SDK/profile/runtime identities, result
version/UDF/adapter identities, duplicate trace IDs, and completed counts.
Multiple compiled versions can be tested, executed, and inspected after a plugin
restart. Read `get_behavior_sdk` in each authoring session before compiling.

For explicit deployments, `CARDINAL_BEHAVIOR_CONFIG` or `--config` can name a JSON
configuration; see `config.example.json` for an operator's direct Query API
configuration. `CARDINAL_BEHAVIOR_API_URL` also remains an explicit direct URL
override and reads its matching data-plane credential from `CARDINAL_QUERY_API_KEY`.
It never falls back to the scoped MCP key. Direct JSON configuration can
select its credential with `api_key_env`; it must not reuse a scoped Maestro MCP
key. `headers_file`, if used, must be private
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
