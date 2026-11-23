from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from icalendar import Calendar, Event as VEvent, vGeo

from .models import CATEGORIES, Event

log = logging.getLogger(__name__)

REFRESH = "PT6H"


def build_calendar(name: str, description: str, events: list[Event]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//tarnow-wydarzenia//PL")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-caldesc", description)
    cal.add("x-wr-timezone", "Europe/Warsaw")
    # Hints for subscribing clients; Google ignores them and polls on its own schedule (~12-24h).
    cal.add("refresh-interval", timedelta(hours=6), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", REFRESH)

    stamp = datetime.now(timezone.utc)
    for e in events:
        ve = VEvent()
        ve.add("uid", e.uid)
        ve.add("dtstamp", stamp)
        if e.all_day:
            ve.add("dtstart", e.start.date())
            last = (e.end or e.start).date()
            ve.add("dtend", last + timedelta(days=1))  # exclusive
        else:
            ve.add("dtstart", e.start)
            if e.end:
                ve.add("dtend", e.end)
        ve.add("summary", ("[ODWOŁANE] " if e.cancelled else "") + e.title)
        if e.cancelled:
            ve.add("status", "CANCELLED")
        loc = e.location_line()
        if loc:
            ve.add("location", loc)
        if e.lat is not None and e.lon is not None:
            ve.add("geo", vGeo((e.lat, e.lon)))
        refs = e.all_sources()
        ve.add("url", refs[0].url)
        ve.add("description", _description(e))
        ve.add("categories", [CATEGORIES.get(c, c) for c in e.categories])
        if e.image:
            ve.add("attach", e.image, parameters={"FMTTYPE": "image/jpeg"})
        cal.add_component(ve)

    try:
        cal.add_missing_timezones()  # icalendar >= 6.1: emits VTIMEZONE for Europe/Warsaw
    except AttributeError:
        pass
    return cal.to_ical()


def _description(e: Event) -> str:
    parts: list[str] = []
    if e.description:
        parts.append(e.description[:3000])
    if e.price:
        parts.append(f"Bilety: {e.price}")
    refs = e.all_sources()
    parts.append("Źródła:\n" + "\n".join(f"• {r.source_name}: {r.url}" for r in refs))
    return "\n\n".join(parts)
