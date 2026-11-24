"""Render vertical 9:16 clips with burned-in word captions (Bebas Neue) and a hook line.

Each moment produces two files:
  long  (>60s)  -> TikTok (Creator Rewards needs over 1 minute)
  short (~40s)  -> YouTube Shorts + Instagram Reels
Files are sized under Telegram's 20MB bot download limit so Telegram can act as storage.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import settings

TG_LIMIT_MB = 19.0
W, H = 1080, 1920


def _ts(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[dict], hook: str, t0: float, t1: float, chunk: int = 3) -> str:
    """ASS subtitle file for the window [t0, t1] of the source; times are shifted to start at 0."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Bebas Neue,110,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,2,0,1,7,2,2,60,60,520,1
Style: Hook,Bebas Neue,120,&H0000E5FF,&H0000E5FF,&H00000000,&H64000000,0,0,0,0,100,100,2,0,1,8,3,8,60,60,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(3.0)},Hook,,0,0,0,,{_esc(hook.upper())}")
    inside = [w for w in words if w["start"] >= t0 and w["end"] <= t1 and w["text"]]
    for i in range(0, len(inside), chunk):
        grp = inside[i:i + chunk]
        start = grp[0]["start"] - t0
        end = grp[-1]["end"] - t0
        if i + chunk < len(inside):  # hold until next chunk starts, avoids flicker
            end = max(end, inside[i + chunk]["start"] - t0)
        text = " ".join(w["text"] for w in grp).upper()
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{_esc(text)}")
    return head + "\n".join(lines) + "\n"


def video_filter(layout: str, ass_path: Path) -> str:
    ass = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    fonts = str(settings.FONTS_DIR).replace("\\", "/").replace(":", r"\:")
    subs = f"subtitles='{ass}':fontsdir='{fonts}'"
    if layout == "crop":
        return f"scale=-2:{H},crop={W}:{H},setsar=1,{subs}"
    return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=20:5[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{subs}")


def video_kbps(duration: float, limit_mb: float = TG_LIMIT_MB, audio_kbps: int = 128) -> int:
    total_kbits = limit_mb * 8 * 1000
    return int(max(800, min(4500, total_kbits / duration - audio_kbps)))


def render(src: Path, out: Path, start: float, end: float, words: list[dict], hook: str,
           layout: str) -> Path:
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(build_ass(words, hook, start, end), encoding="utf-8")
    dur = end - start
    kbps = video_kbps(dur)
    for attempt in range(3):
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-filter_complex", video_filter(layout, ass_path),
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k",
            "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)], check=True)
        if out.stat().st_size / 1e6 <= TG_LIMIT_MB + 0.5:
            return out
        kbps = int(kbps * 0.8)
    raise RuntimeError(f"{out.name} still over Telegram limit after re-encodes")


def render_pair(section: Path, words: list[dict], hook: str, layout: str,
                long_s: int, short_s: int, workdir: Path, name: str) -> tuple[Path, Path]:
    """`section` is the downloaded window (time 0 = window start). Returns (long, short)."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(section)], capture_output=True, text=True, check=True)
    dur = float(probe.stdout.strip())
    long_end = min(dur, float(long_s))
    mid = long_end / 2
    s0 = max(0.0, mid - short_s / 2)
    s1 = min(long_end, s0 + short_s)
    long_out = render(section, workdir / f"{name}_long.mp4", 0.0, long_end, words, hook, layout)
    short_out = render(section, workdir / f"{name}_short.mp4", s0, s1, words, hook, layout)
    return long_out, short_out


FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/BebasNeue-Regular.ttf"


def _ensure_font() -> None:
    f = settings.FONTS_DIR / "BebasNeue-Regular.ttf"
    if any(settings.FONTS_DIR.glob("*.ttf")) if settings.FONTS_DIR.exists() else False:
        return
    try:
        import urllib.request
        settings.FONTS_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(FONT_URL, f)
    except Exception:
        pass  # libass falls back to a default font


def screen_text(caption: str, limit: int = 70) -> str:
    """Caption as on-screen hook: first line, hashtags removed, trimmed."""
    import re
    first = caption.strip().splitlines()[0] if caption.strip() else ""
    txt = re.sub(r"#\w+", "", first).strip()
    txt = re.sub(r"\s+", " ", txt)
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "..."
    return txt


def burn_caption(video: Path, out: Path, caption: str, seconds: float = 4.0, disclosure: str = "") -> Path:
    """Overlay the chosen caption where the old hook sat (top, first few seconds)."""
    text = screen_text(caption)
    if not text and not disclosure:
        return video
    _ensure_font()
    ass_path = out.with_suffix(".ass")
    head = build_ass([], "", 0, 0).replace(
        "\n[Events]", "\nStyle: Ad,Bebas Neue,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,2,0,1,5,2,8,60,60,130,1\n\n[Events]", 1)
    body = f"Dialogue: 1,{_ts(0)},{_ts(seconds)},Hook,,0,0,0,,{_esc(text.upper())}\n"
    if disclosure:
        body += f"Dialogue: 2,{_ts(0)},{_ts(seconds)},Ad,,0,0,0,,{_esc(disclosure.upper())}\n"
    ass_path.write_text(head + body, encoding="utf-8")
    ass = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    fonts = str(settings.FONTS_DIR).replace("\\", "/").replace(":", r"\:")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
        "-vf", f"subtitles='{ass}':fontsdir='{fonts}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    return out
