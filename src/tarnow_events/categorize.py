from __future__ import annotations

import re

from unidecode import unidecode

from .models import CATEGORIES, Event

# Checked in order; an event can land in several (a children's concert is muzyka + dzieci).
# Patterns match word *prefixes* on the diacritic-stripped, lowercased title + description.
RULES: list[tuple[str, list[str]]] = [
    ("kino", ["kino", "seans", "film", "projekcj", "dkf", "pokaz filmu", "premiera filmu"]),
    ("sport", ["mecz", "turniej", "bieg ", "biegi", "maraton", "polmaraton", "zawody", "zuzel", "speedway",
               "liga", "rajd", "parkrun", "siatkow", "pilka", "pilkars", "koszykow", "triathlon", "nordic"]),
    ("wystawy", ["wystaw", "wernisaz", "ekspozycj", "finisaz"]),
    ("teatr", ["spektakl", "teatr", "kabaret", "stand-up", "standup", "stand up", "monodram", "musical",
               "operetk", "balet", "przedstawieni"]),
    ("koncerty", ["koncert", "recital", "jazz", "rock", "orkiestr", "filharmon", "chor ", "chorow", "opera",
                "symfoni", "kwartet", "trio ", "big band", "unplugged", "hip-hop", "hip hop", "metal", "blues"]),
    ("dzieci", ["dla dzieci", "dzieci", "rodzin", "bajk", "mikolaj", "ferie", "przedszkol", "malucha",
                "maluchow", "familijn"]),
    ("festiwale", ["festiwal", "jarmark", "piknik", "dni miasta", "dni gminy", "kiermasz", "targi", "festyn",
                   "swieto ", "dozynk", "fiesta", "parada", "jarmark"]),
    ("imprezy", ["impreza", "party", "taneczn", "quiz", "karaoke", " dj", "disco", "potancowk", "andrzejk", "sylwest",
                 "open mic", "dancing", "koncert zyczen"]),
    ("spotkania", ["warsztat", "wyklad", "spacer", "zwiedzani", "oprowadzani", "spotkanie autorskie", "prelekcj", "szkoleni", "konferencj", "kurs ",
                  "webinar", "spotkanie", "debat", "seminari", "dyskusyjny klub", "promocja ksiazki"]),
    ("uroczystosci", ["msza", "nabozenstw", "uroczystosc", "rocznic", "patriotyczn", "apel pamieci", "swieto niepodleglosci", "rekolekcj", "pielgrzymk", "odpust", "modlitw", "rozaniec"]),
]

_COMPILED = [(cat, re.compile("|".join(r"(?:^|\W)" + re.escape(k.strip()) for k in kws))) for cat, kws in RULES]


def detect(text: str) -> list[str]:
    t = " " + unidecode(text).lower() + " "
    return [cat for cat, rx in _COMPILED if rx.search(t)]


# Older cache entries and source category slugs → our calendar slugs.
ALIASES = {
    "muzyka": "koncerty", "koncert": "koncerty", "film": "kino", "edukacja": "spotkania", "religia": "uroczystosci",
    # The Events Calendar slugs used by it.tarnow.pl / kultura.tarnow.pl
    "wystawy_wernisaze": "wystawy", "spotkania": "spotkania", "warsztaty": "spotkania",
    "spacery-i-zwiedzanie": "spotkania", "kiermasze-i-jarmarki": "festiwale", "festiwale": "festiwale",
    "imprezy_plenerowe": "festiwale", "teatr_wybrane_spektakle": "teatr", "kabarety_stand_upy": "teatr",
    "kino_wybrane_projekcje": "kino", "uroczystosci-patriotyczne": "uroczystosci", "imprezy-taneczne": "imprezy",
    "sport": "sport", "dla-dzieci": "dzieci", "dzieci": "dzieci",
}


def normalize(cat: str | None) -> str | None:
    if not cat:
        return None
    cat = ALIASES.get(cat, cat)
    return cat if cat in CATEGORIES else None


def categorize(ev: Event, source_default: str, from_source: bool) -> None:
    """Title wins over description: 'Koncert dla dzieci' is muzyka first, dzieci second."""
    title_cats = detect(ev.title)
    desc_cats = detect((ev.description or "")[:400]) if not title_cats else []
    cats: list[str] = []
    if from_source:  # the source (or the AI) already classified it
        cats += [n for c in ev.categories if (n := normalize(c)) and n != "inne"]
    cats += title_cats or desc_cats[:1]
    if source_default and source_default != "inne":
        cats.append(source_default)
    cats.append("inne")
    seen: list[str] = []
    for c in cats:
        if c not in seen:
            seen.append(c)
    # Keep "inne" only when nothing else matched.
    ev.categories = [c for c in seen if c != "inne"] or ["inne"]
