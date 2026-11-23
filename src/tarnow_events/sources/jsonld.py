from __future__ import annotations

import json
import logging
import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import http
from .base import Source, absolutize

log = logging.getLogger(__name__)

EVENT_TYPES = {
    "Event", "MusicEvent", "TheaterEvent", "ScreeningEvent", "SportsEvent", "ExhibitionEvent",
    "Festival", "ComedyEvent", "DanceEvent", "ChildrensEvent", "EducationEvent", "LiteraryEvent",
    "SocialEvent", "BusinessEvent", "FoodEvent", "VisualArtsEvent",
}


def extract_jsonld_events(html: str) -> list[dict]:
    tree = HTMLParser(html)
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = set(t) if isinstance(t, list) else {t}
            if types & EVENT_TYPES:
                found.append(node)
                return
            for key in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
                if key in node:
                    walk(node[key])

    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text() or ""
        try:
            walk(json.loads(raw))
        except ValueError:
            # Some CMSes emit trailing commas or several objects back to back.
            try:
                walk(json.loads(re.sub(r",\s*([}\]])", r"\1", raw)))
            except ValueError:
                continue
    return found


class JsonLdSource(Source):
    """Pages that embed schema.org Event JSON-LD (ticketing sites, many modern CMSes).

    Options:
      pages: extra listing URLs to read besides `url`
      follow: regex; links on the listing pages matching it are fetched as detail pages
      max_follow: cap on detail pages per run (default 80)
    """

    type = "jsonld"

    def fetch(self):
        opts = self.cfg.options
        pages = [self.cfg.url, *opts.get("pages", [])]
        follow = re.compile(opts["follow"]) if opts.get("follow") else None
        detail_urls: list[str] = []
        for page in pages:
            html = http.get(page).text
            yield from self._events(html, page)
            if follow:
                for a in HTMLParser(html).css("a[href]"):
                    href = absolutize(a.attributes.get("href"), page)
                    if href and follow.search(href) and href not in detail_urls:
                        detail_urls.append(href)
        for url in detail_urls[: int(opts.get("max_follow", 80))]:
            try:
                yield from self._events(http.get(url).text, url)
            except Exception as e:  # one broken detail page must not sink the source
                log.info("%s: detail %s failed: %s", self.cfg.id, url, e)

    def _events(self, html: str, page_url: str):
        for node in extract_jsonld_events(html):
            loc = node.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            if isinstance(loc, str):
                loc = {"name": loc}
            addr = loc.get("address") or {}
            if isinstance(addr, str):
                addr = {"streetAddress": addr}
            geo = loc.get("geo") or {}
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            price = None
            if offers.get("price") or offers.get("lowPrice"):
                price = f"{offers.get('lowPrice') or offers.get('price')} {offers.get('priceCurrency', 'PLN')}"
            ev = self.make_event(
                title=node.get("name"),
                start=node.get("startDate"),
                end=node.get("endDate"),
                url=node.get("url") or page_url,
                description=node.get("description"),
                location_name=loc.get("name"),
                address=addr.get("streetAddress"),
                town=addr.get("addressLocality"),
                lat=geo.get("latitude"),
                lon=geo.get("longitude"),
                image=image,
                price=price,
                cancelled="Cancelled" in str(node.get("eventStatus", "")),
            )
            if ev:
                yield ev
