"""The link-preview mock storyboard-preview.py renders after a preview with `card`.

storyboard__preview's `card` block (conductor storyboard/card/draft.ts) says
what a posted link to the storyboard will show once the open act is
published: card.og {site_name, title, description, image_alt}, which image a
member link shows (member_preview.image: cover | summary) and, when the call
passed `card`, summary_svg (the summary card's SVG, the bytes card.png
serves). mock_html() lays that out as a Slack-like attachment so Claude can
Read a picture of the unfurl: a 520 px card with a 4 px left bar, the site
name, a link-styled title, the description clamped to 3 lines, the image at
360x189, the same image as a 240 px thumbnail, and a footer saying it is a
mock.

The page is static: no script, no external reference (the image is a data:
URI), every string HTML-escaped. The renderer screenshots it with scripts
disabled and the network locked.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Optional

MOCK_FOOTER = "Mock — fonts differ from the card Cardinal serves"
MOCK_HTML_NAME = "unfurl-mock.html"
MOCK_PNG_NAME = "unfurl-mock.png"
MOCK_VIEWPORT = "560x640"
MOCK_DPR = "2"
# A cover PNG bigger than this is not inlined (the server caps an upload at
# 2 MiB; a render over it cannot be the served image either).
MAX_COVER_BYTES = 2 * 1024 * 1024

_STYLE = """
html,body{margin:0;background:#fff;color:#1d1c1d;
  font:15px/1.46668 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{padding:20px}
.att{width:520px;display:flex;gap:12px}
.bar{width:4px;border-radius:8px;background:#dddddd;flex:none}
.body{min-width:0;flex:1}
.site{font-weight:700;font-size:15px}
.title{color:#1264a3;font-weight:700;text-decoration:none;display:block;margin-top:2px}
.desc{margin-top:2px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}
.img{display:block;width:360px;height:189px;margin-top:8px;border-radius:8px;border:1px solid #e8e8e8;
  object-fit:cover}
.thumbrow{display:flex;align-items:center;gap:12px;margin-top:14px;color:#616061;font-size:13px}
.thumb{width:240px;height:126px;border-radius:6px;border:1px solid #e8e8e8;object-fit:cover}
.foot{margin-top:18px;color:#616061;font-size:12px;border-top:1px solid #e8e8e8;padding-top:8px;width:520px}
"""


def _s(value, limit: int = 1000) -> str:
    s = value if isinstance(value, str) else ""
    return s[:limit]


def mock_image(card: dict, cover_png: Optional[Path]) -> tuple:
    """-> (data URI | None, "cover" | "summary card" | None). The cover PNG
    when member_preview.image is "cover" and the file exists, else the
    summary card SVG when the block carries it."""
    member = card.get("member_preview") if isinstance(card.get("member_preview"), dict) else {}
    if member.get("image") == "cover" and cover_png is not None:
        try:
            data = cover_png.read_bytes() if cover_png.stat().st_size <= MAX_COVER_BYTES else b""
        except OSError:
            data = b""
        if data.startswith(b"\x89PNG"):
            return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), "cover"
    svg = card.get("summary_svg")
    if isinstance(svg, str) and svg.strip():
        return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii"), "summary card"
    return None, None


def mock_html(card: dict, cover_png: Optional[Path]) -> Optional[tuple]:
    """-> (html, image label) for the card block, or None when there is no
    image to show (no summary_svg and no usable cover render)."""
    if not isinstance(card, dict):
        return None
    uri, label = mock_image(card, cover_png)
    if uri is None:
        return None
    og = card.get("og") if isinstance(card.get("og"), dict) else {}
    esc = lambda v, n=1000: html.escape(_s(v, n), quote=True)  # noqa: E731
    site = esc(og.get("site_name") or "Cardinal Storyboards", 100)
    title = esc(og.get("title"), 400)
    desc = esc(og.get("description"), 400)
    alt = esc(og.get("image_alt"), 300)
    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
        "style-src 'unsafe-inline'\">"
        f"<title>Link preview mock</title><style>{_STYLE}</style></head><body><div class=\"wrap\">"
        "<div class=\"att\"><div class=\"bar\"></div><div class=\"body\">"
        f"<div class=\"site\">{site}</div>"
        f"<a class=\"title\">{title}</a>"
        f"<div class=\"desc\">{desc}</div>"
        f"<img class=\"img\" src=\"{uri}\" alt=\"{alt}\" width=\"360\" height=\"189\">"
        "</div></div>"
        f"<div class=\"thumbrow\"><img class=\"thumb\" src=\"{uri}\" alt=\"\" width=\"240\" height=\"126\">"
        "<span>240 px thumbnail</span></div>"
        f"<div class=\"foot\">{html.escape(MOCK_FOOTER)} · image: {html.escape(label)}</div>"
        "</div></body></html>"
    )
    return page, label
