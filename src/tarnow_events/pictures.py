"""Pictures inside an event's description and telling one picture from another (sizes of one WordPress upload)."""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlsplit


def logo_like(img: str) -> bool:
    """A site's mark, not an event's picture: SVG (posters are photos; og:image cannot even be SVG) or "logo"
    in the file name. Sites that put their logo in og:image (Gemini Park) still have the post's picture."""
    name = urlsplit(img).path.lower().rsplit("/", 1)[-1]
    return name.endswith(".svg") or "logo" in name


# WordPress serves one upload under several names: plakat.jpg, plakat-743x1024.jpg, plakat-scaled.jpg,
# plakat-scaled-e1792482722451.jpg (edited in the media library).
_WP_VARIANT = re.compile(r"(?:-\d+x\d+|-scaled|-e\d{9,})+(?=\.\w+$)")
_REUPLOAD = re.compile(r"-\d{1,2}(?=\.\w+$)")


def _key(url: str, loose: bool) -> str:
    # The file name only: a description often embeds the organiser's original (csm.tarnow.pl/uploads/…/x.jpg)
    # of the poster the calendar keeps a copy of (kultura.tarnow.pl/wp-content/uploads/x.jpg).
    parts = urlsplit(url)
    name = _WP_VARIANT.sub("", parts.path.rsplit("/", 1)[-1].lower())
    if loose:  # the same file uploaded again: plakat-1.jpg
        name = _REUPLOAD.sub("", name)
    return f"{name}?{parts.query}" if parts.query else name


def same_picture(a: str | None, b: str | None, loose: bool = False) -> bool:
    """The same picture, maybe in another size or on another site. loose: also when uploaded again (-1, -2):
    for the poster, not among the extra pictures, where foto-1.jpg and foto-2.jpg are two different ones."""
    return bool(a and b) and _key(a, loose) == _key(b, loose)


IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
MAX_IMAGES = 8


def content_images(text: str | None, base: str) -> list[str]:
    """The pictures inside an event's description HTML (a programme, a price list, more posters), largest
    size of each. Icons, emoji, tracking pixels and logos are left out."""
    found: list[str] = []
    for tag in IMG_TAG.findall(text or ""):
        a = {m[0].lower(): html.unescape(m[1] or m[2] or m[3]) for m in _ATTR.findall(tag)}
        if any(a.get(d, "").isdigit() and int(a[d]) < 150 for d in ("width", "height")) or "emoji" in a.get("class", ""):
            continue
        best, best_w = a.get("data-src") or a.get("data-lazy-src") or a.get("src") or "", 0
        for cand in (a.get("data-srcset") or a.get("srcset") or "").split(","):
            parts = cand.split()
            if len(parts) == 2 and parts[1][:-1].isdigit() and parts[1].endswith("w") and int(parts[1][:-1]) > best_w:
                best, best_w = parts[0], int(parts[1][:-1])
        url = urljoin(base, best.strip()) if best.strip() and not best.startswith("data:") else None
        if url and not logo_like(url) and not any(same_picture(url, f) for f in found):
            found.append(url)
    return found[:MAX_IMAGES]
