from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

import feedparser

from .. import http
from ..ai import gemini
from ..htmltext import html_to_text
from ..models import TZ
from .base import Source, absolutize, clean_text

log = logging.getLogger(__name__)

BATCH_CHARS = 20000


class RssSource(Source):
    """RSS/Atom of a news or events section.

    Feed items are usually *announcements*, not structured events: the event date sits in the
    prose. New items are batched and handed to Gemini. Each item remembers which batch answered
    for it, so tomorrow only items never seen before cost an AI call.

    Options:
      max_age_days: ignore items published longer ago than this (default 30)
      fetch_full: also fetch each item's page for the full text (default false)
    """

    type = "rss"

    def fetch(self):
        ai = gemini(self.settings)
        if not ai.enabled:
            log.warning("%s: GEMINI_API_KEY not set, skipping", self.cfg.id)
            return
        opts = self.cfg.options
        feed = feedparser.parse(http.get(self.cfg.url).content)
        cutoff = datetime.now(TZ) - timedelta(days=int(opts.get("max_age_days", 30)))

        index_path = ai.cache_dir / f"rss-index-{self.cfg.id}.json"
        index: dict[str, str] = json.loads(index_path.read_text()) if index_path.exists() else {}

        batch_keys: list[str] = []  # batches whose cached answers cover already-seen items
        fresh: list[tuple[str, str]] = []  # (item key, text) never sent to the AI
        for entry in feed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and datetime(*published[:6], tzinfo=TZ) < cutoff:
                continue
            body = entry.get("summary", "")
            if entry.get("content"):
                body = entry.content[0].get("value", body)
            if opts.get("fetch_full") and entry.get("link"):
                try:
                    body = html_to_text(http.get(entry.link).text, entry.link)[:6000]
                except Exception as e:
                    log.info("%s: full text %s failed: %s", self.cfg.id, entry.link, e)
            block = (
                f"## [{clean_text(entry.get('title'))}]({entry.get('link')})\n"
                f"Opublikowano: {entry.get('published', '')}\n{(clean_text(body, keep_newlines=True) or '')[:8000]}"
            )
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

        items: list[dict] = []
        for key in batch_keys:
            items += ai.load(key) or []
        for batch in batches:
            if not batch:
                continue
            text = "\n\n".join(b for _, b in batch)
            key = hashlib.sha256(("rss" + self.cfg.id + text).encode()).hexdigest()
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
