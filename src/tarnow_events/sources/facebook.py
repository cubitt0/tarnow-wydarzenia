"""Public Facebook events, read one permalink at a time.

Port of MojBarman's FacebookEventPayloadParser. There is no API: the Graph API events edge is
partner-only, and a Page's *list* of events is not served to non-browser clients. A single
public event permalink, however, is server-rendered into `<script type="application/json">`
Relay blocks, which are decoded and walked here.

Event URLs come from:
  1. `events:` in this source's config (hand-curated),
  2. facebook.com/events/<id> links found in any other source's events (auto-discovered).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .. import http
from .base import Source

log = logging.getLogger(__name__)

EVENT_ID_RE = re.compile(r"facebook\.com/events/(?:[^/\s\"']+/)*?(\d{6,})")
CANONICAL = "https://www.facebook.com/events/{}/"


def find_event_ids(text: str | None) -> set[str]:
    return set(EVENT_ID_RE.findall(text or ""))


def _walk(node: Any):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


def parse_event_page(html: str, event_id: str) -> dict | None:
    blocks = []
    for raw in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            continue
    # Match on the event id: the page also carries "suggested events" with identical shape.
    nodes = [n for b in blocks for n in _walk(b) if n.get("id") == event_id and len(n) > 1]
    if not nodes:
        return None
    nodes.sort(key=len, reverse=True)

    def first(key: str):
        for n in nodes:
            if n.get(key) is not None:
                return n[key]
        return None

    start = first("start_timestamp") or first("current_start_timestamp")
    if isinstance(start, str) and start.isdigit():
        start = int(start)
    end = None
    if isinstance(start, int):
        for b in blocks:
            for n in _walk(b):
                if n.get("start_timestamp") == start and isinstance(n.get("end_timestamp"), int) and n["end_timestamp"] > start:
                    end = n["end_timestamp"]
                    break
            if end:
                break

    place = None
    for n in nodes:
        p = n.get("event_place")
        if isinstance(p, dict):
            if p.get("location"):
                place = p
                break
            place = place or p
    place = place or {}
    loc = place.get("location") or {}

    desc = None
    for key in ("event_description", "description"):
        v = first(key)
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            desc = v["text"]
            break
        if isinstance(v, str) and v.strip():
            desc = v
            break

    cover = None
    renderer = first("cover_media_renderer")
    if isinstance(renderer, dict):
        photo = (renderer.get("cover_photo") or {}).get("photo") or {}
        cover = (photo.get("full_image") or {}).get("uri")

    return {
        "title": first("name"),
        "start": start,
        "end": end,
        "description": desc,
        "place": place.get("name") or place.get("contextual_name"),
        "address": (loc.get("reverse_geocode") or {}).get("street") if isinstance(loc.get("reverse_geocode"), dict) else None,
        "town": (loc.get("reverse_geocode") or {}).get("city") if isinstance(loc.get("reverse_geocode"), dict) else None,
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "image": cover,
        "online": bool(first("is_online")),
        "cancelled": bool(first("is_canceled")),
    }


class FacebookSource(Source):
    type = "facebook"

    def __init__(self, cfg, settings):
        super().__init__(cfg, settings)
        self.discovered: set[str] = set()

    def fetch(self):
        ids: set[str] = set(self.discovered)
        for url in self.cfg.options.get("events", []) or []:
            ids |= find_event_ids(url)
        delay = float(self.cfg.options.get("delay", 2))
        unreadable_streak = 0
        for event_id in sorted(ids):
            url = CANONICAL.format(event_id)
            try:
                html = http.get(url, headers={"Sec-Fetch-Site": "same-origin"}).text
            except Exception as e:
                log.info("facebook %s fetch failed: %s", event_id, e)
                continue
            finally:
                time.sleep(delay)
            data = parse_event_page(html, event_id)
            if data is None:
                unreadable_streak += 1
                if unreadable_streak >= 5:
                    log.error("facebook: 5 unreadable events in a row - payload shape changed or IP blocked; stopping")
                    return
                continue
            unreadable_streak = 0
            if data["online"]:
                continue
            ev = self.make_event(
                title=data["title"],
                start=data["start"],
                end=data["end"],
                url=url,
                description=data["description"],
                location_name=data["place"],
                address=data["address"],
                town=data["town"],
                lat=data["lat"],
                lon=data["lon"],
                image=data["image"],
                cancelled=data["cancelled"],
            )
            if ev:
                yield ev
