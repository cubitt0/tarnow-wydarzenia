from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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

# Some Polish sites refuse or drop GitHub Actions' (Azure, US) IPs. The URLs of sources marked
# `proxy: true` in sources.yaml are fetched through a proxy in GCP europe-central2 (Warsaw), see
# proxy/gcp/, which allows one request per site host per day. Each answer is kept in
# cache/proxy/, so later builds that day reuse it instead of asking again, and a refusal falls
# back to the last copy. Other pages on those hosts (event pages, pictures) are not proxied.
# Without TE_PROXY_URL/TE_PROXY_TOKEN (local runs) everything is fetched directly.
PROXY_URL = os.environ.get("TE_PROXY_URL", "").strip()
PROXY_TOKEN = os.environ.get("TE_PROXY_TOKEN", "").strip()
_proxy_hosts: set[str] = set()
_proxy_urls: set[str] = set()
WARSAW = ZoneInfo("Europe/Warsaw")


class ProxyLimitReached(RuntimeError):
    pass


def route_via_proxy(url: str) -> None:
    """Fetch this exact URL (a source's own) through the proxy from now on."""
    _proxy_urls.add(url)
    if host := urlparse(url).hostname:
        _proxy_hosts.add(host.lower().removeprefix("www."))


def _proxy_on() -> bool:
    return bool(PROXY_URL and PROXY_TOKEN)


def _uses_proxy(url: str) -> bool:
    return _proxy_on() and url in _proxy_urls


def _on_proxied_host(url: str) -> bool:
    return _proxy_on() and (urlparse(url).hostname or "").lower().removeprefix("www.") in _proxy_hosts


def _cache_paths(target: str) -> tuple[Path, Path]:
    root = Path(os.environ.get("TE_CACHE_DIR", Path(os.environ.get("TE_ROOT", Path.cwd())) / "cache")) / "proxy"
    key = hashlib.sha256(target.encode()).hexdigest()[:24]
    return root / f"{key}.json", root / f"{key}.bin"


def _cached(target: str) -> tuple[str, httpx.Response] | None:
    """(Warsaw day it was fetched, response) of the last proxied answer for this URL."""
    meta_path, body_path = _cache_paths(target)
    try:
        meta = json.loads(meta_path.read_text())
        resp = httpx.Response(meta["status"], content=body_path.read_bytes(),
                              headers={"Content-Type": meta["content_type"]},
                              request=httpx.Request("GET", meta["final_url"]))
        return meta["day"], resp
    except (OSError, ValueError, KeyError):
        return None


def _store(target: str, resp: httpx.Response) -> None:
    meta_path, body_path = _cache_paths(target)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(resp.content)
        meta_path.write_text(json.dumps({
            "day": f"{datetime.now(WARSAW):%Y-%m-%d}", "status": resp.status_code, "url": target,
            "final_url": str(resp.url), "content_type": resp.headers.get("Content-Type", ""),
        }))
    except OSError as e:
        log.warning("proxy cache: cannot store %s: %s", target, e)


def _proxied_get(url: str, params: dict | None, headers: dict | None, insecure: bool) -> httpx.Response:
    target = str(httpx.URL(url, params=params)) if params else url
    cached = _cached(target)
    if cached and cached[0] == f"{datetime.now(WARSAW):%Y-%m-%d}":
        log.info("proxy: %s already fetched today, reusing that copy", target)
        return cached[1]
    query = {"url": target, **({"insecure": "1"} if insecure else {})}
    resp = client().get(PROXY_URL, params=query, headers={**(headers or {}), "X-Proxy-Token": PROXY_TOKEN},
                        timeout=httpx.Timeout(70.0, connect=15.0))
    if resp.status_code == 429 and "X-Proxy-Limit" in resp.headers:  # a hard cap: retrying is pointless
        reason = resp.headers["X-Proxy-Limit"]
        if cached:
            log.warning("proxy: %s refused (%s), using the copy from %s", target, reason, cached[0])
            return cached[1]
        raise ProxyLimitReached(f"PL proxy: {reason}")
    # Make resp.url (and error messages) show the site's final URL, not the proxy's.
    resp.request = httpx.Request("GET", resp.headers.get("X-Final-Url", target))
    if resp.status_code == 200:
        _store(target, resp)
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


def gone(url: str) -> bool:
    """True only when the server says the file is not there (404/410). Errors, other statuses and
    proxied hosts (whose requests count against the monthly cap) are given the benefit of the doubt."""
    if _on_proxied_host(url):
        return False
    try:
        return client().head(url, timeout=15).status_code in (404, 410)
    except httpx.HTTPError:
        return False


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
