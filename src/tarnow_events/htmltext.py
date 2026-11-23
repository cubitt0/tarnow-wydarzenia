"""HTML → compact text with links kept as [text](href), for feeding pages to the LLM."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

DROP = "script, style, noscript, svg, iframe, form, nav, footer, header, template"
BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "br", "dd", "dt", "time"}


def html_to_text(html: str, base_url: str, selector: str | None = None) -> str:
    tree = HTMLParser(html)
    root: Node | None = None
    if selector:
        nodes = tree.css(selector)
        if nodes:
            # Concatenate all matches - e.g. every ".event-item" on a listing page.
            return "\n\n".join(_render(n, base_url) for n in nodes).strip()
    for n in tree.css(DROP):
        n.decompose()
    root = tree.css_first("main") or tree.body or tree.root
    return _render(root, base_url) if root else ""


def _render(node: Node, base_url: str) -> str:
    out: list[str] = []

    def walk(n: Node) -> None:
        tag = n.tag
        if tag == "-text":
            out.append(n.text(deep=False))
            return
        if tag in ("script", "style", "noscript", "svg"):
            return
        if tag == "a":
            text = " ".join(n.text().split())
            href = n.attributes.get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")) and text:
                out.append(f"[{text}]({urljoin(base_url, href)})")
            elif text:
                out.append(text)
            return
        if tag == "img":
            alt = (n.attributes.get("alt") or "").strip()
            if alt:
                out.append(f" {alt} ")
            return
        if tag == "time" and n.attributes.get("datetime"):
            out.append(f" [{n.attributes['datetime']}] ")
        if tag in BLOCK:
            out.append("\n")
        child = n.child
        while child is not None:
            walk(child)
            child = child.next
        if tag in BLOCK:
            out.append("\n")

    walk(node)
    text = "".join(out)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def chunks(text: str, size: int = 25000) -> list[str]:
    """Split on paragraph boundaries so one event is not cut in half."""
    if len(text) <= size:
        return [text]
    parts, cur, cur_len = [], [], 0
    for para in text.split("\n\n"):
        if cur_len + len(para) > size and cur:
            parts.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(para[:size])
        cur_len += len(para) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return parts
