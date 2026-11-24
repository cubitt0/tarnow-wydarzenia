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


def test_every_page_has_a_preview_image():
    from tarnow_events.seo import _page
    base = "https://tarnow-wydarzenia.pl"
    plain = _page(base, "wydarzenie/x/", "Koncert", "Opis", "<p>x</p>")
    assert 'property="og:image" content="https://tarnow-wydarzenia.pl/og-image.png"' in plain
    assert 'name="twitter:card" content="summary_large_image"' in plain
    poster = _page(base, "wydarzenie/y/", "Spektakl", "Opis", "<p>y</p>", image="https://teatr.pl/plakat.jpg")
    assert 'property="og:image" content="https://teatr.pl/plakat.jpg"' in poster and "og-image.png" not in poster
