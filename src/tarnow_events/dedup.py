"""Merge the same real-world event reported by several sources into one, keeping every link.

Two events are the same when they are on the same local day, their titles are near-identical
(fuzzy), their start times are within an hour (if both have one) and they are not in clearly
different places. Clusters are formed with union-find so A~B and B~C merge all three.

Multi-day and all-day events get a second pass: sources often disagree on the first day
("Festiwal Czekolady" 27-29.11 on it.tarnow, "CzekoFest" "this weekend" on the city's news),
so overlapping ranges in the same place from different sources merge when their titles share a
distinctive word and are reasonably similar.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from rapidfuzz import fuzz
from unidecode import unidecode

from .geo import KNOWN_TOWNS, haversine_km, norm_town
from .models import TZ, Event, SourceRef

log = logging.getLogger(__name__)

TITLE_THRESHOLD = 85
TIME_TOLERANCE_MIN = 60
SCREENING_TOLERANCE_MIN = 5
PLACE_TOLERANCE_KM = 2.0

# Words that carry no identity: "Koncert: X" and "X" are the same event.
NOISE = re.compile(
    r"\b(koncert|spektakl|wystawa|seans|film|premiera|bilety|wydarzenie|zaproszenie|zapraszamy|"
    r"na|w|we|i|oraz|z|ze|do|dla|o|the|tarnow|tarnowie|\d{4})\b"
)


def norm_title(title: str) -> str:
    t = unidecode(title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = NOISE.sub(" ", t)
    return " ".join(t.split())


def norm_url(url: str) -> str:
    u = re.sub(r"^https?://(www\.|m\.)?", "", url.strip().lower())
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/")


def titles_match(a: str, b: str) -> bool:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return fuzz.ratio(unidecode(a).lower(), unidecode(b).lower()) >= 95
    if min(len(na.split()), len(nb.split())) == 1:
        # token_set_ratio says a one-word title matches anything containing that word.
        return fuzz.ratio(na, nb) >= 90 or (na in nb.split() and len(na) >= 6) or (nb in na.split() and len(nb) >= 6)
    return fuzz.token_set_ratio(na, nb) >= TITLE_THRESHOLD


def same_event(a: Event, b: Event) -> bool:
    if a.day != b.day:
        return False
    if same_slot(a, b):
        return True
    # Same URL alone proves nothing: AI-extracted events all point at their listing page.
    if not titles_match(a.title, b.title):
        return False
    if not a.in_main and not b.in_main:
        # Cinema screenings: one per showtime, so a film's 10:00 and 10:45 shows stay apart. Only the
        # same screening reported by two sources (Filmweb and Bilety24) merges.
        if {r.source_id for r in a.all_sources()} & {r.source_id for r in b.all_sources()}:
            return False
        if abs((a.start - b.start).total_seconds()) > SCREENING_TOLERANCE_MIN * 60:
            return False
    elif not a.all_day and not b.all_day:
        if abs((a.start - b.start).total_seconds()) > TIME_TOLERANCE_MIN * 60:
            return False
    return same_place(a, b)


def same_place(a: Event, b: Event) -> bool:
    if None not in (a.lat, a.lon, b.lat, b.lon):
        if haversine_km(a.lat, a.lon, b.lat, b.lon) > PLACE_TOLERANCE_KM:
            # Coordinates may be just the town centre; only trust them if the towns differ too.
            ta, tb = norm_town(a.town), norm_town(b.town)
            if ta and tb and ta != tb:
                return False
            if not (ta and tb):
                return False
    else:
        ta, tb = norm_town(a.town), norm_town(b.town)
        if ta and tb and ta != tb:
            return False
    return True


# ---- one venue, one start time, differently worded titles
# "ANDRZEJKI W RYTMIE HITÓW" and "Wielkie Andrzejki w Tarnowie", both at Arena Jaskółka at 19:00, are one
# event: a venue rarely runs two shows at once, so a shared distinctive word is enough.
SLOT_TOLERANCE_MIN = 15
SLOT_PLACE_KM = 0.15
TOWN_CENTRE_KM = 0.05


def _at_town_centre(lat: float, lon: float) -> bool:
    # Geocoding falls back to the town centre, which says nothing about the venue.
    return any(haversine_km(lat, lon, *c) <= TOWN_CENTRE_KM for c in KNOWN_TOWNS.values())


def same_slot(a: Event, b: Event) -> bool:
    if a.all_day or b.all_day or not (a.in_main and b.in_main):
        return False  # cinemas run several screenings at once, in different halls
    if abs((a.start - b.start).total_seconds()) > SLOT_TOLERANCE_MIN * 60:
        return False
    if None in (a.lat, a.lon, b.lat, b.lon) or _at_town_centre(a.lat, a.lon) or _at_town_centre(b.lat, b.lon):
        return False
    if haversine_km(a.lat, a.lon, b.lat, b.lon) > SLOT_PLACE_KM:
        return False
    if {r.source_id for r in a.all_sources()} & {r.source_id for r in b.all_sources()}:
        return False  # one source never lists the same event twice
    return bool(_stems(a.title) & _stems(b.title))


# ---- multi-day / all-day events whose sources disagree on the dates
SPAN_TITLE_MIN = 55
SPAN_GENERIC = {
    "festiwal", "festiwalu", "swieto", "swieta", "swiateczny", "swiateczne", "jarmark", "jarmarku", "rynku", "rynek",
    "czyli", "edycja", "edycji", "miasta", "miejski", "miejskie", "gminy", "gminny", "gminne", "dzien", "dni", "dzieci",
    "rodzinny", "rodzinna", "wielki", "wielka", "zimowy", "zimowa", "letni", "letnia", "wystawy", "wystawie",
    "konkurs", "konkursu", "warsztaty", "spotkanie", "impreza", "plener", "otwarcie", "wernisaz", "kultury", "centrum",
    "galeria", "muzeum", "weekend", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
    "wrzesnia", "pazdziernika", "listopada", "listopad", "grudnia", "grudzien",
}


def _stems(title: str) -> set[str]:
    return {w[:6] for w in norm_title(title).split() if len(w) >= 5 and w not in SPAN_GENERIC and not w.startswith("tarnow")}


def _first_last(e: Event) -> tuple:
    first = e.start.astimezone(TZ).date()
    return first, max(first, (e.end or e.start).astimezone(TZ).date())


def is_span(e: Event) -> bool:
    first, last = _first_last(e)
    return e.all_day or last > first


def same_span(a: Event, b: Event) -> bool:
    if {r.source_id for r in a.all_sources()} & {r.source_id for r in b.all_sources()}:
        return False  # one source never lists the same event twice: those are different events
    (af, al), (bf, bl) = _first_last(a), _first_last(b)
    if af > bl or bf > al or not same_place(a, b):
        return False
    if not _stems(a.title) & _stems(b.title):
        return False
    return fuzz.token_set_ratio(norm_title(a.title), norm_title(b.title)) >= SPAN_TITLE_MIN


def merge_spans(events: list[Event]) -> list[Event]:
    spans = [e for e in events if is_span(e)]
    parent = list(range(len(spans)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if find(i) != find(j) and same_span(spans[i], spans[j]):
                parent[find(j)] = find(i)
    clusters: dict[int, list[Event]] = defaultdict(list)
    for i, e in enumerate(spans):
        clusters[find(i)].append(e)
    gone: set[int] = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        first = min(_first_last(m)[0] for m in members)
        last = max(_first_last(m)[1] for m in members)
        all_day = all(m.all_day for m in members)
        log.info("merged multi-day: %s", " + ".join(f"{m.title[:40]!r} ({m.source.source_id})" for m in members))
        merged = merge(members)  # primary keeps its UID, gains the others' links and details
        if all_day:  # the sources disagree on the dates: the event covers all of them
            merged.all_day = True
            merged.start = merged.start.astimezone(TZ).replace(year=first.year, month=first.month, day=first.day,
                                                               hour=0, minute=0, second=0, microsecond=0)
            merged.end = merged.start.replace(year=last.year, month=last.month, day=last.day) if last > first else None
        gone.update(id(m) for m in members if m is not merged)
    return [e for e in events if id(e) not in gone]


def _richness(e: Event) -> int:
    return sum(
        bool(x)
        for x in (e.description, e.location_name, e.address, e.lat, e.image, e.price, e.end, not e.all_day)
    )


def merge(cluster: list[Event]) -> Event:
    cluster = sorted(cluster, key=lambda e: (e.priority, _richness(e)), reverse=True)
    primary = cluster[0]
    for other in cluster[1:]:
        for f in ("description", "location_name", "address", "town", "lat", "lon", "image", "price", "tickets", "end"):
            if getattr(primary, f) in (None, "") and getattr(other, f) not in (None, ""):
                setattr(primary, f, getattr(other, f))
        if primary.all_day and not other.all_day:
            # Keep the primary's identity but adopt a real start time from a richer source.
            primary.start, primary.all_day = other.start, False
        if other.description and primary.description and len(other.description) > 2 * len(primary.description):
            primary.description = other.description
        for c in other.categories:
            if c not in primary.categories and c != "inne":
                primary.categories.append(c)
    if len(primary.categories) > 1 and "inne" in primary.categories:
        primary.categories.remove("inne")
    refs: list[SourceRef] = []
    seen: set[str] = set()
    for e in cluster:
        for r in e.all_sources():
            k = norm_url(r.url)
            if k not in seen:
                seen.add(k)
                refs.append(r)
    primary.sources = refs
    primary.in_main = any(e.in_main for e in cluster)
    primary.cancelled = primary.cancelled or any(e.cancelled and e.priority >= primary.priority for e in cluster)
    return primary


def member_key(e: Event) -> str:
    raw = f"{e.source.source_id}|{norm_url(e.source.url)}|{e.day.isoformat()}|{norm_title(e.title)}"
    if not e.in_main:
        raw += f"|{e.start.astimezone(TZ):%H:%M}"  # a film's screenings on one day share title, page and day
    return hashlib.sha1(raw.encode()).hexdigest()


def deduplicate(events: list[Event], uid_state: dict[str, str]) -> list[Event]:
    """Returns merged events with stable UIDs. `uid_state` (member key → uid) is updated in place."""
    by_day: dict = defaultdict(list)
    for e in events:
        by_day[e.day].append(e)

    out: list[Event] = []
    for day_events in by_day.values():
        parent = list(range(len(day_events)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(day_events)):
            for j in range(i + 1, len(day_events)):
                if find(i) != find(j) and same_event(day_events[i], day_events[j]):
                    parent[find(j)] = find(i)

        clusters: dict[int, list[Event]] = defaultdict(list)
        for i, e in enumerate(day_events):
            clusters[find(i)].append(e)

        for members in clusters.values():
            keys = sorted(member_key(m) for m in members)
            merged = merge(members)
            # Reuse a UID any member had before, so calendar apps update instead of duplicating.
            uid = next((uid_state[k] for k in keys if k in uid_state), None) or f"{keys[0][:24]}@tarnow-wydarzenia"
            for k in keys:
                uid_state[k] = uid
            merged.uid = uid
            out.append(merged)

    out = merge_spans(out)
    # Two clusters can claim the same old UID after a split; keep UIDs unique.
    seen: set[str] = set()
    for e in sorted(out, key=lambda e: (-e.priority, e.start)):
        if e.uid in seen:
            e.uid = hashlib.sha1((e.uid + e.title).encode()).hexdigest()[:24] + "@tarnow-wydarzenia"
        seen.add(e.uid)
    return sorted(out, key=lambda e: (e.start, e.title))
