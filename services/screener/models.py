"""Core data models, setting specs, validation, and strategy interface for the SMC screener."""
from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k"))


def compact(x: float) -> str:
    ax = abs(x)
    for div, suffix in _UNITS:
        if ax >= div:
            return f"{x / div:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{x:g}"


@dataclass
class Candle:
    ts: int  # candle OPEN time, epoch seconds
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    tooltip: str
    kind: str  # "int" | "float" | "bool" | "choice" | "text"
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Tuple[Tuple[str, str], ...] = ()  # (value, label)
    group: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "tooltip": self.tooltip,
            "kind": self.kind,
            "min": self.min,
            "max": self.max,
            "choices": [{"value": v, "label": lbl} for v, lbl in self.choices],
            "group": self.group,
        }


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    label: str
    kind: str = "text"  # "text" | "num" | "pct" | "int"
    tooltip: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "tooltip": self.tooltip,
        }


@dataclass
class ScanContext:
    symbol: str
    code: str
    timeframe: str
    intraday: bool
    now: dt.datetime


@dataclass
class ResultRow:
    values: Dict[str, Any]
    valid: bool = True  # False -> rejected row (shown muted when show_invalid is on)


def coerce(spec: SettingSpec, value: Any) -> Tuple[Any, Optional[str]]:
    """Coerce and validate a single setting value. Returns (clean_value, error_or_None)."""
    try:
        if spec.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on"), None
            return bool(value), None
        if spec.kind in ("int", "float"):
            if isinstance(value, str):
                value = value.strip().replace(",", "")
            if isinstance(value, bool):
                return None, f"{spec.label}: '{value}' is not a valid number"
            v: Any = int(float(value)) if spec.kind == "int" else float(value)
            if spec.kind == "int" and float(value) != int(float(value)):
                return None, f"{spec.label}: must be a whole number"
            if spec.min is not None and v < spec.min:
                return None, f"{spec.label}: must be at least {spec.min:g}"
            if spec.max is not None and v > spec.max:
                return None, f"{spec.label}: must be at most {spec.max:g}"
            return v, None
        if spec.kind == "choice":
            allowed = [c[0] for c in spec.choices]
            if value not in allowed:
                return None, f"{spec.label}: choose one of {', '.join(allowed)}"
            return value, None
        return ("" if value is None else str(value)), None
    except (TypeError, ValueError):
        return None, f"{spec.label}: '{value}' is not a valid number"


def validate(
    specs: Sequence[SettingSpec],
    values: Dict[str, Any],
    defaults: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Missing keys fall back to defaults; returns (clean, errors)."""
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for s in specs:
        raw = values.get(s.key, defaults.get(s.key))
        v, err = coerce(s, raw)
        if err:
            errors.append(err)
        else:
            clean[s.key] = v
    return clean, errors


class Strategy(abc.ABC):
    id: str = ""
    name: str = ""
    description: str = ""
    timeframes: Optional[Tuple[str, ...]] = None  # None = all timeframes; else restricted
    ui_keys: Tuple[str, ...] = ()

    def check(self, settings: Dict[str, Any]) -> List[str]:
        """Additional cross-field validation beyond individual SettingSpec bounds."""
        return []

    @abc.abstractmethod
    def settings_specs(self) -> List[SettingSpec]: ...

    @abc.abstractmethod
    def columns(self) -> List[ColumnSpec]: ...

    @abc.abstractmethod
    def scan(
        self,
        candles: Sequence[Candle],
        settings: Dict[str, Any],
        ctx: ScanContext,
    ) -> List[ResultRow]:
        """Evaluate one symbol's candles (oldest first) and return result rows."""

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return 30
