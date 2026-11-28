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
from datetime import datetime, timedelta, timezone
from functools import cache
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

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


WEB = Path(__file__).parent / "web"
# Category icons and short names, as on the calendar (kalendarz.html: ICONS, SHORT).
ICONS = {"koncerty": "music_note", "teatr": "theater_comedy", "kino": "movie", "sport": "sports_soccer", "wystawy": "palette",
         "dzieci": "child_care", "spotkania": "forum", "imprezy": "nightlife", "festiwale": "festival",
         "uroczystosci": "church", "inne": "event"}
SHORT = {"koncerty": "Koncerty", "teatr": "Teatr i stand-up", "kino": "Kino", "sport": "Sport", "wystawy": "Wystawy",
         "dzieci": "Dzieci", "spotkania": "Spotkania", "imprezy": "Imprezy", "festiwale": "Festiwale",
         "uroczystosci": "Uroczystości", "inne": "Inne"}
DAYS_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
# Google Fonts wants the icon names sorted.
_ICON_NAMES = ",".join(sorted({*ICONS.values(), "calendar_month", "confirmation_number", "chevron_left", "chevron_right", "close", "content_copy", "event_available", "event_busy",
                               "history", "local_activity", "location_on", "mail", "museum",
                               "zoom_in", "zoom_out", "open_in_new", "redeem", "schedule",
                               "share"}))
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto+Flex:opsz,wdth,wght@8..144,25..151,100..1000&amp;display=swap">'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..24,400,0..1,0'
         f'&amp;icon_names={_ICON_NAMES}&amp;display=block">')


# The share panel, for browsers without a system share sheet (navigator.share): Chrome on Linux,
# Firefox on desktop. Brand marks from Simple Icons (as the calendar's Apple icon, howto.py), drawn in white.
SIMPLE_ICONS = "https://cdn.jsdelivr.net/npm/simple-icons@13.21.0/icons/{}.svg"
SHARE_TARGETS = [  # name, Simple Icons slug, brand colour, link ({u} page address, {t} title, both URL-encoded)
    ("Facebook", "facebook", "#0866ff", "https://www.facebook.com/sharer/sharer.php?u={u}"),
    ("WhatsApp", "whatsapp", "#25d366", "https://wa.me/?text={t}%20{u}"),
    ("X", "x", "var(--on-surface)", "https://x.com/intent/post?text={t}&url={u}"),
    ("Telegram", "telegram", "#26a5e4", "https://t.me/share/url?url={u}&text={t}"),
]


def share_panel(url: str, title: str) -> str:
    u, t = quote(url, safe=""), quote(title, safe="")
    apps = "".join(
        f'<a href="{escape(href.format(u=u, t=t))}" target="_blank" rel="noopener"><i style="--b:{color}">'
        f'<span class="mk" style="--m:url({SIMPLE_ICONS.format(slug)})"></span></i>{name}</a>'
        for name, slug, color, href in SHARE_TARGETS)
    apps += (f'<a href="mailto:?subject={t}&amp;body={u}"><i style="--b:var(--sec-c);color:var(--on-sec-c)">'
             '<span class="ms">mail</span></i>E-mail</a>')
    return f"""<dialog class="sh" id="share" aria-labelledby="shareT"><div class="shs">
<div class="shh"><h2 id="shareT">Udostępnij</h2><button class="ib" type="button" data-close aria-label="Zamknij"><span class="ms">close</span></button></div>
<p class="shp">{escape(title)}</p>
<div class="apps">{apps}</div>
<div class="copy"><input type="text" readonly value="{escape(url)}" aria-label="Link do wydarzenia"><button class="btn tonal" type="button" data-copy><span class="ms">content_copy</span><span>Kopiuj</span></button></div>
</div></dialog>"""


def cat_vars(c: str) -> str:
    c = c if c in ICONS else "inne"
    return f"--k:var(--k-{c});--ko:var(--ko-{c});--ka:var(--ka-{c});--koa:var(--koa-{c})"


def _calendar_block(start: str, end: str) -> str:
    """A marked block of the calendar page (kalendarz.html), from the line after `start` to `end`."""
    page = (WEB / "kalendarz.html").read_text(encoding="utf-8")
    return page.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0].strip()


@cache
def _tokens() -> str:
    """The calendar's colour tokens (light, dark, category hues): one palette for both pages."""
    return _calendar_block("/* TOKENS-START", "/* TOKENS-END */")


@cache
def _lightbox() -> tuple[str, str]:
    """The calendar's picture viewer (CSS, JS): the same zoom on both pages."""
    css = _calendar_block("/* LIGHTBOX-CSS-START", "/* LIGHTBOX-CSS-END */")
    return css[css.index("*/") + 2:].strip(), _calendar_block("// LIGHTBOX-JS-START", "// LIGHTBOX-JS-END")


# Same building blocks as the calendar (kalendarz.html): app bar, pill buttons, category chips, poster
# cards, the event sheet's info rows / description / source list. Only the layout is this page's own.
STYLE = """*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--surface);color:var(--on-surface);font:400 15px/1.5 var(--font);-webkit-font-smoothing:antialiased;overflow-x:hidden}
a{color:inherit}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:8px}
.ms{font-family:"Material Symbols Rounded";font-weight:400;font-style:normal;font-size:20px;line-height:1;letter-spacing:normal;text-transform:none;
  display:inline-block;white-space:nowrap;direction:ltr;font-feature-settings:"liga";font-variation-settings:"FILL" 0,"opsz" 20;user-select:none;flex:none}
.ms.f{font-variation-settings:"FILL" 1,"opsz" 20}
html:not(.icons) .ms{color:transparent!important;width:1em;overflow:hidden}
.wrap{max-width:1120px;margin:0 auto;padding:0 var(--gutter)}
.bar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--surface) 88%,transparent);backdrop-filter:blur(14px) saturate(1.4);
  -webkit-backdrop-filter:blur(14px) saturate(1.4);box-shadow:0 1px 0 var(--outline-var)}
.bar .wrap{display:flex;align-items:center;gap:12px;min-height:var(--bar-h)}
.brand{display:flex;align-items:center;gap:12px;min-width:0;margin-right:auto;text-decoration:none}
.logo{width:40px;height:40px;border-radius:12px;background:var(--primary);color:var(--on-primary);display:grid;place-items:center;flex:none}
.logo .ms{font-size:24px}
.brand>span:last-child{min-width:0}
.brand b{display:block;overflow:hidden;text-overflow:ellipsis;font:800 22px/1.1 var(--font);font-stretch:85%;letter-spacing:-.2px;white-space:nowrap}
.brand small{display:block;color:var(--on-surface-var);font-size:12px;line-height:16px;white-space:nowrap}
.btn{height:44px;padding:0 18px;border-radius:22px;border:1px solid var(--outline);background:transparent;color:var(--on-surface);font:550 14px/1 var(--font);
  letter-spacing:.1px;display:inline-flex;align-items:center;justify-content:center;gap:8px;flex:none;text-decoration:none;white-space:nowrap;cursor:pointer;
  transition:background-color .2s,box-shadow .2s}
.btn:hover{background:color-mix(in srgb,var(--on-surface) 8%,transparent)}
.btn .ms{font-size:18px}
.btn.tonal{border:0;background:var(--sec-c);color:var(--on-sec-c)}
.btn.tonal:hover{background:color-mix(in srgb,var(--on-sec-c) 10%,var(--sec-c))}
.btn.filled{border:0;background:var(--primary);color:var(--on-primary)}
.btn.filled:hover{background:color-mix(in srgb,var(--on-primary) 14%,var(--primary));box-shadow:var(--shadow-1)}
.bar .btn{height:40px;padding:0 16px 0 12px}
.crumbs{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin:24px 0 16px;font:650 12.5px/1.2 var(--font);letter-spacing:1.2px;
  text-transform:uppercase;color:var(--on-surface-var)}
.crumbs a{text-decoration:none;padding:4px 0}.crumbs a:hover{color:var(--on-surface);text-decoration:underline;text-underline-offset:3px}
.crumbs .ms{font-size:16px}
.ev{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:40px;align-items:start}
.poster{position:sticky;top:calc(var(--bar-h) + 20px);aspect-ratio:1;border-radius:28px;overflow:hidden;background:var(--ka);color:var(--koa);
  box-shadow:var(--shadow-1);isolation:isolate}
.poster .bg{position:absolute;inset:-24px;width:calc(100% + 48px);height:calc(100% + 48px);object-fit:cover;filter:blur(28px) saturate(1.2);opacity:.75}
.poster .fg{position:relative;display:block;width:100%;height:100%;object-fit:contain}
.poster .glyph{position:absolute;right:-24px;bottom:-40px;font-size:260px;opacity:.16;font-variation-settings:"FILL" 1}
.poster .pdate{position:absolute;left:28px;bottom:24px;display:flex;flex-direction:column}
.poster .pdate span{font:700 13px/1.2 var(--font);letter-spacing:1.4px;text-transform:uppercase;opacity:.85}
.poster .pdate b{font:850 112px/.9 var(--font);font-stretch:72%;letter-spacing:-2px;font-variant-numeric:tabular-nums;margin:6px 0 4px}
.poster:not(.noimg) .glyph,.poster:not(.noimg) .pdate{display:none}
.catrow{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.cat{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;font-weight:700;padding:5px 11px 5px 8px;border-radius:10px;background:var(--k);color:var(--ko)}
.cat .ms{font-size:17px}
.cat.xb{background:var(--live);color:var(--on-live);padding-left:11px}
h1{margin:0;font:800 clamp(30px,4.2vw,46px)/1.05 var(--font);font-stretch:82%;letter-spacing:-.4px;overflow-wrap:anywhere;text-wrap:balance}
.x h1{text-decoration:line-through;text-decoration-thickness:3px}
.info{display:grid;gap:4px;margin:24px 0}
.row{display:grid;grid-template-columns:40px 1fr;gap:14px;align-items:center;padding:8px 0}
.row>.ms{width:40px;height:40px;border-radius:12px;background:var(--surface-c-high);color:var(--on-surface-var);display:grid;place-items:center;font-size:21px}
.row>div{min-width:0;overflow-wrap:anywhere;font-size:15.5px;font-weight:550}
.row .sub{display:block;color:var(--on-surface-var);font-size:13px;font-weight:400;margin-top:2px}
.row a{text-decoration-color:var(--outline);text-underline-offset:3px;font-weight:600}
.free{display:inline-flex;align-items:center;gap:3px;margin-left:6px;font-size:12px;font-weight:700;padding:2px 8px;border-radius:8px;
  background:var(--k-sport);color:var(--ko-sport);vertical-align:2px}
.free .ms{font-size:15px}
.actions{display:grid;gap:8px;margin:0 0 28px}
.cta{display:flex;align-items:center;gap:12px;height:56px;padding:0 18px 0 20px;border-radius:28px;background:var(--primary);color:var(--on-primary);
  text-decoration:none;font:650 16px/1.25 var(--font);transition:background-color .2s,box-shadow .2s}
.cta:hover{background:color-mix(in srgb,var(--on-primary) 12%,var(--primary));box-shadow:var(--shadow-1)}
.cta .ms{font-size:22px}
.cta .ext{margin-left:auto;font-size:20px;opacity:.7}
.acts{display:grid;grid-template-columns:repeat(var(--n),minmax(0,1fr));gap:8px}
.act{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;min-height:80px;padding:12px 8px;border:0;border-radius:20px;
  background:var(--surface-c-high);color:var(--on-surface);font:600 13.5px/1.25 var(--font);text-align:center;text-decoration:none;cursor:pointer;
  transition:background-color .2s}
.act:hover{background:var(--surface-c-highest)}
.act .ms{font-size:24px;color:var(--on-surface-var)}
.acts.one .act{flex-direction:row;min-height:52px;gap:10px}
.note{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:16px;border:1px dashed var(--outline);color:var(--on-surface);margin:0 0 14px}
.note .ms{color:var(--on-surface-var);margin-top:1px}
.desc{white-space:pre-line;line-height:1.6;margin:0 0 28px;padding:18px 20px;border-radius:20px;background:var(--surface-c-low);overflow-wrap:anywhere}
.desc a{text-decoration-color:var(--outline);text-underline-offset:3px;font-weight:600}
.desc .more{display:flex;align-items:center;gap:4px;width:max-content;margin-top:12px;text-decoration:none;font-weight:700}
.desc .more .ms{font-size:16px}
.sec{display:flex;align-items:center;gap:10px;margin:0 0 12px;font:750 12.5px/1.2 var(--font);letter-spacing:1.2px;text-transform:uppercase;color:var(--on-surface-var)}
.sec .cnt{font-weight:600;letter-spacing:0;text-transform:none;background:var(--surface-c-high);border-radius:10px;padding:2px 8px;font-size:12px}
.sec .line{flex:1;height:1px;background:var(--outline-var)}
.srcs{display:flex;flex-direction:column;gap:6px}
.srcs a{display:flex;align-items:center;gap:12px;padding:10px 14px 10px 10px;border-radius:16px;background:var(--surface-c-low);text-decoration:none;transition:background-color .2s}
.srcs a:hover{background:var(--surface-c-high)}
.srcs .fi{width:36px;height:36px;border-radius:10px;background:var(--surface-c-lowest);box-shadow:inset 0 0 0 1px var(--outline-var);display:grid;place-items:center;flex:none}
.fav{width:22px;height:22px;border-radius:5px;object-fit:contain}
.fav.av{display:inline-grid;place-items:center;background:var(--inverse);color:var(--on-inverse);font-size:12px;font-weight:800;text-transform:uppercase}
.srcs div{min-width:0;flex:1}
.srcs b{display:block;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srcs small{display:block;color:var(--on-surface-var);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srcs .pri{font-size:10.5px;font-weight:750;padding:2px 7px;border-radius:8px;background:var(--sec-c);color:var(--on-sec-c);flex:none}
.srcs a>.ms{color:var(--on-surface-var);font-size:18px}
.more-sec{margin:48px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
.card{display:flex;flex-direction:column;min-width:0;border-radius:20px;background:var(--surface-c-lowest);color:var(--on-surface);text-decoration:none;
  box-shadow:var(--shadow-1);transition:transform .3s var(--ease),box-shadow .3s var(--ease)}
.card:hover{transform:translateY(-3px);box-shadow:var(--shadow-2)}
.card .media{position:relative;aspect-ratio:4/3;background:var(--ka);color:var(--koa);overflow:hidden;border-radius:20px 20px 0 0}
.card .media img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:transform .6s var(--ease)}
.card:hover .media img{transform:scale(1.04)}
.card .media.img::before{content:"";position:absolute;inset:0;background:var(--img-scrim);z-index:1}
.card .glyph{position:absolute;right:-10px;bottom:-22px;font-size:120px;opacity:.16;font-variation-settings:"FILL" 1}
.card .media.img .glyph{display:none}
.card .lbl{position:absolute;top:10px;left:10px;z-index:2;display:inline-flex;align-items:center;gap:4px;padding:4px 9px 4px 7px;border-radius:9px;
  background:var(--k);color:var(--ko);font-size:11.5px;font-weight:700;letter-spacing:.2px}
.card .lbl .ms{font-size:15px}
.card .media:not(.img) .lbl{background:color-mix(in srgb,var(--koa) 18%,transparent);color:var(--koa)}
.card .tbig{position:absolute;left:12px;bottom:8px;z-index:2;color:#fff;font:850 28px/1 var(--font);font-stretch:75%;font-variant-numeric:tabular-nums;
  text-shadow:0 1px 8px rgb(0 0 0 / .4)}
.card .tbig small{display:block;font-size:13px;font-weight:700;font-stretch:100%;letter-spacing:.6px;text-transform:uppercase;margin-bottom:3px;opacity:.9}
.card .media:not(.img) .tbig{color:var(--koa);text-shadow:none}
.card .body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:5px;min-width:0}
.card h3{margin:0;font:700 16px/1.22 var(--font);font-stretch:92%;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.card .place{display:flex;align-items:center;gap:4px;color:var(--on-surface-var);font-size:12.5px;min-width:0}
.card .place span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .place .ms{font-size:15px}
dialog.sh{border:0;padding:0;background:transparent;color:var(--on-surface);max-width:none;max-height:none;overflow:visible}
dialog.sh::backdrop{background:var(--scrim)}
dialog.sh[open]{position:fixed;inset:0;margin:auto}
.shs{width:min(460px,calc(100vw - 32px));background:var(--surface-c-low);border-radius:28px;padding:16px 20px 20px 24px;
  box-shadow:0 24px 64px rgb(0 0 0 / .3);animation:dlgIn .35s var(--ease-dec)}
@keyframes dlgIn{from{transform:translateY(16px) scale(.98);opacity:0}}
.shh{display:flex;align-items:center;gap:8px}
.shh h2{flex:1;margin:0;font:800 24px/1.1 var(--font);font-stretch:85%}
.ib{width:40px;height:40px;border-radius:20px;border:0;background:transparent;color:var(--on-surface-var);display:grid;place-items:center;cursor:pointer}
.ib:hover{background:var(--surface-c-high)}
.shp{margin:2px 0 0;color:var(--on-surface-var);font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.apps{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px;margin:18px -8px 18px -12px}
.apps a{display:flex;flex-direction:column;align-items:center;gap:7px;padding:10px 2px;border-radius:16px;text-decoration:none;font-size:12.5px;font-weight:600}
.apps a:hover{background:var(--surface-c-high)}
.apps i{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;background:var(--b);color:#fff}
.apps i .ms{font-size:24px}
.apps .mk{width:24px;height:24px;background:currentColor;-webkit-mask:var(--m) center/contain no-repeat;mask:var(--m) center/contain no-repeat}
.apps a:nth-child(3) i{color:var(--surface)}
.copy{display:flex;gap:8px;align-items:center;background:var(--surface-c-lowest);border-radius:24px;padding:4px 4px 4px 16px;
  box-shadow:inset 0 0 0 1px var(--outline-var)}
.copy input{flex:1;min-width:0;border:0;background:transparent;font:inherit;font-size:13.5px;color:var(--on-surface-var);outline:0}
.copy .btn{height:40px}
.nf{padding:48px 0 8px;max-width:640px}
.nf p{color:var(--on-surface-var);font-size:17px;margin:12px 0 24px}
footer{margin-top:56px;border-top:1px solid var(--outline-var)}
footer .wrap{display:flex;gap:16px;flex-wrap:wrap;padding:24px var(--gutter) 40px;color:var(--on-surface-var);font-size:13px}
footer a{color:var(--on-surface-var)}
@media (max-width:860px){
  :root{--gutter:16px;--bar-h:56px}
  .brand b{font-size:20px}.brand small{display:none}.logo{width:36px;height:36px;border-radius:10px}
  .crumbs{margin:14px 0 12px}
  .ev{grid-template-columns:minmax(0,1fr);gap:20px}
  .poster{position:relative;top:0;aspect-ratio:4/3;border-radius:24px}
  .poster.noimg{aspect-ratio:auto;height:148px}
  .poster .glyph{font-size:180px;bottom:-34px}
  .poster .pdate{left:20px;bottom:16px;flex-direction:row;align-items:baseline;gap:10px}
  .poster .pdate b{font-size:72px;margin:0}
  .info{margin:18px 0}
  .act{min-height:76px;padding:10px 6px;font-size:12.5px}
  .desc{padding:16px}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .card,.card .media{border-radius:16px}.card .media{border-bottom-left-radius:0;border-bottom-right-radius:0}
  .card .body{padding:10px}.card h3{font-size:14.5px}.card .tbig{font-size:23px}
  dialog.sh[open]{margin:0;inset:auto 0 0 0;width:100%}
  .shs{width:100%;border-radius:28px 28px 0 0;padding-bottom:max(20px,env(safe-area-inset-bottom,0px))}
}
@media (max-width:420px){.bar .btn{width:40px;padding:0}.bar .bt{display:none}}
@media (max-width:380px){.grid{grid-template-columns:minmax(0,1fr)}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition-duration:1ms!important}}"""

# The visitor's theme choice on the calendar (twc:theme), before anything is drawn.
HEAD_JS = ('<script>try{const t=JSON.parse(localStorage.getItem("twc:theme"));'
           'if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch{}</script>')


def _tail_js() -> str:
    """Icon font ready → show icons; source favicons with the calendar's fallbacks; the share button."""
    from .site import _favicon_js  # site.py already extracts it from the calendar page
    return f"""<script>
try {{
  const ok = () => document.documentElement.classList.add("icons");
  document.fonts.load('20px "Material Symbols Rounded"', "event").then(f => {{ if (f.length) ok(); }});
  document.fonts.ready.then(() => {{ if (document.fonts.check('20px "Material Symbols Rounded"', "event")) ok(); }});
}} catch {{ document.documentElement.classList.add("icons"); }}
{_favicon_js()}
{_lightbox()[1]}
document.querySelectorAll(".fi[data-site]").forEach(t => t.insertAdjacentHTML("afterbegin", favHTML(t.dataset.site, t.dataset.name)));
// Udostępnij: the system share sheet (phones, Windows, macOS, ChromeOS); where the browser has none, our panel.
const share = document.getElementById("share");
document.querySelectorAll("[data-share]").forEach(b => b.addEventListener("click", async () => {{
  const data = {{title: b.dataset.title, url: b.dataset.url}};
  if (navigator.share && (!navigator.canShare || navigator.canShare(data))) {{
    try {{ await navigator.share(data); return; }} catch (e) {{ if (e.name === "AbortError") return; }}
  }}
  share?.showModal();
}}));
share?.addEventListener("click", ev => {{
  if (ev.target === share || ev.target.closest("[data-close]")) return share.close();
  const c = ev.target.closest("[data-copy]"); if (!c) return;
  // Copy from the selected field right away (works in every browser during the click); the clipboard API only
  // when that is refused. Its promise can hang waiting for a permission, so the label does not wait for it.
  const input = share.querySelector("input");
  input.select();
  let done = false;
  try {{ done = document.execCommand("copy"); }} catch {{}}
  if (!done) navigator.clipboard?.writeText(input.value).catch(() => {{}});
  c.lastChild.textContent = "Skopiowano"; setTimeout(() => c.lastChild.textContent = "Kopiuj", 2000);
}});
</script>"""


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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{chr(10).join(meta)}
<meta name="theme-color" content="#faf8f5">
<link rel="icon" href="{base}/favicon.ico" sizes="48x48">
<link rel="icon" href="{base}/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{base}/apple-touch-icon.png">
{FONTS}
{HEAD_JS}
<style>{_tokens()}
{STYLE}
{_lightbox()[0]}</style>
</head>
<body>
<header class="bar"><div class="wrap">
<a class="brand" href="/"><span class="logo" aria-hidden="true"><span class="ms f">local_activity</span></span><span><b>Tarnów – wydarzenia</b><small>Tarnów i 50 km dookoła</small></span></a>
<a class="btn tonal" href="/" aria-label="Kalendarz"><span class="ms">calendar_month</span><span class="bt">Kalendarz</span></a>
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap"><a href="/">Kalendarz</a><a href="/zrodla.html">Źródła wydarzeń</a></div></footer>
{_tail_js()}
</body>
</html>
"""


PAST, GONE = "past", "gone"  # archived pages (archive.py): the event took place / vanished from its sources

# Same rule as the calendar's priceHTML(): these prices mean free entry.
_FREE = re.compile(r"^(0([.,]0+)?\s*(zł|pln)?$|free|bezp[łl]at|wst[eę]p (wolny|bezp|darm)|wej[sś]cie (wolne|bezp|darm)|wolny"
                   r"|udzia[łl] bezp|darmow)", re.I)
_URL = re.compile(r'https?://[^\s<>"]+')


def _linkify(text: str) -> str:
    """Escaped text with its web addresses as links, shortened like on the calendar (linkify())."""
    out, pos = [], 0
    for m in _URL.finditer(text):
        raw = m.group(0)
        url = raw.rstrip(".,;:!?)»”'")
        label = re.sub(r"^https?://(www\.)?", "", url)
        if len(label) > 42:
            label = label[:40] + "…"
        out += [escape(text[pos:m.start()]), f'<a href="{escape(url)}" rel="noopener nofollow">{escape(label)}</a>',
                escape(raw[len(url):])]
        pos = m.end()
    return "".join(out) + escape(text[pos:])


def _ics_href(e: Event, url: str) -> str:
    """A one-event .ics as a data: link, so "Dodaj do kalendarza" needs no script."""
    def z(d: datetime) -> str:
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def esc_ics(s: str) -> str:
        return s.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

    if e.all_day:
        last = (e.end or e.start).astimezone(TZ).date()
        when = [f"DTSTART;VALUE=DATE:{e.start.astimezone(TZ):%Y%m%d}", f"DTEND;VALUE=DATE:{last + timedelta(days=1):%Y%m%d}"]
    else:
        when = [f"DTSTART:{z(e.start)}"] + ([f"DTEND:{z(e.end)}"] if e.end else [])
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//tarnow-wydarzenia//PL", "BEGIN:VEVENT", f"UID:{e.uid}",
             f"DTSTAMP:{z(datetime.now(timezone.utc))}", *when, f"SUMMARY:{esc_ics(e.title)}",
             *([f"LOCATION:{esc_ics(e.location_line())}"] if e.location_line() else []),
             f"DESCRIPTION:{esc_ics(url)}", f"URL:{url}", "END:VEVENT", "END:VCALENDAR"]
    return "data:text/calendar;charset=utf-8," + quote("\r\n".join(lines))


def _card(o: Event, p: str, dated: bool) -> str:
    """A poster card like the calendar's grid (cardHTML()): picture or colour block, time, title, place."""
    t = o.start.astimezone(TZ)
    c = o.categories[0] if o.categories else "inne"
    time = "Cały dzień" if o.all_day else f"{t:%H:%M}"
    day = f"<small>{DAYS_SHORT[t.weekday()]} {t.day}.{t.month:02d}</small>" if dated else ""
    img = (f'<img src="{escape(o.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" '
           f'onerror="this.parentNode.classList.remove(\'img\');this.remove()">') if o.image else ""
    place = o.location_line()
    return (f'<a class="card" href="/{p}" style="{cat_vars(c)}"><div class="media{" img" if o.image else ""}">{img}'
            f'<span class="ms glyph" aria-hidden="true">{ICONS.get(c, "event")}</span>'
            f'<span class="lbl"><span class="ms f">{ICONS.get(c, "event")}</span>{escape(SHORT.get(c, "Inne"))}</span>'
            f'<span class="tbig">{day}{time}</span></div>'
            f'<div class="body"><h3>{escape(o.title)}</h3>'
            + (f'<div class="place"><span class="ms">location_on</span><span>{escape(place)}</span></div>' if place else "")
            + '</div></a>')


def event_page(base: str, e: Event, path: str, siblings: list[tuple[Event, str]], *, screening: bool = False,
               status: str | None = None) -> str:
    """status PAST or GONE: an archived page; siblings are then upcoming events to go to instead."""
    from .pipeline import read_more, short_desc  # one cut-off rule for the calendar and these pages

    s = e.start.astimezone(TZ)
    place = e.location_line()
    cats = [CATEGORIES.get(c, c) for c in e.categories]
    title = f"{'[ODWOŁANE] ' if e.cancelled else ''}{e.title} – {s.day} {MONTHS[s.month - 1]} {s.year}" \
            f"{', ' + e.town if e.town else ''} | {SITE}"
    desc = _desc(f"{when_text(e)}{'. ' + place if place else ''}. {e.description or ', '.join(cats)}", 160)
    src = e.all_sources()
    url = f"{base}/{path}"

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
        "location": loc, "url": url, "inLanguage": "pl",
    }
    if e.end:
        ld["endDate"] = e.end.astimezone(TZ).date().isoformat() if e.all_day else e.end.astimezone(TZ).isoformat()
    if e.description:
        ld["description"] = _desc(e.description, 500)
    if e.image:
        ld["image"] = [e.image, *e.images]
    if e.tickets and not status:
        ld["offers"] = {"@type": "Offer", "url": e.tickets, "availability": "https://schema.org/InStock"}
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Kalendarz", "item": f"{base}/"},
        {"@type": "ListItem", "position": 2, "name": e.title},
    ]}

    main_cat = e.categories[0] if e.categories else "inne"
    chips = "".join(f'<span class="cat" style="{cat_vars(c)}"><span class="ms f">{ICONS.get(c, "event")}</span>'
                    f'{escape(CATEGORIES.get(c, c))}</span>' for c in e.categories)
    if is_long(e):
        chips += f'<span class="cat" style="{cat_vars("wystawy")}"><span class="ms">museum</span>trwa długo</span>'
    if e.cancelled:
        chips += '<span class="cat xb">Odwołane</span>'

    rows = [f'<div class="row"><span class="ms">schedule</span><div>{escape(when_text(e))}</div></div>']
    if place:
        # As on the calendar: a venue name finds the place card on Google Maps, a bare town our geocoded pin.
        venue = e.location_name and e.location_name.strip() != (e.town or "").strip()
        q = place if venue else f"{e.lat},{e.lon}" if e.lat is not None else place
        links = [f'<a href="https://www.google.com/maps/search/?api=1&amp;query={quote(q)}" rel="noopener">Mapy Google</a>']
        if e.lat is not None:
            links.append(f'<a href="https://www.openstreetmap.org/?mlat={e.lat:.5f}&amp;mlon={e.lon:.5f}#map=17/{e.lat:.5f}/{e.lon:.5f}" '
                         'rel="noopener">OpenStreetMap</a>')
        rows.append(f'<div class="row"><span class="ms">location_on</span><div>{escape(place)}'
                    f'<span class="sub">{" · ".join(links)}</span></div></div>')
    if e.price:
        free = ('<span class="free"><span class="ms">redeem</span>Bezpłatne</span>'
                if _FREE.match(e.price.strip()) else "")
        rows.append(f'<div class="row"><span class="ms">confirmation_number</span><div>{escape(e.price)}{free}</div></div>')

    desc_html = ""
    if e.description:
        more = (f'<a class="more" href="{escape(src[0].url)}" target="_blank" rel="noopener nofollow">Czytaj dalej'
                f'<span class="ms">open_in_new</span></a>') if read_more(e) else ""
        desc_html = f'<p class="desc">{_linkify(short_desc(e.description))}{more}</p>'

    # One main action, full width: buying a ticket (an archived page: the calendar's upcoming events).
    # Below it the secondary actions as equal tiles in one row. An archived event is no longer in the
    # calendar, and its tickets are not for sale.
    cta = ""
    if status:
        cta = ('<a class="cta" href="/"><span class="ms">calendar_month</span>Zobacz nadchodzące wydarzenia'
               '<span class="ms ext">chevron_right</span></a>')
    elif e.tickets and not e.cancelled:
        cta = (f'<a class="cta" href="{escape(e.tickets)}" rel="noopener nofollow"><span class="ms">confirmation_number</span>'
               'Kup bilet<span class="ms ext">open_in_new</span></a>')
    acts = []
    if not status:
        acts.append(f'<a class="act" href="/#e={escape(e.uid)}"><span class="ms">calendar_month</span>'
                    '<span>Pokaż w kalendarzu</span></a>')
        if not e.cancelled:
            fname = slugify(e.title, 40) + ".ics"
            acts.append(f'<a class="act" href="{escape(_ics_href(e, url))}" download="{fname}">'
                        '<span class="ms">event_available</span><span>Dodaj do kalendarza</span></a>')
    acts.append(f'<button class="act" type="button" data-share data-url="{escape(url)}" data-title="{escape(e.title)}">'
                '<span class="ms">share</span><span>Udostępnij</span></button>')
    actions = (f'<div class="actions">{cta}<div class="acts{" one" if len(acts) == 1 else ""}" '
               f'style="--n:{len(acts)}">{"".join(acts)}</div></div>')

    note = ""
    if status == GONE:
        note = ('<p class="note"><span class="ms">event_busy</span><span>Tego wydarzenia nie ma już w kalendarzu organizatora – '
                'mogło zostać odwołane albo przeniesione na inny termin. Sprawdź na stronie organizatora.</span></p>')
    elif status == PAST and not e.cancelled:
        note = '<p class="note"><span class="ms">history</span><span>To wydarzenie już się odbyło.</span></p>'

    def host(u: str) -> str:
        return (urlsplit(u).hostname or "").removeprefix("www.")

    sources = "".join(
        f'<a href="{escape(r.url)}" rel="noopener nofollow"><span class="fi" data-site="{escape(r.url)}" data-name="{escape(r.source_name)}"></span>'
        f'<div><b>{escape(r.source_name)}</b><small>{escape(host(r.url))}</small></div>'
        + ('<span class="pri">główne</span>' if i == 0 else "") + '<span class="ms">open_in_new</span></a>'
        for i, r in enumerate(src))

    more = ""
    if siblings:
        heading = "Inne seanse" if screening else "Nadchodzące wydarzenia" if status else "Tego samego dnia"
        cards = "".join(_card(o, p, bool(screening or status)) for o, p in siblings[:12])
        more = (f'<section class="more-sec"><h2 class="sec">{heading}<span class="cnt">{min(len(siblings), 12)}</span>'
                f'<span class="line"></span></h2><div class="grid">{cards}</div></section>')

    img = ""
    if e.image:
        fail = "onerror=\"this.closest('.poster').classList.add('noimg');this.remove()\""
        img = (f'<img class="bg" src="{escape(e.image)}" alt="" referrerpolicy="no-referrer" {fail}>'
               f'<img class="fg" src="{escape(e.image)}" alt="{escape(e.title)}" referrerpolicy="no-referrer" {fail}>')
    zoom = (f' data-zoom="{escape(e.image)}" data-alt="{escape(e.title)}" role="button" tabindex="0" aria-label="Powiększ zdjęcie"'
            if e.image else "")
    # The description's pictures (a programme, a price list) after the poster: arrows switch them (PICS in the calendar's script).
    pics = [e.image, *e.images] if e.image else []
    if len(pics) > 1:
        zoom += f' data-pics="{escape(json.dumps(pics))}"'
        img += pics_controls(len(pics))
    poster = (f'<div class="poster{"" if e.image else " noimg"}" style="{cat_vars(main_cat)}"{zoom}>{img}'
              f'<span class="ms glyph" aria-hidden="true">{ICONS.get(main_cat, "event")}</span>'
              f'<div class="pdate"><span>{DAYS[s.weekday()]}</span><b>{s.day}</b><span>{MONTHS[s.month - 1]} {s.year}</span></div></div>')

    # The date opens that day in the calendar; a long exhibition is on today's page anyway, and an
    # archived event's day has left the calendar.
    day = "/" if is_long(e) or status else f"/#dzien={s.date().isoformat()}"
    body = f"""<nav class="crumbs" aria-label="Okruszki"><a href="/">Kalendarz</a><span class="ms" aria-hidden="true">chevron_right</span><a href="{day}">{"Wystawy wielodniowe" if is_long(e) else escape(long_date(s.date()))}</a></nav>
<article class="ev{" x" if e.cancelled else ""}">
{poster}
<div class="main">
<div class="catrow">{chips}</div>
<h1>{escape(e.title)}</h1>
<div class="info">{"".join(rows)}</div>
{note}{actions}
{desc_html}
<h2 class="sec">Źródła<span class="cnt">{len(src)}</span><span class="line"></span></h2>
<div class="srcs">{sources}</div>
</div>
</article>
{more}
{share_panel(url, e.title)}"""
    # A removed event, or a cancelled one now past, is of no use to anyone searching: drop it from the index.
    hide = screening or status == GONE or (status == PAST and e.cancelled)
    return _page(base, path, title, desc, body, image=e.image, og_type="article",
                 ld=[crumbs] if status == GONE else [ld, crumbs], noindex=hide)


def pics_controls(n: int) -> str:
    """The arrows and dots of a poster with n pictures: the same markup as PICS.controls() in kalendarz.html."""
    dots = '<i class="on"></i>' + "<i></i>" * (n - 1)
    return ('<button class="pv-btn l" type="button" data-step="-1" aria-label="Poprzednie zdjęcie"><span class="ms">chevron_left</span></button>'
            '<button class="pv-btn r" type="button" data-step="1" aria-label="Następne zdjęcie"><span class="ms">chevron_right</span></button>'
            f'<span class="pv-dots" aria-hidden="true">{dots}</span>')


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


NOT_FOUND = """<section class="nf"><h1>Nie znaleziono strony</h1>
<p>To wydarzenie mogło się już odbyć albo zmienić termin. Aktualne wydarzenia znajdziesz w kalendarzu.</p>
<a class="btn filled" href="/"><span class="ms">calendar_month</span>Otwórz kalendarz</a></section>"""


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
