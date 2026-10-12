"""The link-preview mock storyboard-preview.py renders after a preview with `card`.

Moved to the shared core (cardinal_core.storyboard_authoring, "The
link-preview mock"): every runtime's preview hook lays out the same
Slack-like attachment. This module re-exports it for the callers and tests
that import it from hooks/.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core.storyboard_authoring import (  # noqa: E402,F401
    MAX_COVER_BYTES,
    MOCK_DPR,
    MOCK_FOOTER,
    MOCK_HTML_NAME,
    MOCK_PNG_NAME,
    MOCK_STYLE as _STYLE,
    MOCK_VIEWPORT,
    mock_html,
    mock_image,
)
