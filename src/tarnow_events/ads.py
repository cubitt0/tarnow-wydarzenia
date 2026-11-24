"""Optional Google AdSense banner at the bottom of the calendar page.

Off unless both TE_ADSENSE_CLIENT (ca-pub-…) and TE_ADSENSE_SLOT (the ad unit's number) are set:
then the build puts the AdSense script and one horizontal banner into index.html and writes
ads.txt, which AdSense requires at the site root. Without them the page carries no ad code at all.
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


def ad_config(client: str | None, slot: str | None) -> tuple[str, str] | None:
    client, slot = (client or "").strip(), (slot or "").strip()
    if not client and not slot:
        return None
    if not re.fullmatch(r"ca-pub-\d{10,20}", client) or not re.fullmatch(r"\d{5,20}", slot):
        log.warning("ads: TE_ADSENSE_CLIENT=%r / TE_ADSENSE_SLOT=%r not in the expected form "
                    "(ca-pub-<digits> / <digits>); no banner this build", client, slot)
        return None
    return client, slot


def apply_ads(page: str, out: Path, client: str | None, slot: str | None) -> str:
    """Fill (or blank) the ad markers in the calendar page and write/remove ads.txt."""
    cfg = ad_config(client, slot)
    ads_txt = out / "ads.txt"
    if cfg is None:
        ads_txt.unlink(missing_ok=True)
        return page.replace(HEAD_MARK, "").replace(SLOT_MARK, "")
    client, slot = cfg
    ads_txt.write_text(f"google.com, {client.removeprefix('ca-')}, DIRECT, {GOOGLE_TAG_ID}\n")
    head = (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}" '
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
