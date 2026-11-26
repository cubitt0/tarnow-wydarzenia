"""Give a time to "all-day" events whose description (or own page) states one.

Many sources only fill the date field and put the hour in prose: "godz. 18:00", "start 19.30",
"18:00-21:00", "rozpocznie się o godz. 17", "od godz. 9 do 14". A one-day all-day event with
exactly one such time (or one range) gets it.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .models import Event

H, M = r"([01]?\d|2[0-3])", r"([0-5]\d)"
GODZ = r"(?:godz\.?|godzin[aieęy])"
RANGE = re.compile(rf"\b{H}[:.]{M}\s*[-–—]\s*{H}[:.]{M}\b")
# "od godz. 9 do 14", "w godz. 10.00-15.00", "godz. 17-19"
SPAN = re.compile(rf"(?:\bod\s+|\bw\s+){GODZ}\s*:?\s*{H}(?:[:.]{M})?\s*(?:do|[-–—])\s*(?:{GODZ}\s*)?{H}(?:[:.]{M})?(?![\d:])", re.I)
# "do godz. 21" is when it ends, not when it starts.
NOT_END = r"(?<![Dd]o )(?<![Dd]o\xa0)"
CUED = re.compile(rf"{NOT_END}(?:{GODZ}|o\s+godz\.?|start(?:\s+o)?|pocz[aą]tek(?:\s+o)?|od)\s*:?\s*{H}[:.]{M}\b", re.I)
HOUR = re.compile(rf"{NOT_END}\b{GODZ}\s*:?\s*{H}(?:[:.]{M})?(?![\d:])", re.I)  # "o godz. 17"
BARE = re.compile(rf"(?<![\d.:/-]){H}:{M}(?![\d:])")


def time_candidates(text: str, bare: bool = True) -> list[str]:
    """Distinct start times stated in the text: ["18:30"] or ["18:00-21:00"]; several = ambiguous.
    bare=False ignores times without a cue word (a page's "26.11.2026, 10:08" is its publication time)."""
    ranges = {_hm(h1, m1) + "-" + _hm(h2, m2) for h1, m1, h2, m2 in RANGE.findall(text)}
    ranges |= {_hm(h1, m1) + "-" + _hm(h2, m2) for h1, m1, h2, m2 in SPAN.findall(text)}
    cued = {_hm(h, m) for h, m in CUED.findall(text)} | {_hm(h, m) for h, m in HOUR.findall(text)}
    if len(ranges) == 1:
        # One range, unless another cued hour says otherwise ("godz. 9:00 … basen czynny w godz. 11–21").
        return sorted(ranges | (cued - {r.split("-")[0] for r in ranges}))
    if cued:
        return sorted(cued)
    if bare:
        return sorted({_hm(h, m) for h, m in BARE.findall(text)})
    return []


def eligible(ev: Event) -> bool:
    return ev.all_day and not (ev.end and ev.end.date() != ev.start.date())


def infer_time(ev: Event, text: str | None = None, bare: bool = True) -> bool:
    if not eligible(ev):
        return False
    found = time_candidates(text if text is not None else f"{ev.title}\n{ev.description or ''}", bare)
    return len(found) == 1 and apply_time(ev, found[0])


def apply_time(ev: Event, value: str) -> bool:
    start, _, end = value.partition("-")
    h, m = map(int, start.split(":"))
    ev.start = ev.start.replace(hour=h, minute=m, second=0, microsecond=0)
    ev.end = None
    ev.all_day = False
    if end:
        h2, m2 = map(int, end.split(":"))
        e = ev.start.replace(hour=h2, minute=m2)
        ev.end = e if e > ev.start else e + timedelta(days=1)
    return True


def _hm(h: str, m: str) -> str:
    return f"{int(h):02d}:{int(m or 0):02d}"
