"""Last-candle position relative to a support zone and bullish reaction classification."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from services.screener.models import Candle, ColumnSpec, SettingSpec

INSIDE, WICK, NEAR, ABOVE, BELOW, BREAKOUT = (
    "Inside",
    "Wick touch",
    "Near",
    "Above",
    "Below",
    "Breakout candle",
)
STATUS_NOT_AT_ZONE = "Not at zone"
STATUS_NO_REACTION = "No bullish reaction"
GREEN, REJECTION, ENGULFING = "Green", "Rejection wick", "Engulfing"
REACTION_ACCEPT = {
    "green": {GREEN},
    "rejection": {REJECTION},
    "engulfing": {ENGULFING},
    "any": {GREEN, REJECTION, ENGULFING},
}
REACTION_CHOICES = (
    ("off", "Off"),
    ("green", "Green candle"),
    ("rejection", "Rejection wick"),
    ("engulfing", "Bullish engulfing"),
    ("any", "Any of these"),
)
ACCEPT = {
    "off": None,
    "touch": {INSIDE, WICK},
    "inside": {INSIDE},
    "near": {INSIDE, WICK, NEAR},
}


def classify(c: Candle, top: float, bot: float, near_pct: float) -> str:
    if c.c < bot:
        return BELOW
    if c.c <= top:
        return INSIDE
    if c.l <= top:
        return WICK
    if top > 0 and near_pct > 0 and (c.l - top) / top * 100 <= near_pct:
        return NEAR
    return ABOVE


def passes(pos: str, mode: str) -> bool:
    allowed = ACCEPT.get(mode)
    return allowed is None or pos in allowed


def reaction(cs: Sequence[Candle], wick_ratio: float = 2.0) -> List[str]:
    """Bullish reaction tags of the last candle, strongest first: Engulfing, Rejection wick, Green."""
    if not cs:
        return []
    c = cs[-1]
    tags: List[str] = []
    if len(cs) >= 2:
        p = cs[-2]
        if p.c < p.o and c.c > c.o and c.o <= p.c and c.c >= p.o:
            tags.append(ENGULFING)
    rng = c.h - c.l
    body = abs(c.c - c.o)
    lower = min(c.o, c.c) - c.l
    if rng > 0 and lower > 0 and lower >= wick_ratio * body and (c.c - c.l) / rng >= 0.5:
        tags.append(REJECTION)
    if c.c > c.o:
        tags.append(GREEN)
    return tags


def reaction_text(tags: Sequence[str]) -> str:
    return ", ".join(tags) if tags else "-"


def verdict(pos: str, tags: Sequence[str], s: Dict[str, Any]) -> Optional[str]:
    """None when the last candle passes zone-touch and reaction rules; else rejection status."""
    zone_mode = s.get("last_at_zone", "off")
    react_mode = s.get("reaction", "off")
    if react_mode != "off" and zone_mode == "off":
        zone_mode = "touch"
    if not passes(pos, zone_mode):
        return STATUS_NOT_AT_ZONE
    if react_mode != "off" and not set(tags) & REACTION_ACCEPT[react_mode]:
        return STATUS_NO_REACTION
    return None


def specs(group: str, default_hint: str = "") -> List[SettingSpec]:
    return [
        SettingSpec(
            "last_at_zone",
            "Last candle at zone",
            "Keep only stocks whose LAST candle landed on the zone. Touch = closed inside or wicked into it. "
            "Inside = closed inside only. Near = also low within Near % above zone top." + default_hint,
            "choice",
            choices=(
                ("off", "Off"),
                ("touch", "Inside or wick touch"),
                ("inside", "Inside only"),
                ("near", "Inside, wick touch or near"),
            ),
            group=group,
        ),
        SettingSpec(
            "near_pct",
            "Near %",
            "For 'Near': how far above the zone top the last candle's low may be, in percent of the zone top.",
            "float",
            0,
            20,
            group=group,
        ),
        SettingSpec(
            "reaction",
            "Bullish reaction on the zone",
            "Require the last candle on the zone to show a bullish reaction (Green, Rejection wick, or Engulfing).",
            "choice",
            choices=REACTION_CHOICES,
            group=group,
        ),
        SettingSpec(
            "wick_ratio",
            "Wick ratio",
            "For 'Rejection wick': lower wick must be at least this many times the candle body.",
            "float",
            0.5,
            10,
            group=group,
        ),
    ]


def reaction_column() -> ColumnSpec:
    return ColumnSpec(
        "reaction",
        "Reaction",
        tooltip="Bullish reaction of the last candle: Engulfing, Rejection wick, Green, or '-'",
    )


def column() -> ColumnSpec:
    return ColumnSpec(
        "last_pos",
        "Last candle",
        tooltip="Where the last candle landed: Inside, Wick touch, Near, Above, or Breakout candle",
    )
