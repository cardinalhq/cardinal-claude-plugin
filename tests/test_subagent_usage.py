"""Tests for hooks/subagent-usage.py (plugin v0.12, PostToolUse on
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
        return self._make_transcripts_raw(session_id, agent_id, [
            {"message": {"role": "assistant", "usage": u}} for u in usages
        ])

    def _make_transcripts_raw(self, session_id: str, agent_id: str, records: list[dict]) -> Path:
        proj = self.home / "proj"
        sub = proj / session_id / "subagents"
        sub.mkdir(parents=True)
        parent = proj / f"{session_id}.jsonl"
        parent.write_text("")
        lines = []
        # Noise records without usage must be skipped, not crash.
        lines.append(json.dumps({"type": "user", "message": {"role": "user"}}))
        lines.extend(json.dumps(r) for r in records)
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

    def test_component_sums_equal_total_tokens_with_missing_keys(self):
        # Invariant (spec §Field 1): the three component fields sum
        # exactly to total_tokens, including when usage records omit
        # keys entirely.
        parent = self._make_transcripts("sess-c1", "c1", [
            {"input_tokens": 5, "cache_creation_input_tokens": 100,
             "output_tokens": 20},
            {"output_tokens": 30},   # no input/cache keys at all
            {"input_tokens": 7, "cache_read_input_tokens": 500},
        ])
        self._run_hook({
            "session_id": "sess-c1",
            "transcript_path": str(parent),
            "tool_name": "Agent",
            "tool_response": {"agentId": "c1", "agentType": "Explore"},
        })
        attrs = _attrs_of(_OTLPStub.received[0])
        self.assertEqual(attrs["subagent_input_tokens"], "12")
        self.assertEqual(attrs["subagent_output_tokens"], "50")
        self.assertEqual(attrs["subagent_cache_creation_tokens"], "100")
        self.assertEqual(
            int(attrs["subagent_input_tokens"])
            + int(attrs["subagent_output_tokens"])
            + int(attrs["subagent_cache_creation_tokens"]),
            int(attrs["total_tokens"]),
        )
        # No model on any record → dominant-model fields omitted, not
        # guessed (one semantics per field).
        self.assertNotIn("subagent_model", attrs)
        self.assertNotIn("subagent_model_count", attrs)

    def test_mixed_model_transcript_dominant_by_worked_tokens(self):
        parent = self._make_transcripts_raw("sess-c2", "c2", [
            {"message": {"role": "assistant", "model": "claude-sonnet-5",
                         "usage": {"input_tokens": 10, "output_tokens": 10}}},
            {"message": {"role": "assistant", "model": "claude-opus-4-7",
                         "usage": {"input_tokens": 500,
                                   "cache_creation_input_tokens": 400,
                                   "output_tokens": 100}}},
            {"message": {"role": "assistant", "model": "claude-sonnet-5",
                         "usage": {"input_tokens": 20, "output_tokens": 20}}},
        ])
        self._run_hook({
            "session_id": "sess-c2",
            "transcript_path": str(parent),
            "tool_name": "Agent",
            "tool_response": {"agentId": "c2"},
        })
        attrs = _attrs_of(_OTLPStub.received[0])
        # opus worked 1000 vs sonnet 60 → dominant by worked tokens.
        self.assertEqual(attrs["subagent_model"], "claude-opus-4-7")
        self.assertEqual(attrs["subagent_model_count"], "2")

    def test_tool_counts_histogram_includes_mcp_names(self):
        mcp = "mcp__cardinal__lakerunner__execute_logs_query"
        parent = self._make_transcripts_raw("sess-c3", "c3", [
            {"message": {"role": "assistant",
                         "usage": {"input_tokens": 1, "output_tokens": 1},
                         "content": [
                             {"type": "text", "text": "looking"},
                             {"type": "tool_use", "name": mcp, "input": {"q": "x"}},
                             {"type": "tool_use", "name": mcp, "input": {"q": "y"}},
                             {"type": "tool_use", "name": "Read",
                              "input": {"file_path": "a.ts"}},
                         ]}},
            # Assistant record without usage still contributes tool names.
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": mcp, "input": {"q": "z"}},
            ]}},
        ])
        self._run_hook({
            "session_id": "sess-c3",
            "transcript_path": str(parent),
            "tool_name": "Agent",
            "tool_response": {"agentId": "c3"},
        })
        attrs = _attrs_of(_OTLPStub.received[0])
        self.assertEqual(
            json.loads(attrs["subagent_tool_counts"]),
            {mcp: 3, "Read": 1},
        )
        self.assertNotIn("subagent_tool_counts_truncated", attrs)

    def test_tool_counts_capped_at_32_with_truncation_flag(self):
        # 40 distinct names: the first 32 appear twice, the last 8 once —
        # the cap must keep the 32 most frequent and flag the truncation.
        content = []
        for i in range(40):
            reps = 2 if i < 32 else 1
            for _ in range(reps):
                content.append({"type": "tool_use", "name": f"Tool{i:02d}",
                                "input": {}})
        parent = self._make_transcripts_raw("sess-c4", "c4", [
            {"message": {"role": "assistant",
                         "usage": {"input_tokens": 1, "output_tokens": 1},
                         "content": content}},
        ])
        self._run_hook({
            "session_id": "sess-c4",
            "transcript_path": str(parent),
            "tool_name": "Agent",
            "tool_response": {"agentId": "c4"},
        })
        attrs = _attrs_of(_OTLPStub.received[0])
        counts = json.loads(attrs["subagent_tool_counts"])
        self.assertEqual(len(counts), 32)
        self.assertEqual(set(counts), {f"Tool{i:02d}" for i in range(32)})
        self.assertEqual(attrs["subagent_tool_counts_truncated"], "true")

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
        self.assertNotIn("subagent_input_tokens", attrs)
        self.assertNotIn("subagent_tool_counts", attrs)
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
