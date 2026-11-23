from __future__ import annotations

import re
from datetime import date

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import http
from ..categorize import detect
from .base import Source

FRAGMENT = "https://www.bilety24.pl/search/repertoire"
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
    """

    type = "bilety24"

    def fetch(self):
        o = self.cfg.options
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
                cat_text = (cat.text() if cat else "") + " " + kind
                is_film = "film" in cat_text.lower()
                ev = self.make_event(
                    title=name,
                    start=f"{day}T{hm}" if hm else day,
                    end=last_day if last_day and last_day != day else None,
                    url=href,
                    town=town,
                    price=(card.css_first(".cenainfo").text().strip() if card.css_first(".cenainfo") else None),
                    categories=["kino"] if is_film else (detect(cat_text)[:1] or None),
                )
                if ev and is_film:
                    ev.in_main = False  # screenings go to kino.ics only
                if ev:
                    yield ev
            if not new:
                break
