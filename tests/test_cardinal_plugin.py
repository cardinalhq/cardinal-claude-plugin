"""End-to-end tests for the cardinal Claude Code plugin (v0.2).

Each test spins up a stub HTTP server emulating maestro's device-code
endpoints and the maestro-keys revoke endpoint, then runs the plugin's
Python executables as subprocesses with HOME overridden to a temp dir.
We then read back the files the plugin wrote and assert on shape.

Covered:
  - cardinal-connect: device-code happy path; polling pending → success;
    writes settings.json env + ~/.claude.json mcpServers entry + state;
    --telemetry-only opt-out; --rotate guard; --dry-run; existing
    cardinal-* MCP entries auto-removed; ingest/MCP reachability probe
    failures surface as errors.
  - cardinal-disconnect: revokes MCP key (server sees the request),
    removes cardinal entry from ~/.claude.json, removes OTel env block,
    deletes state file; --keep-telemetry leaves the env block alone.
  - cardinal-status: zero-state → "not connected"; happy path renders
    mode + both endpoints.

Run with: python3 -m unittest tests.test_cardinal_plugin -v
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugins" / "cardinal" / "bin"
CONNECT = PLUGIN_BIN / "cardinal-connect"
DISCONNECT = PLUGIN_BIN / "cardinal-disconnect"
STATUS = PLUGIN_BIN / "cardinal-status"


# ---------------------------------------------------------------------------
# Stub server
# ---------------------------------------------------------------------------

class StubMaestro:
    """Tiny HTTP server simulating maestro for device-code + revoke.

    Endpoints:
      POST /api/auth/device/code   → returns a device_code/user_code/uri
      POST /api/auth/device/token  → first call returns authorization_pending,
                                     second call returns the success bundle.
                                     Bundle shape mirrors what conductor's
                                     B4-ingest PR (#949) emits.
      POST /api/maestro-keys/<id>/revoke → 204 always; records the call.
      GET  /api/orgs/<org>/mcp     → reachability probe target (HTTP 405)
      POST <ingest>/v1/metrics      → reachability probe target (HTTP 400 for
                                      empty body)

    Behavior is parameterized so tests can flip individual responses.
    """

    def __init__(self):
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        # Tunable per test:
        self.token_pending_count = 1   # how many pending responses before success
        self.token_calls = 0
        self.last_scopes: list[str] = []
        self.ingest_reachable_status = 400
        self.mcp_reachable_status = 405
        self.revoke_calls: list[tuple[str, str | None]] = []
        self.revoke_status = 204

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # silence
                pass

            def _send(self, status: int, body: dict | None):
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            # ----- /api/auth/device/code ----------------------------------
            def _device_code(self):
                length = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                outer.last_scopes = list(body.get("scopes") or [])
                self._send(201, {
                    "device_code": "dc-xyz",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": f"{outer.url()}/connect?code=ABCD-EFGH",
                    "expires_in": 30,
                    "interval": 1,
                })

            # ----- /api/auth/device/token ---------------------------------
            def _device_token(self):
                length = int(self.headers.get("content-length") or "0")
                _ = self.rfile.read(length)
                outer.token_calls += 1
                if outer.token_calls <= outer.token_pending_count:
                    self._send(400, {"error": "authorization_pending"})
                    return
                bundle = {
                    "org": {"id": "org-uuid-1", "slug": "test-org", "name": "Test Org"},
                    "user": {"id": "user-uuid-1", "email": "rj@example.com"},
                    "telemetry_policy": {
                        "allowed_gates": ["tool_details"],
                        "forced_gates": [],
                        "forbidden_gates": ["user_prompts", "tool_content", "raw_api_bodies"],
                    },
                    "mcp": None,
                    "ingest": None,
                }
                # Mirror the real maestro: scope filter selects which blocks
                # appear in the bundle. Tests can override last_scopes
                # directly if they need a server that misbehaves.
                if "mcp:invoke" in outer.last_scopes:
                    bundle["mcp"] = {
                        "url": f"{outer.url()}/api/orgs/org-uuid-1/mcp",
                        "api_key": "MCPPLAINTEXT" + "x" * 52,
                        "key_id": "mcp-key-uuid-1",
                        "key_prefix": "MCPPLAIN",
                        "created_at": "2026-06-05T00:00:00Z",
                    }
                if "ingest:write" in outer.last_scopes:
                    bundle["ingest"] = {
                        "endpoint": outer.url(),  # the same stub serves /v1/metrics
                        "api_key": "INGESTPLAIN" + "y" * 53,
                        "api_header": "x-cardinalhq-api-key",
                        "key_id": "ingest-key-uuid-1",
                        "key_name": "cardinal-claude-plugin/...",
                        "created_at": "2026-06-05T00:00:00Z",
                        "remote_sync_state": "queued",
                    }
                self._send(200, bundle)

            # ----- /api/maestro-keys/<id>/revoke --------------------------
            def _revoke(self):
                length = int(self.headers.get("content-length") or "0")
                _ = self.rfile.read(length)
                key_id = self.path.split("/")[-2]
                supplied_key = self.headers.get("X-CardinalHQ-API-Key")
                outer.revoke_calls.append((key_id, supplied_key))
                self.send_response(outer.revoke_status)
                self.end_headers()

            def do_GET(self):
                # MCP reachability probe target
                if self.path.startswith("/api/orgs/") and self.path.endswith("/mcp"):
                    self.send_response(outer.mcp_reachable_status)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if self.path == "/api/auth/device/code":
                    self._device_code()
                elif self.path == "/api/auth/device/token":
                    self._device_token()
                elif self.path.startswith("/api/maestro-keys/") and self.path.endswith("/revoke"):
                    self._revoke()
                elif self.path == "/v1/metrics":
                    # Ingest reachability probe target
                    self.send_response(outer.ingest_reachable_status)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_plugin(bin_path: Path, args: list[str], home: Path, env_overrides: dict | None = None,
               timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(bin_path), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = self.home / ".claude" / "settings.json"
        self.claude_json = self.home / ".claude.json"
        self.state = self.home / ".claude" / "cardinal.json"

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    # ----- /code → /token → writes ----------------------------------------

    def test_happy_path_writes_all_three_files(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreater(self.stub.token_calls, 1, "expected at least one pending poll")
        # settings.json env block
        env = read_json(self.settings).get("env", {})
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], self.stub.url())
        self.assertIn("x-cardinalhq-api-key=INGESTPLAIN", env["OTEL_EXPORTER_OTLP_HEADERS"])
        # ~/.claude.json mcpServers.cardinal stanza
        cj = read_json(self.claude_json)
        cardinal = cj["mcpServers"]["cardinal"]
        self.assertEqual(cardinal["type"], "http")
        self.assertTrue(cardinal["url"].endswith("/api/orgs/org-uuid-1/mcp"))
        self.assertTrue(cardinal["headers"]["X-CardinalHQ-API-Key"].startswith("MCPPLAIN"))
        # State file
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-and-mcp")
        self.assertEqual(state["org_id"], "org-uuid-1")
        self.assertEqual(state["mcp_key_id"], "mcp-key-uuid-1")
        self.assertEqual(state["ingest_key_id"], "ingest-key-uuid-1")
        # Plaintexts must NEVER appear in the state file.
        raw_state = self.state.read_text()
        self.assertNotIn("MCPPLAINTEXT", raw_state)
        self.assertNotIn("INGESTPLAIN", raw_state)

    def test_telemetry_only_skips_mcp_entry(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--telemetry-only"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.settings.exists())
        # ~/.claude.json either doesn't exist OR has no cardinal entry.
        cj = read_json(self.claude_json)
        self.assertNotIn("cardinal", cj.get("mcpServers", {}))
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-only")
        self.assertNotIn("mcp_key_id", state)

    def test_already_connected_guard_without_rotate(self):
        # First connect succeeds.
        first = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        # Second invocation without --rotate must refuse.
        second = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already connected", second.stderr.lower())

    def test_rotate_overwrites_existing(self):
        first = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        # Reset stub counters so the second flow goes through the same path.
        self.stub.token_calls = 0
        second = run_plugin(CONNECT, ["--host", self.stub.url(), "--rotate"], self.home)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_dry_run_writes_nothing(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--dry-run"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        # The dry-run output goes to stdout.
        self.assertIn("would_write", res.stdout)
        # No files touched.
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.claude_json.exists())
        self.assertFalse(self.state.exists())

    def test_conflicting_per_driver_entries_are_removed(self):
        # Pre-seed ~/.claude.json with legacy per-integration entries.
        self.claude_json.write_text(json.dumps({
            "someOtherKey": "untouched",
            "mcpServers": {
                "cardinal-lakerunner": {
                    "type": "http",
                    "url": f"{self.stub.url()}/api/orgs/org-uuid-1/integrations/lakerunner/mcp",
                    "headers": {"X-CardinalHQ-API-Key": "old-1"},
                },
                "cardinal-kube": {"type": "http", "url": "https://other.example/x"},
                "user-server": {"type": "http", "url": "https://something-else.example/x"},
            },
        }, indent=2))
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        cj = read_json(self.claude_json)
        servers = cj["mcpServers"]
        # Unified entry present.
        self.assertIn("cardinal", servers)
        # Both legacy entries removed.
        self.assertNotIn("cardinal-lakerunner", servers)
        self.assertNotIn("cardinal-kube", servers)
        # Unrelated entries left alone.
        self.assertIn("user-server", servers)
        self.assertEqual(cj["someOtherKey"], "untouched")
        # A backup must have been written.
        backups = list(self.home.glob(".claude.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_keep_conflicting_flag_preserves_legacy_entries(self):
        self.claude_json.write_text(json.dumps({
            "mcpServers": {
                "cardinal-lakerunner": {
                    "type": "http",
                    "url": f"{self.stub.url()}/api/orgs/org-uuid-1/integrations/lakerunner/mcp",
                },
            },
        }))
        res = run_plugin(
            CONNECT,
            ["--host", self.stub.url(), "--keep-conflicting-mcp-entries"],
            self.home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        servers = read_json(self.claude_json)["mcpServers"]
        self.assertIn("cardinal", servers)
        self.assertIn("cardinal-lakerunner", servers)

    def test_ingest_reachability_failure_aborts_before_writes(self):
        self.stub.ingest_reachable_status = 401
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ingest reachability failed", res.stderr.lower() + res.stdout.lower())
        # No state file should exist after a failed connect.
        self.assertFalse(self.state.exists())

    def test_mcp_reachability_failure_aborts(self):
        self.stub.mcp_reachable_status = 401
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("mcp reachability failed", res.stderr.lower() + res.stdout.lower())

    def test_owned_env_overlay_preserves_unrelated_keys(self):
        # Pre-seed settings.json with a non-OTel key the plugin must not touch.
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({
            "env": {"THEME": "dark", "OTEL_LOG_TOOL_DETAILS": "0"},
        }))
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = read_json(self.settings)["env"]
        self.assertEqual(env["THEME"], "dark")           # untouched
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1")  # overwritten

    def test_no_tool_details_strips_otel_log_tool_details(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--no-tool-details"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = read_json(self.settings)["env"]
        self.assertNotIn("OTEL_LOG_TOOL_DETAILS", env)


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = self.home / ".claude" / "settings.json"
        self.claude_json = self.home / ".claude.json"
        self.state = self.home / ".claude" / "cardinal.json"
        # Run connect first so we have something to disconnect from.
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.stub.revoke_calls.clear()

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_disconnect_revokes_mcp_key_and_removes_everything(self):
        res = run_plugin(DISCONNECT, [], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        # Revoke endpoint was called with the mcp_key_id AND the plaintext.
        self.assertEqual(len(self.stub.revoke_calls), 1)
        key_id, supplied = self.stub.revoke_calls[0]
        self.assertEqual(key_id, "mcp-key-uuid-1")
        self.assertTrue(supplied and supplied.startswith("MCPPLAINTEXT"))
        # Local files: state gone, cardinal entry gone, owned env keys gone.
        self.assertFalse(self.state.exists())
        cj = read_json(self.claude_json)
        self.assertNotIn("cardinal", cj.get("mcpServers", {}))
        env = read_json(self.settings).get("env", {})
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", env)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)

    def test_keep_telemetry_only_removes_mcp_side(self):
        res = run_plugin(DISCONNECT, ["--keep-telemetry"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        # MCP entry removed but settings.json env keys remain.
        cj = read_json(self.claude_json)
        self.assertNotIn("cardinal", cj.get("mcpServers", {}))
        env = read_json(self.settings).get("env", {})
        self.assertEqual(env.get("CLAUDE_CODE_ENABLE_TELEMETRY"), "1")
        # State file rewritten with mode=telemetry-only and mcp fields stripped.
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-only")
        self.assertNotIn("mcp_key_id", state)
        self.assertNotIn("mcp_url", state)

    def test_no_state_file_no_op(self):
        # Wipe the state set up by setUp() and try disconnecting again.
        self.state.unlink()
        # Don't run cardinal-connect's setup again — simulating a fresh box.
        # Remove the writes from setUp so the test reflects "never connected".
        if self.claude_json.exists():
            self.claude_json.unlink()
        if self.settings.exists():
            self.settings.unlink()
        res = run_plugin(DISCONNECT, [], self.home)
        self.assertEqual(res.returncode, 0)
        self.assertIn("not connected", res.stdout.lower())


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_zero_state_says_not_connected(self):
        res = run_plugin(STATUS, [], self.home)
        self.assertEqual(res.returncode, 1)
        self.assertIn("not connected", res.stdout.lower())

    def test_after_connect_renders_both_sides(self):
        run_plugin(CONNECT, ["--host", self.stub.url()], self.home, timeout=15)
        res = run_plugin(STATUS, [], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("telemetry-and-mcp", res.stdout)
        self.assertIn("Telemetry endpoint", res.stdout)
        self.assertIn("MCP endpoint", res.stdout)


if __name__ == "__main__":
    unittest.main()
