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
        '"event_description":{"text":"Opis"}}, "y":{"id":"999999999","name":"Other","start_timestamp":1}}</script>'
    )
    d = parse_event_page(html, "123456789")
    assert d["title"] == "Quiz" and d["place"] == "Pub X" and d["lat"] == 50.01
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


def test_page_image_prefers_og_image():
    from tarnow_events.images import page_image
    html = '<html><head><meta property="og:image" content="/img/plakat.jpg"></head></html>'
    assert page_image(html, "https://x.pl/wydarzenie/1") == "https://x.pl/img/plakat.jpg"
    assert page_image("<html></html>", "https://x.pl/") is None


def test_logo_repeated_across_pages_is_ignored(tmp_path):
    import json, time
    from tarnow_events.images import enrich_images
    now = time.time()
    cache = {f"https://x.pl/e/{i}": {"i": "https://x.pl/logo.png", "t": now, "k": []} for i in range(3)}
    cache["https://x.pl/e/9"] = {"i": "https://x.pl/poster9.jpg", "t": now, "k": []}
    (tmp_path / "images.json").write_text(json.dumps(cache))
    a = ev("A", T, src="x", url="https://x.pl/e/1")
    b = ev("B", T, src="x", url="https://x.pl/e/9")
    enrich_images([a, b], tmp_path, listing_urls=set())
    assert a.image is None and b.image == "https://x.pl/poster9.jpg"


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
    assert sitemap.count("<url>") == 5 and "Było" not in sitemap
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
