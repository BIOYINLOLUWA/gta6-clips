"""Hard block on leaked / pre-release footage.

Rockstar's video policy bans pre-release leaked footage of any kind, however it was
obtained, including reposts and early unboxings. One strike can kill a channel, so
this runs before anything is downloaded and again before anything is posted.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import settings

# Always blocked, before and after launch.
LEAK_TERMS = [
    r"\bleak(s|ed|er)?\b", r"\bcyberleek\b", r"\bearly (copy|access|gameplay)\b",
    r"\bunbox(ing|ed)?\b", r"\bbefore release\b", r"\bpre-?release\b",
    r"\bstolen\b", r"\bdev build\b", r"\bbeta footage\b", r"\binsider footage\b",
    r"\bnot supposed to\b", r"\bgot (the )?game early\b",
]
# Before launch, any GTA 6 gameplay claim from a non-official source is treated as a leak.
GTA6_TERMS = [r"\bgta\s?(6|vi)\b", r"\bgrand theft auto (6|vi)\b", r"\bleonida\b", r"\bvice city\b.*\bgameplay\b"]
GAMEPLAY_TERMS = [r"\bgameplay\b", r"\bfootage\b", r"\bplaythrough\b", r"\bfirst look\b", r"\bin[- ]game\b"]

_leak = re.compile("|".join(LEAK_TERMS), re.I)
_gta6 = re.compile("|".join(GTA6_TERMS), re.I)
_gameplay = re.compile("|".join(GAMEPLAY_TERMS), re.I)


def check(title: str, description: str = "", *, official: bool = False,
          pre_launch_ok: bool = False, at: datetime | None = None) -> str | None:
    """Return a block reason, or None if the source is allowed."""
    text = f"{title}\n{description}"
    m = _leak.search(text)
    if m:
        return f"leak term: '{m.group(0)}'"
    if settings.is_pre_launch(at):
        if not pre_launch_ok:
            return "source not marked pre_launch_ok before launch"
        if not official and _gta6.search(text) and _gameplay.search(text):
            return "pre-launch GTA 6 gameplay from a non-official source"
    return None
