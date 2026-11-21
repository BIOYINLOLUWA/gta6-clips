"""Who's hot: list Twitch streamers currently playing GTA 6 with 500+ live viewers.

Runs every few hours. Keeps a rolling average of each streamer's viewers across runs and
sends a Telegram alert, flagging streamers you have not seen before. This only tells you
who to look at. Clip a streamer only after you have joined their Whop campaign.
"""
from __future__ import annotations

import json
import logging
import os

import requests

from . import approve, db

log = logging.getLogger(__name__)
MIN_VIEWERS = int(os.environ.get("HOT_MIN_VIEWERS", "500"))
GAME_NAME = os.environ.get("HOT_GAME_NAME", "Grand Theft Auto VI")
TOP_N = 15


def _token() -> str:
    cid, secret = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not cid or not secret:
        raise RuntimeError("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (free at dev.twitch.tv/console)")
    r = requests.post("https://id.twitch.tv/oauth2/token", timeout=30, data={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def _get(path: str, token: str, **params) -> list[dict]:
    r = requests.get(f"https://api.twitch.tv/helix/{path}", params=params, timeout=30, headers={
        "Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json().get("data", [])


def find_game(token: str) -> dict | None:
    exact = _get("games", token, name=GAME_NAME)
    if exact:
        return exact[0]
    for g in _get("search/categories", token, query=GAME_NAME, first=10):
        if g["name"].lower().strip() == GAME_NAME.lower():
            return g
    return None


def live_streams(token: str, game_id: str) -> list[dict]:
    return _get("streams", token, game_id=game_id, first=100, type="live")


def update_stats(streams: list[dict], stats: dict) -> list[dict]:
    """Fold this run's viewer counts into rolling averages. Returns hot streamers, best first."""
    hot = []
    for s in streams:
        login = s["user_login"]
        st = stats.get(login) or {"n": 0, "sum": 0, "name": s["user_name"]}
        is_new = st["n"] == 0
        st["n"] += 1
        st["sum"] += s["viewer_count"]
        st["name"] = s["user_name"]
        stats[login] = st
        avg = st["sum"] / st["n"]
        if s["viewer_count"] >= MIN_VIEWERS or avg >= MIN_VIEWERS:
            hot.append({"login": login, "name": st["name"], "now": s["viewer_count"],
                        "avg": avg, "runs": st["n"], "new": is_new, "title": s.get("title", "")})
    hot.sort(key=lambda h: h["now"], reverse=True)
    return hot


def alert_text(hot: list[dict]) -> str:
    lines = [f"🔥 Who's hot on GTA 6 right now ({len(hot)} streamers over {MIN_VIEWERS} viewers)", ""]
    for h in hot[:TOP_N]:
        tag = " 🆕" if h["new"] else ""
        avg = f", avg {h['avg']:.0f} over {h['runs']} checks" if h["runs"] > 1 else ""
        lines.append(f"{h['name']}{tag}: {h['now']:,} now{avg}\nhttps://twitch.tv/{h['login']}")
    lines += ["", "Join their Whop campaign before clipping. No campaign means no pay and a takedown risk."]
    return "\n".join(lines)[:4000]


def run() -> int:
    token = _token()
    game = find_game(token)
    if not game:
        raise RuntimeError(f"Twitch has no category named {GAME_NAME!r}; set HOT_GAME_NAME")
    stats = json.loads(db.kv_get("hot_stats", "{}"))
    hot = update_stats(live_streams(token, game["id"]), stats)
    db.kv_set("hot_stats", json.dumps(stats))
    log.info("%d streamers over %d viewers", len(hot), MIN_VIEWERS)
    if hot:
        approve.notify(alert_text(hot))
    return len(hot)
