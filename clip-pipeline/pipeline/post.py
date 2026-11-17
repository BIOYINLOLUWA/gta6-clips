"""Schedule approved clips on TikTok, YouTube Shorts and Instagram Reels via Upload-Post.

Upload-Post: POST https://api.upload-post.com/api/upload, header "Authorization: Apikey <key>",
multipart fields video, title, user, platform[], optional scheduled_date (ISO-8601) + timezone.
Swap `upload()` for another scheduler's API if you pick a different one.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import approve, db, leak_guard, settings

log = logging.getLogger(__name__)
UPLOAD_URL = "https://api.upload-post.com/api/upload"

# Local posting times per platform (POST_TIMEZONE). UK evening + US afternoon/evening overlap.
SLOTS = {
    "tiktok":    ["07:30", "12:30", "16:30", "19:00", "21:30", "23:30"],
    "youtube":   ["08:00", "13:00", "17:00", "20:00", "22:30"],
    "instagram": ["12:00", "18:00", "21:00"],
}
MIN_LEAD = timedelta(minutes=20)


def next_slot(platform: str, taken: set[datetime], now: datetime, tz: ZoneInfo) -> datetime:
    """First slot for `platform` at least MIN_LEAD after `now` that isn't already taken."""
    local_now = now.astimezone(tz)
    for day in range(0, 14):
        d = (local_now + timedelta(days=day)).date()
        for hhmm in SLOTS[platform]:
            h, m = map(int, hhmm.split(":"))
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if slot >= now + MIN_LEAD and slot not in taken:
                return slot
    raise RuntimeError(f"no free {platform} slot in the next 14 days")


def taken_slots(platform: str, tz: ZoneInfo) -> set[datetime]:
    rows = db.q("SELECT scheduled_for FROM clips WHERE status='scheduled' AND scheduled_for ? %s",
                (platform,))
    out = set()
    for r in rows:
        sf = r["scheduled_for"] if isinstance(r["scheduled_for"], dict) else json.loads(r["scheduled_for"])
        out.add(datetime.fromisoformat(sf[platform]).astimezone(tz))
    return out


def youtube_title(caption: str) -> str:
    base = caption.split("#")[0].strip() or "GTA 6 moment"
    return f"{base[:88]} #shorts"


def upload(video: Path, platform: str, title: str, when: datetime, description: str = "") -> dict:
    settings.require("UPLOAD_POST_API_KEY", "UPLOAD_POST_USER")
    data = [("user", settings.UPLOAD_POST_USER), ("platform[]", platform), ("title", title),
            ("scheduled_date", when.isoformat()), ("timezone", settings.POST_TIMEZONE)]
    if description:
        data.append(("description", description))
    with open(video, "rb") as f:
        r = requests.post(UPLOAD_URL, headers={"Authorization": f"Apikey {settings.UPLOAD_POST_API_KEY}"},
                          data=data, files={"video": f}, timeout=600)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
    if r.status_code >= 400:
        raise RuntimeError(f"Upload-Post {platform} {r.status_code}: {body}")
    return body


def schedule_clip(clip_id: int) -> dict:
    clip = db.one("SELECT * FROM clips WHERE id=%s", (clip_id,))
    source = db.one("SELECT * FROM clip_sources WHERE id=%s", (clip["source_id"],))
    caps = clip["captions"] if isinstance(clip["captions"], list) else json.loads(clip["captions"])
    caption = caps[(clip["chosen_caption"] or 1) - 1]

    # Second leak check on what will actually be published.
    reason = leak_guard.check(f"{caption} {clip['hook']}", official=True, pre_launch_ok=True)
    if reason:
        raise RuntimeError(f"caption blocked by leak guard: {reason}")

    tz = ZoneInfo(settings.POST_TIMEZONE)
    now = settings.now_utc()
    work = settings.WORK_DIR / f"post_{clip_id}"
    long_v = approve.download(clip["tg_long_file_id"], work / "long.mp4")
    short_v = approve.download(clip["tg_short_file_id"], work / "short.mp4")

    credit = f"\nClip from {source['channel']}" if source.get("campaign") else ""
    plan = {
        "tiktok": (long_v, caption, ""),
        "youtube": (short_v, youtube_title(caption), caption + credit),
        "instagram": (short_v, caption + credit, ""),
    }
    when, responses = {}, {}
    for platform, (video, title, desc) in plan.items():
        slot = next_slot(platform, taken_slots(platform, tz), now, tz)
        responses[platform] = upload(video, platform, title, slot, desc)
        when[platform] = slot.isoformat()
    db.q("UPDATE clips SET status='scheduled', scheduled_for=%s, post_response=%s WHERE id=%s",
         (db.as_json(when), db.as_json(responses), clip_id))
    return when


def schedule_approved() -> int:
    done = 0
    for row in db.q("SELECT id FROM clips WHERE status='approved' ORDER BY score DESC"):
        try:
            when = schedule_clip(row["id"])
            pretty = ", ".join(f"{p} {datetime.fromisoformat(t):%a %H:%M}" for p, t in when.items())
            approve.notify(f"📅 Clip #{row['id']} scheduled: {pretty}")
            done += 1
        except Exception as exc:
            log.exception("scheduling clip %s failed", row["id"])
            db.q("UPDATE clips SET status='failed', error=%s WHERE id=%s", (str(exc)[:2000], row["id"]))
            approve.notify(f"⚠️ Clip #{row['id']} failed to schedule: {str(exc)[:300]}")
    return done
