"""Merge the same real-world event reported by several sources into one, keeping every link.

Two events are the same when they are on the same local day, their titles are near-identical
(fuzzy), their start times are within an hour (if both have one) and they are not in clearly
different places. Clusters are formed with union-find so A~B and B~C merge all three.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from rapidfuzz import fuzz
from unidecode import unidecode

from .geo import haversine_km, norm_town
from .models import Event, SourceRef

TITLE_THRESHOLD = 85
TIME_TOLERANCE_MIN = 60
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
    # Same URL alone proves nothing: AI-extracted events all point at their listing page.
    if not titles_match(a.title, b.title):
        return False
    if not a.all_day and not b.all_day:
        if abs((a.start - b.start).total_seconds()) > TIME_TOLERANCE_MIN * 60:
            return False
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

    # Two clusters can claim the same old UID after a split; keep UIDs unique.
    seen: set[str] = set()
    for e in sorted(out, key=lambda e: (-e.priority, e.start)):
        if e.uid in seen:
            e.uid = hashlib.sha1((e.uid + e.title).encode()).hexdigest()[:24] + "@tarnow-wydarzenia"
        seen.add(e.uid)
    return sorted(out, key=lambda e: (e.start, e.title))
