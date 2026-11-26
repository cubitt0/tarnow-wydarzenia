"""Renders docs/SOURCES.md from docs/sources-catalogue.json. The site's zrodla.html reads the JSON itself
(planned / rejected sources with their public reason `r`, and name `pn` where `n` is a working note).

Run after editing the catalogue JSON: python3 scripts/render_catalogue.py
"""

import json
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
GROUPS = [("miasto", "Miasto i informacja turystyczna"), ("kultura", "Kultura"), ("kino", "Kina"),
          ("lokale", "Lokale: kluby, puby, restauracje, hotele"), ("bilety", "Bilety i agregatory"),
          ("media", "Media lokalne"), ("sport", "Sport i bieganie"), ("religia", "Religia"),
          ("edukacja", "Edukacja, NGO, meetupy"), ("region", "Region do 50 km")]
ORDER = {"aktywne": 0, "planowane": 1, "tylko FB/IG": 2, "odrzucone": 3}


def main() -> None:
    recs = json.loads((DOCS / "sources-catalogue.json").read_text(encoding="utf-8"))
    out = ["# Katalog źródeł wydarzeń – Tarnów + 50 km", "",
           f"Stan na {date.today().isoformat()}. Status: **aktywne** = pobierane (albo pokryte przez agregator), "
           "**planowane** = do zrobienia, **tylko FB/IG** = brak strony z wydarzeniami, "
           "**odrzucone** = martwe / zablokowane / poza zasięgiem / nie są wydarzeniami.", ""]
    for g, label in GROUPS:
        rows = sorted((r for r in recs if r["g"] == g), key=lambda r: (ORDER[r["s"]], r.get("d") or 0, r["n"]))
        out += [f"## {label} ({len(rows)})", "", "| Źródło | Miejscowość | Typ | Status | Uwagi |", "|---|---|---|---|---|"]
        for r in rows:
            town = r.get("t", "") + (f" ({r['d']} km)" if r.get("d") else "")
            extra = []
            if r.get("fu"):
                extra.append(f"[feed]({r['fu']})")
            if r.get("fb"):
                extra.append(f"[FB]({r['fb']})")
            if r.get("ids"):
                extra.append("id: " + ", ".join(r["ids"]))
            name = r["n"].replace("|", "/")
            name = f"[{name}]({r['u']})" if r.get("u") else name
            out.append(f"| {name} | {town} | `{r['f']}` | {r['s']} | {' · '.join(extra)} {r.get('o', '').replace('|', '/')} |")
        out.append("")
    (DOCS / "SOURCES.md").write_text("\n".join(out), encoding="utf-8")

    print(f"{len(recs)} sources → docs/SOURCES.md")


if __name__ == "__main__":
    main()
