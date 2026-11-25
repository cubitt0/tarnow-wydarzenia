from __future__ import annotations

import re

from unidecode import unidecode

from .models import CATEGORIES, Event

# Checked in order; an event can land in several (a children's concert is muzyka + dzieci).
# Patterns match word *prefixes* on the diacritic-stripped, lowercased title + description;
# a trailing space means the word must end there ("rock " is rock, not "Rocky").
RULES: list[tuple[str, list[str]]] = [
    ("kino", ["kino", "seans", "film", "projekcj", "dkf", "pokaz filmu", "premiera filmu", "cinemarzenie", "pokaz specjalny"]),
    ("sport", ["mecz", "turniej", "bieg ", "biegi", "biegu ", "biegow", "biegacz", "maraton", "polmaraton", "zawody", "zuzel", "speedway", "handball", "superliga", "pilka reczna", "futsal", "liga mezczyzn", "liga kobiet",
               "liga", "rajd", "parkrun", "siatkow", "pilka", "pilkars", "koszykow", "triathlon", "nordic"]),
    ("wystawy", ["wystaw", "wernisaz", "ekspozycj", "finisaz"]),
    ("teatr", ["spektakl", "teatr", "kabaret", "stand-up", "standup", "stand up", "monodram", "musical",
               "operetk", "balet", "przedstawieni"]),
    ("koncerty", ["koncert", "recital", "jazz", "rock ", "rockow", "orkiestr", "filharmon", "chor ", "choru ",
                "chorow", "opera ", "opery ", "operow", "symfoni", "kwartet", "trio ", "big band", "unplugged",
                "hip-hop", "hip hop", "metal ", "metalow", "blues", "tribute", "punk ", "punkow", "muzyka z film", "muzyki z film", "muzyka filmow", "muzyki filmow", "zespol", "orkiestra", "koled", "piosenk", "muzyczn", "gala operow"]),
    ("dzieci", ["dla dzieci", "dzieci", "rodzin", "bajk", "mikolaj", "ferie", "przedszkol", "malucha",
                "maluchow", "familijn"]),
    ("festiwale", ["festiwal", "jarmark", "piknik", "dni miasta", "dni gminy", "kiermasz", "targi", "festyn",
                   "swieto ", "dozynk", "fiesta", "parada", "jarmark"]),
    ("imprezy", ["impreza", "party", "taneczn", "quiz", "karaoke", " dj", "disco", "potancowk", "andrzejk", "sylwest",
                 "open mic", "dancing", "koncert zyczen"]),
    ("spotkania", ["warsztat", "wyklad", "spacer", "zwiedzani", "oprowadzani", "spotkanie autorskie", "prelekcj", "szkoleni", "konferencj", "kurs ", "kursy ", "kursu ",
                  "webinar", "spotkanie", "debat", "seminari", "dyskusyjny klub", "promocja ksiazki"]),
    ("uroczystosci", ["msza", "nabozenstw", "uroczystosc", "rocznic", "patriotyczn", "apel pamieci", "swieto niepodleglosci", "rekolekcj", "pielgrzymk", "odpust", "modlitw", "rozaniec"]),
]



def _kw(k: str) -> str:
    return r"(?:^|\W)" + re.escape(k.strip()) + (r"(?!\w)" if k.endswith(" ") else "")


_COMPILED = [(cat, re.compile("|".join(_kw(k) for k in kws))) for cat, kws in RULES]

# Pairs that keywords alone get wrong: a film whose title sounds musical ("Orkiestra", "Jestem Rocky")
# or a concert of film music ("Koncert Muzyki Filmowej"). When an event lands in both categories of a pair,
# phrases in its title + description decide: the first rule that matches sets what it keeps. Without a
# match it keeps what the source said (a cinema says kino, a concert hall koncerty), else `default`.
PAIRS: list[tuple[tuple[str, str], list[tuple[str, tuple[str, ...]]], tuple[str, ...]]] = [
    (("kino", "koncerty"), [
        (r"muzyk\w* (z )?film|filmow\w* muzyk|soundtrack|koncert muzyki", ("koncerty",)),  # film music, played live
        (r"koncert|recital|retransmisj|transmisj|na zywo|live\b", ("kino", "koncerty")),   # concert film, live in a cinema
    ], ("kino",)),
    (("kino", "teatr"), [
        (r"retransmisj|transmisj|na zywo|live\b|national theatre|met opera|bolszoj|balet|spektakl", ("kino", "teatr")),
    ], ("kino",)),
]
_PAIRS = [(pair, [(re.compile(r"(?:^|\W)(?:" + rx + ")"), keep) for rx, keep in rules], default) for pair, rules, default in PAIRS]


def resolve_pairs(cats: list[str], text: str, trusted: set[str]) -> list[str]:
    """trusted: categories the source itself gave (not guessed from keywords)."""
    t = " " + unidecode(text).lower() + " "
    for pair, rules, default in _PAIRS:
        if not set(pair) <= set(cats):
            continue
        keep = next((k for rx, k in rules if rx.search(t)), None)
        if keep is None:
            said = [c for c in pair if c in trusted]
            keep = tuple(said) if len(said) == 1 else default
        cats = [c for c in cats if c not in pair or c in keep]
    return cats


# Not events, whatever the source says: council business, shop promotions, incidents, outages.
JUNK = re.compile(
    r"(?:^|\W)(sesj[aie] rady|komisj[aiey]|posiedzeni|wybory do|black ?week|black ?friday|rabat|promocj|wyprzedaz"
    r"|-\d{2}%|wylaczeni[ae] pradu|przerwa w dostawie|zamkniecie drogi|utrudnieni|wypadek|potracon|uderzyl"
    r"|przed sadem|dyzur|przetarg|nabor|rekrutacj|godziny otwarcia|na snieg)"
)


def is_junk(title: str) -> bool:
    return bool(JUNK.search(" " + unidecode(title).lower()))


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
    trusted = set(cats)
    cats += title_cats or desc_cats[:1]
    if source_default and source_default != "inne":
        cats.append(source_default)
        trusted.add(source_default)
    cats.append("inne")
    seen: list[str] = []
    for c in cats:
        if c not in seen:
            seen.append(c)
    seen = resolve_pairs(seen, f"{ev.title} {(ev.description or '')[:400]}", trusted)
    # Keep "inne" only when nothing else matched.
    ev.categories = [c for c in seen if c != "inne"] or ["inne"]
