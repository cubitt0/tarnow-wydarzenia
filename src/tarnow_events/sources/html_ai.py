from __future__ import annotations

import hashlib
import logging
import time

from .. import http
from ..ai import LEGACY_PROMPT_VERSIONS, PROMPT_VERSION, gemini
from ..htmltext import chunks, html_to_text
from .base import Source, absolutize
from .jsonld import JsonLdSource, extract_jsonld_events

log = logging.getLogger(__name__)


class HtmlAiSource(Source):
    """Any HTML page listing events: text is extracted and Gemini turns it into events.

    Gemini is asked again only when the page text changed AND the page's refresh window has
    passed (or the daily AI budget allows it). Otherwise the previous answer is reused, so a
    page never drops out of the calendar because the budget ran out.

    Options:
      pages: extra URLs besides `url` (e.g. page 2 of the listing, next month)
      selector: CSS selector narrowing the page to the event list (cuts tokens a lot)
      max_chars: per-page text cap (default 60000)
      refresh_hours: minimum time between AI reads of a changed page (default 20; media 72)
      insecure_tls: true for sites with a broken certificate chain (public pages only)
    """

    type = "html_ai"

    def fetch(self):
        ai = gemini(self.settings)
        opts = self.cfg.options
        refresh = float(opts.get("refresh_hours", 20)) * 3600
        for page in [self.cfg.url, *opts.get("pages", [])]:
            html = http.get(page, insecure=bool(opts.get("insecure_tls"))).text
            if extract_jsonld_events(html):
                yield from JsonLdSource(self.cfg, self.settings)._events(html, page)
                continue
            text = html_to_text(html, page, opts.get("selector"))[: int(opts.get("max_chars", 60000))]
            if len(text) < 80:
                log.info("%s: page %s has almost no text (JS-rendered?)", self.cfg.id, page)
                continue

            def key(version: str) -> str:
                return hashlib.sha256(f"{version}|{self.cfg.id}|{page}".encode()).hexdigest()[:24]

            page_key = key(PROMPT_VERSION)
            text_hash = hashlib.sha256(text.encode()).hexdigest()[:24]
            # The answer under an earlier prompt counts as this page's last one: reused while the page is
            # unchanged, re-read under the new prompt (when allowed) once it changes.
            state = next((st for v in (PROMPT_VERSION, *LEGACY_PROMPT_VERSIONS) if (st := ai.page_state(key(v)))), None)
            previous = ai.load(state["result"]) if state else None

            if state and state["text"] == text_hash and previous is not None:
                items = previous  # unchanged page: free
                ai.cache_hits += 1
            elif state and previous is not None and (time.time() - state["ts"] < refresh or not ai.budget_left()):
                items = previous  # changed, but too soon to re-read or out of budget: reuse
                ai.stale_reuses += 1
            elif ai.budget_left():
                items = []
                complete = True
                for chunk in chunks(text):
                    got = ai.extract_events(text=chunk, source=self.cfg.name, url=page,
                                            default_location=self.default_location_text())
                    complete = complete and ai.last_ok
                    items += got
                if complete:
                    ai.remember_page(page_key, text_hash, items)
                elif previous is not None:
                    items = previous + items
            else:
                log.info("%s: no AI budget and no previous answer for %s", self.cfg.id, page)
                continue

            for item in items:
                ev = self.make_event(
                    title=item.get("title"),
                    start=item.get("start"),
                    end=item.get("end"),
                    url=absolutize(item.get("url"), page) or page,
                    description=item.get("description"),
                    location_name=item.get("location_name"),
                    address=item.get("address"),
                    town=item.get("town"),
                    price=item.get("price"),
                    categories=[item.get("category")] if item.get("category") else None,
                )
                if ev:
                    yield ev
