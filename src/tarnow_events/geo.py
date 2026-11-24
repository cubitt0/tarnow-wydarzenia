"""Geocoding (OpenStreetMap Nominatim, cached forever) and the 50 km radius check."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx
from unidecode import unidecode

from .models import TARNOW_LAT, TARNOW_LON

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim policy: identify the application, max 1 request/second.
USER_AGENT = "tarnow-wydarzenia/0.1 (event aggregator; https://github.com/)"

# Towns that show up most often. Saves geocoder calls and makes the radius check work even
# for events that only say "Dębica". Values are town centres.
KNOWN_TOWNS: dict[str, tuple[float, float]] = {
    "tarnow": (50.0121, 20.9858),
    "debica": (50.0515, 21.4114),
    "brzesko": (49.9692, 20.6083),
    "bochnia": (49.9690, 20.4302),
    "dabrowa tarnowska": (50.1747, 20.9866),
    "tuchow": (49.8946, 21.0540),
    "ryglice": (49.8780, 21.1390),
    "zakliczyn": (49.8571, 20.8106),
    "ciezkowice": (49.7840, 20.9590),
    "wojnicz": (49.9573, 20.8380),
    "zabno": (50.1342, 20.8862),
    "radlow": (50.0810, 20.8460),
    "pilzno": (49.9800, 21.2900),
    "ropczyce": (50.0520, 21.6080),
    "gorlice": (49.6550, 21.1600),
    "biecz": (49.7350, 21.2630),
    "szczucin": (50.3090, 21.0750),
    "mielec": (50.2870, 21.4230),
    "zalipie": (50.2330, 20.8640),
    "skrzyszow": (50.0560, 21.0560),
    "lisia gora": (50.1050, 21.0010),
    "wierzchoslawice": (50.0460, 20.8410),
    "plesna": (49.9230, 20.9450),
    "gromnik": (49.8380, 20.9600),
    "nowy wisnicz": (49.9170, 20.4650),
    "lipnica murowana": (49.8620, 20.5220),
    "czchow": (49.8340, 20.6820),
    "bobowa": (49.7090, 20.9480),
    "szczurowa": (50.1290, 20.6200),
    "brzostek": (49.8800, 21.4100),
    "zabawa": (50.0630, 20.8940),
    "jodlowka-walki": (49.9910, 21.6420),
    "nowy sacz": (49.6175, 20.7153),
    "jaslo": (49.7450, 21.4720),
    "kasna dolna": (49.8130, 20.8570),
    "luslawice": (49.8530, 20.7890),
    "olesno": (50.2010, 20.9230),
    "luzna": (49.7200, 21.0480),
    "zegocina": (49.8150, 20.3380),
    "zyrakow": (50.0880, 21.3730),
    "grybow": (49.6240, 20.9480),
    "wola rzedzinska": (50.0330, 20.9150),
    "nieciecza": (50.1560, 20.8490),
    "solec-zdroj": (50.3630, 20.8880),
    "dabrowa tarnowska": (50.1747, 20.9866),
}


# Proper spelling for towns that sources write without Polish letters ("Tarnow").
TOWN_NAMES = {norm: name for name in (
    "Tarnów", "Dębica", "Brzesko", "Bochnia", "Dąbrowa Tarnowska", "Tuchów", "Ryglice", "Zakliczyn", "Ciężkowice",
    "Wojnicz", "Żabno", "Radłów", "Pilzno", "Ropczyce", "Gorlice", "Biecz", "Szczucin", "Mielec", "Zalipie",
    "Skrzyszów", "Lisia Góra", "Wierzchosławice", "Pleśna", "Gromnik", "Nowy Wiśnicz", "Lipnica Murowana", "Czchów",
    "Bobowa", "Szczurowa", "Brzostek", "Nowy Sącz", "Jasło", "Lusławice", "Olesno", "Łużna", "Żegocina", "Żyraków",
    "Grybów", "Wola Rzędzińska", "Nieciecza", "Solec-Zdrój",
) for norm in [unidecode(name).lower()]}


def canonical_town(town: str | None) -> str | None:
    return TOWN_NAMES.get(norm_town(town) or "", town)


_TOWN_RX = re.compile(r"\b(" + "|".join(sorted(map(re.escape, TOWN_NAMES), key=len, reverse=True)) + r")\b")


def town_in_text(text: str | None) -> str | None:
    """First known town written in an address ("ul. Kopernika 18d, Brzesko" → "Brzesko").
    Word-bounded, so street names like "ul. Dębicka" or "Krakowska" do not count."""
    m = _TOWN_RX.search(unidecode(text or "").lower())
    return TOWN_NAMES[m.group(1)] if m else None


def nearest_town(lat: float, lon: float, max_km: float = 10.0) -> str | None:
    best = min(KNOWN_TOWNS.items(), key=lambda kv: haversine_km(lat, lon, *kv[1]))
    return TOWN_NAMES.get(best[0], best[0].title()) if haversine_km(lat, lon, *best[1]) <= max_km else None


def correct_tarnow(town: str | None, place_text: str | None) -> str | None:
    """Aggregators (allevents) stamp their whole city listing with "Tarnów" even for events in
    Brzesko or Nowy Sącz. If the address names another town and not Tarnów, that town wins."""
    if norm_town(town) != "tarnow":
        return town
    named = town_in_text(place_text)
    return named if named and named != "Tarnów" else town


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_from_tarnow(lat: float, lon: float) -> float:
    return haversine_km(TARNOW_LAT, TARNOW_LON, lat, lon)


def norm_town(town: str | None) -> str | None:
    if not town:
        return None
    return unidecode(town).lower().strip()


class Geocoder:
    def __init__(self, cache_dir: Path, online: bool = True):
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache_dir / "geocode.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS geo (q TEXT PRIMARY KEY, result TEXT)")
        self.online = online
        self._lock = threading.Lock()
        self._last = 0.0
        self.lookups = 0

    def town(self, town: str | None) -> tuple[float, float] | None:
        key = norm_town(town)
        if not key:
            return None
        if key in KNOWN_TOWNS:
            return KNOWN_TOWNS[key]
        return self.query(f"{town}, Polska")

    def query(self, q: str) -> tuple[float, float] | None:
        q = " ".join(q.split())
        with self._lock:
            row = self.db.execute("SELECT result FROM geo WHERE q = ?", (q,)).fetchone()
            if row is not None:
                res = json.loads(row[0])
                return tuple(res) if res else None
            if not self.online:
                return None
            wait = 1.1 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.lookups += 1
            try:
                resp = httpx.get(
                    NOMINATIM,
                    params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "pl"},
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "pl"},
                    timeout=20,
                )
                resp.raise_for_status()
                hits = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                log.info("geocode failed for %r: %s", q, e)
                return None  # not cached: retry on the next run
            res = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
            self.db.execute("INSERT OR REPLACE INTO geo VALUES (?, ?)", (q, json.dumps(res)))
            self.db.commit()
            return res
