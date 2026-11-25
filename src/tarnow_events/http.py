from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# A coherent desktop-Chrome header set. Facebook in particular rejects a browser UA that
# arrives without the matching sec-* hints, so change these together or not at all.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_client: httpx.Client | None = None

# Some Polish sites refuse or drop GitHub Actions' (Azure, US) IPs. Requests to hosts of sources
# marked `proxy: true` in sources.yaml go through a fetch proxy in GCP europe-central2 (Warsaw),
# see proxy/gcp/. Without TE_PROXY_URL/TE_PROXY_TOKEN (local runs) they are fetched directly.
PROXY_URL = os.environ.get("TE_PROXY_URL", "").strip()
PROXY_TOKEN = os.environ.get("TE_PROXY_TOKEN", "").strip()
_proxy_hosts: set[str] = set()


class ProxyLimitReached(RuntimeError):
    pass


def route_via_proxy(url: str) -> None:
    """Send every later request to this URL's host through the proxy."""
    if host := urlparse(url).hostname:
        _proxy_hosts.add(host.lower().removeprefix("www."))


def _uses_proxy(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(PROXY_URL and PROXY_TOKEN and host in _proxy_hosts)


def _proxied_get(url: str, params: dict | None, headers: dict | None, insecure: bool) -> httpx.Response:
    target = str(httpx.URL(url, params=params)) if params else url
    query = {"url": target, **({"insecure": "1"} if insecure else {})}
    resp = client().get(PROXY_URL, params=query, headers={**(headers or {}), "X-Proxy-Token": PROXY_TOKEN},
                        timeout=httpx.Timeout(70.0, connect=15.0))
    if resp.status_code == 429 and "X-Proxy-Limit" in resp.headers:  # hard monthly cap: retrying is pointless
        raise ProxyLimitReached(f"PL proxy monthly limit ({resp.headers['X-Proxy-Limit']} requests) reached")
    # Make resp.url (and error messages) show the site's final URL, not the proxy's.
    resp.request = httpx.Request("GET", resp.headers.get("X-Final-Url", target))
    return resp


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers=BROWSER_HEADERS,
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            http2=False,
        )
    return _client


def get(
    url: str, *, params: dict | None = None, headers: dict | None = None, retries: int = 2, bare: bool = False,
    insecure: bool = False,
) -> httpx.Response:
    """bare=True sends only a User-Agent: some JSON APIs (Multikino) reject the full browser set.
    insecure=True skips TLS verification, for public pages of sites with a broken certificate chain."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if _uses_proxy(url):
                resp = _proxied_get(url, params, headers, insecure)
            elif bare or insecure:
                base = {"User-Agent": BROWSER_HEADERS["User-Agent"]} if bare else dict(BROWSER_HEADERS)
                resp = httpx.get(url, params=params, follow_redirects=True, timeout=30, verify=not insecure,
                                 headers={**base, **(headers or {})})
            else:
                resp = client().get(url, params=params, headers=headers)
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 and e.response.status_code != 429:
                raise
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last
