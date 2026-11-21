"""Find the best moments in a VOD.

1. Loudness curve over the whole audio (fast, ffmpeg ebur128) -> peak candidates.
2. Transcribe only the candidate windows (faster-whisper) -> hype score.
3. Optional LLM pass (Anthropic) -> final score, hook line, 3 caption variants.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import settings

log = logging.getLogger(__name__)

HYPE = re.compile(
    r"\b(no way|what the|oh my god|omg|bro|let'?s go|insane|crazy|wtf|holy|yo+|wait|run|"
    r"hahaha+|lmao|dude|look at|did you see|cops?|police|wanted|five stars?|crash(ed)?)\b", re.I)


@dataclass
class Moment:
    start: float
    end: float
    peak: float
    score: float
    transcript: str = ""
    hook: str = ""
    captions: list[str] = field(default_factory=list)


def loudness_curve(audio: Path) -> list[float]:
    """Momentary loudness (LUFS) per second."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    per_sec: dict[int, list[float]] = {}
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)", proc.stderr):
        t, val = float(m.group(1)), m.group(2)
        per_sec.setdefault(int(t), []).append(-70.0 if val == "-inf" else float(val))
    if not per_sec:
        return []
    n = max(per_sec) + 1
    return [max(per_sec.get(i, [-70.0])) for i in range(n)]


def pick_peaks(curve: list[float], window: int, k: int, smooth: int = 5) -> list[int]:
    """Top-k loudness peaks at least `window` seconds apart, ignoring the first/last half window."""
    if not curve:
        return []
    sm = []
    for i in range(len(curve)):
        lo, hi = max(0, i - smooth), min(len(curve), i + smooth + 1)
        sm.append(sum(curve[lo:hi]) / (hi - lo))
    half = window // 2
    order = sorted(range(len(sm)), key=lambda i: sm[i], reverse=True)
    chosen: list[int] = []
    for i in order:
        if len(sm) > window and (i < half or i > len(sm) - half):
            continue
        if all(abs(i - c) >= window for c in chosen):
            chosen.append(i)
        if len(chosen) >= k:
            break
    return sorted(chosen)


_whisper = None


def transcribe(audio: Path, start: float | None = None, end: float | None = None,
               words: bool = False) -> list[dict]:
    """Transcribe audio (optionally a window). Returns segments or words with absolute times."""
    global _whisper
    from faster_whisper import WhisperModel

    if _whisper is None:
        _whisper = WhisperModel(settings.WHISPER_MODEL, device="cpu", compute_type="int8")
    offset = start or 0.0
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start), "-to", str(end)]
    cmd += ["-i", str(audio), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"]
    import numpy as np
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg decode failed: " + proc.stderr.decode(errors="replace")[:300])
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    segs, _ = _whisper.transcribe(pcm, word_timestamps=words, vad_filter=True)
    out = []
    for s in segs:
        if words:
            out += [{"start": w.start + offset, "end": w.end + offset, "text": w.word.strip()}
                    for w in (s.words or [])]
        else:
            out.append({"start": s.start + offset, "end": s.end + offset, "text": s.text.strip()})
    return out


def hype_score(text: str, duration: float) -> float:
    words = len(text.split())
    hits = len(HYPE.findall(text))
    density = min(words / max(duration, 1) / 2.5, 1.0)  # ~2.5 words/s = constant talking
    return hits * 1.5 + density * 3


def llm_package(transcript: str, channel: str, title: str) -> dict | None:
    """Ask Claude for a score, hook and captions. Returns None if no API key or on failure."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    import requests

    prompt = (
        "You package GTA gameplay clips for TikTok/Shorts/Reels. Source: "
        f"'{title}' by {channel}.\nTranscript of the ~60s clip:\n\"\"\"{transcript[:3000]}\"\"\"\n\n"
        "Return ONLY JSON: {\"score\": 1-10 virality, \"hook\": on-screen hook line max 8 words, "
        "\"captions\": [3 different post captions, each max 120 chars with 2-4 hashtags incl #gta6]}. "
        "Rules: no claims of leaks or early access, no story spoilers, credit the streamer by name "
        "if it is their stream, no graphic/violent wording in the hook."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": settings.ANTHROPIC_MODEL, "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        if len(data.get("captions", [])) >= 3 and data.get("hook"):
            return data
    except Exception as exc:
        log.warning("LLM packaging failed, using templates: %s", exc)
    return None


def template_package(channel: str, campaign: str | None) -> dict:
    credit = f" (via {channel})" if campaign else ""
    return {
        "hook": "Wait for the ending...",
        "captions": [
            f"GTA 6 never misses{credit} #gta6 #gtavi #gaming",
            f"This is why GTA 6 took 12 years{credit} #gta6 #gta #fyp",
            f"Rate this moment 1-10{credit} #gta6 #gtaonline #gamingclips",
        ],
    }


def find_moments(audio: Path, channel: str, title: str, campaign: str | None,
                 long_s: int, k: int) -> list[Moment]:
    curve = loudness_curve(audio)
    if not curve:
        return []
    duration = len(curve)
    if duration <= long_s:  # short upload: one clip, the whole thing
        peaks = [duration // 2]
    else:
        peaks = pick_peaks(curve, long_s, k * 3)
    moments = []
    for p in peaks:
        start = max(0.0, p - long_s / 2)
        end = min(float(duration), start + long_s)
        start = max(0.0, end - long_s)
        segs = transcribe(audio, start, end)
        text = " ".join(s["text"] for s in segs)
        loud = (sum(curve[int(start):int(end)]) / max(int(end) - int(start), 1) + 70) / 10
        moments.append(Moment(start, end, p, hype_score(text, end - start) + loud, text))
    moments.sort(key=lambda m: m.score, reverse=True)
    best = moments[:k]
    for m in best:
        pkg = llm_package(m.transcript, channel, title)
        if pkg:
            m.score = m.score + float(pkg.get("score", 5))
        pkg = pkg or template_package(channel, campaign)
        m.hook, m.captions = pkg["hook"], pkg["captions"][:3]
    return sorted(best, key=lambda m: m.score, reverse=True)
