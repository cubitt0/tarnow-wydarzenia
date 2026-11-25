import httpx
import pytest

from tarnow_events import http

PROXY = "https://proxy.example/fetch"


@pytest.fixture
def proxied(monkeypatch):
    """Route biecz.pl through a fake proxy; yields the list of requests the proxy received."""
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.get("next") or httpx.Response(
            200, text="<rss/>", headers={"X-Final-Url": "https://www.biecz.pl/rss.xml"})

    monkeypatch.setattr(http, "PROXY_URL", PROXY)
    monkeypatch.setattr(http, "PROXY_TOKEN", "secret")
    monkeypatch.setattr(http, "_proxy_hosts", set())
    monkeypatch.setattr(http, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    http.route_via_proxy("https://www.biecz.pl/asp/_private/rss.xml")
    yield seen, replies


def test_proxied_request_keeps_site_url_and_sends_token(proxied):
    seen, _ = proxied
    resp = http.get("https://biecz.pl/rss.xml", params={"a": "1"})
    assert resp.text == "<rss/>"
    assert str(resp.url) == "https://www.biecz.pl/rss.xml"  # the site's, not the proxy's
    req = seen[0]
    assert str(req.url).startswith(PROXY)
    assert req.url.params["url"] == "https://biecz.pl/rss.xml?a=1"
    assert req.headers["X-Proxy-Token"] == "secret"


def test_insecure_flag_is_passed_to_proxy(proxied):
    seen, _ = proxied
    http.get("https://www.biecz.pl/x.pdf", insecure=True)
    assert seen[0].url.params["insecure"] == "1"


def test_monthly_limit_fails_fast_without_retries(proxied):
    seen, replies = proxied
    replies["next"] = httpx.Response(429, text="limit", headers={"X-Proxy-Limit": "300"})
    with pytest.raises(http.ProxyLimitReached, match="300"):
        http.get("https://www.biecz.pl/rss.xml")
    assert len(seen) == 1


def test_other_hosts_and_missing_config_go_direct(proxied, monkeypatch):
    assert not http._uses_proxy("https://www.tarnow.pl/")
    monkeypatch.setattr(http, "PROXY_TOKEN", "")
    assert not http._uses_proxy("https://www.biecz.pl/rss.xml")
