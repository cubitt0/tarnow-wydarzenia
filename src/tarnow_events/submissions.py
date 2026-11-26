"""Facebook events sent in by visitors.

Anyone can paste a facebook.com/events link into a Google Form ("Dodaj wydarzenie z Facebooka"). The
form's Sheet is published as CSV (`submissions_csv` on the facebook source in sources.yaml) and read in
one request per build. Each new link is fetched once and kept only if one of the event's hosts is a
Facebook page from the research catalogue (docs/sources-catalogue.json, field `fb`) or from
`allowed_pages` on the facebook source: the whitelist.

The ledger (submissions.json) remembers every event id that was handled, accepted or not. Rows stay
in the Sheet, so an id in the ledger is never fetched again: accepted events are published from the
ledger's copy until they end, rejected ones are skipped. It is kept on the site (fetched back by the
workflow like state.json) and in the build cache, like the page archive.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger(__name__)

FILE = "submissions.json"
MAX_TRIES = 3  # unreadable this many builds in a row: rejected
SHARE_RE = re.compile(r"https?://(?:www\.|m\.|web\.)?(?:facebook\.com/share/[^\s\"',]+|fb\.me/[^\s\"',]+)")
NOT_PAGES = {"events", "people", "pages", "profile.php", "groups", "share", "watch", "photo", "permalink.php"}


def page_keys(url: str | None) -> set[str]:
    """What identifies a Facebook page in a link: its numeric id and/or its vanity name (lowercase).
    facebook.com/tarnowbema20 → {tarnowbema20}; profile.php?id=61556… → {61556…};
    /people/Name/1000…/ and /pages/Name/7212…/ → {1000…}; /Dom-Malarek-233116…/ → {dom-malarek-233116…, 233116…}."""
    if not url:
        return set()
    parts = urlsplit(url if "://" in url else "https://" + url)
    if "facebook.com" not in (parts.hostname or ""):
        return set()
    segs = [unquote(s) for s in parts.path.split("/") if s]
    keys: set[str] = set()
    if segs and segs[0] == "profile.php":
        keys.update(parse_qs(parts.query).get("id", []))
    elif segs and segs[0] in ("people", "pages"):
        keys.update(s for s in segs[1:] if s.isdigit())
    elif segs and segs[0].lower() not in NOT_PAGES:
        keys.add(segs[0].lower())
        m = re.search(r"-(\d{6,})$", segs[0])
        if m:
            keys.add(m.group(1))
    return keys


def whitelist(catalogue: list[dict], extra: list[str] | None = None) -> dict[str, str]:
    """page key → page/source name."""
    out: dict[str, str] = {}
    for rec in catalogue:
        # `fb` is the page next to a website; a Facebook-only source has its page as the main link `u`.
        for k in page_keys(rec.get("fb")) | page_keys(rec.get("u")):
            out.setdefault(k, rec.get("pn") or rec.get("n") or k)
    for url in extra or []:
        for k in page_keys(url):
            out.setdefault(k, url)
    return out


def host_match(hosts: list[dict], allowed: dict[str, str]) -> dict | None:
    """The first host whose id or profile link is on the whitelist."""
    for h in hosts:
        keys = page_keys(h.get("url")) | {str(h.get("id") or "")}
        if keys & allowed.keys():
            return h
    return None


def event_ids_in(text: str, resolve) -> set[str]:
    """Event ids in a form answer. Links shared from the app (facebook.com/share/e/…, fb.me/…) are
    followed once; `resolve(url) -> final url | None`. A login wall keeps the event in ?next=."""
    from .sources.facebook import find_event_ids

    ids = find_event_ids(unquote(text))
    if not ids:
        for short in SHARE_RE.findall(text):
            final = resolve(short)
            if final:
                ids |= find_event_ids(unquote(unquote(final)))
    return ids


def read_csv(text: str) -> list[str]:
    """Every answer cell of the published Sheet, joined per row (the timestamp column is harmless)."""
    rows = list(csv.reader(io.StringIO(text)))
    return [" ".join(r) for r in rows[1:]]


class Ledger:
    """event id → {"status": accepted|rejected|retry, "at": UTC iso, "tries", "host", "reason", "data"}."""

    def __init__(self, entries: dict[str, dict] | None = None):
        self.entries = entries or {}

    @classmethod
    def load(cls, *files: Path) -> "Ledger":
        out: dict[str, dict] = {}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:  # an HTML error page, a cut download …
                log.warning("submissions %s unreadable: %s", f, e)
                continue
            for eid, rec in (data.get("events") or {}).items():
                if eid not in out or rec.get("at", "") > out[eid].get("at", ""):
                    out[eid] = rec
        return cls(out)

    def save(self, *files: Path) -> None:
        blob = json.dumps({"events": self.entries}, ensure_ascii=False, indent=0, sort_keys=True)
        for f in files:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(blob, encoding="utf-8")

    def done(self, eid: str) -> bool:
        return self.entries.get(eid, {}).get("status") in ("accepted", "rejected")

    def accepted(self) -> dict[str, dict]:
        """Accepted events that have not ended yet (their data is kept)."""
        return {k: v for k, v in self.entries.items() if v.get("status") == "accepted" and v.get("data")}

    def _set(self, eid: str, **rec) -> None:
        self.entries[eid] = {**rec, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def accept(self, eid: str, data: dict, host: dict) -> None:
        self._set(eid, status="accepted", host=host.get("name"), data=data)

    def reject(self, eid: str, reason: str, host: str | None = None, data: dict | None = None) -> None:
        """data: kept for a host that is not on the whitelist, so adding the page later accepts the event
        at the next build without fetching it again (see recheck)."""
        self._set(eid, status="rejected", reason=reason, host=host, **({"data": data} if data else {}))

    def recheck(self, allowed: dict[str, str]) -> list[str]:
        """Rejected events whose host has since been put on the whitelist become accepted."""
        out = []
        for eid, rec in self.entries.items():
            if rec.get("status") == "rejected" and rec.get("data"):
                host = host_match(rec["data"].get("hosts") or [], allowed)
                if host:
                    self.accept(eid, rec["data"], host)
                    out.append(eid)
        return out

    def retry(self, eid: str) -> None:
        tries = self.entries.get(eid, {}).get("tries", 0) + 1
        if tries >= MAX_TRIES:
            self.reject(eid, "nie da się odczytać wydarzenia")
        else:
            self._set(eid, status="retry", tries=tries)

    def forget_data_before(self, ts: int) -> None:
        """Accepted events that ended keep their id (so the Sheet row stays skipped), not their data."""
        for rec in self.entries.values():
            d = rec.get("data")
            if d and (d.get("end") or d.get("start") or 0) < ts:
                rec.pop("data")
