# Plan-state telemetry

Add per-session visibility into the Claude Code subscription tier and the
current-week rate-limit utilization so the Outcomes dashboard can:

- Group spend / outcomes by `rate_limit_tier` (Pro vs Max 5x vs Max 20x vs
  Team vs API).
- Surface "you're at X% of your weekly Sonnet budget, resets in Y" to the
  user.
- **Derive the absolute weekly caps server-side** by joining our
  per-call token counts (`cardinal.turn_usage`) with the change in
  utilization between two snapshots taken in the same session. The
  Anthropic API never exposes the raw caps directly, but if we capture
  `(util_start, util_end, tokens_observed_between)` per session, then
  `cap = Δtokens / Δutilization`. With enough such samples per
  `rate_limit_tier`, the median converges on the true cap and becomes a
  backend lookup table.

  This delta-form is the only correct form. A single
  `(util, tokens_in_session)` pair gives a meaningless ratio because the
  numerator is session-scoped and the denominator is week-scoped
  (cross-session, cross-device) — they have no fixed ratio. Two
  snapshots in the same session, close enough together that our token
  observation accounts for most of Anthropic's denominator change, are
  what make the math work.

The data already exists on Anthropic's side. Claude Code's own `/usage`
panel reads it via two OAuth-bearer-authenticated endpoints, and the
plugin can call them with the same token Claude Code already stores in
the macOS keychain / `~/.claude/.credentials.json`.

## Feasibility (confirmed)

Verified against the live endpoints on 2026-06-15 with the production
OAuth token from this user's keychain.

### `GET https://api.anthropic.com/api/oauth/profile`

```jsonc
{
  "account": {
    "has_claude_max": true,
    "has_claude_pro": false
    // ...PII fields (email, uuid, display_name, full_name) — NOT read into cache, NOT emitted.
  },
  "organization": {
    "organization_type": "claude_max",
    "rate_limit_tier":   "default_claude_max_20x"   // distinguishes 5x vs 20x
    // ...other org fields — NOT read into cache, NOT emitted.
  }
}
```

### `GET https://api.anthropic.com/api/oauth/usage`

```jsonc
{
  "five_hour":        { "utilization": 4.0, "resets_at": "2026-06-15T21:10:00Z" },
  "seven_day":        { "utilization": 7.0, "resets_at": "2026-06-20T10:00:00Z" },
  "seven_day_sonnet": { "utilization": 0.0, "resets_at": null },
  "seven_day_opus":   null
  // Other null buckets (cinder_cove, iguana_necktie, omelette_promotional,
  // tangelo, seven_day_cowork, seven_day_oauth_apps, seven_day_omelette,
  // extra_usage) are server-side feature-flag experiment buckets or
  // out-of-scope dimensions; we ignore them.
}
```

**Important shape note:** `utilization` is a **percentage 0–100**, not a
0..1 fraction. Anthropic returns one-decimal-place precision (0.1 %
quantization), which sets a floor on how small a usable Δutilization
can be — see §Phase 2.

Both endpoints accept the same `Authorization: Bearer <accessToken>`
header Claude Code already uses. No `x-api-key`, no `anthropic-version`.
Timeouts on the live calls were < 200 ms.

## Design constraints (non-negotiable)

1. **No new sync work in any existing hook.** `limits-gate.py` (the only
   sync hook today) is not touched. Both new fetch points run in async
   hooks (SessionStart, Stop).
2. **Throttled fetches.** Anthropic is hit at most once per Claude Code
   process at SessionStart + at most once per 10 min on Stop firings.
   A heavy user (50 user-turns/day) emits ≤ ~7 usage events/day, not 50.
3. **No PII in cache or on the wire.** The fetch helper uses an
   **explicit allowlist** of fields from the API responses. Any field
   not on the allowlist is discarded before the cache file is written,
   so a future Anthropic-side field addition (e.g. `account.phone`)
   cannot leak by default. Allowlist is the schema in §Cache file.
4. **Token never leaves the local process.** We read it from the keychain
   (macOS) or `~/.claude/.credentials.json` (Linux), make outbound
   HTTPS calls to `api.anthropic.com`, and never include it (or any
   truncation, substring, or reversible transform of it) in any OTLP
   payload or log line. A SHA-256 hex prefix of the token IS stored in
   the cache file for invalidation (see §Cache invalidation) — this is
   one-way and not a token derivative in any threat-modelling sense.
5. **Best-effort, silent failure.** Any error path (keychain locked, no
   token, network blip, endpoint 4xx/5xx, malformed JSON) → exit 0,
   emit nothing. Plan-state is enrichment, not gating.
6. **Atomic cache writes.** Two SessionStart hooks racing on a single
   user's `plan.json` must never produce a torn file. All writes go via
   `tmp + os.replace`. Readers tolerate `FileNotFoundError` and
   `json.JSONDecodeError` by silently treating the cache as absent.
7. **Cache invalidation on account switch.** The cache file carries
   `token_fingerprint` = `sha256(token).hexdigest()[:16]`. If the
   current token's fingerprint doesn't match the cached one, the cache
   is discarded and refetched. This is the only correct behaviour
   under `claude logout && claude login --as <other account>`.

## Triggers

Two events, two trigger points.

### `cardinal.plan_state` — SessionStart

Fires once per Claude Code session. Fetches `/api/oauth/profile` and
`/api/oauth/usage`, writes the full cache, emits one event.

This is the only built-in hook that fires before any user input
arrives, so the first usage snapshot is anchored before the user has
done anything in this session — important for the Δ math.

The existing SessionStart hook is `initiative-convention.py`. We add a
second SessionStart entry rather than extending that one — keeps the two
concerns independently testable, and plan-state is the one hook that
wants network access.

### `cardinal.plan_usage` — Stop (throttled)

Fires on every Stop. Re-fetches `/api/oauth/usage` only if
`now - cache.usage_fetched_at >= 10 min`. When the fetch happens, the
fresh snapshot is written back to `plan.json` AND emitted as a
`cardinal.plan_usage` event. When the fetch is skipped (cache fresh),
nothing is emitted.

Why 10 minutes:

- Heavy active user ≈ 1 user-turn / minute → ~6 fetches/hour. Light
  enough not to look like abuse to api.anthropic.com.
- Δutilization across 10 min of active work usually clears the 0.1 %
  quantization floor (one Sonnet turn ≈ 0.05–0.2 % depending on plan).
- Caps the network surface at ~7 events / day / user even in pathological
  long sessions.

### Why both events, not one

`plan_state` is once-per-session, LWW onto the `agent_sessions` row.
`plan_usage` is many-per-session, append-only into the event stream so
the backend can compute Δs. Different cadences, different storage
shapes, different consumers — keeping them separate keeps each one
simple.

## Event schemas

### `cardinal.plan_state`

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `event_name` | string | constant | `cardinal.plan_state` |
| `session_id` | string | hook payload | |
| `ts` | int (ns) | `time.time_ns()` | UTC |
| `plan_type` | string | derived (see below) | `pro` \| `max` \| `team` \| `enterprise` \| `api` |
| `rate_limit_tier` | string | `organization.rate_limit_tier` | distinguishes max_5x vs max_20x; the cap-derivation grouping key |
| `organization_type` | string | `organization.organization_type` | raw (e.g. `claude_max`); lets backend re-derive `plan_type` later without losing fidelity |
| `billing_type` | string | `organization.billing_type` | raw (e.g. `stripe_subscription`, `invoice`); feeds `billing_mode` derivation and lets the backend distinguish self-serve vs enterprise contract |
| `has_extra_usage_enabled` | bool | `organization.has_extra_usage_enabled` | whether overage credits are configured; flips a plan_based user into hybrid mode |
| `billing_mode` | string | derived (see below) | `usage_based` \| `plan_based` \| `hybrid`; the dashboard's primary mode toggle (see [conductor `dashboard-billing-mode.md`](../../../conductor/docs/specs/dashboard-billing-mode.md)) |

### `cardinal.plan_usage`

One per fetch (SessionStart + each throttled Stop refetch).

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `event_name` | string | constant | `cardinal.plan_usage` |
| `session_id` | string | hook payload | |
| `ts` | int (ns) | `time.time_ns()` at fetch | the snapshot anchor; cap derivation Δs against this |
| `five_hour_utilization` | double | `five_hour.utilization` | percent 0–100; attribute omitted if bucket absent (NOT emitted as `null` string) |
| `five_hour_resets_at` | string | `five_hour.resets_at` | ISO8601 |
| `seven_day_utilization` | double | `seven_day.utilization` | |
| `seven_day_resets_at` | string | `seven_day.resets_at` | |
| `seven_day_sonnet_utilization` | double | `seven_day_sonnet.utilization` | per-model-family denominator |
| `seven_day_sonnet_resets_at` | string | `seven_day_sonnet.resets_at` | |
| `seven_day_opus_utilization` | double | `seven_day_opus.utilization` | |
| `seven_day_opus_resets_at` | string | `seven_day_opus.resets_at` | |

### `plan_type` derivation

The API does not expose a single plan-name field. We synthesize one from
two raw fields so downstream queries can `GROUP BY plan_type` without
knowing the internal taxonomy.

```
account.has_claude_max == true                  → "max"
account.has_claude_pro == true                  → "pro"
organization.organization_type == "team"        → "team"
organization.organization_type == "enterprise"  → "enterprise"
no OAuth token / no profile call                → "api"   (user is on raw API key)
profile call fails after retries                → omit attribute
                                                  (NOT "unknown" — a sentinel
                                                  would pollute cap-derivation
                                                  groupings)
```

`rate_limit_tier` is the unambiguous server-side identifier and the
backend's cap-derivation grouping key. `plan_type` is the
human-readable rollup for dashboards.

### `billing_mode` derivation

The dashboard's mental model splits cleanly into "your bill is tokens ×
rate" vs "your bill is a fixed subscription, your axis is quota %." The
plugin derives this once so every downstream consumer agrees on the
mode without re-deriving it:

```
plan_type == "api"                          → "usage_based"
plan_type ∈ {pro, max, team, enterprise}    → "plan_based"
                              AND
  has_extra_usage_enabled == true           → "hybrid"
  has_extra_usage_enabled == false          → "plan_based"
profile fetch failed                        → omit attribute
```

`hybrid` means "render both axes" — the user has a flat plan AND has
opted into paying per-token overage once the plan is exhausted. The
dashboard de-emphasises the overage axis until the plan quota is hit,
but it can't ignore overage entirely or it would silently undercount
real $ spend.

We emit BOTH the raw inputs (`billing_type`, `has_extra_usage_enabled`)
AND the derived `billing_mode`. The raw inputs keep the backend free to
re-derive if the rules change without requiring a plugin re-rev; the
derived enum keeps every consumer (dashboard, alerts, scripts) from
re-implementing the same case statement.

Note on `team` / `enterprise`: contracts can include either flat
allocations or pure-usage-based billing depending on the deal. `billing_type`
distinguishes them on the wire (`stripe_subscription` vs `invoice` vs
future values), but our `plan_based` default for these tiers is the
common case. The conductor spec documents how the dashboard degrades
when the assumption is wrong — the v1 plugin defers per-contract
sniffing.

### Stamping `plan_type` / `rate_limit_tier` on existing events

Each downstream hook (`turn-usage.py`, `subagent-usage.py`,
`git-state.py`) reads `~/.claude/cardinal/plan.json` at the top of its
`main()` and stamps `plan_type` + `rate_limit_tier` as event-level
attributes on every record it emits. Cache absent → no stamps; existing
behaviour unchanged. One extra file read per hook firing; cost is one
disk seek on a JSON < 1 KB.

(An earlier draft proposed mutating `OTEL_RESOURCE_ATTRIBUTES` from the
SessionStart hook. That doesn't work — env mutations in a subprocess
don't propagate to the parent Claude Code process or to sibling hook
subprocesses. Reading the cache file directly is the correct mechanism.)

## Token sourcing

Read precedence:

1. **macOS keychain.** `security find-generic-password -s "Claude Code-credentials" -a "$USER" -w` returns a JSON blob; the access token is `claudeAiOauth.accessToken`.
2. **Linux credentials file.** `~/.claude/.credentials.json`, same blob layout.
3. **Fail closed.** If neither yields a token, emit `plan_type=api` only, skip usage fields, do not call api.anthropic.com.

We do not read `ANTHROPIC_API_KEY` itself. The OAuth endpoints reject
`x-api-key` auth.

## Cache file

Path: `~/.claude/cardinal/plan.json`. Reader-tolerant, single atomic
writer.

### Schema (the allowlist)

```jsonc
{
  "token_fingerprint":   "<sha256(token)[:16]>",
  "profile_fetched_at":  "2026-06-15T20:30:00Z",
  "usage_fetched_at":    "2026-06-15T20:30:00Z",

  "plan_type":               "max",
  "rate_limit_tier":         "default_claude_max_20x",
  "organization_type":       "claude_max",
  "billing_type":            "stripe_subscription",
  "has_extra_usage_enabled": true,
  "billing_mode":            "hybrid",

  "usage": {
    "five_hour":        { "utilization": 4.0, "resets_at": "2026-06-15T21:10:00Z" },
    "seven_day":        { "utilization": 7.0, "resets_at": "2026-06-20T10:00:00Z" },
    "seven_day_sonnet": { "utilization": 0.0, "resets_at": null },
    "seven_day_opus":   null
  }
}
```

**Anything not on this allowlist is dropped from the API responses
before being written.** Adding a field requires editing the spec and
the helper module — there is no pass-through path. The helper's
projection function takes the parsed API response and constructs a new
dict from the allowlisted keys; it never `dict.update`s the raw
response into the cache.

### Concurrent writes

Two SessionStart hooks for the same user racing on `plan.json` is a
real case (user runs Claude Code in two terminals). Implementation:

```python
def write_cache(payload: dict) -> None:
    path = Path.home() / ".claude" / "cardinal" / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)   # atomic on POSIX
```

Last writer wins on the contents. Last writer wins is fine: profile
fields are identical across concurrent sessions of the same user, and
the usage snapshot from a slightly-later fetch is no worse than the
earlier one for delta purposes (each `plan_usage` event carries its
own snapshot on the wire).

### Cache invalidation on account switch

On every SessionStart, compute the current token's fingerprint
(`sha256(token).hexdigest()[:16]`). Compare to `cache.token_fingerprint`:

- Match → cache is for the right account; honour TTLs below.
- Mismatch (or fingerprint missing) → discard cache entirely; refetch
  both profile and usage; write fresh.

A SHA-256 prefix of an OAuth token is not a token derivative in any
threat-modelling sense — it is one-way, cannot be inverted to the
token, and reveals nothing exploitable. Storing it on the local disk
the user already has full read access to introduces no new exposure.

### TTLs

- Profile: 24 h. Subscription changes through Stripe propagate within
  minutes server-side; 24 h means the next-day session catches them
  without paying for the call every session.
- Usage: 10 min. The cadence the Stop hook checks; see §Triggers.

## Lakerunner ingest

Two cases to add in `internal/agentsessions/processor.go`. One new
table for the `plan_usage` stream so the Δ join has a place to land.

### Migration

`lrdb/migrations/<ts>_agent_sessions_plan.up.sql`:

```sql
ALTER TABLE agent_sessions
  ADD COLUMN plan_type               TEXT,
  ADD COLUMN rate_limit_tier         TEXT,
  ADD COLUMN organization_type       TEXT,
  ADD COLUMN billing_type            TEXT,
  ADD COLUMN has_extra_usage_enabled BOOLEAN,
  ADD COLUMN billing_mode            TEXT,
  ADD COLUMN plan_state_at           TIMESTAMPTZ;

CREATE INDEX agent_sessions_plan_tier
  ON agent_sessions (organization_id, rate_limit_tier, last_seen_at DESC)
  WHERE rate_limit_tier IS NOT NULL;

-- billing_mode is the dashboard's mode toggle; an index supports the
-- "show me all plan_based users in this org" view that the conductor
-- spec lands.
CREATE INDEX agent_sessions_billing_mode
  ON agent_sessions (organization_id, billing_mode, last_seen_at DESC)
  WHERE billing_mode IS NOT NULL;

-- Time-series snapshots used for Δ-based cap derivation.
CREATE TABLE agent_session_plan_usage (
  organization_id  UUID         NOT NULL,
  session_id       TEXT         NOT NULL,
  observed_at      TIMESTAMPTZ  NOT NULL,
  chq_tsns         BIGINT       NOT NULL,

  five_hour_utilization        DOUBLE PRECISION,
  five_hour_resets_at          TIMESTAMPTZ,
  seven_day_utilization        DOUBLE PRECISION,
  seven_day_resets_at          TIMESTAMPTZ,
  seven_day_sonnet_utilization DOUBLE PRECISION,
  seven_day_sonnet_resets_at   TIMESTAMPTZ,
  seven_day_opus_utilization   DOUBLE PRECISION,
  seven_day_opus_resets_at     TIMESTAMPTZ,

  PRIMARY KEY (organization_id, session_id, chq_tsns)
);

CREATE INDEX agent_session_plan_usage_session
  ON agent_session_plan_usage (organization_id, session_id, observed_at);
```

`chq_tsns` in the PK is the same idempotency guard the existing event
tables use: replays of the same OTLP record become no-ops.

### Query

`lrdb/queries/agent_sessions.sql`:

```sql
-- name: ApplyAgentSessionPlanState :exec
UPDATE agent_sessions
   SET plan_type               = COALESCE(@plan_type,               plan_type),
       rate_limit_tier         = COALESCE(@rate_limit_tier,         rate_limit_tier),
       organization_type       = COALESCE(@organization_type,       organization_type),
       billing_type            = COALESCE(@billing_type,            billing_type),
       has_extra_usage_enabled = COALESCE(@has_extra_usage_enabled, has_extra_usage_enabled),
       billing_mode            = COALESCE(@billing_mode,            billing_mode),
       plan_state_at           = GREATEST(COALESCE(plan_state_at, '-infinity'::timestamptz), @plan_state_at)
 WHERE organization_id = @organization_id
   AND session_id      = @session_id;

-- name: InsertAgentSessionPlanUsage :exec
INSERT INTO agent_session_plan_usage (
  organization_id, session_id, observed_at, chq_tsns,
  five_hour_utilization, five_hour_resets_at,
  seven_day_utilization, seven_day_resets_at,
  seven_day_sonnet_utilization, seven_day_sonnet_resets_at,
  seven_day_opus_utilization, seven_day_opus_resets_at
) VALUES (
  @organization_id, @session_id, @observed_at, @chq_tsns,
  @five_hour_utilization, @five_hour_resets_at,
  @seven_day_utilization, @seven_day_resets_at,
  @seven_day_sonnet_utilization, @seven_day_sonnet_resets_at,
  @seven_day_opus_utilization, @seven_day_opus_resets_at
)
ON CONFLICT (organization_id, session_id, chq_tsns) DO NOTHING;
```

### Processor routes

```go
case "cardinal.plan_state":
    return p.store.ApplyAgentSessionPlanState(ctx, lrdb.ApplyAgentSessionPlanStateParams{
        OrganizationID:       ev.OrganizationID,
        SessionID:            ev.SessionID,
        PlanType:             nullableTag(ev.Tags, "plan_type"),
        RateLimitTier:        nullableTag(ev.Tags, "rate_limit_tier"),
        OrganizationType:     nullableTag(ev.Tags, "organization_type"),
        BillingType:          nullableTag(ev.Tags, "billing_type"),
        HasExtraUsageEnabled: parseBoolTag(ev.Tags, "has_extra_usage_enabled"),
        BillingMode:          nullableTag(ev.Tags, "billing_mode"),
        PlanStateAt:          ev.EventTime,
    })
case "cardinal.plan_usage":
    return p.store.InsertAgentSessionPlanUsage(ctx, lrdb.InsertAgentSessionPlanUsageParams{
        OrganizationID:              ev.OrganizationID,
        SessionID:                   ev.SessionID,
        ObservedAt:                  ev.EventTime,
        ChqTsns:                     ev.ChqTsns,
        FiveHourUtilization:         parseFloat64Tag(ev.Tags, "five_hour_utilization"),
        FiveHourResetsAt:            parseTimeTag(ev.Tags, "five_hour_resets_at"),
        SevenDayUtilization:         parseFloat64Tag(ev.Tags, "seven_day_utilization"),
        SevenDayResetsAt:            parseTimeTag(ev.Tags, "seven_day_resets_at"),
        SevenDaySonnetUtilization:   parseFloat64Tag(ev.Tags, "seven_day_sonnet_utilization"),
        SevenDaySonnetResetsAt:      parseTimeTag(ev.Tags, "seven_day_sonnet_resets_at"),
        SevenDayOpusUtilization:     parseFloat64Tag(ev.Tags, "seven_day_opus_utilization"),
        SevenDayOpusResetsAt:        parseTimeTag(ev.Tags, "seven_day_opus_resets_at"),
    })
```

### Tap projection

Add to the `SELECT … FROM read_parquet(?)` projection in
`internal/agentsessions/tap.go`:

```sql
"attrs"['plan_type']                       AS "plan_type",
"attrs"['rate_limit_tier']                 AS "rate_limit_tier",
"attrs"['organization_type']               AS "organization_type",
"attrs"['billing_type']                    AS "billing_type",
"attrs"['has_extra_usage_enabled']         AS "has_extra_usage_enabled",
"attrs"['billing_mode']                    AS "billing_mode",
"attrs"['five_hour_utilization']           AS "five_hour_utilization",
"attrs"['five_hour_resets_at']             AS "five_hour_resets_at",
"attrs"['seven_day_utilization']           AS "seven_day_utilization",
"attrs"['seven_day_resets_at']             AS "seven_day_resets_at",
"attrs"['seven_day_sonnet_utilization']    AS "seven_day_sonnet_utilization",
"attrs"['seven_day_sonnet_resets_at']      AS "seven_day_sonnet_resets_at",
"attrs"['seven_day_opus_utilization']      AS "seven_day_opus_utilization",
"attrs"['seven_day_opus_resets_at']        AS "seven_day_opus_resets_at",
```

## Phase 2 — Server-side cap derivation

Out of scope for the v1 PRs in plugin + lakerunner. Spec'd here so the
v1 event schema captures every field the algorithm needs.

### Δ-pair construction

For each session, pair consecutive `agent_session_plan_usage` rows
within the same Anthropic window (i.e. across an unchanged `resets_at`)
to form (snapshot_A, snapshot_B, Δutilization). For each pair, sum the
matching `cardinal.turn_usage` records between `A.observed_at` and
`B.observed_at`, filtered to the model family for the window:

| Window | Token filter |
|---|---|
| `seven_day_sonnet` | `model LIKE '%sonnet%'` |
| `seven_day_opus`   | `model LIKE '%opus%'` |
| `seven_day`        | all models |
| `five_hour`        | all models |

For the 5-hour window, only pairs where both snapshots fall inside the
same 5-hour boundary are valid — drop pairs that crossed a reset.

### Sample filters

Drop a Δ-pair if any of:

- `Δutilization < 1.0` (% — below this, quantization dominates).
- `B.resets_at != A.resets_at` (Anthropic's window reset between
  snapshots; denominator scale changed).
- `Δutilization < 0` (window crossed, or the user used credits
  refunded server-side).
- `Δtokens == 0` (rare but possible — Δutilization > 0 came from
  another device; this pair under-attributes by definition).

### Per-tier aggregation

```sql
WITH pairs AS (
  SELECT
    a.organization_id,
    a.session_id,
    s.rate_limit_tier,
    a.observed_at AS a_at,
    b.observed_at AS b_at,
    (b.seven_day_sonnet_utilization - a.seven_day_sonnet_utilization) AS d_util,
    a.seven_day_sonnet_resets_at AS resets_at
  FROM agent_session_plan_usage a
  JOIN agent_session_plan_usage b
    ON  a.organization_id = b.organization_id
    AND a.session_id      = b.session_id
    AND b.observed_at      > a.observed_at
    AND b.seven_day_sonnet_resets_at = a.seven_day_sonnet_resets_at
    -- Only adjacent pairs in the same session, same window:
    AND NOT EXISTS (
      SELECT 1 FROM agent_session_plan_usage m
       WHERE m.organization_id = a.organization_id
         AND m.session_id      = a.session_id
         AND m.observed_at > a.observed_at
         AND m.observed_at < b.observed_at
    )
  JOIN agent_sessions s USING (organization_id, session_id)
  WHERE s.rate_limit_tier IS NOT NULL
),
deltas AS (
  SELECT
    p.rate_limit_tier,
    p.d_util,
    (SELECT COALESCE(SUM(e.input_tokens + e.output_tokens + e.cache_creation_input_tokens), 0)
       FROM agent_session_events e
      WHERE e.organization_id = p.organization_id
        AND e.session_id      = p.session_id
        AND e.event_name      = 'cardinal.turn_usage'
        AND e.event_time      > p.a_at
        AND e.event_time     <= p.b_at
        AND e.model LIKE '%sonnet%'
    ) AS d_tokens
  FROM pairs p
  WHERE p.d_util >= 1.0
)
SELECT
  rate_limit_tier,
  COUNT(*) AS sample_count,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY d_tokens / (d_util / 100.0))
    AS inferred_cap_tokens_p50
FROM deltas
WHERE d_tokens > 0
GROUP BY rate_limit_tier
HAVING COUNT(*) >= 30;
```

Notes on this query:

- **p50, not p75.** With the Δ form the noise is bidirectional (we
  under-count Δtokens when the user runs on a second device; we
  over-count never — but Anthropic's quantization rounds either way),
  so the median is the right estimator. The prior draft used p75 to
  defend against a unidirectional bias that the Δ form eliminates.
- **NOT EXISTS for adjacency.** Joining only adjacent snapshots
  prevents double-counting Δtokens across overlapping pairs.
- **`HAVING COUNT(*) >= 30`.** A floor so a tier with three sessions
  doesn't yield a wild headline number; the table publishes `NULL`
  for under-sampled tiers and the front-end falls back to "no estimate
  yet".
- **No `::epoch_ns` casts.** The earlier draft had an invalid cast;
  here all joins are TIMESTAMPTZ-to-TIMESTAMPTZ.

### Output table

```sql
CREATE TABLE rate_limit_caps (
  rate_limit_tier      TEXT NOT NULL,
  window_name          TEXT NOT NULL,  -- five_hour | seven_day | seven_day_sonnet | seven_day_opus
  inferred_cap_tokens  BIGINT NOT NULL,
  sample_count         INT NOT NULL,
  last_updated_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (rate_limit_tier, window_name)
);
```

Recomputed nightly. The dashboard uses this table to render absolute
numbers ("you've used 4.2k of ~60k Sonnet tokens this week").

## Privacy

Wire surface lists every emitted attribute in the §Event schemas tables
above; nothing else goes out. Cache surface lists every stored field
in the §Cache file schema; nothing else is written.

Both surfaces are **explicit allowlists**, enforced at the projection
function — a new field appearing in Anthropic's API response is dropped
by default, not exposed. The same code path writes both surfaces, so
adding a field on the wire requires the same edit as adding it to the
cache.

The OAuth access token never enters any OTLP payload or log line.
Only the SHA-256 prefix is persisted, and only in the local cache file
for invalidation.

## Volume

| Event | Cadence | Heavy user / day |
|---|---|---|
| `cardinal.plan_state` | 1 / SessionStart | ~10 |
| `cardinal.plan_usage` | ≤ 1 / 10 min | ~7 (one SessionStart + ~6 throttled refetches in a full day's active use) |

Trivial for both Anthropic (the rate-limit check) and lakerunner.

## Plugin version

v0.11.0. Conventional commit:
`feat(v0.11.0): cardinal.plan_state / cardinal.plan_usage — per-session plan tier + utilization snapshots`.

## Test plan

Mirrors `tests/test_subagent_usage.py` (stub OTLP server on port 0, hook
run as subprocess with `HOME` redirected, mock `api.anthropic.com`).

| Test | Fixture | Assertion |
|---|---|---|
| `plan_state_emitted_at_session_start` | mock profile+usage 200 OK, keychain has token | 1 `plan_state` + 1 `plan_usage` event, attribute schemas match §Event schemas exactly |
| `derives_plan_type_max` | profile with `has_claude_max=true` | `plan_type=max` |
| `derives_plan_type_team` | profile with organization_type=team | `plan_type=team` |
| `derives_billing_mode_usage_based` | no token (API-key user) | `billing_mode=usage_based` |
| `derives_billing_mode_plan_based` | Max plan, `has_extra_usage_enabled=false` | `billing_mode=plan_based` |
| `derives_billing_mode_hybrid` | Max plan, `has_extra_usage_enabled=true` | `billing_mode=hybrid` |
| `falls_back_to_api_when_no_token` | keychain empty, no credentials.json | `plan_type=api`; no usage event; no HTTPS call made |
| `omits_plan_type_when_profile_500s` | profile 500, usage 200 | `plan_state` not emitted; `plan_usage` still emitted from the usage call |
| `usage_refetch_throttled_to_10min` | second Stop firing 5 min after the first | no Anthropic call; no `plan_usage` event |
| `usage_refetched_after_10min` | second Stop firing 11 min after the first | one Anthropic call; one `plan_usage` event |
| `cache_invalidated_on_token_change` | cached fingerprint != current token fingerprint | profile + usage both refetched; new cache written |
| `cache_concurrent_writes_no_torn_json` | two concurrent SessionStart subprocesses | final `plan.json` parses cleanly with full schema |
| `disallowed_fields_dropped_from_cache` | mock profile response with extra `account.phone` field | cache file does NOT contain `phone` anywhere |
| `disallowed_fields_dropped_from_wire` | same | emitted OTLP records do NOT contain `phone` anywhere |
| `oauth_token_never_in_otlp_payload` | full happy path | grep emitted OTLP bytes for `sk-ant-oat` prefix → none present |
| `oauth_token_never_in_logs` | force a fetch error after token read | stderr / hook log contains no `sk-ant-oat` prefix |
| `null_buckets_omit_attributes_not_strings` | usage with `seven_day_opus: null` | `seven_day_opus_*` attrs not present (NOT as `"null"` string) |
| `downstream_hook_stamps_plan_type_from_cache` | run `turn-usage.py` after cache populated | emitted records carry `plan_type` + `rate_limit_tier` |
| `downstream_hook_unaffected_when_cache_absent` | delete `plan.json` then run `turn-usage.py` | records emitted, `plan_type` / `rate_limit_tier` simply absent |
| `silent_exit_on_anthropic_network_failure` | fetch raises URLError | exit 0, no records emitted |

## Rollout

1. **Plugin v0.11.0 PR** in `cardinal-claude-plugin`:
   - New helper module `hooks/_plan_cache.py` (read/write/invalidate the cache with the allowlist projection).
   - New hook `hooks/plan-state.py` (SessionStart fetch+emit) and
     `hooks/plan-usage.py` (Stop throttled fetch+emit). They share
     `_plan_cache.py`.
   - `hooks.json`: SessionStart adds plan-state.py; Stop adds plan-usage.py.
   - `turn-usage.py`, `subagent-usage.py`, `git-state.py`: one-line
     change each to read `_plan_cache.read()` and stamp attributes if
     present.
   - Tests above.
2. **Lakerunner PR** (sibling, blocked by v0.11.0 publish):
   - Migration: 4 new columns on `agent_sessions` + the
     `agent_session_plan_usage` table + indices.
   - Queries: `ApplyAgentSessionPlanState`, `InsertAgentSessionPlanUsage`.
   - Processor: two new switch cases.
   - Tap: 11 new projected columns.
   - Tests: corpus events for both new paths.
3. **Phase 2 — Cap derivation** (separate, follow-up):
   - `rate_limit_caps` table + migration.
   - Nightly job in lakerunner that runs the Δ-pair query above.
   - Conductor reads `rate_limit_caps` to render absolute numbers.
   - Backfill from existing `agent_session_plan_usage` history once
     enough Δ-pairs accumulate per tier.

## Out of scope

- Surfacing utilization back to the user via `limits-gate.py`
  (separate UX question; the data this PR lands enables it).
- `extra_usage` overage credits as numeric $ — the `has_extra_usage_enabled`
  bool is captured (it flips `billing_mode` to hybrid) but the $ used /
  $ limit numbers are not. Revisit if the dashboard's hybrid-mode
  overage meter wants live $ values.
- `subscription_status` — not needed for cap derivation or billing-mode
  routing; revisit if the dashboard wants subscription-health surfacing
  ("your subscription is past_due").
- Supporting `ANTHROPIC_API_KEY`-only users beyond emitting
  `plan_type=api`. API-key users don't have a weekly cap.
- Per-user weekly cap (vs per-tier). The Δ form is sound at per-session
  granularity but per-user single-session noise is too high to publish;
  per-tier aggregation across many users is the load-bearing piece.
