"""Collapse day-by-day repeats into one multi-day event.

Event calendars often store an exhibition as one occurrence per opening day, so a two-month
show arrives as ~50 separate events. Daily repeats of the same title at the same place from the
same source become one all-day event spanning first..last day. Weekly repeats (a Tuesday class)
are left alone: those are genuinely separate dates someone may want to attend.
"""

from __future__ import annotations

from collections import defaultdict

from .dedup import norm_title
from .models import Event

MIN_OCCURRENCES = 3
MAX_GAP_DAYS = 2  # closed on Mondays still counts as "daily"
LONG_EVENT_DAYS = 7  # longer than this → only in dlugie.ics / category feeds, not the main feeds


def collapse_series(events: list[Event]) -> list[Event]:
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        key = (e.source.source_id, norm_title(e.title) or e.title.lower(), (e.location_name or "").lower())
        groups[key].append(e)

    out: list[Event] = []
    for members in groups.values():
        members.sort(key=lambda e: e.start)
        if len(members) < MIN_OCCURRENCES:
            out.extend(members)
            continue
        # Split into runs of near-consecutive days; each run of >= MIN_OCCURRENCES collapses.
        runs: list[list[Event]] = [[members[0]]]
        for e in members[1:]:
            gap = (e.day - runs[-1][-1].day).days
            if gap <= MAX_GAP_DAYS:  # gap 0 = two showings on one day, same run
                runs[-1].append(e)
            else:
                runs.append([e])
        for run in runs:
            days = {e.day for e in run}
            if len(days) < MIN_OCCURRENCES:
                out.extend(run)
                continue
            first, last = run[0], run[-1]
            hours = f"{first.start:%H:%M}" + (f"–{first.end:%H:%M}" if first.end and first.end.date() == first.start.date() else "")
            note = f"Trwa {first.day:%d.%m}–{last.day:%d.%m.%Y}" + ("" if first.all_day else f", zwykle {hours}") + "."
            first.description = note + ("\n\n" + first.description if first.description else "")
            first.all_day = True
            first.start = first.start.replace(hour=0, minute=0, second=0, microsecond=0)
            first.end = (last.end or last.start).replace(hour=0, minute=0, second=0, microsecond=0)
            out.append(first)
    return out


def is_long(e: Event) -> bool:
    return e.end is not None and (e.end - e.start).days > LONG_EVENT_DAYS
