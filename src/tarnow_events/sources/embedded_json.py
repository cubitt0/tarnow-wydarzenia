from __future__ import annotations

import json
import re

from .. import http
from .base import Source


class EmbeddedJsonSource(Source):
    """Pages that inline their schedule as JSON objects in a <script> (e.g. the WordPress
    "Weekly Class Schedule" plugin used by Teatr Solskiego).

    Every JSON object in the page that has the configured title/start keys is an event.

    Options:
      pages: extra URLs
      title_key (default "title"), start_key ("start"), end_key ("end"), url_key ("permalink"),
      description_key ("excerpt"), location_key (none)
      naive_local: true → ignore the timezone in start/end (plugins that stamp local time "+00:00")
      anchor: regex marking where candidate objects start (default '\\{"title":')
    """

    type = "embedded_json"

    def fetch(self):
        o = self.cfg.options
        keys = {
            "title": o.get("title_key", "title"),
            "start": o.get("start_key", "start"),
            "end": o.get("end_key", "end"),
            "url": o.get("url_key", "permalink"),
            "description": o.get("description_key", "excerpt"),
            "location": o.get("location_key"),
        }
        anchor = re.compile(o.get("anchor", r'\{"title":'))
        decoder = json.JSONDecoder()
        seen: set[tuple] = set()
        for page in [self.cfg.url, *o.get("pages", [])]:
            html = http.get(page).text
            for m in anchor.finditer(html):
                try:
                    obj, _ = decoder.raw_decode(html, m.start())
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not obj.get(keys["start"]) or not obj.get(keys["title"]):
                    continue
                start, end = obj.get(keys["start"]), obj.get(keys["end"])
                if o.get("naive_local"):
                    start = _strip_tz(start)
                    end = _strip_tz(end)
                ident = (obj.get(keys["title"]), start)
                if ident in seen:
                    continue
                seen.add(ident)
                ev = self.make_event(
                    title=obj.get(keys["title"]),
                    start=start,
                    end=end,
                    url=obj.get(keys["url"]) or page,
                    description=obj.get(keys["description"]),
                    location_name=obj.get(keys["location"]) if keys["location"] else None,
                )
                if ev:
                    yield ev


def _strip_tz(v):
    if isinstance(v, str):
        return re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", v)
    return v
