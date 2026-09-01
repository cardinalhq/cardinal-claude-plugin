#!/usr/bin/env python3
"""Shared typed Semantic DAG event engine for Cardinal agent adapters.

Adapters provide only runtime facts: state location, session-id environment
keys, viewer assets, and the default local port. Graph semantics, persistence,
CLI parsing, browser presence, and multi-agent provenance live here once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    """Agent-specific facts supplied by a thin adapter entrypoint."""

    runtime: str
    default_state_dir: str
    default_port: int
    viewer_dir: Path
    native_thread_env: tuple[str, ...] = ()
    project_dir_env: str | None = None

    @property
    def state_dir(self) -> Path:
        configured = os.environ.get("SEMANTIC_DAG_STATE_DIR", self.default_state_dir)
        return Path(os.path.expanduser(configured))

    @property
    def port(self) -> int:
        return int(os.environ.get("SEMANTIC_DAG_PORT", str(self.default_port)))


_CONFIG: RuntimeConfig | None = None


def configure(config: RuntimeConfig) -> None:
    """Configure this process for one adapter runtime."""
    global _CONFIG
    _CONFIG = config
    _threads_dir().mkdir(parents=True, exist_ok=True)
    _bindings_dir().mkdir(parents=True, exist_ok=True)


def _config() -> RuntimeConfig:
    if _CONFIG is None:
        raise RuntimeError("Semantic DAG runtime is not configured")
    return _CONFIG


def _state_dir() -> Path:
    return _config().state_dir


def _threads_dir() -> Path:
    return _state_dir() / "threads"


def _bindings_dir() -> Path:
    return _state_dir() / "bindings"


THREAD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
GENERIC_LABEL_RE = re.compile(
    r"\b(?:phase|stage|step|part|task)\s*[-:#]?\s*\d+\b", re.IGNORECASE
)
NODE_TYPES = {
    "GOAL", "QUESTION", "HYPOTHESIS", "DECISION", "WORK", "EVIDENCE", "OUTCOME"
}
RELATION_TYPES = {
    "decomposes_into", "raises", "tested_by", "supported_by", "refuted_by",
    "resolved_by", "based_on", "leads_to", "depends_on", "produces",
    "implements", "validates", "supersedes",
}
STATUS_TYPES = {
    "pending", "active", "paused", "completed", "confirmed", "rejected",
    "superseded", "resolved", "error",
}


def _safe_thread(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if THREAD_RE.fullmatch(value):
        return value
    return "c-" + hashlib.sha1(value.encode()).hexdigest()[:16]


def _native_thread() -> str | None:
    value = next(
        (os.environ.get(key) for key in _config().native_thread_env if os.environ.get(key)),
        None,
    )
    return _safe_thread(value)


def _safe_agent(value: str | None) -> str:
    value = (value or "root").strip()
    if AGENT_RE.fullmatch(value):
        return value
    return "agent-" + hashlib.sha1(value.encode()).hexdigest()[:10]


def _qualify_node(agent: str, node_id: str) -> str:
    """Namespace child node IDs while preserving legacy root IDs."""
    node_id = node_id.strip()
    if node_id.startswith("root::"):
        return node_id.removeprefix("root::")
    if "::" in node_id or agent == "root":
        return node_id
    return f"{agent}::{node_id}"


def _validate_label(label: str) -> None:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", label)
    if len(words) < 2:
        raise ValueError(
            "node label must be a concrete 2–7 word action or outcome, such as "
            "'Validate token rotation'"
        )
    if len(words) > 7:
        raise ValueError("node label must stay concise (at most 7 words)")
    if GENERIC_LABEL_RE.search(label):
        raise ValueError(
            "node label cannot be a generic numbered phase; describe the actual work"
        )


def _cwd_key() -> str:
    project_env = _config().project_dir_env
    scope = os.environ.get(project_env) if project_env else None
    return hashlib.sha1((scope or os.getcwd()).encode()).hexdigest()[:12]


def _pointer_path() -> Path:
    return _state_dir() / f"current-{_cwd_key()}"


def _read_pointer() -> str | None:
    try:
        return _safe_thread(_pointer_path().read_text().strip())
    except OSError:
        return None


def _write_pointer(thread: str) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _pointer_path().write_text(thread)


def _binding_path() -> Path | None:
    native = _native_thread()
    return _bindings_dir() / f"{native}.json" if native else None


def _read_binding() -> dict:
    path = _binding_path()
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_binding(thread: str, agent: str) -> None:
    path = _binding_path()
    if path is not None:
        path.write_text(json.dumps({"thread": thread, "agent": agent}))


def _thread_dir(thread: str) -> Path:
    directory = _threads_dir() / thread
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _events_file(thread: str) -> Path:
    return _thread_dir(thread) / "events.jsonl"


def _dag_file(thread: str) -> Path:
    return _thread_dir(thread) / "dag.json"


def _new_thread_id() -> str:
    return "t-" + secrets.token_hex(4)


def _empty_dag(thread: str) -> dict:
    now = time.time()
    return {
        "thread": thread,
        "runtime": _config().runtime,
        "topic": "",
        "cwd": os.getcwd(),
        "nodes": {},
        "edges": [],
        "active": None,
        "active_by_agent": {},
        "agents": {
            "root": {
                "id": "root",
                "label": "Root",
                "runtime": _config().runtime,
                "status": "active",
                "created": now,
            }
        },
        "glossary": {},
        "finished": False,
        "summary": "",
        "watch_mode": False,
        "session_started": now,
        "turn": 1,
        "turns": [
            {"n": 1, "topic": "", "started": now, "ended": None, "outcome": ""}
        ],
    }


def _load_dag(thread: str) -> dict:
    try:
        dag = json.loads(_dag_file(thread).read_text())
    except (OSError, ValueError):
        return _empty_dag(thread)
    dag.setdefault("glossary", {})
    dag.setdefault("watch_mode", False)
    dag.setdefault("runtime", _config().runtime)
    active = dag.get("active")
    active_by_agent = dag.setdefault("active_by_agent", {})
    if isinstance(active, list):
        for node_id in active:
            node = dag.get("nodes", {}).get(node_id, {})
            active_by_agent.setdefault(_safe_agent(node.get("agent")), node_id)
        dag["active"] = active_by_agent.get("root")
    elif isinstance(active, str) and active and not active_by_agent:
        node = dag.get("nodes", {}).get(active, {})
        owner = _safe_agent(node.get("agent"))
        active_by_agent[owner] = active
        dag["active"] = active if owner == "root" else None
    elif active is not None and not isinstance(active, str):
        dag["active"] = None
    if "turns" not in dag or not isinstance(dag.get("turns"), list) or not dag["turns"]:
        dag["turns"] = [
            {
                "n": 1,
                "topic": dag.get("topic", ""),
                "started": dag.get("session_started", time.time()),
                "ended": None,
                "outcome": "",
            }
        ]
    max_turn_n = max(int(turn.get("n", 1)) for turn in dag["turns"])
    stored_turn = int(dag.get("turn", max_turn_n))
    dag["turn"] = max_turn_n if stored_turn > max_turn_n else stored_turn
    current_turn = dag["turn"]
    for node in dag.get("nodes", {}).values():
        node.setdefault("type", "WORK")
        if node.get("status") == "done":
            node["status"] = "completed"
        node.setdefault("description", "")
        node.setdefault("concepts", [])
        node.setdefault("notes", [])
        node.setdefault("files", {"read": [], "updated": []})
        node.setdefault("turn", current_turn)
    for edge in dag.get("edges", []):
        edge.setdefault("relationship", "decomposes_into")
        edge.setdefault("turn", current_turn)
    return dag


def _save_dag(thread: str, dag: dict) -> None:
    target = _dag_file(thread)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dag, indent=2))
    temporary.replace(target)


@contextmanager
def _thread_lock(thread: str):
    """Serialize materialization when an optional tool hook emits concurrently."""
    lock_path = _thread_dir(thread) / ".lock"
    with lock_path.open("a") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _last_in_agent(dag: dict, agent: str) -> str | None:
    current_turn = int(dag.get("turn", 1))
    candidates = (
        node
        for node in dag.get("nodes", {}).values()
        if node.get("agent", "root") == agent
        and int(node.get("turn", 1)) == current_turn
    )
    latest = max(candidates, key=lambda node: node.get("created", 0), default=None)
    return latest.get("id") if latest else None


def _apply(dag: dict, event: dict) -> None:
    event_type = event["type"]
    agent = _safe_agent(event.get("agent"))
    active_by_agent = dag.setdefault("active_by_agent", {})
    if not active_by_agent and isinstance(dag.get("active"), str):
        active_by_agent["root"] = dag["active"]
    agents = dag.setdefault("agents", {})
    agents.setdefault(
        "root",
        {
            "id": "root",
            "label": "Root",
            "runtime": dag.get("runtime", _config().runtime),
            "status": "active",
            "created": event["ts"],
        },
    )
    if event_type == "start":
        dag.update(
            {
                "nodes": {},
                "edges": [],
                "active": None,
                "active_by_agent": {},
                "agents": {
                    "root": {
                        "id": "root",
                        "label": event.get("agent_label", "Root"),
                        "runtime": event.get("runtime", dag.get("runtime", _config().runtime)),
                        "status": "active",
                        "created": event["ts"],
                    }
                },
                "glossary": {},
                "finished": False,
                "summary": "",
                "turn": 1,
                "turns": [
                    {
                        "n": 1,
                        "topic": event.get("topic", ""),
                        "started": event["ts"],
                        "ended": None,
                        "outcome": "",
                    }
                ],
            }
        )
        if event.get("topic"):
            dag["topic"] = event["topic"]
        if event.get("runtime"):
            dag["runtime"] = event["runtime"]
        if "watch" in event:
            dag["watch_mode"] = bool(event["watch"])
        dag["session_started"] = event["ts"]
        dag["cwd"] = event.get("cwd", dag.get("cwd", ""))
        return
    if event_type == "reset":
        turns = dag.setdefault("turns", [])
        current_turn = int(dag.get("turn", len(turns) or 1))
        # Close open nodes so the previous turn reads as done.
        for node in dag.get("nodes", {}).values():
            if node.get("status") in ("active", "paused"):
                node["status"] = "completed"
                node["tool"] = None
                node["updated"] = event["ts"]
        if turns:
            turns[-1]["ended"] = event["ts"]
            turns[-1]["outcome"] = (
                event.get("outcome") or dag.get("summary") or turns[-1].get("outcome", "")
            )
        dag["active"] = None
        dag["active_by_agent"] = {}
        dag["finished"] = False
        dag["summary"] = ""
        if "watch" in event:
            dag["watch_mode"] = bool(event["watch"])
        root = agents.setdefault(
            "root",
            {
                "id": "root",
                "label": event.get("agent_label", "Root"),
                "runtime": dag.get("runtime", _config().runtime),
                "status": "active",
                "created": event["ts"],
            },
        )
        root["status"] = "active"
        root["updated"] = event["ts"]
        new_turn = current_turn + 1
        dag["turn"] = new_turn
        turns.append(
            {
                "n": new_turn,
                "topic": event.get("topic", ""),
                "started": event["ts"],
                "ended": None,
                "outcome": "",
            }
        )
        if event.get("topic"):
            dag["topic"] = event["topic"]
        return
    if event_type == "agent_begin":
        agents[agent] = {
            "id": agent,
            "label": event.get("label", agent),
            "status": "active",
            "created": event["ts"],
            "parent": event.get("parent"),
            "parent_agent": event.get("parent_agent", "root"),
            "task": event.get("task", ""),
            "description": event.get("description", ""),
        }
        return
    if event_type == "agent_finish":
        for node in dag["nodes"].values():
            if node.get("agent", "root") == agent and node["status"] in (
                "active",
                "paused",
            ):
                node["status"] = "completed"
                node["tool"] = None
                node["updated"] = event["ts"]
        if agent in agents:
            agents[agent]["status"] = "completed"
            agents[agent]["summary"] = event.get("summary", "")
            agents[agent]["updated"] = event["ts"]
        active_by_agent.pop(agent, None)
        return
    if event_type == "topic":
        dag["topic"] = event.get("topic", "")
        return
    if event_type == "watch":
        dag["watch_mode"] = bool(event.get("enabled"))
        return
    if event_type == "finish":
        for node in dag["nodes"].values():
            if node["status"] in ("active", "paused"):
                node["status"] = "completed"
                node["tool"] = None
                node["updated"] = event["ts"]
        dag["active"] = None
        dag["active_by_agent"] = {}
        for item in dag.get("agents", {}).values():
            item["status"] = "completed"
            item["updated"] = event["ts"]
        dag["finished"] = True
        dag["summary"] = event.get("summary", "")
        turns = dag.get("turns")
        if turns:
            turns[-1]["ended"] = event["ts"]
            turns[-1]["outcome"] = event.get("summary") or turns[-1].get("outcome", "")
        return
    if event_type == "add":
        node_id = event["id"]
        existing = dag["nodes"].get(node_id)
        if existing:
            existing["type"] = event["semantic_type"]
            existing["label"] = event["label"]
            if "description" in event:
                existing["description"] = event["description"]
            existing["updated"] = event["ts"]
            return
        explicit_parent = event.get("parent")
        current_turn = int(dag.get("turn", 1))
        if not explicit_parent and not event.get("root"):
            event["parent"] = _last_in_agent(dag, agent)
            if not event["parent"]:
                agent_parent = agents.get(agent, {}).get("parent")
                parent_node = dag["nodes"].get(agent_parent) if agent_parent else None
                if parent_node and int(parent_node.get("turn", 1)) == current_turn:
                    event["parent"] = agent_parent
        node_turn = int(dag.get("turn", 1))
        event["turn"] = node_turn
        dag["nodes"][node_id] = {
            "id": node_id,
            "type": event["semantic_type"],
            "label": event["label"],
            "description": event.get("description", ""),
            "concepts": [],
            "notes": [],
            "files": {"read": [], "updated": []},
            "agent": agent,
            "local_id": event.get("local_id", node_id),
            "status": "pending",
            "tool": None,
            "created": event["ts"],
            "updated": event["ts"],
            "turn": node_turn,
        }
        if event.get("parent"):
            relationship = event.get("relationship") or (
                "decomposes_into" if explicit_parent or agents.get(agent, {}).get("parent") == event["parent"]
                else "leads_to"
            )
            event["relationship"] = relationship
            edge = {
                "from": event["parent"],
                "to": node_id,
                "relationship": relationship,
                "turn": node_turn,
            }
            if not any(
                e["from"] == edge["from"] and e["to"] == edge["to"]
                and e["relationship"] == edge["relationship"]
                for e in dag["edges"]
            ):
                dag["edges"].append(edge)
        return
    if event_type == "describe":
        node_id = event["id"]
        if node_id in dag["nodes"]:
            dag["nodes"][node_id]["description"] = event.get("description", "")
            dag["nodes"][node_id]["updated"] = event["ts"]
        return
    if event_type == "concept":
        node_id = event["id"]
        if node_id in dag["nodes"]:
            concepts = dag["nodes"][node_id].setdefault("concepts", [])
            concept = {
                "term": event.get("term", ""),
                "definition": event.get("definition", ""),
                "updated": event["ts"],
            }
            match = next(
                (
                    index
                    for index, item in enumerate(concepts)
                    if item.get("term", "").casefold() == concept["term"].casefold()
                ),
                None,
            )
            if match is None:
                concepts.append(concept)
            else:
                concepts[match] = concept
            dag["nodes"][node_id]["updated"] = event["ts"]
            term = concept["term"].strip()
            if term:
                dag.setdefault("glossary", {})[term] = concept["definition"]
        return
    if event_type == "note":
        node_id = event["id"]
        if node_id in dag["nodes"]:
            dag["nodes"][node_id].setdefault("notes", []).append(
                {"text": event.get("text", ""), "ts": event["ts"]}
            )
            dag["nodes"][node_id]["updated"] = event["ts"]
        return
    if event_type == "file":
        node_id = event["id"]
        if node_id in dag["nodes"]:
            kind = event.get("kind", "read")
            files = dag["nodes"][node_id].setdefault(
                "files", {"read": [], "updated": []}
            )
            bucket = files.setdefault(kind, [])
            path = event.get("path", "")
            if path and path not in bucket:
                bucket.append(path)
            dag["nodes"][node_id]["updated"] = event["ts"]
        return
    if event_type == "define":
        term = event.get("term", "").strip()
        if term:
            dag.setdefault("glossary", {})[term] = event.get("definition", "")
        return
    if event_type == "undefine":
        term = event.get("term", "").strip()
        dag.setdefault("glossary", {}).pop(term, None)
        return
    if event_type == "link":
        link_turn = int(dag.get("turn", 1))
        event["turn"] = link_turn
        edge = {
            "from": event["from"], "to": event["to"],
            "relationship": event["relationship"],
            "turn": link_turn,
        }
        if not any(
            e["from"] == edge["from"] and e["to"] == edge["to"]
            and e["relationship"] == edge["relationship"]
            for e in dag["edges"]
        ):
            dag["edges"].append(edge)
        return
    if event_type == "activate":
        node_id = event["id"]
        previous = active_by_agent.get(agent)
        if previous in dag["nodes"] and dag["nodes"][previous]["status"] == "active":
            dag["nodes"][previous]["status"] = "paused"
        if node_id in dag["nodes"]:
            dag["nodes"][node_id]["status"] = "active"
            dag["nodes"][node_id]["tool"] = None
            dag["nodes"][node_id]["updated"] = event["ts"]
            active_by_agent[agent] = node_id
            if agent == "root":
                dag["active"] = node_id
        return
    if event_type in ("done", "error", "status"):
        node_id = event["id"]
        status = "completed" if event_type == "done" else (
            "error" if event_type == "error" else event["status"]
        )
        if status == "active":
            previous = active_by_agent.get(agent)
            if previous in dag["nodes"] and previous != node_id and dag["nodes"][previous]["status"] == "active":
                dag["nodes"][previous]["status"] = "paused"
        if node_id in dag["nodes"]:
            dag["nodes"][node_id]["status"] = status
            dag["nodes"][node_id]["tool"] = None
            dag["nodes"][node_id]["updated"] = event["ts"]
            if event.get("reason"):
                dag["nodes"][node_id]["reason"] = event["reason"]
        for owner, active_id in list(active_by_agent.items()):
            if active_id == node_id and status != "active":
                active_by_agent.pop(owner, None)
        if status == "active":
            active_by_agent[agent] = node_id
            if agent == "root":
                dag["active"] = node_id
        elif dag.get("active") == node_id:
            dag["active"] = None
        return
    if event_type == "tool":
        node_id = event["id"]
        if node_id in dag["nodes"]:
            dag["nodes"][node_id]["tool"] = {
                "name": event["tool"],
                "summary": event.get("summary", ""),
                "ts": event["ts"],
            }
            dag["nodes"][node_id]["updated"] = event["ts"]


def _activation_note(dag: dict, event: dict) -> dict | None:
    is_activation = event.get("type") == "activate" or (
        event.get("type") == "status" and event.get("status") == "active"
    )
    if not is_activation:
        return None
    node_id = event.get("id")
    node = dag.get("nodes", {}).get(node_id)
    if not node or node.get("notes"):
        return None
    description = str(node.get("description") or "").strip()
    label = str(node.get("label") or "this phase").strip()
    text = f"Now working: {description}" if description else f"Now working on {label}."
    return {
        "type": "note",
        "id": node_id,
        "agent": event.get("agent", "root"),
        "text": text,
        "source": "activation",
        "ts": max(time.time(), float(event["ts"]) + 0.000001),
    }


def emit(thread: str, event: dict) -> None:
    event.setdefault("ts", time.time())
    with _thread_lock(thread):
        dag = _load_dag(thread)
        _apply(dag, event)
        events = [event]
        note = _activation_note(dag, event)
        if note:
            _apply(dag, note)
            events.append(note)
        with _events_file(thread).open("a") as stream:
            for item in events:
                stream.write(json.dumps(item) + "\n")
        _save_dag(thread, dag)


def _pop_flag(arguments: list[str], name: str) -> str | None:
    if name not in arguments:
        return None
    index = arguments.index(name)
    if index + 1 >= len(arguments):
        raise ValueError(f"{name} requires a value")
    value = arguments[index + 1]
    del arguments[index : index + 2]
    return value


def _display_path(value: str) -> str:
    path = Path(os.path.expanduser(value))
    resolved = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def _resolve_thread(arguments: list[str], required: bool = True) -> str | None:
    override = _safe_thread(_pop_flag(arguments, "--thread"))
    shared = _safe_thread(
        os.environ.get("SEMANTIC_DAG_ROOT_THREAD")
        or os.environ.get("SEMANTIC_DAG_THREAD")
    )
    bound = _safe_thread(_read_binding().get("thread"))
    thread = override or shared or bound or _native_thread() or _read_pointer()
    if required and not thread:
        print("no current thread; run `emit.py begin \"<topic>\"` first", file=sys.stderr)
        raise SystemExit(2)
    return thread


def _resolve_agent(arguments: list[str]) -> str:
    return _safe_agent(
        _pop_flag(arguments, "--agent")
        or os.environ.get("SEMANTIC_DAG_AGENT")
        or _read_binding().get("agent")
    )


def _default_agent_label(agent: str) -> str:
    if agent == "root":
        return "Root"
    words = re.sub(r"[_./-]+", " ", agent).strip()
    return words.title() if words else agent


def _server_reachable(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.2)
        try:
            client.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _ensure_server(port: int) -> bool:
    if os.environ.get("SEMANTIC_DAG_NO_SERVER"):
        return False
    if _server_reachable(port):
        return True
    server = _config().viewer_dir / "server.py"
    log = _state_dir() / "viewer.log"
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_PORT"] = str(port)
    try:
        log_stream = open(log, "ab")
        subprocess.Popen(
            [sys.executable, str(server)],
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=log_stream,
            env=environment,
            start_new_session=sys.platform != "win32",
            creationflags=0x00000008 if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        print(f"failed to spawn viewer: {exc}", file=sys.stderr)
        return False
    deadline = time.time() + 3
    while time.time() < deadline:
        if _server_reachable(port):
            return True
        time.sleep(0.1)
    return False


def _viewer_present(port: int, thread: str) -> bool:
    """Return whether this DAG currently has a live SSE viewer."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/t/{thread}/presence", timeout=0.35
        ) as response:
            payload = json.loads(response.read())
        return int(payload.get("viewers", 0)) > 0
    except (OSError, ValueError, TypeError):
        return False


def _open_browser(url: str) -> None:
    if os.environ.get("SEMANTIC_DAG_NO_OPEN"):
        return
    commands = {
        "darwin": ["open", url],
        "win32": ["cmd", "/c", "start", "", url],
    }
    command = commands.get(sys.platform, ["xdg-open", url])
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _url(thread: str) -> str:
    return f"http://127.0.0.1:{_config().port}/t/{thread}"


def main(config: RuntimeConfig) -> int:
    configure(config)
    arguments = sys.argv[1:]
    if not arguments:
        print(__doc__, file=sys.stderr)
        return 2
    command, arguments = arguments[0], arguments[1:]

    try:
        agent = _resolve_agent(arguments)
        if command in ("begin", "start"):
            override = _safe_thread(_pop_flag(arguments, "--thread"))
            shared = _safe_thread(
                os.environ.get("SEMANTIC_DAG_ROOT_THREAD")
                or os.environ.get("SEMANTIC_DAG_THREAD")
            )
            parent = _pop_flag(arguments, "--parent")
            parent_agent = _safe_agent(_pop_flag(arguments, "--parent-agent"))
            agent_label = _pop_flag(arguments, "--agent-label")
            description = _pop_flag(arguments, "--description")
            topic = arguments[0] if arguments else ""
            thread = override or shared or _native_thread() or _read_pointer() or _new_thread_id()
            existed = _dag_file(thread).exists()
            if agent == "root":
                event_type = "reset" if command == "begin" and existed else "start"
                event = {
                    "type": event_type,
                    "topic": topic,
                    "cwd": os.getcwd(),
                    "watch": True,
                    "runtime": _config().runtime,
                    "agent_label": agent_label or "Root",
                }
            else:
                event = {
                    "type": "agent_begin",
                    "agent": agent,
                    "label": agent_label or _default_agent_label(agent),
                    "task": topic,
                    "parent_agent": parent_agent,
                    "description": description or f"Sub-agent {agent} is working on {topic or 'its assigned task'}.",
                }
                if parent:
                    event["parent"] = _qualify_node("root", parent)
            emit(thread, event)
            _write_pointer(thread)
            _write_binding(thread, agent)
            url = _url(thread)
            reachable = _ensure_server(_config().port)
            if reachable and agent == "root" and (
                command == "start" or not _viewer_present(_config().port, thread)
            ):
                _open_browser(url)
            print(f"THREAD={thread}")
            print(f"URL={url}")
            if not reachable and not os.environ.get("SEMANTIC_DAG_NO_SERVER"):
                print(f"viewer failed to start; see {_state_dir() / 'viewer.log'}", file=sys.stderr)
            return 0

        thread = _resolve_thread(arguments)
        assert thread is not None
        if command == "url":
            print(_url(thread))
            return 0
        if command == "watch":
            if not arguments or arguments[0].lower() not in ("on", "off"):
                raise ValueError("usage: watch <on|off>")
            emit(thread, {"type": "watch", "enabled": arguments[0].lower() == "on", "agent": agent})
            return 0
        if command in ("reset", "topic", "finish"):
            value = arguments[0] if arguments else ""
            if command == "finish" and agent != "root":
                emit(thread, {"type": "agent_finish", "agent": agent, "summary": value})
            else:
                key = "summary" if command == "finish" else "topic"
                emit(thread, {"type": command, key: value, "agent": agent})
            return 0
        if command == "add":
            parent = _pop_flag(arguments, "--parent")
            relationship = _pop_flag(arguments, "--relation")
            description = _pop_flag(arguments, "--description")
            root = "--root" in arguments
            if root:
                arguments.remove("--root")
            if len(arguments) < 3:
                raise ValueError(
                    "usage: add <id> <TYPE> <label> [--parent <id> | --root] "
                    "[--relation <relationship>] [--description <text>]"
                )
            semantic_type = arguments[1].upper()
            if semantic_type not in NODE_TYPES:
                raise ValueError(f"node type must be one of: {', '.join(sorted(NODE_TYPES))}")
            if relationship and relationship not in RELATION_TYPES:
                raise ValueError(f"relationship must be one of: {', '.join(sorted(RELATION_TYPES))}")
            _validate_label(arguments[2])
            node_id = _qualify_node(agent, arguments[0])
            event = {
                "type": "add",
                "id": node_id,
                "local_id": arguments[0],
                "semantic_type": semantic_type,
                "label": arguments[2],
                "agent": agent,
            }
            if parent:
                event["parent"] = _qualify_node(agent, parent)
            if root:
                event["root"] = True
            if relationship:
                event["relationship"] = relationship
            if description:
                event["description"] = description
            emit(thread, event)
            return 0
        if command == "concept":
            if len(arguments) < 3:
                raise ValueError("usage: concept <id> <term> <definition>")
            term, definition = arguments[1].strip(), arguments[2].strip()
            if not term or not definition:
                raise ValueError("concept term and definition must be non-empty")
            emit(
                thread,
                {
                    "type": "concept",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "term": term,
                    "definition": definition,
                },
            )
            return 0
        if command == "note":
            if len(arguments) < 2:
                raise ValueError("usage: note <id> <text>")
            emit(
                thread,
                {
                    "type": "note",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "text": arguments[1],
                },
            )
            return 0
        if command == "file":
            if len(arguments) < 3:
                raise ValueError("usage: file <id> <read|updated> <path>")
            kind = arguments[1].strip().lower()
            if kind not in ("read", "updated"):
                raise ValueError("file kind must be `read` or `updated`")
            if not arguments[2].strip():
                raise ValueError("file path must be non-empty")
            emit(
                thread,
                {
                    "type": "file",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "kind": kind,
                    "path": _display_path(arguments[2]),
                },
            )
            return 0
        if command == "define":
            if len(arguments) < 2 or not arguments[0].strip() or not arguments[1].strip():
                raise ValueError("usage: define <term> <definition>")
            emit(
                thread,
                {"type": "define", "term": arguments[0].strip(), "definition": arguments[1].strip()},
            )
            return 0
        if command == "undefine":
            if not arguments:
                raise ValueError("usage: undefine <term>")
            emit(thread, {"type": "undefine", "term": arguments[0].strip()})
            return 0
        if command == "describe":
            if len(arguments) < 2:
                raise ValueError("usage: describe <id> <description>")
            emit(
                thread,
                {
                    "type": "describe",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "description": arguments[1],
                },
            )
            return 0
        if command == "link":
            if len(arguments) < 3:
                raise ValueError("usage: link <from> <relationship> <to>")
            relationship = arguments[1]
            if relationship not in RELATION_TYPES:
                raise ValueError(f"relationship must be one of: {', '.join(sorted(RELATION_TYPES))}")
            emit(
                thread,
                {
                    "type": "link",
                    "from": _qualify_node(agent, arguments[0]),
                    "relationship": relationship,
                    "to": _qualify_node(agent, arguments[2]),
                    "agent": agent,
                },
            )
            return 0
        if command in ("activate", "done"):
            if not arguments:
                raise ValueError(f"usage: {command} <id>")
            emit(
                thread,
                {"type": command, "id": _qualify_node(agent, arguments[0]), "agent": agent},
            )
            return 0
        if command == "error":
            if not arguments:
                raise ValueError("usage: error <id> [reason]")
            emit(
                thread,
                {
                    "type": "error",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "reason": arguments[1] if len(arguments) > 1 else "",
                },
            )
            return 0
        if command == "status":
            if len(arguments) < 2:
                raise ValueError("usage: status <id> <status> [reason]")
            status = arguments[1].lower()
            if status not in STATUS_TYPES:
                raise ValueError(f"status must be one of: {', '.join(sorted(STATUS_TYPES))}")
            emit(
                thread,
                {
                    "type": "status",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "status": status,
                    "reason": arguments[2] if len(arguments) > 2 else "",
                },
            )
            return 0
        if command == "tool":
            if len(arguments) < 2:
                raise ValueError("usage: tool <id> <tool> [summary]")
            emit(
                thread,
                {
                    "type": "tool",
                    "id": _qualify_node(agent, arguments[0]),
                    "agent": agent,
                    "tool": arguments[1],
                    "summary": arguments[2] if len(arguments) > 2 else "",
                },
            )
            return 0
        raise ValueError(f"unknown command: {command}")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(
        "cardinal_core.semantic_dag is a shared engine; run an adapter's emit.py"
    )
