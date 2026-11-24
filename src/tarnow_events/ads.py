"""Optional Google AdSense banner at the bottom of the calendar page.

Two steps, each switched on by an environment variable:
- TE_ADSENSE_CLIENT (ca-pub-…) alone: the site-ownership meta tag in index.html and ads.txt at the
  site root, which is what AdSense needs to verify and review the site. No ad code is loaded.
- plus TE_ADSENSE_SLOT (the ad unit's number, available after approval): the AdSense script and one
  horizontal banner above the footer.
Without them the page carries nothing ad-related at all.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

HEAD_MARK = "<!--AD-HEAD-->"
SLOT_MARK = "<!--AD-SLOT-->"
# Google's AdSense certification authority ID, the same for every publisher.
GOOGLE_TAG_ID = "f08c47fec0942fa0"


def ad_config(client: str | None, slot: str | None) -> tuple[str | None, str | None]:
    """(client, slot) that passed validation; a malformed value is dropped with a warning."""
    client, slot = (client or "").strip() or None, (slot or "").strip() or None
    if client and not re.fullmatch(r"ca-pub-\d{10,20}", client):
        log.warning("ads: TE_ADSENSE_CLIENT=%r is not ca-pub-<digits>; ads stay off this build", client)
        client = None
    if slot and not re.fullmatch(r"\d{5,20}", slot):
        log.warning("ads: TE_ADSENSE_SLOT=%r is not <digits>; no banner this build", slot)
        slot = None
    return client, slot if client else None


def apply_ads(page: str, out: Path, client: str | None, slot: str | None) -> str:
    """Fill (or blank) the ad markers in the calendar page and write/remove ads.txt."""
    client, slot = ad_config(client, slot)
    ads_txt = out / "ads.txt"
    if not client:
        ads_txt.unlink(missing_ok=True)
        return page.replace(HEAD_MARK, "").replace(SLOT_MARK, "")
    ads_txt.write_text(f"google.com, {client.removeprefix('ca-')}, DIRECT, {GOOGLE_TAG_ID}\n")
    verify = f'<meta name="google-adsense-account" content="{client}">'
    if not slot:
        log.info("ads: %s verification tag and ads.txt only (no TE_ADSENSE_SLOT, no banner)", client)
        return page.replace(HEAD_MARK, verify).replace(SLOT_MARK, "")
    head = verify + "\n" + (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}" '
            'crossorigin="anonymous"></script>\n'
            "<style>.ad-wrap{margin:24px 0 0;text-align:center;min-height:120px}"
            ".ad-wrap .ad-label{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;"
            "color:var(--on-surface-var);margin-bottom:4px}</style>")
    banner = (f'<aside class="ad-wrap" aria-label="Reklama"><span class="ad-label">Reklama</span>'
              f'<ins class="adsbygoogle" style="display:block" data-ad-client="{client}" data-ad-slot="{slot}" '
              'data-ad-format="horizontal" data-full-width-responsive="true"></ins>'
              "<script>(adsbygoogle = window.adsbygoogle || []).push({});</script></aside>")
    log.info("ads: banner %s / %s on index.html, ads.txt written", client, slot)
    return page.replace(HEAD_MARK, head).replace(SLOT_MARK, banner)
