"""Diff logic — find IR-page events missing from 9fin's calendar."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List

from .normalise import normalise


DATE_TOLERANCE_DAYS = 5


@dataclass(frozen=True)
class EventLike:
    """Minimal shape shared between IR-scraped and 9fin-calendar events."""
    date: date
    title: str


def _get(e, attr: str):
    """Read ``attr`` from either an object (``e.attr``) or a mapping (``e[attr]``)."""
    if isinstance(e, dict):
        return e.get(attr)
    return getattr(e, attr, None)


def _as_date(v) -> date:
    """Coerce a value to a ``datetime.date`` — accepts ``date`` or ISO string."""
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    raise TypeError(f"Cannot coerce {v!r} ({type(v).__name__}) to date")


def _covered_by_ninefin(ir_date: date, ninefin: Iterable) -> bool:
    """True if 9fin already has any event within ±DATE_TOLERANCE_DAYS of the IR date.

    We intentionally ignore titles here — 9fin's title conventions differ from IR-page
    conventions (Trading Update vs Interim vs Results), and the practical question is
    "is this date already on 9fin?", not "does the wording match?".
    """
    for nf in ninefin:
        nf_d = _as_date(_get(nf, "date"))
        if abs((ir_date - nf_d).days) <= DATE_TOLERANCE_DAYS:
            return True
    return False


def find_gaps(ir_events: Iterable, ninefin_events: Iterable) -> List:
    """Return every IR event (kind ∈ RESULTS/CALL) not covered by a same-date 9fin event.

    Both sides may be lists of dicts (``{"date": ..., "title": ...}``) or lists
    of objects with ``.date``/``.title`` attributes. ``date`` values may be
    ``datetime.date`` or ISO strings; both are coerced consistently.
    """
    scoped = [
        e for e in ir_events
        if normalise(str(_get(e, "title") or "")).kind in ("RESULTS", "CALL")
    ]
    ninefin_list = list(ninefin_events)
    return [
        ir for ir in scoped
        if not _covered_by_ninefin(_as_date(_get(ir, "date")), ninefin_list)
    ]
