from __future__ import annotations

import logging

from .. import http
from ..geo import KNOWN_TOWNS, norm_town
from .base import Source
from .jsonld import JsonLdSource

log = logging.getLogger(__name__)

API = "https://www.ebilet.pl/api"


class EbiletSource(Source):
    """eBilet.pl. A city landing page lists *titles* (often national tours); each title page
    carries schema.org Event JSON-LD for every date, and only dates in our towns are kept.

    Options:
      cities: eBilet city slugs (default [tarnow])
      max_titles: cap on title pages fetched per run (default 120)
    """

    type = "ebilet"

    def fetch(self):
        opts = self.cfg.options
        titles: dict[str, str] = {}  # slug → page url
        for city in opts.get("cities", ["tarnow"]):
            try:
                landing = http.get(f"{API}/LandingPageLocation/{city}", headers={"Accept": "application/json"}).json()
            except Exception as e:  # not every town has an eBilet landing page
                log.info("ebilet city %s: %s", city, e)
                continue
            for section in landing.get("sections") or []:
                for group in (section.get("productSectionInfo") or {}).get("groups") or []:
                    try:
                        data = http.get(f"{API}/LandingPage/group/{group['id']}/title",
                                        headers={"Accept": "application/json"}).json()
                    except Exception as e:
                        log.info("ebilet group %s: %s", group.get("id"), e)
                        continue
                    for t in (data.get("titles") or []) + (data.get("titlesInRange") or []):
                        if t.get("slug") and t.get("category"):
                            path = "/".join(p for p in (t["category"], t.get("subcategory"), t["slug"]) if p)
                            titles.setdefault(t["slug"], f"https://www.ebilet.pl/{path}")

        parser = JsonLdSource(self.cfg, self.settings)
        for url in list(titles.values())[: int(opts.get("max_titles", 120))]:
            try:
                html = http.get(url).text
            except Exception as e:
                log.info("ebilet title %s: %s", url, e)
                continue
            for ev in parser._events(html, url):
                # Tours: keep only the dates in towns we know are inside the radius.
                if norm_town(ev.town) in KNOWN_TOWNS:
                    yield ev
