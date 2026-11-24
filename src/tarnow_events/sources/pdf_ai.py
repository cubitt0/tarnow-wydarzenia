from __future__ import annotations

import logging
import re

from .. import http
from ..ai import gemini
from .base import Source, absolutize

log = logging.getLogger(__name__)


class PdfAiSource(Source):
    """Annual event calendars published as PDF ("kalendarz imprez 2026"). Gemini reads the
    PDF itself; the answer is cached by the file's hash, so it costs one AI call per new file.

    `url` is either the PDF or a page linking to it. Options:
      link: regex picking the PDF link on the page (default: first link ending in .pdf)
      insecure_tls: true for sites with a broken certificate chain
    """

    type = "pdf_ai"

    def fetch(self):
        ai = gemini(self.settings)  # without a key only a cached answer can be used
        insecure = bool(self.cfg.options.get("insecure_tls"))
        resp = http.get(self.cfg.url, insecure=insecure)
        pdf_url = self.cfg.url
        if "pdf" not in resp.headers.get("content-type", ""):
            rx = re.compile(self.cfg.options.get("link", r"\.pdf$"), re.I)
            links = [absolutize(h, self.cfg.url) for h in re.findall(r'href="([^"]+)"', resp.text)]
            pdf_url = next((u for u in links if u and rx.search(u)), None)
            if not pdf_url:
                log.info("%s: no PDF link found on %s", self.cfg.id, self.cfg.url)
                return
            resp = http.get(pdf_url, insecure=insecure)
        for item in ai.extract_events_pdf(pdf=resp.content, source=self.cfg.name, url=pdf_url,
                                          default_location=self.default_location_text()):
            ev = self.make_event(
                title=item.get("title"),
                start=item.get("start"),
                end=item.get("end"),
                url=pdf_url,
                description=item.get("description"),
                location_name=item.get("location_name"),
                address=item.get("address"),
                town=item.get("town"),
                price=item.get("price"),
                categories=[item.get("category")] if item.get("category") else None,
            )
            if ev:
                yield ev
