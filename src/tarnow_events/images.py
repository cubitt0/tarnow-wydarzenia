"""Read the events' own pages once and take from them what the sources did not give us:
the picture (og:image & co.) and a ticket-shop link (see tickets.py).

Runs after dedup, so each real-world event is looked up once. Results (including "nothing
found") are cached for 30 days. A picture that one site returns for 3+ different pages is its
logo or a default banner, not the event's poster, and is ignored for that site.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import http
from .models import Event
from .sources.base import is_machine_url
from .sources.jsonld import extract_jsonld_events
from .tickets import choose_ticket, free_ticket, is_ticket_url, page_ticket_candidates

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


def enrich_images(events: list[Event], cache_dir: Path, listing_urls: set[str], max_fetches: int = 250) -> dict:
    """Fill ev.image and ev.tickets from the events' pages. Cache entry: {i: image, t: fetched at, k: ticket links}."""
    path = cache_dir / "images.json"
    try:
        raw: dict = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    # Entries written before ticket links were read are [image, time]: their pages get one more look.
    cache: dict[str, dict] = {k: (v if isinstance(v, dict) else {"i": v[0], "t": v[1]}) for k, v in raw.items()}
    now = time.time()

    # Which image does each site hand out for many different pages? That one is the logo.
    per_host: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for page, entry in cache.items():
        if entry.get("i"):
            per_host[urlparse(page).hostname or ""][entry["i"]].add(page)

    def is_logo(page: str, img: str) -> bool:
        return len(per_host[urlparse(page).hostname or ""][img]) >= LOGO_REPEATS

    def readable(page: str) -> bool:
        host = urlparse(page).hostname or ""
        return page.startswith("http") and host not in SKIP_HOSTS and page not in listing_urls \
            and not is_machine_url(page) and not is_ticket_url(page)

    free = 0
    for ev in events:
        if not ev.tickets:
            ev.tickets = free_ticket(ev)
            free += bool(ev.tickets)

    def wants(ev: Event) -> tuple[bool, bool]:
        need_img = not ev.image or is_logo(ev.source.url, ev.image)
        return need_img, not ev.tickets and not ev.cancelled

    # Pages to (re)read, in event order, up to the per-run cap; the rest wait for the next runs.
    todo: list[str] = []
    for ev in events:
        need_img, need_tk = wants(ev)
        if not (need_img or need_tk):
            continue
        for ref in ev.all_sources():
            page = ref.url
            if not readable(page) or page in todo:
                continue
            entry = cache.get(page)
            if entry is None or now - entry["t"] > TTL or (need_tk and "k" not in entry):
                if len(todo) < max_fetches:
                    todo.append(page)

    def read(page: str) -> tuple[str, dict]:
        try:
            html = http.get(page, retries=0).text
            return page, {"i": page_image(html, page), "t": now, "k": page_ticket_candidates(html, page)}
        except Exception:
            old = cache.get(page, {})
            return page, {"i": old.get("i"), "t": now, "k": old.get("k", [])}

    with ThreadPoolExecutor(8) as pool:
        for page, entry in pool.map(read, todo):
            cache[page] = entry
            if entry["i"]:
                per_host[urlparse(page).hostname or ""][entry["i"]].add(page)

    filled = found = 0
    for ev in events:
        need_img, need_tk = wants(ev)
        if need_img:
            ev.image = None
        for ref in ev.all_sources():
            entry = cache.get(ref.url)
            if not entry or not readable(ref.url):
                continue
            if need_img and not ev.image and entry.get("i") and not is_logo(ref.url, entry["i"]):
                ev.image = entry["i"]
                filled += 1
            if need_tk and not ev.tickets and entry.get("k"):
                ev.tickets = choose_ticket(ev, entry["k"])
                found += bool(ev.tickets)
    path.write_text(json.dumps(cache))
    return {"image_pages_fetched": len(todo), "images_filled": filled, "tickets_from_sources": free, "tickets_from_pages": found}
