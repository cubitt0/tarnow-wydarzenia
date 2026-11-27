"""One better description for an event that several sources report.

After dedup an event keeps every source's own text (Event.source_texts). When two or more of them
say something different, Gemini writes one description from all of them: plain Polish prose, no
emoji (the prompt asks; clean() makes sure) and no dashes as punctuation (clean() replaces them). Events go ~10 to a request,
soonest first, within their own daily allowance (TE_AI_DESC_DAILY_LIMIT), so the sources' calls are
never spent on this.

Answers are cached by the texts they were written from (cache/ai/descriptions.json): an event is
described once and again only when a source changes its text. When there is no answer (no key,
allowance used up, quota or network error) the event keeps the description it already has, which is
the main source's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime

from .ai import DESC_VERSION, Gemini
from .models import Event

log = logging.getLogger(__name__)

BATCH = 10  # events per request
TEXT_MAX = 2500  # characters of one source's text sent along
MIN_LEN = 40  # shorter answers are not descriptions
KEEP_DAYS = 60  # cache entries not used for this long are dropped

# Pictographs, dingbats, flags, keycaps and the joiners that glue them into one emoji.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002300-\U000023FF"
                    "\U0001F1E6-\U0001F1FF\U000020E3\U0000FE0E\U0000FE0F\U0000200D]")
_URL = re.compile(r"\(?https?://\S+\)?|\bwww\.\S+")


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".…! ")


def distinct_texts(e: Event) -> list[tuple[str, str]]:
    """The sources' texts, without repeats: a text that only repeats (the start of) another is dropped,
    so a portal's lead next to the organiser's full article does not count as a second view."""
    texts = [(name, " ".join(text.split())[:TEXT_MAX]) for name, text in e.source_texts if text and text.strip()]
    keep: list[int] = []
    for i, (_, t) in sorted(enumerate(texts), key=lambda x: -len(x[1][1])):
        if not any(_norm(t) in _norm(texts[j][1]) for j in keep):
            keep.append(i)
    return [texts[i] for i in sorted(keep)]


def clean(text: str) -> str | None:
    """The model's answer as published: no emoji, no dashes as punctuation, no links or Markdown.
    None when nothing usable is left."""
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"\s*[—―]\s*", ", ", t)  # em dash
    t = re.sub(r"\s+–\s+", ", ", t)  # en dash used as a dash; "18:00–20:00" stays
    t = _URL.sub("", t)
    t = re.sub(r"\*\*|__|^#+\s*|^\s*[-*•]\s+", "", t, flags=re.M)
    t = "\n\n".join(" ".join(p.split()) for p in re.split(r"\n\s*\n", t))
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r",(\s*[,.;:])+", lambda m: m.group(0)[-1], t)  # ", ," and ", ." left by removed dashes
    t = t.strip(" ,;:\n")
    return t if len(t) >= MIN_LEN else None


def _key(e: Event, texts: list[tuple[str, str]]) -> str:
    raw = json.dumps([DESC_VERSION, e.title, texts], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def improve_descriptions(events: list[Event], ai: Gemini, now: datetime) -> dict:
    """Give every upcoming event with 2+ distinct source texts the AI description, from the cache or
    a new request. Returns counts for status.json."""
    path = ai.cache_dir / "descriptions.json"
    try:
        cache: dict[str, dict] = json.loads(path.read_text())
    except (OSError, ValueError):
        cache = {}
    stamp = time.time()

    todo: list[tuple[str, Event, list[tuple[str, str]]]] = []
    applied = 0
    for e in sorted(events, key=lambda e: e.start):
        if not e.in_main or (e.end or e.start) < now:
            continue
        texts = distinct_texts(e)
        if len(texts) < 2:
            continue
        key = _key(e, texts)
        if key in cache:
            e.description = cache[key]["d"]
            cache[key]["t"] = stamp
            applied += 1
        else:
            todo.append((key, e, texts))

    written = 0
    for start in range(0, len(todo) if ai.enabled else 0, BATCH):
        batch = todo[start:start + BATCH]
        answer = ai.describe([{"id": str(i), "title": e.title, "texts": texts} for i, (_, e, texts) in enumerate(batch)])
        if answer is None:  # blocked: these and the rest keep the main source's description
            break
        for i, (key, e, _) in enumerate(batch):
            text = clean(answer.get(str(i), ""))
            if text:
                cache[key] = {"d": text, "t": stamp}
                e.description = text
                written += 1
    if todo:
        log.info("AI descriptions: %d new, %d waiting for the next run", written, len(todo) - written)

    cache = {k: v for k, v in cache.items() if stamp - v["t"] < KEEP_DAYS * 86400}
    path.write_text(json.dumps(cache, ensure_ascii=False))
    return {"descriptions_ai": applied + written, "descriptions_new": written, "descriptions_waiting": len(todo) - written}
