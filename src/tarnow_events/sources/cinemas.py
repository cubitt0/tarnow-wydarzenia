"""Cinema showtimes: Filmweb (10 cinemas around Tarnów, incl. Multikino) and Helios.

Both produce one event per film per day ("Film X (Multikino)", times in the description)
rather than one per screening, and only go to the cinema calendar (main_feed: false).
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import time
from collections import defaultdict
from datetime import date, timedelta

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import http
from .base import Source

log = logging.getLogger(__name__)


class FilmwebSource(Source):
    """Filmweb cinema pages (/showtimes/<city>/<cinema>-<id>?day=N) carry every showtime as
    `seanceTile` elements with data-date / data-value (HH:MM) / data-lang / data-type.

    Options:
      cinemas: list of {path, name, town, lat, lon}
      days: how many days ahead (default 7)
    """

    type = "filmweb"

    def fetch(self):
        days = int(self.cfg.options.get("days", 7))
        for cinema in self.cfg.options.get("cinemas", []):
            showings: dict[tuple[str, str], dict] = {}
            for day in range(days):
                url = f"https://www.filmweb.pl{cinema['path']}" + (f"?day={day}" if day else "")
                try:
                    page = HTMLParser(http.get(url).text)
                except Exception as e:
                    log.info("filmweb %s day %d: %s", cinema["name"], day, e)
                    break
                time.sleep(0.3)
                for item in page.css(".ShowtimesFilmsItem"):
                    title_a = item.css_first(".preview__title a")
                    tiles = item.css(".seanceTiles .seanceTile[data-date][data-value]")
                    if not title_a or not tiles:
                        continue
                    title = htmllib.unescape(title_a.text(strip=True))
                    film_url = title_a.attributes.get("href") or ""
                    poster = (item.css_first(".poster[data-image]") or title_a).attributes.get("data-image")
                    for t in tiles:
                        d, hm = t.attributes["data-date"], t.attributes["data-value"]
                        rec = showings.setdefault((title, d), {"url": film_url, "img": poster, "times": set()})
                        kind = " ".join(x for x in (t.attributes.get("data-type"), t.attributes.get("data-lang")) if x)
                        rec["times"].add((hm, kind))
            loc = {"location_name": cinema["name"], "town": cinema.get("town"), "lat": cinema.get("lat"), "lon": cinema.get("lon")}
            for (title, d), rec in sorted(showings.items()):
                times = sorted(rec["times"])
                ev = self.make_event(
                    title=f"{title} ({cinema['name']})",
                    start=f"{d}T{times[0][0]}",
                    url=("https://www.filmweb.pl" + rec["url"]) if rec["url"].startswith("/") else rec["url"],
                    description="Seanse: " + " · ".join(f"{hm} {k}".strip() for hm, k in times),
                    image=rec["img"],
                    categories=["kino"],
                    **loc,
                )
                if ev:
                    ev.in_main = False
                    yield ev


class HeliosSource(Source):
    """Helios cinemas (restapi.helios.pl): screenings for a date range, titles from /api/movie/{id}.

    Options: cinema_id, days (default 7), and the usual location for the cinema.
    """

    type = "helios"
    API = "https://restapi.helios.pl/api"

    def fetch(self):
        cid = self.cfg.options["cinema_id"]
        start = date.today()
        end = start + timedelta(days=int(self.cfg.options.get("days", 7)))
        screenings = http.get(
            f"{self.API}/cinema/{cid}/screening",
            params={"dateTimeFrom": f"{start}T00:00:00", "dateTimeTo": f"{end}T00:00:00"},
            bare=True,
        ).json()
        titles: dict[str, dict] = {}
        per_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for s in screenings:
            per_day[(s["movieId"], s["screeningTimeFrom"][:10])].append(s)
        for (movie_id, day), shows in sorted(per_day.items()):
            if movie_id not in titles:
                try:
                    titles[movie_id] = http.get(f"{self.API}/movie/{movie_id}", bare=True).json()
                except Exception as e:
                    log.info("helios movie %s: %s", movie_id, e)
                    titles[movie_id] = {}
            movie = titles[movie_id]
            name = movie.get("title") or movie.get("name") or movie.get("originalTitle")
            if not name:
                continue
            shows.sort(key=lambda s: s["screeningTimeFrom"])
            times = " · ".join(
                f"{s['screeningTimeFrom'][11:16]} {(s.get('printType') or '')} {(s.get('speakingType') or s.get('language') or '')}".strip()
                for s in shows
            )
            ev = self.make_event(
                title=f"{name} ({self.cfg.name})",
                start=shows[0]["screeningTimeFrom"],
                url=self.cfg.url,
                description=f"Seanse: {times}",
                image=(movie.get("posters") or [None])[0],
                categories=["kino"],
            )
            if ev:
                ev.in_main = False
                yield ev
