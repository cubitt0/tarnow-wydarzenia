"""Find where to buy tickets for an event.

Cheapest first: one of the event's own sources is a ticket shop (eBilet, Bilety24…), or its
description links to one. Otherwise the event's page is scanned (by pages.py, together with the
image lookup) for links to ticket shops, and a link is taken only if it clearly belongs to this
event: it names the event in its address, it is the page's schema.org offer, or it is the only
ticket link in the page's content (links in headers, footers, menus and sidebars do not count).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from unidecode import unidecode

from .models import Event
from .sources.base import URL_RE
from .sources.jsonld import extract_jsonld_events

TICKET_DOMAINS = (
    "ebilet.pl", "bilety24.pl", "biletyna.pl", "kupbilecik.pl", "kupbilecik.com", "eventim.pl",
    "ticketmaster.pl", "goingapp.pl", "biletomat.pl", "ewejsciowki.pl", "abilet.pl", "evenea.pl",
    "bilety.fm", "proticket.pl", "kicket.com", "eventbrite.com", "eventbrite.pl", "ticketos.pl",
    "biletin.pl", "tix.pl", "bilety.pl", "ticketpro.pl", "app.konfeo.com", "konfeo.com",
    "marzenieonline.tck.pl",  # Kino Marzenie / TCK box office
)
# Venues' own box offices: bilety.teatr.pl, ticket.mck.pl, …
TICKET_SUBDOMAIN = re.compile(r"^(?:bilety?|tickets?|kasa-?biletowa|e-?bilety?)\.", re.I)
CHROME = re.compile(r"footer|header|menu|nav|sidebar|widget|cookie|breadcrumb|related|polecane|social", re.I)
MAX_CANDIDATES = 30
# Title words too common to identify an event in a ticket address.
STOP = {"koncert", "koncertu", "spektakl", "tarnow", "tarnowie", "tarnowa", "wystawa", "festiwal", "zaprasza",
        "zapraszamy", "teatr", "teatru", "bilety", "dzieci", "dzien", "wieczor", "edycja", "muzyki", "orkiestra"}


# "Wstęp wolny", "Udział w zawodach jest bezpłatny", "bezpłatne zajęcia", "darmowe wejściówki"…
FREE = re.compile(
    r"\b(?:wst[eę]p|wej[sś]cie|udzia[łl])\b[\s:–-]*(?:\w+\s+){0,3}?(?:wolny|wolne|bezp[łl]atn\w*|darmow\w*)"
    r"|\b(?:bezp[łl]atn|darmow)\w*\s+(?:wst[eę]p|wej[sś]ci\w*|udzia[łl]|zaj[eę]cia|warsztaty|koncert|spektakl"
    r"|seans|pokaz|wydarzeni\w*|impreza|spotkanie|wyk[łl]ad)"
    r"|\bfree (?:entry|admission)\b",
    re.I,
)
# …but not when it is free only for some ("wstęp wolny dla dzieci do lat 3") or tickets cost money.
FREE_FOR_SOME = re.compile(r"^\W*(?:\w+\s+){0,3}?(?:do\s+lat|dla\s+(?:posiadacz|cz[łl]onk)|z\s+kart|po\s+okazaniu|za\s+okazaniem)", re.I)
PAID = re.compile(r"\d+(?:[.,]\d+)?\s*(?:zł|pln)\b|\bbilet\w*\s*:?\s*\d", re.I)


def says_free(text: str | None) -> bool:
    """The text says entry is free, for everyone, and names no price."""
    if not text or PAID.search(text):
        return False
    return any(not FREE_FOR_SOME.match(text[m.end():m.end() + 60]) for m in FREE.finditer(text))


def infer_free(ev: Event, text: str | None = None) -> bool:
    """No price from the source, but the description says "wstęp wolny": the event is free."""
    if ev.price or not says_free(text if text is not None else f"{ev.title}\n{ev.description or ''}"):
        return False
    ev.price = "Wstęp wolny"
    return True


def is_ticket_url(url: str | None) -> bool:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").removeprefix("www.")
    if not host or len(parts.path.strip("/")) < 2:  # a shop's front page does not sell this event
        return False
    return host.endswith(TICKET_DOMAINS) or bool(TICKET_SUBDOMAIN.match(host))


def free_ticket(ev: Event) -> str | None:
    """A ticket link we already have: a ticket-shop source, or a shop link in the description."""
    for ref in ev.all_sources():
        if is_ticket_url(ref.url):
            return ref.url
    for url in URL_RE.findall(ev.description or ""):
        url = url.rstrip(".,;:!?)»”'\"")
        if is_ticket_url(url):
            return url
    return None


def _in_chrome(node) -> bool:
    while node is not None and node.tag not in ("body", "html"):
        if node.tag in ("header", "footer", "nav", "aside"):
            return True
        attrs = node.attributes or {}
        if CHROME.search(f"{attrs.get('class') or ''} {attrs.get('id') or ''}"):
            return True
        node = node.parent
    return False


def page_ticket_candidates(html: str, page_url: str) -> list[dict]:
    """Ticket-shop links on a page: {u: url, x: link text, c: 1 if in header/footer/menu, j: 1 if schema.org offer}."""
    out: dict[str, dict] = {}
    for node in extract_jsonld_events(html):
        offers = node.get("offers") or []
        for offer in offers if isinstance(offers, list) else [offers]:
            url = urljoin(page_url, offer.get("url") or "") if isinstance(offer, dict) else ""
            if is_ticket_url(url):
                out.setdefault(url, {"u": url, "x": "", "c": 0, "j": 1})
    for a in HTMLParser(html).css("a[href]"):
        url = urljoin(page_url, (a.attributes.get("href") or "").strip())
        if url in out or not is_ticket_url(url):
            continue
        out[url] = {"u": url, "x": a.text(strip=True)[:80], "c": int(_in_chrome(a)), "j": 0}
        if len(out) >= MAX_CANDIDATES:
            break
    return list(out.values())


def _title_words(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", unidecode(title).lower())
    return {w for w in words if len(w) >= 5 and w not in STOP}


def choose_ticket(ev: Event, candidates: list[dict]) -> str | None:
    content = [c for c in candidates if not c.get("c")]
    if not content:
        return None
    for c in content:
        if c.get("j"):
            return c["u"]
    words = _title_words(ev.title)
    for c in content:
        address = unidecode(unquote(c["u"])).lower()
        if words and any(w in address for w in words):
            return c["u"]
    distinct = {c["u"].split("?")[0].rstrip("/") for c in content}
    return content[0]["u"] if len(distinct) == 1 else None  # the page's one and only ticket link
