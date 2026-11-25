from __future__ import annotations

import json
import logging
import re
from datetime import date

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import http
from ..categorize import detect
from .base import Source

log = logging.getLogger(__name__)

FRAGMENT = "https://www.bilety24.pl/search/repertoire"
# "/kino/1225-jestem-rocky-168244?id=1018893": 1225 is the organiser, for films the cinema itself.
DEALER_RE = re.compile(r"^/[\w-]+/(\d+)-")
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def venue_from_page(url: str) -> dict | None:
    """The film page's schema.org Event names the venue: {"name": …, "address": street}."""
    try:
        html = http.get(url).text
    except Exception as exc:  # one missing venue must not cost the whole source
        log.warning("bilety24: venue lookup failed for %s: %s", url, exc)
        return None
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            loc = item.get("location") if isinstance(item, dict) else None
            if isinstance(loc, dict) and loc.get("name"):
                addr = loc.get("address")
                return {"name": " ".join(str(loc["name"]).split()),
                        "address": addr.get("streetAddress") if isinstance(addr, dict) else None}
    return None
TITLE_RE = re.compile(
    r"^([^:]+):\s*(.+?) - (\d{4}-\d{2}-\d{2})(?: (\d{1,2}:\d{2}))?(?: - (\d{4}-\d{2}-\d{2}))? - (.+)$"
)


class Bilety24Source(Source):
    """Bilety24.pl search with its own radius filter (location + distance in km).

    The search results come as an HTML fragment of cards; each card is one date ("term") with
    a modal holding "DD.MM.YYYY HH:MM, Town".

    Options:
      search_location (default "Tarnów"), distance (default 50), max_pages (default 30)
      max_span_days: skip offers running longer than this, e.g. museum entry tickets (default 45)
      venues: organiser id -> {name, address} for films. Cards only say the town; a short cinema name
        gives screenings their place (page addresses, map pins at the cinema). Unknown ids are read once
        per run from a film's own page (its long official name).
    """

    type = "bilety24"

    def fetch(self):
        o = self.cfg.options
        venues: dict[str, dict | None] = {str(k): v for k, v in (o.get("venues") or {}).items()}
        seen: set[str] = set()
        for page in range(1, int(o.get("max_pages", 30)) + 1):
            html = http.get(FRAGMENT, params={
                "page": page,
                "search[location]": o.get("search_location", "Tarnów"),
                "search[distance]": o.get("distance", 50),
            }).text
            cards = HTMLParser(html).css("div.k_polecane")
            if not cards:
                break
            new = 0
            for card in cards:
                link = card.css_first("a.full-info")
                if not link:
                    continue
                href = link.attributes.get("href") or ""
                if href in seen:
                    continue
                seen.add(href)
                new += 1
                # Every card's link title: "Film: LALKA - 2026-11-24 20:00 - Nowy Sącz" or
                # "Wydarzenie: Zwiedzanie wystaw - 2026-11-23 - 2027-12-31 - Limanowa".
                m = TITLE_RE.match(" ".join((link.attributes.get("title") or "").split()))
                if not m:
                    continue
                kind, name, day, hm, last_day, town = m.groups()
                if last_day and (date.fromisoformat(last_day) - date.fromisoformat(day)).days > int(o.get("max_span_days", 45)):
                    continue  # standing offer (museum entry, workshops "until 2027")
                cat = card.css_first(".kategoria")
                # Poster sits in a CSS background; the bare ".../nf_repertoire_medium_scale" is a placeholder.
                bg = card.css_first(".k_obrazek_promowane[style*='url(']")
                poster = None
                if bg:
                    pm = re.search(r"url\(['\"]?([^'\")]+)", bg.attributes.get("style") or "")
                    if pm and not pm.group(1).rstrip("/").endswith("nf_repertoire_medium_scale"):
                        poster = pm.group(1)
                cat_text = (cat.text() if cat else "") + " " + kind
                is_film = "film" in cat_text.lower()
                venue = None
                if is_film and (dm := DEALER_RE.match(href)):
                    if dm.group(1) not in venues:
                        venues[dm.group(1)] = venue_from_page("https://www.bilety24.pl" + href)
                        log.info("bilety24: organiser %s not in sources.yaml venues, page says %s",
                                 dm.group(1), venues[dm.group(1)])
                    venue = venues[dm.group(1)]
                ev = self.make_event(
                    title=name,
                    start=f"{day}T{hm}" if hm else day,
                    end=last_day if last_day and last_day != day else None,
                    url=href,
                    town=town,
                    location_name=(venue or {}).get("name"),
                    address=(venue or {}).get("address"),
                    price=(card.css_first(".cenainfo").text().strip() if card.css_first(".cenainfo") else None),
                    image=poster,
                    categories=["kino"] if is_film else (detect(cat_text)[:1] or None),
                )
                if ev and is_film:
                    ev.in_main = False  # screenings go to kino.ics only
                if ev:
                    yield ev
            if not new:
                break
