"""Resolve this plugin's canonical version at hook-execution time.

The version stamped on emitted telemetry (`cardinal.plugin_version`
resource attribute + OTel scope version) must reflect the plugin
package that is CURRENTLY installed, not whatever value ended up baked
into `~/.claude/settings.json:OTEL_RESOURCE_ATTRIBUTES` at first
install. When users upgrade the plugin, only `plugin.json` and the
hook code itself change on disk — nothing rewrites their settings
file — so the previous approach permanently reported the install-time
version.

Reading from the sibling `../.claude-plugin/plugin.json` at every
hook execution self-heals on upgrade: the new hook + new plugin.json
land together, and the very next event carries the correct version.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache


_FALLBACK = "unknown"


@lru_cache(maxsize=1)
def plugin_version() -> str:
    """Read the plugin's canonical semver from ../.claude-plugin/plugin.json.

    Returns "unknown" if the file is missing or unparseable — never
    raises, since a version-stamp bug must not break telemetry
    emission itself.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    manifest = os.path.join(here, "..", ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _FALLBACK
    v = data.get("version")
    if isinstance(v, str) and v:
        return v
    return _FALLBACK
