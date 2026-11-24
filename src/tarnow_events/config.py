from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SourceConfig:
    id: str
    name: str
    type: str  # ics | rss | tribe | jsonld | html_ai | facebook
    url: str
    category: str = "inne"  # default category when the source gives none
    group: str = "inne"  # catalogue group, e.g. "kultura", "venue", "bilety"
    town: str | None = None  # default town when the event has none
    location: dict[str, Any] | None = None  # default venue {name, address, lat, lon}
    priority: int = 50  # organiser 80, venue 70, municipal 60, aggregator 40, media 30
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Settings:
    root: Path
    out_dir: Path
    cache_dir: Path
    sources_file: Path
    gemini_api_key: str | None
    gemini_models: list[str]
    ai_max_calls: int
    ai_daily_limit: int
    radius_km: float
    days_back: int
    days_ahead: int
    public_base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.environ.get("TE_ROOT", Path.cwd()))
        return cls(
            root=root,
            out_dir=Path(os.environ.get("TE_OUT_DIR", root / "public")),
            cache_dir=Path(os.environ.get("TE_CACHE_DIR", root / "cache")),
            sources_file=Path(os.environ.get("TE_SOURCES", root / "sources.yaml")),
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            # Free-tier ladder: the lite models carry ~500 requests/day, the rest ~20.
            gemini_models=os.environ.get(
                "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
            ).split(","),
            ai_max_calls=int(os.environ.get("TE_AI_MAX_CALLS", "60")),
            # Hard cap per calendar day across all runs. Free tier for flash-lite is ~500/day;
            # this keeps the project at a fraction of the key's quota.
            ai_daily_limit=int(os.environ.get("TE_AI_DAILY_LIMIT", "60")),
            radius_km=float(os.environ.get("TE_RADIUS_KM", "50")),
            days_back=int(os.environ.get("TE_DAYS_BACK", "7")),
            days_ahead=int(os.environ.get("TE_DAYS_AHEAD", "365")),
            public_base_url=os.environ.get("TE_PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
        )


def load_sources(path: Path) -> list[SourceConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[SourceConfig] = []
    seen: set[str] = set()
    for item in raw.get("sources", []):
        known = {k: item.pop(k) for k in list(item) if k in SourceConfig.__dataclass_fields__}
        known.setdefault("options", {}).update(item)  # unknown keys become options
        cfg = SourceConfig(**known)
        if cfg.id in seen:
            raise ValueError(f"duplicate source id {cfg.id!r} in {path}")
        seen.add(cfg.id)
        out.append(cfg)
    return out
