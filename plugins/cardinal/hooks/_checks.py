"""Checks preview opt-in: local state, skill install/removal, and the label
ledger the guided flow keeps while teaching a check. Shared by
bin/cardinal-checks and hooks/checks-gate.py.

Checks are reusable, evidence-backed decisions over telemetry, built on the
existing cardinal-behavior MCP tools. The preview is off unless the user runs
`cardinal-checks on`. Off means Claude cannot invoke the skill, not just that
nothing suggests it: the skill is not shipped in the auto-loaded skills/
directory. It lives as a template in checks/skill/ and `on` copies it to
skills/checks/ inside this plugin (Claude Code then lists it as
cardinal:checks); `off` removes that copy. hooks/checks-gate.py reconciles the
copy with the opt-in at session start (a plugin update installs a fresh
version directory without it) and denies the Skill tool for it while the
opt-in is off (a session that listed the skill before `off` keeps its
listing until skills reload).

State (~/.claude/cardinal/checks/, removed with the other identity caches on
disconnect, which turns the preview off):
  enabled.json             {"enabled": bool, "updated_at": ...}
  drafts/<draft>.json      the teaching examples of one check being built

Never raises from the read helpers; the install helpers raise OSError for the
CLI to report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path("checks") / "skill"
INSTALL_DIR = Path("skills") / "checks"
# Written into the installed copy so only a copy this plugin made is ever
# removed or replaced; holds the template digest it was copied from.
MARKER = ".cardinal-checks-install"
STATE_DIR = Path(".claude") / "cardinal" / "checks"

VERDICTS = ("MATCH", "NON_MATCH", "UNKNOWN", "NOT_APPLICABLE")
DRAFT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRACE_RE = re.compile(r"^[a-f0-9]{32}$")


def home_dir(home: Path | None = None) -> Path:
    if home is not None:
        return home
    return Path(os.environ.get("HOME") or str(Path.home()))


def state_dir(home: Path | None = None) -> Path:
    return home_dir(home) / STATE_DIR


# --- opt-in ---------------------------------------------------------------

def is_enabled(home: Path | None = None) -> bool:
    try:
        data = json.loads((state_dir(home) / "enabled.json").read_text())
        return isinstance(data, dict) and data.get("enabled") is True
    except (OSError, ValueError):
        return False


def set_enabled(enabled: bool, home: Path | None = None) -> None:
    path = state_dir(home) / "enabled.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": bool(enabled), "updated_at": time.time()}) + "\n")
    os.replace(tmp, path)


# --- skill install ----------------------------------------------------------

def template_dir(root: Path | None = None) -> Path:
    return (root or PLUGIN_ROOT) / TEMPLATE_DIR


def install_dir(root: Path | None = None) -> Path:
    return (root or PLUGIN_ROOT) / INSTALL_DIR


def template_digest(root: Path | None = None) -> str | None:
    """SHA-256 over the template's relative paths and bytes; None when absent."""
    src = template_dir(root)
    if not (src / "SKILL.md").is_file():
        return None
    h = hashlib.sha256()
    for path in sorted(p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(path.relative_to(src).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def installed_digest(root: Path | None = None) -> str | None:
    """The template digest recorded in an installed copy this plugin made."""
    try:
        data = json.loads((install_dir(root) / MARKER).read_text())
        return data.get("template_sha256") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_installed(root: Path | None = None) -> bool:
    return (install_dir(root) / "SKILL.md").is_file()


def install(root: Path | None = None) -> bool:
    """Copy the template to skills/checks/. True when anything changed.

    Leaves an up-to-date copy alone; replaces one this plugin made from an
    older template. Raises FileNotFoundError when the template is missing,
    FileExistsError when skills/checks/ exists but was not made here."""
    digest = template_digest(root)
    if digest is None:
        raise FileNotFoundError(f"checks skill template missing: {template_dir(root)}")
    dest = install_dir(root)
    current = installed_digest(root)
    if current == digest and is_installed(root):
        return False
    if dest.exists() and current is None:
        raise FileExistsError(f"{dest} exists and was not installed by cardinal-checks")
    staging = dest.with_name(".checks-staging")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(template_dir(root), staging, ignore=shutil.ignore_patterns("__pycache__"))
    (staging / MARKER).write_text(json.dumps({"template_sha256": digest, "installed_at": time.time()}) + "\n")
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return True


def uninstall(root: Path | None = None) -> bool:
    """Remove skills/checks/ if this plugin installed it. True when removed."""
    dest = install_dir(root)
    if not dest.exists():
        return False
    if installed_digest(root) is None and not (dest / MARKER).exists():
        raise FileExistsError(f"{dest} was not installed by cardinal-checks; not removing it")
    shutil.rmtree(dest)
    return True


def reconcile(home: Path | None = None, root: Path | None = None) -> bool:
    """Make the installed copy match the opt-in. True when it changed."""
    if is_enabled(home):
        return install(root)
    return uninstall(root)


def is_checks_skill(name, cwd: str | None = None, home: Path | None = None) -> bool:
    """True for this plugin's checks skill as the Skill tool names it.

    Claude Code passes a plugin skill as "cardinal:checks". A bare "checks"
    counts too, unless the user or project has a skill of their own by that
    name (which a bare name would then mean)."""
    if not isinstance(name, str):
        return False
    name = name.strip().lstrip("/")
    if name == "cardinal:checks":
        return True
    if name != "checks":
        return False
    own = [home_dir(home) / ".claude" / "skills" / "checks"]
    if cwd:
        own.append(Path(cwd) / ".claude" / "skills" / "checks")
    return not any((d / "SKILL.md").is_file() for d in own)


# --- teaching-example ledger ----------------------------------------------
#
# The guided flow (CORE.md steps 6-8) separates two things a user might
# conflate: whether a trace's evidence is sufficient under the contract
# (mechanical, decided before anything is shown) and how the contract applies
# to a trace with sufficient evidence (the user's call). A label cannot create
# evidence that is not in the trace, so an insufficient example is UNKNOWN by
# construction and no correction can relabel it. Labels are fixed before the
# first test_behavior call: the regression ledger refuses a changed expected
# verdict for a tested trace (compilation/regression.py reserve), so a later
# correction needs a revised contract, i.e. a new draft.

class LabelRefused(ValueError):
    """A label or correction the evidence or the draft's state does not allow."""


def draft_path(draft: str, home: Path | None = None) -> Path:
    if not isinstance(draft, str) or not DRAFT_RE.match(draft):
        raise ValueError("draft must match [A-Za-z0-9_-]{1,64}")
    return state_dir(home) / "drafts" / f"{draft}.json"


def read_draft(draft: str, home: Path | None = None) -> dict:
    path = draft_path(draft, home)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"draft": draft, "examples": {}, "locked": False}
    data.setdefault("examples", {})
    data.setdefault("locked", False)
    return data


def write_draft(data: dict, home: Path | None = None) -> None:
    path = draft_path(data["draft"], home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _check_trace(trace: str) -> None:
    if not isinstance(trace, str) or not TRACE_RE.match(trace):
        raise ValueError("trace must be a 32-character lowercase hex trace id")


def _check_verdict(verdict: str) -> None:
    if verdict not in VERDICTS:
        raise ValueError("verdict must be one of " + ", ".join(VERDICTS))


def _check_unlocked(data: dict) -> None:
    if data.get("locked"):
        raise LabelRefused(
            "these labels were already used in a test; a changed label for a tested trace is refused "
            "by the regression suite. Revise the contract (a new draft) instead.")


def set_example(draft: str, trace: str, sufficient: bool, proposed: str, reason: str,
                missing: str | None = None, home: Path | None = None) -> dict:
    """Record a candidate with its sufficiency result and Claude's proposed label.

    An insufficient candidate must be proposed as UNKNOWN and name what is
    missing; it is approved as UNKNOWN at once (there is nothing for the user
    to decide)."""
    _check_trace(trace)
    _check_verdict(proposed)
    data = read_draft(draft, home)
    _check_unlocked(data)
    if len(data["examples"]) >= 8 and trace not in data["examples"]:
        raise LabelRefused("a teaching batch holds at most 8 traces")
    if not sufficient:
        if proposed != "UNKNOWN":
            raise LabelRefused("evidence is insufficient, so this example is UNKNOWN by construction")
        if not (missing or "").strip():
            raise ValueError("name the missing evidence for an insufficient example")
    if not (reason or "").strip():
        raise ValueError("give the reason for the proposed classification")
    example = {"trace_id": trace, "sufficient": bool(sufficient), "missing": (missing or "").strip() or None,
               "proposed": proposed, "reason": reason.strip(), "label": None, "approved": False,
               "corrected": False}
    if not sufficient:
        example.update(label="UNKNOWN", approved=True)
    data["examples"][trace] = example
    write_draft(data, home)
    return example


def approve(draft: str, trace: str, label: str | None = None, home: Path | None = None) -> dict:
    """Approve Claude's proposed label, or record the user's correction.

    Refused: a correction away from UNKNOWN on an insufficient example (the
    label cannot create evidence), and any change once the labels were used in
    a test."""
    _check_trace(trace)
    data = read_draft(draft, home)
    _check_unlocked(data)
    example = data["examples"].get(trace)
    if example is None:
        raise ValueError(f"no candidate {trace} in draft {draft}; record it first")
    final = label or example["proposed"]
    _check_verdict(final)
    if not example["sufficient"] and final != "UNKNOWN":
        raise LabelRefused(
            f"refused: {final} needs evidence this trace does not have ({example['missing']}); "
            "it stays UNKNOWN, or swap in another candidate")
    example.update(label=final, approved=True, corrected=final != example["proposed"])
    write_draft(data, home)
    return example


def remove_example(draft: str, trace: str, home: Path | None = None) -> bool:
    """Drop a candidate (e.g. a mistyped trace id) before the labels are locked."""
    _check_trace(trace)
    data = read_draft(draft, home)
    _check_unlocked(data)
    removed = data["examples"].pop(trace, None) is not None
    if removed:
        write_draft(data, home)
    return removed


def expected_verdicts(draft: str, home: Path | None = None, lock: bool = True) -> dict:
    """{trace_id: label} for test_behavior's expected_verdicts.

    Every example must be approved. Locks the draft: from now on its labels
    are the regression suite's gold."""
    data = read_draft(draft, home)
    examples = data["examples"]
    if not examples:
        raise LabelRefused("no examples recorded for this draft")
    pending = sorted(t for t, e in examples.items() if not e.get("approved"))
    if pending:
        raise LabelRefused("approve every example before the first test; pending: " + ", ".join(pending))
    if lock and not data.get("locked"):
        data["locked"] = True
        data["locked_at"] = time.time()
        write_draft(data, home)
    return {t: e["label"] for t, e in sorted(examples.items())}


# --- accepted checks (reuse) ------------------------------------------------

def behavior_output_dir() -> Path:
    return Path(os.environ.get("CARDINAL_BEHAVIOR_OUTPUT_DIR")
                or str(home_dir() / ".cardinal" / "behavior-executions")).expanduser()


def accepted_checks(output: Path | None = None) -> list[dict]:
    """Checks accepted through the behavior MCP, newest first, read from its
    retained programs: description (the contract), service, the accepted
    incumbent version and when it was accepted. Read-only."""
    output = output or behavior_output_dir()
    found: dict = {}
    try:
        programs = list((output / "programs").glob("*.json"))
    except OSError:
        programs = []
    for path in programs:
        try:
            program = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        context = program.get("compilation_context") or {}
        if not program.get("acceptance_id") or not context:
            continue
        scope = program.get("compilation_scope")
        incumbent = None
        if scope:
            try:
                incumbent = json.loads((output / "compilation" / scope / "regression.json").read_text()).get("incumbent")
            except (OSError, ValueError):
                incumbent = None
        version = program.get("diagnostic_version")
        if incumbent and version != incumbent:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        key = scope or version
        if key in found and found[key]["accepted_at"] >= mtime:
            continue
        found[key] = {"description": context.get("description"), "service_name": context.get("service_name"),
                      "diagnostic_version": version, "acceptance_id": program.get("acceptance_id"),
                      "compilation_scope": scope, "accepted_at": mtime}
    return sorted(found.values(), key=lambda c: c["accepted_at"], reverse=True)


# --- background range runs (written by lib/behavior/range_runner.py) ----------

RANGE_TERMINAL = ("needs_extents", "completed", "failed", "stalled")
RANGE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def range_dir(range_id: str, output: Path | None = None) -> Path:
    if not isinstance(range_id, str) or not RANGE_ID_RE.match(range_id):
        raise ValueError("range id must be 32 lowercase hex characters")
    return (output or behavior_output_dir()) / "ranges" / range_id


def range_worker_running(directory: Path) -> bool:
    import fcntl
    try:
        with (directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def _counts(counts) -> str:
    if not isinstance(counts, dict):
        return "counts not reported"
    order = ("MATCH", "NON_MATCH", "UNKNOWN", "NOT_APPLICABLE", "ERROR")
    return " · ".join(f"{k} {counts.get(k, 0)}" for k in order)


def _window(event) -> str:
    w = event.get("window") or ["?", "?"]
    return f"{w[0]}–{w[1]}"


def format_range_event(event: dict, events_file: Path) -> str | None:
    """One notification line per event worth acting on; None for the rest."""
    kind = event.get("event")
    if kind == "started":
        return (f"started: {event.get('population')} {event.get('start')}–{event.get('end')}, "
                f"{event.get('windows')} window(s){' (judge-based: experimental)' if event.get('judge_based') else ''}")
    if kind in ("window_done", "whole_done"):
        progress = event.get("progress") or {}
        where = "whole-trace window" if kind == "whole_done" else "window"
        tail = (f" ({progress.get('windows_done')} done, {progress.get('windows_left')} left)"
                if kind == "window_done" else "")
        return f"{where} {_window(event)} {event.get('status', '').lower()}: {_counts(event.get('verdicts'))}{tail}"
    if kind == "window_split":
        return f"window {_window(event)} split in two: {event.get('reason')}"
    if kind in ("window_rejected", "whole_rejected", "window_unreadable"):
        return f"{kind.replace('_', ' ')} {_window(event)}: {event.get('error') or 'results could not be read'}"
    if kind == "read_retry":
        then = "giving up on this execution" if event.get("final") else "retrying"
        return f"results read for {_window(event)} failed ({event.get('failures')}x): {event.get('error')}; {then}"
    if kind == "needs_extents":
        return (f"needs_extents: {event.get('multi_window_trace_count', 0)} trace(s) seen in several windows, "
                f"{len(event.get('boundaries') or [])} boundaries. Read the last line of {events_file}, measure "
                "span extents with the query tools, then call continue_behavior_range.")
    if kind == "resumed":
        return "resumed: the range worker restarted from its saved state"
    if kind == "extents_received":
        return (f"extents received for {event.get('traces')} trace(s); "
                f"{event.get('whole_trace_windows')} whole-trace window(s) to run")
    if kind == "completed":
        return (f"completed: {event.get('population')} traces · {_counts(event.get('counts'))} · flagged "
                f"{event.get('flagged')} · edge traces {event.get('edge_traces')} · conflicts {event.get('conflicts')}"
                f" · coverage holes {event.get('coverage_holes')} · judge calls {event.get('judge_calls')}"
                f"{' · EXPERIMENTAL' if event.get('experimental') else ''}. Read {event.get('summary_file')}")
    if kind == "failed":
        return f"failed: {event.get('error')}"
    if kind == "stalled":
        return f"stalled: {event.get('error')}. Call continue_behavior_range to restart the worker."
    return None


def watch_range(range_id: str, out, poll_s: float = 1.0, idle_s: float = 30.0, output: Path | None = None,
                sleep=None) -> int:
    """Stream a range run's events as lines until a terminal event.

    Starts at the current segment: the events after the last terminal event
    that later events superseded (continue_behavior_range writes `resumed` or
    `extents_received` before it returns), so a watch started after a continue
    never stops at an old needs_extents. Then follows the file. Exits 0 on a
    terminal event that is the latest event; prints a stalled line and exits 0
    when the worker is gone with nothing new for idle_s. Exits 2 for an unknown
    range."""
    import time as _time
    sleep = sleep or _time.sleep
    directory = range_dir(range_id, output)
    events_file = directory / "events.jsonl"
    if not directory.is_dir():
        print(f"cardinal-checks: no range run {range_id} under {directory.parent}", file=out)
        return 2
    offset, idle, first = 0, 0.0, True
    while True:
        try:
            with events_file.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            chunk = b""
        complete = chunk[: chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""
        offset += len(complete)
        events = []
        for raw in complete.decode("utf-8", "replace").splitlines():
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        if first:
            first = False
            superseded = [i for i, e in enumerate(events[:-1]) if e.get("event") in RANGE_TERMINAL]
            if superseded:
                events = events[superseded[-1] + 1:]
        for index, event in enumerate(events):
            line = format_range_event(event, events_file)
            if line:
                print(line, file=out, flush=True)
            if event.get("event") in RANGE_TERMINAL and index == len(events) - 1:
                return 0
        if complete:
            idle = 0.0
        else:
            idle += poll_s
            if idle >= idle_s and not range_worker_running(directory):
                print(f"stalled: the range worker is not running and wrote nothing for {int(idle)}s. "
                      "Call continue_behavior_range to restart it.", file=out, flush=True)
                return 0
        sleep(poll_s)


def range_status(range_id: str, output: Path | None = None) -> dict:
    directory = range_dir(range_id, output)
    state = json.loads((directory / "state.json").read_text())
    out = {"range_id": range_id, "phase": state.get("phase"), "population": state.get("population"),
           "start": state.get("start"), "end": state.get("end"),
           "windows_done": len(state.get("windows", [])), "windows_left": len(state.get("pending", [])),
           "worker_running": range_worker_running(directory), "events_file": str(directory / "events.jsonl")}
    if state.get("error"):
        out["error"] = state["error"]
    if (directory / "summary.json").is_file():
        out["summary_file"] = str(directory / "summary.json")
        out["result_file"] = str(directory / "result.json")
    return out
