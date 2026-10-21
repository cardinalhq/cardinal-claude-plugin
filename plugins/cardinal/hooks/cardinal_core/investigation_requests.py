"""Request/result dependencies, folded from authenticated immutable events.

Acknowledgments, delivery, deadlines and feedback review never resolve a request.
Only its requester can accept an exact complete response or explicitly cancel.
"""
from __future__ import annotations

from . import investigation_consumer as c
from . import investigation_events as ie
from pathlib import Path
import hashlib
import json
import uuid

TYPES = ("request.created", "request.responded", "request.resolved")
ACTIONS = ("edit", "commit", "push", "merge", "verdict")
OUTCOMES = ("complete", "blocked", "abstain", "reject")


def party(principal, session):
    return {"principal": principal, "session": None if principal.startswith("grantee:") else session}


def producer(event):
    p = event.get("producer") or {}
    key = c.principal_key(p.get("principal"))
    if not key:
        raise ValueError("request event has no authenticated producer")
    if key.startswith("grantee:") and p.get("lineage_root_id"):
        key = "grantee:" + p["lineage_root_id"]
    return party(key, p.get("session_id"))


def fold(state, events):
    requests = state.setdefault("requests", {})
    for e in events:
        typ, p = e.get("type"), e.get("payload") or {}
        if typ not in TYPES:
            continue
        actor = producer(e)
        if typ == TYPES[0]:
            if (not isinstance(p.get("assignee"), dict) or not p.get("actions")
                    or any(a not in ACTIONS for a in p["actions"])):
                raise ValueError("malformed request")
            requests[str(e["seq"])] = {"request": e["seq"], "requester": actor,
                "assignee": p["assignee"], "actions": p["actions"], "status": "waiting",
                "response": None, "resolved": False, "text": p.get("text"), "refs": p.get("refs", []), "result": None}
            continue
        request = requests.get(str(p.get("request")))
        if not request or request["resolved"]:
            raise ValueError("request transition without an unresolved request")
        if typ == TYPES[1]:
            if actor != request["assignee"] or p.get("outcome") not in OUTCOMES:
                raise ValueError("request response identity/outcome mismatch")
            if request["status"] not in ("waiting", "blocked"):
                raise ValueError("request response is already final")
            request.update(status=p["outcome"], response=e["seq"], result={"text": p.get("text"), "refs": p.get("refs", []), "producer": e["producer"]})
        else:
            if actor != request["requester"]:
                raise ValueError("request resolution identity mismatch")
            if p.get("outcome") == "accepted":
                if request["status"] != "complete" or p.get("response") != request["response"]:
                    raise ValueError("request acceptance needs its exact complete response")
            elif p.get("outcome") != "cancelled" or "response" in p:
                raise ValueError("invalid request resolution")
            request.update(resolved=True, status=p["outcome"])
    return requests


def waiting(state, principal, session, action):
    who = party(principal, session)
    return [r for r in state.get("requests", {}).values()
            if r["requester"] == who and not r["resolved"] and action in r["actions"]]


def relevant(event, state, principal, session):
    who = party(principal, session)
    if event["type"] == TYPES[0]:
        return event["payload"].get("assignee") == who
    request = state.get("requests", {}).get(str(event["payload"].get("request")))
    return bool(request and (request["requester"] == who or request["assignee"] == who)
                and producer(event) != who)


def outbox_directory(home, session, credential_file=None):
    return c.private_directory(Path(credential_file).parent / "outbox" if credential_file
        else ie.sessions_dir(Path(home)) / (session + ".request-outbox"))


def create(conn, investigation, session, payload, *, home, client, key=None, credential_file=None):
    """Fresh task per successful invocation; uncertain retries reuse one intent.

    Unlike an advisory repost, a later identical delegation is a NEW dependency.
    A timed-out create retains its identity and locally holds dependent actions.
    """
    from .investigation_gate import _save
    material = [c.identity({**conn, "investigation_id": investigation}), session, payload]
    name = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    path = outbox_directory(home, session, credential_file) / ("request-" + name + ".json")
    with c.locked(path):
        try:
            saved = c.private_read(path)
        except FileNotFoundError:
            saved = {}
        if "result" in saved or "refused" in saved:
            saved = {}
        if saved:
            intent = saved["intent"]
            if intent.get("identity") != material[0] or intent.get("payload") != payload or (key and intent["key"] != key):
                raise ValueError("retry the unresolved request unchanged before creating another")
        else:
            intent = {"identity": material[0], "type": TYPES[0], "payload": payload,
                "session": session, "key": key or "request:" + uuid.uuid4().hex}
            saved = {"intent": intent}
            _save(path, saved)
        try:
            out = ie.append_event(conn, investigation, TYPES[0], payload, idempotency_key=intent["key"],
                client=client, session_id=None if conn.get("grant_id") else session)
        except Exception as exc:
            c.settle_refused(path, saved, exc)
            raise
        saved["result"] = out
        _save(path, saved)
        return out


def uncertain(home, session, conn, action, credential_file=None):
    directory = Path(credential_file).parent / "outbox" if credential_file else ie.sessions_dir(Path(home)) / (session + ".request-outbox")
    for path in sorted(directory.glob("request-*.json")) if directory.exists() else ():
        value = c.private_read(path)
        intent = value.get("intent") or {}
        if (intent.get("identity") == c.identity(conn) and "result" not in value and "refused" not in value
                and action in intent.get("payload", {}).get("actions", [])):
            return True
    return False
