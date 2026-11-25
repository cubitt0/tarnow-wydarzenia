import json

import httpx
import pytest

from tarnow_events import http

PROXY = "https://proxy.example/fetch"
SOURCE = "https://www.biecz.pl/asp/_private/rss.xml"


@pytest.fixture
def proxied(monkeypatch, tmp_path):
    """Route SOURCE through a fake proxy; yields (requests seen, queue of replies to give)."""
    seen: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if replies:
            return replies.pop(0)
        return httpx.Response(200, text="<rss/>", headers={"X-Final-Url": SOURCE})

    monkeypatch.setenv("TE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(http, "PROXY_URL", PROXY)
    monkeypatch.setattr(http, "PROXY_TOKEN", "secret")
    monkeypatch.setattr(http, "_proxy_hosts", set())
    monkeypatch.setattr(http, "_proxy_urls", set())
    monkeypatch.setattr(http, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    http.route_via_proxy(SOURCE)
    yield seen, replies


def _age_cache(tmp_path, day="2000-01-01"):
    for meta in (tmp_path / "proxy").glob("*.json"):
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "day": day}))


def test_source_url_goes_via_proxy_with_token_and_keeps_site_url(proxied):
    seen, _ = proxied
    resp = http.get(SOURCE)
    assert resp.text == "<rss/>"
    assert str(resp.url) == SOURCE  # the site's, not the proxy's
    assert str(seen[0].url).startswith(PROXY)
    assert seen[0].url.params["url"] == SOURCE
    assert seen[0].headers["X-Proxy-Token"] == "secret"


def test_second_fetch_same_day_reuses_copy_without_asking_proxy(proxied):
    seen, _ = proxied
    http.get(SOURCE)
    again = http.get(SOURCE)
    assert again.text == "<rss/>" and str(again.url) == SOURCE
    assert len(seen) == 1


def test_next_day_asks_proxy_again(proxied, tmp_path):
    seen, replies = proxied
    http.get(SOURCE)
    _age_cache(tmp_path)
    replies.append(httpx.Response(200, text="<rss>new</rss>", headers={"X-Final-Url": SOURCE}))
    assert http.get(SOURCE).text == "<rss>new</rss>"
    assert len(seen) == 2


def test_refusal_falls_back_to_last_copy(proxied, tmp_path):
    seen, replies = proxied
    http.get(SOURCE)
    _age_cache(tmp_path)
    replies.append(httpx.Response(429, headers={"X-Proxy-Limit": "daily limit reached"}))
    assert http.get(SOURCE).text == "<rss/>"
    assert len(seen) == 2


def test_refusal_without_copy_fails_fast_without_retries(proxied):
    seen, replies = proxied
    replies.append(httpx.Response(429, headers={"X-Proxy-Limit": "daily limit reached"}))
    with pytest.raises(http.ProxyLimitReached, match="daily limit"):
        http.get(SOURCE)
    assert len(seen) == 1


def test_errors_are_not_cached(proxied):
    seen, replies = proxied
    replies.append(httpx.Response(404, headers={"X-Final-Url": SOURCE}))
    with pytest.raises(httpx.HTTPStatusError):
        http.get(SOURCE)
    assert http.get(SOURCE).text == "<rss/>"
    assert len(seen) == 2


def test_insecure_flag_is_passed_to_proxy(proxied):
    seen, _ = proxied
    http.get(SOURCE, insecure=True)
    assert seen[0].url.params["insecure"] == "1"


def test_only_the_source_url_is_proxied(proxied, monkeypatch):
    seen, _ = proxied
    http.get("https://www.biecz.pl/aktualnosci/1")  # an event page on the same host goes direct
    assert seen[0].url.host == "www.biecz.pl"
    assert http._on_proxied_host("https://biecz.pl/x.jpg")  # but gone() still leaves the host alone
    monkeypatch.setattr(http, "PROXY_TOKEN", "")
    assert not http._uses_proxy(SOURCE)
