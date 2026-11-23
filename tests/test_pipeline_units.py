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
