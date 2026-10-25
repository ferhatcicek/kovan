"""Güvenli Markdown işleme: HTML girişi kapalı + nh3 beyaz liste temizleme."""
from __future__ import annotations

import nh3
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")

_TAGS = {
    "a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "s", "code", "pre",
    "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "img",
}
_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title"}, "code": {"class"}, "th": {"align"},
          "td": {"align"}, "ol": {"start"}}


def render_markdown(text: str) -> str:
    html = _md.render(text)
    return nh3.clean(html, tags=_TAGS, attributes=_ATTRS, url_schemes={"http", "https", "mailto"},
                     link_rel="noopener noreferrer nofollow")


def first_paragraph(text: str, limit: int = 160) -> str:
    """README'den kart açıklaması: başlık/rozet/kod olmayan ilk düz paragraf."""
    in_code = False
    for block in text.replace("\r\n", "\n").split("\n\n"):
        stripped = block.strip()
        if stripped.startswith("```"):
            in_code = stripped.count("```") % 2 == 1 and not in_code
            continue
        if in_code or not stripped:
            continue
        if stripped.startswith(("#", "!", "[!", "<", "|", "-", "*", ">", "=")):
            continue
        line = " ".join(stripped.split())
        return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
