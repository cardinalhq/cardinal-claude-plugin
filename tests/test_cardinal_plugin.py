"""End-to-end tests for the cardinal Claude Code plugin (v0.3).

v0.3 declares the `cardinal` MCP server natively via .mcp.json with
${CARDINAL_MCP_URL} / ${CARDINAL_MCP_API_KEY} substitution. The plugin
just sets those env vars in ~/.claude/settings.json; Claude Code does
the rest. ~/.claude.json is no longer touched as a write target — only
prune-on-connect for the v0.2→v0.3 migration.

Each test spins up a stub HTTP server emulating maestro's device-code
+ revoke endpoints, then runs the plugin's Python executables as
subprocesses with HOME overridden to a temp dir.

Run with: python3 -m unittest tests.test_cardinal_plugin -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugins" / "cardinal" / "bin"
CONNECT = PLUGIN_BIN / "cardinal-connect"
DISCONNECT = PLUGIN_BIN / "cardinal-disconnect"
STATUS = PLUGIN_BIN / "cardinal-status"


# ---------------------------------------------------------------------------
# Stub server
# ---------------------------------------------------------------------------

class StubMaestro:
    """HTTP server emulating maestro's device-code + revoke endpoints.

      POST /api/auth/device/code   → returns device_code/user_code/uri.
                                     Records the scopes requested so the
                                     bundle can be scope-filtered.
      POST /api/auth/device/token  → first call returns authorization_pending,
                                     second returns the success bundle
                                     filtered by the requested scopes.
      POST /api/maestro-keys/<id>/revoke → 204 + records call.
      GET  /api/orgs/<org>/mcp     → MCP reachability probe target.
      POST <ingest>/v1/metrics     → ingest reachability probe target.
    """

    def __init__(self):
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.token_pending_count = 1
        self.token_calls = 0
        self.last_scopes: list[str] = []
        self.ingest_reachable_status = 400
        self.mcp_reachable_status = 405
        # When True, the /token response's ingest block carries
        # endpoint=null — simulates a maestro deployment with
        # MAESTRO_INGEST_ENDPOINT unset (the misconfig fixed in v1.52.0-rc3).
        self.bundle_null_ingest_endpoint = False
        # First N ingest probes return 401 before falling through to
        # `ingest_reachable_status`. Simulates the
        # provision_ingest_key worker race: bundle is back to the plugin
        # but Lakerunner doesn't see the key yet (the race v0.3.3
        # papers over with backoff retry).
        self.ingest_transient_401_count = 0
        self.ingest_probe_count = 0
        self.revoke_calls: list[tuple[str, str | None]] = []
        self.revoke_status = 204

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status: int, body: dict | None):
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

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
                        "endpoint": None if outer.bundle_null_ingest_endpoint else outer.url(),
                        "api_key": "INGESTPLAIN" + "y" * 53,
                        "api_header": "x-cardinalhq-api-key",
                        "key_id": "ingest-key-uuid-1",
                        "key_name": "cardinal-claude-plugin/...",
                        "created_at": "2026-06-05T00:00:00Z",
                        "remote_sync_state": "queued",
                    }
                self._send(200, bundle)

            def _revoke(self):
                length = int(self.headers.get("content-length") or "0")
                _ = self.rfile.read(length)
                key_id = self.path.split("/")[-2]
                supplied_key = self.headers.get("X-CardinalHQ-API-Key")
                outer.revoke_calls.append((key_id, supplied_key))
                self.send_response(outer.revoke_status)
                self.end_headers()

            def do_GET(self):
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
                    outer.ingest_probe_count += 1
                    if outer.ingest_probe_count <= outer.ingest_transient_401_count:
                        self.send_response(401)
                    else:
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


def settings_env(home: Path) -> dict:
    settings = read_json(home / ".claude" / "settings.json")
    return settings.get("env", {}) if isinstance(settings.get("env"), dict) else {}


# ---------------------------------------------------------------------------
# Static manifest tests — the plugin's declarative MCP is correct
# ---------------------------------------------------------------------------

class ManifestTests(unittest.TestCase):
    PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "cardinal"

    def test_mcp_json_declares_cardinal_with_env_substitution(self):
        path = self.PLUGIN_ROOT / ".mcp.json"
        self.assertTrue(path.exists(), ".mcp.json must exist at the plugin root")
        data = json.loads(path.read_text())
        self.assertIn("cardinal", data)
        entry = data["cardinal"]
        self.assertEqual(entry["type"], "http")
        # The URL and header value MUST be env-var placeholders; a literal
        # URL would defeat the per-user-key design.
        self.assertEqual(entry["url"], "${CARDINAL_MCP_URL}")
        self.assertEqual(entry["headers"]["X-CardinalHQ-API-Key"], "${CARDINAL_MCP_API_KEY}")

    def test_plugin_json_version_matches_bin_constant(self):
        manifest = json.loads((self.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        # Grep the PLUGIN_VERSION constant out of cardinal-connect so a
        # mismatched bump in either file is caught.
        text = (self.PLUGIN_ROOT / "bin" / "cardinal-connect").read_text()
        import re
        m = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m, "PLUGIN_VERSION constant not found in cardinal-connect")
        self.assertEqual(manifest["version"], m.group(1))


# ---------------------------------------------------------------------------
# Connect tests
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

    def test_happy_path_writes_env_and_state_only(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreater(self.stub.token_calls, 1, "expected at least one pending poll")

        env = settings_env(self.home)
        # Telemetry env keys present
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], self.stub.url())
        self.assertIn("x-cardinalhq-api-key=INGESTPLAIN", env["OTEL_EXPORTER_OTLP_HEADERS"])
        # MCP env vars present — these are what the plugin's .mcp.json substitutes
        self.assertTrue(env["CARDINAL_MCP_URL"].endswith("/api/orgs/org-uuid-1/mcp"))
        self.assertTrue(env["CARDINAL_MCP_API_KEY"].startswith("MCPPLAIN"))

        # State file
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-and-mcp")
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["org_id"], "org-uuid-1")
        self.assertEqual(state["mcp_key_id"], "mcp-key-uuid-1")
        # Plaintexts MUST never appear in the state file.
        raw_state = self.state.read_text()
        self.assertNotIn("MCPPLAINTEXT", raw_state)
        self.assertNotIn("INGESTPLAIN", raw_state)

        # ~/.claude.json must NOT exist after a clean connect — the plugin
        # doesn't touch it on greenfield installs.
        self.assertFalse(self.claude_json.exists(),
                         "~/.claude.json must not be created by v0.3 connect")

    def test_telemetry_only_omits_mcp_env_vars(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--telemetry-only"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = settings_env(self.home)
        self.assertIn("CLAUDE_CODE_ENABLE_TELEMETRY", env)
        self.assertNotIn("CARDINAL_MCP_URL", env)
        self.assertNotIn("CARDINAL_MCP_API_KEY", env)
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-only")
        self.assertNotIn("mcp_key_id", state)

    def test_already_connected_guard_without_rotate(self):
        first = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        second = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already connected", second.stderr.lower())

    def test_rotate_overwrites_existing(self):
        first = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        self.stub.token_calls = 0
        second = run_plugin(CONNECT, ["--host", self.stub.url(), "--rotate"], self.home)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_dry_run_writes_nothing(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--dry-run"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("settings_env_keys", res.stdout)
        self.assertIn("CARDINAL_MCP_URL", res.stdout)
        # No files touched
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.claude_json.exists())
        self.assertFalse(self.state.exists())

    def test_v02_legacy_entries_pruned_from_claude_json(self):
        # Pre-seed ~/.claude.json with v0.2's mcpServers.cardinal entry +
        # legacy per-driver cardinal-* entries + an unrelated user-server.
        self.claude_json.write_text(json.dumps({
            "someOtherKey": "untouched",
            "mcpServers": {
                "cardinal": {  # what v0.2 wrote — must be removed for v0.3
                    "type": "http",
                    "url": f"{self.stub.url()}/api/orgs/org-uuid-1/mcp",
                    "headers": {"X-CardinalHQ-API-Key": "old-v02-key"},
                },
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
        # v0.2 cardinal entry gone — would otherwise collide with .mcp.json's declaration
        self.assertNotIn("cardinal", servers)
        # cardinal-* entries gone
        self.assertNotIn("cardinal-lakerunner", servers)
        self.assertNotIn("cardinal-kube", servers)
        # unrelated entries untouched
        self.assertIn("user-server", servers)
        self.assertEqual(cj["someOtherKey"], "untouched")
        # Backup created
        backups = list(self.home.glob(".claude.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_skip_legacy_cleanup_preserves_v02_entries(self):
        self.claude_json.write_text(json.dumps({
            "mcpServers": {
                "cardinal": {"type": "http", "url": "https://stale.example/mcp"},
            },
        }))
        res = run_plugin(
            CONNECT,
            ["--host", self.stub.url(), "--skip-legacy-cleanup"],
            self.home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        # Stale entry left untouched per opt-in flag.
        servers = read_json(self.claude_json)["mcpServers"]
        self.assertIn("cardinal", servers)

    def test_ingest_reachability_failure_aborts_before_writes(self):
        # Permanent 401 — even after the v0.3.3 retry backoff exhausts, the
        # bin must abort before any writes and surface a clear message.
        self.stub.ingest_reachable_status = 401
        # Match the bin's full retry sleep budget so we don't hang the suite
        # forever if the retries somehow misbehave.
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home, timeout=45)
        self.assertNotEqual(res.returncode, 0)
        out = (res.stderr + res.stdout).lower()
        self.assertIn("ingest reachability failed", out)
        # v0.3.3 surfaces the retry-exhausted hint.
        self.assertIn("never propagated to lakerunner", out)
        self.assertFalse(self.state.exists())

    def test_ingest_probe_recovers_from_transient_401(self):
        # Simulates the provision_ingest_key worker race: the OTLP intake
        # 401s on the first probe (Lakerunner hasn't seen the new key
        # yet), then accepts the second probe (worker pushed it through
        # in the 1s gap). The bin must NOT abort — it should retry and
        # complete the connect cleanly.
        self.stub.ingest_transient_401_count = 1
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        out = res.stdout + res.stderr
        # The retry progress line confirms the backoff loop fired.
        self.assertIn("ingest key 401s", out.lower())
        # Two probes total — one 401, one success.
        self.assertEqual(self.stub.ingest_probe_count, 2)
        # State and env both committed normally.
        self.assertTrue(self.state.exists())
        env = settings_env(self.home)
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")

    def test_mcp_reachability_failure_aborts(self):
        self.stub.mcp_reachable_status = 401
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("mcp reachability failed", res.stderr.lower() + res.stdout.lower())

    def test_null_ingest_endpoint_fails_cleanly_not_traceback(self):
        # Simulates the misconfig that bit dogfood: server returns the
        # ingest block with endpoint=null because MAESTRO_INGEST_ENDPOINT
        # was never plumbed through. v0.3.1 would crash here with
        # AttributeError: 'NoneType' object has no attribute 'rstrip'.
        # v0.3.2's guard surfaces a clear operator-misconfig message.
        self.stub.bundle_null_ingest_endpoint = True
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(res.returncode, 0)
        out = res.stdout.lower() + res.stderr.lower()
        # No Python traceback / AttributeError
        self.assertNotIn("traceback", out)
        self.assertNotIn("attributeerror", out)
        self.assertNotIn("nonetype", out)
        # Clear, actionable error
        self.assertIn("ingest", out)
        self.assertIn("endpoint", out)
        # Nothing was written.
        self.assertFalse(self.state.exists())

    def test_owned_env_overlay_preserves_unrelated_keys(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({
            "env": {"THEME": "dark", "OTEL_LOG_TOOL_DETAILS": "0"},
        }))
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = settings_env(self.home)
        self.assertEqual(env["THEME"], "dark")              # untouched
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1") # overwritten

    def test_no_tool_details_strips_otel_log_tool_details(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url(), "--no-tool-details"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = settings_env(self.home)
        self.assertNotIn("OTEL_LOG_TOOL_DETAILS", env)

    def test_telemetry_only_after_full_connect_strips_mcp_env_vars(self):
        # Full connect populates both env vars
        run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        env = settings_env(self.home)
        self.assertIn("CARDINAL_MCP_URL", env)
        # Rotate with --telemetry-only — owned-key overlay should DROP the
        # MCP keys (they weren't in the new env block).
        self.stub.token_calls = 0
        run_plugin(
            CONNECT,
            ["--host", self.stub.url(), "--rotate", "--telemetry-only"],
            self.home,
        )
        env = settings_env(self.home)
        self.assertNotIn("CARDINAL_MCP_URL", env)
        self.assertNotIn("CARDINAL_MCP_API_KEY", env)


# ---------------------------------------------------------------------------
# Disconnect tests
# ---------------------------------------------------------------------------
# Pending-file lifecycle (v0.3.1)
#
# Claude Code's Bash tool buffers stdout until a command returns. The
# v0.3.1 patch writes the verification URL to ~/.claude/cardinal-pending.json
# right after /code returns so Claude can surface it while the polling
# loop blocks. These tests pin the file's appearance, shape, and cleanup.
# ---------------------------------------------------------------------------

class PendingFileTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.pending = self.home / ".claude" / "cardinal-pending.json"

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def _start_connect(self) -> subprocess.Popen:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        return subprocess.Popen(
            [sys.executable, str(CONNECT), "--host", self.stub.url()],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _wait_for_pending(self, timeout_s: float = 5.0) -> dict:
        """Poll for the pending file the way the SKILL.md tells Claude to.
        Fails the test if it doesn't appear within timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.pending.exists():
                return json.loads(self.pending.read_text())
            time.sleep(0.1)
        self.fail(f"pending file did not appear within {timeout_s}s")

    def test_pending_file_appears_with_correct_shape(self):
        # Make the stub take ~3s before flipping to success so we have a
        # comfortable window to read the pending file mid-poll.
        self.stub.token_pending_count = 3
        proc = self._start_connect()
        try:
            pending = self._wait_for_pending()
            # Required fields per the SKILL.md contract
            self.assertIn("verification_uri", pending)
            self.assertTrue(pending["verification_uri"].endswith("/connect?code=ABCD-EFGH"))
            self.assertEqual(pending["user_code"], "ABCD-EFGH")
            self.assertGreater(int(pending["expires_in"]), 0)
            self.assertIn("written_at", pending)
            # Version is stamped but exact value is bumped per release —
            # just confirm it's present and non-empty.
            self.assertTrue(pending.get("plugin_version"))
        finally:
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    def test_pending_file_removed_on_success_exit(self):
        # token_pending_count=1: first poll pending, second poll succeeds.
        # Cardinal-connect completes normally; the finally should clean up.
        proc = self._start_connect()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, f"stdout={out}\nstderr={err}")
        self.assertFalse(
            self.pending.exists(),
            f"pending file leaked after successful exit; stdout=\n{out}",
        )

    def test_pending_file_removed_on_denied_exit(self):
        # Simulate the user denying mid-flow by sending access_denied from
        # /token. Easiest path: monkey-patch the stub to return access_denied
        # via overriding token_pending_count to a sentinel; instead, just
        # use a second StubMaestro variant by intercepting the response.
        # The simpler trick: drive the bin to fail at /token via a host
        # that returns 400 access_denied after the first poll.
        import threading as _th
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class DenyHandler(BaseHTTPRequestHandler):
            seen_token = False
            def log_message(self, *_): pass
            def do_POST(self):
                length = int(self.headers.get("content-length") or "0")
                self.rfile.read(length)
                if self.path == "/api/auth/device/code":
                    self.send_response(201)
                    self.send_header("content-type", "application/json")
                    body = json.dumps({
                        "device_code": "dc-xyz",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "http://example.invalid/connect?code=ABCD-EFGH",
                        "expires_in": 30,
                        "interval": 1,
                    }).encode()
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/auth/device/token":
                    self.send_response(400)
                    self.send_header("content-type", "application/json")
                    body = b'{"error":"access_denied"}'
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404); self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), DenyHandler)
        port = server.server_address[1]
        thread = _th.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["HOME"] = str(self.home)
            proc = subprocess.run(
                [sys.executable, str(CONNECT), "--host", f"http://127.0.0.1:{port}"],
                env=env, capture_output=True, text=True, timeout=15,
            )
            # Bin exits non-zero on access_denied
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("denied", (proc.stdout + proc.stderr).lower())
            # Finally still clears the pending file
            self.assertFalse(
                self.pending.exists(),
                "pending file leaked after access_denied exit",
            )
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------

class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = self.home / ".claude" / "settings.json"
        self.state = self.home / ".claude" / "cardinal.json"
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.stub.revoke_calls.clear()

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_disconnect_revokes_via_env_plaintext_and_strips_env(self):
        res = run_plugin(DISCONNECT, [], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        # Revoke endpoint called with the mcp_key_id AND the plaintext
        # sourced from settings.json env (v0.3 path), not ~/.claude.json.
        self.assertEqual(len(self.stub.revoke_calls), 1)
        key_id, supplied = self.stub.revoke_calls[0]
        self.assertEqual(key_id, "mcp-key-uuid-1")
        self.assertTrue(supplied and supplied.startswith("MCPPLAINTEXT"))
        # State gone, owned env keys gone.
        self.assertFalse(self.state.exists())
        env = settings_env(self.home)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", env)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)
        self.assertNotIn("CARDINAL_MCP_URL", env)
        self.assertNotIn("CARDINAL_MCP_API_KEY", env)

    def test_keep_telemetry_only_removes_mcp_side(self):
        res = run_plugin(DISCONNECT, ["--keep-telemetry"], self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = settings_env(self.home)
        # MCP env vars gone, telemetry env vars preserved.
        self.assertNotIn("CARDINAL_MCP_URL", env)
        self.assertNotIn("CARDINAL_MCP_API_KEY", env)
        self.assertEqual(env.get("CLAUDE_CODE_ENABLE_TELEMETRY"), "1")
        # State rewritten as telemetry-only.
        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-only")
        self.assertNotIn("mcp_key_id", state)

    def test_no_state_file_no_op(self):
        self.state.unlink()
        if self.settings.exists():
            self.settings.unlink()
        res = run_plugin(DISCONNECT, [], self.home)
        self.assertEqual(res.returncode, 0)
        self.assertIn("not connected", res.stdout.lower())


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------

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

    def test_status_fails_when_mcp_env_vars_missing(self):
        # Connect, then nuke just the env vars from settings.json so the
        # state and env are out of sync. status should surface the
        # mismatch and exit 1 with a repair hint.
        run_plugin(CONNECT, ["--host", self.stub.url()], self.home, timeout=15)
        settings = read_json(self.home / ".claude" / "settings.json")
        env = settings.get("env", {})
        env.pop("CARDINAL_MCP_URL", None)
        env.pop("CARDINAL_MCP_API_KEY", None)
        settings["env"] = env
        (self.home / ".claude" / "settings.json").write_text(json.dumps(settings))

        res = run_plugin(STATUS, [], self.home)
        self.assertEqual(res.returncode, 1)
        self.assertIn("CARDINAL_MCP_URL", res.stdout)
        self.assertIn("--rotate", res.stdout)


# ---------------------------------------------------------------------------
# Initiative resolution tests (cardinal.initiative.* attribution)
# ---------------------------------------------------------------------------
# These exercise the 8 cases enumerated in conductor
# docs/specs/cardinal-initiative.md §"Test plan → Plugin (P0)":
#   1. Valid JSON file → name + description.
#   2. Malformed JSON → fall through, no crash.
#   3. JSON with bad name (missing / not string / not kebab) → fall through.
#   4. CARDINAL_INITIATIVE env var → wins over file.
#   5. No env, no file, branch `feat/foo-bar` → name=`foo`.
#   6. No env, no file, no branch, HEAD subject `feat(baz): …` → name=`baz`.
#   7. No signal → (None, None) — hook emits nothing.
#   8. File created mid-session → next call picks it up.
#
# We import git-state.py directly (importlib gymnastics for the hyphen)
# so each case is a fast, isolated function call. The full hook is
# exercised by the existing connect/disconnect tests; the resolution
# chain doesn't need an HTTP stub.

HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks" / "git-state.py"
)
_spec = importlib.util.spec_from_file_location("git_state_hook", HOOK_PATH)
git_state_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(git_state_hook)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    )


def _init_repo(root: Path, branch: str = "main") -> None:
    """Initialise a minimal git repo with one committable file so that
    HEAD resolves. Branch defaults to `main`; callers override via the
    `branch=` kwarg or check out a new branch afterwards.
    """
    _git(["init", "-q", "-b", branch], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "README").write_text("seed\n")
    _git(["add", "README"], root)
    _git(["commit", "-q", "-m", "chore: seed"], root)


class InitiativeResolutionTests(unittest.TestCase):
    """Unit tests for git_state_hook._resolve_initiative()."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Empty settings_env — tests that need to override settings.json
        # pass their own dict in. CARDINAL_INITIATIVE env is also wiped
        # so it doesn't leak in from the caller's shell.
        self.settings_env: dict[str, str] = {}
        self._env_patcher = mock.patch.dict(
            os.environ, {}, clear=False,
        )
        self._env_patcher.start()
        os.environ.pop("CARDINAL_INITIATIVE", None)

    def tearDown(self):
        self._env_patcher.stop()
        self.tmp.cleanup()

    # --- Case 1 -----------------------------------------------------------
    def test_valid_json_file_yields_name_and_description(self):
        _init_repo(self.root)
        (self.root / ".cardinal-initiative").write_text(json.dumps({
            "name": "outcomes-observability",
            "description": "Make agent spend traceable to the initiative it served.",
        }))
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertEqual(name, "outcomes-observability")
        self.assertEqual(
            desc, "Make agent spend traceable to the initiative it served.",
        )
        # File omits `type` — attribute is dropped (caller will omit emit).
        self.assertIsNone(itype)

    # --- Case 2 -----------------------------------------------------------
    def test_malformed_json_falls_through_without_crash(self):
        # Set up a branch whose prefix produces a clear next-signal name
        # so we can verify the fall-through landed somewhere sensible.
        _init_repo(self.root, branch="feat/fallback-branch")
        (self.root / ".cardinal-initiative").write_text("{not valid json,,,")
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        # Falls through to branch prefix: feat/fallback-branch → "fallback".
        self.assertEqual(name, "fallback")
        self.assertIsNone(desc)

    # --- Case 3 -----------------------------------------------------------
    def test_json_with_bad_name_falls_through(self):
        # Four sub-cases collapsed into one test: each invalidates the
        # file and forces the resolver into the next signal. Branch
        # prefix `feat/fallback-x` → name="fallback".
        for bad_doc in [
            {"description": "name missing"},
            {"name": 123, "description": "name not string"},
            {"name": "NotKebab_Case", "description": "name not kebab"},
            {"name": "five-segments-is-too-many-here", "description": "too many"},
        ]:
            with self.subTest(doc=bad_doc), TemporaryDirectory() as raw:
                root = Path(raw)
                _init_repo(root, branch="feat/fallback-x")
                (root / ".cardinal-initiative").write_text(
                    json.dumps(bad_doc)
                )
                name, desc, itype = git_state_hook._resolve_initiative(
                    str(root), self.settings_env,
                )
                self.assertEqual(name, "fallback")
                self.assertIsNone(desc)

    # --- Case 4 -----------------------------------------------------------
    def test_env_var_wins_over_file(self):
        _init_repo(self.root)
        (self.root / ".cardinal-initiative").write_text(json.dumps({
            "name": "file-name",
            "description": "the file's description should not be used",
        }))
        os.environ["CARDINAL_INITIATIVE"] = "env-override"
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertEqual(name, "env-override")
        # Per spec: description and type come from the file source only.
        # Env override sets name only, even if the file has them.
        self.assertIsNone(desc)
        self.assertIsNone(itype)

    def test_env_var_settings_block_also_wins(self):
        # The hook reads either os.environ OR the settings.json env block.
        # This pins the second surface so devs can opt into either path.
        _init_repo(self.root)
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root),
            {"CARDINAL_INITIATIVE": "settings-env-override"},
        )
        self.assertEqual(name, "settings-env-override")
        self.assertIsNone(desc)

    # --- Case 5 -----------------------------------------------------------
    def test_branch_prefix_yields_first_segment(self):
        _init_repo(self.root, branch="feat/foo-bar")
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertEqual(name, "foo")
        self.assertIsNone(desc)

    # --- Case 6 -----------------------------------------------------------
    def test_conventional_commit_scope_of_head(self):
        # Branch has no slash so branch-prefix source doesn't trigger;
        # HEAD subject carries a conventional scope, which is the fourth
        # signal.
        _init_repo(self.root, branch="trunk")
        (self.root / "f.txt").write_text("x\n")
        _git(["add", "f.txt"], self.root)
        _git(["commit", "-q", "-m", "feat(baz): wire stuff up"], self.root)
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertEqual(name, "baz")
        self.assertIsNone(desc)

    # --- Case 7 -----------------------------------------------------------
    def test_no_signal_returns_none(self):
        # Branch without `/`, HEAD subject without a conventional scope,
        # no file, no env → no signal.
        _init_repo(self.root, branch="trunk")
        (self.root / "f.txt").write_text("x\n")
        _git(["add", "f.txt"], self.root)
        _git(["commit", "-q", "-m", "just a plain subject, no scope"], self.root)
        name, desc, itype = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertIsNone(name)
        self.assertIsNone(desc)

    # --- Case 8 -----------------------------------------------------------
    def test_file_created_mid_session_is_picked_up_on_next_call(self):
        # Simulates Claude using Write to create .cardinal-initiative
        # between turns. First resolution falls through; the second
        # picks up the freshly-written file. The hook is per-turn, so
        # "next turn" is "next resolve_initiative() call."
        _init_repo(self.root, branch="trunk")
        before_name, before_desc, before_type = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertIsNone(before_name)
        self.assertIsNone(before_desc)
        self.assertIsNone(before_type)
        (self.root / ".cardinal-initiative").write_text(json.dumps({
            "name": "mid-session-write",
            "description": "Authored by Claude during turn 1.",
            "type": "feature",
        }))
        after_name, after_desc, after_type = git_state_hook._resolve_initiative(
            str(self.root), self.settings_env,
        )
        self.assertEqual(after_name, "mid-session-write")
        self.assertEqual(after_desc, "Authored by Claude during turn 1.")
        self.assertEqual(after_type, "feature")

    # --- `type` field — closed vocabulary --------------------------------
    def test_valid_type_emitted_alongside_name_and_description(self):
        # Every value in the closed vocabulary flows through. A single
        # sub-test per value pins the enum membership against drift.
        for valid_type in ["feature", "bugfix", "refactor", "infra", "research"]:
            with self.subTest(type=valid_type), TemporaryDirectory() as raw:
                root = Path(raw)
                _init_repo(root)
                (root / ".cardinal-initiative").write_text(json.dumps({
                    "name": "typed-initiative",
                    "description": "desc",
                    "type": valid_type,
                }))
                name, desc, itype = git_state_hook._resolve_initiative(
                    str(root), self.settings_env,
                )
                self.assertEqual(name, "typed-initiative")
                self.assertEqual(desc, "desc")
                self.assertEqual(itype, valid_type)

    def test_invalid_type_drops_type_but_keeps_name_and_description(self):
        # Unknown `type` value is dropped to None — same fall-through as
        # a missing description — but the file's name+description still
        # flow through (the name field is independently valid).
        for bad_type in ["bug", "FEATURE", 42, None, ["feature"]]:
            with self.subTest(type=bad_type), TemporaryDirectory() as raw:
                root = Path(raw)
                _init_repo(root)
                (root / ".cardinal-initiative").write_text(json.dumps({
                    "name": "bad-type-initiative",
                    "description": "still-valid",
                    "type": bad_type,
                }))
                name, desc, itype = git_state_hook._resolve_initiative(
                    str(root), self.settings_env,
                )
                self.assertEqual(name, "bad-type-initiative")
                self.assertEqual(desc, "still-valid")
                self.assertIsNone(itype)


# ---------------------------------------------------------------------------
# SessionStart nudge — initiative-prompt.py
# ---------------------------------------------------------------------------

INITIATIVE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks" / "initiative-prompt.py"
)


def _run_initiative_prompt(cwd: Path) -> subprocess.CompletedProcess:
    payload = json.dumps({
        "session_id": "sess-1",
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    return subprocess.run(
        [sys.executable, str(INITIATIVE_PROMPT_PATH)],
        input=payload, capture_output=True, text=True, timeout=10,
    )


class InitiativePromptHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emits_additional_context_when_file_absent(self):
        _init_repo(self.root)
        res = _run_initiative_prompt(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(
            body["hookSpecificOutput"]["hookEventName"], "SessionStart",
        )
        prompt = body["hookSpecificOutput"]["additionalContext"]
        # Must mention the file name and the Write tool — these are the
        # two pieces Claude needs to act on the instruction.
        self.assertIn(".cardinal-initiative", prompt)
        self.assertIn("Write tool", prompt)
        # The `type` enum is documented inline so Claude doesn't have to
        # guess at the closed vocabulary.
        for v in ["feature", "bugfix", "refactor", "infra", "research"]:
            self.assertIn(v, prompt)

    def test_silent_when_file_already_exists(self):
        _init_repo(self.root)
        (self.root / ".cardinal-initiative").write_text(
            '{"name": "x", "description": "y"}'
        )
        res = _run_initiative_prompt(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_silent_when_not_a_git_repo(self):
        # No git init — the cwd has no repo root, so there's nowhere
        # to author the file at and the nudge would be wasted context.
        res = _run_initiative_prompt(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")


if __name__ == "__main__":
    unittest.main()
