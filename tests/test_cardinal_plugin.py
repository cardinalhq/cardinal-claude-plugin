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
        # Spend-limits status endpoint: the verdict to serve (None → 404,
        # simulating a maestro without the limits feature) + call count.
        self.limits_verdict: dict | None = None
        self.limits_calls = 0

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
                    # Spend-limits discovery rides alongside the ingest key
                    # it authenticates with (see device-auth.ts).
                    bundle["limits"] = {
                        "status_url": f"{outer.url()}/api/agent-limits/status",
                        "enabled": True,
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
                if self.path.startswith("/api/agent-limits/status"):
                    outer.limits_calls += 1
                    if outer.limits_verdict is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._send(200, outer.limits_verdict)
                    return
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
# Initiative resolution tests — cardinal.initiative.* attribution
# ---------------------------------------------------------------------------
# v0.6.0 reduces the resolver to a pure function: branch in, (name, type)
# out. No file lookup, no env var, no priority chain, no conventional-
# commit fallback. These tests pin the four buckets that branch can fall
# into and verify the closed enum is enforced.

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
    """Pure-function tests for git_state_hook._resolve_initiative(branch).

    The function is intentionally state-free in v0.6.0, so we call it
    directly with branch strings rather than spinning up git repos.
    """

    # --- Protected/trunk branches → research, no name -------------------
    def test_protected_branches_yield_research_with_no_name(self):
        for branch in ["main", "master", "develop", "trunk"]:
            with self.subTest(branch=branch):
                name, itype = git_state_hook._resolve_initiative(branch)
                self.assertIsNone(
                    name,
                    f"{branch}: protected branches must NOT supply a "
                    f"name (would collapse unrelated work into one fake "
                    f"initiative)",
                )
                self.assertEqual(
                    itype, "research",
                    f"{branch}: protected branches default to research",
                )

    # --- No branch / detached HEAD → research, no name ------------------
    def test_no_branch_yields_research_with_no_name(self):
        for sentinel in [None, "", "HEAD"]:
            with self.subTest(sentinel=sentinel):
                name, itype = git_state_hook._resolve_initiative(sentinel)
                self.assertIsNone(name)
                self.assertEqual(itype, "research")

    # --- Recognized prefixes → (rest, mapped type) ----------------------
    def test_recognized_prefixes_map_to_canonical_types(self):
        # Pin every prefix → type mapping in _PREFIX_TO_TYPE. Aliases
        # collapse: feat/feature both → feature, fix/bugfix both →
        # bugfix, chore/infra both → infra, spike/research both →
        # research. This is what enables consistent classification
        # across teams using different conventions.
        cases = [
            ("feat/outcomes-observability",   "outcomes-observability",   "feature"),
            ("feature/outcomes-observability","outcomes-observability",   "feature"),
            ("fix/login-crash",               "login-crash",              "bugfix"),
            ("bugfix/login-crash",            "login-crash",              "bugfix"),
            ("refactor/auth-token",           "auth-token",               "refactor"),
            ("infra/k8s-bump",                "k8s-bump",                 "infra"),
            ("chore/k8s-bump",                "k8s-bump",                 "infra"),
            ("research/data-pipeline-spike",  "data-pipeline-spike",      "research"),
            ("spike/data-pipeline-spike",     "data-pipeline-spike",      "research"),
            # Conventional-but-uncanonical prefixes (Phase 0 of conductor's
            # ai-hygiene-feedback-loop spec): the tail is the name — the
            # slash must NOT leak into the emitted initiative name — and
            # the type maps to the closest member of the closed enum.
            ("perf/logs-raw-wide-window-latency", "logs-raw-wide-window-latency", "feature"),
            ("cleanup/dead-flags",            "dead-flags",               "refactor"),
            ("test/flaky-suite-quarantine",   "flaky-suite-quarantine",   "infra"),
            ("tests/flaky-suite-quarantine",  "flaky-suite-quarantine",   "infra"),
            ("ci/release-pipeline",           "release-pipeline",         "infra"),
            ("build/esbuild-migration",       "esbuild-migration",        "infra"),
            ("deps/react-19-bump",            "react-19-bump",            "infra"),
            ("docs/install-guide",            "install-guide",            "infra"),
            ("doc/install-guide",             "install-guide",            "infra"),
        ]
        for branch, want_name, want_type in cases:
            with self.subTest(branch=branch):
                name, itype = git_state_hook._resolve_initiative(branch)
                self.assertEqual(name, want_name)
                self.assertEqual(itype, want_type)

    # --- Recognized prefix, case-insensitive ----------------------------
    def test_prefix_match_is_case_insensitive(self):
        # Branch like `Feat/foo-bar` still maps. Real-world branches
        # are almost always lowercase, but case-folding the prefix
        # comparison keeps a typo from silently defaulting to feature.
        name, itype = git_state_hook._resolve_initiative("Feat/foo-bar")
        self.assertEqual(name, "foo-bar")
        self.assertEqual(itype, "feature")

    # --- Multi-segment tail flows through verbatim ----------------------
    def test_multi_segment_tail_kept_intact(self):
        # `feat/multi-segment-name-keeps-going` keeps the whole tail as
        # the initiative name — kebab-case, multiple segments. The
        # convention recommends 1–4 segments but the resolver doesn't
        # enforce it (humans don't always conform; we don't drop their
        # session attribution because of it).
        name, itype = git_state_hook._resolve_initiative(
            "feat/multi-segment-name-keeps-going",
        )
        self.assertEqual(name, "multi-segment-name-keeps-going")
        self.assertEqual(itype, "feature")

    # --- Unrecognized prefix → branch verbatim, type=feature ------------
    def test_unrecognized_prefix_falls_back_to_feature_with_branch_as_name(self):
        # `rjha/some-thing` doesn't match a known prefix. The whole
        # branch becomes the name (so sessions on it cluster together)
        # and the type defaults to feature (the modal piece of work).
        name, itype = git_state_hook._resolve_initiative("rjha/some-thing")
        self.assertEqual(name, "rjha/some-thing")
        self.assertEqual(itype, "feature")

    # --- No slash, not protected → branch verbatim, type=feature --------
    def test_unprefixed_branch_falls_back_to_feature(self):
        # A branch like `my-personal-work` (no slash, not in protected
        # set) becomes name=branch, type=feature.
        name, itype = git_state_hook._resolve_initiative("my-personal-work")
        self.assertEqual(name, "my-personal-work")
        self.assertEqual(itype, "feature")

    # --- EnterWorktree branches: strip the worktree head ----------------
    def test_worktree_noise_is_stripped_from_the_name(self):
        # EnterWorktree generates branches like `worktree-fix-1018-
        # github-app-repo-picker`. The `worktree` segment plus any
        # immediately following noise segments ({fix, feat, bug, bugfix,
        # issue, issues, pr}) and pure-numeric segments are dropped until
        # the first real segment, so the emitted initiative name is the
        # actual work, not plumbing. Mirrors conductor's
        # normalizeInitiativeName (ui-pages dashboards/system/initiative.ts).
        cases = [
            ("worktree-fix-1018-github-app-repo-picker", "github-app-repo-picker"),
            ("worktree-investigate-log-query-step", "investigate-log-query-step"),
            ("worktree-issue-862-split-auth-context", "split-auth-context"),
        ]
        for branch, want_name in cases:
            with self.subTest(branch=branch):
                name, itype = git_state_hook._resolve_initiative(branch)
                self.assertEqual(name, want_name)
                self.assertEqual(itype, "feature")

    def test_worktree_strip_applies_after_prefix_strip(self):
        # A typed worktree branch strips the prefix first, then the
        # worktree head — both halves of the pollution fix compose.
        name, itype = git_state_hook._resolve_initiative(
            "fix/worktree-fix-1018-github-app-repo-picker",
        )
        self.assertEqual(name, "github-app-repo-picker")
        self.assertEqual(itype, "bugfix")

    def test_worktree_branch_with_no_real_segments_kept_verbatim(self):
        # `worktree-fix-1018` is all noise after the head — stripping
        # would leave nothing, so the original name is kept (a stable,
        # if ugly, cluster key beats an empty one).
        name, itype = git_state_hook._resolve_initiative("worktree-fix-1018")
        self.assertEqual(name, "worktree-fix-1018")
        self.assertEqual(itype, "feature")

    def test_worktree_strip_is_idempotent(self):
        # Re-resolving an already-stripped name must not strip further:
        # the noise words only count while consuming the worktree head.
        # `test-in-pod` (real name starting with a noise-ish word) and a
        # stripped result both pass through unchanged.
        for already_clean in [
            "github-app-repo-picker",
            "investigate-log-query-step",
            "test-in-pod",
            "fix-1018-something",
        ]:
            with self.subTest(name=already_clean):
                name, _ = git_state_hook._resolve_initiative(already_clean)
                self.assertEqual(name, already_clean)
        # And the strip helper itself is a fixed point on its own output.
        once = git_state_hook._strip_worktree_noise(
            "worktree-fix-1018-github-app-repo-picker",
        )
        self.assertEqual(git_state_hook._strip_worktree_noise(once), once)

    def test_user_namespace_branches_stay_whole(self):
        # `rjha/scratch` is a user namespace, not a type prefix — the
        # whole branch remains the name, untouched by either the prefix
        # map or the worktree strip.
        for branch in ["rjha/scratch", "rjha/worktree-fix-1018-thing"]:
            with self.subTest(branch=branch):
                name, itype = git_state_hook._resolve_initiative(branch)
                self.assertEqual(name, branch)
                self.assertEqual(itype, "feature")

    # --- Recognized prefix with empty tail → fallback -------------------
    def test_recognized_prefix_with_empty_tail_falls_back(self):
        # `feat/` (trailing slash, no tail) shouldn't yield name="" —
        # that's a degenerate cluster key. Falls back to the unprefixed
        # rule: name=branch verbatim, type=feature.
        name, itype = git_state_hook._resolve_initiative("feat/")
        self.assertEqual(name, "feat/")
        self.assertEqual(itype, "feature")

    # --- Closed enum is enforced ----------------------------------------
    def test_type_is_always_from_closed_enum(self):
        # No matter what branch we hand in, the returned type must be
        # one of {feature, bugfix, refactor, infra, research}. This is
        # the dashboard's contract: no nulls, no unknown values.
        for branch in [
            None, "", "HEAD", "main", "master", "develop", "trunk",
            "feat/x", "fix/x", "refactor/x", "infra/x", "chore/x",
            "research/x", "spike/x", "feature/x", "bugfix/x",
            "perf/x", "cleanup/x", "test/x", "tests/x", "ci/x",
            "build/x", "deps/x", "docs/x", "doc/x",
            "worktree-fix-1018-thing", "worktree-fix-1018",
            "weird-branch", "Some/Weird-Path", "user/scratchpad",
        ]:
            with self.subTest(branch=branch):
                _name, itype = git_state_hook._resolve_initiative(branch)
                self.assertIn(itype, git_state_hook._INITIATIVE_TYPES)

    # --- Stability: same branch always → same (name, type) --------------
    def test_resolution_is_a_pure_function(self):
        # Same branch in → same (name, type) out. This is the property
        # that makes GROUP BY initiative_name cluster correctly across
        # machines, users, and time.
        for branch in [
            "feat/auth", "fix/crash", "main", "weird-thing",
            "perf/hot-path", "worktree-fix-1018-github-app-repo-picker",
        ]:
            with self.subTest(branch=branch):
                first = git_state_hook._resolve_initiative(branch)
                second = git_state_hook._resolve_initiative(branch)
                third = git_state_hook._resolve_initiative(branch)
                self.assertEqual(first, second)
                self.assertEqual(second, third)


# ---------------------------------------------------------------------------
# SessionStart nudge — initiative-convention.py
# ---------------------------------------------------------------------------
# v0.6.0 replaces the old "write a .cardinal-initiative file" hook with
# a "follow the branch-naming convention" hook. Same surface (SessionStart
# additionalContext), different content: the prompt now steers Claude's
# branch creation rather than its file authoring.

INITIATIVE_CONVENTION_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks" / "initiative-convention.py"
)


def _run_initiative_convention(cwd: Path, home: Path | None = None) -> subprocess.CompletedProcess:
    payload = json.dumps({
        "session_id": "sess-1",
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    env = os.environ.copy()
    # Hermetic HOME: the hook reads ~/.claude/cardinal.json for the
    # spend-limits standing fetch — never let a test see the developer's
    # real connection state (or hit the real network).
    env["HOME"] = str(home if home is not None else cwd)
    return subprocess.run(
        [sys.executable, str(INITIATIVE_CONVENTION_PATH)],
        input=payload, capture_output=True, text=True, timeout=10, env=env,
    )


class InitiativeConventionHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emits_convention_prompt_when_in_git_repo(self):
        _init_repo(self.root)
        res = _run_initiative_convention(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(
            body["hookSpecificOutput"]["hookEventName"], "SessionStart",
        )
        prompt = body["hookSpecificOutput"]["additionalContext"]
        # The convention's three load-bearing pieces must be present:
        # the type-prefix vocabulary, the kebab-name shape, and at
        # least one concrete example so Claude has something to pattern-
        # match against.
        for prefix in ["feat", "fix", "refactor", "infra", "chore", "research", "spike"]:
            self.assertIn(prefix, prompt)
        # The recognized-but-uncanonical prefixes are enumerated too, so
        # Claude knows e.g. `perf/...` still classifies cleanly.
        for prefix in ["perf", "cleanup", "tests", "ci", "build", "deps", "docs"]:
            self.assertIn(prefix, prompt)
        self.assertIn("kebab", prompt.lower())
        self.assertIn("feat/", prompt)  # at least one example branch

    def test_silent_when_not_a_git_repo(self):
        # No git init — the cwd is not inside a repo, so there's no
        # branch to advise on. Suppress the prompt to avoid wasted
        # context in non-code Claude sessions.
        res = _run_initiative_convention(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_fires_unconditionally_when_in_repo(self):
        # Unlike the old hook (which gated on absence of a file), the
        # convention hook fires every session in a git repo — Claude
        # needs the convention whether or not the repo has prior
        # branches matching it.
        _init_repo(self.root, branch="feat/already-following-convention")
        res = _run_initiative_convention(self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertIn("additionalContext", body["hookSpecificOutput"])


# ---------------------------------------------------------------------------
# Spend-limits delivery — _limits_common.py + limits-gate.py
# ---------------------------------------------------------------------------
# Two-hook split (conductor docs/specs/agent-spend-limits.md §Delivery):
# git-state.py's async fetch writes <session>.verdict.json; the sync
# limits-gate.py reads it and emits hook JSON. These tests pin the gate's
# channel mapping (notify/warn/block), the anti-nag hysteresis, the
# staleness fail-open windows, and the TTL-honoring refresh.

HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugins" / "cardinal" / "hooks"
LIMITS_COMMON_PATH = HOOKS_DIR / "_limits_common.py"
LIMITS_GATE_PATH = HOOKS_DIR / "limits-gate.py"


def _load_limits_common(home: Path):
    """Import _limits_common with HOME pointed at a temp dir. The module
    binds its paths from Path.home() at import time, so each test gets a
    fresh module object."""
    prior = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location(
            f"limits_common_{id(home)}", LIMITS_COMMON_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prior is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prior


def _write_verdict(home: Path, session_id: str, verdict: dict) -> None:
    path = home / ".claude" / "cardinal" / "limits" / f"{session_id}.verdict.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict))


def _run_gate(home: Path, session_id: str = "sess-1") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    payload = json.dumps({
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "do the thing",
    })
    return subprocess.run(
        [sys.executable, str(LIMITS_GATE_PATH)],
        input=payload, capture_output=True, text=True, timeout=10, env=env,
    )


def _warn_verdict(band: int = 90, **overrides) -> dict:
    v = {
        "decision": "warn",
        "band": band,
        "binding_scope": "initiative",
        "evaluations": [],
        "headline": "Initiative 'x' is at 90% ($45.10) of the $50 budget Priya M. set on it.",
        "agent_context": "[Cardinal spend status] Initiative 'x' is at 90%. Work economically.",
        "user_message": "Initiative 'x' is at 90% ($45.10) of the $50 budget Priya M. set on it.",
        "block_reason": "",
        "recommendation": {"action": "clear_context", "rationale": "cache re-reads"},
        "overridable": True,
        "ttl_seconds": 120,
        "fetched_at": time.time(),
    }
    v.update(overrides)
    return v


class LimitsGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_verdict_file_is_silent(self):
        res = _run_gate(self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_corrupt_verdict_file_is_silent(self):
        path = self.home / ".claude" / "cardinal" / "limits" / "sess-1.verdict.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        res = _run_gate(self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_warn_emits_both_channels_and_writes_ack(self):
        _write_verdict(self.home, "sess-1", _warn_verdict())
        res = _run_gate(self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # systemMessage: the human sees the standing + recommendation —
        # /clear and "split the PR" are human actions.
        self.assertIn("Priya M.", out["systemMessage"])
        # additionalContext: the model economizes within the session.
        self.assertEqual(
            out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        self.assertIn("Cardinal spend status", out["hookSpecificOutput"]["additionalContext"])
        ack = read_json(self.home / ".claude" / "cardinal" / "limits" / "sess-1.ack.json")
        self.assertEqual(ack.get("band"), 90)

    def test_hysteresis_same_band_speaks_once(self):
        _write_verdict(self.home, "sess-1", _warn_verdict())
        first = _run_gate(self.home)
        self.assertNotEqual(first.stdout, "")
        second = _run_gate(self.home)
        self.assertEqual(second.stdout, "", "same band must not re-surface")

    def test_hysteresis_rising_band_speaks_again(self):
        _write_verdict(self.home, "sess-1", _warn_verdict(band=75))
        _run_gate(self.home)
        _write_verdict(self.home, "sess-1", _warn_verdict(band=90))
        res = _run_gate(self.home)
        self.assertNotEqual(res.stdout, "", "band 75 → 90 crossing must surface")

    def test_notify_tier_is_context_only(self):
        # decision=allow with band>0 = a notify-action policy crossed a
        # threshold: the model hears about it, the user is not nagged.
        _write_verdict(self.home, "sess-1", _warn_verdict(decision="allow", band=75))
        res = _run_gate(self.home)
        out = json.loads(res.stdout)
        self.assertNotIn("systemMessage", out)
        self.assertIn("additionalContext", out["hookSpecificOutput"])

    def test_stale_warn_verdict_fails_open(self):
        _write_verdict(
            self.home, "sess-1",
            _warn_verdict(fetched_at=time.time() - 11 * 60),
        )
        res = _run_gate(self.home)
        self.assertEqual(res.stdout, "")

    def test_block_emits_decision_block_every_turn(self):
        _write_verdict(
            self.home, "sess-1",
            _warn_verdict(
                decision="block", band=100,
                block_reason="Spend limit reached: set by Priya M. Run /cardinal:override to continue.",
            ),
        )
        first = _run_gate(self.home)
        out = json.loads(first.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Priya M.", out["reason"])
        # No hysteresis for blocks — enforced every turn while in force.
        second = _run_gate(self.home)
        self.assertEqual(json.loads(second.stdout)["decision"], "block")

    def test_stale_block_fails_open_after_an_hour(self):
        _write_verdict(
            self.home, "sess-1",
            _warn_verdict(decision="block", band=100, block_reason="x",
                          fetched_at=time.time() - 61 * 60),
        )
        res = _run_gate(self.home)
        self.assertEqual(res.stdout, "")

    def test_override_file_downgrades_block_to_warn_tier(self):
        _write_verdict(
            self.home, "sess-1",
            _warn_verdict(decision="block", band=100, block_reason="halt"),
        )
        override = self.home / ".claude" / "cardinal" / "limits" / "sess-1.override.json"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(json.dumps({"overridden_at": time.time()}))
        res = _run_gate(self.home)
        out = json.loads(res.stdout)
        self.assertNotIn("decision", out)
        self.assertIn("systemMessage", out)


class LimitsCommonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_connected_state(self, status_url: str = "https://app.example.com/api/agent-limits/status"):
        claude = self.home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "cardinal.json").write_text(json.dumps({
            "limits": {"status_url": status_url, "enabled": True},
        }))
        (claude / "settings.json").write_text(json.dumps({
            "env": {"OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=sekrit"},
        }))

    def test_ingest_api_key_parses_otlp_headers(self):
        self._write_connected_state()
        lc = _load_limits_common(self.home)
        self.assertEqual(lc.ingest_api_key(), "sekrit")
        self.assertIsNone(lc.ingest_api_key({"OTEL_EXPORTER_OTLP_HEADERS": "other=x"}))

    def test_limits_config_absent_or_disabled_is_none(self):
        lc = _load_limits_common(self.home)
        self.assertIsNone(lc.limits_config())
        claude = self.home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "cardinal.json").write_text(json.dumps({
            "limits": {"status_url": "https://x", "enabled": False},
        }))
        self.assertIsNone(lc.limits_config())

    def test_maybe_refresh_honors_server_ttl(self):
        self._write_connected_state()
        lc = _load_limits_common(self.home)
        calls = []

        def fake_fetch(*args, **kwargs):
            calls.append(args)
            return {"decision": "allow", "band": 0, "ttl_seconds": 9999}

        lc.fetch_status = fake_fetch
        first = lc.maybe_refresh_verdict("sess-1", repo="o/r", branch="feat/x")
        self.assertEqual(len(calls), 1)
        self.assertIn("fetched_at", first)
        # Within TTL: served from the file, no second fetch.
        lc.maybe_refresh_verdict("sess-1", repo="o/r", branch="feat/x")
        self.assertEqual(len(calls), 1)
        # force=True bypasses the TTL (SessionStart warm fetch).
        lc.maybe_refresh_verdict("sess-1", repo="o/r", branch="feat/x", force=True)
        self.assertEqual(len(calls), 2)

    def test_maybe_refresh_noop_without_limits_config(self):
        lc = _load_limits_common(self.home)
        lc.fetch_status = lambda *a, **k: self.fail("must not fetch when unconfigured")
        self.assertIsNone(lc.maybe_refresh_verdict("sess-1", repo=None, branch=None))

    def test_fetch_failure_keeps_prior_verdict(self):
        self._write_connected_state()
        lc = _load_limits_common(self.home)
        lc.fetch_status = lambda *a, **k: {"decision": "warn", "band": 90, "ttl_seconds": 0}
        first = lc.maybe_refresh_verdict("sess-1", repo=None, branch=None)
        self.assertEqual(first["band"], 90)
        lc.fetch_status = lambda *a, **k: None  # network down; ttl 0 forces refetch
        kept = lc.maybe_refresh_verdict("sess-1", repo=None, branch=None)
        self.assertEqual(kept["band"], 90, "failed refresh must keep the prior verdict")

    def test_standing_lines_formats_evaluations(self):
        lc = _load_limits_common(self.home)
        lines = lc.standing_lines({
            "evaluations": [
                {"scope": "engineer", "window": "week", "spent_usd": 142, "limit_usd": 200,
                 "fraction": 0.71,
                 "set_by": {"email": "p@x.io", "display_name": "Priya M.", "self": False, "targeted": False}},
                {"scope": "session", "window": "lifetime", "spent_usd": 3.2, "limit_usd": 10,
                 "fraction": 0.32,
                 "set_by": {"email": "me@x.io", "display_name": "Me", "self": True, "targeted": True}},
            ],
        })
        self.assertEqual(len(lines), 2)
        self.assertIn("engineer (week): $142.00 of $200.00 (71%) — set by Priya M.", lines[0])
        self.assertIn("set by you", lines[1])


class LimitsConnectAndSessionStartTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_connect_persists_limits_block(self):
        res = run_plugin(CONNECT, ["--host", self.stub.url()], self.home, timeout=15)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        state = read_json(self.home / ".claude" / "cardinal.json")
        self.assertEqual(
            state.get("limits", {}).get("status_url"),
            f"{self.stub.url()}/api/agent-limits/status",
        )
        self.assertTrue(state["limits"]["enabled"])

    def test_session_start_injects_budget_standing(self):
        # Wire the temp HOME as a connected install pointing at the stub,
        # then verify the convention prompt carries the standing block AND
        # the verdict file is warm-written for the per-turn gate.
        claude = self.home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "cardinal.json").write_text(json.dumps({
            "limits": {
                "status_url": f"{self.stub.url()}/api/agent-limits/status",
                "enabled": True,
            },
        }))
        (claude / "settings.json").write_text(json.dumps({
            "env": {"OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=sekrit"},
        }))
        self.stub.limits_verdict = {
            "decision": "allow",
            "band": 0,
            "evaluations": [
                {"scope": "engineer", "window": "week", "spent_usd": 142, "limit_usd": 200,
                 "fraction": 0.71,
                 "set_by": {"email": "p@x.io", "display_name": "Priya M.", "self": False,
                            "targeted": False}},
            ],
            "user_message": "",
            "ttl_seconds": 120,
        }
        repo = self.home / "work"
        repo.mkdir()
        _init_repo(repo, branch="feat/limits")

        res = _run_initiative_convention(repo, home=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        ctx = body["hookSpecificOutput"]["additionalContext"]
        self.assertIn("kebab", ctx.lower())  # convention prompt intact
        self.assertIn("Cardinal spend budgets", ctx)
        self.assertIn("Priya M.", ctx)
        self.assertEqual(self.stub.limits_calls, 1)
        verdict_file = claude / "cardinal" / "limits" / "sess-1.verdict.json"
        self.assertTrue(verdict_file.exists(), "SessionStart must warm-write the verdict file")

    def test_session_start_standing_failure_keeps_convention_prompt(self):
        claude = self.home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "cardinal.json").write_text(json.dumps({
            "limits": {
                "status_url": f"{self.stub.url()}/api/agent-limits/status",
                "enabled": True,
            },
        }))
        (claude / "settings.json").write_text(json.dumps({
            "env": {"OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=sekrit"},
        }))
        self.stub.limits_verdict = None  # endpoint 404s (older maestro)
        repo = self.home / "work"
        repo.mkdir()
        _init_repo(repo)

        res = _run_initiative_convention(repo, home=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        ctx = body["hookSpecificOutput"]["additionalContext"]
        self.assertIn("kebab", ctx.lower())
        self.assertNotIn("Cardinal spend budgets", ctx)


class CommandDetectionTests(unittest.TestCase):
    """Pure-function tests for git_state_hook._detect_command(prompt) —
    the slash-command stamp behind cardinal.command (v0.8.0, see
    docs/skill-command-telemetry.md). Name only, never args; raw and
    <command-name>-wrapped payload shapes both accepted."""

    def test_table_cases(self):
        cases = [
            # raw typed commands
            ("/code-review", "code-review"),
            ("/code-review --fix high", "code-review"),
            ("  /verify", "verify"),
            ("/model claude-fable-5", "model"),
            # namespaced (plugin:command) passes through verbatim
            ("/commit-commands:commit-push-pr now", "commit-commands:commit-push-pr"),
            # expanded <command-name> form
            ("<command-name>/deep-research</command-name> args follow", "deep-research"),
            ("<command-name>loop</command-name>", "loop"),
            # non-commands
            ("fix the /etc/hosts parser", None),
            ("please run /code-review for me", None),
            ("plain prompt", None),
            ("", None),
            (None, None),
            # a bare slash is not a command
            ("/", None),
            ("/ leading space", None),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(git_state_hook._detect_command(prompt), expected)

    def test_args_never_leak(self):
        """The detected value is the NAME alone — args can carry sensitive
        free text and must never reach the wire."""
        got = git_state_hook._detect_command("/deep-research acme corp acquisition plans")
        self.assertEqual(got, "deep-research")

    def test_attribute_emitted_only_for_commands(self):
        """End-to-end shape check on the log-record attribute list: the
        cardinal.command kv appears exactly when the prompt is a command,
        mirroring how cardinal.initiative.name is conditionally emitted."""
        for prompt, expect_attr in [("/verify", True), ("do the thing", False)]:
            with self.subTest(prompt=prompt):
                command = git_state_hook._detect_command(prompt)
                attrs = [
                    *(
                        [git_state_hook._kv("cardinal.command", command)]
                        if command
                        else []
                    ),
                ]
                names = [a["key"] for a in attrs]
                if expect_attr:
                    self.assertEqual(names, ["cardinal.command"])
                    self.assertEqual(
                        attrs[0]["value"]["stringValue"], "verify"
                    )
                else:
                    self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main()
