"""Public Facebook events, read one permalink at a time.

Port of MojBarman's FacebookEventPayloadParser. There is no API: the Graph API events edge is
partner-only, and a Page's *list* of events is not served to non-browser clients. A single
public event permalink, however, is server-rendered into `<script type="application/json">`
Relay blocks, which are decoded and walked here.

Event URLs come from:
  1. `events:` in this source's config (hand-curated),
  2. facebook.com/events/<id> links found in any other source's events (auto-discovered),
  3. links visitors send in through a Google Form, kept only when a whitelisted page hosts the
     event (see submissions.py).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import http
from ..submissions import FILE as LEDGER_FILE
from ..submissions import Ledger, event_ids_in, host_match, read_csv, whitelist
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

    hosts: list[dict] = []
    for h in (first("event_hosts_that_can_view_guestlist") or []) + \
            ((first("event_accepted_cohosts") or {}).get("nodes") or []) + \
            [first("event_creator") or {}, first("page_as_owner") or {}]:
        if isinstance(h, dict) and h.get("id") and all(x["id"] != h["id"] for x in hosts):
            hosts.append({"id": str(h["id"]), "name": h.get("name"), "url": h.get("url")})

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
        "hosts": hosts,
        "tickets": _ticket_url(first("event_buy_ticket_url")),
    }


def _ticket_url(url: Any) -> str | None:
    """The event's "Buy tickets" link, unwrapped from Facebook's l.facebook.com redirect."""
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    parts = urlsplit(url)
    if parts.hostname in ("l.facebook.com", "lm.facebook.com"):
        url = (parse_qs(parts.query).get("u") or [""])[0]
    return url if url.startswith("http") else None


class FacebookSource(Source):
    type = "facebook"

    def __init__(self, cfg, settings):
        super().__init__(cfg, settings)
        self.discovered: set[str] = set()

    def fetch(self):
        files = (self.settings.out_dir / LEDGER_FILE, self.settings.cache_dir / LEDGER_FILE)
        ledger = Ledger.load(*files)
        ledger.forget_data_before(int(time.time()) - self.settings.days_back * 86400)
        self._streak = 0
        handled: set[str] = set()
        allowed = self._whitelist() if self.cfg.options.get("submissions_csv") else {}
        for event_id in ledger.recheck(allowed):
            log.info("facebook submission %s accepted: its host is on the whitelist now", event_id)
        try:
            # Accepted submissions: from the ledger, never fetched again.
            for event_id, rec in ledger.accepted().items():
                handled.add(event_id)
                ev = self._event(event_id, rec["data"])
                if ev:
                    yield ev
            if self.cfg.options.get("submissions_csv"):
                yield from self._submissions(ledger, handled, allowed)
            ids: set[str] = set(self.discovered)
            for url in self.cfg.options.get("events", []) or []:
                ids |= find_event_ids(url)
            for event_id in sorted(ids - handled):
                data = self._read(event_id)
                if data is False:
                    return
                if data:
                    ev = self._event(event_id, data)
                    if ev:
                        yield ev
        finally:
            ledger.save(*files)

    def _submissions(self, ledger: Ledger, handled: set[str], allowed: dict[str, str]):
        try:
            text = http.get(self.cfg.options["submissions_csv"], bare=True).text
        except Exception as e:  # the form's Sheet being down must not cost the other Facebook events
            log.warning("facebook submissions: sheet unreadable: %s", e)
            return
        new: list[str] = []
        for row in read_csv(text):
            for event_id in sorted(event_ids_in(row, self._resolve)):
                if event_id not in handled and not ledger.done(event_id) and event_id not in new:
                    new.append(event_id)
        limit = int(self.cfg.options.get("submissions_per_build", 20))
        if len(new) > limit:
            log.info("facebook submissions: %d new, reading %d this build", len(new), limit)
        accepted = 0
        for event_id in new[:limit]:
            handled.add(event_id)
            data = self._read(event_id)
            if data is False:
                return
            if data is None:
                ledger.retry(event_id)
                continue
            host = host_match(data["hosts"], allowed)
            if not host:
                names = ", ".join(h["name"] or h["id"] for h in data["hosts"]) or "?"
                log.info("facebook submission %s rejected: host %s not on the whitelist", event_id, names)
                ledger.reject(event_id, "organizator spoza listy stron", names, data)
                continue
            ledger.accept(event_id, data, host)
            accepted += 1
            ev = self._event(event_id, data)
            if ev:
                yield ev
        log.info("facebook submissions: %d new, %d accepted", min(len(new), limit), accepted)

    def _whitelist(self) -> dict[str, str]:
        try:
            catalogue = json.loads((self.settings.root / "docs" / "sources-catalogue.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalogue = []
        allowed = whitelist(catalogue, self.cfg.options.get("allowed_pages"))
        if not allowed:
            log.warning("facebook submissions: empty whitelist, every submission will be rejected")
        return allowed

    def _resolve(self, url: str) -> str | None:
        try:
            return str(http.get(url, retries=0).url)
        except Exception:
            return None

    def _read(self, event_id: str) -> dict | None | bool:
        """The event's data; None when unreadable; False after 5 unreadable in a row (stop for today)."""
        if self._streak >= 5:
            return False
        try:
            html = http.get(CANONICAL.format(event_id), headers={"Sec-Fetch-Site": "same-origin"}).text
        except Exception as e:
            log.info("facebook %s fetch failed: %s", event_id, e)
            return None
        finally:
            time.sleep(float(self.cfg.options.get("delay", 2)))
        data = parse_event_page(html, event_id)
        if data is None:
            self._streak += 1
            if self._streak >= 5:
                log.error("facebook: 5 unreadable events in a row - payload shape changed or IP blocked; stopping")
                return False
            return None
        self._streak = 0
        return data

    def _event(self, event_id: str, data: dict):
        if data["online"]:
            return None
        ev = self.make_event(
            title=data["title"],
            start=data["start"],
            end=data["end"],
            url=CANONICAL.format(event_id),
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
            ev.tickets = data.get("tickets")  # absent from submissions accepted before it was read
        return ev
