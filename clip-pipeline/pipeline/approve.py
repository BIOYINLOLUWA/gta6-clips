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


def _safe(method: str, **params) -> None:
    """Best-effort Telegram call: a late button reply must never crash the review run."""
    try:
        _call(method, **params)
    except Exception as exc:
        log.warning("%s skipped: %s", method, exc)


def review_text(clip: dict, source: dict) -> str:
    caps = clip["captions"] if isinstance(clip["captions"], list) else json.loads(clip["captions"])
    camp = source.get("campaign") or "own channel"
    lines = [
        f"🎬 Clip #{clip['id']}  |  score {clip['score']:.1f}",
        f"📺 {source['channel']}: {source['title'][:80]}",
        f"💰 Campaign: {camp}",
        "",
        *[f"{i + 1}. {c}" for i, c in enumerate(caps)],
        "",
        "✏️ Your own caption: reply to this video with your text, then tap ✅ 1. (Your caption shows on screen for the first 4s.)",
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
    if Path(short_path) == Path(long_path):  # one cut fits every platform
        short = msg
    else:
        with open(short_path, "rb") as f:
            short = _call("sendVideo", files={"video": f}, chat_id=settings.TELEGRAM_CHAT_ID,
                          caption=f"Shorts/Reels cut for #{clip_id}", disable_notification=True,
                          reply_parameters={"message_id": msg["message_id"]})
    db.q("UPDATE clips SET tg_long_file_id=%s, tg_short_file_id=%s, tg_message_id=%s, "
         "status='pending_review' WHERE id=%s",
         (msg["video"]["file_id"], short["video"]["file_id"], msg["message_id"], clip_id))


def send_tiktok_manual(clip_id: int, video: Path, caption: str) -> None:
    """Send the TikTok cut as a file (saves to phone at full quality) plus a copy-ready caption."""
    with open(video, "rb") as f:
        _call("sendDocument", files={"document": f}, chat_id=settings.TELEGRAM_CHAT_ID,
              caption=f"🎵 TikTok clip #{clip_id}: save this file, then post it on TikTok.")
    _call("sendMessage", chat_id=settings.TELEGRAM_CHAT_ID,
          text=f"Caption for TikTok (clip #{clip_id}), copy this:\n\n{caption}"[:4000])


def parse_callback(data: str) -> tuple[str, int, int | None]:
    parts = data.split(":")
    if parts[0] == "a" and len(parts) == 3:
        return "approve", int(parts[1]), int(parts[2])
    if parts[0] == "r" and len(parts) == 2:
        return "reject", int(parts[1]), None
    raise ValueError(f"bad callback data: {data}")


def apply_caption_reply(msg: dict) -> bool:
    """A text reply to a clip's video replaces caption 1 with the user's own words."""
    text = (msg.get("text") or "").strip()
    reply = msg.get("reply_to_message")
    if not text or not reply:
        return False
    clip = db.one("SELECT * FROM clips WHERE tg_message_id=%s", (reply["message_id"],))
    if not clip or clip["status"] != "pending_review":
        return False
    caps = clip["captions"] if isinstance(clip["captions"], list) else json.loads(clip["captions"])
    clip["captions"] = [text[:2200]] + list(caps)[:2]
    db.q("UPDATE clips SET captions=%s WHERE id=%s", (db.as_json(clip["captions"]), clip["id"]))
    source = db.one("SELECT * FROM clip_sources WHERE id=%s", (clip["source_id"],))
    try:
        _call("editMessageCaption", chat_id=settings.TELEGRAM_CHAT_ID, message_id=clip["tg_message_id"],
              caption=review_text(clip, source), reply_markup=keyboard(clip["id"]))
    except Exception as exc:  # Telegram refuses an edit that changes nothing
        log.warning("caption refresh failed: %s", exc)
    _call("sendMessage", chat_id=settings.TELEGRAM_CHAT_ID, reply_parameters={"message_id": msg["message_id"]},
          text=f"✏️ Caption 1 for clip #{clip['id']} is now your text. Tap ✅ 1 to approve.")
    return True


def poll() -> list[int]:
    """Process button presses since last run. Returns ids of newly approved clips."""
    offset = int(db.kv_get("tg_offset", "0"))
    updates = _call("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query", "message"])
    approved = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message")
        if msg and str(msg["chat"]["id"]) == str(settings.TELEGRAM_CHAT_ID):
            apply_caption_reply(msg)
            continue
        cq = u.get("callback_query")
        if not cq or str(cq["message"]["chat"]["id"]) != str(settings.TELEGRAM_CHAT_ID):
            continue
        try:
            action, clip_id, cap = parse_callback(cq.get("data", ""))
        except ValueError:
            continue
        clip = db.one("SELECT status FROM clips WHERE id=%s", (clip_id,))
        if not clip or clip["status"] != "pending_review":
            _safe("answerCallbackQuery", callback_query_id=cq["id"], text="Already handled")
            continue
        if action == "approve":
            db.q("UPDATE clips SET status='approved', chosen_caption=%s, reviewed_at=now() WHERE id=%s",
                 (cap, clip_id))
            approved.append(clip_id)
            note = f"Approved with caption {cap}, scheduling..."
        else:
            db.q("UPDATE clips SET status='rejected', reviewed_at=now() WHERE id=%s", (clip_id,))
            note = "Rejected"
        _safe("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        _safe("editMessageReplyMarkup", chat_id=cq["message"]["chat"]["id"],
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
