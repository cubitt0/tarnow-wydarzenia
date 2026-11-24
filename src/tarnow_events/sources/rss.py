from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

import feedparser

from .. import http
from ..ai import PROMPT_VERSION, gemini
from ..htmltext import html_to_text
from ..models import TZ
from ..categorize import is_junk
from .base import Source, absolutize, clean_text

log = logging.getLogger(__name__)

BATCH_CHARS = 40000

# An article worth asking the AI about mentions a date or a time somewhere.
DATE_HINT = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}\b|\b\d{1,2}\s+(stycz|lut|mar|kwie|maj|czerw|lip|sierp|wrze|pa[zź]dz|listop|grud)\w*"
    r"|\bgodz\.?\s*\d|\b\d{1,2}:\d{2}\b|\b(poniedzia|wtor|[sś]rod|czwart|pi[aą]t|sobot|niedziel)\w*",
    re.I,
)


class RssSource(Source):
    """RSS/Atom of a news or events section.

    Feed items are usually *announcements*, not structured events: the event date sits in the
    prose. New items are batched and handed to Gemini. Each item remembers which batch answered
    for it, so tomorrow only items never seen before cost an AI call.

    Options:
      max_age_days: ignore items published longer ago than this (default 30)
      fetch_full: also fetch each item's page for the full text (default false)
      max_items: newest N items considered per run (default 40)
      format: "wp_json" when `url` is a WordPress /wp-json/wp/v2/posts endpoint instead of a feed

    Items with a junk title (accidents, promotions, council sessions) or without any date or time
    in their text are dropped before the AI sees them.
    """

    type = "rss"

    def fetch(self):
        ai = gemini(self.settings)  # without a key: cached answers only, no new calls
        opts = self.cfg.options
        if opts.get("format") == "wp_json":
            feed = _wp_json_feed(http.get(self.cfg.url, bare=True).json())
        else:
            feed = feedparser.parse(http.get(self.cfg.url, insecure=bool(opts.get("insecure_tls"))).content)
        cutoff = datetime.now(TZ) - timedelta(days=int(opts.get("max_age_days", 30)))

        index_path = ai.cache_dir / f"rss-index-{self.cfg.id}-{PROMPT_VERSION}.json"
        index: dict[str, str] = json.loads(index_path.read_text()) if index_path.exists() else {}

        batch_keys: list[str] = []  # batches whose cached answers cover already-seen items
        fresh: list[tuple[str, str]] = []  # (item key, text) never sent to the AI
        skipped = 0
        for entry in feed.entries[: int(opts.get("max_items", 40))]:
            if is_junk(entry.get("title") or ""):
                skipped += 1
                continue
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and datetime(*published[:6], tzinfo=TZ) < cutoff:
                continue
            body = entry.get("summary", "")
            if entry.get("content"):
                body = entry.get("content")[0].get("value", body)
            if opts.get("fetch_full") and entry.get("link"):
                try:
                    body = html_to_text(http.get(entry.get("link")).text, entry.get("link"))[:6000]
                except Exception as e:
                    log.info("%s: full text %s failed: %s", self.cfg.id, entry.get("link"), e)
            block = (
                f"## [{clean_text(entry.get('title'))}]({entry.get('link')})\n"
                f"Opublikowano: {entry.get('published', '')}\n{(clean_text(body, keep_newlines=True) or '')[:8000]}"
            )
            if not DATE_HINT.search(block):
                skipped += 1
                continue
            item_key = hashlib.sha256(block.encode()).hexdigest()[:32]
            if item_key in index and ai.load(index[item_key]) is not None:
                if index[item_key] not in batch_keys:
                    batch_keys.append(index[item_key])
            else:
                fresh.append((item_key, block))

        # Batch the new items (~15 articles per call).
        batches: list[list[tuple[str, str]]] = [[]]
        size = 0
        for item in fresh:
            if size + len(item[1]) > BATCH_CHARS and batches[-1]:
                batches.append([])
                size = 0
            batches[-1].append(item)
            size += len(item[1])

        if skipped:
            log.info("%s: %d items skipped before AI (junk title or no date)", self.cfg.id, skipped)
        items: list[dict] = []
        for key in batch_keys:
            items += ai.load(key) or []
        for batch in batches:
            if not batch or not ai.budget_left():
                continue  # unseen items wait for tomorrow's budget
            text = "\n\n".join(b for _, b in batch)
            key = hashlib.sha256(("rss" + PROMPT_VERSION + self.cfg.id + text).encode()).hexdigest()
            result = ai.extract_events(
                text=text, source=self.cfg.name, url=self.cfg.url,
                default_location=self.default_location_text(), key=key,
            )
            if ai.load(key) is not None:  # only index items whose batch really got an answer
                for item_key, _ in batch:
                    index[item_key] = key
            items += result
        index_path.write_text(json.dumps(index))

        seen: set[tuple] = set()
        for item in items:
            ident = (item.get("title"), item.get("start"))
            if ident in seen:
                continue
            seen.add(ident)
            ev = self.make_event(
                title=item.get("title"),
                start=item.get("start"),
                end=item.get("end"),
                url=absolutize(item.get("url"), self.cfg.url),
                description=item.get("description"),
                location_name=item.get("location_name"),
                address=item.get("address"),
                town=item.get("town"),
                price=item.get("price"),
                categories=[item.get("category")] if item.get("category") else None,
            )
            if ev:
                yield ev


def _wp_json_feed(posts: list[dict]):
    """WordPress REST posts → the subset of feedparser's shape this adapter reads."""
    from types import SimpleNamespace
    import time as _time

    entries = []
    for p in posts:
        try:
            ts = _time.strptime(p.get("date_gmt") or p.get("date"), "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            ts = None
        entries.append({
            "title": (p.get("title") or {}).get("rendered"),
            "link": p.get("link"),
            "summary": (p.get("excerpt") or {}).get("rendered", ""),
            "content": [{"value": (p.get("content") or {}).get("rendered", "")[:8000]}],
            "published": p.get("date"),
            "published_parsed": ts,
        })
    return SimpleNamespace(entries=entries)

