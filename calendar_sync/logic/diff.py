"""Diff logic — find IR-page events missing from 9fin's calendar."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List

from .normalise import normalise


DATE_TOLERANCE_DAYS = 5


@dataclass(frozen=True)
class EventLike:
    """Minimal shape shared between IR-scraped and 9fin-calendar events."""
    date: date
    title: str


def _covered_by_ninefin(ir: EventLike, ninefin: List[EventLike]) -> bool:
    """True if 9fin already has any event within ±DATE_TOLERANCE_DAYS of the IR date.

    We intentionally ignore titles here — 9fin's title conventions differ from IR-page
    conventions (Trading Update vs Interim vs Results), and the practical question is
    "is this date already on 9fin?", not "does the wording match?".
    """
    for nf in ninefin:
        if abs((ir.date - nf.date).days) <= DATE_TOLERANCE_DAYS:
            return True
    return False


def find_gaps(ir_events: List[EventLike], ninefin_events: List[EventLike]) -> List[EventLike]:
    """Return every IR event (kind ∈ RESULTS/CALL) not covered by a same-date 9fin event."""
    scoped = [e for e in ir_events if normalise(e.title).kind in ("RESULTS", "CALL")]
    return [ir for ir in scoped if not _covered_by_ninefin(ir, ninefin_events)]
