"""Give events without an image the picture from their own event page (og:image & co.).

Runs after dedup, so each real-world event is looked up once. Results (including "no image")
are cached for 30 days. A picture that one site returns for 3+ different pages is its logo or
a default banner, not the event's poster, and is ignored for that site.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import http
from .models import Event
from .sources.jsonld import extract_jsonld_events

log = logging.getLogger(__name__)

TTL = 30 * 86400
SKIP_HOSTS = {"www.facebook.com", "facebook.com", "api.lu.ma", "calendar.google.com"}
LOGO_REPEATS = 3


def page_image(html: str, url: str) -> str | None:
    tree = HTMLParser(html)
    for sel, attr in (('meta[property="og:image"]', "content"), ('meta[property="og:image:url"]', "content"),
                      ('meta[name="twitter:image"]', "content"), ('link[rel="image_src"]', "href")):
        node = tree.css_first(sel)
        if node and (node.attributes.get(attr) or "").strip():
            return urljoin(url, node.attributes[attr].strip())
    for ev in extract_jsonld_events(html):
        img = ev.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, str) and img:
            return urljoin(url, img)
    return None


def enrich_images(events: list[Event], cache_dir: Path, listing_urls: set[str], max_fetches: int = 150) -> dict:
    path = cache_dir / "images.json"
    try:
        cache: dict[str, list] = json.loads(path.read_text())
    except (OSError, ValueError):
        cache = {}
    now = time.time()

    # Which image does each site hand out for many different pages? That one is the logo.
    per_host: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for page, (img, _ts) in cache.items():
        if img:
            per_host[urlparse(page).hostname or ""][img].add(page)

    def is_logo(page: str, img: str) -> bool:
        return len(per_host[urlparse(page).hostname or ""][img]) >= LOGO_REPEATS

    fetched = filled = 0
    for ev in events:
        if ev.image and not is_logo(ev.source.url, ev.image):
            continue
        ev.image = None
        for ref in ev.all_sources():
            page = ref.url
            host = urlparse(page).hostname or ""
            if not page.startswith("http") or host in SKIP_HOSTS or page in listing_urls:
                continue
            hit = cache.get(page)
            if hit is None or now - hit[1] > TTL:
                if fetched >= max_fetches:
                    continue
                fetched += 1
                try:
                    img = page_image(http.get(page, retries=0).text, page)
                except Exception:
                    img = None
                cache[page] = [img, now]
                if img:
                    per_host[host][img].add(page)
            img = cache[page][0]
            if img and not is_logo(page, img):
                ev.image = img
                filled += 1
                break
    path.write_text(json.dumps(cache))
    return {"image_pages_fetched": fetched, "images_filled": filled}
