"""Cinema showtimes: Filmweb (cinemas around Tarnów, incl. Multikino), Helios, Kino Marzenie and
Kino Millenium. One event per screening ("Film X (Multikino Tarnów)" at 10:00, another at 12:45…),
each with its booking link where the cinema publishes one. They only go to the cinema calendar
(main_feed: false).
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import time
from datetime import date, timedelta

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import http
from .base import Source

log = logging.getLogger(__name__)
HM = re.compile(r"(\d{1,2}:\d{2})\s*$")
# Filmweb posters (fwcdn.pl/fpo/…/<id>.<size>.<ext>): the listing hands out 140×200 (.7.webp);
# size 8 (webp) / 3 (jpg) is the same poster at 500×625.
FW_POSTER = re.compile(r"^(https://fwcdn\.pl/fpo/.+\.)\d+\.(webp|jpg)$")


def big_poster(url: str | None) -> str | None:
    m = FW_POSTER.match(url or "")
    return f"{m.group(1)}{'8' if m.group(2) == 'webp' else '3'}.{m.group(2)}" if m else url


def _screening(src: Source, *, film: str, cinema: str, day: str, hm: str, url: str, kinds: list[str],
               image: str | None = None, extra: str | None = None, tickets: str | None = None, **loc):
    kinds = [k for k in dict.fromkeys(k.strip() for k in kinds) if k]
    desc = "Seans" + (": " + " / ".join(kinds) if kinds else "")
    if extra:
        desc += "\n" + extra
    ev = src.make_event(title=f"{film} ({cinema})", start=f"{day}T{hm.zfill(5)}", url=url, description=desc,
                        image=image, categories=["kino"], **loc)
    if ev:
        ev.in_main = False
        ev.tickets = tickets
    return ev


class FilmwebSource(Source):
    """Filmweb cinema pages (/showtimes/<city>/<cinema>-<id>?day=N) carry every showtime as
    `seanceTile` elements with data-date / data-value (HH:MM) / data-lang / data-type, and for some
    cinemas (Multikino) a link to the screening's booking page.

    Each screening links to the cinema's Filmweb page for that day, scrolled to the film. Filmweb only
    takes a day offset from today (?day=N); the web calendar recomputes it from the event's date.

    Options:
      cinemas: list of {path, name, town, lat, lon}
      days: how many days ahead (default 7)
    """

    type = "filmweb"

    def fetch(self):
        days = int(self.cfg.options.get("days", 7))
        today = date.today()
        for cinema in self.cfg.options.get("cinemas", []):
            loc = {"location_name": cinema["name"], "town": cinema.get("town"), "lat": cinema.get("lat"), "lon": cinema.get("lon")}
            shows: dict[tuple[str, str, str], dict] = {}
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
                    if not title_a:
                        continue
                    title = htmllib.unescape(title_a.text(strip=True))
                    poster = item.css_first(".poster[data-image]")
                    film_id = (item.css_first("[data-film-id]") or title_a).attributes.get("data-film-id")
                    for t in item.css(".seanceTiles .seanceTile[data-date][data-value]"):
                        d, hm = t.attributes["data-date"], t.attributes["data-value"]
                        offset = (date.fromisoformat(d) - today).days
                        page_url = f"https://www.filmweb.pl{cinema['path']}" + (f"?day={offset}" if offset > 0 else "") \
                            + (f"#{film_id}" if film_id else "")
                        # Two halls at the same minute (2D and 3D) are one entry with both versions.
                        rec = shows.setdefault((title, d, hm), {"url": page_url, "img": big_poster(poster.attributes.get("data-image")) if poster else None,
                                                                "kinds": [], "buy": None})
                        rec["kinds"].append(" ".join(x for x in (t.attributes.get("data-type"), t.attributes.get("data-lang")) if x))
                        link = t.css_first("a.seanceTile__link[href]")
                        if link and link.attributes["href"].startswith("http") and not rec["buy"]:
                            rec["buy"] = link.attributes["href"]
            for (title, d, hm), rec in sorted(shows.items()):
                ev = _screening(self, film=title, cinema=cinema["name"], day=d, hm=hm, url=rec["url"], kinds=rec["kinds"],
                                image=rec["img"], tickets=rec["buy"], **loc)
                if ev:
                    yield ev


class KinoMarzenieSource(Source):
    """Kino Marzenie, Tarnów: the site's per-day listing (/embed/events?start_date=D&end_date=D) has every
    film with its poster, and each screening's time, version and booking link (marzenieonline.tck.pl).
    It shows one day per request whatever the range, so the days are read one by one. A screening
    links to the cinema's repertoire for its day.

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
        day_url = f"{self.BASE}/repertuar?start_date={d}&end_date={d}"
        for item in page.css(".row.item"):
            title_a = item.css_first("h4 a[href]")
            if not title_a:
                continue
            title = " ".join(title_a.text(deep=False, strip=True).split()) or title_a.text(strip=True)
            img = item.css_first("img[src]")
            genre = item.css_first("span.small")
            for a in item.css("a[href*='MSI']"):
                m = HM.search(a.text(separator=" ", strip=True))
                if not m:
                    continue  # past days keep the films but drop their screenings
                version = a.parent.css_first(".flex-wrap") if a.parent else None
                ev = _screening(self, film=title, cinema=self.cfg.name, day=d, hm=m.group(1), url=day_url,
                                kinds=[version.text(strip=True) if version else ""], image=img.attributes["src"] if img else None,
                                extra=genre.text(strip=True) if genre else None, tickets=a.attributes["href"])
                if ev:
                    yield ev


class KinoTarnowSource(Source):
    """Kino Millenium (Centrum Sztuki Mościce), kino.tarnow.pl: the per-day listing
    (/wydarzenia/embed/list?categoryId=5&startDate=D&endDate=D) has each film with its screenings:
    time, hall, version and a booking link (bilety.csm.tarnow.pl). A screening links to the
    cinema's repertoire for its day.

    Options: days (default 14), category_id (default 5: films).
    """

    type = "kinotarnow"
    BASE = "https://kino.tarnow.pl"

    def fetch(self):
        days = int(self.cfg.options.get("days", 14))
        cat = self.cfg.options.get("category_id", 5)
        for n in range(days):
            d = (date.today() + timedelta(days=n)).isoformat()
            try:
                page = HTMLParser(http.get(f"{self.BASE}/wydarzenia/embed/list",
                                           params={"categoryId": cat, "startDate": d, "endDate": d}).text)
            except Exception as e:
                log.info("kino.tarnow.pl %s: %s", d, e)
                break
            time.sleep(0.3)
            yield from self.parse_day(page, d)

    def parse_day(self, page: HTMLParser, d: str):
        day_url = f"{self.BASE}/wydarzenia/repertuar?startDate={d}&endDate={d}"
        for item in page.css(".item-box-1"):
            h3 = item.css_first("h3")
            if not h3:
                continue
            title = h3.text(strip=True)
            img = item.css_first("img[src]")
            genre = item.css_first(".meta-tags li span:last-child")
            for a in item.css("a.btn-ticket[href]"):
                when = a.css_first(".display-4")
                m = HM.search(when.text(separator=" ", strip=True)) if when else None
                if not m:
                    continue
                hall = a.css_first(".text-white")
                version = a.css_first(".text-secondary-300")
                kinds = [" ".join(version.text(strip=True).split()) if version else "", hall.text(strip=True).lower() if hall else ""]
                buy = a.attributes["href"].replace("returnlink=http://localhost/", f"returnlink={self.BASE}/")
                ev = _screening(self, film=title, cinema=self.cfg.name, day=d, hm=m.group(1), url=day_url, kinds=kinds,
                                image=img.attributes["src"] if img else None, extra=genre.text(strip=True) if genre else None,
                                tickets=buy)
                if ev:
                    yield ev


class HeliosSource(Source):
    """Helios cinemas (restapi.helios.pl): screenings for a date range, titles from /api/movie/{id}.
    Helios publishes no booking links; screenings link to the cinema's page.

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
        # Two halls at the same minute (dubbing and subtitles) are one entry with both versions.
        slots: dict[tuple[str, str], list[str]] = {}
        for s in sorted(screenings, key=lambda s: s["screeningTimeFrom"]):
            kind = f"{s.get('printType') or ''} {s.get('speakingType') or s.get('language') or ''}".strip()
            slots.setdefault((s["movieId"], s["screeningTimeFrom"]), []).append(kind)
        for (movie_id, ts), kinds in slots.items():
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
            ev = _screening(self, film=name, cinema=self.cfg.name, day=ts[:10], hm=ts[11:16], url=self.cfg.url,
                            kinds=kinds, image=(movie.get("posters") or [None])[0])
            if ev:
                yield ev
