"""Fetch proxy deployed as a Cloud Run function in europe-central2 (Warsaw).

Some Polish sites (municipal CMS hosting, Cloudflare, gov firewalls) refuse GitHub Actions'
Azure IPs. The scraper sends those requests here instead, so the site sees a Polish IP.
Not an open proxy: requires X-Proxy-Token and only fetches hosts from ALLOWED_HOSTS.

Hard cap: every accepted request takes one unit of MONTHLY_LIMIT, counted in Firestore
(usage/<YYYY-MM>, Warsaw time). Over the limit, or if the counter cannot be read, it refuses
with 429 and fetches nothing: an outage of the counter must never mean unlimited requests.
"""
import gzip
import hmac
import logging
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import functions_framework
import requests
from google.cloud import firestore

TOKEN = os.environ["PROXY_TOKEN"]
ALLOWED = {h.strip() for h in os.environ["ALLOWED_HOSTS"].split(",") if h.strip()}
MONTHLY_LIMIT = int(os.environ["MONTHLY_LIMIT"])
FORWARD = {"user-agent", "accept", "accept-language"}
MAX_REDIRECTS = 5

_db = None


def _allowed(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and p.hostname in ALLOWED


@firestore.transactional
def _increment(tx, ref) -> int | None:
    snap = ref.get(transaction=tx)
    used = int((snap.to_dict() or {}).get("count", 0)) if snap.exists else 0
    if used >= MONTHLY_LIMIT:
        return None
    tx.set(ref, {"count": used + 1, "limit": MONTHLY_LIMIT, "updated": firestore.SERVER_TIMESTAMP}, merge=True)
    return used + 1


def _take_quota() -> int | None:
    """Count this request; None when the month's limit is used up or the counter is unavailable."""
    global _db
    try:
        _db = _db or firestore.Client()
        month = datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m")
        return _increment(_db.transaction(), _db.collection("usage").document(month))
    except Exception:
        logging.exception("quota counter unavailable, refusing")
        return None


def _limited():
    return f"monthly limit of {MONTHLY_LIMIT} requests reached", 429, {"X-Proxy-Limit": str(MONTHLY_LIMIT)}


@functions_framework.http
def fetch(request):
    if not hmac.compare_digest(request.headers.get("X-Proxy-Token", ""), TOKEN):
        return "forbidden", 403
    url = request.args.get("url", "")
    if url != "ip" and not _allowed(url):
        return f"host not allowed: {urlparse(url).hostname}", 400
    used = _take_quota()
    if used is None:
        return _limited()
    if url == "ip":  # debug: which IP and country do sites see?
        return requests.get("https://ipinfo.io/json", timeout=10).text, 200, {"Content-Type": "application/json"}

    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD}
    verify = request.args.get("insecure") != "1"  # mirrors the scraper's insecure_tls option
    # Follow redirects by hand so a redirect cannot leave the allowlist.
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url):
            return f"host not allowed: {urlparse(url).hostname}", 400
        r = requests.get(url, headers=headers, timeout=50, allow_redirects=False, verify=verify)
        if r.is_redirect and "location" in r.headers:
            url = urljoin(url, r.headers["location"])
            continue
        body, out = r.content, {
            "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
            "X-Final-Url": url,
            "X-Proxy-Used": f"{used}/{MONTHLY_LIMIT}",
        }
        # Outbound data is the only real cost: feeds of several MB shrink ~10x with gzip.
        if "gzip" in request.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body, out["Content-Encoding"] = gzip.compress(body, 6), "gzip"
        return body, r.status_code, out
    return "too many redirects", 508
