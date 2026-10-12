"""Checks preview for Claude Code: a thin wrapper over cardinal_core's
checks_install (the opt-in and the skill copy) and lib/behavior's
checks_ledger (the label ledger, accepted checks, range reads), which every
adapter shares. Used by hooks/checks-gate.py.

Claude specifics: the skill installs into this plugin's skills/checks/
(listed as cardinal:checks); state lives in ~/.claude/cardinal/checks/.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core import checks_cli, checks_install  # noqa: E402

RUNTIME = "claude"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ROUTING_RULE = checks_install.ROUTING_RULE
VERDICTS = checks_cli.VERDICTS


def is_enabled(home: Path | None = None) -> bool:
    return checks_install.is_enabled(RUNTIME, home)


def set_enabled(enabled: bool, home: Path | None = None) -> None:
    checks_install.set_enabled(RUNTIME, enabled, home)


def install_dir(root: Path | None = None) -> Path:
    return checks_install.install_dir(RUNTIME, root or PLUGIN_ROOT)


def is_installed(root: Path | None = None) -> bool:
    return checks_install.is_installed(install_dir(root))


def install(root: Path | None = None) -> bool:
    return checks_install.install(root or PLUGIN_ROOT, install_dir(root))


def uninstall(root: Path | None = None) -> bool:
    return checks_install.uninstall(install_dir(root))


def reconcile(home: Path | None = None, root: Path | None = None) -> bool:
    return checks_install.reconcile(RUNTIME, root or PLUGIN_ROOT, home)


def is_checks_skill(name, cwd: str | None = None, home: Path | None = None) -> bool:
    return checks_install.is_checks_skill(name, cwd, home)


def ledger():
    """lib/behavior/checks_ledger.py (the code behind the MCP tools)."""
    return checks_cli.load_ledger(PLUGIN_ROOT)


def watch_range(range_id: str, out, **kwargs) -> int:
    return ledger().watch_range(range_id, out, **kwargs)


def range_status(range_id: str, output: Path | None = None) -> dict:
    return ledger().range_status(range_id, output)


def accepted_checks(output: Path | None = None) -> list:
    return ledger().accepted_checks(output)
