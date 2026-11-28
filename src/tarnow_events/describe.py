"""Better descriptions, written by Gemini from the sources' own texts.

After dedup an event keeps every source's own text (Event.source_texts). Two kinds of event get one:
- several sources say something different: one description from all of them;
- one source's text is longer than the page shows (pipeline.DESC_CHARS): shortened to what matters,
  instead of being cut mid-text. The page still links "Czytaj dalej" to the main source (Event.desc_more).
Plain Polish prose, no emoji (the prompt asks; clean() makes sure) and no dashes as punctuation (clean()
replaces them). Sources with `ai_description: false` (sources.yaml) are left out: their texts are never
sent, and an event whose main source is one of them keeps its text as it is.

Events go ~10 to a request, soonest first, within their own daily allowance (TE_AI_DESC_DAILY_LIMIT), so
the sources' calls are never spent on this. Answers are cached by the texts they were written from
(cache/ai/descriptions.json): an event is described once and again only when a source changes its text.
When there is no answer (no key, allowance used up, quota or network error) the event keeps the
description it already has, which is the main source's, cut as before.
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
TEXT_MAX = 6000  # characters of one source's text sent along (a long one is sent whole to be shortened)
MIN_LEN = 40  # shorter answers are not descriptions
KEEP_DAYS = 60  # cache entries not used for this long are dropped

# Pictographs, dingbats, flags, keycaps and the joiners that glue them into one emoji.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002300-\U000023FF"
                    "\U0001F1E6-\U0001F1FF\U000020E3\U0000FE0E\U0000FE0F\U0000200D]")
_URL = re.compile(r"\(?https?://\S+\)?|\bwww\.\S+")


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".…! ")


def distinct_texts(e: Event, skip: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """(source name, text) of the sources' texts, without repeats: a text that only repeats (the start of)
    another is dropped, so a portal's lead next to the organiser's full article does not count as a second
    view. skip: ids of sources whose texts must not go to the AI."""
    texts = [(name, " ".join(text.split())[:TEXT_MAX]) for sid, name, text in e.source_texts
             if text and text.strip() and sid not in skip]
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


def improve_descriptions(events: list[Event], ai: Gemini, now: datetime,
                         no_ai: set[str] | frozenset[str] = frozenset(), new: set[str] | None = None) -> dict:
    """Give every upcoming event with 2+ distinct source texts, or with one text too long for the page, the
    AI description, from the cache or a new request. no_ai: ids of sources kept away from the AI.
    new: uids of events first seen in this build. Only those get a long text shortened; events published
    before keep their text (cut on the page, with "Czytaj dalej"). None: every event counts as new.
    A new event that cannot be shortened in its first build (allowance used up) waits in
    cache/ai/shorten_pending.json until it is. Returns counts for status.json."""
    from .pipeline import DESC_CHARS  # pipeline imports this module

    path, pending_path = ai.cache_dir / "descriptions.json", ai.cache_dir / "shorten_pending.json"
    cache: dict[str, dict] = _load(path)
    pending: dict[str, float] = _load(pending_path)  # uid -> first seen
    stamp = time.time()

    todo: list[tuple[str, Event, list[tuple[str, str]]]] = []
    applied = kept_long = 0
    for e in sorted(events, key=lambda e: e.start):
        if not e.in_main or (e.end or e.start) < now or e.all_sources()[0].source_id in no_ai:
            continue
        texts = distinct_texts(e, no_ai)
        shorten = len(texts) == 1 and len(texts[0][1]) > DESC_CHARS
        if not (len(texts) >= 2 or shorten):
            continue
        key = _key(e, texts)
        if key in cache:
            e.description = cache[key]["d"]
            cache[key]["t"] = stamp
            applied += 1
        elif shorten and new is not None and e.uid not in new and e.uid not in pending:
            kept_long += 1  # published before: its text stays as it was
        else:
            todo.append((key, e, texts))

    written: set[str] = set()
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
                written.add(key)
    if todo:
        log.info("AI descriptions: %d new, %d waiting for the next run", len(written), len(todo) - len(written))

    # Still waiting to be shortened: carried to the next build, however long that takes.
    pending = {e.uid: pending.get(e.uid, stamp) for key, e, texts in todo if key not in written and len(texts) == 1}
    cache = {k: v for k, v in cache.items() if stamp - v["t"] < KEEP_DAYS * 86400}
    path.write_text(json.dumps(cache, ensure_ascii=False))
    pending_path.write_text(json.dumps(pending))
    return {"descriptions_ai": applied + len(written), "descriptions_new": len(written),
            "descriptions_waiting": len(todo) - len(written), "descriptions_long_kept": kept_long}


def _load(path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
