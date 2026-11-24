"""Campaign caption rules: required hashtags and the paid-disclosure tag."""
from __future__ import annotations

import yaml

from . import settings


def _load() -> dict:
    p = settings.ROOT / "config" / "campaigns.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def rules_for(source: dict) -> dict:
    """Returns {'disclosure': str, 'hashtags': [..]} or empty dict for non-campaign sources."""
    if not source.get("campaign"):
        return {}
    cfg = _load()
    out = {"disclosure": (cfg.get("default") or {}).get("disclosure", ""), "hashtags": []}
    name = (source.get("channel") or "").lower()
    for r in cfg.get("rules") or []:
        if r.get("match", "").lower() in name:
            out["hashtags"] += list(r.get("hashtags") or [])
    return out


def final_caption(caption: str, source: dict) -> str:
    """Caption with the disclosure first and any missing campaign hashtags appended."""
    r = rules_for(source)
    if not r:
        return caption
    text = caption.strip()
    low = text.lower()
    disc = r.get("disclosure", "")
    if disc and disc.lower() not in low:
        text = f"{disc} {text}"
    missing = [h for h in r.get("hashtags", []) if h.lower() not in low]
    if missing:
        text = f"{text} {' '.join(missing)}"
    return text
