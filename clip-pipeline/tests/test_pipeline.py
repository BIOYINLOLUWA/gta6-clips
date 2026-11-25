import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pipeline import approve, detect, leak_guard, post, render, settings

PRE = datetime(2026, 11, 14, tzinfo=timezone.utc)
POST = datetime(2026, 11, 20, tzinfo=timezone.utc)


# ---------- leak guard ----------
@pytest.mark.parametrize("title", [
    "GTA 6 LEAKED gameplay 4K", "cyberleek drop #8", "GTA VI unboxing early copy",
    "I got the game early!!", "GTA 6 pre-release footage",
])
def test_leak_terms_always_blocked(title):
    assert leak_guard.check(title, official=True, pre_launch_ok=True, at=PRE)
    assert leak_guard.check(title, official=True, pre_launch_ok=True, at=POST)


def test_pre_launch_requires_flag():
    assert leak_guard.check("GTA V funny moments", pre_launch_ok=False, at=PRE)
    assert leak_guard.check("GTA V funny moments", pre_launch_ok=True, at=PRE) is None


def test_pre_launch_gta6_gameplay_from_unofficial_blocked():
    assert leak_guard.check("GTA 6 gameplay first look", pre_launch_ok=True, at=PRE)
    assert leak_guard.check("GTA 6 gameplay first look", pre_launch_ok=True, official=True, at=PRE) is None
    assert leak_guard.check("GTA 6 theory: Jason's backstory", pre_launch_ok=True, at=PRE) is None


def test_post_launch_gameplay_allowed():
    assert leak_guard.check("GTA 6 gameplay first look", at=POST) is None


# ---------- scheduling ----------
def test_next_slot_skips_taken_and_lead_time():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 11, 19, 12, 20, tzinfo=tz)  # 12:30 slot is only 10 min away -> skip
    first = post.next_slot("tiktok", set(), now, tz)
    assert (first.hour, first.minute) == (16, 30)
    second = post.next_slot("tiktok", {first}, now, tz)
    assert (second.hour, second.minute) == (19, 0)


def test_next_slot_rolls_to_next_day():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 11, 19, 23, 55, tzinfo=tz)
    s = post.next_slot("instagram", set(), now, tz)
    assert s.day == 20 and (s.hour, s.minute) == (12, 0)


def test_youtube_title():
    t = post.youtube_title("This NPC had enough #gta6 #fyp")
    assert t == "This NPC had enough #shorts"
    assert len(post.youtube_title("x" * 300)) <= 100


# ---------- telegram ----------
def test_parse_callback():
    assert approve.parse_callback("a:12:3") == ("approve", 12, 3)
    assert approve.parse_callback("r:12") == ("reject", 12, None)
    with pytest.raises(ValueError):
        approve.parse_callback("x:1")


def test_review_text_fits_caption_limit():
    clip = {"id": 1, "score": 7.2, "hook": "h", "captions": ["c" * 300] * 3}
    src = {"campaign": None, "channel": "me", "title": "t" * 200}
    assert len(approve.review_text(clip, src)) <= 1024


# ---------- detection / render helpers ----------
def test_pick_peaks_spacing():
    curve = [-40.0] * 600
    for p in (100, 130, 400):
        for i in range(p - 3, p + 4):
            curve[i] = -10.0
    peaks = detect.pick_peaks(curve, window=68, k=3)
    assert any(abs(p - 100) <= 5 or abs(p - 130) <= 5 for p in peaks)
    assert any(abs(p - 400) <= 3 for p in peaks)
    assert all(abs(a - b) >= 68 for a in peaks for b in peaks if a != b)


def test_build_ass_shifts_and_filters():
    words = [{"start": 10.0 + i, "end": 10.5 + i, "text": f"w{i}"} for i in range(9)]
    ass = render.build_ass(words, "wait for it", 12.0, 18.0)
    assert "WAIT FOR IT" in ass
    assert "W2 W3 W4" in ass and "W0" not in ass
    assert "Dialogue: 0,0:00:00.00" in ass  # w2 at 12.0 -> 0


def test_video_kbps_fits_telegram():
    kbps = render.video_kbps(68)
    assert (kbps + 128) * 68 / 8 / 1000 <= render.TG_LIMIT_MB


def test_watchlist_loads():
    wl = settings.load_watchlist()
    assert wl.long_cut_s > 60 and wl.sources


# ---------- real ffmpeg render ----------
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
@pytest.mark.parametrize("layout", ["crop", "blur"])
def test_render_pair_end_to_end(tmp_path: Path, layout):
    src = tmp_path / "sec.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=70",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    words = [{"start": i * 0.6, "end": i * 0.6 + 0.5, "text": f"word{i}"} for i in range(110)]
    long_v, short_v = render.render_pair(src, words, "No way he did that", layout, 68, 40, tmp_path, "c")
    # 70s section: full cut for TikTok, first 59s for Shorts/Reels
    for f, want in ((long_v, 70), (short_v, 59)):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "default=nw=1", str(f)],
                             capture_output=True, text=True, check=True).stdout
        assert "width=1080" in out and "height=1920" in out
        dur = float([l for l in out.splitlines() if l.startswith("duration=")][0].split("=")[1])
        assert abs(dur - want) < 1.0
        assert f.stat().st_size / 1e6 < 20


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_loudness_curve_finds_loud_section(tmp_path: Path):
    a = tmp_path / "a.wav"
    # 30s quiet, 10s loud, 30s quiet
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,30,40),0.9,0.01)*sin(2*PI*440*t)':d=70:s=16000", str(a)], check=True)
    curve = detect.loudness_curve(a)
    assert 68 <= len(curve) <= 71
    loudest = max(range(len(curve)), key=curve.__getitem__)
    assert 30 <= loudest <= 41


def test_short_clip_is_one_file(tmp_path: Path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    a, b = render.render_pair(src, [], "", "blur", 68, 40, tmp_path, "x")
    assert a == b


def test_chat_spike_beats_loudness_only():
    from pipeline import chat, detect
    loud = [-30.0] * 900
    loud[200:205] = [-12.0] * 5          # loud but chat silent
    msgs = [(float(t), "KEKW LMAO") for t in range(606, 620) for _ in range(6)]
    msgs += [(float(t), "hi") for t in range(0, 900, 5)]
    curve = detect.signal_curve(loud, chat.rate_curve(msgs, 900))
    assert any(abs(p - 603) < 15 for p in detect.candidate_windows(curve, 68, 2))


def test_judge_filters_bad_picks():
    from pipeline import detect
    cands = {1: {"id": 1, "start": 100.0, "end": 168.0, "peak": 140, "text": "t", "segments": []}}
    good = {"id": 1, "start": 110, "end": 140, "score": 8, "kind": "fail", "spoiler": False,
            "hook": "He trusted the bus", "captions": ["a #gta6", "b #gta6", "c #gta6"]}
    assert detect._valid(good, cands)["end"] == 140
    assert detect._valid({**good, "score": 5}, cands) is None
    assert detect._valid({**good, "kind": "cutscene"}, cands) is None
    assert detect._valid({**good, "spoiler": True}, cands) is None
    assert detect._valid({**good, "start": 10, "end": 300}, cands)["start"] == 100.0
