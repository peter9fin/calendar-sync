"""IR page scraper — pulls calendar-style earnings/call events from an issuer's IR page.

Extraction philosophy:
  For each candidate future date we look at ±100 chars of context. We only accept the
  date if that window contains BOTH an event-type token (results / earnings / call /
  trading update / AGM …) AND a period token (Q1-Q4 / H1-H2 / FY / half-year etc.).
  We drop hits whose window includes cookie-banner / consent boilerplate.

  The emitted title is short and structured — "Q3 2026 Results" / "FY 2026 Earnings
  Call" — which lets the diff normalisation match cleanly against 9fin events.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from playwright.sync_api import BrowserContext

log = logging.getLogger(__name__)


# Patterns for structured dates embedded in HTML attributes / URLs — e.g. iCal
# add-to-calendar links contain "startdt=2026-10-22T05:00:00Z", HTML5 <time> uses
# datetime="...", and many CMSs embed data-date="..." on event tiles.
_RE_HTML_DATES = re.compile(
    r"(?:startdt|dtstart|DTSTART|data-date|data-event-date|datetime)"
    r"[=:\"' \t]{1,4}"
    r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
)


_CAL_LINK_TEXT_RX = re.compile(
    r"(?:financial|investor|corporate)\s+calendar|"
    r"upcoming\s+events|upcoming\s+dates|"
    r"^events?$|^calendar$|events and presentations|"
    r"schedule|reporting\s+calendar",
    re.I,
)
_CAL_HREF_TOKEN_RX = re.compile(r"calendar|events|schedule|upcoming", re.I)


MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
    "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}

_P1 = re.compile(
    r"(\d{1,2})\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\.?\s+(\d{4})",
    re.IGNORECASE,
)
_P2 = re.compile(
    r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_P3 = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_P4 = re.compile(r"\b(\d{1,2})[\/\.](\d{1,2})[\/\.](\d{4})\b")
# Bare month-day (no year) — e.g. "Nov 05" or "05 Nov" — common on IR pages that
# use a "2026" section header above a list of events. Year is resolved from the
# nearest preceding standalone year token (or the next-future occurrence).
_P5 = re.compile(
    r"(?<!\w)(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC|"
    r"JANUARY|FEBRUARY|MARCH|APRIL|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
    r"\.?\s+(\d{1,2})(?!\s*\d{2,4})(?!\w)",
    re.IGNORECASE,
)
_P6 = re.compile(
    r"(?<!\w)(\d{1,2})\s+"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC|"
    r"JANUARY|FEBRUARY|MARCH|APRIL|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
    r"\.?(?!\s*\d{2,4})(?!\w)",
    re.IGNORECASE,
)
_RE_YEAR_HEADER = re.compile(r"\b(20\d{2})\b")

_CONTEXT_WINDOW = 300
# "Entry" boundary: blank lines (or whitespace-only lines) between calendar rows.
# Splitting on single newlines is too tight — many IR pages spread one entry across
# 2-3 lines (date / label / description). Splitting on blank lines keeps a row's
# lines together while stopping labels leaking between adjacent rows.
_RE_BLANK_LINE = re.compile(r"\n[ \t ]*\n")

# Event-type detection (order matters: check CALL first so a "results call" is CALL).
_TYPE_ORDER: List[Tuple[str, re.Pattern]] = [
    ("Earnings Call",   re.compile(r"EARNINGS\s+CALL|CONFERENCE\s+CALL|ANALYST\s+CALL|WEBCAST", re.I)),
    ("Trading Update",  re.compile(r"TRADING\s+UPDATE|QUARTERLY\s+STATEMENT", re.I)),
    ("AGM",             re.compile(r"\bAGM\b|ANNUAL\s+GENERAL\s+MEETING", re.I)),
    ("Results",         re.compile(
        r"RESULTS|EARNINGS(?!\s+CALL)|INTERIM(?:\s+REPORT)?|"
        r"ANNUAL\s+REPORT|PRELIM|"
        r"HALF[- ]?YEAR(?:LY)?|FULL[- ]?YEAR(?:LY)?|"
        r"QUARTERLY\s+(?:FINANCIAL\s+)?REPORT|FINANCIAL\s+REPORT|"
        # Bare "Q1 Report" / "H1 Report" / "Q3 presentation" — the period token
        # combined with a reporting-verb is enough signal. Year is optional between
        # them ("Q3 2026 presentation").
        r"\b(?:Q[1-4]|H[12]|FY)\s+(?:20\d{2}\s+)?(?:REPORT|PRESENTATION|STATEMENT)\b",
        re.I,
    )),
]

# Period detection.
_RE_PERIOD_CANONICAL = re.compile(r"\b(Q[1-4]|H[12]|FY|1H|2H|HY)\s*(?:FY)?\s*(\d{2,4})?\b", re.I)
_RE_PERIOD_WORDY = re.compile(
    r"\b(FIRST|SECOND|THIRD|FOURTH|1ST|2ND|3RD|4TH)\s+QUARTER\b|\b(HALF[- ]YEAR|FULL[- ]YEAR|ANNUAL)\b",
    re.I,
)

_WORDY_TO_PERIOD = {
    "FIRST": "Q1", "1ST": "Q1",
    "SECOND": "Q2", "2ND": "Q2",
    "THIRD": "Q3", "3RD": "Q3",
    "FOURTH": "Q4", "4TH": "Q4",
    "HALF-YEAR": "H1", "HALF YEAR": "H1", "HY": "H1", "1H": "H1", "2H": "H2",
    "FULL-YEAR": "FY", "FULL YEAR": "FY", "ANNUAL": "FY", "FY": "FY",
}

# Windows that contain any of these are almost never real event mentions.
_RE_BOILERPLATE = re.compile(
    r"COOKIE|CONSENT|PRIVACY|DISCLAIMER|WARNING!|I DO NOT AGREE|"
    r"SUBSCRIBE|NEWSLETTER|CONTACT US|FOLLOW US|COPYRIGHT|"
    r"CAREERS|SITEMAP|ALL RIGHTS RESERVED|"
    r"SILENT\s+PERIOD|QUIET\s+PERIOD|CLOSED\s+PERIOD|BLACKOUT\s+PERIOD",
    re.I,
)

# Section markers indicating an event has already happened / been archived —
# skip any date whose surrounding text is nested under one of these headings.
_RE_PAST_SECTION = re.compile(
    r"\b(?:past\s+event|previous\s+event|archived\s+event|archive|past\s+webcast|"
    r"past\s+presentation|reported|announced)\b",
    re.I,
)

# Entry types we explicitly DO NOT want to flag (investor days, CMDs, roadshows —
# they aren't earnings/results events). If the entry's dominant label is one of
# these, we skip even if it contains Q1/H1/FY tokens elsewhere.
_RE_NON_EARNINGS = re.compile(
    r"CAPITAL\s+MARKETS?\s+DAY|INVESTOR\s+DAY|ANALYST\s+DAY|"
    r"ROADSHOW|SITE\s+VISIT",
    re.I,
)

# Live-timestamp guard: any date immediately followed by "HH:MM" or "at HH:MM"
# is almost always a rendered page-load timestamp, not a calendar entry.
_RE_TIMESTAMP_TAIL = re.compile(r"\s*(?:at\s+)?\d{1,2}:\d{2}(:\d{2})?", re.I)


@dataclass
class IRPageResult:
    company_id: str
    street_name: str
    ir_url: str
    text_len: int
    events: List[dict] = field(default_factory=list)
    error: Optional[str] = None
    rescue_text: Optional[str] = None  # rendered text for manual-mode LLM rescue


def _detect_type(window: str) -> Optional[str]:
    for label, rx in _TYPE_ORDER:
        if rx.search(window):
            return label
    return None


_RE_INTERIM_HINT = re.compile(r"\bINTERIM(?:\s+RESULTS?|\s+REPORT)?\b|\bHALF[- ]?YEAR(?:LY)?\b|\bHY\b", re.I)
_RE_FULLYEAR_HINT = re.compile(
    r"\bFULL[- ]?YEAR(?:LY)?\b|\bANNUAL\s+(?:RESULTS?|REPORT)\b|\bPRELIM(?:INARY)?\s+RESULTS?\b|\bFY\b",
    re.I,
)


def _infer_period_from_month(month: int) -> Optional[str]:
    """Rough seasonal mapping used only when the entry has no explicit period token."""
    if 1 <= month <= 3: return "Q4"       # Q4/FY reporting cycle
    if 4 <= month <= 6: return "Q1"
    if 7 <= month <= 9: return "H1"       # H1/Q2 mid-year cycle
    if 10 <= month <= 12: return "Q3"
    return None


def _detect_period(window: str, event_year: int, event_month: Optional[int] = None,
                   anchor: Optional[int] = None) -> Optional[str]:
    """Detect the reporting period in `window`. When `anchor` is provided (the
    date's position within the window), we prefer the period token nearest to it —
    that avoids "Q1 Report" and "Q3 Report" listed together mis-attributing.
    """
    def _all_periods() -> List[Tuple[int, str]]:
        found: List[Tuple[int, str]] = []
        for m in _RE_PERIOD_CANONICAL.finditer(window):
            tok = m.group(1).upper()
            period = _WORDY_TO_PERIOD.get(tok, tok if tok in {"Q1","Q2","Q3","Q4","H1","H2","FY"} else None)
            if period:
                found.append((m.start(), period))
        for m in _RE_PERIOD_WORDY.finditer(window):
            raw = (m.group(1) or m.group(2) or "").upper().replace("- ", "-")
            mapped = _WORDY_TO_PERIOD.get(raw)
            if mapped:
                found.append((m.start(), mapped))
        return found

    candidates = _all_periods()
    if candidates:
        if anchor is not None:
            candidates.sort(key=lambda pp: abs(pp[0] - anchor))
        return candidates[0][1]
    # Word-only hints — no explicit Q/H/FY token, but the label itself implies one.
    if _RE_INTERIM_HINT.search(window):
        return "H1"
    if _RE_FULLYEAR_HINT.search(window):
        return "FY"
    # No inference from month alone — that produced false positives by inventing
    # a period label from the date's month rather than the page's actual content.
    return None


def _resolve_year(text: str, pos: int, month: int, day: int, today: date) -> Optional[int]:
    """Find the year for a bare month-day date at text[pos].

    Strategy:
      1. Prefer the nearest standalone 20YY token before `pos` within 3000 chars —
         IR pages typically group events under a year header.
      2. Fall back to the year that makes (year, month, day) the next occurrence
         from today.
    """
    scan_start = max(0, pos - 3000)
    candidates = list(_RE_YEAR_HEADER.finditer(text, scan_start, pos))
    if candidates:
        try:
            return int(candidates[-1].group(1))
        except ValueError:
            pass
    for y in (today.year, today.year + 1):
        try:
            dt = date(y, month, day)
        except ValueError:
            continue
        if dt >= today:
            return y
    return None


def _extract_from_html_attrs(html: str, today: date, horizon: date) -> List[dict]:
    """Pull dates out of iCal/HTML5-datetime attributes and pair each with a title.

    Strict rule: the title MUST come from the same URL string as the date
    (e.g. Outlook add-to-cal `startdt=…&subject=…`). If there is no subject in
    the same URL, we skip the date — inferring a label from arbitrary nearby
    HTML produced systematic false positives (unrelated conferences labelled
    as Q3 earnings).
    """
    if not html:
        return []
    hits: dict = {}
    # Match a URL that carries BOTH a date and a subject/title together — the
    # "add to calendar" pattern used by Outlook / Google Calendar links.
    URL_WITH_DATE = re.compile(
        r"(?:startdt|dtstart|DTSTART|dates)=(\d{4})[-/T]?(\d{1,2})[-/T]?(\d{1,2})"
        r"[^\"'\s]*?"
        r"(?:subject|text|title|body)=([^&\"']{4,200})",
        re.I,
    )
    from urllib.parse import unquote_plus
    for m in URL_WITH_DATE.finditer(html):
        try:
            dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if dt < today or dt > horizon:
            continue
        title = unquote_plus(m.group(4))[:200].strip()
        if not title:
            continue
        # Skip non-earnings labels (conferences, roadshows, CMD).
        if _RE_NON_EARNINGS.search(title):
            continue
        etype = _detect_type(title)
        if not etype:
            continue
        period = _detect_period(title, dt.year, dt.month)
        if not period:
            continue
        key = (period, str(dt.year), etype)
        rec = {
            "date": dt.isoformat(),
            "title": f"{period} {dt.year} {etype}",
            "period": period,
            "year": str(dt.year),
            "type": etype,
            "entry": f"[html-attr] {title[:200]}",
        }
        if key not in hits or hits[key]["date"] > rec["date"]:
            hits[key] = rec
    return sorted(hits.values(), key=lambda h: h["date"])


def _extract_events(text: str, today: date, horizon: date) -> List[dict]:
    """Regex the page text for structured, near-context earnings/call/AGM events."""
    # Normalise whitespace-heavy pages: coalesce any run of blank / whitespace-only
    # lines into a single blank line. Sites like Daimler Truck emit long
    # `\n \n \n \n` sequences between calendar rows, which broke the extractor's
    # "look-ahead one blank line" heuristic.
    text = re.sub(r"(?:\n[ \t\xa0]*){2,}", "\n\n", text)
    raw_hits: List[dict] = []

    # Precompute date positions so we can truncate an entry at the NEXT date match —
    # this stops a single date's context from vacuum-cleaning up labels that belong
    # to adjacent calendar rows.
    date_positions: List[int] = []
    for pat in (_P1, _P2, _P3, _P4, _P5, _P6):
        for m in pat.finditer(text):
            date_positions.append(m.start())
    date_positions.sort()

    def _next_date_pos_after(pos: int) -> Optional[int]:
        import bisect
        i = bisect.bisect_right(date_positions, pos)
        if i < len(date_positions):
            return date_positions[i]
        return None

    def _entry_around(mstart: int, mend: int) -> str:
        """Return the calendar entry for a date match — tight window, forward only.

        We want the label that belongs to THIS specific date, not any adjacent
        row's. Rules:
          - Backward: just the immediate prefix on the same line (max 40 chars).
          - Forward: up to the NEXT date match on the page, capped at 200 chars.
            No blank-line extension — that logic was pulling in unrelated rows.
        """
        # Backward: same line only.
        prev_newline = text.rfind("\n", max(0, mstart - 40), mstart)
        start = prev_newline + 1 if prev_newline >= 0 else max(0, mstart - 40)

        # Forward: cap at 200 chars OR the next date match, whichever is closer.
        hard_end = min(len(text), mend + 200)
        next_dt = _next_date_pos_after(mend)
        if next_dt is not None and next_dt < hard_end:
            hard_end = next_dt

        return re.sub(r"[ \t]+", " ", text[start:hard_end]).strip()

    def push(dt: date, mstart: int, mend: int):
        if dt < today or dt > horizon:
            return
        entry = _entry_around(mstart, mend)
        if not entry or len(entry) > 800:
            return  # ambiguous — either empty or so long it spans multiple items
        # Skip dates that appear as a reporting-period reference ("as at 30 Sept
        # 2026", "period ended", "as of") rather than the event date itself.
        pre = text[max(0, mstart - 30):mstart].lower()
        if re.search(r"\b(?:as\s+at|as\s+of|ending|period\s+ended?|for\s+the\s+period\s+(?:ended?|ending))\s*$", pre):
            return
        if _RE_BOILERPLATE.search(entry):
            return
        # Reject-window for non-earnings labels (Capital Markets Day, Investor Day,
        # Roadshow) is wider than the type-detection window — these headings often
        # sit ABOVE the date on IR pages.
        reject_window = text[max(0, mstart - 400) : min(len(text), mend + 400)]
        if _RE_NON_EARNINGS.search(reject_window):
            return
        # Check if this date sits under a "past events" / "archive" heading —
        # look backward from the date position for such a heading in the last
        # ~500 chars without hitting a "upcoming events" heading first.
        prev_slice = text[max(0, mstart - 500):mstart].lower()
        past_pos = -1
        for m2 in _RE_PAST_SECTION.finditer(prev_slice):
            past_pos = m2.start()
        upcoming_pos = prev_slice.rfind("upcoming")
        forthcoming_pos = prev_slice.rfind("forthcoming")
        latest_upcoming = max(upcoming_pos, forthcoming_pos)
        if past_pos > latest_upcoming and past_pos >= 0:
            return  # this date is in a "past events" section
        etype = _detect_type(entry)
        if not etype:
            return
        # Anchor period-detection at the date's offset inside `entry` so the
        # closest Q/H/FY token wins over more distant ones.
        entry_search_start = text.find(entry[:40]) if entry else -1
        anchor = mstart - entry_search_start if entry_search_start >= 0 else None
        period = _detect_period(entry, dt.year, dt.month, anchor=anchor)
        if not period:
            return
        year = str(dt.year)
        title = f"{period} {year} {etype}"
        raw_hits.append({
            "date": dt.isoformat(),
            "title": title,
            "period": period,
            "year": year,
            "type": etype,
            "entry": entry,
        })

    def _is_timestamp(mend: int) -> bool:
        return bool(_RE_TIMESTAMP_TAIL.match(text[mend : mend + 15]))

    for m in _P1.finditer(text):
        if _is_timestamp(m.end()):
            continue
        try:
            d = int(m.group(1))
            mo = MONTH_MAP[m.group(2).upper().replace(".", "")]
            y = int(m.group(3))
            push(date(y, mo, d), m.start(), m.end())
        except (KeyError, ValueError):
            continue
    for m in _P2.finditer(text):
        if _is_timestamp(m.end()):
            continue
        try:
            mo = MONTH_MAP[m.group(1).upper().replace(".", "")]
            d = int(m.group(2))
            y = int(m.group(3))
            push(date(y, mo, d), m.start(), m.end())
        except (KeyError, ValueError):
            continue
    for m in _P3.finditer(text):
        # Skip page-load timestamps like "2026-09-02 09:12:39".
        tail = text[m.end() : m.end() + 10]
        if _RE_TIMESTAMP_TAIL.match(tail):
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            push(date(y, mo, d), m.start(), m.end())
        except ValueError:
            continue
    for m in _P4.finditer(text):
        if _is_timestamp(m.end()):
            continue
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            push(date(y, mo, d), m.start(), m.end())
        except ValueError:
            continue
    # Bare "Mon DD" — year resolved from nearest preceding year header.
    for m in _P5.finditer(text):
        try:
            mo = MONTH_MAP[m.group(1).upper().replace(".", "")]
            d = int(m.group(2))
        except (KeyError, ValueError):
            continue
        y = _resolve_year(text, m.start(), mo, d, today)
        if y is None:
            continue
        try:
            push(date(y, mo, d), m.start(), m.end())
        except ValueError:
            continue
    # Bare "DD Mon".
    for m in _P6.finditer(text):
        try:
            d = int(m.group(1))
            mo = MONTH_MAP[m.group(2).upper().replace(".", "")]
        except (KeyError, ValueError):
            continue
        y = _resolve_year(text, m.start(), mo, d, today)
        if y is None:
            continue
        try:
            push(date(y, mo, d), m.start(), m.end())
        except ValueError:
            continue

    # Dedupe: prefer the earliest date per (period, year, type) — different date
    # matches within one calendar entry (e.g. "20-22 October") collapse to one.
    keyed: dict = {}
    for h in raw_hits:
        k = (h["period"], h["year"], h["type"])
        if k not in keyed or h["date"] < keyed[k]["date"]:
            keyed[k] = h
    return sorted(keyed.values(), key=lambda h: h["date"])


def scrape_one(
    context: BrowserContext,
    company_id: str,
    street_name: str,
    ir_url: str,
    lookahead_days: int,
    timeout_s: int,
) -> IRPageResult:
    page = context.new_page()
    result = IRPageResult(company_id=company_id, street_name=street_name, ir_url=ir_url, text_len=0)
    try:
        page.add_init_script(
            """
            // Best-effort dismisser for cookie / consent / country banners. Tries
            // reject-first patterns, then falls back to accept patterns so we can
            // at least get past. Also handles Onetrust and TrustArc classes.
            const rejectRx = /reject all|decline all|only necessary|reject cookies|deny|refuse/i;
            const acceptRx = /accept all|allow all|i agree|got it|ok|continue|stay on current|proceed/i;
            const clickIfMatch = (el, rx) => {
                const t = (el.innerText || el.textContent || "").trim();
                if (rx.test(t)) { try { el.click(); return true; } catch(e) {} }
                return false;
            };
            const sweep = () => {
                const candidates = document.querySelectorAll(
                    'button, a, [role="button"], .ot-pc-refuse-all-handler, #onetrust-reject-all-handler, ' +
                    '#onetrust-accept-btn-handler, .cookie-consent__button, .cc-btn'
                );
                for (const el of candidates) {
                    if (clickIfMatch(el, rejectRx)) return;
                }
                for (const el of candidates) {
                    if (clickIfMatch(el, acceptRx)) return;
                }
            };
            new MutationObserver(sweep).observe(document.documentElement, {childList: true, subtree: true});
            setTimeout(sweep, 500);
            setTimeout(sweep, 2000);
            """
        )
        page.goto(ir_url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        # Scroll to trigger any lazy-loaded calendar widgets, then wait briefly.
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
        except Exception:
            pass
        # Primary: use body.innerText — respects layout, gives blank-line boundaries
        # the extractor is tuned for. Fallback (only when zero events found) uses a
        # DOM-walk textContent to reach text hidden behind CSS display:none, plus
        # any iframe bodies for widgets that render inside iframes.
        text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 60000)")
        result.text_len = len(text or "")
        if result.text_len < 500:
            # Fall through to the fallback text if innerText is thin.
            text_primary_short = True
        else:
            text_primary_short = False

        today = date.today()
        horizon = today + timedelta(days=lookahead_days)
        result.events = _extract_events(text or "", today, horizon)

        # Additive richer-text extractor: DOM-walk textContent (includes CSS-hidden
        # nodes) + iframe bodies. Merge into results — a page whose innerText has
        # some events might still have OTHER events hidden behind a tab.
        try:
            walk_text = page.evaluate("""
                () => {
                    function walk(node, out) {
                        if (node.nodeType === 3) { out.push(node.textContent); return; }
                        if (node.nodeType !== 1) return;
                        const tag = node.tagName;
                        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return;
                        const blk = /^(DIV|P|LI|TR|SECTION|ARTICLE|H[1-6]|UL|OL|TABLE|TBODY|THEAD|TFOOT|BR)$/.test(tag);
                        if (blk) out.push('\\n\\n');
                        for (const c of node.childNodes) walk(c, out);
                        if (blk) out.push('\\n');
                    }
                    const out = [];
                    if (document.body) walk(document.body, out);
                    return out.join('').replace(/[ \\t\\xa0]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n').slice(0, 80000);
                }
            """)
        except Exception:
            walk_text = ""
        iframe_text = ""
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                itext = fr.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                itext = ""
            if itext:
                iframe_text += "\n\n" + itext[:20000]
        combined = (walk_text or "") + iframe_text
        result.text_len = max(result.text_len, len(combined))
        if len(combined) >= 500:
            more = _extract_events(combined, today, horizon)
            existing_keys = {(e["period"], e["year"], e["type"]) for e in result.events}
            for ev in more:
                k = (ev["period"], ev["year"], ev["type"])
                if k not in existing_keys:
                    result.events.append(ev)
                    existing_keys.add(k)

        # Third-tier fallback: if we still have no events, look for a link on the
        # current page whose text/href points at a calendar/events sub-page and
        # try that URL. Catches "landing page" cases like Arkema, BlueNord.
        if not result.events:
            try:
                links = page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        href: a.href,
                        text: (a.innerText || a.textContent || '').trim().slice(0, 120)
                    }))
                """)
            except Exception:
                links = []
            from urllib.parse import urlparse
            base_host = urlparse(ir_url).netloc
            candidates = []
            seen = set()
            for l in links:
                href = (l.get("href") or "").split("#")[0]
                text = l.get("text") or ""
                if not href or href.startswith("javascript:"):
                    continue
                if href == ir_url or href.rstrip("/") == ir_url.rstrip("/"):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                host = urlparse(href).netloc
                if host and host != base_host:
                    continue
                score = 0
                if _CAL_LINK_TEXT_RX.search(text): score += 3
                if _CAL_HREF_TOKEN_RX.search(href): score += 2
                if score > 0:
                    candidates.append((score, href))
            candidates.sort(key=lambda x: -x[0])
            for _, link in candidates[:3]:
                try:
                    page.goto(link, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                    try: page.wait_for_load_state("networkidle", timeout=6_000)
                    except Exception: pass
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
                    page.evaluate("() => window.scrollTo(0, 0)")
                    link_text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 60000)")
                    if link_text and len(link_text) >= 500:
                        events2 = _extract_events(link_text, today, horizon)
                        if events2:
                            result.events = events2
                            result.text_len = max(result.text_len, len(link_text))
                            result.ir_url = link  # note: caller sees the sub-page we actually used
                            break
                except Exception:
                    continue

        # Additive fourth extractor: parse structured dates out of raw HTML
        # attributes (iCal add-to-calendar links carry startdt=YYYY-MM-DD, HTML5
        # <time datetime=>, data-date attrs). SPA calendars often hide the visible
        # label but keep these attributes intact. We MERGE these with the text
        # extractor's output so a text-side false positive can't block the real
        # date.
        try:
            html = page.content()
        except Exception:
            html = ""
        events_from_html = _extract_from_html_attrs(html, today, horizon)
        if events_from_html:
            existing_keys = {(e["period"], e["year"], e["type"]) for e in result.events}
            for ev in events_from_html:
                k = (ev["period"], ev["year"], ev["type"])
                if k not in existing_keys:
                    result.events.append(ev)
                    existing_keys.add(k)
            result.events.sort(key=lambda h: h["date"])

        if result.text_len < 500 and not result.events:
            result.error = "thin_page"
        return result
    except Exception as exc:
        result.error = f"scrape_error: {type(exc).__name__}: {str(exc)[:120]}"
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass


def scrape_many(
    context: BrowserContext,
    companies: List[dict],
    lookahead_days: int,
    timeout_s: int,
    concurrency: int = 1,
) -> List[IRPageResult]:
    """Scrape a batch of companies serially in a shared browser context.

    `companies` is a list of dicts with keys: company_id, street_name, ir_url.
    """
    if concurrency != 1:
        log.info("IR scrape: concurrency=%d requested, running serial (sync Playwright)", concurrency)

    results: List[IRPageResult] = []
    total = len(companies)
    for i, c in enumerate(companies, start=1):
        r = scrape_one(
            context,
            c["company_id"],
            c["street_name"],
            c["ir_url"],
            lookahead_days,
            timeout_s,
        )
        results.append(r)
        if i % 10 == 0 or i == total or r.error:
            log.info(
                "IR scrape progress: %d/%d (last: %s, events=%d, err=%s)",
                i, total, r.street_name, len(r.events), r.error or "-",
            )
    return results
