from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")

# Tarnów Rynek - the centre of the 50 km radius.
TARNOW_LAT = 50.0121
TARNOW_LON = 20.9858

CATEGORIES = {
    "koncerty": "Koncerty i muzyka",
    "teatr": "Teatr, kabaret, stand-up",
    "kino": "Kino i film",
    "sport": "Sport i rekreacja",
    "wystawy": "Wystawy i muzea",
    "dzieci": "Dzieci i rodzina",
    "spotkania": "Spotkania, wykłady, warsztaty, spacery",
    "imprezy": "Imprezy, kluby, puby",
    "festiwale": "Festiwale, jarmarki, plenery",
    "uroczystosci": "Uroczystości religijne i patriotyczne",
    "inne": "Inne",
}


@dataclass
class SourceRef:
    """One place an event was seen. Every published event keeps all of them."""

    source_id: str
    source_name: str
    url: str


@dataclass
class Event:
    title: str
    start: datetime  # tz-aware; for all-day events the time part is 00:00 local
    source: SourceRef
    end: datetime | None = None
    all_day: bool = False
    description: str | None = None
    location_name: str | None = None
    address: str | None = None
    town: str | None = None
    lat: float | None = None
    lon: float | None = None
    image: str | None = None
    price: str | None = None
    tickets: str | None = None  # where to buy tickets (a ticket shop's page for this event)
    categories: list[str] = field(default_factory=list)
    cancelled: bool = False
    # Filled by dedup: every source that reported this event, primary first.
    sources: list[SourceRef] = field(default_factory=list)
    uid: str | None = None
    # Source priority: official organiser > venue > aggregator. Higher wins on merge.
    priority: int = 50
    # False for high-volume sources (cinema showtimes): category/source feeds only, not the main ones.
    in_main: bool = True
    # Filled by dedup: (source id, source name, its own description) of every merged source that had one,
    # primary first. describe.py writes one description from them. Not published (events.json, archive).
    source_texts: list[tuple[str, str, str]] = field(default_factory=list)
    # The description is not the whole story: it was cut (by us or the source), shortened by AI, or the main
    # source asks for it (read_more: always). The page then links "Czytaj dalej" to the main source.
    desc_more: bool = False

    @property
    def day(self) -> date:
        return self.start.astimezone(TZ).date()

    def all_sources(self) -> list[SourceRef]:
        return self.sources or [self.source]

    def location_line(self) -> str | None:
        parts = [p for p in (self.location_name, self.address, self.town) if p]
        # Avoid "Teatr, ul. X, Tarnów, Tarnów" when the address already ends with the town.
        out: list[str] = []
        for p in parts:
            if not any(p.lower() in o.lower() for o in out):
                out.append(p)
        return ", ".join(out) or None
