from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .ai import gemini
from .categorize import categorize, is_junk
from .config import Settings, SourceConfig, load_sources
from .dedup import deduplicate
from .geo import Geocoder, canonical_town, distance_from_tarnow, norm_town
from .ical_out import build_calendar
from .models import CATEGORIES, TZ, Event
from .series import collapse_series, is_long
from .site import render_index
from .sources import REGISTRY
from .sources.facebook import FacebookSource, find_event_ids

log = logging.getLogger(__name__)

TARNOW_CITY_KM = 6.0  # radius treated as "Tarnów itself" for tarnow.ics


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
    others = [s for s in sources if not isinstance(s, FacebookSource)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda s: run_source(s, stats), others))
    events = [e for batch in batches for e in batch]

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
        ev.town = canonical_town(ev.town)
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
    merged = deduplicate(kept, uid_state)

    write_outputs(settings, merged, configs, stats)
    ai = gemini(settings)
    summary = {
        "generated_at": now.isoformat(),
        "raw_events": len(events),
        "kept": len(kept),
        "published": len(merged),
        "dropped": dropped,
        "ai_calls": ai.calls,
        "ai_cache_hits": ai.cache_hits,
        "geocoder_lookups": geo.lookups,
        "seconds": round(time.monotonic() - t0, 1),
        "sources": stats,
    }
    # Prune UIDs not seen for a while is unnecessary at this scale; state stays small.
    state_path.write_text(json.dumps({"uids": uid_state}, ensure_ascii=False))
    (out / "status.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    log.info("published %d events (raw %d, dropped %s) in %.0fs", len(merged), len(events), dropped, summary["seconds"])
    return summary


def is_tarnow(e: Event) -> bool:
    if norm_town(e.town) == "tarnow":
        return True
    return e.lat is not None and distance_from_tarnow(e.lat, e.lon) <= TARNOW_CITY_KM


def write_outputs(settings: Settings, events: list[Event], configs: list[SourceConfig], stats: dict) -> None:
    out: Path = settings.out_dir
    (out / "kategoria").mkdir(parents=True, exist_ok=True)
    (out / "zrodlo").mkdir(parents=True, exist_ok=True)

    feeds: list[dict] = []

    def feed(path: str, name: str, desc: str, evs: list[Event], group: str) -> None:
        (out / path).write_bytes(build_calendar(name, desc, evs))
        feeds.append({"path": path, "name": name, "count": len(evs), "group": group})

    radius = int(settings.radius_km)
    # Multi-week exhibitions would sit as all-day banners on every day of the main feeds;
    # they get their own feed (and stay in the category feeds).
    short = [e for e in events if not is_long(e) and e.in_main]
    feed("wszystkie.ics", "Tarnów i okolice – wszystko", f"Wszystkie wydarzenia: Tarnów + {radius} km", short, "main")
    feed("tarnow.ics", "Tarnów – miasto", "Wydarzenia w granicach Tarnowa", [e for e in short if is_tarnow(e)], "main")
    feed("okolice.ics", f"Okolice Tarnowa ({radius} km)", "Wydarzenia poza Tarnowem",
         [e for e in short if not is_tarnow(e)], "main")
    feed("dlugie.ics", "Tarnów i okolice – wystawy i wydarzenia wielodniowe", "Trwające dłużej niż tydzień",
         [e for e in events if is_long(e)], "main")
    for slug, label in CATEGORIES.items():
        feed(f"kategoria/{slug}.ics", f"Tarnów: {label}", label, [e for e in events if slug in e.categories], "category")
    for cfg in configs:
        evs = [e for e in events if any(r.source_id == cfg.id for r in e.all_sources())]
        feed(f"zrodlo/{cfg.id}.ics", f"Tarnów: {cfg.name}", f"Źródło: {cfg.url}", evs, "source")

    def ev_json(e: Event) -> dict:
        d = asdict(e)
        d["start"] = e.start.isoformat()
        d["end"] = e.end.isoformat() if e.end else None
        d["sources"] = [asdict(r) for r in e.all_sources()]
        d.pop("source")
        return d

    (out / "events.json").write_text(json.dumps([ev_json(e) for e in events], ensure_ascii=False, indent=1))
    (out / "feeds.json").write_text(json.dumps(feeds, ensure_ascii=False, indent=1))
    (out / "calendar.json").write_text(json.dumps(calendar_json(events, feeds), ensure_ascii=False, separators=(",", ":")))
    (out / "zrodla.html").write_text(render_index(settings, feeds, events, configs, stats), encoding="utf-8")
    catalogue = settings.root / "docs" / "sources.html"
    if catalogue.exists():  # research catalogue of every source considered, published as-is
        (out / "katalog-zrodel.html").write_text(catalogue.read_text(encoding="utf-8"), encoding="utf-8")
    # The web calendar is a static page that reads calendar.json.
    (out / "index.html").write_text((Path(__file__).parent / "web" / "kalendarz.html").read_text(encoding="utf-8"),
                                    encoding="utf-8")


def calendar_json(events: list[Event], feeds: list[dict]) -> dict:
    """Compact payload for the web calendar (index.html)."""
    rows = []
    for e in events:
        rows.append({
            "id": e.uid,
            "t": ("[ODWOŁANE] " if e.cancelled else "") + e.title,
            "s": e.start.isoformat(),
            "e": e.end.isoformat() if e.end else None,
            "ad": e.all_day,
            "loc": e.location_line(),
            "geo": [round(e.lat, 5), round(e.lon, 5)] if e.lat is not None else None,
            "c": e.categories,
            "d": (e.description or "")[:700] or None,
            "p": e.price,
            "img": e.image,
            "src": [[r.source_name, r.url] for r in e.all_sources()],
            "main": e.in_main,
            "long": is_long(e),
            "x": e.cancelled,
        })
    return {
        "generated": datetime.now(TZ).isoformat(),
        "categories": CATEGORIES,
        "feeds": [{"path": f["path"], "name": f["name"], "count": f["count"]} for f in feeds if f["group"] != "source"],
        "events": rows,
    }
