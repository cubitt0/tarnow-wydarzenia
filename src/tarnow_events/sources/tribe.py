from __future__ import annotations

import html as htmllib
from datetime import date, timedelta
from urllib.parse import urlsplit

from .. import http
from ..categorize import detect, normalize
from ..models import SourceRef
from ..pictures import content_images
from .base import Source, is_machine_url

# Short links in the "event website" field: followed once per run to the page they point at.
SHORTENERS = {"tiny.pl", "fb.me", "bit.ly", "tinyurl.com", "goo.gl", "t.ly", "shorturl.at"}


class TribeSource(Source):
    """WordPress + The Events Calendar plugin: /wp-json/tribe/events/v1/events.

    Very common on Polish cultural-centre sites. `url` is the site root.

    The plugin's "event website" field (often the organiser's article: tarnow.pl, tck.pl…) becomes a
    second link of the event, so people can open it and the picture/ticket lookup reads it too.
    """

    type = "tribe"

    def fetch(self):
        base = self.cfg.url.rstrip("/") + "/wp-json/tribe/events/v1/events"
        params = {
            "start_date": (date.today() - timedelta(days=self.settings.days_back)).isoformat(),
            "end_date": (date.today() + timedelta(days=self.settings.days_ahead)).isoformat(),
            "per_page": 50,
            "page": 1,
        }
        for _ in range(40):  # hard page cap
            data = http.get(base, params=params, headers={"Accept": "application/json"}).json()
            for item in data.get("events", []):
                venue = item.get("venue") or {}
                if isinstance(venue, list):
                    venue = venue[0] if venue else {}
                ev = self.make_event(
                    title=item.get("title"),
                    start=item.get("start_date"),
                    end=item.get("end_date"),
                    all_day=bool(item.get("all_day")),
                    url=item.get("url"),
                    description=item.get("excerpt") or item.get("description"),
                    location_name=venue.get("venue"),
                    address=venue.get("address"),
                    town=venue.get("city"),
                    lat=venue.get("geo_lat"),
                    lon=venue.get("geo_lng"),
                    image=(item.get("image") or {}).get("url") if isinstance(item.get("image"), dict) else None,
                    # From the full description even when the excerpt is shown: a programme is often only a picture.
                    images=content_images(item.get("description"), self.cfg.url),
                    price=item.get("cost") or None,
                    # Site category slugs ("wystawy_wernisaze", "koncert") → our calendars;
                    # generic ones ("kultura", "wydarzenia_specjalne") fall through to keywords.
                    categories=[n for c in item.get("categories") or [] if (n := normalize(c.get("slug")))]
                    or detect(" ".join(c.get("name", "") for c in item.get("categories") or []))[:2] or None,
                )
                if ev:
                    self.add_website(ev, item.get("website"))
                    yield ev
            if not data.get("next_rest_url"):
                break
            params["page"] += 1

    def add_website(self, ev, website) -> None:
        url = htmllib.unescape(website or "").strip()
        if not url.startswith("http") or is_machine_url(url):
            return
        if (urlsplit(url).hostname or "").removeprefix("www.") in SHORTENERS:
            url = self._resolve(url)
            if not url:
                return
        if url.rstrip("/") == ev.source.url.rstrip("/"):
            return
        host = (urlsplit(url).hostname or "").removeprefix("www.").removeprefix("m.")
        name = "Facebook" if host in ("facebook.com", "fb.me", "fb.com") else f"Strona wydarzenia ({host})"
        ev.sources = [ev.source, SourceRef(self.cfg.id, name, url)]

    def _resolve(self, url: str) -> str | None:
        cache = self.__dict__.setdefault("_short", {})
        if url not in cache:
            try:
                final = str(http.get(url, retries=0).url)
                cache[url] = url if "/login" in final else final  # Facebook asks to log in: keep the short link
            except Exception:
                cache[url] = None
        return cache[url]
