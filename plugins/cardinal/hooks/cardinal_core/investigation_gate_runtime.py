"""Automatic direct CLI coordination, durable consumer routing and delivery ownership.

Legacy readiness helpers remain for explicit low-level activation. Automatic
dispatch requires no mode switch, policy file or enable command.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import shutil
import json
import time
from pathlib import Path

from . import investigation_consumer as c
from . import investigation_events as ie
from . import investigation_gate as engine

MODE = "CARDINAL_COORDINATION_MODE"
STATE_DIR = "CARDINAL_COORDINATION_STATE_DIR"
REQUIRED = {"codex": {"Bash", "apply_patch"}, "claude": {"Bash", "Write", "Edit", "NotebookEdit"}}
GATE_RULE = "Coordination is automatic on supported direct CLI paths. Handle attributed feedback before covered actions. When delegating required work, record an investigation request with its assignee and dependent actions; use respond for complete, blocked, abstain or reject, and resolve to accept the exact completed result or cancel with a reason."
PREFIX = "Coordination: gate-read --batch B for every chunk; handle/review, then choose again.\n"


def route_path(home, sid):
    if not ie.valid_session(sid):
        raise ValueError("invalid gate session")
    override = os.environ.get(STATE_DIR)
    directory = Path(override) if override else ie.sessions_dir(Path(home))
    if not directory.is_absolute():
        raise ValueError(STATE_DIR + " must be an absolute private directory")
    return directory / (sid + ".gate-route.json")


def ready_path(home, sid):
    return route_path(home, sid).with_suffix(".ready.json")


def _read_route(home, sid, path=None):
    try:
        out = c.private_read(path or route_path(home, sid))
    except FileNotFoundError:
        return None
    if (out.get("version") != 1 or out.get("session") != sid or type(out.get("enabled")) is not bool
            or out.get("runtime") not in REQUIRED or out.get("state") not in ("activating", "active", "superseded")
            or not ie.valid_investigation(out.get("investigation_id")) or not Path(out.get("gate_file", "")).is_absolute()):
        raise ValueError("invalid coordination route; repair or disable it explicitly")
    if out.get("policy_hash") not in engine.compatible_policy_hashes(engine.policy(out.get("policy"))):
        raise ValueError("coordination route policy hash mismatch")
    if out.get("previous_policy") and out.get("previous_policy_hash") not in engine.compatible_policy_hashes(engine.policy(out["previous_policy"])):
        raise ValueError("previous coordination route policy hash mismatch")
    return out


def index_path(home, sid):
    return route_path(home, sid).with_suffix(".consumers.json")


def all_routes(home, sid):
    paths = [route_path(home, sid)]
    # Include a persisted activating route left before its index publication.
    # These are our own typed route records, never a scan for credentials.
    if paths[0].parent.exists():
        paths.extend(paths[0].parent.glob(sid + ".consumer-*.json"))
    if len(paths) > 128:
        raise ValueError("too many coordination consumers; resolve retired associations")
    try:
        index = c.private_read(index_path(home, sid))
        for name in index["routes"]:
            p = Path(name)
            if p.parent != paths[0].parent or not p.name.startswith(sid + ".consumer-") or p.suffix != ".json":
                raise ValueError("invalid coordination consumer route")
            if not p.exists():
                raise ValueError("indexed coordination consumer activation is incomplete")
            paths.append(p)
    except FileNotFoundError:
        pass
    return [(p, r) for p in dict.fromkeys(paths) if (r := _read_route(home, sid, p)) is not None]


def route(home, sid, investigation=None, batch=None, credential_file=None):
    candidates = [r for _, r in all_routes(home, sid)
                  if r["state"] != "superseded" and (not investigation or r["investigation_id"] == investigation)]
    if credential_file:
        candidates = [r for r in candidates if r.get("credential_file") == str(Path(credential_file).absolute())]
    if batch:
        matching = []
        for candidate in candidates:
            pending = c.private_read(Path(candidate["gate_file"])).get("gate", {}).get("batch")
            if pending and pending["page"]["batch"] == batch:
                matching.append(candidate)
        candidates = matching
    if len(candidates) > 1:
        raise ValueError("multiple coordination consumers; specify --investigation/--credential-file or the exact --batch")
    return candidates[0] if candidates else None


def owns_delivery(home, sid, investigation=None, runtime=None):
    """Conservatively retain legacy queues on unreadable/activating routes."""
    try:
        for _, selected in all_routes(home, sid):
            if selected["state"] == "superseded":
                continue
            if runtime and selected["runtime"] != runtime:
                continue
            if investigation and selected["investigation_id"] != investigation:
                continue
            # Automatic coordination retains delivery ownership even during
            # repair/rearming; disabling cannot become a persistent opt-out.
            if selected.get("automatic") or selected["enabled"]:
                return True
        return False
    except (ValueError, OSError):
        return True


@contextlib.contextmanager
def delivery_guard(home, sid, investigation=None, runtime=None):
    path = route_path(home, sid)
    if not path.parent.exists():
        yield True
        return
    lock = c.locked(path)
    try:
        lock.__enter__()
    except BlockingIOError:
        yield False
        return
    try:
        yield not owns_delivery(home, sid, investigation, runtime)
    finally:
        lock.__exit__(None, None, None)


def runtime_pid(runtime):
    """Find the CLI ancestor by executable name, without reading prompts/argv."""
    pid = os.getppid()
    deadline = time.monotonic() + 0.5
    for _ in range(12):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=min(0.1, remaining))
        parent, _, comm = result.stdout.strip().partition(" ")
        if Path(comm.strip()).name == runtime:
            return pid
        if not parent.isdigit() or int(parent) <= 1:
            break
        pid = int(parent)
    return None


def hook_path(cli, runtime):
    return Path(cli).resolve().parent.parent / "hooks" / (
        "cardinal-codex-telemetry.py" if runtime == "codex" else "coordination-gate.py")


def source_hash(path):
    h = hashlib.sha256()
    for item in (Path(path), Path(__file__), Path(engine.__file__), Path(ie.__file__)):
        h.update(item.read_bytes())
    return h.hexdigest()


def normalized(runtime, payload):
    tool, args = payload.get("tool_name"), payload.get("tool_input")
    if runtime == "codex" and tool in ("Bash", "exec_command", "functions.exec_command"):
        args = args if isinstance(args, dict) else {}
        return "exec_command", {"cmd": args.get("command", args.get("cmd"))}, "Bash"
    if runtime == "codex" and tool in ("apply_patch", "functions.apply_patch"):
        return "apply_patch", args, "apply_patch"
    return tool, args, tool


def record_ready(home, sid, runtime, tool, args, path, environ):
    if environ.get(MODE) != "direct-cli" or tool not in REQUIRED[runtime]:
        return
    pid = runtime_pid(runtime)
    if pid is None:
        return
    p = ready_path(home, sid)
    c.private_directory(p.parent)
    with c.locked(p):
        signature = source_hash(path)
        try:
            saved = c.private_read(p)
        except FileNotFoundError:
            saved = {}
        if saved.get("pid") != pid or saved.get("source") != signature or saved.get("runtime") != runtime:
            saved = {"version": 1, "pid": pid, "source": signature, "runtime": runtime, "tools": {}}
        saved["tools"][tool] = time.time()
        if tool == "Bash":
            command = args.get("cmd", args.get("command")) if isinstance(args, dict) else None
            saved["last_command"] = engine.digest(engine.simple_command(command))
        c.private_write(p, saved)


def preflight(home, sid, runtime, cli, environ, *, argv=None, prefixes=()):
    if runtime not in REQUIRED or environ.get(MODE) != "direct-cli":
        raise ValueError("gate enablement requires a supported direct CLI launched with " + MODE + "=direct-cli")
    try:
        proof = c.private_read(ready_path(home, sid))
    except FileNotFoundError:
        raise ValueError("no live pre-tool readiness receipts; exercise the registered transports before gate-enable") from None
    # The sandboxed CLI may not execute ps. Bind its exact enable argv to the
    # immediately preceding unsandboxed pre-tool receipt instead. The hook has
    # already tied every transport probe to one live CLI ancestor process.
    pid = proof.get("pid") if argv is not None else runtime_pid(runtime)
    if type(pid) is not int or proof.get("pid") != pid or proof.get("runtime") != runtime or proof.get("source") != source_hash(hook_path(cli, runtime)):
        raise ValueError("readiness belongs to another runtime process or hook version; repeat transport probes")
    now = time.time()
    missing = [t for t in REQUIRED[runtime] if type(proof.get("tools", {}).get(t)) not in (float, int)
               or not 0 <= now - proof["tools"][t] <= 300]
    if missing:
        raise ValueError("missing fresh pre-tool receipts: " + ", ".join(sorted(missing)) + "; exercise harmless calls then retry gate-enable")
    if argv is not None:
        commands = [prefix + ["investigation", *argv] for prefix in prefixes if prefix and Path(prefix[-1]).resolve() == Path(cli).resolve()]
        if (proof.get("last_command") not in [engine.digest(cmd) for cmd in commands]
                or not 0 <= now - proof["tools"]["Bash"] <= 30):
            raise ValueError("gate-enable needs its own fresh pre-tool receipt through a registered CLI prefix")
    return proof


def activate(gate, value, *, runtime, cli, environ, new_epoch=False, budget_file=None, argv=None):
    value = engine.policy(value)
    if value.get("runtime") != runtime:
        raise ValueError("policy runtime does not match this CLI adapter")
    c.private_directory(ready_path(gate.home, gate.session).parent)
    with c.locked(ready_path(gate.home, gate.session)):
        proof = preflight(gate.home, gate.session, runtime, cli, environ, argv=argv, prefixes=value["cli_prefixes"])
        if argv is not None:
            proof["last_command"] = None  # consume the enable receipt exactly once
            c.private_write(ready_path(gate.home, gate.session), proof)
    p = route_path(gate.home, gate.session)
    c.private_directory(p.parent)
    if gate.path == p or gate.path == ready_path(gate.home, gate.session):
        raise ValueError("gate state must be separate from routing/readiness files")
    with c.locked(p):
        old = route(gate.home, gate.session)
        selected = {"version": 1, "session": gate.session, "runtime": runtime, "enabled": True,
                    "state": "activating", "investigation_id": gate.conn["investigation_id"],
                    "policy": engine.policy(value),
                    "policy_hash": engine.policy_hash(value),
                    "gate_file": str(gate.path), "credential_file": str(gate.credential_file) if gate.credential_file else None}
        if old and old["enabled"] and any(old.get(k) != selected[k] for k in
                ("runtime", "gate_file", "investigation_id", "credential_file")):
            raise ValueError("another gate owns this session; disable it explicitly before replacing its route")
        if old and old["enabled"]:
            selected["previous_policy"] = old.get("previous_policy", old["policy"])
            selected["previous_policy_hash"] = engine.policy_hash(selected["previous_policy"])
        engine._save(p, selected)
        result = gate.enable(value, new_epoch=new_epoch, budget_file=budget_file)
        selected["state"] = "active"
        selected.pop("previous_policy", None)
        selected.pop("previous_policy_hash", None)
        engine._save(p, selected)
    return {**result, "runtime_mode": "direct-cli", "guidance": GATE_RULE}


def deactivate(gate):
    p = route_path(gate.home, gate.session)
    c.private_directory(p.parent)
    with c.locked(p):
        selected = route(gate.home, gate.session)
        if selected and selected["gate_file"] != str(gate.path):
            raise ValueError("this session routes another gate file")
        result = {"enabled": False, "activation_cancelled": selected["state"] == "activating", "missing_state": True} if (
            selected and not gate.path.exists()) else gate.disable()
        if selected:
            selected["enabled"] = False
            engine._save(p, selected)
        return result


def denial(message):
    if len(message.encode()) > 1000:
        message = "Coordination feedback exceeded its byte budget; use gate-status/gate-read and resolve before retrying."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": message}}


def default_policy(runtime, hook, role="worker"):
    """Stable authenticated-peer selection; never rebuilt from observed peers."""
    roots = {Path(hook).absolute().parent.parent, Path(hook).resolve().parent.parent}
    prefixes = []
    for root in sorted(roots):
        cli = root / ("scripts" if runtime == "codex" else "bin") / "cardinal-storyboard"
        prefixes.extend([[str(cli)], [sys.executable, str(cli)], ["python3", str(cli)]])
        for python in ("/usr/bin/python3", shutil.which("python3")):
            if python:
                prefixes.append([python, str(cli)])
    return engine.policy({"version": 1, "runtime": runtime, "role": role,
        "authenticated_peers": 1, "peers": [], "cli_prefixes": prefixes,
        "git": list(dict.fromkeys(["git", "/usr/bin/git", shutil.which("git") or "git"])),
        "gh": list(dict.fromkeys(["gh", shutil.which("gh") or "gh"]))})


def _command_credential(value, tool, args):
    if tool not in ("exec_command", "Bash") or not isinstance(args, dict):
        return None
    tokens = engine.simple_command(args.get("cmd", args.get("command"))) or []
    for prefix in value["cli_prefixes"]:
        if not engine.prefix_matches(tokens, prefix) or tokens[len(prefix):len(prefix) + 1] != ["investigation"]:
            continue
        from .investigation_cli import build_parser, normalize_argv
        from types import SimpleNamespace
        import io
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                parsed = build_parser(SimpleNamespace(environ={})).parse_args(normalize_argv(tokens[len(prefix) + 1:]))
            return parsed
        except (SystemExit, ValueError):
            return None
    return None


def ensure_automatic(home, sid, runtime, conn, client, hook, credential_file=None, deadline=1.0):
    """Bind one verified consumer without replacing another consumer's state."""
    role = "supervisor" if conn.get("grant_id") and "supervise" in conn.get("scopes", []) else "participant" if conn.get("grant_id") else "worker"
    value = default_policy(runtime, hook, role)
    identity = {"connection": c.identity(conn), "runtime": runtime,
                "credential": engine.digest({k: conn.get(k) for k in ("key", "token", "api_key")}),
                "credential_file": str(Path(credential_file).absolute()) if credential_file else None}
    consumer = engine.digest(identity)
    parent = c.private_directory(route_path(home, sid).parent)
    path = parent / (sid + ".consumer-" + consumer + ".json")
    # Reuse a legacy route for this exact consumer so outstanding hashes remain
    # readable until explicit handling. Its next clean epoch adopts defaults.
    for candidate, selected in all_routes(home, sid):
        if (selected["investigation_id"] == conn["investigation_id"] and selected["runtime"] == runtime
                and selected.get("credential_file") == identity["credential_file"]
                and selected.get("consumer_identity") in (None, identity)):
            if selected["state"] == "superseded":
                continue
            path = candidate
            break
    with c.locked(path):
        old = _read_route(home, sid, path)
        if old and old.get("automatic") and not conn.get("grant_id"):
            value["role"] = old["policy"]["role"]
        if old:
            for name in ("git", "gh", "cli_prefixes"):
                for item in old["policy"][name]:
                    if item not in value[name]:
                        value[name].append(item)
        gate_file = Path(old["gate_file"]) if old else parent / (sid + ".state-" + consumer + ".json")
        gate = engine.Gate(conn, gate_file, session=sid, client=client, home=home,
                           credential_file=credential_file, deadline=deadline)
        if old and old["state"] == "active" and not gate_file.exists():
            raise ValueError("active coordination state is missing; restore it before continuing")
        if old and old.get("consumer_identity") not in (None, identity):
            raise ValueError("coordination credentials changed; retain old obligations and use a new consumer")
        if old and gate_file.exists():
            with c.locked(gate_file):
                state = gate._load()
                if state["gate"].get("batch"):
                    # Do not rewrite pending pages, policy hashes or chunks.
                    if not state["gate"]["enabled"]:
                        state["gate"]["enabled"] = True
                        engine._save(gate_file, state)
                    old.update(enabled=True, automatic=True)
                    engine._save(path, old)
                    return old, gate
        selected = {"version": 1, "session": sid, "runtime": runtime, "enabled": True,
            "automatic": True, "consumer_identity": identity, "state": "activating",
            "investigation_id": conn["investigation_id"], "policy": value,
            "policy_hash": engine.policy_hash(value), "gate_file": str(gate_file),
            "credential_file": identity["credential_file"]}
        # Publish discovery before network activation: a crash cannot silently
        # drop a scoped consumer from later covered calls.
        if not old:
            engine._save(path, selected)
        index = index_path(home, sid)
        with c.locked(index):
            try:
                saved = c.private_read(index)
            except FileNotFoundError:
                saved = {"routes": []}
            if path != route_path(home, sid) and str(path) not in saved["routes"]:
                saved["routes"].append(str(path))
            engine._save(index, saved)
        if old and old.get("automatic") and old.get("policy_hash") == selected["policy_hash"] and old.get("state") == "active":
            # Identity was checked above. Every action still samples a fresh
            # versioned snapshot; there is no cached permit.
            with c.locked(gate_file):
                state = gate._load()
                if not state["gate"]["enabled"]:
                    state["gate"]["enabled"] = True
                    engine._save(gate_file, state)
            return old, gate
        engine._save(path, selected)
        gate.enable(value, new_epoch=gate_file.exists(), budget_file=conn.get("_coordination_budget"))
        actual = c.private_read(gate_file)["gate"]
        selected["policy"], selected["policy_hash"] = actual["policy"], actual["policy_hash"]
        selected["state"] = "active"
        engine._save(path, selected)
        return selected, gate


def automatic_dispatch(payload, *, runtime, home, connection, client, hook, environ):
    from . import investigation_grants as grants
    end = time.monotonic() + 3.3
    sid = payload["session_id"]
    tool, args, _ = normalized(runtime, payload)
    value = default_policy(runtime, hook)
    context = dict(session=sid, investigation="inv_" + "0" * 24, gate_file=route_path(home, sid))
    command = _command_credential(value, tool, args)
    known = [(p, r) for p, r in all_routes(home, sid) if r["state"] != "superseded"]
    covered = engine.classify(value, tool, args, **context) == "covered"
    for _, selected in known:
        for policy in (selected["policy"], selected.get("previous_policy")):
            if policy and engine.classify(policy, tool, args, session=sid,
                    investigation=selected["investigation_id"], gate_file=selected["gate_file"],
                    credential_file=selected.get("credential_file")) == "covered":
                covered = True
    # A command using a credential establishes that explicit association even
    # when it is a read/control command. Never discover credentials by scanning.
    if not covered and not (command and command.credential_file):
        return None
    if any(r["runtime"] != runtime for _, r in known):
        return denial("This session already belongs to a different direct CLI runtime; use its original runtime.")
    binding = ie.read_binding(Path(home), sid)
    scoped = grants.token_connection(environ) if grants.token_set(environ) else None
    if not known and not binding and not scoped and not (command and command.credential_file):
        own = connection()
        if not own or not own.get("key"):
            return None  # Truly unconnected sessions remain silent.
        from . import investigation_bootstrap as bootstrap
        result = bootstrap.ensure(Path(home), sid, own, client, context={},
            wanted=environ.get("CARDINAL_INVESTIGATION_ID") or None, timeout=min(1.0, max(0.001, end - time.monotonic())))
        binding = result.get("binding")
        if not binding:
            return denial("Coordination is initializing this connected session's Investigation. Resolve bootstrap/connection failure before covered actions.")
    if runtime_pid(runtime) is None:
        return denial("Automatic coordination requires a supported direct CLI process; this runtime path is unvalidated.")
    contexts = []
    if scoped:
        p = route_path(home, sid).with_name(sid + ".scope-" + scoped["grant_id"] + ".json")
        c.private_directory(p.parent)
        c.private_write(p, {"token": scoped["token"], "origin": scoped["origin"]})
        contexts.append((scoped, p))
    elif binding:
        own = connection()
        if not own or not own.get("key"):
            raise ValueError("bound session has no author connection")
        contexts.append(({**own, "investigation_id": binding["investigation_id"]}, None))
    if command and command.credential_file:
        conn = c.credential_connection(command.credential_file)
        if command.session != sid or (command.investigation or command.investigation_id) not in (None, conn["investigation_id"]):
            raise ValueError("credential command differs from this consumer session/investigation")
        contexts.append((conn, command.credential_file))
    for _, selected in known:
        cred = selected.get("credential_file")
        if cred:
            credential = Path(cred)
            expected = selected.get("consumer_identity", {}).get("connection")
            ancestors = []
            for _ in range(32):
                if credential.exists():
                    break
                successor = c.private_read(credential.with_suffix(credential.suffix + ".successor.json"))
                if successor.get("old_identity") != expected:
                    raise ValueError("credential succession identity mismatch")
                ancestors.append(expected["grant_id"])
                next_identity = successor.get("new_identity") or {}
                if any(next_identity.get(k) != expected.get(k) for k in ("origin", "org", "investigation_id")):
                    raise ValueError("credential succession changed Investigation/endpoint")
                expected = next_identity
                credential = Path(successor["credential_file"])
            else:
                raise ValueError("credential succession chain is too long")
            loaded = c.credential_connection(credential)
            if ancestors and expected != c.identity(loaded):
                raise ValueError("successor credential identity differs from its explicit association")
            previous = c.private_read(Path(selected["gate_file"]))["gate"]
            old_conn = selected.get("consumer_identity", {}).get("connection", c.identity(loaded))
            if old_conn.get("grant_id") and (old_conn.get("grant_id") != loaded.get("grant_id")
                    or selected.get("consumer_identity", {}).get("credential") != engine.digest({k: loaded.get(k) for k in ("key", "token", "api_key")})):
                loaded["_coordination_budget"] = previous.get("budget_file")
                loaded["_previous_grant_ids"] = sorted(set(ancestors + [old_conn["grant_id"]]))
                loaded["_supersedes_gate_files"] = [selected["gate_file"]]
            contexts.append((loaded, credential))
        elif not scoped:
            own = connection()
            if not own or not own.get("key"):
                raise ValueError("existing author consumer lost its connection")
            contexts.append(({**own, "investigation_id": selected["investigation_id"]}, None))
    # Merge proof and pinned-budget metadata before activation. Explicit NEW
    # command order must not discard the OLD consumer's required response guard.
    grouped = {}
    for conn, credential in contexts:
        ident = engine.digest({"conn": c.identity(conn), "credential": str(credential) if credential else None,
            "secret": engine.digest({k: conn.get(k) for k in ("key", "token", "api_key")})})
        if ident not in grouped:
            grouped[ident] = (dict(conn), credential)
        target = grouped[ident][0]
        pins = {x for x in (target.get("_coordination_budget"), conn.get("_coordination_budget")) if x}
        if len(pins) > 1:
            raise ValueError("incompatible pinned supervisor response guards")
        if pins:
            target["_coordination_budget"] = next(iter(pins))
        target["_previous_grant_ids"] = sorted(set(target.get("_previous_grant_ids", [])) | set(conn.get("_previous_grant_ids", [])))
        target["_supersedes_gate_files"] = sorted(set(target.get("_supersedes_gate_files", [])) | set(conn.get("_supersedes_gate_files", [])))
    contexts = list(grouped.values())
    seen, gates = set(), []
    for conn, credential in contexts:
        ident = engine.digest({"conn": c.identity(conn), "credential": str(credential) if credential else None})
        if ident in seen:
            continue
        seen.add(ident)
        remaining = end - time.monotonic()
        if remaining <= 0.15:
            return denial("Coordination consumers are still initializing. Retry the chosen action; no clearance was issued.")
        _, gate = ensure_automatic(home, sid, runtime, conn, client, hook, credential, deadline=min(1.0, remaining))
        gates.append(gate)
        if conn.get("grant_id"):
            new_state = c.private_read(gate.path)
            for old_path, old in known:
                old_identity = old.get("consumer_identity", {}).get("connection", {})
                if old_identity.get("grant_id") is None or old["gate_file"] == str(gate.path):
                    continue
                if any(old_identity.get(k) != conn.get(k) for k in ("origin", "org", "investigation_id")):
                    continue
                # Matching lineage alone is insufficient: a still-live token
                # copied to another path is another consumer, not renewal.
                if (old["gate_file"] in conn.get("_supersedes_gate_files", [])
                        and old_identity["grant_id"] in new_state["gate"].get("previous_grant_ids", [])):
                    with c.locked(old_path):
                        old.update(state="superseded", successor_file=str(gate.path))
                        engine._save(old_path, old)
    checked = {str(gate.path) for gate in gates}
    for old_path, old in known:
        current = _read_route(home, sid, old_path)
        if current["state"] != "superseded" and old["gate_file"] not in checked:
            return denial("A prior coordination consumer still has unresolved credential identity. Restore it or supply its authenticated successor; no clearance was issued.")
    if not covered:
        return None
    for gate in gates:
        remaining = end - time.monotonic()
        if remaining <= 0.15:
            return denial("Coordination snapshot is incomplete across consumers. Retry the chosen action; no clearance was issued.")
        gate.deadline = min(1.0, remaining)
        result = gate.check(tool, args)
        if result.status == "defer":
            return denial(PREFIX + result.message)
    if time.monotonic() >= end:
        return denial("Coordination deadline reached. Choose the next action again.")
    return None


def dispatch(payload, *, runtime, home, connection, client, hook, environ):
    """Thin adapters supply connection lazily. No output means pass-through.

    Actual hook crashes/timeouts remain fail-open runtime limitations. Caught
    operational failures return supported denial JSON when a route is present.
    """
    if not isinstance(payload, dict) or payload.get("agent_id") or payload.get("hook_event_name") != "PreToolUse":
        return None
    sid = payload.get("session_id")
    if not ie.valid_session(sid):
        return None
    try:
        return automatic_dispatch(payload, runtime=runtime, home=home, connection=connection,
            client=client, hook=hook, environ=environ)
    except Exception as exc:
        return denial("Automatic coordination unavailable (" + type(exc).__name__ + "). Resolve the connection/runtime state before retrying.")
