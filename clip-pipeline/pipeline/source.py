"""Discover new uploads/VODs on the watchlist and download only what is needed.

Long VODs are never downloaded in full: audio is pulled first for moment detection,
then only the chosen sections are downloaded as video.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from . import db, leak_guard, settings

log = logging.getLogger(__name__)


def _ydl(opts: dict):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    return yt_dlp.YoutubeDL({**base, **opts})


def list_recent(src: settings.Source, lookback_hours: int) -> list[dict]:
    """Return recent entries (url, title, description, duration) for a channel/VOD page."""
    cutoff = settings.now_utc() - timedelta(hours=lookback_hours)
    with _ydl({"extract_flat": "in_playlist", "playlistend": 4}) as y:
        info = y.extract_info(src.url, download=False)
    entries = info.get("entries") or [info]
    out = []
    for e in entries:
        url = e.get("url") or e.get("webpage_url")
        if not url:
            continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        if ts is None:  # flat listings often omit dates; fetch the full entry
            try:
                with _ydl({}) as y:
                    e = y.extract_info(url, download=False)
            except Exception as exc:  # age-restricted or removed video: skip it, keep the rest
                log.warning("skipping %s: %s", url, str(exc)[:120])
                continue
            ts = e.get("timestamp") or e.get("release_timestamp")
        if ts and ts < cutoff.timestamp():
            continue
        out.append({"url": e.get("webpage_url") or url, "title": e.get("title") or "",
                    "description": e.get("description") or "", "duration": e.get("duration")})
    return out


def discover(wl: settings.Watchlist) -> int:
    """Insert new sources into clip_sources, applying the leak guard. Returns count allowed."""
    allowed = 0
    for src in wl.sources:
        try:
            entries = list_recent(src, wl.lookback_hours)
        except Exception as exc:  # one bad channel must not stop the run
            log.warning("listing %s failed: %s", src.name, exc)
            continue
        for e in entries:
            reason = leak_guard.check(e["title"], e["description"],
                                      official=src.official, pre_launch_ok=src.pre_launch_ok)
            if not reason and e["duration"] and e["duration"] > wl.max_source_hours * 3600:
                reason = f"longer than {wl.max_source_hours}h"
            row = db.one(
                "INSERT INTO clip_sources(url, channel, title, campaign, layout, status, block_reason, duration_s) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING RETURNING id",
                (e["url"], src.name, e["title"], src.campaign, src.layout,
                 "blocked" if reason else "new", reason, e["duration"]),
            )
            if row and not reason:
                allowed += 1
                log.info("queued: %s | %s", src.name, e["title"])
            elif row:
                log.info("BLOCKED (%s): %s | %s", reason, src.name, e["title"])
    return allowed


def download_audio(url: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bestaudio/best", "outtmpl": str(dest / "audio.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}
    with _ydl(opts) as y:
        y.download([url])
    return next(dest.glob("audio.*"))


def download_section(url: str, start: float, end: float, dest: Path, name: str) -> Path:
    from yt_dlp.utils import download_range_func

    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]",
        "merge_output_format": "mp4",
        "outtmpl": str(dest / f"{name}.%(ext)s"),
        "download_ranges": download_range_func(None, [(max(0, start), end)]),
        "force_keyframes_at_cuts": True,
    }
    with _ydl(opts) as y:
        y.download([url])
    return next(dest.glob(f"{name}.*"))
