"""Give a time to "all-day" events whose description states one.

Many sources only fill the date field and put the hour in prose: "godz. 18:00", "start 19.30",
"18:00-21:00". A one-day all-day event with exactly one such time (or one range) gets it.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .models import Event

RANGE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*[-–—]\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b")
CUED = re.compile(r"(?:godz\.?|godzin[aie]|o\s+godz\.?|start(?:\s+o)?|pocz[aą]tek|od)\s*:?\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b", re.I)
BARE = re.compile(r"(?<![\d.:/-])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")


def infer_time(ev: Event) -> bool:
    if not ev.all_day or (ev.end and ev.end.date() != ev.start.date()):
        return False
    text = f"{ev.title}\n{ev.description or ''}"
    ranges = {m.groups() for m in RANGE.finditer(text)}
    if len(ranges) == 1:
        h1, m1, h2, m2 = map(int, next(iter(ranges)))
        _set(ev, h1, m1)
        end = ev.start.replace(hour=h2, minute=m2)
        ev.end = end if end > ev.start else end + timedelta(days=1)
        return True
    for rx in (CUED, BARE):
        times = {(int(h), int(m)) for h, m in rx.findall(text)}
        if len(times) == 1:
            _set(ev, *next(iter(times)))
            return True
        if len(times) > 1:
            return False  # several different hours: cannot tell which one is the start
    return False


def _set(ev: Event, h: int, m: int) -> None:
    ev.start = ev.start.replace(hour=h, minute=m, second=0, microsecond=0)
    ev.end = None
    ev.all_day = False
