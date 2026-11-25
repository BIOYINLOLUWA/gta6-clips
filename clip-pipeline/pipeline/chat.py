"""Chat replay for a VOD, as (seconds, text) pairs. Chat speed is the best signal for funny moments.

Twitch: public GQL comments endpoint (the one Twitch's own player uses).
YouTube: yt-dlp live chat replay.
Kick and anything else: no chat, the caller falls back to loudness only.
Every path is best effort: on any error it returns [] and the run carries on.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

# Words chat spams when something is funny or hype. Weighted extra in chat speed.
HYPE_WORDS = re.compile(
    r"(lmao+|lmfao|lol+|kekw|omegalul|lul+|icant|💀|😂|🤣|clip (it|that)|clipit|w+\b|\bl\b|holy|"
    r"no way|wtf|pog+|pogchamp|ayo+|bro+|nah+|dead|crying|insane|omg)", re.I)


def chat_weight(text: str) -> float:
    return 1.0 + min(len(HYPE_WORDS.findall(text)), 3) * 0.7


def twitch_video_id(url: str) -> str | None:
    m = re.search(r"twitch\.tv/videos/(\d+)", url)
    return m.group(1) if m else None


def twitch_chat(video_id: str, budget_s: float = 300, max_pages: int = 2500) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    variables: dict = {"videoID": video_id, "contentOffsetSeconds": 0}
    t0 = time.time()
    s = requests.Session()
    for _ in range(max_pages):
        if time.time() - t0 > budget_s:
            log.warning("twitch chat: time budget hit after %d messages", len(out))
            break
        body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}}}]
        r = s.post(TWITCH_GQL, headers=headers, data=json.dumps(body), timeout=20)
        r.raise_for_status()
        data = r.json()[0]
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:200])
        comments = ((data.get("data") or {}).get("video") or {}).get("comments") or {}
        edges = comments.get("edges") or []
        for e in edges:
            n = e.get("node") or {}
            frags = ((n.get("message") or {}).get("fragments")) or []
            text = "".join(f.get("text") or "" for f in frags)
            out.append((float(n.get("contentOffsetSeconds") or 0), text))
        page = comments.get("pageInfo") or {}
        if not edges or not page.get("hasNextPage"):
            break
        variables = {"videoID": video_id, "cursor": edges[-1].get("cursor")}
    return out


def twitch_viewer_clips(video_id: str) -> list[tuple[float, str]]:
    """Clips viewers made during this VOD (official Helix API). Each one is an audience vote for that
    moment, so it is turned into a burst of weighted 'messages' at the clip's position, more for
    clips with more views. Needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET."""
    import math
    import os
    from datetime import datetime, timedelta

    cid, secret = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not cid or not secret:
        log.info("twitch viewer clips: no TWITCH_CLIENT_ID/SECRET, skipped")
        return []
    tok = requests.post("https://id.twitch.tv/oauth2/token", timeout=20, params={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}).json()["access_token"]
    h = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
    v = requests.get("https://api.twitch.tv/helix/videos", params={"id": video_id}, headers=h, timeout=20).json()["data"][0]
    start = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
    m = re.findall(r"(\d+)([hms])", v.get("duration", ""))
    secs = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in m) or 12 * 3600
    end = start + timedelta(seconds=secs + 3600)
    out, cursor = [], None
    for _ in range(20):
        params = {"broadcaster_id": v["user_id"], "started_at": start.isoformat().replace("+00:00", "Z"),
                  "ended_at": end.isoformat().replace("+00:00", "Z"), "first": 100}
        if cursor:
            params["after"] = cursor
        r = requests.get("https://api.twitch.tv/helix/clips", params=params, headers=h, timeout=20).json()
        for c in r.get("data", []):
            if c.get("video_id") == video_id and c.get("vod_offset") is not None:
                t = float(c["vod_offset"]) + float(c.get("duration") or 30) * 0.6
                votes = 3 + min(int(math.log2(int(c.get("view_count") or 0) + 1) * 3), 45)
                out += [(t, "CLIPIT " + (c.get("title") or ""))] * votes
        cursor = (r.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    log.info("twitch viewer clips: %d vote-messages from viewer clips", len(out))
    return out


def youtube_chat(url: str, work: Path) -> list[tuple[float, str]]:
    from .source import _ydl

    work.mkdir(parents=True, exist_ok=True)
    opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
            "outtmpl": str(work / "chat.%(ext)s")}
    with _ydl(opts) as y:
        y.download([url])
    f = next(work.glob("chat*.json"), None)
    if not f:
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
            act = d["replayChatItemAction"]
            ms = float(act.get("videoOffsetTimeMsec", 0))
            for a in act.get("actions", []):
                item = (a.get("addChatItemAction") or {}).get("item", {})
                r = item.get("liveChatTextMessageRenderer")
                if not r:
                    continue
                text = "".join(run.get("text", "") or run.get("emoji", {}).get("shortcuts", [""])[0]
                               for run in r.get("message", {}).get("runs", []))
                out.append((ms / 1000.0, text))
        except Exception:
            continue
    return out


def fetch(url: str, work: Path) -> list[tuple[float, str]]:
    try:
        vid = twitch_video_id(url)
        if vid:
            msgs = []
            try:
                msgs = twitch_chat(vid)  # often refused now ("failed integrity check")
            except Exception as exc:
                log.info("twitch chat replay unavailable: %s", str(exc)[:120])
            msgs += twitch_viewer_clips(vid)
        elif "youtube.com" in url or "youtu.be" in url:
            msgs = youtube_chat(url, work / "chat")
        else:
            msgs = []
        log.info("chat: %d messages for %s", len(msgs), url)
        return msgs
    except Exception as exc:
        log.warning("chat unavailable for %s, using loudness only: %s", url, str(exc)[:200])
        return []


def rate_curve(msgs: list[tuple[float, str]], duration: int) -> list[float]:
    """Hype-weighted chat messages per second."""
    curve = [0.0] * max(duration, 1)
    for t, text in msgs:
        i = int(t)
        if 0 <= i < len(curve):
            curve[i] += chat_weight(text)
    return curve


def top_messages(msgs: list[tuple[float, str]], start: float, end: float, n: int = 12) -> list[str]:
    """A sample of what chat said in a window (for Claude to see the reaction)."""
    inside = [m for t, m in msgs if start <= t <= end and m.strip()]
    inside.sort(key=chat_weight, reverse=True)
    return [m[:60] for m in inside[:n]]
