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

# One description written from what several sources say about the same event (describe.py).
DESC_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "description": {"type": "string"}},
                "required": ["id", "description"],
            },
        }
    },
    "required": ["items"],
}

DESC_PROMPT = """Piszesz opisy wydarzeń do publicznego kalendarza "Wydarzenia Tarnów".
Każde wydarzenie poniżej opisuje kilka stron. Dla każdego napisz JEDEN opis po polsku na podstawie ich tekstów.

Zasady:
- Tylko fakty z podanych tekstów. Niczego nie dopisuj, nie zgaduj i nie oceniaj. Nie uzupełniaj imion, nazw
  ani faktów z własnej wiedzy (jeśli tekst podaje samo nazwisko, zostaw samo nazwisko).
- Połącz to, co najciekawsze ze wszystkich źródeł: o czym jest wydarzenie, kto występuje, program, dla kogo,
  zapisy, co zabrać. Bez powtórzeń.
- Pomiń datę, godzinę, adres i cenę, chyba że teksty podają coś więcej (kilka terminów, harmonogram, ulgi).
- Jeśli źródła sobie przeczą, pomiń tę informację.
- Długość zależna od tego, ile mówią źródła: od 2 zdań do 2 lub 3 akapitów, najwyżej 1200 znaków.
  Akapity oddziel pustą linią. Zwykły tekst: bez Markdown, nagłówków, list i linków.
- Bez emoji i innych symboli graficznych.
- Bez zwrotów reklamowych typu "nie przegap", "serdecznie zapraszamy", "wyjątkowe wydarzenie".
- Język rzeczowy i naturalny, w trzeciej osobie.
- Pole id przepisz dokładnie z nagłówka wydarzenia.

Wydarzenia:
"""

DESC_VERSION = hashlib.sha256((DESC_PROMPT + json.dumps(DESC_SCHEMA, sort_keys=True)).encode()).hexdigest()[:10]


class Gemini:
    def __init__(self, settings: Settings):
        self.key = settings.gemini_api_key
        self.models = [m.strip() for m in settings.gemini_models if m.strip()]
        self.max_calls = settings.ai_max_calls
        self.cache_dir = settings.cache_dir / "ai"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.cache_hits = 0
        self.stale_reuses = 0
        self.last_ok = True  # did the last extract_events call get a real (or cached) answer?
        # Daily cap across all runs of the day (pushes, manual runs), persisted next to the cache.
        self.daily_limit = settings.ai_daily_limit
        # Descriptions (describe.py) have their own, smaller allowance: they must not take the sources' calls.
        self.desc_daily_limit = settings.ai_desc_daily_limit
        self.desc_calls = 0
        self._usage_path = self.cache_dir / "usage.json"
        self._pages_path = self.cache_dir / "pages.json"
        try:
            self._pages: dict[str, dict] = json.loads(self._pages_path.read_text())
        except (OSError, ValueError):
            self._pages = {}
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._exhausted: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    # ---- budget -------------------------------------------------------------------------

    def _usage(self) -> dict:
        try:
            u = json.loads(self._usage_path.read_text())
        except (OSError, ValueError):
            u = {}
        return u if u.get("date") == date.today().isoformat() else {"date": date.today().isoformat(), "calls": 0}

    def calls_today(self) -> int:
        return self._usage()["calls"]

    def budget_left(self) -> bool:
        return self.enabled and self.calls < self.max_calls and self.calls_today() < self.daily_limit

    def desc_budget_left(self) -> bool:
        return self.enabled and self._usage().get("desc", 0) < self.desc_daily_limit

    def _count_call(self, kind: str = "calls") -> None:
        u = self._usage()
        u[kind] = u.get(kind, 0) + 1
        self._usage_path.write_text(json.dumps(u))

    # ---- per-page memory: what the AI last said about a page, and when -------------------

    def page_state(self, page_key: str) -> dict | None:
        return self._pages.get(page_key)

    def remember_page(self, page_key: str, text_hash: str, events: list[dict]) -> None:
        result_key = f"page-{page_key}-{text_hash}"
        self.save(result_key, events)
        self._pages[page_key] = {"ts": time.time(), "text": text_hash, "result": result_key}
        self._pages_path.write_text(json.dumps(self._pages))

    def save(self, key: str, events: list[dict]) -> None:
        (self.cache_dir / f"{key}.json").write_text(json.dumps(events, ensure_ascii=False))

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
            self.last_ok = True
            return cached

        result = self._generate(prompt)
        self.last_ok = result is not None
        if result is None:
            return []
        events = [e for e in result.get("events", []) if e.get("public_event", True)]
        (self.cache_dir / f"{key}.json").write_text(json.dumps(events, ensure_ascii=False))
        return events

    def load(self, key: str) -> list[dict] | None:
        path = self.cache_dir / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def extract_events_pdf(self, *, pdf: bytes, source: str, url: str, default_location: str) -> list[dict]:
        """Annual event calendars published as PDF: Gemini reads the PDF directly. Cached by file hash."""
        key = "pdf-" + PROMPT_VERSION + "-" + hashlib.sha256(pdf).hexdigest()
        cached = self.load(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if not self.budget_left():
            return []
        prompt = PROMPT.format(today=date.today().isoformat(), source=source, url=url,
                               default_location=default_location or "brak", text="(treść w załączonym pliku PDF)")
        import base64
        result = self._generate(prompt, extra_parts=[{"inline_data": {"mime_type": "application/pdf",
                                                                      "data": base64.b64encode(pdf).decode()}}])
        if result is None:
            return []
        events = [e for e in result.get("events", []) if e.get("public_event", True)]
        self.save(key, events)
        return events

    def describe(self, items: list[dict]) -> dict[str, str] | None:
        """items: {id, title, texts: [(source name, text)]}. Returns id -> description, or None when
        no answer came (no key, budget used up, quota or network error): the caller keeps what it has."""
        blocks = []
        for it in items:
            texts = "\n\n".join(f"[{name}]\n{text}" for name, text in it["texts"])
            blocks.append(f"### id: {it['id']}\nTytuł: {it['title']}\n{texts}")
        result = self._generate(DESC_PROMPT + "\n\n".join(blocks), schema=DESC_SCHEMA, desc=True)
        if result is None:
            return None
        return {str(r.get("id", "")).strip(): r.get("description") or "" for r in result.get("items", [])}

    def _generate(self, prompt: str, extra_parts: list[dict] | None = None, *, schema: dict = EVENT_SCHEMA,
                  desc: bool = False) -> dict | None:
        """desc: a describe() call, counted against the descriptions' own allowance."""
        if not self.key:
            return None
        budget_left = self.desc_budget_left if desc else self.budget_left
        with self._lock:  # serialise: the free tier is ~15 requests/minute per project
            if not budget_left():
                if desc:
                    log.info("AI description budget used up (%d/day); sources' own descriptions stay",
                             self.desc_daily_limit)
                else:
                    log.warning("AI budget exhausted (run %d/%d, today %d/%d); reusing previous answers",
                                self.calls, self.max_calls, self.calls_today(), self.daily_limit)
                return None
            for attempt, model in ((a, m) for m in self.models for a in range(3)):
                if model in self._exhausted:
                    continue
                if not budget_left():  # retries count too
                    return None
                wait = 4.5 - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
                if desc:
                    self.desc_calls += 1
                else:
                    self.calls += 1
                self._count_call("desc" if desc else "calls")
                try:
                    resp = httpx.post(
                        BASE.format(model=model),
                        # Header, not ?key=: URLs end up in exception messages and proxy logs.
                        headers={"x-goog-api-key": self.key},
                        json={
                            "contents": [{"parts": [{"text": prompt}, *(extra_parts or [])]}],
                            "generationConfig": {
                                "temperature": 0,
                                "responseMimeType": "application/json",
                                "responseSchema": schema,
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

