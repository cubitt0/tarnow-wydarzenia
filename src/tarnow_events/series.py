"""Collapse day-by-day *opening hours* into one multi-day event.

Event calendars often store an exhibition as one occurrence per opening day, so a two-month
show arrives as ~50 separate events. Daily repeats of the same title at the same place from the
same source become one all-day event spanning first..last day - but only when they look like
opening hours (exhibitions, or 6 h or more a day, or already all-day). A play running three
evenings in a row ("Azoty Fiction", 27-29.11 at 18:00/17:00) stays three timed events: those are
separate performances someone buys a ticket for. Weekly repeats are never collapsed.
"""

from __future__ import annotations

from collections import defaultdict

from .dedup import norm_title
from .models import Event

MIN_OCCURRENCES = 3
MAX_GAP_DAYS = 4  # museums closed at weekends / on Mondays still count as one run
LONG_EVENT_DAYS = 7
OPENING_HOURS_MIN = 6 * 60  # a daily slot this long is opening hours, not a performance
PERFORMANCE = {"teatr", "koncerty", "kino", "sport", "imprezy", "spotkania", "dzieci"}  # longer than this → only in dlugie.ics / category feeds, not the main feeds


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
            if len(days) < MIN_OCCURRENCES or not _opening_hours(run):
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


def _opening_hours(run: list[Event]) -> bool:
    if all(e.all_day for e in run):
        return True
    durations = sorted((e.end - e.start).total_seconds() / 60 for e in run if e.end and not e.all_day)
    if durations and durations[len(durations) // 2] >= OPENING_HOURS_MIN:
        return True
    # An exhibition's daily slots are visiting hours whatever their length ("Re:Wizja 14-15").
    return run[0].categories[:1] == ["wystawy"]


def is_long(e: Event) -> bool:
    return e.end is not None and (e.end - e.start).days > LONG_EVENT_DAYS
