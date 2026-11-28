from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from dateutil import parser as dtparser

from ..config import Settings, SourceConfig
from ..pictures import content_images
from ..models import TZ, Event, SourceRef

log = logging.getLogger(__name__)


class Source:
    """One configured source. Subclasses implement fetch() and yield Events."""

    type: str = ""

    def __init__(self, cfg: SourceConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        cfg.url = expand_url(cfg.url)
        if cfg.options.get("pages"):
            cfg.options["pages"] = [expand_url(u) for u in cfg.options["pages"]]

    def fetch(self) -> Iterable[Event]:  # pragma: no cover - interface
        raise NotImplementedError

    # --- helpers shared by all adapters -------------------------------------------------

    def ref(self, url: str | None, description: str | None = None) -> SourceRef:
        return SourceRef(self.cfg.id, self.cfg.name, self.readable_url(url, description))

    def readable_url(self, url: str | None, description: str | None = None) -> str:
        """The link people click must open a page they can read, never the feed/API we scraped.
        Order: the event's own link, a link from its description, the configured homepage,
        the source page itself, the site's front page."""
        url = absolutize(url, self.cfg.url)
        if url and not is_machine_url(url):
            return url
        for found in URL_RE.findall(description or ""):
            found = found.rstrip(".,;:!?)»”'\"")
            if not is_machine_url(found):
                return found
        home = self.cfg.options.get("homepage")
        if home:
            return home
        if not is_machine_url(self.cfg.url):
            return self.cfg.url
        parts = urlsplit(self.cfg.url)
        host = parts.netloc.removeprefix("api.")
        return f"{parts.scheme}://{host}/"

    def make_event(
        self,
        *,
        title: str | None,
        start: Any,
        url: str | None = None,
        end: Any = None,
        all_day: bool | None = None,
        description: str | None = None,
        location_name: str | None = None,
        address: str | None = None,
        town: str | None = None,
        lat: Any = None,
        lon: Any = None,
        image: str | None = None,
        images: list[str] | None = None,
        price: str | None = None,
        categories: list[str] | None = None,
        cancelled: bool = False,
    ) -> Event | None:
        """images: extra pictures; by default the ones inside an HTML description."""
        title = clean_text(title)
        start_dt, start_all_day = to_datetime(start)
        if not title or start_dt is None:
            return None
        end_dt, _ = to_datetime(end) if end else (None, False)
        if end_dt is not None and end_dt < start_dt:
            end_dt = None
        loc = self.cfg.location or {}
        ev = Event(
            title=title,
            start=start_dt,
            end=end_dt,
            all_day=start_all_day if all_day is None else all_day,
            source=self.ref(url, description),
            description=clean_text(description, keep_newlines=True),
            location_name=clean_text(location_name) or loc.get("name"),
            address=clean_text(address) or loc.get("address"),
            town=clean_town(town) or loc.get("town") or self.cfg.town,
            lat=to_float(lat) if lat is not None else loc.get("lat"),
            lon=to_float(lon) if lon is not None else loc.get("lon"),
            image=absolutize(image, self.cfg.url),
            images=[u for u in (absolutize(i, self.cfg.url) for i in images) if u] if images is not None
            else content_images(description, self.cfg.url),
            price=clean_text(price),
            categories=[c for c in (categories or []) if c] or [self.cfg.category],
            cancelled=cancelled,
            priority=self.cfg.priority,
            in_main=bool(self.cfg.options.get("main_feed", True)),
        )
        return ev

    def default_location_text(self) -> str:
        loc = self.cfg.location or {}
        parts = [loc.get("name"), loc.get("address"), loc.get("town") or self.cfg.town]
        return ", ".join(p for p in parts if p)


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t ​]+")


def clean_text(text: str | None, keep_newlines: bool = False) -> str | None:
    if text is None:
        return None
    text = str(text)
    if "<" in text:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
        text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text)
    if keep_newlines:
        text = re.sub(r"\n\s*\n+", "\n\n", "\n".join(line.strip() for line in text.splitlines()))
    else:
        text = " ".join(text.split())
    text = text.strip()
    return text or None


def expand_url(url: str) -> str:
    """Date placeholders for listing URLs: {today}, {today+60}, {year}, {month}, {next_month}, {next_year}."""
    today = date.today()
    nm_year, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)

    def repl(m: re.Match) -> str:
        name, delta = m.group(1), m.group(2)
        if name == "today":
            return (today + timedelta(days=int(delta or 0))).isoformat()
        return str({"year": today.year, "month": today.month, "next_month": nm, "next_year": nm_year}[name])

    return re.sub(r"\{(today|year|month|next_month|next_year)([+-]\d+)?\}", repl, url)


def clean_town(town: str | None) -> str | None:
    """'33-100 Tarnów' → 'Tarnów'."""
    t = clean_text(town)
    if not t:
        return None
    t = re.sub(r"^\d{2}-\d{3}\s*", "", t).strip(" ,")
    return t or None


def to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_datetime(v: Any) -> tuple[datetime | None, bool]:
    """Returns (tz-aware datetime in Europe/Warsaw, is_all_day)."""
    if v is None or v == "":
        return None, False
    if isinstance(v, datetime):
        dt = v
        all_day = False
    elif isinstance(v, date):
        return datetime.combine(v, time.min, TZ), True
    elif isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, TZ), False
    else:
        s = str(v).strip()
        all_day = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
        try:
            dt = dtparser.isoparse(s) if re.match(r"\d{4}-\d{2}-\d{2}", s) else dtparser.parse(s, dayfirst=True)
        except (ValueError, OverflowError):
            return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ), all_day


def absolutize(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    return urljoin(base, url)


URL_RE = re.compile(r"https?://[^\s<>\"']+")
MACHINE_URL = re.compile(
    r"\.(?:ics|xml|json|rss|atom)(?:[?#]|$)|/(?:rss|feed|atom|ical|ics|wp-json|api)(?:/|[?#]|$)"
    r"|[?&](?:format=feed|ical=|outlook-ical|feed=)|//api\.",
    re.I,
)


def is_machine_url(url: str | None) -> bool:
    """Feeds, calendar files and APIs: useful to us, unreadable for a person."""
    return bool(url and MACHINE_URL.search(url))
