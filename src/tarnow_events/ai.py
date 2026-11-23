"""Gemini-based extraction of events from unstructured text (HTML pages, posts, articles).

Every call is cached on disk by a hash of (prompt, input), so a page that did not change since
yesterday costs nothing. That is what keeps the daily run inside the free tier.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date

import httpx

from .config import Settings

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601 local time, YYYY-MM-DDTHH:MM or YYYY-MM-DD if no time"},
                    "end": {"type": "string", "nullable": True},
                    "location_name": {"type": "string", "nullable": True},
                    "address": {"type": "string", "nullable": True},
                    "town": {"type": "string", "nullable": True},
                    "url": {"type": "string", "nullable": True, "description": "link to the event's own page if present in the text"},
                    "description": {"type": "string", "nullable": True},
                    "price": {"type": "string", "nullable": True},
                    "public_event": {"type": "boolean", "description": "true only for events open to the public that someone can attend"},
                    "category": {
                        "type": "string",
                        "enum": ["koncerty", "teatr", "kino", "wystawy", "sport", "dzieci", "spotkania",
                                 "imprezy", "festiwale", "uroczystosci", "inne"],
                    },
                },
                "required": ["title", "start", "category", "public_event"],
            },
        }
    },
    "required": ["events"],
}

PROMPT = """Jesteś parserem wydarzeń dla publicznego kalendarza "co się dzieje w Tarnowie i okolicy".
Dzisiaj jest {today}. Z poniższego tekstu strony "{source}" ({url}) wypisz wydarzenia, na które
mieszkaniec może PRZYJŚĆ: koncerty, spektakle, kabarety, seanse, wystawy, mecze i zawody, biegi, warsztaty,
wykłady, spotkania autorskie, festiwale, jarmarki, pikniki, imprezy, potańcówki, quizy, zwiedzanie z przewodnikiem,
uroczystości otwarte dla publiczności.

NIE są wydarzeniami (pomiń je albo ustaw public_event=false):
- wypadki, kolizje, przestępstwa, rozprawy sądowe, interwencje służb, pogoda,
- wyłączenia prądu/wody, utrudnienia drogowe, remonty, przetargi, nabory, rekrutacje, konkursy z terminem zgłoszeń,
- sesje rad, komisje, posiedzenia urzędowe, dyżury, konsultacje i spotkania dla wąskich grup (np. tylko dla szkół, pracowników),
- promocje sklepów, wyprzedaże, black week, godziny otwarcia, oferty stałe,
- relacje z wydarzeń, które już się odbyły (przed {today}).

Zasady:
- Data wydarzenia musi wynikać z treści. Data publikacji artykułu NIE jest datą wydarzenia.
- Jeśli rok nie jest podany, przyjmij najbliższą przyszłą datę.
- Wydarzenie wielodniowe: start = pierwszy dzień, end = ostatni dzień.
- Godziny w czasie lokalnym (Polska), bez strefy. Brak godziny = sama data YYYY-MM-DD.
- url: link do strony tego wydarzenia, jeśli występuje w tekście (linki są w formacie [tekst](adres)).
- description: 1-2 zdania po polsku, tylko fakty z tekstu.
- town: miejscowość, w której wydarzenie się odbywa. Domyślne miejsce, jeśli tekst nie podaje innego: {default_location}.
Tekst:
---
{text}
---"""


# Changes whenever the prompt or schema changes, so cached answers made under old rules are not reused.
PROMPT_VERSION = hashlib.sha256((PROMPT + json.dumps(EVENT_SCHEMA, sort_keys=True)).encode()).hexdigest()[:10]


class Gemini:
    def __init__(self, settings: Settings):
        self.key = settings.gemini_api_key
        self.models = [m.strip() for m in settings.gemini_models if m.strip()]
        self.max_calls = settings.ai_max_calls
        self.cache_dir = settings.cache_dir / "ai"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._exhausted: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def extract_events(
        self, *, text: str, source: str, url: str, default_location: str, key: str | None = None
    ) -> list[dict]:
        prompt = PROMPT.format(
            today=date.today().isoformat(), source=source, url=url,
            default_location=default_location or "brak", text=text,
        )
        # The cache key leaves out today's date so an unchanged page is not re-sent daily;
        # the date filter downstream drops anything that has since passed.
        key = key or hashlib.sha256((PROMPT + source + url + default_location + text).encode()).hexdigest()
        cached = self.load(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        result = self._generate(prompt)
        if result is None:
            return []
        events = [e for e in result.get("events", []) if e.get("public_event", True)]
        (self.cache_dir / f"{key}.json").write_text(json.dumps(events, ensure_ascii=False))
        return events

    def load(self, key: str) -> list[dict] | None:
        path = self.cache_dir / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _generate(self, prompt: str) -> dict | None:
        if not self.key:
            return None
        with self._lock:  # serialise: the free tier is ~15 requests/minute per project
            if self.calls >= self.max_calls:
                log.warning("AI call budget (%d) exhausted for this run", self.max_calls)
                return None
            for attempt, model in ((a, m) for m in self.models for a in range(3)):
                if model in self._exhausted:
                    continue
                wait = 4.5 - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
                self.calls += 1
                try:
                    resp = httpx.post(
                        BASE.format(model=model),
                        # Header, not ?key=: URLs end up in exception messages and proxy logs.
                        headers={"x-goog-api-key": self.key},
                        json={
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {
                                "temperature": 0,
                                "responseMimeType": "application/json",
                                "responseSchema": EVENT_SCHEMA,
                            },
                        },
                        timeout=120,
                    )
                except httpx.HTTPError as e:
                    log.warning("Gemini %s transport error: %s", model, e)
                    continue
                if resp.status_code in (429, 404):
                    log.info("Gemini %s unavailable (%s), next model", model, resp.status_code)
                    self._exhausted.add(model)
                    continue
                if resp.status_code in (500, 503) and attempt < 2:
                    # "Model overloaded" spikes pass within seconds; retry the same model.
                    time.sleep(10 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    log.warning("Gemini %s HTTP %s: %s", model, resp.status_code, resp.text[:300])
                    continue
                try:
                    parts = resp.json()["candidates"][0]["content"]["parts"]
                    # Thinking models may prepend thought parts; the answer is the non-thought text.
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    return json.loads(text)
                except (KeyError, IndexError, ValueError) as e:
                    log.warning("Gemini %s unparseable response: %s", model, e)
                    continue
            return None


_instance: Gemini | None = None


def gemini(settings: Settings) -> Gemini:
    global _instance
    if _instance is None:
        _instance = Gemini(settings)
    return _instance

