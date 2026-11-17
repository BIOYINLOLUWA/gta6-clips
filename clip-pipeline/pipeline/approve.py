"""Telegram approval gate. Every clip is reviewed by a human before it can be posted.

You get the TikTok cut with 4 buttons: approve with caption 1/2/3, or reject.
Telegram also stores the files (file_id), so stateless GitHub runners can post later.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from . import db, settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, files=None, **params) -> dict:
    settings.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    url = API.format(token=settings.TELEGRAM_BOT_TOKEN, method=method)
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    r = requests.post(url, data=data, files=files, timeout=300)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body["result"]


def review_text(clip: dict, source: dict) -> str:
    caps = clip["captions"] if isinstance(clip["captions"], list) else json.loads(clip["captions"])
    camp = source.get("campaign") or "own channel"
    lines = [
        f"🎬 Clip #{clip['id']}  |  score {clip['score']:.1f}",
        f"📺 {source['channel']}: {source['title'][:80]}",
        f"💰 Campaign: {camp}",
        f"🪝 Hook: {clip['hook']}",
        "",
        *[f"{i + 1}. {c}" for i, c in enumerate(caps)],
        "",
        "Check: no leaks, no cutscene/story spoilers, no graphic first 7s.",
    ]
    return "\n".join(lines)[:1024]


def keyboard(clip_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ 1", "callback_data": f"a:{clip_id}:1"},
        {"text": "✅ 2", "callback_data": f"a:{clip_id}:2"},
        {"text": "✅ 3", "callback_data": f"a:{clip_id}:3"},
        {"text": "❌", "callback_data": f"r:{clip_id}"},
    ]]}


def send_for_review(clip_id: int, long_path: Path, short_path: Path) -> None:
    clip = db.one("SELECT * FROM clips WHERE id = %s", (clip_id,))
    source = db.one("SELECT * FROM clip_sources WHERE id = %s", (clip["source_id"],))
    with open(long_path, "rb") as f:
        msg = _call("sendVideo", files={"video": f}, chat_id=settings.TELEGRAM_CHAT_ID,
                    caption=review_text(clip, source), reply_markup=keyboard(clip_id),
                    supports_streaming=True)
    with open(short_path, "rb") as f:
        short = _call("sendVideo", files={"video": f}, chat_id=settings.TELEGRAM_CHAT_ID,
                      caption=f"Shorts/Reels cut for #{clip_id}", disable_notification=True,
                      reply_parameters={"message_id": msg["message_id"]})
    db.q("UPDATE clips SET tg_long_file_id=%s, tg_short_file_id=%s, tg_message_id=%s, "
         "status='pending_review' WHERE id=%s",
         (msg["video"]["file_id"], short["video"]["file_id"], msg["message_id"], clip_id))


def parse_callback(data: str) -> tuple[str, int, int | None]:
    parts = data.split(":")
    if parts[0] == "a" and len(parts) == 3:
        return "approve", int(parts[1]), int(parts[2])
    if parts[0] == "r" and len(parts) == 2:
        return "reject", int(parts[1]), None
    raise ValueError(f"bad callback data: {data}")


def poll() -> list[int]:
    """Process button presses since last run. Returns ids of newly approved clips."""
    offset = int(db.kv_get("tg_offset", "0"))
    updates = _call("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query"])
    approved = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq or str(cq["message"]["chat"]["id"]) != str(settings.TELEGRAM_CHAT_ID):
            continue
        try:
            action, clip_id, cap = parse_callback(cq.get("data", ""))
        except ValueError:
            continue
        clip = db.one("SELECT status FROM clips WHERE id=%s", (clip_id,))
        if not clip or clip["status"] != "pending_review":
            _call("answerCallbackQuery", callback_query_id=cq["id"], text="Already handled")
            continue
        if action == "approve":
            db.q("UPDATE clips SET status='approved', chosen_caption=%s, reviewed_at=now() WHERE id=%s",
                 (cap, clip_id))
            approved.append(clip_id)
            note = f"Approved with caption {cap}, scheduling..."
        else:
            db.q("UPDATE clips SET status='rejected', reviewed_at=now() WHERE id=%s", (clip_id,))
            note = "Rejected"
        _call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        _call("editMessageReplyMarkup", chat_id=cq["message"]["chat"]["id"],
              message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
    db.kv_set("tg_offset", str(offset))
    return approved


def download(file_id: str, dest: Path) -> Path:
    info = _call("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{info['file_path']}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def notify(text: str) -> None:
    try:
        _call("sendMessage", chat_id=settings.TELEGRAM_CHAT_ID, text=text[:4000])
    except Exception as exc:
        log.warning("notify failed: %s", exc)
