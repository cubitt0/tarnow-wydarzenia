"""Read the events' own pages once and take from them what the sources did not give us:
the picture (og:image & co.), a ticket-shop link (see tickets.py), and from the page's text the
start time of an "all-day" event and whether entry is free. A news listing often shows only the
article's lead ("Dziś spotkanie Mamoteki"); the hour and "Wstęp wolny" are in the article itself.
Text hints are used only when the page is about this one event: no other event links to it and its
heading shares a word with the event's title.

Runs after dedup, so each real-world event is looked up once. Results (including "nothing
found") are cached for 30 days. A picture that one site returns for 3+ different pages is its
logo or a default banner, not the event's poster, and is ignored for that site. The dated pages
of one repeated event (Tribe calendars: /wydarzenie/opowiesc-wigilijna-2/2026-12-11/) count as one page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from unidecode import unidecode

from . import http
from .models import Event
from .sources.base import is_machine_url
from .sources.jsonld import extract_jsonld_events
from .tickets import STOP, choose_ticket, free_ticket, is_ticket_url, page_ticket_candidates, says_free
from .timeinfer import apply_time, eligible, time_candidates

log = logging.getLogger(__name__)

TTL = 30 * 86400
CHECK_EVERY = 20 * 3600  # a little under a day, so the daily build always re-checks
SKIP_HOSTS = {"www.facebook.com", "facebook.com", "api.lu.ma", "calendar.google.com"}
LOGO_REPEATS = 3
_OCCURRENCE = re.compile(r"(?:-\d+)?(?:/\d{4}-\d{2}-\d{2})?/?$")


def event_key(page: str) -> str:
    """The page without its occurrence date and WordPress's "-2" slug suffix: every show of a
    repeated event (and its re-added copies) shares one poster, which is not the site's logo."""
    return _OCCURRENCE.sub("", page)  # query kept: ?id=5 and ?id=6 are different events


ARTICLE_TYPES = {"Article", "BlogPosting", "NewsArticle", "WebPage", "ImageObject"}
PAGE_VERSION = 2  # bump when page_image() learns something: pages read before are read again for a picture


def logo_like(img: str) -> bool:
    """A site's mark, not an event's picture: SVG (posters are photos; og:image cannot even be SVG) or "logo"
    in the file name. Sites that put their logo in og:image (Gemini Park) still have the post's picture."""
    name = urlparse(img).path.lower().rsplit("/", 1)[-1]
    return name.endswith(".svg") or "logo" in name


def _jsonld_images(html: str) -> list[str]:
    """Pictures the page's structured data gives for itself: an article's image (SEO plugins put the post's
    picture there), the page's primary image."""
    tree = HTMLParser(html)
    found: list[str] = []

    def url_of(v) -> str | None:
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            v = v.get("url") or v.get("contentUrl")
        return v if isinstance(v, str) and v.strip() else None

    def walk(node) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            if ({*t} if isinstance(t, list) else {t}) & ARTICLE_TYPES:
                for key in ("image", "primaryImageOfPage", "thumbnailUrl", "contentUrl"):
                    if (img := url_of(node.get(key))):
                        found.append(img)
            walk(node.get("@graph"))

    for script in tree.css('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.text() or ""))
        except ValueError:
            continue
    return found


def page_image(html: str, url: str) -> str | None:
    """The event's picture on its page: og:image & co., then the structured data's (an event's, then the
    article's), then the article's first real picture. Logo-like ones are passed over."""
    tree = HTMLParser(html)
    candidates: list[str] = []
    for sel, attr in (('meta[property="og:image"]', "content"), ('meta[property="og:image:url"]', "content"),
                      ('meta[name="twitter:image"]', "content"), ('link[rel="image_src"]', "href")):
        node = tree.css_first(sel)
        if node and (node.attributes.get(attr) or "").strip():
            candidates.append(node.attributes[attr].strip())
    for ev in extract_jsonld_events(html):
        img = ev.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, str) and img:
            candidates.append(img)
    candidates += _jsonld_images(html)
    for node in tree.css("article img, main img, .entry-content img, .post img"):
        a = node.attributes
        src = a.get("src") or a.get("data-src") or a.get("data-lazy-src")
        small = any((a.get(d) or "").isdigit() and int(a[d]) < 150 for d in ("width", "height"))
        if src and not src.startswith("data:") and not small:
            candidates.append(src)
            break
    pictures = [urljoin(url, c) for c in candidates]
    return next((p for p in pictures if not logo_like(p)), None)


# Not the page's own content: menus, sidebars with other articles, cookie bars.
TEXT_DROP = "script, style, noscript, svg, iframe, form, nav, footer, header, aside, template"
TEXT_CHROME = re.compile(r"sidebar|widget|cookie|breadcrumb|related|polecane|social|menu|comment|komentarz", re.I)


def page_text(html: str) -> tuple[str, str]:
    """(heading, main text) of an event's page, without menus and sidebars."""
    tree = HTMLParser(html)
    heading = ""
    for sel, attr in (('meta[property="og:title"]', "content"), ("h1", None), ("title", None)):
        node = tree.css_first(sel)
        heading = (node.attributes.get(attr) or "") if (node and attr) else (node.text() if node else "")
        if heading.strip():
            break
    for n in tree.css(TEXT_DROP):
        n.decompose()
    for n in reversed(tree.css("[class], [id]")):  # innermost first: no touching a removed node's children
        attrs = n.attributes
        if n.tag not in ("html", "body", "main", "article") and \
                TEXT_CHROME.search(f"{attrs.get('class') or ''} {attrs.get('id') or ''}"):
            n.decompose()
    root = tree.body or tree.root
    return " ".join(heading.split()), " ".join((root.text(separator=" ") if root else "").split())


def page_hints(html: str) -> dict:
    """Cache fields: h = start times the page states (cued ones only), f = says entry is free, n = heading."""
    heading, text = page_text(html)
    return {"h": time_candidates(text, bare=False), "f": says_free(text), "n": heading[:200]}


def _stems(title: str) -> set[str]:
    # Five-letter stems, so "Mamoteki" in the title matches "Mamoteka" on the page.
    return {w[:5] for w in re.findall(r"[a-z0-9]+", unidecode(title).lower()) if len(w) >= 5 and w not in STOP}


def about(ev: Event, heading: str | None) -> bool:
    return bool(heading) and bool(_stems(ev.title) & _stems(heading))


def enrich_images(events: list[Event], cache_dir: Path, listing_urls: set[str], max_fetches: int = 250,
                  max_checks: int = 400) -> dict:
    """Fill ev.image and ev.tickets from the events' pages.
    Cache entry: {i: image, t: fetched at, k: ticket links, v: image last seen alive}."""
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
            per_host[urlparse(page).hostname or ""][entry["i"]].add(event_key(page))

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

    def wants_text(ev: Event) -> bool:
        return not ev.cancelled and (eligible(ev) or not ev.price)

    # A page several events link to (a daily digest, a programme) cannot tell whose time it states.
    linked: dict[str, int] = defaultdict(int)
    for ev in events:
        for page in {ref.url for ref in ev.all_sources()}:
            linked[page] += 1

    # Sites rename re-uploaded pictures (BWA adds a new hash), so a cached URL can start to 404
    # within the 30 days. Check the pictures in use once a day; a dead one sends its page to be re-read.
    to_check: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        if not wants(ev)[0]:
            continue
        for ref in ev.all_sources():
            entry = cache.get(ref.url)
            if entry and entry.get("i") and readable(ref.url) and now - entry.get("v", entry["t"]) > CHECK_EVERY:
                to_check[entry["i"]].append(ref.url)
    checks = list(to_check)[:max_checks]
    dead_images = 0
    with ThreadPoolExecutor(8) as pool:
        for img, dead in zip(checks, pool.map(http.gone, checks)):
            dead_images += dead
            for page in to_check[img]:
                if dead:  # forget the picture and re-read the page in this run
                    cache[page].update(i=None, t=0)
                    per_host[urlparse(page).hostname or ""][img].discard(event_key(page))
                else:
                    cache[page]["v"] = now

    # Pages to (re)read, in event order, up to the per-run cap; the rest wait for the next runs.
    todo: list[str] = []
    for ev in events:
        need_img, need_tk = wants(ev)
        need_text = wants_text(ev)
        if not (need_img or need_tk or need_text):
            continue
        for ref in ev.all_sources():
            page = ref.url
            if not readable(page) or page in todo:
                continue
            entry = cache.get(page)
            # Entries from before text hints were read have no "h": their pages get one more look.
            if entry is None or now - entry["t"] > TTL or (need_tk and "k" not in entry) \
                    or (need_text and "h" not in entry and linked[page] == 1) \
                    or (need_img and entry.get("pv", 1) < PAGE_VERSION  # read before page_image() improved,
                        and (not entry.get("i") or logo_like(entry["i"]) or is_logo(page, entry["i"]))):  # with no picture
                if len(todo) < max_fetches:
                    todo.append(page)

    def read(page: str) -> tuple[str, dict]:
        try:
            html = http.get(page, retries=0).text
            return page, {"i": page_image(html, page), "t": now, "k": page_ticket_candidates(html, page),
                          "pv": PAGE_VERSION, **page_hints(html)}
        except Exception:
            old = cache.get(page, {})
            return page, {"i": old.get("i"), "t": now, "k": old.get("k", []), "pv": PAGE_VERSION,
                          **{k: old[k] for k in ("h", "f", "n") if k in old}}

    with ThreadPoolExecutor(8) as pool:
        for page, entry in pool.map(read, todo):
            cache[page] = entry
            if entry["i"]:
                per_host[urlparse(page).hostname or ""][entry["i"]].add(event_key(page))

    filled = found = timed = free_entry = 0
    for ev in events:
        need_img, need_tk = wants(ev)
        if need_img:
            ev.image = None
        for ref in ev.all_sources():
            entry = cache.get(ref.url)
            if not entry or not readable(ref.url):
                continue
            if need_img and not ev.image and entry.get("i") and not is_logo(ref.url, entry["i"]) \
                    and not logo_like(entry["i"]):
                ev.image = entry["i"]
                filled += 1
            if need_tk and not ev.tickets and entry.get("k"):
                ev.tickets = choose_ticket(ev, entry["k"])
                found += bool(ev.tickets)
            if linked[ref.url] == 1 and about(ev, entry.get("n")):
                if eligible(ev) and len(entry.get("h") or []) == 1:
                    timed += apply_time(ev, entry["h"][0])
                if not ev.price and entry.get("f") and not ev.tickets:
                    ev.price = "Wstęp wolny"
                    free_entry += 1
    path.write_text(json.dumps(cache))
    return {"image_pages_fetched": len(todo), "images_filled": filled, "dead_images": dead_images,
            "tickets_from_sources": free, "tickets_from_pages": found, "times_from_pages": timed,
            "free_from_pages": free_entry}
