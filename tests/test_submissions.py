import json
from types import SimpleNamespace

from tarnow_events import http
from tarnow_events.config import Settings, SourceConfig
from tarnow_events.sources.facebook import FacebookSource, parse_event_page
from tarnow_events.submissions import Ledger, event_ids_in, host_match, page_keys, read_csv, whitelist


def test_page_keys():
    assert page_keys("https://www.facebook.com/tarnowbema20") == {"tarnowbema20"}
    assert page_keys("https://www.facebook.com/TarnowMojeM/") == {"tarnowmojem"}
    assert page_keys("https://www.facebook.com/profile.php?id=61556342545859") == {"61556342545859"}
    assert page_keys("https://www.facebook.com/people/Piwnica-pod-Gwiazdami/61594151276309/") == {"61594151276309"}
    assert page_keys("https://www.facebook.com/pages/Gmina-Wojnicz/109744312385834") == {"109744312385834"}
    assert page_keys("https://www.facebook.com/Dom-Malarek-w-Zalipiu-233116693701844/") == {
        "dom-malarek-w-zalipiu-233116693701844", "233116693701844"}
    assert page_keys("https://www.facebook.com/events/123456789/") == set()
    assert page_keys("https://example.com/tarnowbema20") == set()


def test_host_match_by_vanity_or_id():
    allowed = whitelist([{"n": "Bema20", "fb": "https://www.facebook.com/tarnowbema20"},
                         {"n": "Piwnica", "fb": "https://www.facebook.com/profile.php?id=61594151276309"},
                         {"n": "No FB"}], extra=["https://www.facebook.com/extrapage"])
    assert host_match([{"id": "1", "url": "https://www.facebook.com/tarnowbema20"}], allowed)
    assert host_match([{"id": "61594151276309", "url": "https://www.facebook.com/people/X/61594151276309/"}], allowed)
    assert host_match([{"id": "9", "url": "https://www.facebook.com/ExtraPage"}], allowed)
    assert host_match([{"id": "2", "url": "https://www.facebook.com/someoneelse"}], allowed) is None


def _event_html(event_id, host_url, host_id="100063745472179", start=1796500000):
    return ('<script type="application/json">' + json.dumps({"data": {"event": {
        "id": event_id, "name": "Koncert", "start_timestamp": start,
        "event_place": {"name": "Klub", "location": {"latitude": 50.01, "longitude": 20.98}},
        "event_creator": {"__typename": "User", "name": "Host", "id": host_id},
        "event_hosts_that_can_view_guestlist": [{"id": host_id, "name": "Host", "url": host_url}],
        "page_as_owner": {"id": "2000496216889547"},
    }}}) + "</script>")


def test_parser_reads_hosts():
    d = parse_event_page(_event_html("123456789", "https://www.facebook.com/tarnowbema20"), "123456789")
    assert {"id": "100063745472179", "name": "Host", "url": "https://www.facebook.com/tarnowbema20"} in d["hosts"]
    assert {"id": "2000496216889547", "name": None, "url": None} in d["hosts"]
    assert len(d["hosts"]) == 2  # the creator is the same profile as the host


def test_share_links_followed_through_login_wall():
    login = "https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Fevents%2F987654321%2F"
    assert event_ids_in("2026-11-26,https://www.facebook.com/share/e/1AbCd/", lambda u: login) == {"987654321"}
    assert event_ids_in("https://m.facebook.com/events/123456789?ref=x", lambda u: None) == {"123456789"}
    assert read_csv("Sygnatura czasowa,Link\n2026-11-26,https://fb.me/e/x\n") == ["2026-11-26 https://fb.me/e/x"]


def test_ledger_retries_then_rejects(tmp_path):
    led = Ledger()
    for _ in range(3):
        assert not led.done("1")
        led.retry("1")
    assert led.done("1") and led.entries["1"]["status"] == "rejected"
    led.accept("2", {"start": 100, "end": None}, {"name": "Host"})
    led.forget_data_before(200)
    assert led.done("2") and led.accepted() == {}
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    led.save(a)
    Ledger({"1": {"status": "retry", "at": "2000-01-01T00:00:00+00:00"}}).save(b)
    assert Ledger.load(a, b).entries["1"]["status"] == "rejected"  # the newer copy wins


def test_submissions_end_to_end(tmp_path, monkeypatch):
    root = tmp_path
    (root / "docs").mkdir()
    (root / "docs" / "sources-catalogue.json").write_text(json.dumps([{"n": "Bema20", "fb": "https://www.facebook.com/tarnowbema20"}]))
    settings = Settings(root=root, out_dir=root / "public", cache_dir=root / "cache", sources_file=root / "s.yaml",
                        gemini_api_key=None, gemini_models=[], ai_max_calls=0, ai_daily_limit=0, radius_km=50,
                        days_back=7, days_ahead=365, public_base_url="http://x")
    csv_text = ("Sygnatura czasowa,Link\n"
                "1,https://www.facebook.com/events/111111111/\n"
                "2,https://www.facebook.com/events/222222222/?ref=x\n")
    pages = {"111111111": _event_html("111111111", "https://www.facebook.com/tarnowbema20"),
             "222222222": _event_html("222222222", "https://www.facebook.com/random.club", host_id="5")}
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        if "docs.google" in url:
            return SimpleNamespace(text=csv_text, url=url)
        return SimpleNamespace(text=pages[url.rstrip("/").rsplit("/", 1)[1]], url=url)

    monkeypatch.setattr(http, "get", fake_get)
    cfg = SourceConfig(id="facebook", name="Facebook", type="facebook", url="https://www.facebook.com/events/",
                       options={"submissions_csv": "https://docs.google.com/x.csv", "delay": 0})
    events = list(FacebookSource(cfg, settings).fetch())
    assert [e.source.url for e in events] == ["https://www.facebook.com/events/111111111/"]
    led = Ledger.load(settings.out_dir / "submissions.json")
    assert led.entries["111111111"]["status"] == "accepted"
    assert led.entries["222222222"]["status"] == "rejected"
    assert (settings.cache_dir / "submissions.json").exists()

    # Next build: same Sheet rows, nothing fetched but the Sheet; the accepted event is still published.
    fetched.clear()
    events = list(FacebookSource(cfg, settings).fetch())
    assert fetched == ["https://docs.google.com/x.csv"]
    assert [e.source.url for e in events] == ["https://www.facebook.com/events/111111111/"]


def test_rejected_event_accepted_after_page_whitelisted():
    led = Ledger()
    data = {"start": 1796500000, "end": None, "hosts": [{"id": "5", "name": "Club", "url": "https://www.facebook.com/random.club"}]}
    led.reject("3", "organizator spoza listy stron", "Club", data)
    assert led.recheck(whitelist([])) == [] and led.accepted() == {}
    assert led.recheck(whitelist([], extra=["https://www.facebook.com/random.club"])) == ["3"]
    assert led.accepted()["3"]["host"] == "Club"
