"""Phase 2: SEC EDGAR poller.

For every company in the source registry classified `sec_edgar`, fetch their
recent 8-K / 6-K filings, extract earnings-date announcements ("Company will
report Q3 2026 results on November 4, 2026") and compare against 9fin.

Structured, free, no scraping guesswork — the filing text is either explicit
about the date or it isn't.

Output:
    state/announcements_{slug}.json — one row per announcement candidate
    logs/sec_review_{date}.md      — the analyst review report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config, configure_logging
from calendar_sync.sources.ninefin_client import fetch_calendar, load_context

log = logging.getLogger("poll_sec")

UA = "9fin credit desk calendar-sync research@9fin.com"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_DOC = "https://www.sec.gov/Archives/edgar/data/{cik_no_leading}/{accession_nodash}/{doc}"

# Forms that carry earnings-date announcements.
EARNINGS_FORMS = {"8-K", "6-K"}
# 8-K item codes that typically carry earnings news.
EARNINGS_ITEMS = {"2.02", "7.01", "8.01"}

# HTML → text
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")

# Date detectors — same shapes as elsewhere, tuned for filings which mostly use
# "Month D, YYYY" format.
_MONTH = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
_MON_INT = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12,
            "january":1,"february":2,"march":3,"april":4,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
_DATE_PATTERNS = [
    (re.compile(rf"({_MONTH})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I), "mon_d_y"),
    (re.compile(rf"(\d{{1,2}})\s+({_MONTH})\.?\s+(\d{{4}})", re.I), "d_mon_y"),
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
]

# The trigger phrases we look for near a future date to accept it as an
# earnings-announcement. Deliberately narrow to avoid false positives from
# unrelated content in the filing.
_TRIGGERS = re.compile(
    # "will (be) released/announced/reported" family — allow optional "be" or
    # "expected to be" between the modal and the verb.
    r"(?:will|shall|plans?\s+to|expects?\s+to|is\s+(?:scheduled|expected|planned|due)\s+to|"
    r"intends?\s+to|is\s+to)\s+(?:be\s+)?"
    r"(?:announced?|released?|reported?|published?|held|hosted|issued|"
    r"holding|releasing|reporting|publishing|hosting|issuing)"
    r"|"
    # Standalone earnings-scheduling phrases.
    r"\b(?:earnings|quarterly|half[- ]?year|full[- ]?year|nine[- ]?month|interim|"
    r"first[- ]?quarter|second[- ]?quarter|third[- ]?quarter|fourth[- ]?quarter|"
    r"q[1-4](?:\s*\d{2,4})?|h[12](?:\s*\d{2,4})?)\s+"
    r"(?:results?|earnings|report|call|conference|webcast|announcement|"
    r"trading\s+(?:report|update|statement)|"
    r"financial\s+(?:report|statement|calendar))\b"
    r"|"
    r"\btrading\s+(?:report|update|statement)\b"
    r"|"
    r"\bforward\s+calendar\b",
    re.I,
)
_PERIOD_HINT = re.compile(
    r"\b(Q[1-4]|H[12]|FY|first quarter|second quarter|third quarter|fourth quarter|"
    r"half[- ]year|full[- ]year|nine[- ]month|"
    r"1H|2H)\b",
    re.I,
)
_TYPE_HINT = [
    ("Earnings Call",   re.compile(r"conference\s+call|earnings\s+call|analyst\s+call|webcast", re.I)),
    ("Trading Update",  re.compile(r"trading\s+(?:report|update|statement)|quarterly\s+statement", re.I)),
    ("Results",         re.compile(r"results|earnings|financial\s+report|interim\s+report|quarterly\s+report|half[- ]?year\s+report|full[- ]?year|nine[- ]?month|annual\s+report", re.I)),
]
_WORDY_PERIOD = {"first quarter": "Q1", "second quarter": "Q2", "third quarter": "Q3", "fourth quarter": "Q4",
                 "half-year": "H1", "half year": "H1", "full-year": "FY", "full year": "FY",
                 "nine-month": "Q3", "nine month": "Q3", "1h": "H1", "2h": "H2"}


@dataclass
class Announcement:
    company: str
    cik: str
    ticker: str
    filing_date: str
    form: str
    accession: str
    filing_url: str
    event_date: str
    event_period: str
    event_year: str
    event_type: str
    source_excerpt: str
    on_9fin: bool = False
    ninefin_events: List[dict] = field(default_factory=list)


def _sec_get(url: str, sleep: float = 0.12) -> Optional[requests.Response]:
    time.sleep(sleep)
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}, timeout=25)
        if r.status_code == 200:
            return r
        log.warning("SEC GET %s → HTTP %d", url, r.status_code)
    except requests.RequestException as e:
        log.warning("SEC GET %s → %s", url, e)
    return None


def _fetch_submissions(cik: str) -> Optional[dict]:
    r = _sec_get(SEC_SUBMISSIONS.format(cik=str(cik).zfill(10)))
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _fetch_doc_text(cik: str, accession: str, doc: str) -> Tuple[str, str]:
    """Fetch the primary document of a filing as plain text. Returns (text, url)."""
    cik_int = str(int(cik))
    acc_nodash = accession.replace("-", "")
    url = SEC_DOC.format(cik_no_leading=cik_int, accession_nodash=acc_nodash, doc=doc)
    r = _sec_get(url, sleep=0.15)
    if r is None:
        return "", url
    text = r.text
    if "<" in text[:200]:
        text = _TAG.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text, url


def _extract_announcements_from_text(text: str, today: date, horizon: date) -> List[Tuple[date, str, str, str, str]]:
    """Return list of (event_date, period, year, type, source_excerpt).

    We find every future date; for each, we check the ±250-char window for one
    of the trigger phrases; if present, we build a normalised event tuple.
    """
    out: List[Tuple[date, str, str, str, str]] = []
    seen_dates: set = set()

    for pat, kind in _DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                if kind == "mon_d_y":
                    mo = _MON_INT[m.group(1).lower().rstrip(".")]
                    d = int(m.group(2)); y = int(m.group(3))
                elif kind == "d_mon_y":
                    d = int(m.group(1))
                    mo = _MON_INT[m.group(2).lower().rstrip(".")]
                    y = int(m.group(3))
                elif kind == "ymd":
                    y = int(m.group(1)); mo = int(m.group(2)); d = int(m.group(3))
                else:
                    continue
                dt = date(y, mo, d)
            except (KeyError, ValueError):
                continue
            if dt < today or dt > horizon:
                continue
            if dt in seen_dates:
                continue

            window = text[max(0, m.start() - 250) : min(len(text), m.end() + 250)]
            if not _TRIGGERS.search(window):
                continue

            # Determine period
            period = ""
            pm = _PERIOD_HINT.search(window)
            if pm:
                tok = pm.group(1).lower().replace(" ", "-")
                if tok.upper() in {"Q1","Q2","Q3","Q4","H1","H2","FY"}:
                    period = tok.upper()
                else:
                    period = _WORDY_PERIOD.get(tok.lower(), "")
                    if not period:
                        # Try wordy variants without dash
                        period = _WORDY_PERIOD.get(pm.group(1).lower(), "")
            if not period:
                # Infer from month of event
                mm = dt.month
                if mm <= 3: period = "Q4"
                elif mm <= 6: period = "Q1"
                elif mm <= 9: period = "H1"
                else: period = "Q3"

            # Determine type
            etype = "Results"
            for label, rx in _TYPE_HINT:
                if rx.search(window):
                    etype = label
                    break

            # Excerpt — collapse whitespace
            excerpt = re.sub(r"\s+", " ", window).strip()[:400]
            out.append((dt, period, str(dt.year), etype, excerpt))
            seen_dates.add(dt)

    return out


def _process_filings(company: str, cik: str, ticker: str, subs: dict,
                     today: date, horizon: date, lookback_days: int
                     ) -> List[Announcement]:
    recent = subs.get("filings", {}).get("recent", {})
    if not recent:
        return []
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    forms = recent.get("form", [])
    items = recent.get("items", [])
    docs = recent.get("primaryDocument", [])

    cutoff = (today - timedelta(days=lookback_days)).isoformat()
    out: List[Announcement] = []
    for i in range(len(accessions)):
        form = forms[i]
        if form not in EARNINGS_FORMS:
            continue
        f_date = dates[i]
        if f_date < cutoff:
            continue
        # For 8-K, require earnings-adjacent items. 6-K is a catch-all.
        item_str = items[i] if i < len(items) else ""
        if form == "8-K":
            item_set = {x.strip() for x in item_str.split(",") if x.strip()}
            if not (item_set & EARNINGS_ITEMS):
                continue
        acc = accessions[i]
        doc = docs[i] if i < len(docs) else ""
        if not doc:
            continue

        text, url = _fetch_doc_text(cik, acc, doc)
        if not text:
            continue
        for dt, period, year, etype, excerpt in _extract_announcements_from_text(text, today, horizon):
            out.append(Announcement(
                company=company, cik=cik, ticker=ticker,
                filing_date=f_date, form=form, accession=acc, filing_url=url,
                event_date=dt.isoformat(), event_period=period,
                event_year=year, event_type=etype,
                source_excerpt=excerpt,
            ))
    return out


def _ninefin_by_company(cfg: Config) -> Dict[str, List[dict]]:
    from playwright.sync_api import sync_playwright
    state_path = cfg.state_dir / "9fin_state.json"
    events = []
    with sync_playwright() as p:
        ctx = load_context(p, state_path, headless=True)
        try:
            events = fetch_calendar(ctx, cfg.ninefin_base_url, cfg.lookahead_days)
        finally:
            ctx.close()
    by = {}
    for e in events:
        by.setdefault(e.company.lower().strip(), []).append(
            {"date": e.start_date.isoformat(), "title": e.title})
    return by


def main() -> int:
    configure_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--sources", type=Path, required=True,
                   help="Source registry JSON (from classify_sources.py)")
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument("--horizon-days", type=int, default=120)
    args = p.parse_args()

    cfg = Config.load()
    sources = json.loads(args.sources.read_text())
    sec_entries = [s for s in sources if s["source_type"] == "sec_edgar"]
    log.info("SEC-classified companies to poll: %d", len(sec_entries))

    log.info("Fetching 9fin calendar…")
    nf_by = _ninefin_by_company(cfg)
    log.info("9fin has %d distinct companies", len(nf_by))

    today = date.today()
    horizon = today + timedelta(days=args.horizon_days)

    all_announcements: List[Announcement] = []
    for i, s in enumerate(sec_entries, 1):
        log.info("[%d/%d] %s (CIK %s)…", i, len(sec_entries), s["company"], s["source_id"])
        subs = _fetch_submissions(s["source_id"])
        if not subs:
            log.warning("  no submissions payload")
            continue
        anns = _process_filings(
            s["company"], s["source_id"], s.get("match_ticker",""),
            subs, today, horizon, args.lookback_days,
        )
        # Cross-check vs 9fin (same company, same date within ±5 days)
        nf_events = nf_by.get(s["company"].lower().strip(), [])
        nf_dates = [datetime.strptime(e["date"], "%Y-%m-%d").date() for e in nf_events]
        for a in anns:
            ed = datetime.strptime(a.event_date, "%Y-%m-%d").date()
            a.on_9fin = any(abs((ed - d).days) <= 5 for d in nf_dates)
            a.ninefin_events = nf_events
        # Dedupe: same event_date + period + type + year → keep latest filing
        keyed = {}
        for a in anns:
            k = (a.event_date, a.event_period, a.event_type)
            if k not in keyed or a.filing_date > keyed[k].filing_date:
                keyed[k] = a
        anns = list(keyed.values())
        log.info("  → %d announcement(s)", len(anns))
        for a in anns:
            marker = "✓ 9fin" if a.on_9fin else "✗ NOT on 9fin"
            log.info("     %s %s %s   [%s]", a.event_date, a.event_period, a.event_type, marker)
        all_announcements.extend(anns)

    # Write results
    out_slug = args.sources.stem.replace("sources_", "")
    out_path = cfg.state_dir / f"announcements_{out_slug}.json"
    with out_path.open("w") as f:
        json.dump([a.__dict__ for a in all_announcements], f, indent=2)
    log.info("Wrote %s", out_path)

    # Summary
    n_gap = sum(1 for a in all_announcements if not a.on_9fin)
    n_covered = sum(1 for a in all_announcements if a.on_9fin)
    log.info("=== Summary ===")
    log.info("  Announcements extracted: %d", len(all_announcements))
    log.info("  Covered by 9fin:         %d", n_covered)
    log.info("  Gaps to file on 9fin:    %d", n_gap)
    if n_gap:
        log.info("Gaps:")
        for a in all_announcements:
            if not a.on_9fin:
                log.info("  %s %s → %s %s (%s)", a.company, a.event_date, a.event_period, a.event_type, a.filing_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
