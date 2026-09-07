"""Title / event-kind normalisation for the diff.

An event is fully identified by (kind, period, year). Two events match if:
  - Same kind (RESULTS release vs CALL — treated as separate events)
  - Same period token (Q1..Q4, H1/H2, FY)
  - Same year (4-digit; 2-digit inputs like "FY27" get expanded to "2027")

Ambiguous / out-of-scope titles are classified as OTHER — filtered out at diff time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_RE_PERIOD_YEAR = re.compile(
    r"\b(Q[1-4]|H[12]|FY)\s*(?:FY)?\s*(\d{2,4})\b",
    flags=re.IGNORECASE,
)
_RE_LONG_PERIOD = re.compile(
    r"\b(FULL YEAR|HALF YEAR|HY|1H|2H)\s*(\d{2,4})\b",
    flags=re.IGNORECASE,
)
_RE_CALL = re.compile(
    r"CONFERENCE CALL|EARNINGS CALL|WEBCAST|ANALYST CALL",
    flags=re.IGNORECASE,
)
_RE_RESULTS = re.compile(
    r"RESULTS|EARNINGS|INTERIM|ANNUAL REPORT|FULL[- ]YEAR|HALF[- ]YEAR|TRADING UPDATE|PRELIM",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalisedEvent:
    kind: str  # "RESULTS" | "CALL" | "OTHER"
    period: Optional[str]  # "Q1".."Q4" | "H1"/"H2" | "FY" | None
    year: Optional[str]  # 4-digit string or None


def _expand_year(y: str) -> str:
    y = y.strip()
    if len(y) == 2:
        return "20" + y
    return y


def normalise(title: str) -> NormalisedEvent:
    if not title:
        return NormalisedEvent("OTHER", None, None)

    t = title.upper()

    kind = "OTHER"
    if _RE_CALL.search(t):
        kind = "CALL"
    elif _RE_RESULTS.search(t):
        kind = "RESULTS"

    m = _RE_PERIOD_YEAR.search(t)
    if m:
        period = m.group(1).upper()
        year = _expand_year(m.group(2))
        return NormalisedEvent(kind=kind, period=period, year=year)

    m = _RE_LONG_PERIOD.search(t)
    if m:
        token = m.group(1).upper()
        period = "FY" if token.startswith("F") else "H1"  # 1H → H1, 2H is rare; treat as H2 fallback
        if token in ("2H",):
            period = "H2"
        year = _expand_year(m.group(2))
        return NormalisedEvent(kind=kind, period=period, year=year)

    return NormalisedEvent(kind=kind, period=None, year=None)
