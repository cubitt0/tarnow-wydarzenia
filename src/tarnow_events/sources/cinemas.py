"""Cinema showtimes: Filmweb (10 cinemas around Tarnów, incl. Multikino), Helios and Kino Marzenie.

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
                        rec = showings.setdefault((title, d), {"url": film_url, "img": poster, "times": set(), "buy": {}})
                        kind = " ".join(x for x in (t.attributes.get("data-type"), t.attributes.get("data-lang")) if x)
                        rec["times"].add((hm, kind))
                        # Some cinemas (Multikino) let Filmweb link each screening to its booking page.
                        link = t.css_first("a.seanceTile__link[href]")
                        if link and link.attributes["href"].startswith("http"):
                            rec["buy"].setdefault(hm, link.attributes["href"])
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
                    ev.tickets = next((rec["buy"][hm] for hm, _ in times if hm in rec["buy"]), None)
                    yield ev


class KinoMarzenieSource(Source):
    """Kino Marzenie, Tarnów: the site's per-day listing (/embed/events?start_date=D&end_date=D) has every
    film with its page, poster, and each screening's time, version and booking link (marzenieonline.tck.pl).
    It shows one day per request whatever the range, so the days are read one by one.

    Options: days (default 14), category_id (default 8: films).
    """

    type = "kinomarzenie"
    BASE = "https://www.kinomarzenie.pl"

    def fetch(self):
        days = int(self.cfg.options.get("days", 14))
        cat = self.cfg.options.get("category_id", 8)
        for n in range(days):
            d = (date.today() + timedelta(days=n)).isoformat()
            try:
                page = HTMLParser(http.get(f"{self.BASE}/embed/events",
                                           params={"start_date": d, "end_date": d, "category_id": cat}).text)
            except Exception as e:
                log.info("kino marzenie %s: %s", d, e)
                break
            time.sleep(0.3)
            yield from self.parse_day(page, d)

    def parse_day(self, page: HTMLParser, d: str):
        for item in page.css(".row.item"):
            title_a = item.css_first("h4 a[href]")
            if not title_a:
                continue
            title = " ".join(title_a.text(deep=False, strip=True).split()) or title_a.text(strip=True)
            shows = []
            for a in item.css("a[href*='MSI']"):
                m = re.search(r"(\d{1,2}:\d{2})\s*$", a.text(separator=" ", strip=True))
                if not m:
                    continue
                version = a.parent.css_first(".flex-wrap") if a.parent else None
                shows.append((m.group(1).zfill(5), version.text(strip=True) if version else "", a.attributes["href"]))
            if not shows:
                continue  # past days keep the films but drop their screenings
            shows.sort()
            img = item.css_first("img[src]")
            genre = item.css_first("span.small")
            desc = "Seanse: " + " · ".join(f"{hm} {v}".strip() for hm, v, _ in shows)
            if genre:
                desc += "\n" + genre.text(strip=True)
            ev = self.make_event(
                title=f"{title} ({self.cfg.name})",
                start=f"{d}T{shows[0][0]}",
                url=self.BASE + title_a.attributes["href"] if title_a.attributes["href"].startswith("/") else title_a.attributes["href"],
                description=desc,
                image=img.attributes["src"] if img else None,
                categories=["kino"],
            )
            if ev:
                ev.in_main = False
                ev.tickets = shows[0][2]
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
