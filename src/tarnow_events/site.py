"""zrodla.html: where the events come from and whether each source works, the sources considered and not
used (planned / rejected, with the reason, from docs/sources-catalogue.json), then the numbers.
Adding calendars lives in the calendar page's "Dodaj do swojego kalendarza" wizard."""

from __future__ import annotations

import re
from datetime import datetime
from functools import cache
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from .models import TZ
from .submissions import page_keys

WEB = Path(__file__).parent / "web"


@cache
def _favicon_js() -> str:
    """Source icons with the same fallbacks as the calendar (parent domain, hand-picked icons, letter)."""
    page = (WEB / "kalendarz.html").read_text(encoding="utf-8")
    esc = re.search(r"^const esc = .*$", page, re.M).group(0)
    block = page.split("// FAV-JS-START", 1)[1].split("\n", 1)[1]  # from the line after the marker
    return esc + "\n" + block.split("// FAV-JS-END", 1)[0]


def _plural(n: int) -> str:
    if n == 1:
        return "wydarzenie"
    return "wydarzenia" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "wydarzeń"


FB_ONLY = "Wydarzenia ogłasza tylko na Facebooku lub Instagramie, bez strony z kalendarzem."


def _unused(recs: list[dict], groups: dict[str, str]) -> str:
    """Rows for sources that are not fetched: name (linked), town, kind, and why."""
    items = ""
    for r in sorted(recs, key=lambda r: (list(groups).index(r["g"]) if r["g"] in groups else 99, (r.get("pn") or r["n"]).lower())):
        name = escape(r.get("pn") or r["n"])
        url = r.get("u") or r.get("fb")
        link = f'<a href="{escape(url)}" rel="noopener nofollow">{name}</a>' if url else f"<b>{name}</b>"
        where = " · ".join(x for x in (r.get("t", "") + (f" ({r['d']:.0f} km)" if r.get("d") else ""), groups.get(r["g"], "")) if x)
        why = r.get("r") or (FB_ONLY if r["s"] == "tylko FB/IG" else r.get("o", ""))
        tile = (f'<span class="fi" data-site="{escape(url)}" data-name="{name}"></span>' if url
                else f'<span class="fi"><span class="fav av">{name[:1]}</span></span>')
        items += f'<li>{tile}<div>{link}<small>{escape(where)}</small><p class="why">{escape(why)}</p></div></li>'
    return f'<ul class="srcs">{items}</ul>'


def render_index(settings, feeds, events, configs, stats, groups: dict[str, str] | None = None,
                 totals: dict[str, int] | None = None, catalogue: list[dict] | None = None,
                 fb_form: str | None = None) -> str:
    """totals: event counts for the numbers section (all, tarnow, okolice, kino).
    catalogue: research records; "planowane", "odrzucone" and "tylko FB/IG" ones are listed as not used.
    fb_form: the Google Form for Facebook events (sources.yaml, facebook → form_url); with it, the
    "Dodaj wydarzenie" section and the pages whose events it accepts (submissions.py)."""
    base = settings.public_base_url
    now = datetime.now(TZ)
    groups = groups or {}
    totals = totals or {"all": len(events)}
    per_source = {f["src"]: f["count"] for f in feeds if f["group"] == "source"}

    def host(u: str) -> str:
        return (urlsplit(u).hostname or "").removeprefix("www.")

    order = list(groups) + sorted({c.group for c in configs} - set(groups))
    src_html = ""
    for g in order:
        members = sorted((c for c in configs if c.group == g), key=lambda c: c.name.lower())
        if not members:
            continue
        bad = sum(1 for c in members if c.id in stats and not stats[c.id].get("ok"))
        items = ""
        for c in members:
            s = stats.get(c.id)
            state = ('<span class="st ok" title="Działa"></span>' if s and s.get("ok") else
                     f'<span class="st err" title="{escape((s or {}).get("error", ""))}"></span>' if s else
                     '<span class="st off" title="Wyłączone"></span>')
            n = per_source.get(c.id, 0)
            err = f' · <span class="errt">{escape(s.get("error", "")[:120])}</span>' if s and not s.get("ok") else ""
            items += (f'<li><span class="fi" data-site="{escape(c.url)}" data-name="{escape(c.name)}">{state}</span>'
                      f'<div><a href="{escape(c.url)}" rel="noopener">{escape(c.name)}</a>'
                      f'<small>{escape(host(c.url))}{err}</small></div><span class="n">{n}<small>{_plural(n)}</small></span></li>')
        badge = f'<span class="bad">{bad} z błędem</span>' if bad else ""
        src_html += (f'<details{" open" if g in ("miasto", "kultura") else ""}><summary><b>{escape(groups.get(g, g))}</b>'
                     f'<span class="cnt">{len(members)}</span>{badge}<span class="chev">⌄</span></summary>'
                     f'<ul class="srcs">{items}</ul></details>')

    catalogue = catalogue or []
    planned = [r for r in catalogue if r["s"] == "planowane"]
    rejected = [r for r in catalogue if r["s"] == "odrzucone"]
    # The catalogue's "Facebook Events" record is the city-wide search; the Facebook source above covers it.
    fb_only = [r for r in catalogue if r["s"] == "tylko FB/IG" and not r["n"].startswith("Facebook Events")]
    unused_html = ""
    if planned:
        unused_html += (f'<h2>Planowane <span class="h2n">{len(planned)}</span></h2><p class="sub">Źródła, które chcemy dodać.</p>'
                        f'<div class="box">{_unused(planned, groups)}</div>')
    if rejected or fb_only:
        unused_html += (f'<h2>Odrzucone <span class="h2n">{len(rejected) + len(fb_only)}</span></h2><p class="sub">Sprawdzone, '
                        'ale nieużywane. Ich wydarzenia często i tak trafiają do kalendarza przez inne źródła.</p>')
        for label, recs in (("Nie działają lub nie mają wydarzeń z okolicy", rejected), ("Tylko Facebook lub Instagram", fb_only)):
            if recs:
                unused_html += (f'<details><summary><b>{label}</b><span class="cnt">{len(recs)}</span><span class="chev">⌄</span>'
                                f'</summary>{_unused(recs, groups)}</details>')

    add_html = ""
    if fb_form:
        pages = sorted({(r.get("pn") or r["n"], fb) for r in catalogue
                        for fb in [r.get("fb") or (r.get("u") if page_keys(r.get("u")) else None)] if fb},
                       key=lambda x: x[0].lower())
        page_items = "".join(f'<li><div><a href="{escape(u)}" rel="noopener">{escape(n)}</a></div></li>' for n, u in pages)
        # Only the list: the form itself is linked from the calendar's "Wydarzenie z FB" button.
        add_html = (f'<details id="dodaj"><summary><b>Strony, których wydarzenia przyjmujemy</b><span class="cnt">{len(pages)}</span>'
                    f'<span class="chev">⌄</span></summary><ul class="srcs">{page_items}</ul></details>')

    ok = sum(1 for s in stats.values() if s.get("ok"))
    tiles = [(totals.get("all", len(events)), "wszystkich wydarzeń"), (totals.get("tarnow"), "w Tarnowie"),
             (totals.get("okolice"), f"w okolicach (do {int(settings.radius_km)} km)"), (totals.get("kino"), "seansów kinowych"),
             (f"{ok}/{len(stats)}", "źródeł działa")]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{escape(label)}</span></div>' for v, label in tiles if v is not None)

    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Źródła wydarzeń – Wydarzenia Tarnów</title>
<meta name="description" content="Skąd pochodzą wydarzenia z Tarnowa i okolic: {len(configs)} źródeł (instytucje kultury, gminy, kina, serwisy biletowe, media), stan każdego z nich i źródła sprawdzone, ale nieużywane.">
<link rel="canonical" href="{escape(base)}/zrodla.html">
<meta property="og:site_name" content="Wydarzenia Tarnów">
<meta property="og:locale" content="pl_PL">
<meta property="og:type" content="website">
<meta property="og:title" content="Źródła wydarzeń – Wydarzenia Tarnów">
<meta property="og:description" content="Skąd pochodzą wydarzenia z Tarnowa i okolic i czy każde źródło działa.">
<meta property="og:url" content="{escape(base)}/zrodla.html">
<meta property="og:image" content="{escape(base)}/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{escape(base)}/og-image.png">
<meta name="theme-color" content="#faf8f5">
<link rel="icon" href="{escape(base)}/favicon.ico" sizes="48x48">
<link rel="icon" href="{escape(base)}/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{escape(base)}/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto+Flex:opsz,wdth,wght@8..144,25..151,100..1000&display=swap">
<script>try{{const t=JSON.parse(localStorage.getItem("twc:theme"));if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}}catch{{}}</script>
<style>
:root{{--bg:#faf8f5;--card:#fff;--low:#f4f1ed;--fg:#1c1b1a;--mut:#4b4844;--line:#cdc8c2;--ink:#1c1b1a;--on-ink:#faf8f5;
  --ok:#057836;--err:#b3261e;--font:"Roboto Flex",Roboto,system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141312;--card:#1c1b1a;--low:#201f1e;--fg:#e9e5e1;
  --mut:#cbc6c0;--line:#4a4743;--ink:#f0ece8;--on-ink:#1c1b1a;--ok:#7cd591;--err:#ffb4ab;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#141312;--card:#1c1b1a;--low:#201f1e;--fg:#e9e5e1;--mut:#cbc6c0;--line:#4a4743;--ink:#f0ece8;
  --on-ink:#1c1b1a;--ok:#7cd591;--err:#ffb4ab;color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:400 15px/1.5 var(--font);-webkit-font-smoothing:antialiased}}
a{{color:inherit}}
.wrap{{max-width:880px;margin:0 auto;padding:0 16px}}
header{{border-bottom:1px solid var(--line)}}
header .wrap{{display:flex;align-items:center;justify-content:space-between;gap:12px;height:60px}}
header .brand{{font:800 19px/1 var(--font);font-stretch:85%;text-decoration:none}}
header .back{{text-decoration:none;font-weight:600;padding:8px 14px;border-radius:999px;border:1px solid var(--line)}}
.hero{{padding:36px 0 20px}}
h1{{font:800 clamp(30px,6vw,46px)/1.04 var(--font);font-stretch:80%;letter-spacing:-.5px;margin:0 0 12px}}
.lead{{color:var(--mut);font-size:17px;max-width:640px;margin:0}}
h2{{font:800 26px/1.1 var(--font);font-stretch:85%;margin:40px 0 14px}}
details{{background:var(--card);border-radius:18px;box-shadow:inset 0 0 0 1px var(--line);margin:0 0 8px;overflow:hidden}}
summary{{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;list-style:none}}
summary::-webkit-details-marker{{display:none}}
summary b{{font-size:16px}}.cnt{{color:var(--mut);font-size:13px;font-weight:600}}
.bad{{background:var(--err);color:var(--bg);border-radius:999px;font-size:11.5px;font-weight:700;padding:2px 8px}}
.chev{{margin-left:auto;color:var(--mut);font-size:20px;line-height:1;transition:transform .2s}}
details[open] .chev{{transform:rotate(180deg)}}
.srcs{{list-style:none;margin:0;padding:0 8px 8px}}
.srcs li{{display:flex;align-items:center;gap:12px;padding:10px 8px;border-top:1px solid var(--line)}}
.srcs li div{{flex:1;min-width:0}}
.srcs li a{{font-weight:650;text-decoration:none}}.srcs li a:hover{{text-decoration:underline}}
.srcs li div small{{display:block;color:var(--mut);font-size:12.5px;overflow-wrap:anywhere}}
.srcs .n{{text-align:right;font-weight:750;font-size:16px;line-height:1.1;flex:none;min-width:64px}}
.srcs .n small{{display:block;color:var(--mut);font-weight:500;font-size:11.5px}}
.errt{{color:var(--err)}}
.fi{{position:relative;width:36px;height:36px;border-radius:10px;background:#fff;box-shadow:inset 0 0 0 1px var(--line);display:grid;place-items:center;flex:none}}
.fav{{width:22px;height:22px;border-radius:5px;object-fit:contain}}
.fav.av{{display:inline-grid;place-items:center;background:var(--ink);color:var(--on-ink);font-size:12px;font-weight:800;text-transform:uppercase}}
.st{{position:absolute;right:-3px;bottom:-3px;width:12px;height:12px;border-radius:50%;border:2px solid var(--card)}}
.st.ok{{background:var(--ok)}}.st.err{{background:var(--err)}}.st.off{{background:var(--line)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tile{{background:var(--card);border-radius:18px;box-shadow:inset 0 0 0 1px var(--line);padding:16px}}
.tile b{{display:block;font:800 30px/1 var(--font);font-stretch:85%;margin-bottom:6px}}
.tile span{{color:var(--mut);font-size:13px}}
.upd{{color:var(--mut);font-size:13px;margin:12px 0 0}}
.h2n{{color:var(--mut);font-size:18px;font-weight:700;margin-left:4px}}
.sub{{color:var(--mut);margin:-6px 0 14px}}
.box{{background:var(--card);border-radius:18px;box-shadow:inset 0 0 0 1px var(--line);overflow:hidden}}
.box .srcs li:first-child{{border-top:0}}
.srcs li:has(.why){{align-items:flex-start}}
.why{{margin:4px 0 0;font-size:13.5px;color:var(--fg)}}
footer{{border-top:1px solid var(--line);margin-top:48px;padding:20px 0 48px;color:var(--mut);font-size:13px}}
footer a{{color:var(--mut)}}
</style></head><body>
<header><div class="wrap"><a class="brand" href="{escape(base)}/">Tarnów – wydarzenia</a><a class="back" href="{escape(base)}/">← Kalendarz</a></div></header>
<main class="wrap">
<section class="hero">
<h1>Źródła wydarzeń</h1>
<p class="lead">Wydarzenia zbieramy z {len(configs)} stron: instytucji kultury, gmin, kin, serwisów biletowych i mediów.
Każde wydarzenie w kalendarzu ma link do swojego źródła, a powtórzenia z kilku stron są łączone w jedno.
Niżej także źródła, które sprawdziliśmy, ale (jeszcze) nie używamy.</p>
</section>
<h2>Używane <span class="h2n">{len(configs)}</span></h2>
{src_html}
{unused_html}
{add_html}
<h2>W liczbach</h2>
<div class="tiles">{tiles_html}</div>
<p class="upd">Ostatnia aktualizacja: {now:%d.%m.%Y, %H:%M}. Stan źródeł i liczby wydarzeń pochodzą z tej aktualizacji.</p>
</main>
<footer class="wrap"><a href="{escape(base)}/">Kalendarz</a></footer>
<script>
{_favicon_js()}
document.querySelectorAll(".fi[data-site]").forEach(t => t.insertAdjacentHTML("afterbegin", favHTML(t.dataset.site, t.dataset.name)));
</script>
</body></html>"""
