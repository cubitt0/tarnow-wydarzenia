from __future__ import annotations

import argparse
import json
import logging
import time

from .config import Settings, load_sources
from .pipeline import run
from .sources import REGISTRY


def main() -> None:
    ap = argparse.ArgumentParser(prog="tarnow-events")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="fetch all sources and write the .ics feeds")
    r.add_argument("--only", help="comma-separated source ids")
    r.add_argument("--no-ai", action="store_true", help="skip Gemini-based sources")
    r.add_argument("--every", type=int, default=0, help="repeat every N seconds (docker mode)")

    c = sub.add_parser("check", help="run one source and print what it returns (no output files)")
    c.add_argument("source_id")

    sub.add_parser("list", help="list configured sources")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()

    if args.cmd == "list":
        for s in load_sources(settings.sources_file):
            print(f"{'+' if s.enabled else '-'} {s.id:34} {s.type:8} {s.group:12} {s.url}")
    elif args.cmd == "check":
        cfg = next(s for s in load_sources(settings.sources_file) if s.id == args.source_id)
        for ev in REGISTRY[cfg.type](cfg, settings).fetch():
            print(json.dumps({
                "title": ev.title, "start": ev.start.isoformat(), "all_day": ev.all_day,
                "end": ev.end.isoformat() if ev.end else None, "where": ev.location_line(),
                "url": ev.source.url, "cat": ev.categories,
            }, ensure_ascii=False))
    elif args.cmd == "run":
        only = set(args.only.split(",")) if args.only else None
        while True:
            run(settings, only=only, use_ai=not args.no_ai)
            if not args.every:
                break
            time.sleep(args.every)


if __name__ == "__main__":
    main()
