"""Static pages for search engines and link previews.

The calendar (index.html) is drawn by JavaScript and addresses events by #fragment, which search
engines do not index. Every upcoming event therefore also gets a plain HTML page at
/wydarzenie/<date>/<slug>/ with its own title, description, Open Graph tags and schema.org Event
data; sitemap.xml + robots.txt point crawlers at them, and the home page carries a plain list of the
coming days for crawlers that do not run JavaScript. Pages of past events stay for a year (archive.py).
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path

from .models import CATEGORIES, TZ, Event
from .series import is_long

SITE = "Wydarzenia Tarnów"
DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
          "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]
# NFKD does not decompose ł; everything else (ą, ę, ó, ś, ż…) loses its diacritic there.
_TRANSLIT = str.maketrans({"ł": "l", "Ł": "L"})


def slugify(text: str, max_len: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "wydarzenie"


def page_paths(events: list[Event], keep: dict[str, str] | None = None) -> dict[str, str]:
    """uid -> site path ("wydarzenie/2026-11-28/koncert-kult/"). Same-day namesakes get a uid suffix.
    keep: uid -> earlier path; an event keeps its address while its date stays, even if its title is corrected."""
    keep = keep or {}
    paths: dict[str, str] = {}
    for e in events:
        p = keep.get(e.uid)
        if p and p.startswith(f"wydarzenie/{e.day.isoformat()}/") and p not in paths.values():
            paths[e.uid] = p
    taken = set(paths.values())
    for e in events:
        if e.uid in paths:
            continue
        base = f"wydarzenie/{e.day.isoformat()}/{slugify(e.title)}"
        path = base + "/" if base + "/" not in taken else f"{base}-{re.sub(r'[^a-z0-9]', '', e.uid.lower())[:6]}/"
        taken.add(path)
        paths[e.uid] = path
    return paths


# Cinema screenings are not in the main feeds, but each still gets a page, so a shared link shows the
# poster and showtime and leads to the ticket. Hundreds of near-identical pages would only dilute
# search results, so they are noindex and stay out of the sitemap.
def is_screening(e: Event) -> bool:
    return not e.in_main and "kino" in e.categories


def film_title(title: str) -> str:
    """"Jestem Rocky (Kino Millenium (CSM))" -> "Jestem Rocky": drops a trailing (cinema), nested brackets too."""
    t = title.strip()
    if t.endswith(")"):
        depth = 0
        for i in range(len(t) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(t[i], 0)
            if depth == 0:
                if t[:i].strip():
                    t = t[:i].strip()
                break
    return t


def screening_paths(shows: list[Event], taken: set[str]) -> dict[str, str]:
    """uid -> "wydarzenie/2026-11-29/jestem-rocky/kino-millenium-csm-20-15/"; two shows at one time get -2."""
    paths: dict[str, str] = {}
    for e in shows:
        s = e.start.astimezone(TZ)
        place = slugify(re.sub(r"\(.*", "", e.location_name or "") or e.town or "kino", 30)
        base = f"wydarzenie/{s.date().isoformat()}/{slugify(film_title(e.title))}/{place}-{s:%H-%M}"
        path, n = base, 2
        while path in taken:
            path, n = f"{base}-{n}", n + 1
        taken.add(path)
        paths[e.uid] = path + "/"
    return paths


def long_date(d) -> str:
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year}"


def when_text(e: Event) -> str:
    s = e.start.astimezone(TZ)
    end = e.end.astimezone(TZ) if e.end else None
    if e.all_day:  # the model's all-day end is the last day itself (sources/ics.py, ical_out.py)
        if end and end.date() > s.date():
            return f"{long_date(s.date())} – {long_date(end.date())}"
        return f"{long_date(s.date())}, cały dzień"
    out = f"{long_date(s.date())}, {s:%H:%M}"
    if end and end.date() == s.date():
        out += f"–{end:%H:%M}"
    elif end:
        out += f" – {long_date(end.date())}, {end:%H:%M}"
    return out


def _desc(text: str | None, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(" ,.;:–-") + "…"


def _jsonld(data: dict) -> str:
    return ('<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + "</script>")


STYLE = """:root{--bg:#faf8f5;--card:#fff;--fg:#1c1b1a;--mut:#4b4844;--line:#cdc8c2;--acc:#1c1b1a;--chip:#eeebe7}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--card:#1c1b1a;--fg:#e9e5e1;--mut:#cbc6c0;--line:#4a4743;--acc:#f0ece8;--chip:#2a2826}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}a{color:var(--acc)}
header{border-bottom:1px solid var(--line)}header .wrap{display:flex;align-items:center;gap:12px;padding:12px 16px}
header a{font-weight:700;text-decoration:none;font-size:18px}nav.crumbs{font-size:13px;color:var(--mut);margin:8px 0 16px}
nav.crumbs a{color:var(--mut)}h1{font-size:clamp(24px,5vw,34px);line-height:1.2;margin:0 0 12px}
.hero{width:100%;max-height:420px;object-fit:contain;background:var(--chip);border-radius:16px;margin:0 0 16px;display:block}
.cats{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}.cat{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:13px}
.x{color:#b3261e;font-weight:700}.note{background:var(--chip);border-radius:12px;padding:10px 14px;margin:0 0 16px}dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:0 0 16px}
dt{color:var(--mut)}dd{margin:0}.desc{white-space:pre-line;overflow-wrap:anywhere}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.btn{display:inline-block;padding:10px 18px;border-radius:999px;
border:1px solid var(--line);text-decoration:none;font-weight:600}.btn.f{background:var(--acc);color:var(--bg);border-color:var(--acc)}
h2{font-size:20px;margin:28px 0 8px}ul.list{list-style:none;padding:0;margin:0}ul.list li{padding:8px 0;border-bottom:1px solid var(--line)}
ul.list small{color:var(--mut);display:block}.t{color:var(--mut);display:inline-block;min-width:3.2em}
footer{border-top:1px solid var(--line);margin-top:32px;padding:16px 0 40px;font-size:13px;color:var(--mut)}footer a{color:var(--mut)}"""


def _page(base: str, path: str, title: str, desc: str, body: str, *, image: str | None = None,
          og_type: str = "website", ld: list[dict] = (), noindex: bool = False) -> str:
    url = f"{base}/{path}"
    meta = [
        f'<title>{escape(title)}</title>',
        f'<meta name="description" content="{escape(desc)}">',
        '<meta name="robots" content="noindex">' if noindex else f'<link rel="canonical" href="{escape(url)}">',
        f'<meta property="og:site_name" content="{SITE}">',
        '<meta property="og:locale" content="pl_PL">',
        f'<meta property="og:type" content="{og_type}">',
        f'<meta property="og:title" content="{escape(title)}">',
        f'<meta property="og:description" content="{escape(desc)}">',
        f'<meta property="og:url" content="{escape(url)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{escape(title)}">',
        f'<meta name="twitter:description" content="{escape(desc)}">',
    ]
    # The event's poster when it has one, otherwise the site's own preview image.
    img = image or f"{base}/og-image.png"
    meta += [f'<meta property="og:image" content="{escape(img)}">', f'<meta name="twitter:image" content="{escape(img)}">',
             f'<meta property="og:image:alt" content="{escape(title)}">']
    if not image:
        meta += ['<meta property="og:image:width" content="1200">', '<meta property="og:image:height" content="630">',
                 '<meta property="og:image:type" content="image/png">']
    meta += [_jsonld(d) for d in ld]
    return f"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{chr(10).join(meta)}
<link rel="icon" href="{base}/favicon.ico" sizes="48x48">
<link rel="icon" href="{base}/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{base}/apple-touch-icon.png">
<style>{STYLE}</style>
</head>
<body>
<header><div class="wrap"><a href="/">Tarnów – wydarzenia</a></div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap"><a href="/">Kalendarz</a> · <a href="/zrodla.html">Źródła wydarzeń</a></footer>
</body>
</html>
"""


PAST, GONE = "past", "gone"  # archived pages (archive.py): the event took place / vanished from its sources


def event_page(base: str, e: Event, path: str, siblings: list[tuple[Event, str]], *, screening: bool = False,
               status: str | None = None) -> str:
    """status PAST or GONE: an archived page; siblings are then upcoming events to go to instead."""
    s = e.start.astimezone(TZ)
    place = e.location_line()
    cats = [CATEGORIES.get(c, c) for c in e.categories]
    title = f"{'[ODWOŁANE] ' if e.cancelled else ''}{e.title} – {s.day} {MONTHS[s.month - 1]} {s.year}" \
            f"{', ' + e.town if e.town else ''} | {SITE}"
    desc = _desc(f"{when_text(e)}{'. ' + place if place else ''}. {e.description or ', '.join(cats)}", 160)
    src = e.all_sources()

    loc: dict = {"@type": "Place", "name": e.location_name or e.town or "Tarnów",
                 "address": {"@type": "PostalAddress", "addressLocality": e.town or "Tarnów",
                             "addressCountry": "PL", **({"streetAddress": e.address} if e.address else {})}}
    if e.lat is not None:
        loc["geo"] = {"@type": "GeoCoordinates", "latitude": round(e.lat, 5), "longitude": round(e.lon, 5)}
    ld: dict = {
        "@context": "https://schema.org", "@type": "Event", "name": e.title,
        "startDate": s.date().isoformat() if e.all_day else s.isoformat(),
        "eventStatus": "https://schema.org/EventCancelled" if e.cancelled else "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": loc, "url": f"{base}/{path}", "inLanguage": "pl",
    }
    if e.end:
        ld["endDate"] = e.end.astimezone(TZ).date().isoformat() if e.all_day else e.end.astimezone(TZ).isoformat()
    if e.description:
        ld["description"] = _desc(e.description, 500)
    if e.image:
        ld["image"] = [e.image]
    if e.tickets and not status:
        ld["offers"] = {"@type": "Offer", "url": e.tickets, "availability": "https://schema.org/InStock"}
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Kalendarz", "item": f"{base}/"},
        {"@type": "ListItem", "position": 2, "name": e.title},
    ]}

    rows = [f"<dt>Kiedy</dt><dd>{escape(when_text(e))}</dd>"]
    if place:
        maps = "https://www.google.com/maps/search/?api=1&query=" + escape(
            place if e.location_name else f"{e.lat},{e.lon}" if e.lat is not None else place, quote=True)
        rows.append(f'<dt>Gdzie</dt><dd>{escape(place)} · <a href="{maps}" rel="noopener">mapa</a></dd>')
    if e.price:
        rows.append(f"<dt>Cena</dt><dd>{escape(e.price)}</dd>")
    rows.append("<dt>Źródła</dt><dd>" + ", ".join(
        f'<a href="{escape(r.url)}" rel="noopener nofollow">{escape(r.source_name)}</a>' for r in src) + "</dd>")

    desc_html = ""
    if e.description:
        from .pipeline import short_desc  # one cut-off rule for the calendar and these pages
        desc_html = f'<p class="desc">{escape(short_desc(e.description))}</p>'
    # An archived event is no longer in the calendar, and its tickets are not for sale.
    btns = [f'<a class="btn f" href="/">Zobacz nadchodzące wydarzenia</a>' if status else
            f'<a class="btn f" href="/#e={escape(e.uid)}">Pokaż w kalendarzu</a>']
    if e.tickets and not e.cancelled and not status:
        btns.insert(0, f'<a class="btn f" href="{escape(e.tickets)}" rel="noopener nofollow">Kup bilet</a>')
    btns.append(f'<a class="btn" href="{escape(src[0].url)}" rel="noopener nofollow">Strona organizatora</a>')
    note = ""
    if status == GONE:
        note = ('<p class="note">Tego wydarzenia nie ma już w kalendarzu organizatora – mogło zostać odwołane '
                'albo przeniesione na inny termin. Sprawdź na stronie organizatora.</p>')
    elif status == PAST and not e.cancelled:
        note = '<p class="note">To wydarzenie już się odbyło.</p>'
    more = ""
    if siblings:
        def when(o: Event) -> str:
            t = o.start.astimezone(TZ)
            dated = screening or status
            return "cały dzień" if o.all_day else f"{DAYS[t.weekday()][:3]} {t.day}.{t.month:02d}, {t:%H:%M}" if dated else f"{t:%H:%M}"
        items = "".join(
            f'<li><span class="t">{when(o)}</span> '
            f'<a href="/{p}">{escape(o.title)}</a><small>{escape(o.location_line() or "")}</small></li>'
            for o, p in siblings[:12])
        heading = "Inne seanse" if screening else "Nadchodzące wydarzenia" if status else "Tego samego dnia"
        more = f"<h2>{heading}</h2><ul class=\"list\">{items}</ul>"

    # The date opens that day in the calendar; a long exhibition is on today's page anyway, and an
    # archived event's day has left the calendar.
    day = "/" if is_long(e) or status else f"/#dzien={s.date().isoformat()}"
    body = f"""<nav class="crumbs"><a href="/">Kalendarz</a> › <a href="{day}">{"Wystawy wielodniowe" if is_long(e) else escape(long_date(s.date()))}</a></nav>
<article>
{f'<img class="hero" src="{escape(e.image)}" alt="{escape(e.title)}" referrerpolicy="no-referrer">' if e.image else ""}
<div class="cats">{"".join(f'<span class="cat">{escape(c)}</span>' for c in cats)}</div>
<h1>{'<span class="x">Odwołane: </span>' if e.cancelled else ""}{escape(e.title)}</h1>
{note}<dl>{"".join(rows)}</dl>
{desc_html}
<div class="btns">{"".join(btns)}</div>
</article>
{more}"""
    # A removed event, or a cancelled one now past, is of no use to anyone searching: drop it from the index.
    hide = screening or status == GONE or (status == PAST and e.cancelled)
    return _page(base, path, title, desc, body, image=e.image, og_type="article",
                 ld=[crumbs] if status == GONE else [ld, crumbs], noindex=hide)


def related(e: Event, upcoming: list[Event], paths: dict[str, str], n: int = 6) -> list[tuple[Event, str]]:
    """Upcoming events for an archived page: same venue first, then the same kind, soonest first."""
    pool = [o for o in upcoming if not is_long(o) and o.uid != e.uid]
    venue = [o for o in pool if e.location_name and o.location_name == e.location_name]
    seen = {o.uid for o in venue}
    kind = [o for o in pool if o.uid not in seen and set(o.categories) & set(e.categories)]
    return [(o, paths[o.uid]) for o in (venue[:n // 2] + kind)[:n]]


def redirect_page(base: str, path: str, title: str) -> str:
    """The old address of an event that moved to another date: an instant redirect, read as permanent."""
    url = f"{base}/{path}"
    return (f'<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>{escape(title)}</title>'
            f'<link rel="canonical" href="{escape(url)}"><meta http-equiv="refresh" content="0; url=/{escape(path)}">'
            f'</head><body><p>Strona wydarzenia jest teraz tutaj: <a href="/{escape(path)}">{escape(title)}</a></p></body></html>')


def home_list(events: list[Event], paths: dict[str, str], now: datetime, days: int = 7) -> str:
    """Plain list of the coming days for the home page, for crawlers that do not run JavaScript.
    The page hides it before it is drawn when scripts run (html.js), so visitors never see it."""
    last = now.astimezone(TZ).date().toordinal() + days
    by_day: dict = {}
    for e in sorted((e for e in events if e.uid in paths and e.in_main and not is_long(e)), key=lambda e: (e.start, e.title)):
        if (e.end or e.start) >= now and e.day.toordinal() < last:
            by_day.setdefault(e.day, []).append(e)
    sections = "".join(
        f'<h3>{escape(long_date(d)).capitalize()}</h3><ul>'
        + "".join(f'<li>{"cały dzień" if e.all_day else f"{e.start.astimezone(TZ):%H:%M}"} '
                  f'<a href="{paths[e.uid]}">{escape(e.title)}</a>'
                  + (f' <small>{escape(e.location_line())}</small>' if e.location_line() else "") + "</li>" for e in evs)
        + "</ul>" for d, evs in by_day.items())
    return (f'<section class="static-list" aria-label="Najbliższe wydarzenia"><h1>Wydarzenia w Tarnowie i okolicach – najbliższe {days} dni</h1>'
            f'{sections}</section>')


NOT_FOUND = """<h1>Nie znaleziono strony</h1>
<p>To wydarzenie mogło się już odbyć albo zmienić termin. Aktualne wydarzenia znajdziesz w kalendarzu.</p>
<div class="btns"><a class="btn f" href="/">Otwórz kalendarz</a></div>"""


def write_seo(out: Path, base: str, events: list[Event], now: datetime, archived: dict[str, dict] | None = None,
              fetched: set[str] | None = None) -> dict[str, str]:
    """Write event pages, sitemap.xml, robots.txt and 404.html. Returns uid -> path of today's events.

    archived: the page archive (archive.py), updated in place; its events that are over or gone keep
    their pages. fetched: the sources read successfully in this run (None: all)."""
    from .archive import update

    archived = {} if archived is None else archived
    upcoming = sorted((e for e in events if e.in_main and (e.end or e.start) >= now),
                      key=lambda e: (e.start, e.title))
    paths = page_paths(upcoming, {uid: r["path"] for uid, r in archived.items()})
    kept = update(archived, events, paths, now, fetched)
    # Screenings are not archived (noindex, hundreds a week), but must not take an archived address.
    shows = sorted((e for e in events if is_screening(e) and e.start >= now), key=lambda e: (e.start, e.title))
    show_paths = screening_paths(shows, {p.rstrip("/") for p in [*paths.values(), *(r["path"] for _, r in kept)]})
    shutil.rmtree(out / "wydarzenie", ignore_errors=True)  # rebuilt whole: today's events + the archive
    by_day: dict = {}
    for e in upcoming:
        by_day.setdefault(e.day, []).append(e)

    def write(path: str, html: str) -> None:
        f = out / path / "index.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(html, encoding="utf-8")

    for e in upcoming:
        write(paths[e.uid], event_page(base, e, paths[e.uid], [(o, paths[o.uid]) for o in by_day[e.day] if o.uid != e.uid]))
    for e, rec in kept:
        if rec["gone"] or (e.end or e.start) < now:
            write(rec["path"], event_page(base, e, rec["path"], related(e, upcoming, paths),
                                          status=GONE if rec["gone"] else PAST))
        else:  # not listed today, but its source could not be read: the page stays as it was
            write(rec["path"], event_page(base, e, rec["path"], [(o, paths[o.uid]) for o in by_day.get(e.day, [])]))
    pages = {*paths.values(), *show_paths.values(), *(r["path"] for _, r in kept)}
    for uid, rec in archived.items():
        for old in rec.get("old", []):
            if old not in pages:
                write(old, redirect_page(base, rec["path"], rec["event"]["title"]))
    by_film: dict = {}
    for e in shows:
        by_film.setdefault(slugify(film_title(e.title)), []).append(e)
    for e in shows:
        # Other showtimes of the film, nearest first: same cinema before other cinemas.
        others = sorted((o for o in by_film[slugify(film_title(e.title))] if o.uid != e.uid),
                        key=lambda o: (o.location_name != e.location_name, o.start))
        f = out / show_paths[e.uid] / "index.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(event_page(base, e, show_paths[e.uid], [(o, show_paths[o.uid]) for o in others], screening=True),
                     encoding="utf-8")
    shutil.rmtree(out / "wydarzenia", ignore_errors=True)  # the old day-by-day list page, now gone
    (out / "404.html").write_text(_page(base, "404.html", f"Nie znaleziono | {SITE}", "Nie znaleziono strony.",
                                        NOT_FOUND, noindex=True), encoding="utf-8")

    today = now.date().isoformat()
    urls = [(f"{base}/", today, "daily", "1.0"), (f"{base}/zrodla.html", today, "weekly", "0.3")]
    urls += [(f"{base}/{paths[e.uid]}", None, None, "0.6") for e in upcoming]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod, freq, prio in urls:
        lines.append(f"<url><loc>{escape(loc)}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "")
                     + (f"<changefreq>{freq}</changefreq>" if freq else "") + f"<priority>{prio}</priority></url>")
    lines.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    return {**paths, **show_paths}
