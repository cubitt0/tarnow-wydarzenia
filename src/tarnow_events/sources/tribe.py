from __future__ import annotations

from datetime import date, timedelta

from .. import http
from ..categorize import detect, normalize
from .base import Source


class TribeSource(Source):
    """WordPress + The Events Calendar plugin: /wp-json/tribe/events/v1/events.

    Very common on Polish cultural-centre sites. `url` is the site root.
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
                    price=item.get("cost") or None,
                    # Site category slugs ("wystawy_wernisaze", "koncert") → our calendars;
                    # generic ones ("kultura", "wydarzenia_specjalne") fall through to keywords.
                    categories=[n for c in item.get("categories") or [] if (n := normalize(c.get("slug")))]
                    or detect(" ".join(c.get("name", "") for c in item.get("categories") or []))[:2] or None,
                )
                if ev:
                    yield ev
            if not data.get("next_rest_url"):
                break
            params["page"] += 1
