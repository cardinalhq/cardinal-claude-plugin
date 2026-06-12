"""Tests for hooks/subagent-usage.py (plugin v0.9, PostToolUse on
Agent|Task → cardinal.subagent_usage OTLP event).

Each test runs the hook as a subprocess with HOME pointed at a temp dir
whose .claude/settings.json routes OTLP to a local stub server, and a
fabricated transcript tree:

    <tmp>/proj/<session_id>.jsonl                      (parent, unused)
    <tmp>/proj/<session_id>/subagents/agent-<id>.jsonl (per-request usage)

Run with: python3 -m unittest tests.test_subagent_usage -v
"""

import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

HOOK = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks" / "subagent-usage.py"
)


class _OTLPStub(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _attrs_of(event_body: dict) -> dict[str, str]:
    recs = event_body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    return {
        kv["key"]: kv["value"]["stringValue"]
        for kv in recs[0]["attributes"]
    }


class SubagentUsageHookTest(unittest.TestCase):
    def setUp(self):
        _OTLPStub.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({
            "env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self.server.server_port}",
                "OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=test-key",
                "OTEL_RESOURCE_ATTRIBUTES": "user.email=t@example.com",
            }
        }))

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def _make_transcripts(self, session_id: str, agent_id: str, usages: list[dict]) -> Path:
        proj = self.home / "proj"
        sub = proj / session_id / "subagents"
        sub.mkdir(parents=True)
        parent = proj / f"{session_id}.jsonl"
        parent.write_text("")
        lines = []
        # Noise records without usage must be skipped, not crash.
        lines.append(json.dumps({"type": "user", "message": {"role": "user"}}))
        for u in usages:
            lines.append(json.dumps({"message": {"role": "assistant", "usage": u}}))
        (sub / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
        return parent

    def _run_hook(self, payload: dict) -> None:
        proc = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(payload).encode(),
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_sums_transcript_usage_into_total_tokens(self):
        parent = self._make_transcripts("sess-1", "abc123", [
            {"input_tokens": 5, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 1000, "output_tokens": 20},
            {"input_tokens": 1, "cache_creation_input_tokens": 50,
             "cache_read_input_tokens": 2000, "output_tokens": 30},
        ])
        self._run_hook({
            "session_id": "sess-1",
            "transcript_path": str(parent),
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore"},
            "tool_response": {
                "agentId": "abc123", "agentType": "Explore",
                "totalTokens": 3081, "totalToolUseCount": 7,
                "totalDurationMs": 4500,
            },
        })
        self.assertEqual(len(_OTLPStub.received), 1)
        attrs = _attrs_of(_OTLPStub.received[0])
        self.assertEqual(attrs["event_name"], "cardinal.subagent_usage")
        self.assertEqual(attrs["session_id"], "sess-1")
        self.assertEqual(attrs["subagent_type"], "Explore")
        self.assertEqual(attrs["agent_id"], "abc123")
        # worked = (5+100+20) + (1+50+30) = 206; cache_read = 3000
        self.assertEqual(attrs["total_tokens"], "206")
        self.assertEqual(attrs["subagent_cache_read_tokens"], "3000")
        self.assertEqual(attrs["subagent_request_count"], "2")
        self.assertEqual(attrs["final_context_tokens"], "3081")
        self.assertEqual(attrs["subagent_tool_use_count"], "7")
        self.assertEqual(attrs["subagent_duration_ms"], "4500")

    def test_missing_transcript_emits_without_total_tokens(self):
        proj = self.home / "proj"
        proj.mkdir()
        parent = proj / "sess-2.jsonl"
        parent.write_text("")
        self._run_hook({
            "session_id": "sess-2",
            "transcript_path": str(parent),
            "tool_name": "Task",
            "tool_input": {},
            "tool_response": {"agentId": "missing", "totalTokens": 999},
        })
        self.assertEqual(len(_OTLPStub.received), 1)
        attrs = _attrs_of(_OTLPStub.received[0])
        # One semantics per field: no transcript → no total_tokens, the
        # processor skips subtok; footprint still reported honestly.
        self.assertNotIn("total_tokens", attrs)
        self.assertEqual(attrs["final_context_tokens"], "999")
        self.assertEqual(attrs["subagent_type"], "general-purpose")

    def test_non_agent_tool_is_ignored(self):
        self._run_hook({
            "session_id": "sess-3",
            "tool_name": "Bash",
            "tool_response": {},
        })
        self.assertEqual(len(_OTLPStub.received), 0)

    def test_no_endpoint_is_silent(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {}}))
        self._run_hook({
            "session_id": "sess-4",
            "tool_name": "Agent",
            "tool_response": {"agentId": "x"},
        })
        self.assertEqual(len(_OTLPStub.received), 0)

    def test_api_key_header_is_sent(self):
        captured = {}
        orig = _OTLPStub.do_POST

        def capture(handler):
            captured["key"] = handler.headers.get("x-cardinalhq-api-key")
            orig(handler)

        _OTLPStub.do_POST = capture
        try:
            parent = self._make_transcripts("sess-5", "k1", [
                {"input_tokens": 1, "output_tokens": 1},
            ])
            self._run_hook({
                "session_id": "sess-5",
                "transcript_path": str(parent),
                "tool_name": "Agent",
                "tool_response": {"agentId": "k1"},
            })
            self.assertEqual(captured.get("key"), "test-key")
        finally:
            _OTLPStub.do_POST = orig


if __name__ == "__main__":
    unittest.main()
