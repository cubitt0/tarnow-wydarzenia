from datetime import datetime, timedelta

import icalendar

from tarnow_events.categorize import detect
from tarnow_events.dedup import deduplicate, titles_match
from tarnow_events.ical_out import build_calendar
from tarnow_events.models import TZ, Event, SourceRef
from tarnow_events.sources.base import to_datetime
from tarnow_events.sources.facebook import find_event_ids, parse_event_page


def ev(title, start, src="a", url=None, town="Tarnów", lat=None, lon=None, prio=50, all_day=False):
    return Event(
        title=title, start=start, all_day=all_day, town=town, lat=lat, lon=lon, priority=prio,
        source=SourceRef(src, src.upper(), url or f"https://{src}.pl/{title}"),
    )


T = datetime(2026, 12, 5, 19, 0, tzinfo=TZ)


def test_titles_match():
    assert titles_match("Koncert: Kult w Tarnowie", "KULT")
    assert titles_match("Dziady – spektakl Teatru Solskiego", "Dziady")
    assert not titles_match("Kult", "Kultura")
    assert not titles_match("Jazz w Bazie", "Rock w Bazie")


def test_inflected_titles_match():
    assert titles_match("Dyskusyjny Klub Sportowy", "Spotkanie Dyskusyjnego Klubu Sportowego")
    assert not titles_match("Dyskusyjny Klub Sportowy", "Dyskusyjny Klub Książki")
    assert not titles_match("Klub Sportowy", "Spotkanie Klubu Sportowego Unia")  # one stem is not enough


def test_dedup_merges_across_sources_and_keeps_all_links():
    a = ev("Kult – koncert", T, src="ebilet", prio=40)
    b = ev("KULT", T + timedelta(minutes=30), src="venue", prio=70, lat=50.01, lon=20.98)
    c = ev("Kult", T, src="other", town="Dębica")  # different town → different event
    state: dict = {}
    out = deduplicate([a, b, c], state)
    assert len(out) == 2
    merged = next(e for e in out if len(e.all_sources()) == 2)
    assert merged.source.source_id == "venue"  # higher priority wins
    assert {r.source_id for r in merged.all_sources()} == {"ebilet", "venue"}


def test_same_facebook_event_merges_whatever_the_titles():
    a = ev("Oczami i sercem - wernisaż malarstwa, ikon i fotografii artystycznej", T, src="facebook",
           url="https://www.facebook.com/events/29083040651352149/", lat=50.02693, lon=20.99951)
    b = ev("Wernisaż: Oczami i sercem. Dwa spojrzenia wspólna pasja.", T, src="it",
           url="https://www.it.tarnow.pl/wydarzenie/wernisaz-oczami-i-sercem/", lat=50.0121, lon=20.9858)
    b.sources = [b.source, SourceRef("it", "FB", "https://www.facebook.com/events/29083040651352149/?acontext=%7B%7D")]
    out = deduplicate([a, b], {})
    assert len(out) == 1 and len(out[0].all_sources()) == 2  # the two Facebook links are one link


def test_uids_are_stable_between_runs():
    state: dict = {}
    first = deduplicate([ev("Dziady", T, src="teatr")], state)[0].uid
    # Next day a second source appears; the merged event keeps the old UID.
    second = deduplicate([ev("Dziady", T, src="teatr"), ev("Dziady spektakl", T, src="ebilet", prio=90)], state)
    assert len(second) == 1 and second[0].uid == first


def test_categories():
    assert detect("Koncert kolęd dla dzieci")[:2] == ["koncerty", "dzieci"]
    assert detect("Mecz Unia Tarnów – Stal")[0] == "sport"
    assert detect("Wernisaż wystawy malarstwa")[0] == "wystawy"
    # A trailing space in a keyword means a whole word: rock is music, Rocky is not.
    assert "koncerty" not in detect("Jestem Rocky")
    assert "koncerty" in detect("Koncert rockowy") and "koncerty" in detect("Rock & Blues Night")
    assert "koncerty" not in detect("Operator drona – pokaz")


def test_film_and_concert_pairs_resolved():
    from tarnow_events.categorize import categorize

    def cats(title, source_default="inne", source_cats=None, desc=None):
        e = ev(title, T)
        e.description = desc
        e.categories = source_cats or [source_default]
        categorize(e, source_default, from_source=e.categories != [source_default])
        return e.categories

    assert cats("Jestem Rocky (Kino Millenium (CSM))", "kino", desc="biograficzny, dramat") == ["kino"]
    assert cats("Orkiestra (Multikino)", "kino") == ["kino"]
    assert cats("Jestem Rocky / Napisy", source_cats=["kino", "koncerty"]) == ["kino"]  # AI guessed both
    assert cats("Koncert Muzyki Filmowej", "koncerty") == ["koncerty"]
    assert cats("MUZYKA Z FILMÓW ANDRZEJA WAJDY") == ["koncerty"]
    assert set(cats("Metallica: koncert w kinie – retransmisja", "kino")) == {"kino", "koncerty"}
    assert set(cats("Met Opera: Tosca – transmisja", "kino")) == {"kino", "koncerty"}
    assert set(cats("Balet Bolszoj: Dziadek do orzechów – retransmisja", "kino")) == {"kino", "teatr"}
    assert cats("Kino moralnego niepokoju", "teatr") == ["teatr"]  # a play titled after film: theatre says teatr


def test_to_datetime():
    dt, all_day = to_datetime("2026-12-05")
    assert all_day and dt.tzinfo is not None
    dt, all_day = to_datetime("2026-12-05T19:00")
    assert not all_day and dt.hour == 19


def test_ics_roundtrip():
    e = ev("Dziady", T)
    e.uid = "x@t"
    e.sources = [e.source, SourceRef("b", "B", "https://b.pl/x")]
    cal = icalendar.Calendar.from_ical(build_calendar("Test", "d", [e]))
    vevents = [c for c in cal.walk("VEVENT")]
    assert len(vevents) == 1
    assert "https://b.pl/x" in str(vevents[0]["DESCRIPTION"])
    assert str(vevents[0]["URL"]).startswith("https://a.pl")


def test_facebook_parser():
    html = (
        '<script type="application/json">{"x":{"id":"123456789","name":"Quiz","start_timestamp":1796500000,'
        '"event_place":{"name":"Pub X","location":{"latitude":50.01,"longitude":20.98}},'
        '"event_description":{"text":"Opis"},'
        '"event_buy_ticket_url":"https://l.facebook.com/l.php?u=https%3A%2F%2Fbiletyna.pl%2Fquiz%2F1&h=x"},'
        ' "y":{"id":"999999999","name":"Other","start_timestamp":1}}</script>'
    )
    d = parse_event_page(html, "123456789")
    assert d["title"] == "Quiz" and d["place"] == "Pub X" and d["lat"] == 50.01
    assert d["tickets"] == "https://biletyna.pl/quiz/1"
    assert find_event_ids("see https://www.facebook.com/events/foo/bar/123456789/?x=1") == {"123456789"}


def test_collapse_daily_exhibition_but_not_weekly_class():
    from tarnow_events.series import collapse_series, is_long
    daily = [ev("Wystawa X", T + timedelta(days=d), src="muzeum") for d in (0, 1, 2, 4, 5, 6, 7, 8, 9)]
    for e in daily:
        e.categories = ["wystawy"]
    weekly = [ev("Joga", T + timedelta(days=7 * w), src="muzeum") for w in range(4)]
    out = collapse_series(daily + weekly)
    exh = [e for e in out if e.title == "Wystawa X"]
    assert len(exh) == 1 and exh[0].all_day and is_long(exh[0])
    assert len([e for e in out if e.title == "Joga"]) == 4


def test_junk_filter_and_town_names():
    from tarnow_events.categorize import is_junk
    from tarnow_events.geo import canonical_town
    for t in ["XXXI Sesja Rady Gminy Wierzchosławice", "DEICHMANN: black week -50% na wybrane produkty",
              "Planowane wyłączenia prądu", "BMW uderzyło w tył Nissana", "XVII Komisja Kultury, Sportu"]:
        assert is_junk(t), t
    for t in ["Koncert kolęd", "Komisarz Alex – spektakl dla dzieci", "Dzień otwarty w ZSM"]:
        assert not is_junk(t), t
    assert canonical_town("Tarnow") == "Tarnów" and canonical_town("Kraków") == "Kraków"
    assert detect("Tribute to Pink Floyd")[0] == "koncerty"
    assert detect("Lotto SuperLiga: Handball Stal Mielec")[0] == "sport"


def test_rss_prefilter_needs_a_date():
    from tarnow_events.sources.rss import DATE_HINT
    assert DATE_HINT.search("Koncert odbędzie się 12 grudnia o godz. 18")
    assert DATE_HINT.search("Zapraszamy w sobotę 05.12")
    assert not DATE_HINT.search("Radni przyjęli budżet gminy na przyszły rok")


def test_daily_ai_budget_is_enforced(tmp_path):
    from tarnow_events.ai import Gemini
    from tarnow_events.config import Settings
    s = Settings.from_env()
    s.cache_dir, s.gemini_api_key, s.ai_daily_limit, s.ai_max_calls = tmp_path, "test-key", 2, 100
    g = Gemini(s)
    assert g.budget_left()
    g._count_call(); g._count_call()
    assert not g.budget_left()
    assert Gemini(s).calls_today() == 2  # persisted across runs of the same day
    assert g._generate("x") is None  # refuses without making a request


def test_consecutive_performances_are_not_collapsed():
    from tarnow_events.series import collapse_series
    shows = []
    for d, h in ((0, 18), (1, 17), (2, 17)):
        e = ev("Azoty Fiction", T.replace(hour=h) + timedelta(days=d), src="teatr")
        e.end, e.categories = e.start + timedelta(hours=3), ["teatr"]
        shows.append(e)
    out = collapse_series(shows)
    assert len(out) == 3 and not any(e.all_day for e in out)


def test_time_is_read_from_description():
    from tarnow_events.timeinfer import infer_time
    a = ev("Koncert", T.replace(hour=0), all_day=True); a.description = "Zapraszamy, godz. 18:30, wstęp wolny"
    assert infer_time(a) and a.start.hour == 18 and a.start.minute == 30 and not a.all_day
    b = ev("Spektakl", T.replace(hour=0), all_day=True); b.description = "27 listopada 18:00-21:00"
    assert infer_time(b) and b.start.hour == 18 and b.end.hour == 21
    c = ev("Warsztaty", T.replace(hour=0), all_day=True); c.description = "Grupa I 10:00, grupa II 12:00"
    assert not infer_time(c) and c.all_day  # ambiguous: stays all-day
    d = ev("Mamoteka", T.replace(hour=0), all_day=True); d.description = "Spotkanie rozpocznie się o godz. 17. Wstęp wolny."
    assert infer_time(d) and (d.start.hour, d.start.minute) == (17, 0)
    e = ev("Targ", T.replace(hour=0), all_day=True); e.description = "Targ będzie czynny od godz. 9 do 14."
    assert infer_time(e) and e.start.hour == 9 and e.end.hour == 14
    f = ev("Lodowisko", T.replace(hour=0), all_day=True); f.description = "Otwarte do godz. 21."
    assert not infer_time(f) and f.all_day  # when it closes, not when it starts
    g = ev("Miasteczko Mikołaja", T.replace(hour=0), all_day=True)
    g.description = "Uzdrowisko, godz: 09:00. Basen jest czynny w weekendy w godz. 11–21."
    assert not infer_time(g) and g.all_day  # the pool's opening hours are not the event's
    h = ev("Debata", T.replace(hour=0), all_day=True); h.description = "Wstęp wolny. Początek o 10:00."
    assert infer_time(h) and h.start.hour == 10


def test_free_entry_is_read_from_text():
    from tarnow_events.tickets import infer_free, says_free
    for text in ("Wstęp wolny.", "🎟️ WSTĘP WOLNY 🎟️", "Udział w zawodach jest bezpłatny.", "Wstęp: wolny",
                 "Bezpłatne zajęcia dla dzieci", "Darmowe wejściówki dostępne są w szkołach."):
        assert says_free(text), text
    for text in ("Wstęp wolny dla dzieci do lat 3.", "Bilety: 20 zł, wstęp wolny dla seniorów",
                 "Odbierz bezpłatną puszkę napoju", "Wstęp wolny dla posiadaczy karty", None):
        assert not says_free(text), text
    a = ev("Wernisaż", T); a.description = "Termin: 19 listopada, godzina 17:00\n\nWstęp wolny."
    assert infer_free(a) and a.price == "Wstęp wolny"
    b = ev("Koncert", T); b.price = "40 zł"; b.description = "Wstęp wolny"
    assert not infer_free(b) and b.price == "40 zł"  # the source's price wins


ARTICLE = """<html><head><meta property="og:title" content="Dziś kolejne spotkanie Mamoteki - TARNÓW"></head><body>
<header><a href="/">Tarnów</a> godz. 8:00-15:30 urząd czynny</header>
<main><section><aside>WARTO PRZECZYTAĆ <a href="/x">Koncert o godz. 19</a></aside>
<div>26.11.2026, 10:08</div><p>Już dzisiaj odbędzie się kolejne spotkanie Mamoteki.</p>
<p>Spotkanie rozpocznie się o godz. 17. Wstęp wolny.</p></section></main></body></html>"""


def test_time_and_free_entry_from_the_events_own_page(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tarnow_events import http, images
    monkeypatch.setattr(http, "get", lambda url, **kw: SimpleNamespace(text=ARTICLE))
    assert images.page_hints(ARTICLE) == {"h": ["17:00"], "f": True, "n": "Dziś kolejne spotkanie Mamoteki - TARNÓW"}
    a = ev("Spotkanie Mamoteka", T.replace(hour=0), src="tarnow", url="https://tarnow.pl/mamoteka", all_day=True)
    # A daily digest page two events link to: whose hour is it? Not used.
    b = ev("Spotkanie Mamoteki", T.replace(hour=0), src="d", url="https://d.pl/dzis", all_day=True)
    c = ev("Kiermasz ciast", T.replace(hour=0), src="d", url="https://d.pl/dzis", all_day=True)
    # A page whose heading is about something else is not this event's page.
    d = ev("Turniej szachowy", T.replace(hour=0), src="x", url="https://x.pl/mamoteka", all_day=True)
    stats = images.enrich_images([a, b, c, d], tmp_path, listing_urls=set())
    assert not a.all_day and (a.start.hour, a.start.minute) == (17, 0) and a.price == "Wstęp wolny"
    assert b.all_day and c.all_day and d.all_day and not (b.price or c.price or d.price)
    assert stats["times_from_pages"] == 1 and stats["free_from_pages"] == 1


def test_page_image_prefers_og_image():
    from tarnow_events.images import page_image
    html = '<html><head><meta property="og:image" content="/img/plakat.jpg"></head></html>'
    assert page_image(html, "https://x.pl/wydarzenie/1") == "https://x.pl/img/plakat.jpg"
    assert page_image("<html></html>", "https://x.pl/") is None


def test_logo_repeated_across_pages_is_ignored(tmp_path):
    import json, time
    from tarnow_events.images import enrich_images
    now = time.time()
    cache = {f"https://x.pl/e/{i}": {"i": "https://x.pl/logo.png", "t": now, "k": [], "h": []} for i in range(3)}
    cache["https://x.pl/e/9"] = {"i": "https://x.pl/poster9.jpg", "t": now, "k": [], "h": []}
    (tmp_path / "images.json").write_text(json.dumps(cache))
    a = ev("A", T, src="x", url="https://x.pl/e/1")
    b = ev("B", T, src="x", url="https://x.pl/e/9")
    enrich_images([a, b], tmp_path, listing_urls=set())
    assert a.image is None and b.image == "https://x.pl/poster9.jpg"


def test_repeated_event_poster_is_not_a_logo(tmp_path):
    import json, time
    from tarnow_events.images import enrich_images, event_key
    now = time.time()
    # One exhibition, one page per day (+ a re-added "-2" copy): the same poster on all of them.
    pages = [f"https://it.pl/wydarzenie/wystawa/2026-11-{d}/" for d in (19, 20, 21, 22)]
    pages.append("https://it.pl/wydarzenie/wystawa-2/2026-11-23/")
    cache = {p: {"i": "https://it.pl/wystawa.webp", "t": now, "v": now, "k": [], "h": []} for p in pages}
    (tmp_path / "images.json").write_text(json.dumps(cache))
    a = ev("Wystawa", T, src="it", url=pages[0])
    enrich_images([a], tmp_path, listing_urls=set())
    assert a.image == "https://it.pl/wystawa.webp"
    assert event_key("https://it.pl/wydarzenie/x/2026-11-19/") == event_key("https://it.pl/wydarzenie/x-3/") \
        == "https://it.pl/wydarzenie/x"
    assert event_key("https://x.pl/e/1") != event_key("https://x.pl/e/2")


def test_dead_cached_image_makes_page_be_read_again(tmp_path, monkeypatch):
    import json, time
    from types import SimpleNamespace
    from tarnow_events import http, images
    now = time.time()
    (tmp_path / "images.json").write_text(json.dumps({
        "https://x.pl/e/1": {"i": "https://x.pl/old-hash1.jpg", "t": now - 86400, "k": [], "h": []},
        "https://x.pl/e/2": {"i": "https://x.pl/ok.jpg", "t": now - 86400, "k": [], "h": []},
    }))
    monkeypatch.setattr(http, "gone", lambda url: "old-hash" in url)
    page = '<meta property="og:image" content="/new-hash2.jpg">'
    fetched = []
    monkeypatch.setattr(http, "get", lambda url, **kw: fetched.append(url) or SimpleNamespace(text=page))
    a = ev("A", T, src="x", url="https://x.pl/e/1")
    b = ev("B", T, src="x", url="https://x.pl/e/2")
    stats = images.enrich_images([a, b], tmp_path, listing_urls=set())
    assert a.image == "https://x.pl/new-hash2.jpg" and b.image == "https://x.pl/ok.jpg"
    assert fetched == ["https://x.pl/e/1"] and stats["dead_images"] == 1
    cache = json.loads((tmp_path / "images.json").read_text())
    assert cache["https://x.pl/e/2"]["v"] > now - 5  # checked today, not again until tomorrow


def test_listing_town_is_corrected_from_address():
    from tarnow_events.geo import correct_tarnow, nearest_town
    assert correct_tarnow("Tarnów", "Klub Attenzione Brzesko ul. Kopernika 18d,Brzesko") == "Brzesko"
    assert correct_tarnow("Tarnów", "ulica Rynek 1, 33-300 Nowy Sącz, Polska") == "Nowy Sącz"
    assert correct_tarnow("Tarnów", "Lusławice 250, 32-840 Zakliczyn gmina") == "Lusławice"
    # Street names derived from towns, or an address that also says Tarnów, keep Tarnów.
    assert correct_tarnow("Tarnów", "Hala, ul. Dębicka 12") == "Tarnów"
    assert correct_tarnow("Tarnów", "Galeria Krakowska, ul. Krakowska 5, Tarnów") == "Tarnów"
    assert correct_tarnow("Dębica", "ul. Tarnowska 1") == "Dębica"
    assert nearest_town(49.98908, 20.61254) == "Brzesko"


def test_source_pin_far_from_tarnow_overrides_listing_town(tmp_path):
    from tarnow_events.config import SourceConfig
    from tarnow_events.geo import Geocoder
    from tarnow_events.pipeline import is_tarnow, locate
    e = ev("Impreza", T, town="Tarnów", lat=49.98908, lon=20.61254)
    cfg = SourceConfig(id="a", name="A", type="jsonld", url="https://a.pl", town="Tarnów")
    dist = locate(e, cfg, Geocoder(tmp_path, online=False), 50)
    assert 20 < dist < 35 and e.town == "Brzesko" and not is_tarnow(e)


def test_links_point_to_readable_pages():
    from tarnow_events.config import Settings, SourceConfig
    from tarnow_events.sources.base import Source, is_machine_url
    for u in ["https://api.lu.ma/ics/get?entity=calendar&id=x", "https://www.halogorlice.info/rss/articles/pl",
              "https://x.pl/feed/", "https://x.pl/wp-json/tribe/events/v1/events", "https://x.pl/?format=feed&type=rss"]:
        assert is_machine_url(u), u
    for u in ["https://luma.com/enkn0pqj", "https://x.pl/feed-the-birds", "https://x.pl/plik.pdf"]:
        assert not is_machine_url(u), u
    luma = Source(SourceConfig(id="l", name="L", type="ics", url="https://api.lu.ma/ics/get?id=1"), Settings.from_env())
    e = luma.make_event(title="Meetup", start="2026-11-24T17:30", url=None,
                        description="Uzyskaj aktualne informacje na: https://luma.com/enkn0pqj\n\nAdres: Sala")
    assert e.source.url == "https://luma.com/enkn0pqj"
    assert luma.readable_url(None) == "https://lu.ma/"


def test_rss_events_link_to_their_article():
    from tarnow_events.config import Settings, SourceConfig
    from tarnow_events.sources.rss import RssSource
    feed = "https://www.halogorlice.info/rss/articles/pl"
    src = RssSource(SourceConfig(id="h", name="H", type="rss", url=feed), Settings.from_env())
    arts = [(t, link, "", 0.0) for t, link in [
        ("Koncert kolęd w gorlickiej bazylice już w niedzielę", "https://h.pl/artykul/1"),
        ("Ruch wahadłowy na ul. Sienkiewicza", "https://h.pl/artykul/2"),
        ("Mecz Glinika", "https://h.pl/artykul/3"), ("Mecz Sokoła", "https://h.pl/artykul/4")]]
    arts += [("Co? Gdzie? Halo! Weekend", "https://h.pl/artykul/5", "kino wystawa pt jozef barut architekt miasta w dworze", 1.0),
             ("Co? Gdzie? Halo! Tydzień", "https://h.pl/artykul/6", "wystawa pt jozef barut architekt miasta ostatnie dni", 2.0)]
    # Round-up article: the newest one whose text names the event.
    assert src._article_url({"title": "Wystawa pt. Józef Barut. Architekt miasta"}, arts) == "https://h.pl/artykul/6"
    assert src._article_url({"title": "Koncert kolęd", "url": feed}, arts) == "https://h.pl/artykul/1"
    assert src._article_url({"title": "Mecz", "url": None}, arts) is None  # ambiguous: no guessing
    assert src._article_url({"title": "X", "url": "https://h.pl/artykul/9"}, arts) == "https://h.pl/artykul/9"


def test_long_description_is_cut_at_a_word_with_ellipsis():
    from tarnow_events.pipeline import DESC_CHARS, short_desc
    text = "Zapraszamy na koncert. " * 60
    out = short_desc(text)
    assert out.endswith("…") and len(out) <= DESC_CHARS + 1 and not out[:-1].endswith(("Zaprasz", " "))
    assert short_desc("Krótki opis.") == "Krótki opis." and short_desc("") is None


def test_ticket_link_belongs_to_the_event():
    from tarnow_events.tickets import choose_ticket, free_ticket, is_ticket_url, page_ticket_candidates
    assert is_ticket_url("https://www.ebilet.pl/muzyka/rock/kult/") and is_ticket_url("https://bilety.teatr.tarnow.pl/spektakl/12")
    assert not is_ticket_url("https://biletyna.pl/") and not is_ticket_url("https://teatr.tarnow.pl/repertuar/")
    html = """<html><body><header><a href="https://www.ebilet.pl/teatr/inny-spektakl/">Kup</a></header>
      <article><h1>Dziady</h1><p>Bilety: <a href="https://biletyna.pl/teatr/Dziady/Tarnow">kup bilet</a></p></article>
      <footer><a href="https://www.bilety24.pl/wydarzenie/cos-innego-123">Polecamy</a></footer></body></html>"""
    cands = page_ticket_candidates(html, "https://teatr.pl/dziady")
    assert len(cands) == 3 and sum(c["c"] for c in cands) == 2  # header + footer are not the event's
    assert choose_ticket(ev("Dziady – spektakl", T), cands) == "https://biletyna.pl/teatr/Dziady/Tarnow"
    # Several shop links in the content and none names the event: no guessing.
    many = [{"u": f"https://www.ebilet.pl/x/inne-{i}/", "x": "", "c": 0, "j": 0} for i in range(3)]
    assert choose_ticket(ev("Jazz w Bazie", T), many) is None
    e = ev("Kult", T, url="https://kult.pl/trasa"); e.description = "Bilety: https://www.ebilet.pl/muzyka/rock/kult/."
    assert free_ticket(e) == "https://www.ebilet.pl/muzyka/rock/kult/"


def test_slugify_polish_and_length():
    from tarnow_events.seo import slugify
    assert slugify("Łódź – Żółć: koncert „Kult” #2") == "lodz-zolc-koncert-kult-2"
    assert slugify("!!!") == "wydarzenie"
    assert len(slugify("słowo " * 40)) <= 70 and not slugify("słowo " * 40).endswith("-")


def test_seo_pages_sitemap_and_jsonld(tmp_path):
    import json
    import re

    from tarnow_events.seo import write_seo
    a, b = ev("Koncert Kult", T, src="a"), ev("Koncert Kult", T + timedelta(hours=2), src="b")
    past = ev("Było", T - timedelta(days=3), src="c")
    cinema = ev("Seans", T, src="d")
    cinema.in_main = False
    for i, e in enumerate((a, b, past, cinema)):
        e.uid = f"uid{i}abcdef@x"
    a.cancelled, a.description = True, "Opis <b>koncertu</b>"
    pages = write_seo(tmp_path, "https://example.pl", [a, b, past, cinema], T - timedelta(days=1))
    assert pages == {"uid0abcdef@x": "wydarzenie/2026-12-05/koncert-kult/",
                     "uid1abcdef@x": "wydarzenie/2026-12-05/koncert-kult-uid1ab/"}
    html = (tmp_path / pages[a.uid] / "index.html").read_text()
    assert '<link rel="canonical" href="https://example.pl/wydarzenie/2026-12-05/koncert-kult/">' in html
    assert "<p class=\"desc\">Opis &lt;b&gt;koncertu" in html and "<b>koncertu</b>" not in html
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html).group(1))
    assert ld["@type"] == "Event" and ld["eventStatus"].endswith("EventCancelled")
    assert ld["startDate"] == "2026-12-05T19:00:00+01:00"
    assert f'href="/{pages[b.uid]}"' in html  # same-day events link to each other
    sitemap = (tmp_path / "sitemap.xml").read_text()
    assert sitemap.count("<url>") == 4 and "Było" not in sitemap and "/wydarzenia/" not in sitemap  # home, zrodla, 2 events
    assert not (tmp_path / "wydarzenia").exists() and 'href="/#dzien=2026-12-05"' in html
    assert "Sitemap: https://example.pl/sitemap.xml" in (tmp_path / "robots.txt").read_text()
    assert 'name="robots" content="noindex"' in (tmp_path / "404.html").read_text()


def test_screenings_get_noindex_pages_by_film_cinema_and_time(tmp_path):
    from tarnow_events.seo import film_title, home_list, write_seo
    assert film_title("Jestem Rocky (Kino Millenium (CSM))") == "Jestem Rocky"
    assert film_title("(Nie)znajomi") == "(Nie)znajomi" and film_title("JESTEM ROCKY") == "JESTEM ROCKY"

    def show(title, place, start, n):
        e = ev(title, start, src=f"s{n}")
        e.location_name, e.in_main, e.categories, e.uid = place, False, ["kino"], f"show{n}@x"
        return e
    a = show("Jestem Rocky (Kino Millenium (CSM))", "Kino Millenium (CSM)", T + timedelta(hours=1, minutes=15), 1)
    a.tickets = "https://bilety.csm.tarnow.pl/Kino/default.aspx?event_id=36993"
    b = show("Jestem Rocky (Multikino Tarnów)", "Multikino Tarnów", T + timedelta(days=1), 2)
    twin = show("Jestem Rocky (Kino Millenium (CSM))", "Kino Millenium (CSM)", T + timedelta(hours=1, minutes=15), 3)
    main = ev("Koncert Kult", T, src="m"); main.uid = "main@x"
    pages = write_seo(tmp_path, "https://example.pl", [main, a, b, twin], T - timedelta(days=1))
    assert pages["show1@x"] == "wydarzenie/2026-12-05/jestem-rocky/kino-millenium-20-15/"
    assert pages["show3@x"] == "wydarzenie/2026-12-05/jestem-rocky/kino-millenium-20-15-2/"
    assert pages["show2@x"] == "wydarzenie/2026-12-06/jestem-rocky/multikino-tarnow-19-00/"
    html = (tmp_path / pages["show1@x"] / "index.html").read_text()
    assert 'name="robots" content="noindex"' in html and "Inne seanse" in html
    assert f'href="/{pages["show2@x"]}"' in html and ">Kup bilet<" in html
    assert "jestem-rocky" not in (tmp_path / "sitemap.xml").read_text()
    assert "Rocky" not in home_list([main, a, b], pages, T - timedelta(hours=1))


def test_ads_off_by_default_and_on_with_both_ids(tmp_path):
    from tarnow_events.ads import HEAD_MARK, SLOT_MARK, apply_ads
    page = f"<head>{HEAD_MARK}</head><main>{SLOT_MARK}<footer></footer></main>"
    (tmp_path / "ads.txt").write_text("stale")
    off = apply_ads(page, tmp_path, None, None)
    assert off == "<head></head><main><footer></footer></main>" and not (tmp_path / "ads.txt").exists()
    assert "adsbygoogle" not in apply_ads(page, tmp_path, "ca-pub-123", "x\"><script>")  # malformed: stays off
    verify_only = apply_ads(page, tmp_path, "ca-pub-1234567890123456", None)  # before approval: no slot yet
    assert '<meta name="google-adsense-account" content="ca-pub-1234567890123456">' in verify_only
    assert "adsbygoogle" not in verify_only and (tmp_path / "ads.txt").exists()
    assert "<script" not in apply_ads(page, tmp_path, "ca-pub-1234567890123456", "x\"><script>")
    on = apply_ads(page, tmp_path, "ca-pub-1234567890123456", "9876543210")
    assert 'data-ad-client="ca-pub-1234567890123456"' in on and 'data-ad-slot="9876543210"' in on
    assert on.index("adsbygoogle.js") < on.index("</head>") < on.index("<ins") < on.index("<footer>")
    assert (tmp_path / "ads.txt").read_text() == "google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0\n"


def test_web_all_day_end_is_exclusive_like_ical():
    # Festiwal Czekolady 27-29.11: the model keeps the last day; the page wants the day after it.
    from tarnow_events.pipeline import json_end
    e = ev("Festiwal Czekolady", T.replace(month=11, day=27, hour=0), all_day=True)
    e.end = T.replace(month=11, day=29, hour=0)
    assert json_end(e).startswith("2026-11-30T00:00")
    timed = ev("Koncert", T); timed.end = T + timedelta(hours=2)
    assert json_end(timed) == timed.end.isoformat()
    assert json_end(ev("X", T)) is None


def test_multi_day_events_from_different_sources_merge():
    from tarnow_events.dedup import deduplicate
    d = lambda day: T.replace(month=11, day=day, hour=0)
    a = ev("Festiwal Czekolady na Rynku w Tarnowie", d(27), src="ittarnow", all_day=True); a.end = d(29)
    b = ev("CzekoFest, czyli święto czekolady na rynku", d(28), src="umtarnow", all_day=True); b.end = d(29)
    # Two different exhibitions from one source, overlapping, sharing a word: stay apart.
    x = ev("Wystawa czasowa „do CIEBIE”", d(18), src="ittarnow", all_day=True); x.end = d(30)
    y = ev("Wernisaż wystawy czasowej „ZAUWAŻONE”", d(20), src="muzeum", all_day=True); y.end = d(30)
    out = deduplicate([a, b, x, y], {})
    choc = [e for e in out if "zekol" in e.title.lower()]
    assert len(choc) == 1 and choc[0].day.day == 27 and choc[0].end.day == 29
    assert {r.source_id for r in choc[0].all_sources()} == {"ittarnow", "umtarnow"}
    assert len(out) == 3


def test_daily_digest_repeating_an_exhibition_merges():
    from tarnow_events.dedup import deduplicate
    d = lambda day: T.replace(month=11, day=day, hour=0)
    # halogorlice's daily column lists the same exhibition every day, starting it on the issue's date.
    issues = [ev("Wystawa pt. Józef Barut. Architekt miasta z gruzów", d(23), src="halo", all_day=True,
                 url=f"https://halo.pl/{day}") for day in (23, 24, 25)]
    issues[1].end = issues[2].end = d(30)
    # Another exhibition from that source in the same days stays apart.
    other = ev("Pokaz filatelistyczny pt. Bohaterom Ziemi Gorlickiej", d(24), src="halo", all_day=True)
    other.end = d(30)
    for e, day in zip(issues, (23, 24, 25)):
        e.start = d(day)
    out = deduplicate([*issues, other], {})
    barut = [e for e in out if "Barut" in e.title]
    assert len(barut) == 1 and barut[0].day.day == 23 and barut[0].end.day == 30
    assert len(out) == 2


def test_same_venue_same_time_merges_differently_worded_titles():
    t = T.replace(month=11, day=27)
    arena = dict(lat=50.00907, lon=20.92941)
    a = ev("ANDRZEJKI W RYTMIE HITÓW", t, src="biletyna", **arena)
    b = ev("Wielkie Andrzejki w Tarnowie", t, src="tarnowskatv", **arena)
    # Same place and time, nothing in common: two events.
    c = ev("Wernisaż wystawy „Trigger”", t, src="muzeum", **arena)
    # Only the town centre is known: the venue is not proven.
    x = ev("Andrzejki w klubie", t, src="x", lat=50.0121, lon=20.9858)
    y = ev("Andrzejkowa potańcówka", t, src="y", lat=50.0121, lon=20.9858)
    # Cinema screenings at the same time are separate shows.
    k1 = ev("Zwierzogród 2 (dubbing)", t, src="kino1", **arena); k1.in_main = False
    k2 = ev("Zwierzogród 2 (napisy)", t, src="kino2", **arena); k2.in_main = False
    out = deduplicate([a, b, c, x, y, k1, k2], {})
    assert len(out) == 6
    andrz = [e for e in out if {r.source_id for r in e.all_sources()} == {"biletyna", "tarnowskatv"}]
    assert len(andrz) == 1


def test_kino_marzenie_one_event_per_screening_with_ticket_link():
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
    from tarnow_events.config import SourceConfig, Settings
    from tarnow_events.sources.cinemas import KinoMarzenieSource
    html = """
    <div class="row item"><a href="/repertuar/1052,ebenezer"><img src="https://www.kinomarzenie.pl/p.jpeg"/></a>
      <h4><a class="color-0" href="/repertuar/1052,ebenezer"><span class="sr-only">Zobacz więcej na temat:</span>
        EBENEZER SCROOGE</a></h4><span class="d-block color-3 small">FANTASY | 12+ LAT</span>
      <div class="d-flex"><div class="d-flex flex-wrap"><span>DUBBING</span></div>
        <a class="btn" href="https://marzenieonline.tck.pl/MSI/Default.aspx?event_id=1"><span>Kup bilet</span>
          <span><span class="sr-only">Czwartek 26-11-2026 na godzinę</span> 16:45</span></a></div>
      <div class="d-flex"><div class="d-flex flex-wrap"><span>NAPISY</span></div>
        <a class="btn" href="https://marzenieonline.tck.pl/MSI/Default.aspx?event_id=2"><span>Kup bilet</span>
          <span><span class="sr-only">Czwartek 26-11-2026 na godzinę</span> 19:30</span></a></div>
    </div>
    <div class="row item"><h4><a href="/repertuar/1051,lalka">LALKA</a></h4></div>"""
    cfg = SourceConfig(id="kino-marzenie", name="Kino Marzenie", type="kinomarzenie", url="https://www.kinomarzenie.pl/repertuar",
                       town="Tarnów", options={"main_feed": False})
    out = list(KinoMarzenieSource(cfg, Settings.from_env()).parse_day(HTMLParser(html), "2026-11-26"))
    assert [(e.start.hour, e.start.minute) for e in out] == [(16, 45), (19, 30)]  # a film without screenings is skipped
    assert [e.tickets[-1] for e in out] == ["1", "2"]
    assert out[0].title == "EBENEZER SCROOGE (Kino Marzenie)" and out[0].description.startswith("Seans: DUBBING")
    assert out[0].source.url == "https://www.kinomarzenie.pl/repertuar?start_date=2026-11-26&end_date=2026-11-26"
    assert not out[0].in_main


def test_bilety24_films_get_their_cinema_from_the_organiser_id(monkeypatch):
    from types import SimpleNamespace

    from tarnow_events.config import Settings, SourceConfig
    from tarnow_events.sources import bilety24

    def card(href, title):
        return f'''<div class="k_polecane"><a href="{href}" class="full-info" title="{title}">
          <span class="kategoria kategoria-film">FILM</span></a><span class="cenainfo">od 18,00 pln</span></div>'''
    page = (card("/kino/1225-jestem-rocky-168244?id=1", "Film: JESTEM ROCKY - 2026-11-29 17:55 - Nowy Sącz")
            + card("/kino/9999-lalka-1?id=2", "Film: LALKA - 2026-11-29 18:00 - Limanowa")
            + card("/kino/9999-lalka-1?id=3", "Film: LALKA - 2026-11-30 18:00 - Limanowa"))
    film_page = '''<script type="application/ld+json">{"@type": "Event", "location": {"@type": "Place",
      "name": "Limanowski  Dom Kultury", "address": {"streetAddress": "Bronisława Czecha 4"}}}</script>'''
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(url)
        if url == bilety24.FRAGMENT:
            return SimpleNamespace(text=page if params["page"] == 1 else "")
        return SimpleNamespace(text=film_page)
    monkeypatch.setattr(bilety24.http, "get", fake_get)
    cfg = SourceConfig(id="bilety24", name="Bilety24", type="bilety24", url="https://www.bilety24.pl/",
                       options={"venues": {1225: {"name": "Kino Sokół Nowy Sącz", "address": "ul. Długosza 3"}}})
    out = list(bilety24.Bilety24Source(cfg, Settings.from_env()).fetch())
    assert [(e.location_name, e.address) for e in out] == [
        ("Kino Sokół Nowy Sącz", "ul. Długosza 3"),
        ("Limanowski Dom Kultury", "Bronisława Czecha 4"), ("Limanowski Dom Kultury", "Bronisława Czecha 4")]
    assert sum("/kino/9999" in u for u in calls) == 1  # an unknown organiser is looked up once
    assert not any(e.in_main for e in out)


def test_kino_millenium_screenings_from_kino_tarnow_pl():
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
    from tarnow_events.config import SourceConfig, Settings
    from tarnow_events.sources.cinemas import KinoTarnowSource
    html = """
    <div class="row gx-2 gx-md-3 item-box-1"><img class="media" src="https://kino.tarnow.pl/h.jpg" alt="Hokus">
      <ul class="meta-tags"><li class="item"><span class="visually-hidden">Gatunek:</span><span>animacja</span></li></ul>
      <h3 class="display-3">Hokus: Królestwo magii</h3>
      <a class="btn btn-ticket" href="https://bilety.csm.tarnow.pl/Kino/default.aspx?event_id=7&amp;typetran=0&amp;returnlink=http://localhost/">
        <span><span class="display-4 fw-semibold"><span class="visually-hidden">2026.11.25, godzina:</span> 14:00</span>
        <span class="display-6 text-white text-uppercase ms-1">sala duża</span></span>
        <span class="display-6 text-secondary-300">2D • DUBBING</span></a>
      <a class="btn btn-ticket" href="https://bilety.csm.tarnow.pl/Kino/default.aspx?event_id=8&amp;typetran=0">
        <span><span class="display-4 fw-semibold"><span class="visually-hidden">2026.11.25, godzina:</span> 16:00</span></span></a>
    </div>"""
    cfg = SourceConfig(id="kino-millenium", name="Kino Millenium (CSM)", type="kinotarnow", url="https://kino.tarnow.pl/wydarzenia/repertuar",
                       town="Tarnów", options={"main_feed": False})
    out = list(KinoTarnowSource(cfg, Settings.from_env()).parse_day(HTMLParser(html), "2026-11-25"))
    assert [f"{e.start:%H:%M}" for e in out] == ["14:00", "16:00"]
    assert out[0].title == "Hokus: Królestwo magii (Kino Millenium (CSM))"
    assert "event_id=7" in out[0].tickets and "localhost" not in out[0].tickets
    assert out[0].description.startswith("Seans: 2D • DUBBING / sala duża")
    assert out[0].source.url == "https://kino.tarnow.pl/wydarzenia/repertuar?startDate=2026-11-25&endDate=2026-11-25"


def test_screenings_stay_separate_but_merge_across_sources():
    t = T.replace(hour=10)
    def show(src, start):
        e = ev("Lalka (Kino Sokół)", start, src=src, url=f"https://{src}.pl/lalka"); e.in_main = False
        return e
    out = deduplicate([show("filmweb", t), show("filmweb", t + timedelta(minutes=45)), show("bilety24", t)], {})
    assert len(out) == 2
    assert {r.source_id for r in out[0].all_sources()} == {"filmweb", "bilety24"}
    assert len({e.uid for e in out}) == 2


def test_theatre_links_play_page_and_falls_back_to_box_office():
    from tarnow_events.config import SourceConfig
    from tarnow_events.pipeline import apply_tickets_fallback
    from tarnow_events.sources.embedded_json import _dig
    obj = {"permalink": "https://teatr.tarnow.pl/class/x/?wcs_timestamp=1",
           "buttons": {"related_post": {"href": "https://teatr.tarnow.pl/spektakle/x/"}, "main": None}}
    assert _dig(obj, "buttons.related_post.href") == "https://teatr.tarnow.pl/spektakle/x/"
    assert _dig(obj, "buttons.main.href") is None
    box = "https://www.bilety24.pl/organizator/teatr-1151.html"
    cfgs = [SourceConfig(id="teatr", name="T", type="embedded_json", url="https://t.pl", options={"tickets_fallback": box})]
    a, b, c = ev("Dziady", T, src="teatr"), ev("Dziady", T, src="teatr"), ev("Koncert", T, src="inne")
    b.tickets = "https://www.bilety24.pl/kup-bilet-na-1151-dziady-1"
    apply_tickets_fallback([a, b, c], cfgs)
    assert a.tickets == box and b.tickets.endswith("dziady-1") and c.tickets is None


def test_filmweb_poster_is_requested_large():
    from tarnow_events.sources.cinemas import big_poster
    assert big_poster("https://fwcdn.pl/fpo/32/13/10103213/8275725.7.webp") == "https://fwcdn.pl/fpo/32/13/10103213/8275725.8.webp"
    assert big_poster("https://fwcdn.pl/fpo/32/13/10103213/8275725.2.jpg") == "https://fwcdn.pl/fpo/32/13/10103213/8275725.3.jpg"
    assert big_poster("https://example.com/a.7.webp") == "https://example.com/a.7.webp"
    assert big_poster(None) is None


def test_tribe_event_website_becomes_second_link():
    from tarnow_events.config import SourceConfig, Settings
    from tarnow_events.sources.tribe import TribeSource
    src = TribeSource(SourceConfig(id="it-tarnow", name="IT", type="tribe", url="https://www.it.tarnow.pl"), Settings.from_env())
    e = ev("Ciężarówka", T, src="it-tarnow", url="https://www.it.tarnow.pl/wydarzenie/ciezarowka/")
    src.add_website(e, "https://www.tarnow.pl/Dla-mieszkancow/Aktualnosci/Miasto/Ciezarowka")
    assert [r.url for r in e.all_sources()] == ["https://www.it.tarnow.pl/wydarzenie/ciezarowka/",
                                                "https://www.tarnow.pl/Dla-mieszkancow/Aktualnosci/Miasto/Ciezarowka"]
    assert e.all_sources()[1].source_name == "Strona wydarzenia (tarnow.pl)"
    same = ev("X", T, src="it-tarnow", url="https://www.it.tarnow.pl/wydarzenie/x/")
    src.add_website(same, "https://www.it.tarnow.pl/wydarzenie/x")
    src.add_website(same, "")
    assert len(same.all_sources()) == 1


def test_home_page_lists_coming_week_for_crawlers():
    from tarnow_events.seo import home_list
    now = T.replace(hour=12)
    soon, later, past = ev("Dziady", T, src="a"), ev("Wesele", T + timedelta(days=9), src="a"), ev("Rano", T.replace(hour=9), src="a")
    soon.uid, later.uid, past.uid = "s", "l", "p"
    html = home_list([soon, later, past], {"s": "wydarzenie/2026-12-05/dziady/", "l": "x/", "p": "y/"}, now)
    assert '<a href="wydarzenie/2026-12-05/dziady/">Dziady</a>' in html and "19:00" in html
    assert "Wesele" not in html and "Rano" not in html
    page = open("src/tarnow_events/web/kalendarz.html", encoding="utf-8").read()
    head = page.split("</head>")[0]
    assert ".js .static-list{display:none}" in head and 'classList.add("js")' in head and "<!--STATIC-LIST-->" in page


def test_every_page_has_a_preview_image():
    from tarnow_events.seo import _page
    base = "https://tarnow-wydarzenia.pl"
    plain = _page(base, "wydarzenie/x/", "Koncert", "Opis", "<p>x</p>")
    assert 'property="og:image" content="https://tarnow-wydarzenia.pl/og-image.png"' in plain
    assert 'name="twitter:card" content="summary_large_image"' in plain
    poster = _page(base, "wydarzenie/y/", "Spektakl", "Opis", "<p>y</p>", image="https://teatr.pl/plakat.jpg")
    assert 'property="og:image" content="https://teatr.pl/plakat.jpg"' in poster and "og-image.png" not in poster


def test_feeds_per_region_and_kind_for_the_subscribe_wizard(tmp_path):
    import json
    from tarnow_events.config import Settings, SourceConfig
    from tarnow_events.pipeline import write_outputs
    settings = Settings(root=tmp_path, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache", sources_file=tmp_path / "s.yaml",
                        gemini_api_key=None, gemini_models=[], ai_max_calls=0, ai_daily_limit=0, radius_km=50,
                        days_back=1, days_ahead=60, public_base_url="https://x.pl")
    city = ev("Koncert w mieście", T, src="a", url="https://a.pl/1", lat=50.0125, lon=20.9886)
    city.categories, city.uid = ["koncerty"], "u1"
    village = ev("Koncert na wsi", T, src="b", url="https://b.pl/1", town="Dębica", lat=50.05, lon=21.41)
    village.categories, village.uid = ["koncerty"], "u2"
    configs = [SourceConfig(id="a", name="Źródło A", type="ics", url="https://a.pl/", group="kultura"),
               SourceConfig(id="b", name="Źródło B", type="ics", url="https://b.pl/", group="region"),
               SourceConfig(id="c", name="Puste", type="ics", url="https://c.pl/", group="media")]
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "sources-catalogue.json").write_text(json.dumps([
        {"n": "Klub X", "g": "lokale", "t": "Tarnów", "u": "https://x.pl", "s": "tylko FB/IG", "o": "English note"},
        {"n": "Portal Y", "g": "media", "u": "https://y.pl", "s": "odrzucone", "o": "Site down.", "r": "Strona nie działa."},
        {"n": "Gmina Z", "g": "region", "s": "planowane", "r": "Do zrobienia."},
        {"n": "Aktywne W", "g": "kultura", "s": "aktywne"}]))
    write_outputs(settings, [city, village], configs, {"a": {"ok": True}, "b": {"ok": False, "error": "x"}})
    out = settings.out_dir
    assert b"Koncert w mie" in (out / "region/tarnow/koncerty.ics").read_bytes()
    assert b"Koncert na wsi" not in (out / "region/tarnow/koncerty.ics").read_bytes()
    assert b"Koncert na wsi" in (out / "region/okolice/koncerty.ics").read_bytes()
    cal = json.loads((out / "calendar.json").read_text())
    by_path = {f["path"]: f for f in cal["feeds"]}
    assert by_path["region/okolice/koncerty.ics"] | {} == {"path": "region/okolice/koncerty.ics", "count": 1, "group": "region",
                                                            "region": "okolice", "cat": "koncerty",
                                                            "name": by_path["region/okolice/koncerty.ics"]["name"]}
    assert by_path["tarnow.ics"]["region"] == "tarnow" and by_path["kategoria/koncerty.ics"]["region"] == "all"
    assert by_path["zrodlo/a.ics"]["label"] == "Źródło A" and by_path["zrodlo/a.ics"]["sgroup"] == "kultura"
    assert "zrodlo/c.ics" not in by_path  # nothing upcoming: not offered
    assert "Google Calendar" in cal["howto"]
    page = (out / "zrodla.html").read_text()
    assert "Gminy i miasta w okolicy" in page and "favHTML" in page and "z błędem" in page and ".ics" not in page
    assert "Strona nie działa." in page and "Site down." not in page and "Tylko Facebook lub Instagram" in page
    assert "English note" not in page and "Gmina Z" in page and "Aktywne W" not in page
    assert [a["name"] for a in cal["apps"]][:2] == ["Google Calendar", "Apple Calendar"]


def test_archive_keeps_past_pages_and_tells_removed_from_source_down(tmp_path):
    import re

    from tarnow_events import archive
    from tarnow_events.seo import write_seo

    def mk(title, start, src, uid):
        e = ev(title, start, src=src)
        e.uid, e.categories, e.location_name = uid, ["koncerty"], "Hala"
        return e

    held = mk("Koncert Kult", T, "a", "held@x")
    held.tickets = "https://bilety.pl/kult"
    dropped = mk("Spektakl", T + timedelta(days=5), "b", "dropped@x")
    unread = mk("Wystawa", T + timedelta(days=5), "c", "unread@x")
    moved = mk("Jarmark", T + timedelta(days=3), "a", "moved@x")
    later = mk("Koncert Dżem", T + timedelta(days=9), "a", "later@x")
    arch: dict = {}
    first = write_seo(tmp_path, "https://example.pl", [held, dropped, unread, moved, later], T - timedelta(days=1), arch)

    # Next run, after the concert: the play is no longer listed by its (working) source, the exhibition's
    # source is down, the fair moved by a day.
    moved.start += timedelta(days=1)
    now = T + timedelta(days=1)
    archive.save(arch, tmp_path / "a" / archive.FILE)
    (tmp_path / "broken.gz").write_text("<html>404</html>")  # a failed download: the other copy is used
    arch = archive.load(tmp_path / "missing.gz", tmp_path / "broken.gz", tmp_path / "a" / archive.FILE)
    pages = write_seo(tmp_path, "https://example.pl", [moved, later], now, arch, fetched={"a", "b"})

    past = (tmp_path / first["held@x"] / "index.html").read_text()
    assert "już się odbyło" in past and 'content="noindex"' not in past and "Kup bilet" not in past
    assert '"@type":"Event"' in past and "offers" not in past
    assert f'href="/{pages["later@x"]}"' in past and "Nadchodzące wydarzenia" in past  # somewhere to go instead
    gone = (tmp_path / first["dropped@x"] / "index.html").read_text()
    assert "nie ma już w kalendarzu" in gone and 'content="noindex"' in gone and '"@type":"Event"' not in gone
    kept = (tmp_path / first["unread@x"] / "index.html").read_text()
    assert "nie ma już" not in kept and "już się odbyło" not in kept and 'content="noindex"' not in kept
    assert pages["moved@x"] != first["moved@x"]
    old = (tmp_path / first["moved@x"] / "index.html").read_text()
    assert re.search(rf'http-equiv="refresh" content="0; url=/{re.escape(pages["moved@x"])}"', old)
    sitemap = (tmp_path / "sitemap.xml").read_text()
    assert pages["later@x"] in sitemap and first["held@x"] not in sitemap and first["dropped@x"] not in sitemap

    # A title fixed by the source keeps the address; a year on, the archive lets the concert go.
    later.title = "Koncert zespołu Dżem"
    assert write_seo(tmp_path, "https://example.pl", [later], now, arch)["later@x"] == pages["later@x"]
    write_seo(tmp_path, "https://example.pl", [later], T + timedelta(days=367), arch)
    assert "held@x" not in arch and not (tmp_path / first["held@x"]).exists()


def test_merge_keeps_every_sources_text():
    a = ev("Kult – koncert", T, src="ebilet", prio=40)
    b = ev("KULT", T, src="venue", prio=70)
    a.description, b.description = "Koncert zespołu Kult. Bilety w sprzedaży.", "Kult zagra największe przeboje."
    merged = deduplicate([a, b], {})
    assert len(merged) == 1
    assert merged[0].source_texts == [("venue", "VENUE", "Kult zagra największe przeboje."),
                                      ("ebilet", "EBILET", "Koncert zespołu Kult. Bilety w sprzedaży.")]
    assert merged[0].description == "Kult zagra największe przeboje."  # the main source's, until AI rewrites it


def test_description_texts_and_cleanup():
    from tarnow_events.describe import clean, distinct_texts

    e = ev("Mamoteka", T)
    e.source_texts = [("p", "Portal", "Dziś spotkanie Mamoteki…"),
                      ("b", "Biblioteka", "Dziś spotkanie Mamoteki w bibliotece. Zabawy dla dzieci do 3 lat."),
                      ("m", "Miasto", "Zapisy pod numerem telefonu biblioteki.")]
    assert [n for n, _ in distinct_texts(e)] == ["Biblioteka", "Miasto"]  # the lead repeats the article
    out = clean("🎉 **Koncert** zespołu — wielkie przeboje – i nowe utwory 🎸. Więcej: https://x.pl/a\n\nGodziny 18:00–20:00.")
    assert out == "Koncert zespołu, wielkie przeboje, i nowe utwory. Więcej:\n\nGodziny 18:00–20:00."
    assert clean("🎉🎉 —") is None


def test_ai_descriptions_cached_and_fallback(tmp_path):
    from tarnow_events.describe import improve_descriptions

    class FakeAI:
        enabled, cache_dir = True, tmp_path

        def __init__(self, answer):
            self.answer, self.calls = answer, []

        def describe(self, items):
            self.calls.append(items)
            return None if self.answer is None else {it["id"]: self.answer for it in items}

    def event():
        e = ev("Kult", T)
        e.description = "Koncert zespołu Kult."
        e.source_texts = [("a", "A", "Koncert zespołu Kult."), ("b", "B", "Kult zagra w Tarnowie przeboje z płyty Ostateczny krach.")]
        return e

    now = T - timedelta(days=1)
    single = ev("Solo", T)
    single.description, single.source_texts = "Tylko jedno źródło.", [("a", "A", "Tylko jedno źródło.")]
    e = event()
    stats = improve_descriptions([e, single], FakeAI("Zespół Kult zagra w Tarnowie przeboje — także z płyty Ostateczny krach 🎸."), now)
    assert e.description == "Zespół Kult zagra w Tarnowie przeboje, także z płyty Ostateczny krach."
    assert single.description == "Tylko jedno źródło." and stats["descriptions_new"] == 1
    # Blocked the next day: the cached answer still applies.
    again = event()
    blocked = FakeAI(None)
    improve_descriptions([again], blocked, now)
    assert again.description == e.description and not blocked.calls
    # A source changed its text and the AI is blocked: the main source's description stays.
    changed = event()
    changed.source_texts[1] = ("b", "B", "Nowy tekst o koncercie zespołu Kult w Tarnowie.")
    stats = improve_descriptions([changed], blocked, now)
    assert changed.description == "Koncert zespołu Kult." and stats["descriptions_waiting"] == 1


def test_desc_cut_by_us_or_by_source():
    from tarnow_events.pipeline import DESC_CHARS, desc_cut

    lead = "Po gorąco przyjętej trasie The Best Of zapraszamy na drugą odsłonę koncertów z utworami jego idoli - Ciechowskiego, Wodec..."
    assert desc_cut(lead) and desc_cut(lead[:-3] + "…") and desc_cut(lead[:-3] + " […]")
    assert desc_cut("Słowo " * (DESC_CHARS // 5))  # too long: we cut it
    assert not desc_cut("Zapraszamy...") and not desc_cut(lead[:-3] + ".") and not desc_cut(None)


def test_excerpt_sources_mark_their_cut():
    from tarnow_events.pipeline import desc_cut, mark_excerpt

    body = "Bas może być nie tylko fundamentem sekcji rytmicznej. " * 4
    cut = mark_excerpt(body + "Bas może być pełnoprawnym")  # allevents.in: stops mid-sentence
    assert cut.endswith("Bas może być…") and desc_cut(cut)
    assert mark_excerpt(body + "Szczególnie, jeśli jego partnerkami są stewardessy. P").endswith("są stewardessy…")
    assert mark_excerpt(body.rstrip()) == body.rstrip()  # ends a sentence: left as it is
    assert mark_excerpt("Wejście z biletem...") == "Wejście z biletem..." and mark_excerpt(None) is None


def test_long_text_shortened_by_ai_and_no_ai_sources(tmp_path):
    from tarnow_events.describe import improve_descriptions
    from tarnow_events.pipeline import DESC_CHARS, desc_cut, read_more

    class FakeAI:
        enabled, cache_dir = True, tmp_path

        def __init__(self):
            self.sent = []

        def describe(self, items):
            self.sent += items
            return {it["id"]: "Krótszy opis wydarzenia napisany z długiego tekstu organizatora." for it in items}

    long = "Długi opis wydarzenia z programem. " * (DESC_CHARS // 30)
    venue = ev("Spektakl", T, src="venue")
    venue.description, venue.source_texts = long, [("venue", "VENUE", long)]
    info = ev("Wystawa", T, src="it-tarnow")
    info.description, info.source_texts = long, [("it-tarnow", "IT", long)]
    both = ev("Koncert", T, src="org")  # the tourist office is only a second source here
    both.description = "Koncert w sali kameralnej."
    both.source_texts = [("org", "ORG", "Koncert w sali kameralnej."), ("it-tarnow", "IT", "Inny tekst informacji turystycznej.")]
    for e in (venue, info, both):
        e.desc_more = desc_cut(e.description)  # as the pipeline does before the AI
    ai = FakeAI()
    stats = improve_descriptions([venue, info, both], ai, T - timedelta(days=1), no_ai={"it-tarnow"})
    assert venue.description.startswith("Krótszy opis") and read_more(venue)  # shortened, still links to the source
    assert info.description == long and read_more(info)  # never sent: cut on the page as before
    assert both.description == "Koncert w sali kameralnej."  # one text left without the office's: nothing to merge
    assert [it["title"] for it in ai.sent] == ["Spektakl"] and stats["descriptions_new"] == 1


def test_only_new_events_get_long_text_shortened(tmp_path):
    from tarnow_events.describe import improve_descriptions
    from tarnow_events.pipeline import DESC_CHARS

    class FakeAI:
        enabled, cache_dir = True, tmp_path

        def __init__(self, ok=True):
            self.ok, self.sent = ok, []

        def describe(self, items):
            self.sent += [it["title"] for it in items]
            return {it["id"]: f"Skrócony opis wydarzenia {it['title']} z długiego tekstu." for it in items} if self.ok else None

    long = "Długi opis wydarzenia z programem. " * (DESC_CHARS // 30)

    def event(title, uid):
        e = ev(title, T, src=title.lower())
        e.uid, e.description, e.source_texts = uid, long, [(title.lower(), title, long)]
        return e

    now = T - timedelta(days=1)
    old, fresh = event("Stary", "old@x"), event("Nowy", "new@x")
    ai = FakeAI()
    stats = improve_descriptions([old, fresh], ai, now, new={"new@x"})
    assert ai.sent == ["Nowy"] and old.description == long and fresh.description.startswith("Skrócony")
    assert stats["descriptions_long_kept"] == 1
    # New, but the allowance is used up today: it waits, and is shortened in a later build (no longer "new").
    late = event("Późny", "late@x")
    improve_descriptions([late], FakeAI(ok=False), now, new={"late@x"})
    assert late.description == long
    later = event("Późny", "late@x")
    ai = FakeAI()
    improve_descriptions([later], ai, now, new=set())
    assert ai.sent == ["Późny"] and later.description.startswith("Skrócony")


def test_html_links_feed_takes_article_links():
    from tarnow_events.sources.rss import _html_links_feed

    page = """<a href="/Dla-mieszkancow/Aktualnosci/Sport">Sport</a>
      <a href="/Dla-mieszkancow/Aktualnosci/Sport/Trzy-mecze"><img src="x.jpg"></a>
      <a href="/Dla-mieszkancow/Aktualnosci/Sport/Trzy-mecze">Trzy mecze tarnowskich futsalistów</a>
      <a href="/Dla-mieszkancow/Aktualnosci/Kultura/Recital">Recital organowy</a>
      <a href="/Dla-mieszkancow/Aktualnosci?page=2">2</a>"""
    feed = _html_links_feed(page, "https://www.tarnow.pl/Dla-mieszkancow/Aktualnosci",
                            r"tarnow\.pl/Dla-mieszkancow/Aktualnosci/[^/?#]+/[^/?#]+/?$")
    assert [(e["title"], e["link"]) for e in feed.entries] == [
        ("Trzy mecze tarnowskich futsalistów", "https://www.tarnow.pl/Dla-mieszkancow/Aktualnosci/Sport/Trzy-mecze"),
        ("Recital organowy", "https://www.tarnow.pl/Dla-mieszkancow/Aktualnosci/Kultura/Recital")]


def test_page_image_passes_over_logo_for_the_posts_picture():
    from tarnow_events.images import page_image

    base = "https://tarnow.geminipark.pl/aktualnosci/krawczyk/"
    logo = '<meta property="og:image" content="https://tarnow.geminipark.pl/wp-content/uploads/2022/06/Logo-GEMINI-PARK.svg">'
    ld = ('<script type="application/ld+json">{"@graph":[{"@type":"BlogPosting","image":{"@type":"ImageObject",'
          '"url":"https://tarnow.geminipark.pl/wp-content/uploads/2026/11/krawczyk.jpg"}}]}</script>')
    body = '<article><img src="/ikona.png" width="24"><img src="/wp-content/uploads/plakat.jpg"></article>'
    assert page_image(logo + ld + body, base).endswith("/2026/11/krawczyk.jpg")  # the post's picture, not the logo
    assert page_image(logo + body, base) == "https://tarnow.geminipark.pl/wp-content/uploads/plakat.jpg"  # first real <img>
    assert page_image(logo, base) is None
    photo = '<meta property="og:image" content="https://teatr.pl/plakat.jpg">'
    assert page_image(photo + ld, base) == "https://teatr.pl/plakat.jpg"  # a real og:image still comes first
