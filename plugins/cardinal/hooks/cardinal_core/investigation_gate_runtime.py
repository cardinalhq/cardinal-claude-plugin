"""Direct CLI routing, readiness receipts and legacy-delivery ownership.

Receipts prove the hook entry point was exercised, not an OS security boundary.
The operator must launch a supported CLI with CARDINAL_COORDINATION_MODE=direct-cli.
No settings, registrations, delivery cursors or credentials are changed here.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import time
from pathlib import Path

from . import investigation_consumer as c
from . import investigation_events as ie
from . import investigation_gate as engine

MODE = "CARDINAL_COORDINATION_MODE"
STATE_DIR = "CARDINAL_COORDINATION_STATE_DIR"
REQUIRED = {"codex": {"Bash", "apply_patch"}, "claude": {"Bash", "Write", "Edit", "NotebookEdit"}}
GATE_RULE = "When a coordination gate defers a call, handle its attributed batch, then choose your next action again."
PREFIX = "Gate: read all chunks with gate-read; acknowledge/review, then choose again.\n"


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


def route(home, sid):
    try:
        out = c.private_read(route_path(home, sid))
    except FileNotFoundError:
        return None
    if (out.get("version") != 1 or out.get("session") != sid or type(out.get("enabled")) is not bool
            or out.get("runtime") not in REQUIRED or out.get("state") not in ("activating", "active")
            or not ie.valid_investigation(out.get("investigation_id")) or not Path(out.get("gate_file", "")).is_absolute()):
        raise ValueError("invalid coordination route; repair or disable it explicitly")
    if out.get("policy_hash") != engine.policy_hash(engine.policy(out.get("policy"))):
        raise ValueError("coordination route policy hash mismatch")
    if out.get("previous_policy") and out.get("previous_policy_hash") != engine.policy_hash(engine.policy(out["previous_policy"])):
        raise ValueError("previous coordination route policy hash mismatch")
    return out


def owns_delivery(home, sid, investigation=None, runtime=None):
    """Conservatively retain legacy queues on unreadable/activating routes."""
    try:
        selected = route(home, sid)
        if not selected or not selected["enabled"]:
            return False
        if runtime and selected["runtime"] != runtime:
            return False
        if investigation and selected["investigation_id"] != investigation:
            return False
        if selected["state"] == "active":
            state = c.private_read(Path(selected["gate_file"]))
            if state.get("gate", {}).get("enabled") is False:
                return False
        return True
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
    tool, args, transport = normalized(runtime, payload)
    try:
        record_ready(home, sid, runtime, transport, args, hook, environ)
    except Exception:
        pass  # never turn a failed receipt into successful enablement
    try:
        selected = route(home, sid)
        if not selected or not selected["enabled"]:
            return None
        local_policy = {**selected["policy"], "runtime": runtime}
        kind = engine.classify(local_policy, tool, args, session=sid,
                               investigation=selected["investigation_id"], gate_file=selected["gate_file"],
                               credential_file=selected.get("credential_file"))
        if selected.get("previous_policy") and engine.classify({**selected["previous_policy"], "runtime": runtime}, tool, args,
                session=sid, investigation=selected["investigation_id"], gate_file=selected["gate_file"],
                credential_file=selected.get("credential_file")) == "covered":
            kind = "covered"
        if kind != "covered":
            return None
        if selected["runtime"] != runtime or environ.get(MODE) != "direct-cli":
            return denial("Enabled gate is scoped to its declared direct CLI runtime. Use that runtime or explicitly disable it.")
        # Credentials and investigation come from the explicit private route;
        # a supervisor's own binding must not retarget its worker gate.
        cred = selected.get("credential_file")
        conn = c.credential_connection(Path(cred)) if cred else connection()
        if not conn:
            raise ValueError("no connection")
        inv = selected["investigation_id"]
        if conn.get("investigation_id") not in (None, inv):
            raise ValueError("credential targets another investigation")
        gate = engine.Gate({**conn, "investigation_id": inv}, Path(selected["gate_file"]),
                           session=sid, client=client, home=home, credential_file=cred, deadline=2.0)
        if selected["state"] != "active":
            return denial("Gate activation is incomplete. Retry gate-enable or disable it explicitly before continuing.")
        if not gate.path.exists():
            raise ValueError("active gate state is missing")
        result = gate.check(tool, args)
        if result.status == "defer":
            return denial(PREFIX + result.message)
        return None
    except Exception as exc:
        return denial("Coordination adapter unavailable (" + type(exc).__name__ + "). Resolve it or explicitly disable the gate.")
