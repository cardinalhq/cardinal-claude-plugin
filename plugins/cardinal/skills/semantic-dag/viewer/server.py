#!/usr/bin/env python3
"""Local viewer for the semantic-dag skill (multi-thread).

Routes:
  GET  /                    → thread index
  GET  /health              → {"ok":true}
  GET  /t/<thread>          → HTML viewer for that thread
  GET  /t/<thread>/state    → materialized dag.json
  GET  /t/<thread>/events   → SSE stream for that thread only
  POST /t/<thread>/reset    → convenience button (clears the thread's DAG)

Pure stdlib. No install step.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue, Empty

PORT = int(os.environ.get("SEMANTIC_DAG_PORT", "8765"))
STATE_DIR = Path(
    os.path.expanduser(
        os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.claude/state/semantic-dag")
    )
)
THREADS_DIR = STATE_DIR / "threads"
THREADS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_HTML = Path(__file__).parent / "index.html"
EMIT = Path(__file__).parent.parent / "emit.py"

THREAD_RE = re.compile(r"^t-[a-f0-9]+$|^[A-Za-z0-9_.-]{1,64}$")

# per-thread subscriber lists
_subs: dict[str, list[Queue]] = {}
_subs_lock = threading.Lock()


def _add_sub(thread: str, q: Queue) -> None:
    with _subs_lock:
        _subs.setdefault(thread, []).append(q)


def _remove_sub(thread: str, q: Queue) -> None:
    with _subs_lock:
        lst = _subs.get(thread)
        if lst and q in lst:
            lst.remove(q)


def _subscriber_count(thread: str) -> int:
    with _subs_lock:
        return len(_subs.get(thread, []))


def _broadcast(thread: str, payload: dict) -> None:
    data = json.dumps(payload)
    with _subs_lock:
        for q in list(_subs.get(thread, [])):
            try:
                q.put_nowait(data)
            except Exception:
                pass


def _events_file(thread: str) -> Path:
    return THREADS_DIR / thread / "events.jsonl"


def _dag_file(thread: str) -> Path:
    return THREADS_DIR / thread / "dag.json"


def _load_dag(thread: str) -> dict:
    f = _dag_file(thread)
    dag = None
    if f.exists():
        try:
            dag = json.loads(f.read_text())
        except Exception:
            dag = None
    if not dag:
        dag = {
            "thread": thread,
            "topic": "",
            "nodes": {},
            "edges": [],
            "active": [],
            "agents": {},
            "glossary": {},
            "finished": False,
            "summary": "",
        }
    # normalize legacy shape: active used to be None or a single string
    a = dag.get("active")
    if a is None:
        dag["active"] = []
    elif isinstance(a, str):
        dag["active"] = [a] if a else []
    elif not isinstance(a, list):
        dag["active"] = list(a)
    dag.setdefault("agents", {})
    dag.setdefault("glossary", {})
    return dag


def _viewer_version() -> str:
    try:
        return hashlib.sha1(INDEX_HTML.read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


def _render_viewer() -> bytes:
    return INDEX_HTML.read_text().replace("__VIEWER_BUILD__", _viewer_version()).encode()


def _tail_all_threads_forever() -> None:
    """Poll every thread's events.jsonl for new lines and broadcast."""
    positions: dict[str, int] = {}
    while True:
        try:
            for tdir in THREADS_DIR.iterdir():
                if not tdir.is_dir():
                    continue
                thread = tdir.name
                if not THREAD_RE.match(thread):
                    continue
                ef = tdir / "events.jsonl"
                if not ef.exists():
                    continue
                size = ef.stat().st_size
                pos = positions.get(thread, 0)
                if size < pos:  # truncated
                    pos = 0
                if size > pos:
                    with ef.open("r") as f:
                        f.seek(pos)
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            _broadcast(thread, {"kind": "event", "event": ev})
                        positions[thread] = f.tell()
            time.sleep(0.15)
        except Exception:
            time.sleep(0.5)


def _list_threads() -> list[dict]:
    out = []
    for tdir in sorted(THREADS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not tdir.is_dir():
            continue
        thread = tdir.name
        if not THREAD_RE.match(thread):
            continue
        dag = _load_dag(thread)
        out.append(
            {
                "thread": thread,
                "topic": dag.get("topic", ""),
                "finished": dag.get("finished", False),
                "nodes": len(dag.get("nodes", {})),
                "mtime": tdir.stat().st_mtime,
            }
        )
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _parse_thread_path(self):
        """/t/<thread>[/<suffix>] → (thread, suffix) or None."""
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "t":
            thread = parts[1]
            if not THREAD_RE.match(thread):
                return None
            suffix = "/".join(parts[2:])
            return thread, suffix
        return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        if path == "/version":
            self._send(200, json.dumps({"version": _viewer_version()}), "application/json")
            return
        if path == "/":
            self._send(200, _render_index(_list_threads()), "text/html; charset=utf-8")
            return

        parsed = self._parse_thread_path()
        if parsed is None:
            self._send(404, b"not found", "text/plain")
            return
        thread, suffix = parsed

        if suffix == "":
            try:
                html = _render_viewer()
            except FileNotFoundError:
                html = b"<h1>index.html missing</h1>"
            self._send(200, html, "text/html; charset=utf-8")
            return
        if suffix == "state":
            self._send(200, json.dumps(_load_dag(thread)).encode(), "application/json")
            return
        if suffix == "presence":
            self._send(200, json.dumps({"viewers": _subscriber_count(thread)}), "application/json")
            return
        if suffix == "events":
            self._stream_events(thread)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = self._parse_thread_path()
        if parsed is None:
            self._send(404, b"not found", "text/plain")
            return
        thread, suffix = parsed
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        if suffix == "reset":
            subprocess.Popen(
                ["python3", str(EMIT), "reset", "--thread", thread],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def _stream_events(self, thread: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: Queue = Queue(maxsize=1024)
        _add_sub(thread, q)
        try:
            snap = json.dumps({"kind": "snapshot", "dag": _load_dag(thread)})
            self.wfile.write(f"data: {snap}\n\n".encode())
            self.wfile.flush()
            last_ping = time.time()
            while True:
                try:
                    data = q.get(timeout=1.0)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                except Empty:
                    pass
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _remove_sub(thread, q)


def _render_index(threads: list[dict]) -> bytes:
    rows = []
    for t in threads:
        badge = "✓" if t["finished"] else "●"
        color = "#4ade80" if t["finished"] else "#6ea8ff"
        topic = (t["topic"] or "(no topic)").replace("<", "&lt;")
        rows.append(
            f'<li><a href="/t/{t["thread"]}">'
            f'<span style="color:{color}">{badge}</span> '
            f'<b>{topic}</b> <span style="color:#7d848f">· {t["thread"]} · '
            f'{t["nodes"]} nodes</span></a></li>'
        )
    body = f"""
<!doctype html><html><head><meta charset="utf-8"><title>semantic-dag threads</title>
<style>
  body {{ background:#0b0d10; color:#e6e8eb; font:14px -apple-system,system-ui,sans-serif;
    padding: 40px; }}
  h1 {{ font-weight:600; letter-spacing:.3px; margin-bottom: 24px; color:#e6e8eb; }}
  ul {{ list-style:none; padding:0; max-width: 720px; }}
  li {{ margin: 6px 0; }}
  li a {{ display:block; padding:10px 14px; background:#14171c; border:1px solid #2a2f38;
    border-radius:8px; color:#e6e8eb; text-decoration:none; }}
  li a:hover {{ border-color:#6ea8ff; }}
</style></head><body>
<h1>semantic-dag threads</h1>
<ul>{''.join(rows) or '<li style="color:#7d848f">no threads yet</li>'}</ul>
</body></html>
"""
    return body.encode()


def main() -> None:
    t = threading.Thread(target=_tail_all_threads_forever, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"semantic-dag viewer on http://localhost:{PORT}", flush=True)
    print(f"state: {STATE_DIR}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
