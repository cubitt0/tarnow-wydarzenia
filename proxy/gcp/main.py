"""Fetch proxy deployed as a Cloud Run function in europe-central2 (Warsaw).

Some Polish sites (municipal CMS hosting, Cloudflare, gov firewalls) refuse GitHub Actions'
Azure IPs. The scraper sends those requests here instead, so the site sees a Polish IP.
Not an open proxy: requires X-Proxy-Token and only fetches hosts from ALLOWED_HOSTS.

Hard caps, counted in Firestore in one transaction (Warsaw time):
- DAILY_PER_HOST accepted requests per site host per day (daily/<YYYY-MM-DD>_<host>), so each
  source is fetched at most once a day however many builds run; the scraper reuses its copy.
- MONTHLY_LIMIT accepted requests per month in total (usage/<YYYY-MM>).
Over a limit, or if the counter cannot be read, it refuses with 429 and fetches nothing: an
outage of the counter must never mean unlimited requests. Refusals do not count.
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
DAILY_PER_HOST = int(os.environ.get("DAILY_PER_HOST", "1"))
FORWARD = {"user-agent", "accept", "accept-language"}
MAX_REDIRECTS = 5

_db = None


def _allowed(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and p.hostname in ALLOWED


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _count(snap) -> int:
    return int((snap.to_dict() or {}).get("count", 0)) if snap.exists else 0


@firestore.transactional
def _increment(tx, month_ref, day_ref) -> tuple[str | None, int]:
    """(None, used this month) when the request may go; (refusal reason, 0) when it may not."""
    used = _count(month_ref.get(transaction=tx))
    if used >= MONTHLY_LIMIT:
        return f"monthly limit of {MONTHLY_LIMIT} requests reached", 0
    if day_ref is not None:
        today = _count(day_ref.get(transaction=tx))
        if today >= DAILY_PER_HOST:
            return f"daily limit of {DAILY_PER_HOST} request(s) for this host reached", 0
        tx.set(day_ref, {"count": today + 1, "updated": firestore.SERVER_TIMESTAMP})
    tx.set(month_ref, {"count": used + 1, "limit": MONTHLY_LIMIT, "updated": firestore.SERVER_TIMESTAMP}, merge=True)
    return None, used + 1


def _take_quota(url: str) -> tuple[str | None, int]:
    """Count this request against the monthly cap and, for a site URL, its host's daily cap."""
    global _db
    try:
        _db = _db or firestore.Client()
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        month_ref = _db.collection("usage").document(now.strftime("%Y-%m"))
        day_ref = None if url == "ip" else _db.collection("daily").document(f"{now:%Y-%m-%d}_{_host(url)}")
        return _increment(_db.transaction(), month_ref, day_ref)
    except Exception:
        logging.exception("quota counter unavailable, refusing")
        return "quota counter unavailable", 0


def _limited(reason: str):
    return reason, 429, {"X-Proxy-Limit": reason}


@functions_framework.http
def fetch(request):
    if not hmac.compare_digest(request.headers.get("X-Proxy-Token", ""), TOKEN):
        return "forbidden", 403
    url = request.args.get("url", "")
    if url != "ip" and not _allowed(url):
        return f"host not allowed: {urlparse(url).hostname}", 400
    refused, used = _take_quota(url)
    if refused:
        return _limited(refused)
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
