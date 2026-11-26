"""Rebuild the site in public/ from the live site's data, without fetching any source: a quick local
preview of page/feed changes. Then `docker compose up web` → http://localhost:8080

    docker compose run --rm -v ./scripts:/app/scripts:ro --entrypoint python scraper scripts/preview.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from urllib.request import urlopen

from tarnow_events.config import Settings, load_sources
from tarnow_events.models import Event, SourceRef
from tarnow_events.pipeline import write_outputs

LIVE = sys.argv[1] if len(sys.argv) > 1 else "https://tarnow-wydarzenia.pl"


def event(d: dict) -> Event:
    refs = [SourceRef(**r) for r in d.pop("sources")]
    d["start"] = datetime.fromisoformat(d["start"])
    d["end"] = datetime.fromisoformat(d["end"]) if d["end"] else None
    return Event(source=refs[0], sources=refs, **d)


def main() -> None:
    settings = Settings.from_env()
    events = [event(d) for d in json.load(urlopen(f"{LIVE}/events.json"))]
    stats = json.load(urlopen(f"{LIVE}/status.json")).get("sources", {})
    configs = [c for c in load_sources(settings.sources_file) if c.enabled]
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    write_outputs(settings, events, configs, stats)
    print(f"{len(events)} events from {LIVE} → {settings.out_dir}")


if __name__ == "__main__":
    main()
