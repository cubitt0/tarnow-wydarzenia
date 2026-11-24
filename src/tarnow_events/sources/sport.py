"""Sport calendars without feeds: the national running calendar and 90minut.pl league fixtures."""

from __future__ import annotations

import html as htmllib
import re
from datetime import date

from .. import http
from ..geo import KNOWN_TOWNS, norm_town
from .base import Source

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
          "sierpnia": 8, "wrzesnia": 9, "września": 9, "pazdziernika": 10, "października": 10,
          "listopada": 11, "grudnia": 12}


def _clean(x: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").split())


class MaratonyPolskieSource(Source):
    """maratonypolskie.pl calendar: a national table (date, town, name). Rows from towns we know
    to be within the radius are kept; the geo filter downstream does the exact check."""

    type = "maratonypolskie"
    ROW = re.compile(
        r"<CENTER>(\d{2})\.(\d{2})\.(\d{4})[^<]*</TD>\s*<TD[^>]*>\s*<FONT[^>]*>([^<]+)</TD>\s*"
        r"<TD[^>]*>\s*<FONT[^>]*>\s*<a class='czte' href='([^']+)'>([^<]+)</a>",
        re.I,
    )

    def fetch(self):
        text = http.get(self.cfg.url).content.decode("iso-8859-2", errors="replace")
        towns = set(KNOWN_TOWNS) | {norm_town(t) for t in self.cfg.options.get("extra_towns", [])}
        for d, m, y, town, href, name in self.ROW.findall(text):
            town = _clean(town)
            if norm_town(town) not in towns:
                continue
            ev = self.make_event(
                title=_clean(name),
                start=f"{y}-{m}-{d}",
                url="https://www.maratonypolskie.pl/" + htmllib.unescape(href),
                town=town,
                categories=["sport"],
            )
            if ev:
                yield ev


class NinetyMinutSource(Source):
    """90minut.pl league page: rounds of "home – away – date" rows. Home matches of the configured
    teams become events once the league has scheduled them (spring rounds are dateless until then).

    Options:
      home_teams: {team name: town}
      league: label appended to the title (e.g. "IV liga")
    """

    type = "90minut"
    ROW = re.compile(
        r'<tr align="left">\s*<td nowrap valign="top" width="180">(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*'
        r'<td nowrap valign="top" width="180">(.*?)</td>\s*<td[^>]*>(.*?)</td>',
        re.S,
    )
    WHEN = re.compile(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)(?:\s+(\d{4}))?,?\s*(\d{1,2}:\d{2})?", re.I)

    def fetch(self):
        text = http.get(self.cfg.url).content.decode("iso-8859-2", errors="replace")
        teams = self.cfg.options.get("home_teams", {})
        league = self.cfg.options.get("league", "")
        today = date.today()
        for home, score, away, when in self.ROW.findall(text):
            home, away, when = _clean(home), _clean(away), _clean(when)
            if home not in teams or not when:
                continue
            m = self.WHEN.match(when)
            month = MONTHS.get((m.group(2) or "").lower()) if m else None
            if not month:
                continue
            day, year, hm = int(m.group(1)), m.group(3), m.group(4)
            if year:
                y = int(year)
            else:  # no year: the date nearest to today within a season (-6 / +8 months)
                y = today.year
                if (date(y, month, 1) - today).days < -183:
                    y += 1
                elif (date(y, month, 1) - today).days > 245:
                    y -= 1
            ev = self.make_event(
                title=f"{home} – {away}" + (f" ({league})" if league else ""),
                start=f"{y}-{month:02d}-{day:02d}" + (f"T{hm}" if hm else ""),
                url=self.cfg.url,
                description=f"Mecz {league}. Wynik: {score}" if score.strip() not in ("", "-") else f"Mecz {league}, gospodarz: {home}.",
                location_name=f"Stadion – {home}",
                town=teams[home],
                categories=["sport"],
            )
            if ev:
                yield ev
