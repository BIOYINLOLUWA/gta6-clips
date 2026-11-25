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


def _robust_z(xs: list[float], floor: float = 1e-6) -> list[float]:
    """(x - median) / MAD, so one huge spike does not flatten everything else. `floor` stops a very
    steady signal (flat audio, steady chat) from turning tiny wobbles into fake spikes."""
    if not xs:
        return []
    srt = sorted(xs)
    med = srt[len(srt) // 2]
    mad = max(sorted(abs(x - med) for x in xs)[len(xs) // 2], floor)
    return [(x - med) / (1.4826 * mad) for x in xs]


def _smooth(xs: list[float], r: int) -> list[float]:
    out, acc = [], [0.0]
    for x in xs:
        acc.append(acc[-1] + x)
    for i in range(len(xs)):
        lo, hi = max(0, i - r), min(len(xs), i + r + 1)
        out.append((acc[hi] - acc[lo]) / (hi - lo))
    return out


def signal_curve(loud: list[float], chat_rate: list[float]) -> list[float]:
    """Per-second interest score: loudness spikes plus (when available) hype-weighted chat speed.
    Chat reacts a few seconds after the moment, so it is shifted earlier by 6s."""
    z = [max(-3.0, min(v, 6.0)) for v in _robust_z(_smooth(loud, 3), floor=1.0)]
    if chat_rate and sum(chat_rate) > 30:
        shifted = chat_rate[6:] + [0.0] * 6
        zc = [max(-3.0, min(v, 6.0)) for v in _robust_z(_smooth(shifted, 5), floor=max(0.3, sorted(shifted)[len(shifted) // 2] * 0.5))]
        return [a + 1.5 * b for a, b in zip(z, zc)]  # chat counts more: it is the audience voting
    return z


def candidate_windows(curve: list[float], window: int, k: int, gap: int = 90) -> list[int]:
    """Top-k peaks at least `gap` seconds apart (peak second indices)."""
    half = window // 2
    order = sorted(range(len(curve)), key=lambda i: curve[i], reverse=True)
    chosen: list[int] = []
    for i in order:
        if len(curve) > window and (i < half or i > len(curve) - half):
            continue
        if all(abs(i - c) >= gap for c in chosen):
            chosen.append(i)
        if len(chosen) >= k:
            break
    return sorted(chosen)


RUBRIC_PATH = settings.ROOT / "config" / "rubric.md"


def judge(cands: list[dict], channel: str, title: str, campaign: str | None, k: int) -> list[dict] | None:
    """Claude reads every candidate (timestamped transcript + chat sample) against the rubric and returns
    tight picks: [{id, start, end, score, kind, spoiler, hook, captions, reason}]. None if unavailable."""
    if not settings.ANTHROPIC_API_KEY or not cands:
        return None
    import requests

    rubric = RUBRIC_PATH.read_text(encoding="utf-8") if RUBRIC_PATH.exists() else ""
    blocks = []
    for c in cands:
        lines = "\n".join(f"[{s['start']:.1f}] {s['text']}" for s in c["segments"]) or "(no speech)"
        chat = " | ".join(c["chat"]) or "(no chat data)"
        blocks.append(f"### Candidate {c['id']}  window {c['start']:.0f}-{c['end']:.0f}s  "
                      f"signal {c['signal']:.1f}\nTranscript:\n{lines}\nChat during it: {chat}")
    credit = f"Credit the streamer {channel} in every caption." if campaign else ""
    prompt = (
        f"You are an expert short-form clipper for GTA 6 streams. Stream: '{title}' by {channel}.\n\n"
        f"{rubric}\n\n" + "\n\n".join(blocks) + "\n\n"
        f"Pick at most {k} clips (fewer is fine; none if nothing is good). For each, choose a tight start and "
        "end in absolute seconds from the transcript timestamps (15-59s long, inside its window). "
        "Return ONLY a JSON list, best first: [{\"id\": candidate id, \"start\": s, \"end\": s, "
        "\"score\": 1-10, \"kind\": one of gameplay|reaction|chase|fail|funny|skill|cutscene|in-game-tv|other, "
        "\"spoiler\": true/false, \"hook\": on-screen hook 3-7 words that sets up the payoff without giving it "
        "away, \"captions\": [3 different post captions, max 120 chars, 2-4 hashtags incl #gta6], "
        "\"reason\": one line citing the transcript/chat evidence}]. "
        f"No leak or early-access claims, no story spoilers, no graphic wording. {credit}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": settings.ANTHROPIC_MODEL, "max_tokens": 3000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        picks = json.loads(text[text.find("["): text.rfind("]") + 1])
        return [p for p in picks if isinstance(p, dict)]
    except Exception as exc:
        log.warning("Claude judging failed, falling back to signal ranking: %s", str(exc)[:300])
        return None


def _valid(p: dict, cands: dict[int, dict]) -> dict | None:
    """Keep a pick only if it is good, honest and inside its window."""
    try:
        c = cands[int(p["id"])]
        start, end, score = float(p["start"]), float(p["end"]), float(p["score"])
    except Exception:
        return None
    if score < 6 or p.get("spoiler") or p.get("kind") in ("cutscene", "in-game-tv"):
        return None
    start = max(start, c["start"])
    end = min(end, c["end"], start + 59)
    if end - start < 12:
        return None
    caps = [str(x) for x in (p.get("captions") or [])][:3]
    if len(caps) < 3 or not p.get("hook"):
        return None
    return {**p, "start": start, "end": end, "score": score, "captions": caps, "transcript": c["text"]}


def find_moments(audio: Path, channel: str, title: str, campaign: str | None,
                 long_s: int, k: int, chat_msgs: list | None = None) -> list[Moment]:
    from . import chat as chatmod

    loud = loudness_curve(audio)
    if not loud:
        return []
    duration = len(loud)
    rate = chatmod.rate_curve(chat_msgs or [], duration) if chat_msgs else []
    curve = signal_curve(loud, rate)
    log.info("signals: %ds of audio, chat %s, Claude judging %s", duration,
             "on" if rate and sum(rate) > 30 else "off",
             "on" if settings.ANTHROPIC_API_KEY else "OFF (no ANTHROPIC_API_KEY secret)")
    window = min(long_s, 70)
    peaks = [duration // 2] if duration <= window else candidate_windows(curve, window, max(k * 3, 8))
    cands: dict[int, dict] = {}
    for n, p in enumerate(peaks, 1):
        start = max(0.0, p - window * 0.6)  # moment usually peaks after its setup
        end = min(float(duration), start + window)
        start = max(0.0, end - window)
        segs = transcribe(audio, start, end)
        cands[n] = {"id": n, "start": start, "end": end, "peak": p, "segments": segs,
                    "text": " ".join(s["text"] for s in segs),
                    "signal": max(curve[int(start):int(end)] or [0.0]),
                    "chat": chatmod.top_messages(chat_msgs or [], start, end + 8)}
    picks = judge(list(cands.values()), channel, title, campaign, k)
    moments: list[Moment] = []
    if picks is not None:
        for p in picks:
            v = _valid(p, cands)
            if v and all(abs(v["start"] - m.start) > 20 for m in moments):
                moments.append(Moment(v["start"], v["end"], cands[int(v["id"])]["peak"], v["score"],
                                      v["transcript"], v["hook"], v["captions"]))
        log.info("Claude kept %d of %d candidates", len(moments), len(cands))
        return moments[:k]
    # No API key or Claude failed: rank by signal + speech hype, template captions.
    ranked = sorted(cands.values(), key=lambda c: c["signal"] + hype_score(c["text"], window) / 3,
                    reverse=True)[:k]
    for c in ranked:
        pkg = llm_package(c["text"], channel, title) or template_package(channel, campaign)
        moments.append(Moment(c["start"], c["end"], c["peak"], c["signal"], c["text"],
                              pkg["hook"], pkg["captions"][:3]))
    return moments
