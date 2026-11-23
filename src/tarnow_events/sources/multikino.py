from __future__ import annotations

from collections import defaultdict

from .. import http
from .base import Source

API = "https://www.multikino.pl/api/microservice/showings/cinemas/{cinema}/films"


class MultikinoSource(Source):
    """Multikino showtimes. One event per film per day ("Film X", 11:05 / 14:30 / 18:00 in the
    description) instead of one per screening, which would bury every other calendar.

    Options: cinema (Multikino cinema id, Tarnów = "0050")
    """

    type = "multikino"

    def fetch(self):
        cinema = self.cfg.options.get("cinema", "0050")
        data = http.get(API.format(cinema=cinema), bare=True).json()
        for film in data.get("result") or []:
            title = film.get("filmTitle") or film.get("title")
            per_day: dict[str, list[dict]] = defaultdict(list)
            for group in film.get("showingGroups") or []:
                for sess in group.get("sessions") or []:
                    if sess.get("startTime"):
                        per_day[sess["startTime"][:10]].append(sess)
            for day, sessions in sorted(per_day.items()):
                sessions.sort(key=lambda s: s["startTime"])
                times = []
                for s in sessions:
                    attrs = " ".join(a.get("shortName", "") for a in s.get("attributes") or []
                                     if a.get("attributeType") in ("Language", "Session"))
                    times.append(f"{s['startTime'][11:16]} {attrs}".strip())
                ev = self.make_event(
                    title=f"{title} (Multikino)",
                    start=sessions[0]["startTime"],
                    end=sessions[-1].get("endTime"),
                    url=film.get("filmUrl"),
                    description="Seanse: " + " · ".join(times) + (f"\n\n{film['synopsisShort']}" if film.get("synopsisShort") else ""),
                    image=film.get("posterImageSrc"),
                    price=sessions[0].get("formattedPrice"),
                    categories=["kino"],
                )
                if ev:
                    yield ev
