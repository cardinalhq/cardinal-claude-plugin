"""Tests for hooks/turn-usage.py (plugin v0.10, Stop hook →
cardinal.turn_usage + cardinal.turn_tool OTLP events).

Each test runs the hook as a subprocess with HOME pointed at a temp dir
whose .claude/settings.json routes OTLP to a local stub server, and a
fabricated main-session transcript at <tmp>/proj/<session_id>.jsonl.

Run with: python3 -m unittest tests.test_turn_usage -v
"""

import json
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

HOOK = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "cardinal" / "hooks" / "turn-usage.py"
)


class _OTLPStub(BaseHTTPRequestHandler):
    received: list[dict] = []
    delay_s: float = 0.0

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(json.loads(body))
        if type(self).delay_s > 0:
            time.sleep(type(self).delay_s)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


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
    return out


def _records_by_event(event_body: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for rec in _log_records(event_body):
        attrs = _attrs_of(rec)
        name = attrs.get("event_name", "")
        grouped.setdefault(name, []).append(attrs)
    return grouped


def _assistant_msg(usage: dict, content: list | None = None, model: str = "claude-opus-4-7") -> dict:
    msg: dict = {"role": "assistant", "model": model, "usage": usage}
    if content is not None:
        msg["content"] = content
    return {"type": "assistant", "message": msg}


def _user_text_msg(text: str = "hi") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _user_tool_result_msg(tool_use_id: str = "tu1") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
    ]}}


def _tool_use_block(name: str, input_: dict, block_id: str = "tu1") -> dict:
    return {"type": "tool_use", "id": block_id, "name": name, "input": input_}


class TurnUsageHookTest(unittest.TestCase):
    def setUp(self):
        _OTLPStub.received = []
        _OTLPStub.delay_s = 0.0
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

    def _write_transcript(self, session_id: str, records: list[dict]) -> Path:
        proj = self.home / "proj"
        proj.mkdir(exist_ok=True)
        path = proj / f"{session_id}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def _run_hook(self, payload: dict, expect_rc: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(payload).encode(),
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr.decode())
        return proc

    def test_sums_usage_records_across_model_calls(self):
        path = self._write_transcript("sess-1", [
            _user_text_msg("go"),
            _assistant_msg({"input_tokens": 100, "cache_creation_input_tokens": 200,
                            "cache_read_input_tokens": 5000, "output_tokens": 50}),
            _assistant_msg({"input_tokens": 110, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 6000, "output_tokens": 60}),
            _assistant_msg({"input_tokens": 120, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 200, "output_tokens": 70}),
        ])
        self._run_hook({"session_id": "sess-1", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 1)
        by_event = _records_by_event(_OTLPStub.received[0])
        usages = by_event.get("cardinal.turn_usage", [])
        self.assertEqual(len(usages), 3)
        self.assertEqual([u["turn_seq"] for u in usages], [0, 1, 2])
        self.assertEqual([u["cache_read_input_tokens"] for u in usages], [5000, 6000, 200])
        self.assertEqual(usages[0]["model"], "claude-opus-4-7")

    def test_tool_use_records_link_to_parent_turn_seq(self):
        path = self._write_transcript("sess-2", [
            _user_text_msg(),
            _assistant_msg(
                {"input_tokens": 10, "output_tokens": 5},
                content=[
                    {"type": "text", "text": "reading"},
                    _tool_use_block("Read", {"file_path": "src/foo.ts"}, "t1"),
                    _tool_use_block("Edit", {"file_path": "src/foo.ts"}, "t2"),
                ],
            ),
        ])
        self._run_hook({"session_id": "sess-2", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event.get("cardinal.turn_tool", [])
        self.assertEqual(len(tools), 2)
        self.assertEqual([t["tool_name"] for t in tools], ["Read", "Edit"])
        self.assertEqual([t["tool_seq"] for t in tools], [0, 1])
        self.assertEqual({t["turn_seq"] for t in tools}, {0})
        self.assertEqual([t["target"] for t in tools], ["src/foo.ts", "src/foo.ts"])

    def test_tool_target_omitted_for_bash_and_grep(self):
        path = self._write_transcript("sess-3", [
            _user_text_msg(),
            _assistant_msg(
                {"input_tokens": 1, "output_tokens": 1},
                content=[
                    _tool_use_block("Bash", {"command": "ls -la /tmp"}, "t1"),
                    _tool_use_block("Grep", {"pattern": "SECRET_TOKEN"}, "t2"),
                ],
            ),
        ])
        self._run_hook({"session_id": "sess-3", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event.get("cardinal.turn_tool", [])
        self.assertEqual(len(tools), 2)
        for t in tools:
            self.assertNotIn("target", t, f"{t['tool_name']} must not emit target")

    def test_user_boundary_excludes_prior_turn(self):
        path = self._write_transcript("sess-4", [
            _user_text_msg("first prompt"),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1}),
            _user_text_msg("second prompt"),
            _assistant_msg({"input_tokens": 2, "output_tokens": 2}),
        ])
        self._run_hook({"session_id": "sess-4", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        usages = by_event.get("cardinal.turn_usage", [])
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["input_tokens"], 2)

    def test_tool_result_user_messages_are_not_treated_as_boundary(self):
        path = self._write_transcript("sess-5", [
            _user_text_msg("start"),
            _assistant_msg(
                {"input_tokens": 1, "output_tokens": 1},
                content=[_tool_use_block("Read", {"file_path": "a.ts"}, "t1")],
            ),
            _user_tool_result_msg("t1"),
            _assistant_msg({"input_tokens": 2, "output_tokens": 2}),
        ])
        self._run_hook({"session_id": "sess-5", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        usages = by_event.get("cardinal.turn_usage", [])
        tools = by_event.get("cardinal.turn_tool", [])
        # Both assistant model calls belong to the same user turn (the
        # tool_result user message is loop continuation, not a boundary).
        self.assertEqual(len(usages), 2)
        self.assertEqual([u["turn_seq"] for u in usages], [0, 1])
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["turn_seq"], 0)

    def test_truncates_above_cap(self):
        records = [_user_text_msg()]
        for i in range(100):
            records.append(_assistant_msg(
                {"input_tokens": i, "output_tokens": 1, "cache_read_input_tokens": i},
            ))
        path = self._write_transcript("sess-6", records)
        self._run_hook({"session_id": "sess-6", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        usages = by_event.get("cardinal.turn_usage", [])
        self.assertEqual(len(usages), 64)
        # Truncation flag rides the last usage record so consumers fail
        # loud rather than treat partial as complete.
        self.assertTrue(usages[-1].get("truncated"))
        # Earlier records are NOT flagged.
        self.assertNotIn("truncated", usages[0])

    def test_missing_transcript_silent_exit(self):
        self._run_hook({"session_id": "sess-7"})
        self.assertEqual(len(_OTLPStub.received), 0)

    def test_no_endpoint_silent_exit(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {}}))
        path = self._write_transcript("sess-8", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1}),
        ])
        self._run_hook({"session_id": "sess-8", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 0)

    def test_api_key_header_sent(self):
        captured = {}
        orig = _OTLPStub.do_POST

        def capture(handler):
            captured["key"] = handler.headers.get("x-cardinalhq-api-key")
            orig(handler)

        _OTLPStub.do_POST = capture
        try:
            path = self._write_transcript("sess-9", [
                _user_text_msg(),
                _assistant_msg({"input_tokens": 1, "output_tokens": 1}),
            ])
            self._run_hook({"session_id": "sess-9", "transcript_path": str(path)})
            self.assertEqual(captured.get("key"), "test-key")
        finally:
            _OTLPStub.do_POST = orig

    def test_chaos_lakerunner_slow_does_not_block(self):
        # Stub delays 5s after capturing the POST; hook timeout is 2s so
        # urlopen raises and we still exit 0 within a few seconds. Proves
        # a slow lakerunner cannot stretch the async hook indefinitely.
        _OTLPStub.delay_s = 5.0
        path = self._write_transcript("sess-10", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1}),
        ])
        start = time.monotonic()
        self._run_hook({"session_id": "sess-10", "transcript_path": str(path)})
        elapsed = time.monotonic() - start
        # 2s hook timeout + ~1s subprocess overhead headroom.
        self.assertLess(elapsed, 4.5, f"hook hung for {elapsed:.2f}s under slow lakerunner")

    def test_no_assistant_records_silent_exit(self):
        # Stop fires but the slice has nothing emittable (e.g. truncated
        # transcript). Don't emit an empty POST.
        path = self._write_transcript("sess-11", [_user_text_msg()])
        self._run_hook({"session_id": "sess-11", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 0)


if __name__ == "__main__":
    unittest.main()
