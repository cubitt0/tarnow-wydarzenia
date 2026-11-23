from __future__ import annotations

import feedparser

from .. import http
from ..categorize import detect
from .base import Source


class MecSource(Source):
    """WordPress + Modern Events Calendar: its RSS (`/feed/?post_type=mec-events`) carries
    structured mec:startDate/startHour/endDate/endHour/location fields. No AI needed.

    `url` is the feed URL. Paged with &paged=N because WordPress caps a feed at ~10 items.
    Options: max_pages (default 30)
    """

    type = "mec"

    def fetch(self):
        seen: set[str] = set()
        sep = "&" if "?" in self.cfg.url else "?"
        for page in range(1, int(self.cfg.options.get("max_pages", 30)) + 1):
            url = self.cfg.url if page == 1 else f"{self.cfg.url}{sep}paged={page}"
            try:
                feed = feedparser.parse(http.get(url).content)
            except Exception:
                if page == 1:
                    raise
                break  # WordPress 404s past the last page
            new = [e for e in feed.entries if e.get("link") not in seen]
            if not new:
                break
            for e in new:
                seen.add(e.get("link"))
                start_d, start_h = e.get("mec_startdate"), e.get("mec_starthour")
                end_d, end_h = e.get("mec_enddate"), e.get("mec_endhour")
                if not start_d:
                    continue
                all_day = not start_h or start_h in ("00:00", "0:00")
                ev = self.make_event(
                    title=e.get("title"),
                    start=start_d if all_day else f"{start_d}T{start_h}",
                    end=(end_d if all_day else f"{end_d}T{end_h or start_h}") if end_d else None,
                    all_day=all_day,
                    url=e.get("link"),
                    description=e.get("summary"),
                    location_name=e.get("mec_location"),
                    categories=detect(e.get("mec_category", ""))[:2] or None,
                )
                if ev:
                    yield ev
