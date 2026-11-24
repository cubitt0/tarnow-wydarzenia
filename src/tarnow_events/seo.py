"""Static pages for search engines and link previews.

The calendar (index.html) is drawn by JavaScript and addresses events by #fragment, which search
engines do not index. Every upcoming event therefore also gets a plain HTML page at
/wydarzenie/<date>/<slug>/ with its own title, description, Open Graph tags and schema.org Event
data; /wydarzenia/ lists them all by day, and sitemap.xml + robots.txt point crawlers at them.
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


def page_paths(events: list[Event]) -> dict[str, str]:
    """uid -> site path ("wydarzenie/2026-11-28/koncert-kult/"). Same-day namesakes get a uid suffix."""
    paths: dict[str, str] = {}
    taken: set[str] = set()
    for e in events:
        base = f"wydarzenie/{e.day.isoformat()}/{slugify(e.title)}"
        path = base if base not in taken else f"{base}-{re.sub(r'[^a-z0-9]', '', e.uid.lower())[:6]}"
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
.x{color:#b3261e;font-weight:700}dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:0 0 16px}
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
<footer class="wrap"><a href="/">Kalendarz</a> · <a href="/wydarzenia/">Wszystkie wydarzenia</a> ·
<a href="/zrodla.html">Źródła i subskrypcje iCal</a></footer>
</body>
</html>
"""


def event_page(base: str, e: Event, path: str, siblings: list[tuple[Event, str]]) -> str:
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
    if e.tickets:
        ld["offers"] = {"@type": "Offer", "url": e.tickets, "availability": "https://schema.org/InStock"}
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Kalendarz", "item": f"{base}/"},
        {"@type": "ListItem", "position": 2, "name": "Wydarzenia", "item": f"{base}/wydarzenia/"},
        {"@type": "ListItem", "position": 3, "name": e.title},
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
    btns = [f'<a class="btn f" href="/#e={escape(e.uid)}">Pokaż w kalendarzu</a>']
    if e.tickets and not e.cancelled:
        btns.insert(0, f'<a class="btn f" href="{escape(e.tickets)}" rel="noopener nofollow">Kup bilet</a>')
    btns.append(f'<a class="btn" href="{escape(src[0].url)}" rel="noopener nofollow">Strona organizatora</a>')
    more = ""
    if siblings:
        items = "".join(
            f'<li><span class="t">{"cały dzień" if o.all_day else f"{o.start.astimezone(TZ):%H:%M}"}</span> '
            f'<a href="/{p}">{escape(o.title)}</a><small>{escape(o.location_line() or "")}</small></li>'
            for o, p in siblings[:12])
        more = f"<h2>Tego samego dnia</h2><ul class=\"list\">{items}</ul>"

    anchor = "wystawy" if is_long(e) else s.date().isoformat()
    body = f"""<nav class="crumbs"><a href="/">Kalendarz</a> › <a href="/wydarzenia/#{anchor}">{"Wystawy trwające" if is_long(e) else escape(long_date(s.date()))}</a></nav>
<article>
{f'<img class="hero" src="{escape(e.image)}" alt="{escape(e.title)}" referrerpolicy="no-referrer">' if e.image else ""}
<div class="cats">{"".join(f'<span class="cat">{escape(c)}</span>' for c in cats)}</div>
<h1>{'<span class="x">Odwołane: </span>' if e.cancelled else ""}{escape(e.title)}</h1>
<dl>{"".join(rows)}</dl>
{desc_html}
<div class="btns">{"".join(btns)}</div>
</article>
{more}"""
    return _page(base, path, title, desc, body, image=e.image, og_type="article", ld=[ld, crumbs])


def list_page(base: str, events: list[Event], paths: dict[str, str]) -> str:
    def items(evs: list[Event], when) -> str:
        return "".join(f'<li><span class="t">{when(e)}</span> <a href="/{paths[e.uid]}">{escape(e.title)}</a>'
                       f'<small>{escape(e.location_line() or "")}</small></li>' for e in evs)

    by_day: dict = {}
    for e in events:
        if not is_long(e):
            by_day.setdefault(e.day, []).append(e)
    sections = [f'<h2 id="{d.isoformat()}">{escape(long_date(d)).capitalize()}</h2><ul class="list">'
                f'{items(evs, lambda e: "cały dzień" if e.all_day else f"{e.start.astimezone(TZ):%H:%M}")}</ul>'
                for d, evs in by_day.items()]
    long = [e for e in events if is_long(e)]
    if long:  # exhibitions that run for weeks would sit under a long-past opening day
        sections.append('<h2 id="wystawy">Wystawy i wydarzenia trwające</h2><ul class="list">'
                        + items(long, lambda e: f"do {e.end:%d.%m}" if e.end else "") + "</ul>")
    body = (f'<h1>Wydarzenia w Tarnowie i okolicach</h1><p>{len(events)} nadchodzących wydarzeń w Tarnowie i do 50 km: '
            f'koncerty, teatr, wystawy, sport, imprezy dla dzieci. <a href="/">Otwórz kalendarz</a>.</p>'
            + "".join(sections))
    return _page(base, "wydarzenia/", f"Lista wydarzeń – Tarnów i okolice | {SITE}",
                 "Wszystkie nadchodzące wydarzenia w Tarnowie i do 50 km, dzień po dniu: koncerty, teatr, kino, "
                 "wystawy, sport, imprezy dla dzieci i festiwale.", body)


NOT_FOUND = """<h1>Nie znaleziono strony</h1>
<p>To wydarzenie mogło się już odbyć albo zmienić termin. Aktualne wydarzenia znajdziesz w kalendarzu.</p>
<div class="btns"><a class="btn f" href="/">Otwórz kalendarz</a><a class="btn" href="/wydarzenia/">Lista wydarzeń</a></div>"""


def write_seo(out: Path, base: str, events: list[Event], now: datetime) -> dict[str, str]:
    """Write event pages, the list page, sitemap.xml, robots.txt and 404.html. Returns uid -> path."""
    upcoming = sorted((e for e in events if e.in_main and (e.end or e.start) >= now),
                      key=lambda e: (e.start, e.title))
    paths = page_paths(upcoming)
    shutil.rmtree(out / "wydarzenie", ignore_errors=True)  # events gone from the window lose their pages
    by_day: dict = {}
    for e in upcoming:
        by_day.setdefault(e.day, []).append(e)
    for e in upcoming:
        siblings = [(o, paths[o.uid]) for o in by_day[e.day] if o.uid != e.uid]
        f = out / paths[e.uid] / "index.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(event_page(base, e, paths[e.uid], siblings), encoding="utf-8")
    (out / "wydarzenia").mkdir(parents=True, exist_ok=True)
    (out / "wydarzenia" / "index.html").write_text(list_page(base, upcoming, paths), encoding="utf-8")
    (out / "404.html").write_text(_page(base, "404.html", f"Nie znaleziono | {SITE}", "Nie znaleziono strony.",
                                        NOT_FOUND, noindex=True), encoding="utf-8")

    today = now.date().isoformat()
    urls = [(f"{base}/", today, "daily", "1.0"), (f"{base}/wydarzenia/", today, "daily", "0.8"),
            (f"{base}/zrodla.html", today, "weekly", "0.3")]
    urls += [(f"{base}/{paths[e.uid]}", None, None, "0.6") for e in upcoming]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod, freq, prio in urls:
        lines.append(f"<url><loc>{escape(loc)}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "")
                     + (f"<changefreq>{freq}</changefreq>" if freq else "") + f"<priority>{prio}</priority></url>")
    lines.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    return paths
