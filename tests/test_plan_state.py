"""Tests for hooks/plan-state.py + hooks/plan-usage.py + hooks/_plan_cache.py
(plugin v0.11, SessionStart + Stop → cardinal.plan_state + cardinal.plan_usage).

Each test runs the hook as a subprocess with:
- HOME pointed at a temp dir,
- .claude/settings.json routing OTLP to a local stub server,
- .claude/.credentials.json containing a fixture OAuth token (when wanted),
- CARDINAL_PLAN_OAUTH_BASE_URL pointing at a local mock api.anthropic.com.

Run with: python3 -m unittest tests.test_plan_state -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


HOOKS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks"
)
PLAN_STATE_HOOK = HOOKS_DIR / "plan-state.py"
PLAN_USAGE_HOOK = HOOKS_DIR / "plan-usage.py"
TURN_USAGE_HOOK = HOOKS_DIR / "turn-usage.py"

FIXTURE_TOKEN = "sk-ant-oat01-TESTTOKEN-NEVER-LEAK-ME-PLEASE-12345"


# --- OTLP stub -------------------------------------------------------------


class _OTLPStub(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append({
            "raw": body,
            "parsed": json.loads(body) if body else None,
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


# --- Anthropic API stub ----------------------------------------------------


class _AnthropicStub(BaseHTTPRequestHandler):
    profile_response: dict | None = None
    profile_status: int = 200
    usage_response: dict | None = None
    usage_status: int = 200
    # If set, raise instead of responding (simulates URLError).
    fail_with_reset: bool = False
    # Track GET calls observed: list of paths.
    calls: list[str] = []
    # Track tokens that were sent in Authorization headers; tests assert
    # the token value but never let it appear in OTLP payloads.
    auth_tokens: list[str] = []

    def do_GET(self):
        type(self).calls.append(self.path)
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            type(self).auth_tokens.append(auth[len("Bearer "):])
        if type(self).fail_with_reset:
            # Close the connection without sending anything to simulate
            # a network blip; urlopen will raise URLError on the client.
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if self.path == "/api/oauth/profile":
            self.send_response(type(self).profile_status)
            body = json.dumps(type(self).profile_response or {}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/oauth/usage":
            self.send_response(type(self).usage_status)
            body = json.dumps(type(self).usage_response or {}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


# --- Helpers ---------------------------------------------------------------


def _log_records(event_body: dict) -> list[dict]:
    return event_body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def _attrs_of(rec: dict) -> dict:
    out = {}
    for kv in rec["attributes"]:
        v = kv["value"]
        if "stringValue" in v:
            out[kv["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[kv["key"]] = int(v["intValue"])
        elif "boolValue" in v:
            out[kv["key"]] = v["boolValue"]
        elif "doubleValue" in v:
            out[kv["key"]] = float(v["doubleValue"])
    return out


def _records_by_event(event_body: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for rec in _log_records(event_body):
        attrs = _attrs_of(rec)
        name = attrs.get("event_name", "")
        grouped.setdefault(name, []).append(attrs)
    return grouped


def _default_profile() -> dict:
    return {
        "account": {
            "has_claude_max": True,
            "has_claude_pro": False,
            "email": "redacted@example.com",
            "uuid": "abc-uuid",
        },
        "organization": {
            "organization_type": "claude_max",
            "rate_limit_tier": "default_claude_max_20x",
            "billing_type": "stripe_subscription",
            "has_extra_usage_enabled": False,
        },
    }


def _default_usage() -> dict:
    return {
        "five_hour":        {"utilization": 4.0, "resets_at": "2026-06-15T21:10:00Z"},
        "seven_day":        {"utilization": 7.0, "resets_at": "2026-06-20T10:00:00Z"},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
        "seven_day_opus":   None,
    }


# --- Base test case --------------------------------------------------------


class _PlanHookBase(unittest.TestCase):
    def setUp(self):
        _OTLPStub.received = []
        _AnthropicStub.profile_response = _default_profile()
        _AnthropicStub.profile_status = 200
        _AnthropicStub.usage_response = _default_usage()
        _AnthropicStub.usage_status = 200
        _AnthropicStub.fail_with_reset = False
        _AnthropicStub.calls = []
        _AnthropicStub.auth_tokens = []

        self.otlp_server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.otlp_server.serve_forever, daemon=True).start()
        self.anthropic_server = ThreadingHTTPServer(("127.0.0.1", 0), _AnthropicStub)
        threading.Thread(target=self.anthropic_server.serve_forever, daemon=True).start()

        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({
            "env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self.otlp_server.server_port}",
                "OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=test-key",
                "OTEL_RESOURCE_ATTRIBUTES": "user.email=t@example.com",
            }
        }))

    def tearDown(self):
        self.otlp_server.shutdown()
        self.anthropic_server.shutdown()
        self.tmp.cleanup()

    def _write_credentials(self, token: str = FIXTURE_TOKEN) -> None:
        path = self.home / ".claude" / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))

    def _delete_credentials(self) -> None:
        path = self.home / ".claude" / ".credentials.json"
        if path.exists():
            path.unlink()

    def _env(self) -> dict:
        return {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            # darwin keychain probe must be skipped, otherwise it could
            # return a real production token. Force the helper down the
            # file path by clearing USER on darwin (security CLI returns
            # nonzero without -a, but belt-and-braces).
            "USER": "",
            "CARDINAL_PLAN_OAUTH_BASE_URL": f"http://127.0.0.1:{self.anthropic_server.server_port}",
        }

    def _run(self, hook: Path, payload: dict | None = None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = self._env()
        if extra_env:
            env.update(extra_env)
        body = json.dumps(payload or {}).encode()
        proc = subprocess.run(
            ["python3", str(hook)],
            input=body,
            env=env,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc


# --- Test cases ------------------------------------------------------------


class PlanStateHookTest(_PlanHookBase):
    def test_plan_state_emitted_at_session_start(self):
        self._write_credentials()
        self._run(PLAN_STATE_HOOK, {"session_id": "sess-A"})
        # One POST containing both plan_state + plan_usage records.
        self.assertEqual(len(_OTLPStub.received), 1)
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        states = by_event.get("cardinal.plan_state", [])
        self.assertEqual(len(states), 1)
        s = states[0]
        self.assertEqual(s["session_id"], "sess-A")
        self.assertEqual(s["plan_type"], "max")
        self.assertEqual(s["rate_limit_tier"], "default_claude_max_20x")
        self.assertEqual(s["organization_type"], "claude_max")
        self.assertEqual(s["billing_type"], "stripe_subscription")
        self.assertEqual(s["billing_mode"], "plan_based")
        self.assertFalse(s["has_extra_usage_enabled"])
        usages = by_event.get("cardinal.plan_usage", [])
        self.assertEqual(len(usages), 1)
        u = usages[0]
        self.assertAlmostEqual(u["five_hour_utilization"], 4.0)
        self.assertEqual(u["five_hour_resets_at"], "2026-06-15T21:10:00Z")

    def test_derives_plan_type_max(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {"has_claude_max": True, "has_claude_pro": False},
            "organization": {"organization_type": "claude_max", "rate_limit_tier": "x"},
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertEqual(by_event["cardinal.plan_state"][0]["plan_type"], "max")

    def test_derives_plan_type_team(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {"has_claude_max": False, "has_claude_pro": False},
            "organization": {"organization_type": "team"},
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertEqual(by_event["cardinal.plan_state"][0]["plan_type"], "team")

    def test_derives_billing_mode_usage_based(self):
        # No token → api → usage_based. No HTTPS to api.anthropic.com.
        self._delete_credentials()
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertEqual(by_event["cardinal.plan_state"][0]["plan_type"], "api")
        self.assertEqual(by_event["cardinal.plan_state"][0]["billing_mode"], "usage_based")
        self.assertEqual(_AnthropicStub.calls, [])

    def test_derives_billing_mode_plan_based(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {"has_claude_max": True, "has_claude_pro": False},
            "organization": {"organization_type": "claude_max",
                             "rate_limit_tier": "x",
                             "has_extra_usage_enabled": False},
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertEqual(by_event["cardinal.plan_state"][0]["billing_mode"], "plan_based")

    def test_derives_billing_mode_hybrid(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {"has_claude_max": True, "has_claude_pro": False},
            "organization": {"organization_type": "claude_max",
                             "rate_limit_tier": "x",
                             "has_extra_usage_enabled": True},
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertEqual(by_event["cardinal.plan_state"][0]["billing_mode"], "hybrid")

    def test_falls_back_to_api_when_no_token(self):
        self._delete_credentials()
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        states = by_event.get("cardinal.plan_state", [])
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["plan_type"], "api")
        # No usage event — API-key users have no weekly cap to report.
        self.assertNotIn("cardinal.plan_usage", by_event)
        self.assertEqual(_AnthropicStub.calls, [])

    def test_omits_plan_type_when_profile_500s(self):
        self._write_credentials()
        _AnthropicStub.profile_status = 500
        _AnthropicStub.profile_response = {"error": "boom"}
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        # plan_state cannot be emitted (no profile-derived fields), but
        # usage still emits from the usage call.
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        self.assertNotIn("cardinal.plan_state", by_event)
        self.assertIn("cardinal.plan_usage", by_event)

    def test_silent_exit_on_anthropic_network_failure(self):
        self._write_credentials()
        _AnthropicStub.fail_with_reset = True
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        # Cache was never written → no useful blob → no OTLP POST.
        self.assertEqual(len(_OTLPStub.received), 0)

    def test_null_buckets_omit_attributes_not_strings(self):
        self._write_credentials()
        _AnthropicStub.usage_response = {
            "five_hour":        {"utilization": 4.0, "resets_at": "2026-06-15T21:10:00Z"},
            "seven_day":        {"utilization": 7.0, "resets_at": "2026-06-20T10:00:00Z"},
            "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
            "seven_day_opus":   None,
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        u = by_event["cardinal.plan_usage"][0]
        # seven_day_opus is null at the bucket level — must not appear.
        self.assertNotIn("seven_day_opus_utilization", u)
        self.assertNotIn("seven_day_opus_resets_at", u)
        # seven_day_sonnet has utilization but null resets_at — util
        # present, resets_at omitted (NOT emitted as the string "null").
        self.assertIn("seven_day_sonnet_utilization", u)
        self.assertNotIn("seven_day_sonnet_resets_at", u)
        # No record contains a literal "null" string for these keys.
        raw = _OTLPStub.received[0]["raw"]
        self.assertNotIn(b"\"null\"", raw)


class CacheBehaviourTest(_PlanHookBase):
    def test_cache_invalidated_on_token_change(self):
        # First run with token A populates cache + fingerprint A.
        self._write_credentials("sk-ant-oat01-TOKEN-A-aaaaaaaaaaaaaaaaaa")
        self._run(PLAN_STATE_HOOK, {"session_id": "s1"})
        self.assertEqual(len(_AnthropicStub.calls), 2)

        # Swap token → next SessionStart must refetch both endpoints.
        _AnthropicStub.calls = []
        _OTLPStub.received = []
        self._write_credentials("sk-ant-oat01-TOKEN-B-bbbbbbbbbbbbbbbbbb")
        self._run(PLAN_STATE_HOOK, {"session_id": "s2"})
        self.assertEqual(
            sorted(_AnthropicStub.calls),
            ["/api/oauth/profile", "/api/oauth/usage"],
        )
        # And the new cache file matches the new token's fingerprint.
        cache = json.loads((self.home / ".claude" / "cardinal" / "plan.json").read_text())
        import hashlib
        expected = hashlib.sha256(
            b"sk-ant-oat01-TOKEN-B-bbbbbbbbbbbbbbbbbb"
        ).hexdigest()[:16]
        self.assertEqual(cache["token_fingerprint"], expected)

    def test_cache_concurrent_writes_no_torn_json(self):
        self._write_credentials()
        procs = []
        for i in range(4):
            procs.append(subprocess.Popen(
                ["python3", str(PLAN_STATE_HOOK)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(),
            ))
        for p in procs:
            p.communicate(input=json.dumps({"session_id": "race"}).encode(), timeout=15)
            self.assertEqual(p.returncode, 0)
        cache_path = self.home / ".claude" / "cardinal" / "plan.json"
        # The .json.tmp.<pid> sidecars must have been renamed away — no
        # half-written files left over.
        leftovers = list(cache_path.parent.glob("plan.json.tmp.*"))
        self.assertEqual(leftovers, [], f"torn temp files survived: {leftovers}")
        # Final file parses cleanly with full schema.
        blob = json.loads(cache_path.read_text())
        self.assertEqual(blob["plan_type"], "max")
        self.assertEqual(blob["rate_limit_tier"], "default_claude_max_20x")
        self.assertIn("usage", blob)


class PrivacyTest(_PlanHookBase):
    def test_disallowed_fields_dropped_from_cache(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {
                "has_claude_max": True,
                "has_claude_pro": False,
                "phone": "555-1234-SECRET",
                "email": "leaked@example.com",
            },
            "organization": {
                "organization_type": "claude_max",
                "rate_limit_tier": "default_claude_max_20x",
                "phone": "555-9999-OTHER-SECRET",
            },
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        cache = (self.home / ".claude" / "cardinal" / "plan.json").read_text()
        self.assertNotIn("555-1234-SECRET", cache)
        self.assertNotIn("555-9999-OTHER-SECRET", cache)
        self.assertNotIn("leaked@example.com", cache)
        self.assertNotIn("phone", cache)
        self.assertNotIn("email", cache)

    def test_disallowed_fields_dropped_from_wire(self):
        self._write_credentials()
        _AnthropicStub.profile_response = {
            "account": {"has_claude_max": True, "has_claude_pro": False,
                        "phone": "555-1234-SECRET"},
            "organization": {"organization_type": "claude_max",
                             "rate_limit_tier": "x"},
        }
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        raw = _OTLPStub.received[0]["raw"]
        self.assertNotIn(b"555-1234-SECRET", raw)
        self.assertNotIn(b"phone", raw)

    def test_oauth_token_never_in_otlp_payload(self):
        self._write_credentials()
        self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        raw = _OTLPStub.received[0]["raw"]
        # Server saw the token (it was on the Authorization header) but
        # the OTLP payload must not contain it in any form.
        self.assertIn(FIXTURE_TOKEN, _AnthropicStub.auth_tokens)
        self.assertNotIn(FIXTURE_TOKEN.encode(), raw)
        self.assertNotIn(b"sk-ant-oat", raw)

    def test_oauth_token_never_in_logs(self):
        self._write_credentials()
        _AnthropicStub.fail_with_reset = True
        proc = self._run(PLAN_STATE_HOOK, {"session_id": "s"})
        self.assertNotIn(FIXTURE_TOKEN.encode(), proc.stderr)
        self.assertNotIn(FIXTURE_TOKEN.encode(), proc.stdout)
        self.assertNotIn(b"sk-ant-oat", proc.stderr)
        self.assertNotIn(b"sk-ant-oat", proc.stdout)


# --- plan-usage hook (Stop, throttled) ------------------------------------


class PlanUsageHookTest(_PlanHookBase):
    def _prime_cache(self, usage_age_minutes: float | None) -> None:
        """Drop a pre-filled cache file (token A) at HOME so plan-usage.py
        finds it. `usage_age_minutes` controls how stale the usage half
        is. None means the prior fetch was exactly at the boundary."""
        self._write_credentials()
        import hashlib
        fp = hashlib.sha256(FIXTURE_TOKEN.encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc)
        last = now - timedelta(minutes=(usage_age_minutes or 0))
        blob = {
            "token_fingerprint": fp,
            "profile_fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "usage_fetched_at": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plan_type": "max",
            "rate_limit_tier": "default_claude_max_20x",
            "organization_type": "claude_max",
            "billing_type": "stripe_subscription",
            "has_extra_usage_enabled": False,
            "billing_mode": "plan_based",
            "usage": {
                "five_hour": {"utilization": 4.0, "resets_at": "2026-06-15T21:10:00Z"},
                "seven_day": {"utilization": 7.0, "resets_at": "2026-06-20T10:00:00Z"},
            },
        }
        path = self.home / ".claude" / "cardinal" / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(blob))

    def test_usage_refetch_throttled_to_10min(self):
        # Last fetch 5 min ago → too fresh, no refetch, no event.
        self._prime_cache(usage_age_minutes=5)
        self._run(PLAN_USAGE_HOOK, {"session_id": "s"})
        self.assertEqual(_AnthropicStub.calls, [])
        self.assertEqual(_OTLPStub.received, [])

    def test_usage_refetched_after_10min(self):
        # Last fetch 11 min ago → stale, refetch happens.
        self._prime_cache(usage_age_minutes=11)
        # Bump utilization to confirm we're actually reading the new
        # response, not echoing the cache.
        _AnthropicStub.usage_response = {
            "five_hour": {"utilization": 12.5, "resets_at": "2026-06-15T22:00:00Z"},
            "seven_day": {"utilization": 9.0, "resets_at": "2026-06-20T10:00:00Z"},
        }
        self._run(PLAN_USAGE_HOOK, {"session_id": "s"})
        self.assertEqual(_AnthropicStub.calls, ["/api/oauth/usage"])
        self.assertEqual(len(_OTLPStub.received), 1)
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        u = by_event["cardinal.plan_usage"][0]
        self.assertAlmostEqual(u["five_hour_utilization"], 12.5)

    def test_silent_exit_when_cache_absent(self):
        # No plan.json on disk → plan-usage MUST NOT bootstrap; that's
        # plan-state's job. Silent exit, no Anthropic fetch.
        self._write_credentials()
        self._run(PLAN_USAGE_HOOK, {"session_id": "s"})
        self.assertEqual(_AnthropicStub.calls, [])
        self.assertEqual(_OTLPStub.received, [])


# --- Downstream stamping ---------------------------------------------------


class DownstreamStampTest(_PlanHookBase):
    def _write_turn_transcript(self) -> Path:
        proj = self.home / "proj"
        proj.mkdir(exist_ok=True)
        path = proj / "sess-stamp.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}),
            json.dumps({"type": "assistant", "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }}),
        ]) + "\n")
        return path

    def _prime_cache_plan_only(self) -> None:
        """Write a cache file with plan_type/rate_limit_tier only — no
        Anthropic call needed."""
        path = self.home / ".claude" / "cardinal" / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "plan_type": "max",
            "rate_limit_tier": "default_claude_max_20x",
        }))

    def test_downstream_hook_stamps_plan_type_from_cache(self):
        self._prime_cache_plan_only()
        path = self._write_turn_transcript()
        proc = subprocess.run(
            ["python3", str(TURN_USAGE_HOOK)],
            input=json.dumps({"session_id": "sess-stamp", "transcript_path": str(path)}).encode(),
            env=self._env(),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(_OTLPStub.received), 1)
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        usages = by_event.get("cardinal.turn_usage", [])
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["plan_type"], "max")
        self.assertEqual(usages[0]["rate_limit_tier"], "default_claude_max_20x")

    def test_downstream_hook_unaffected_when_cache_absent(self):
        # No plan.json on disk → turn-usage still emits, just without
        # plan_type/rate_limit_tier attributes.
        path = self._write_turn_transcript()
        proc = subprocess.run(
            ["python3", str(TURN_USAGE_HOOK)],
            input=json.dumps({"session_id": "sess-stamp", "transcript_path": str(path)}).encode(),
            env=self._env(),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(_OTLPStub.received), 1)
        by_event = _records_by_event(_OTLPStub.received[0]["parsed"])
        u = by_event["cardinal.turn_usage"][0]
        self.assertNotIn("plan_type", u)
        self.assertNotIn("rate_limit_tier", u)


if __name__ == "__main__":
    unittest.main()
