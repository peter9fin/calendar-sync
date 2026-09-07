"""Change detection for IR pages.

Stateless functions: given today's text and yesterday's text for the same URL,
classify what changed. Also finds date tokens for the "new date" signal.

We do NOT try to extract structured events — the analyst does that by
eyeballing the page. Our only job is producing a short, prioritised review
list each morning.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Set, Tuple

# Broad date detector — we want to catch ANY plausible date token so the
# "new dates on page vs yesterday" set-diff has good coverage. Precision doesn't
# matter here; a human is going to look at the page.
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
_DATE_PATTERNS = [
    # 2026-11-05 / 2026/11/05
    (re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"), "ymd"),
    # 5.11.2026 / 05.11.2026 / 5/11/2026
    (re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b"), "dmy"),
    # 5 November 2026 / 05 Nov 2026
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\.?\s+(\d{{4}})\b", re.I), "d_mon_y"),
    # November 5, 2026 / Nov 5 2026
    (re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I), "mon_d_y"),
    # Bare "Nov 05" (no year — very common on IR pages that group by year header).
    # Year resolved from the nearest preceding standalone 20YY on the page.
    (re.compile(rf"(?<!\w)({_MONTHS})\.?\s+(\d{{1,2}})(?!\s*\d{{2,4}})(?!\w)", re.I), "mon_d"),
    # Bare "05 Nov"
    (re.compile(rf"(?<!\w)(\d{{1,2}})\s+({_MONTHS})\.?(?!\s*\d{{2,4}})(?!\w)", re.I), "d_mon"),
]
_YEAR_HEADER = re.compile(r"\b(20\d{2})\b")

_MON_TO_INT = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _resolve_year_from_text(text: str, pos: int, mo: int, d: int, today: date) -> int:
    """For a bare month-day match, pick a year from the nearest preceding
    standalone 20YY token, else the next-future occurrence."""
    scan_start = max(0, pos - 3000)
    yr = None
    for m in _YEAR_HEADER.finditer(text, scan_start, pos):
        yr = int(m.group(1))
    if yr is not None:
        try:
            date(yr, mo, d)
            return yr
        except ValueError:
            pass
    for y in (today.year, today.year + 1):
        try:
            if date(y, mo, d) >= today:
                return y
        except ValueError:
            continue
    return today.year


def extract_date_set(text: str, today: date, horizon_days: int = 120) -> Set[str]:
    """Return an ISO-date string set of all plausible future dates in the text."""
    horizon = today + timedelta(days=horizon_days)
    out: Set[str] = set()
    if not text:
        return out
    for pat, kind in _DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                if kind == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif kind == "dmy":
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif kind == "d_mon_y":
                    d = int(m.group(1))
                    mo = _MON_TO_INT[m.group(2).lower().rstrip(".")]
                    y = int(m.group(3))
                elif kind == "mon_d_y":
                    mo = _MON_TO_INT[m.group(1).lower().rstrip(".")]
                    d = int(m.group(2))
                    y = int(m.group(3))
                elif kind == "mon_d":
                    mo = _MON_TO_INT[m.group(1).lower().rstrip(".")]
                    d = int(m.group(2))
                    y = _resolve_year_from_text(text, m.start(), mo, d, today)
                elif kind == "d_mon":
                    d = int(m.group(1))
                    mo = _MON_TO_INT[m.group(2).lower().rstrip(".")]
                    y = _resolve_year_from_text(text, m.start(), mo, d, today)
                else:
                    continue
                dt = date(y, mo, d)
            except (KeyError, ValueError):
                continue
            if dt < today or dt > horizon:
                continue
            out.add(dt.isoformat())
    return out


# Cookie-banner strip. Some IR pages return 3-10kb of cookie-consent text at
# the top of body.innerText before the actual calendar. When that block
# dominates the leading portion, skip past it so the extractor's leading-
# context checks aren't poisoned.
_COOKIE_MARKERS = re.compile(
    r"cookie|consent|accept all|reject all|privacy policy|"
    r"data protection|preferences|cookie banner|onetrust",
    re.I,
)
_COOKIE_END_MARKERS = re.compile(
    r"^\s*(?:financial\s+calendar|investor\s+calendar|reporting\s+calendar|"
    r"upcoming\s+events|events\s+and\s+presentations|calendar|"
    r"investor\s+relations|home)\b", re.I | re.M,
)


def strip_cookie_prefix(text: str) -> str:
    """If the first ~2500 chars are cookie-heavy, cut them off."""
    if not text or len(text) < 3000:
        return text
    head = text[:2500]
    marker_count = len(_COOKIE_MARKERS.findall(head))
    if marker_count < 3:
        return text  # not cookie-dominant
    # Look for a good cut point after the cookie block — a heading line, or a
    # date. Otherwise cut at 2000 chars.
    m = _COOKIE_END_MARKERS.search(text, 500)
    if m and m.start() < 5000:
        return text[m.start():]
    return text[2000:]


def classify(
    today_text: str,
    yesterday_text: str,
    today_dates: Set[str],
    yesterday_dates: Set[str],
) -> Tuple[str, Set[str]]:
    """Return (priority, new_dates_set).

    Priorities:
      NEW_BASELINE : never fetched before
      HIGH         : content changed AND has ≥1 new future date
      MEDIUM       : content changed AND some old dates disappeared (rescheduled?)
      LOW          : content changed but no date-level change
      UNCHANGED    : identical text
    """
    if not yesterday_text:
        return ("NEW_BASELINE", today_dates)
    if today_text == yesterday_text:
        return ("UNCHANGED", set())
    new_dates = today_dates - yesterday_dates
    dropped_dates = yesterday_dates - today_dates
    if new_dates:
        return ("HIGH", new_dates)
    if dropped_dates:
        return ("MEDIUM", set())  # old dates gone — possibly rescheduled/cancelled
    return ("LOW", set())
