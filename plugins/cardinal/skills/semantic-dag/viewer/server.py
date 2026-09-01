#!/usr/bin/env python3
"""Unified local HTTP/SSE viewer for Cardinal semantic DAG sessions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

PORT = int(os.environ.get("SEMANTIC_DAG_PORT", "8766"))
STATE_DIR = Path(
    os.path.expanduser(
        os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.cardinal/state/semantic-dag")
    )
)
THREADS_DIR = STATE_DIR / "threads"
THREADS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_HTML = Path(__file__).parent / "index.html"
CARDINAL_LOGO = Path(__file__).parent / "assets" / "cardinal-bird.png"
EMIT = Path(__file__).resolve().parents[1] / "emit.py"
THREAD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SESSION_ACTIVE_WINDOW_SECONDS = int(
    os.environ.get("SEMANTIC_DAG_ACTIVE_WINDOW_SECONDS", "120")
)

_subscribers: dict[str, list[Queue]] = {}
_subscriber_lock = threading.Lock()


def _dag_file(thread: str) -> Path:
    return THREADS_DIR / thread / "dag.json"


def _load_dag(thread: str) -> dict:
    try:
        return json.loads(_dag_file(thread).read_text())
    except (OSError, ValueError):
        return {
            "thread": thread,
            "runtime": "unknown",
            "topic": "",
            "nodes": {},
            "edges": [],
            "active": None,
            "active_by_agent": {},
            "agents": {"root": {"id": "root", "label": "Root", "status": "active"}},
            "finished": False,
            "summary": "",
        }


def _add_subscriber(thread: str, queue: Queue) -> None:
    with _subscriber_lock:
        _subscribers.setdefault(thread, []).append(queue)


def _remove_subscriber(thread: str, queue: Queue) -> None:
    with _subscriber_lock:
        if queue in _subscribers.get(thread, []):
            _subscribers[thread].remove(queue)


def _subscriber_count(thread: str) -> int:
    with _subscriber_lock:
        return len(_subscribers.get(thread, []))


def _broadcast(thread: str, payload: dict) -> None:
    data = json.dumps(payload)
    with _subscriber_lock:
        for queue in list(_subscribers.get(thread, [])):
            try:
                queue.put_nowait(data)
            except Exception:
                pass


def _tail_events() -> None:
    positions: dict[str, int] = {}
    while True:
        try:
            for directory in THREADS_DIR.iterdir():
                thread = directory.name
                events_file = directory / "events.jsonl"
                if not directory.is_dir() or not THREAD_RE.fullmatch(thread) or not events_file.exists():
                    continue
                size = events_file.stat().st_size
                position = positions.get(thread, 0)
                if size < position:
                    position = 0
                if size == position:
                    continue
                with events_file.open() as stream:
                    stream.seek(position)
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        _broadcast(thread, {"kind": "event", "event": event})
                    positions[thread] = stream.tell()
            time.sleep(0.12)
        except Exception:
            time.sleep(0.5)


def _session_status(dag: dict, updated: float, *, now: float | None = None) -> str:
    """Summarize persisted workflow state without calling stale work live."""
    statuses = {
        str(node.get("status") or "pending")
        for node in dag.get("nodes", {}).values()
        if isinstance(node, dict)
    }
    if "error" in statuses:
        return "error"
    if dag.get("finished"):
        return "completed"
    has_active = bool(dag.get("active_by_agent")) or "active" in statuses
    if has_active:
        age = max(0.0, (time.time() if now is None else now) - updated)
        return "active" if age <= SESSION_ACTIVE_WINDOW_SECONDS else "stale"
    if "paused" in statuses:
        return "paused"
    terminal = {
        "completed", "confirmed", "rejected", "resolved", "superseded", "done"
    }
    if statuses and statuses <= terminal:
        return "completed"
    return "pending"


def _list_threads() -> list[dict]:
    threads = []
    directories = []
    for path in THREADS_DIR.iterdir():
        if not path.is_dir():
            continue
        dag_file = path / "dag.json"
        try:
            updated = dag_file.stat().st_mtime
        except OSError:
            updated = path.stat().st_mtime
        directories.append((path, updated))
    for directory, updated in sorted(
        directories, key=lambda item: item[1], reverse=True
    ):
        if not THREAD_RE.fullmatch(directory.name):
            continue
        dag = _load_dag(directory.name)
        threads.append(
            {
                "thread": directory.name,
                "topic": dag.get("topic", ""),
                "runtime": dag.get("runtime", "unknown"),
                "finished": bool(dag.get("finished")),
                "status": _session_status(dag, updated),
                "nodes": len(dag.get("nodes", {})),
                "agents": len(dag.get("agents", {})),
                "updated": updated,
            }
        )
    return threads


def _delete_thread(thread: str) -> None:
    """Purge a thread's on-disk state: dag/events/lock, bindings, cwd pointers,
    and any in-memory SSE subscribers. Idempotent; missing paths are ignored."""
    if not THREAD_RE.fullmatch(thread):
        return
    shutil.rmtree(THREADS_DIR / thread, ignore_errors=True)
    bindings_dir = STATE_DIR / "bindings"
    if bindings_dir.is_dir():
        for path in bindings_dir.iterdir():
            try:
                if json.loads(path.read_text()).get("thread") == thread:
                    path.unlink()
            except (OSError, ValueError):
                continue
    for pointer in STATE_DIR.glob("current-*"):
        try:
            if pointer.read_text().strip() == thread:
                pointer.unlink()
        except OSError:
            continue
    with _subscriber_lock:
        for queue in list(_subscribers.get(thread, [])):
            try:
                queue.put_nowait(json.dumps({"kind": "deleted", "thread": thread}))
            except Exception:
                pass
        _subscribers.pop(thread, None)


def _viewer_version() -> str:
    try:
        return hashlib.sha1(INDEX_HTML.read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


def _render_viewer() -> bytes:
    version = _viewer_version()
    return INDEX_HTML.read_text().replace("__VIEWER_BUILD__", version).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _send(self, status: int, body: bytes | str, content_type: str) -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _thread_path(self) -> tuple[str, str] | None:
        parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
        if len(parts) < 2 or parts[0] != "t" or not THREAD_RE.fullmatch(parts[1]):
            return None
        return parts[1], "/".join(parts[2:])

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, '{"ok":true}', "application/json")
            return
        if path == "/version":
            self._send(
                200,
                json.dumps({"version": _viewer_version()}),
                "application/json",
            )
            return
        if path == "/assets/cardinal-bird.png":
            try:
                self._send(200, CARDINAL_LOGO.read_bytes(), "image/png")
            except OSError:
                self._send(404, "logo missing", "text/plain")
            return
        if path == "/sessions":
            self._send(200, json.dumps({"sessions": _list_threads()}), "application/json")
            return
        if path == "/":
            try:
                self._send(200, _render_viewer(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "viewer HTML missing", "text/plain")
            return
        parsed = self._thread_path()
        if parsed is None:
            self._send(404, "not found", "text/plain")
            return
        thread, suffix = parsed
        if not suffix:
            try:
                self._send(200, _render_viewer(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "viewer HTML missing", "text/plain")
        elif suffix == "state":
            self._send(200, json.dumps(_load_dag(thread)), "application/json")
        elif suffix == "presence":
            self._send(
                200,
                json.dumps({"viewers": _subscriber_count(thread)}),
                "application/json",
            )
        elif suffix == "events":
            self._stream_events(thread)
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self) -> None:
        parsed = self._thread_path()
        if parsed is None:
            self._send(404, "not found", "text/plain")
            return
        thread, suffix = parsed
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 10000))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if suffix == "reset":
            subprocess.Popen(
                [sys.executable, str(EMIT), "reset", "--thread", thread],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._send(200, '{"ok":true}', "application/json")
        elif suffix == "delete":
            _delete_thread(thread)
            self._send(200, '{"ok":true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def _stream_events(self, thread: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        queue: Queue = Queue(maxsize=1024)
        _add_subscriber(thread, queue)
        try:
            snapshot = json.dumps({"kind": "snapshot", "dag": _load_dag(thread)})
            self.wfile.write(f"data: {snapshot}\n\n".encode())
            self.wfile.flush()
            last_ping = time.time()
            while True:
                try:
                    data = queue.get(timeout=1)
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
            _remove_subscriber(thread, queue)


def main() -> None:
    threading.Thread(target=_tail_events, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"semantic-dag viewer on http://127.0.0.1:{PORT}", flush=True)
    print(f"state: {STATE_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
