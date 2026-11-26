"""Event pages outlive the events.

Every build replaces the whole site and knows only what the sources list today, so an event's page
used to disappear (404) the day the event ended. The archive remembers every event that had a page,
for a year after it took place, and its page is rebuilt from that:

- the event took place: the page stays (indexable) and says so;
- the source marked it cancelled: the page says "Odwołane", as before;
- it vanished from its sources before its date, while those sources were read fine: it was probably
  cancelled or moved, so the page says it was removed and asks search engines to forget it (noindex).
  Vanishing while a source is down proves nothing: the page then stays as it was.

An event whose date (and so its address) changes leaves a redirect at the old address.

The archive is kept twice, on the site (archive.json.gz, fetched back by the workflow like state.json)
and in the build cache, so one failed download does not cost a year of pages.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import TZ, Event, SourceRef

log = logging.getLogger(__name__)

KEEP_DAYS = 365
FILE = "archive.json.gz"
MAX_OLD_PATHS = 5


def dump(e: Event) -> dict:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat() if e.end else None
    return d


def restore(d: dict) -> Event:
    d = {k: v for k, v in d.items() if k in Event.__dataclass_fields__}  # fields dropped since are ignored
    d["start"] = datetime.fromisoformat(d["start"])
    d["end"] = datetime.fromisoformat(d["end"]) if d.get("end") else None
    d["source"] = SourceRef(**d["source"])
    d["sources"] = [SourceRef(**r) for r in d.get("sources") or []]
    return Event(**d)


def load(*files: Path) -> dict[str, dict]:
    """uid -> {"event", "path", "old": [earlier paths], "gone": bool, "seen": UTC time}; the newest copy wins."""
    out: dict[str, dict] = {}
    for f in files:
        try:
            data = json.loads(gzip.decompress(f.read_bytes()))
        except FileNotFoundError:
            continue
        except (OSError, EOFError, ValueError) as e:  # an HTML error page, a cut download …
            log.warning("archive %s unreadable: %s", f, e)
            continue
        for uid, rec in data.get("events", {}).items():
            if uid not in out or rec.get("seen", "") > out[uid].get("seen", ""):
                out[uid] = rec
    return out


def save(archive: dict[str, dict], *files: Path) -> None:
    blob = gzip.compress(json.dumps({"events": archive}, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(blob)


def _source_ids(rec: dict) -> set[str]:
    ev = rec["event"]
    return {r["source_id"] for r in ev.get("sources") or [ev["source"]]}


def update(archive: dict[str, dict], events: list[Event], paths: dict[str, str], now: datetime,
           fetched: set[str] | None = None) -> list[tuple[Event, dict]]:
    """Record today's pages (paths: uid -> path) in the archive and judge the events that have none today.

    fetched: the sources read successfully in this run (None: all of them). Returns the archived pages
    to rebuild beside today's, newest first, each address once.
    """
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    current = {e.uid: e for e in events if e.in_main}
    for uid, path in paths.items():
        rec = archive.get(uid, {})
        old = rec.get("old", []) + ([rec["path"]] if rec.get("path") and rec["path"] != path else [])
        archive[uid] = {"event": dump(current[uid]), "path": path, "old": [p for p in old if p != path][-MAX_OLD_PATHS:],
                        "gone": False, "seen": stamp}

    cutoff = now.astimezone(TZ).date() - timedelta(days=KEEP_DAYS)
    taken = set(paths.values())
    out: list[tuple[Event, dict]] = []
    for uid, rec in sorted(archive.items(), key=lambda kv: kv[1].get("seen", ""), reverse=True):
        if uid in paths:
            continue
        if uid in current:  # still listed, already over: keep what the source says now (e.g. cancelled late)
            rec.update(event=dump(current[uid]), gone=False, seen=stamp)
        e = restore(rec["event"])
        if (e.end or e.start).astimezone(TZ).date() < cutoff or rec["path"] in taken:
            del archive[uid]  # a year old, or its address belongs to an event listed today (the same one, re-identified)
            continue
        if uid not in current and (e.end or e.start) >= now and (fetched is None or _source_ids(rec) <= fetched):
            rec["gone"] = True
        taken.add(rec["path"])
        out.append((e, rec))
    return out
