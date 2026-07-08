"""Tests for hooks/turn-usage.py (plugin v0.12, Stop hook →
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


def _all_records_by_event(event_bodies: list[dict]) -> dict[str, list[dict]]:
    """Like _records_by_event but folds every received POST — chunked
    emission spreads one firing across several bodies."""
    grouped: dict[str, list[dict]] = {}
    for body in event_bodies:
        for name, attrs_list in _records_by_event(body).items():
            grouped.setdefault(name, []).extend(attrs_list)
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

    def test_long_turn_chunks_into_multiple_posts_no_truncation(self):
        # 300 tools + 1 usage = 301 records → two POSTs (256 + 45) with
        # CONTIGUOUS 1-ns offsets across the batch boundary (chq_tsns PK
        # uniqueness) and no truncated flag — the data used to be
        # dropped past the old 256-tool cap; now it all lands.
        mcp = "mcp__cardinal__lakerunner__execute_logs_query"
        tool_blocks = [
            _tool_use_block(mcp, {"q": str(j)}, f"t{j}") for j in range(300)
        ]
        path = self._write_transcript("sess-6", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1},
                           content=tool_blocks),
        ])
        self._run_hook({"session_id": "sess-6", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 2)
        self.assertEqual(len(_log_records(_OTLPStub.received[0])), 256)
        self.assertEqual(len(_log_records(_OTLPStub.received[1])), 45)
        ts_values = [
            int(r["timeUnixNano"])
            for body in _OTLPStub.received
            for r in _log_records(body)
        ]
        self.assertEqual(
            ts_values,
            list(range(ts_values[0], ts_values[0] + 301)),
            "offsets must continue across batches without gap or reset",
        )
        by_event = _all_records_by_event(_OTLPStub.received)
        self.assertEqual(len(by_event["cardinal.turn_tool"]), 300)
        for u in by_event["cardinal.turn_usage"]:
            self.assertNotIn("truncated", u)

    def test_pathological_turn_hits_ceiling_and_flags_truncated(self):
        # 5,000-tool turn → the 4,096-record absolute ceiling applies
        # (1 usage + 4,095 tools) with truncated=true on the usage
        # record, spread over 16 POSTs of ≤256.
        tool_blocks = [
            _tool_use_block("Read", {"file_path": f"f{j}.ts"}, f"t{j}")
            for j in range(5000)
        ]
        path = self._write_transcript("sess-6b", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1},
                           content=tool_blocks),
        ])
        self._run_hook({"session_id": "sess-6b", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 16)
        total_records = sum(
            len(_log_records(body)) for body in _OTLPStub.received
        )
        self.assertEqual(total_records, 4096)
        by_event = _all_records_by_event(_OTLPStub.received)
        usages = by_event["cardinal.turn_usage"]
        self.assertEqual(len(usages), 1)
        self.assertEqual(len(by_event["cardinal.turn_tool"]), 4095)
        self.assertTrue(usages[-1].get("truncated"))

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

    def test_notebookedit_target_uses_notebook_path(self):
        # NotebookEdit's tool schema uses `notebook_path`, not `file_path`.
        # C3 promote-to-CLAUDE.md must see the notebook target so users
        # who reference the same notebook across sessions get a hit.
        path = self._write_transcript("sess-12", [
            _user_text_msg(),
            _assistant_msg(
                {"input_tokens": 1, "output_tokens": 1},
                content=[
                    _tool_use_block(
                        "NotebookEdit",
                        {"notebook_path": "nb/foo.ipynb",
                         "cell_id": "c1", "new_source": "..."},
                        "t1",
                    ),
                ],
            ),
        ])
        self._run_hook({"session_id": "sess-12", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event.get("cardinal.turn_tool", [])
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_name"], "NotebookEdit")
        self.assertEqual(tools[0]["target"], "nb/foo.ipynb")

    def test_ceiling_truncation_stops_usage_emission(self):
        # 3 assistants × 2,500 tools; the ceiling lands mid-way through
        # the second assistant's tool stream, and the third must NOT
        # emit turn_usage either — one truncation point, one flag, one
        # meaning ("everything past this point dropped").
        records = [_user_text_msg()]
        for i in range(3):
            tool_blocks = [
                _tool_use_block(
                    "Read",
                    {"file_path": f"a{i}-{j}.ts"},
                    f"t{i}-{j}",
                )
                for j in range(2500)
            ]
            records.append(_assistant_msg(
                {"input_tokens": i, "output_tokens": 1,
                 "cache_read_input_tokens": i},
                content=tool_blocks,
            ))
        path = self._write_transcript("sess-13", records)
        self._run_hook({"session_id": "sess-13", "transcript_path": str(path)})
        by_event = _all_records_by_event(_OTLPStub.received)
        usages = by_event.get("cardinal.turn_usage", [])
        tools = by_event.get("cardinal.turn_tool", [])
        self.assertEqual(len(usages), 2, "usage emission must stop at the ceiling break")
        # 4,096 total records − 2 usages = 4,094 tools.
        self.assertEqual(len(tools), 4094)
        self.assertTrue(usages[-1].get("truncated"))
        # Earlier usage records are not flagged.
        self.assertNotIn("truncated", usages[0])

    def test_malformed_utf8_transcript_silent_exit(self):
        # A non-UTF-8 byte sequence in the transcript must not break the
        # 'silent exit on any failure' contract. open(encoding='utf-8')
        # raises UnicodeDecodeError, which _walk_current_turn must catch.
        proj = self.home / "proj"
        proj.mkdir(exist_ok=True)
        path = proj / "sess-14.jsonl"
        path.write_bytes(
            b'{"type":"user","message":{"role":"user","content":"x"}}\n'
            b"\xff\xfe\xfd\n"
        )
        self._run_hook({"session_id": "sess-14", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 0)


    def test_user_turn_seq_matches_real_user_message_count(self):
        # Third real user turn → every emitted record (usage AND tool)
        # carries user_turn_seq=3, giving the harvester the
        # (user_turn_seq, turn_seq, tool_seq) total order.
        path = self._write_transcript("sess-15", [
            _user_text_msg("first"),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1}),
            _user_text_msg("second"),
            _assistant_msg({"input_tokens": 2, "output_tokens": 2}),
            _user_text_msg("third"),
            _assistant_msg(
                {"input_tokens": 3, "output_tokens": 3},
                content=[_tool_use_block("Read", {"file_path": "a.ts"}, "t1")],
            ),
        ])
        self._run_hook({"session_id": "sess-15", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        for attrs in (by_event["cardinal.turn_usage"]
                      + by_event["cardinal.turn_tool"]):
            self.assertEqual(attrs["user_turn_seq"], 3)

    def test_user_turn_seq_ignores_tool_result_continuations(self):
        # tool_result-only user messages are loop continuation, not a
        # turn boundary — they must not increment user_turn_seq.
        path = self._write_transcript("sess-16", [
            _user_text_msg("start"),
            _assistant_msg(
                {"input_tokens": 1, "output_tokens": 1},
                content=[_tool_use_block("Read", {"file_path": "a.ts"}, "t1")],
            ),
            _user_tool_result_msg("t1"),
            _assistant_msg({"input_tokens": 2, "output_tokens": 2}),
        ])
        self._run_hook({"session_id": "sess-16", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        usages = by_event["cardinal.turn_usage"]
        tools = by_event["cardinal.turn_tool"]
        self.assertEqual(len(usages), 2)
        for attrs in usages + tools:
            self.assertEqual(attrs["user_turn_seq"], 1)

    def test_bash_class_table(self):
        # Table-driven: one Bash tool_use per case; class is derived from
        # the command word only and the command string is never emitted.
        cases = [
            ("git status", "git-read"),
            ("git log --oneline -5", "git-read"),
            ("git commit -m msg", "git-write"),
            ("git push origin main", "git-write"),
            ("git checkout -b feat/x", "git-write"),
            ("pytest tests/ -v", "test"),
            ("go test ./...", "test"),
            ("npm test", "test"),
            ("cargo test", "test"),
            ("make -j4", "build"),
            ("go build ./...", "build"),
            ("npm run build", "build"),
            ("tsc --noEmit", "build"),
            ("pip install requests", "pkg"),
            ("npm i lodash", "pkg"),
            ("brew install jq", "pkg"),
            ("cargo add serde", "pkg"),
            ("ls -la sub/dir", "file-read"),
            ("cat notes.txt", "file-read"),
            ("grep -rn pattern .", "file-read"),
            ("rm -rf build/", "file-write"),
            ("sed -i s/a/b/ f.txt", "file-write"),
            ("curl -s https://example.com", "network"),
            ("gh pr view 12", "network"),
            ("python3 script.py", "other"),
            # Env-var prefix and sudo are stripped before lookup.
            ("FOO=bar sudo make install", "build"),
            ("/usr/bin/git status", "git-read"),
        ]
        blocks = [
            _tool_use_block("Bash", {"command": cmd}, f"t{i}")
            for i, (cmd, _) in enumerate(cases)
        ]
        path = self._write_transcript("sess-17", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1},
                           content=blocks),
        ])
        self._run_hook({"session_id": "sess-17", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event["cardinal.turn_tool"]
        self.assertEqual(len(tools), len(cases))
        for (cmd, expected), attrs in zip(cases, tools):
            self.assertEqual(
                attrs.get("bash_class"), expected,
                f"command {cmd!r} expected {expected}",
            )
            # Single-segment commands never set bash_multi.
            self.assertNotIn("bash_multi", attrs)

    def test_bash_compound_command_most_write_risky_wins(self):
        cases = [
            # {git-read, file-read} → git-read outranks file-read.
            ("git diff | head -50", "git-read", True),
            # {file-write, git-write} → file-write is riskiest.
            ("mkdir -p x && git commit -m y", "file-write", True),
            # Same class on both sides → no bash_multi.
            ("ls -la; cat f.txt", "file-read", False),
        ]
        blocks = [
            _tool_use_block("Bash", {"command": cmd}, f"t{i}")
            for i, (cmd, _, _) in enumerate(cases)
        ]
        path = self._write_transcript("sess-18", [
            _user_text_msg(),
            _assistant_msg({"input_tokens": 1, "output_tokens": 1},
                           content=blocks),
        ])
        self._run_hook({"session_id": "sess-18", "transcript_path": str(path)})
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event["cardinal.turn_tool"]
        for (cmd, expected, multi), attrs in zip(cases, tools):
            self.assertEqual(attrs.get("bash_class"), expected, repr(cmd))
            if multi:
                self.assertIs(attrs.get("bash_multi"), True, repr(cmd))
            else:
                self.assertNotIn("bash_multi", attrs, repr(cmd))

    def test_bash_command_text_never_leaks_into_otlp_body(self):
        # The privacy boundary for Field 4: only the closed enum leaves
        # the process. Scan the raw OTLP bodies for any fragment of the
        # command string.
        marker = "XyZZy-secret-arg-9137"
        command = f"curl -H 'Authorization: Bearer {marker}' https://internal.example/{marker}"
        path = self._write_transcript("sess-19", [
            _user_text_msg(),
            _assistant_msg(
                {"input_tokens": 1, "output_tokens": 1},
                content=[_tool_use_block("Bash", {"command": command}, "t1")],
            ),
        ])
        self._run_hook({"session_id": "sess-19", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 1)
        raw = json.dumps(_OTLPStub.received)
        self.assertNotIn(marker, raw)
        self.assertNotIn("curl", raw)
        self.assertNotIn("Authorization", raw)
        by_event = _records_by_event(_OTLPStub.received[0])
        tools = by_event["cardinal.turn_tool"]
        self.assertEqual(tools[0].get("bash_class"), "network")

    def test_each_log_record_gets_unique_timeUnixNano(self):
        # Regression: pre-0.10.1 the hook stamped every log record in a
        # batch with `now_ns`. lakerunner server-side maps timeUnixNano →
        # `agent_session_events.chq_tsns`, which is part of that table's
        # PK (org, session, chq_tsns). Uniform timestamps collapsed
        # whole batches to one row via ON CONFLICT DO NOTHING, so the
        # C3/A1/D1 detectors saw 1 record per Stop firing instead of N.
        # Fix: offset each record's timeUnixNano by its index in the
        # batch.
        msg1 = _assistant_msg(
            {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 100},
            content=[
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.ts"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "x"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.ts"}},
            ],
        )
        msg2 = _assistant_msg(
            {"input_tokens": 2, "output_tokens": 2, "cache_read_input_tokens": 200},
            content=[{"type": "tool_use", "name": "Grep", "input": {"pattern": "y"}}],
        )
        proj = self.home / ".claude" / "projects" / "p1"
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / "sess-ts.jsonl"
        path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "do thing"}}) + "\n" +
            json.dumps(msg1) + "\n" +
            json.dumps(msg2) + "\n"
        )
        self._run_hook({"session_id": "sess-ts", "transcript_path": str(path)})
        self.assertEqual(len(_OTLPStub.received), 1)
        log_records = _log_records(_OTLPStub.received[0])
        # 2 usage records + 4 tool records = 6 log records
        self.assertEqual(len(log_records), 6)
        ts_values = [int(r["timeUnixNano"]) for r in log_records]
        self.assertEqual(
            len(set(ts_values)),
            len(ts_values),
            f"every log record must have a unique timeUnixNano (got {ts_values})",
        )
        # Records must be strictly monotonically increasing — ordering
        # is what makes the index-offset safe across batches.
        self.assertEqual(ts_values, sorted(ts_values))
        # And observedTimeUnixNano must track timeUnixNano (kept in
        # sync so server-side chq_tsns derivation can use either).
        observed = [int(r["observedTimeUnixNano"]) for r in log_records]
        self.assertEqual(observed, ts_values)


if __name__ == "__main__":
    unittest.main()
