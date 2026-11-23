from __future__ import annotations

import logging

from .. import http
from ..ai import gemini
from ..htmltext import chunks, html_to_text
from .base import Source, absolutize
from .jsonld import JsonLdSource, extract_jsonld_events

log = logging.getLogger(__name__)


class HtmlAiSource(Source):
    """Any HTML page listing events: text is extracted and Gemini turns it into events.

    The catch-all for municipal sites, cultural centres and venues with no feed. If a page
    turns out to carry JSON-LD events, those are used instead and no AI call is made.

    Options:
      pages: extra URLs besides `url` (e.g. page 2 of the listing, next month)
      selector: CSS selector narrowing the page to the event list (cuts tokens a lot)
      max_chars: per-page text cap (default 60000)
    """

    type = "html_ai"

    def fetch(self):
        ai = gemini(self.settings)
        opts = self.cfg.options
        for page in [self.cfg.url, *opts.get("pages", [])]:
            html = http.get(page).text
            if extract_jsonld_events(html):
                yield from JsonLdSource(self.cfg, self.settings)._events(html, page)
                continue
            if not ai.enabled:
                log.warning("%s: GEMINI_API_KEY not set, skipping AI extraction", self.cfg.id)
                return
            text = html_to_text(html, page, opts.get("selector"))[: int(opts.get("max_chars", 60000))]
            if len(text) < 80:
                log.info("%s: page %s has almost no text (JS-rendered?)", self.cfg.id, page)
                continue
            for chunk in chunks(text):
                for item in ai.extract_events(
                    text=chunk, source=self.cfg.name, url=page, default_location=self.default_location_text()
                ):
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
