from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from html import escape

from . import archive
from .ai import gemini
from .describe import improve_descriptions
from .categorize import categorize, is_junk
from .config import Settings, SourceConfig, load_sources
from .dedup import deduplicate
from .howto import APPS, app_icon, howto_html
from .images import enrich_images
from .geo import Geocoder, canonical_town, correct_tarnow, distance_from_tarnow, nearest_town, norm_town
from .ical_out import build_calendar
from .models import CATEGORIES, TZ, Event
from .series import collapse_series, is_long
from .tickets import free_ticket, infer_free
from .seo import home_list, write_seo
from .ads import apply_ads
from .site import render_index
from .timeinfer import infer_time
from .sources import REGISTRY
from .sources.facebook import FacebookSource, find_event_ids

log = logging.getLogger(__name__)

TARNOW_CITY_KM = 6.0
AI_TYPES = {"html_ai", "rss", "pdf_ai"}  # radius treated as "Tarnów itself" for tarnow.ics


def run_source(src, stats: dict) -> list[Event]:
    t0 = time.monotonic()
    try:
        events = list(src.fetch())
        stats[src.cfg.id] = {"ok": True, "events": len(events), "seconds": round(time.monotonic() - t0, 1)}
        log.info("%-32s %4d events (%.1fs)", src.cfg.id, len(events), time.monotonic() - t0)
        return events
    except Exception as e:  # a broken source must never break the feed
        stats[src.cfg.id] = {"ok": False, "events": 0, "error": f"{type(e).__name__}: {e}"[:300]}
        log.warning("%-32s FAILED: %s: %s", src.cfg.id, type(e).__name__, e)
        return []


def locate(ev: Event, cfg: SourceConfig, geo: Geocoder, radius_km: float) -> float | None:
    """Fill lat/lon and return distance from Tarnów, or None when the place is unknowable."""
    town_pt = geo.town(ev.town) if ev.town else None
    if ev.lat is not None and ev.lon is not None and norm_town(ev.town) == "tarnow":
        # The source's own pin is far outside the city: its "Tarnów" is the listing's city, not the venue's.
        d = distance_from_tarnow(ev.lat, ev.lon)
        if d > 2 * TARNOW_CITY_KM:
            ev.town = nearest_town(ev.lat, ev.lon)
            town_pt = geo.town(ev.town) if ev.town else None
    if ev.lat is None or ev.lon is None:
        pt = None
        town = ev.town or cfg.town
        if ev.location_name or ev.address:
            q = ", ".join(p for p in (ev.address or ev.location_name, town) if p)
            pt = geo.query(q)
            if pt is None and ev.address and ev.location_name:
                pt = geo.query(", ".join(p for p in (ev.location_name, town) if p))
        pt = pt or town_pt or geo.town(cfg.town)
        if pt is None:
            return None
        ev.lat, ev.lon = pt
        if not ev.town:
            ev.town = cfg.town
    dist = distance_from_tarnow(ev.lat, ev.lon)
    # A venue geocoded to a same-named street in another city: trust the town instead.
    if dist > radius_km and town_pt and distance_from_tarnow(*town_pt) <= radius_km:
        ev.lat, ev.lon = town_pt
        dist = distance_from_tarnow(*town_pt)
    return dist


def run(settings: Settings, only: set[str] | None = None, use_ai: bool = True) -> dict:
    t0 = time.monotonic()
    configs = [c for c in load_sources(settings.sources_file) if c.enabled and (not only or c.id in only)]
    if not use_ai:
        settings.gemini_api_key = None
    sources = []
    for cfg in configs:
        cls = REGISTRY.get(cfg.type)
        if cls is None:
            log.error("source %s: unknown type %r", cfg.id, cfg.type)
            continue
        sources.append(cls(cfg, settings))

    stats: dict[str, dict] = {}
    fb_sources = [s for s in sources if isinstance(s, FacebookSource)]
    ai_sources = [s for s in sources if s.cfg.type in AI_TYPES]
    others = [s for s in sources if not isinstance(s, FacebookSource) and s.cfg.type not in AI_TYPES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda s: run_source(s, stats), others))
    events = [e for batch in batches for e in batch]
    # AI sources one by one, most important first: if the daily budget runs out, it is the
    # news portals that wait for tomorrow, not the theatre.
    for src in sorted(ai_sources, key=lambda s: -s.cfg.priority):
        events += run_source(src, stats)

    # Facebook permalinks found in any other source's events are followed up.
    fb_ids: set[str] = set()
    for e in events:
        for r in e.all_sources():
            fb_ids |= find_event_ids(r.url)
        fb_ids |= find_event_ids(e.description)
    for fb in fb_sources:
        fb.discovered = fb_ids
        events += run_source(fb, stats)

    cfg_by_id = {c.id: c for c in configs}
    now = datetime.now(TZ)
    lo, hi = now - timedelta(days=settings.days_back), now + timedelta(days=settings.days_ahead)
    geo = Geocoder(settings.cache_dir)
    kept: list[Event] = []
    dropped = {"window": 0, "junk": 0, "no_location": 0, "too_far": 0}
    for ev in events:
        cfg = cfg_by_id[ev.source.source_id]
        if is_junk(ev.title):
            dropped["junk"] += 1
            continue
        if cfg.options.get("excerpt"):
            ev.description = mark_excerpt(ev.description)
        ev.town = correct_tarnow(canonical_town(ev.town), " ".join(p for p in (ev.location_name, ev.address) if p))
        infer_time(ev)
        infer_free(ev)
        if (ev.end or ev.start) < lo or ev.start > hi:
            dropped["window"] += 1
            continue
        dist = locate(ev, cfg, geo, settings.radius_km)
        if dist is None:
            dropped["no_location"] += 1
            continue
        if dist > settings.radius_km:
            dropped["too_far"] += 1
            continue
        categorize(ev, cfg.category, from_source=ev.categories != [cfg.category])
        kept.append(ev)
    kept = collapse_series(kept)

    out = settings.out_dir
    state_path = out / "state.json"
    uid_state: dict[str, str] = {}
    if state_path.exists():
        uid_state = json.loads(state_path.read_text()).get("uids", {})
    # Events the last build published (their UIDs): only new ones get a long text shortened by AI. No state
    # (first build, failed download) counts nothing as new, so the whole calendar is not rewritten at once.
    published_before = set(uid_state.values())
    merged = deduplicate(kept, uid_state)
    # Pictures and ticket links from the events' own pages (cinema has posters already; skip it).
    img_stats = enrich_images([e for e in merged if e.in_main], settings.cache_dir,
                              listing_urls={c.url for c in configs})
    # Cinema: no page reading, but a screening listed by a ticket shop (Bilety24) is its own ticket link.
    for e in merged:
        if not e.in_main and not e.tickets:
            e.tickets = free_ticket(e)
    apply_tickets_fallback(merged, configs)
    # "Czytaj dalej" to the main source: decided on the texts as fetched, before the AI rewrites any.
    always_more = {c.id for c in configs if c.options.get("read_more") == "always"}
    for e in merged:
        e.desc_more = desc_cut(e.description) or e.all_sources()[0].source_id in always_more
    # Events several sources report, and texts too long for the page: one description by Gemini (cached).
    ai = gemini(settings)
    no_ai = {c.id for c in configs if c.options.get("ai_description") is False}
    new = {e.uid for e in merged if e.uid not in published_before} if published_before else set()
    desc_stats = improve_descriptions(merged, ai, now, no_ai, new)

    write_outputs(settings, merged, configs, stats)
    summary = {
        "generated_at": now.isoformat(),
        "raw_events": len(events),
        "kept": len(kept),
        "published": len(merged),
        "dropped": dropped,
        "ai_calls": ai.calls,
        "ai_calls_today": ai.calls_today(),
        "ai_daily_limit": ai.daily_limit,
        "ai_cache_hits": ai.cache_hits,
        "ai_stale_reuses": ai.stale_reuses,
        "ai_desc_calls": ai.desc_calls,
        **desc_stats,
        "geocoder_lookups": geo.lookups,
        **img_stats,
        "seconds": round(time.monotonic() - t0, 1),
        "sources": stats,
    }
    # Prune UIDs not seen for a while is unnecessary at this scale; state stays small.
    state_path.write_text(json.dumps({"uids": uid_state}, ensure_ascii=False))
    (out / "status.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    log.info("published %d events (raw %d, dropped %s) in %.0fs", len(merged), len(events), dropped, summary["seconds"])
    return summary


DESC_CHARS = 1200  # room for the AI's multi-source descriptions (describe.py), up to 1200


# A source that sends only the start of its text marks the cut ("...", "…", "[…]") - the rest is on its page.
_SOURCE_CUT = re.compile(r"(\.\.\.|…|\[(…|\.\.\.)\])\s*$")


def desc_cut(text: str | None) -> bool:
    """Is the description incomplete (cut by us or by the source)? Then the page links to the main source with
    "Czytaj dalej". Short texts ending in "..." are taken as style, not a cut."""
    text = (text or "").strip()
    return len(text) > DESC_CHARS or (len(text) >= 100 and bool(_SOURCE_CUT.search(text)))


_SENTENCE_END = re.compile(r"[.!?…\"”»)]\s*$")


def mark_excerpt(text: str | None) -> str | None:
    """Text from a source that sends only the start of its page (sources.yaml: excerpt: true; allevents.in and
    Bilety24 stop at ~300 characters, often mid-word, without saying so). When it stops mid-sentence it ends at
    the last whole word with "…": readers see the cut and desc_cut() offers "Czytaj dalej"."""
    t = (text or "").rstrip()
    if not t or _SENTENCE_END.search(t):
        return text
    m = re.match(r"(?s)(.*\S)\s+\S{1,40}$", t)  # drop the last word: it may be cut ("Ra", "powr", "http")
    return (m.group(1) if m else t).rstrip(" ,;:–-.\n") + "…"


def read_more(e: Event) -> bool:
    """Link "Czytaj dalej" to the main source: the text was cut or shortened (desc_more, set before the AI
    rewrote it), the main source always wants it, or it is cut now (archived events, local previews)."""
    return bool(e.description) and (e.desc_more or desc_cut(e.description))


def short_desc(text: str | None) -> str | None:
    """Descriptions on the page are previews: cut at a word boundary and mark the cut with "…"."""
    text = (text or "").strip()
    if len(text) <= DESC_CHARS:
        return text or None
    cut = text[:DESC_CHARS]
    if " " in cut[DESC_CHARS // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:–-\n") + "…"


def json_end(e: Event) -> str | None:
    """End for the web page. All-day ends follow iCal there: the day *after* the last day
    (the model keeps the last day itself, like ical_out/seo do before converting)."""
    if not e.end:
        return None
    if e.all_day:
        after = e.end.astimezone(TZ).date() + timedelta(days=1)
        return datetime(after.year, after.month, after.day, tzinfo=TZ).isoformat()
    return e.end.isoformat()


def apply_tickets_fallback(events: list[Event], configs: list[SourceConfig]) -> None:
    """No ticket link found: a source's general box office (`tickets_fallback`), e.g. a theatre's Bilety24 page."""
    fallback = {c.id: c.options["tickets_fallback"] for c in configs if c.options.get("tickets_fallback")}
    for e in events:
        if not e.tickets and not e.cancelled:
            e.tickets = next((fallback[r.source_id] for r in e.all_sources() if r.source_id in fallback), None)


def is_tarnow(e: Event) -> bool:
    if norm_town(e.town) == "tarnow":
        return True
    return e.lat is not None and distance_from_tarnow(e.lat, e.lon) <= TARNOW_CITY_KM


def write_outputs(settings: Settings, events: list[Event], configs: list[SourceConfig], stats: dict) -> None:
    out: Path = settings.out_dir
    (out / "kategoria").mkdir(parents=True, exist_ok=True)
    (out / "zrodlo").mkdir(parents=True, exist_ok=True)

    feeds: list[dict] = []

    def feed(path: str, name: str, desc: str, evs: list[Event], group: str, **meta) -> None:
        """meta (for the subscribe wizard and zrodla.html): region, cat, or src/site/sgroup/label for a source."""
        (out / path).write_bytes(build_calendar(name, desc, evs))
        feeds.append({"path": path, "name": name, "count": len(evs), "group": group, **meta})

    radius = int(settings.radius_km)
    regions = {"all": f"Tarnów i okolice ({radius} km)", "tarnow": "Tarnów – miasto", "okolice": f"Okolice Tarnowa ({radius} km)"}

    def in_region(e: Event, region: str) -> bool:
        return region == "all" or is_tarnow(e) == (region == "tarnow")

    # Multi-week exhibitions would sit as all-day banners on every day of the main feeds;
    # they get their own feed (and stay in the category feeds).
    short = [e for e in events if not is_long(e) and e.in_main]
    long_ = [e for e in events if is_long(e)]
    feed("wszystkie.ics", "Tarnów i okolice – wszystko", f"Wszystkie wydarzenia: Tarnów + {radius} km", short, "main",
         region="all")
    feed("tarnow.ics", "Tarnów – miasto", "Wydarzenia w granicach Tarnowa", [e for e in short if is_tarnow(e)], "main",
         region="tarnow")
    feed("okolice.ics", f"Okolice Tarnowa ({radius} km)", "Wydarzenia poza Tarnowem",
         [e for e in short if not is_tarnow(e)], "main", region="okolice")
    feed("dlugie.ics", "Tarnów i okolice – wystawy i wydarzenia wielodniowe", "Trwające dłużej niż tydzień",
         long_, "main", region="all", cat="dlugie")
    for slug, label in CATEGORIES.items():
        feed(f"kategoria/{slug}.ics", f"Tarnów: {label}", label, [e for e in events if slug in e.categories], "category",
             region="all", cat=slug)
    # The same per region: region/tarnow/koncerty.ics, region/okolice/dlugie.ics …
    for region in ("tarnow", "okolice"):
        (out / "region" / region).mkdir(parents=True, exist_ok=True)
        rname = regions[region]
        feed(f"region/{region}/dlugie.ics", f"{rname} – wystawy i wydarzenia wielodniowe", "Trwające dłużej niż tydzień",
             [e for e in long_ if in_region(e, region)], "region", region=region, cat="dlugie")
        for slug, label in CATEGORIES.items():
            feed(f"region/{region}/{slug}.ics", f"{rname}: {label}", label,
                 [e for e in events if slug in e.categories and in_region(e, region)], "region", region=region, cat=slug)
    for cfg in configs:
        evs = [e for e in events if any(r.source_id == cfg.id for r in e.all_sources())]
        feed(f"zrodlo/{cfg.id}.ics", f"Tarnów: {cfg.name}", f"Źródło: {cfg.url}", evs, "source",
             src=cfg.id, label=cfg.name, site=cfg.url, sgroup=cfg.group)

    def ev_json(e: Event) -> dict:
        d = asdict(e)
        d["start"] = e.start.isoformat()
        d["end"] = e.end.isoformat() if e.end else None
        d["sources"] = [asdict(r) for r in e.all_sources()]
        d.pop("source")
        d.pop("source_texts")  # input for describe.py, not data to publish
        return d

    (out / "events.json").write_text(json.dumps([ev_json(e) for e in events], ensure_ascii=False, indent=1))
    (out / "feeds.json").write_text(json.dumps(feeds, ensure_ascii=False, indent=1))
    # Crawlable page per upcoming event, and for a year per past one (archive.py), sitemap.xml, robots.txt, 404.html.
    # A source that failed or listed nothing today proves nothing about its events having been removed.
    arch_files = (out / archive.FILE, settings.cache_dir / archive.FILE)
    arch = archive.load(*arch_files)
    fetched = {sid for sid, s in stats.items() if s.get("ok") and s.get("events")}
    pages = write_seo(out, settings.public_base_url, events, datetime.now(TZ), arch, fetched)
    archive.save(arch, *arch_files)
    cal = json.dumps(calendar_json(events, feeds, pages), ensure_ascii=False, separators=(",", ":"))
    (out / "calendar.json").write_text(cal)
    # The page loads data as a script (works from file:// too, no fetch/CORS concerns).
    (out / "data.js").write_text("window.CAL = " + cal.replace("</", "<\\/") + ";\n")
    # Sources page: what we fetch (with its state), then sources considered and not used, from the research
    # catalogue (docs/sources-catalogue.json, see scripts/render_catalogue.py).
    try:
        catalogue = json.loads((settings.root / "docs" / "sources-catalogue.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalogue = []
    totals = {"all": len(events), "tarnow": sum(1 for e in events if e.in_main and is_tarnow(e)),
              "okolice": sum(1 for e in events if e.in_main and not is_tarnow(e)), "kino": sum(1 for e in events if not e.in_main)}
    fb_form = next((c.options.get("form_url") for c in configs if c.type == "facebook" and c.options.get("form_url")), None)
    (out / "zrodla.html").write_text(render_index(settings, feeds, events, configs, stats, SOURCE_GROUPS, totals=totals,
                                                  catalogue=catalogue, fb_form=fb_form), encoding="utf-8")
    (out / "katalog-zrodel.html").unlink(missing_ok=True)  # merged into zrodla.html
    web = Path(__file__).parent / "web"
    # Site icon (Material "local_activity" ticket, same as the logo); PNG/ICO are pre-rendered from the SVG.
    for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png", "og-image.png"):
        (out / name).write_bytes((web / name).read_bytes())
    # The web calendar is a static page that reads calendar.json.
    page = (web / "kalendarz.html").read_text(encoding="utf-8")
    page = page.replace("<!--STATIC-LIST-->", home_list(events, pages, datetime.now(TZ)))
    if fb_form:  # the button takes the place of ‹ Dziś › (keyboard ←/t/→ still works)
        a, b = page.index("<!--NAV-->"), page.index("<!--/NAV-->") + len("<!--/NAV-->")
        page = page[:a] + FB_ADD_BUTTON.replace("{href}", escape(fb_form, quote=True)) + page[b:]
    (out / "index.html").write_text(apply_ads(page, out, settings.adsense_client, settings.adsense_slot),
                                    encoding="utf-8")


# Facebook logo: Simple Icons (CC0), drawn in the text colour.
FB_ADD_BUTTON = (
    '<a class="fbadd sl" href="{href}" target="_blank" rel="noopener" title="Dodaj wydarzenie z Facebooka">'
    '<span class="ms" aria-hidden="true">add</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 '
    '1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 '
    '26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 '
    '1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 '
    '5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z"/></svg>'
    'Wydarzenie z FB</a>'
)

REGION_NAMES = {"all": "Tarnów i okolice", "tarnow": "Tarnów – miasto", "okolice": "Okolice Tarnowa"}
# sources.yaml `group` → heading in the subscribe wizard and on zrodla.html, in this order.
SOURCE_GROUPS = {"miasto": "Miasto Tarnów", "kultura": "Kultura w Tarnowie", "kino": "Kina", "bilety": "Serwisy biletowe",
                 "sport": "Sport", "edukacja": "Uczelnie i szkoły", "religia": "Kościół", "lokale": "Lokale i galerie",
                 "media": "Media", "region": "Gminy i miasta w okolicy", "facebook": "Facebook"}


def calendar_json(events: list[Event], feeds: list[dict], pages: dict[str, str] | None = None) -> dict:
    """Compact payload for the web calendar (index.html). pages: uid -> the event's own page (seo.py)."""
    pages = pages or {}
    rows = []
    for e in events:
        rows.append({
            "id": e.uid,
            "t": ("[ODWOŁANE] " if e.cancelled else "") + e.title,
            "s": e.start.isoformat(),
            "e": json_end(e),
            "ad": e.all_day,
            "loc": e.location_line(),
            "geo": [round(e.lat, 5), round(e.lon, 5)] if e.lat is not None else None,
            "c": e.categories,
            "d": short_desc(e.description),
            "dm": read_more(e),  # the page offers "Czytaj dalej" to the main source
            "p": e.price,
            "img": e.image,
            "tk": e.tickets,
            "u": pages.get(e.uid),
            "src": [[r.source_name, r.url] for r in e.all_sources()],
            "main": e.in_main,
            "long": is_long(e),
            "x": e.cancelled,
            # Where filter on the web page: Tarnów itself vs. the surroundings, and distance from the Rynek.
            "tw": is_tarnow(e),
            "km": round(distance_from_tarnow(e.lat, e.lon)) if e.lat is not None else None,
            "town": e.town,
        })
    return {
        "generated": datetime.now(TZ).isoformat(),
        "categories": CATEGORIES,
        # Every feed for the subscribe wizard; a source with nothing upcoming has nothing to subscribe to.
        "feeds": [{k: v for k, v in f.items() if k != "name" or f["group"] != "source"}
                  for f in feeds if f["count"] or f["group"] != "source"],
        "regions": REGION_NAMES,
        "groups": SOURCE_GROUPS,
        "howto": howto_html(),
        "apps": [{**a, "icon": app_icon(a["icon"])} for a in APPS],
        "events": rows,
    }
