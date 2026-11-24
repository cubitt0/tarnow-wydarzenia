"""The static index page: subscribe links for every feed, the next events, source health."""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote

from .models import TZ

DAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


def render_index(settings, feeds, events, configs, stats) -> str:
    base = settings.public_base_url
    now = datetime.now(TZ)

    def feed_row(f: dict) -> str:
        https = f"{base}/{f['path']}"
        webcal = "webcal://" + https.split("://", 1)[1]
        google = "https://calendar.google.com/calendar/r?cid=" + quote(webcal, safe="")
        return (
            f"<tr><td>{escape(f['name'])}</td><td class=n>{f['count']}</td>"
            f"<td><a href='{escape(webcal)}'>Subskrybuj</a> · <a href='{escape(google)}'>Google</a> · "
            f"<a href='{escape(f['path'])}'>.ics</a> "
            f"<button onclick=\"navigator.clipboard.writeText('{escape(https)}')\">kopiuj URL</button></td></tr>"
        )

    groups = {"main": "Główne", "category": "Kategorie", "source": "Pojedyncze źródła"}
    feed_html = ""
    for g, label in groups.items():
        rows = "".join(feed_row(f) for f in feeds if f["group"] == g and (f["count"] or g != "source"))
        body = f"<table>{rows}</table>"
        feed_html += f"<details{' open' if g == 'main' else ''}><summary>{label}</summary>{body}</details>"

    upcoming = [e for e in events if (e.end or e.start) >= now][:150]
    ev_rows = []
    for e in upcoming:
        when = f"{DAYS[e.start.weekday()]} {e.start:%d.%m}" + ("" if e.all_day else f" {e.start:%H:%M}")
        links = " ".join(f"<a href='{escape(r.url)}'>{escape(r.source_name)}</a>" for r in e.all_sources())
        ev_rows.append(
            f"<li><span class=d>{when}</span> <b>{escape(e.title)}</b>"
            f"<br><small>{escape(e.location_line() or '')} — {links}</small></li>"
        )

    src_rows = []
    for c in configs:
        s = stats.get(c.id, {})
        status = "✅" if s.get("ok") else "❌"
        err = f" <small>{escape(s.get('error', ''))}</small>" if not s.get("ok") else ""
        src_rows.append(
            f"<tr><td>{status}</td><td><a href='{escape(c.url)}'>{escape(c.name)}</a>{err}</td>"
            f"<td>{escape(c.group)}</td><td class=n>{s.get('events', 0)}</td></tr>"
        )

    return f"""<!doctype html><html lang=pl><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Tarnów Wydarzenia</title>
<link rel="icon" href="favicon.ico" sizes="48x48">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{{--bg:#fff;--fg:#1b1b1b;--mut:#666;--line:#e4e4e4;--acc:#0b5fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#eee;--mut:#9a9a9a;--line:#2c2c2c;--acc:#7aa7ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px}}
a{{color:var(--acc)}} table{{width:100%;border-collapse:collapse}} td{{border-bottom:1px solid var(--line);padding:6px 4px;vertical-align:top}}
td.n{{text-align:right;color:var(--mut)}} small{{color:var(--mut)}} summary{{font-weight:600;cursor:pointer;margin:12px 0 4px}}
ul{{list-style:none;padding:0}} li{{padding:6px 0;border-bottom:1px solid var(--line)}} .d{{color:var(--mut);display:inline-block;min-width:105px}}
button{{font-size:12px}}
</style></head><body>
<h1>Tarnów i okolice ({int(settings.radius_km)} km) — kalendarze</h1>
<p><a href='index.html'>← Kalendarz</a></p>
<p><small>Aktualizacja: {now:%Y-%m-%d %H:%M}. {len(events)} wydarzeń z {sum(1 for s in stats.values() if s.get('ok'))} źródeł.
Każde wydarzenie ma link do źródła; duplikaty z różnych źródeł są scalane.</small></p>
<h2>Kalendarze</h2>{feed_html}
<h2>Najbliższe wydarzenia</h2><ul>{''.join(ev_rows)}</ul>
<details><summary>Źródła ({len(configs)})</summary><table>{''.join(src_rows)}</table></details>
</body></html>"""
