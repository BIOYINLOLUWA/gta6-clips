"""Environment + watchlist config. All secrets come from env vars (GitHub Actions secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.environ.get("CLIP_WORK_DIR", ROOT / "work"))
FONTS_DIR = Path(os.environ.get("CLIP_FONTS_DIR", ROOT / "fonts"))

LAUNCH_AT = datetime.fromisoformat(os.environ.get("GTA6_LAUNCH_AT", "2026-11-19T00:00:00+00:00"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UPLOAD_POST_API_KEY = os.environ.get("UPLOAD_POST_API_KEY", "")
UPLOAD_POST_USER = os.environ.get("UPLOAD_POST_USER", "")       # profile name in Upload-Post
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")     # optional: better hooks/captions
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
POST_TIMEZONE = os.environ.get("POST_TIMEZONE", "Europe/London")


@dataclass
class Source:
    name: str
    url: str
    layout: str = "blur"
    campaign: str | None = None
    pre_launch_ok: bool = False
    official: bool = False


@dataclass
class Watchlist:
    sources: list[Source]
    lookback_hours: int = 48
    max_source_hours: int = 8
    clips_per_source: int = 4
    long_cut_s: int = 68
    short_cut_s: int = 40
    extra: dict = field(default_factory=dict)


def load_watchlist(path: Path | None = None) -> Watchlist:
    path = path or ROOT / "config" / "watchlist.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    sources = [Source(**s) for s in (data.get("sources") or [])]
    s = data.get("settings") or {}
    wl = Watchlist(sources=sources, **{k: v for k, v in s.items() if k in Watchlist.__dataclass_fields__})
    if wl.long_cut_s <= 60:
        raise ValueError("long_cut_s must be over 60s to qualify for TikTok Creator Rewards")
    return wl


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_pre_launch(at: datetime | None = None) -> bool:
    return (at or now_utc()) < LAUNCH_AT


def require(*names: str) -> None:
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
