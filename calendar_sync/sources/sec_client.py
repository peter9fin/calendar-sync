"""SEC EDGAR event source.

For a set of CIKs, fetches recent 8-K / 6-K filings, extracts
earnings-date announcements ("Company will report Q3 2026 results on
November 4, 2026"), and returns them in the {date, title} shape the
diff step expects.

Regex + extraction logic mirrors scripts/poll_sec.py — kept
self-contained here so the main pipeline never has to import from
scripts/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

UA = "9fin credit desk calendar-sync research@9fin.com"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_DOC = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"

EARNINGS_FORMS = {"8-K", "6-K"}
EARNINGS_ITEMS = {"2.02", "7.01", "8.01"}
SEC_MIN_INTERVAL = 0.11  # ~9 req/s, well under SEC's 10 req/s cap

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")

_MONTH = ("Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
          "January|February|March|April|June|July|August|September|"
          "October|November|December")
_MON_INT = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,
            "sep":9,"sept":9,"oct":10,"nov":11,"dec":12,
            "january":1,"february":2,"march":3,"april":4,"june":6,"july":7,
            "august":8,"september":9,"october":10,"november":11,"december":12}
_DATE_PATTERNS = [
    (re.compile(rf"({_MONTH})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I), "mon_d_y"),
    (re.compile(rf"(\d{{1,2}})\s+({_MONTH})\.?\s+(\d{{4}})", re.I), "d_mon_y"),
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
]
_TRIGGERS = re.compile(
    r"(?:will|shall|plans?\s+to|expects?\s+to|is\s+(?:scheduled|expected|planned|due)\s+to|"
    r"intends?\s+to|is\s+to)\s+(?:be\s+)?"
    r"(?:announced?|released?|reported?|published?|held|hosted|issued|"
    r"holding|releasing|reporting|publishing|hosting|issuing)"
    r"|"
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
    r"half[- ]year|full[- ]year|nine[- ]month|1H|2H)\b",
    re.I,
)
_TYPE_HINT = [
    ("Earnings Call",  re.compile(r"conference\s+call|earnings\s+call|analyst\s+call|webcast", re.I)),
    ("Trading Update", re.compile(r"trading\s+(?:report|update|statement)|quarterly\s+statement", re.I)),
    ("Results",        re.compile(r"results|earnings|financial\s+report|interim\s+report|quarterly\s+report|half[- ]?year\s+report|full[- ]?year|nine[- ]?month|annual\s+report", re.I)),
]
_WORDY_PERIOD = {"first quarter":"Q1","second quarter":"Q2","third quarter":"Q3","fourth quarter":"Q4",
                 "half-year":"H1","half year":"H1","full-year":"FY","full year":"FY",
                 "nine-month":"Q3","nine month":"Q3","1h":"H1","2h":"H2"}

_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|inc\.|incorporated|corp|corp\.|corporation|co|co\.|company|"
    r"ltd|ltd\.|limited|plc|nv|n\.v\.|sa|s\.a\.|se|ag|ab|kgaa|holdings|"
    r"group|international|intl|the|and|&)\b",
    re.I,
)


def _normalise(name: str) -> str:
    """Same aggressive normalisation as classify_sources._normalise."""
    s = name.lower().strip()
    s = re.sub(r"[.,'()/\-]", " ", s)
    s = _LEGAL_SUFFIX.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing single letters (Nordic A/S residue etc.)
    while s and len(s.split()[-1]) == 1:
        s = " ".join(s.split()[:-1])
    return s


def load_sec_ticker_index(cache_path: Path) -> Dict[str, dict]:
    """Load or download the SEC company_tickers.json. Return {normalised_name: entry}."""
    if not cache_path.exists() or cache_path.stat().st_size < 1000:
        import requests
        log.info("SEC ticker cache missing — downloading %s", SEC_TICKERS_URL)
        r = requests.get(SEC_TICKERS_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
    raw = json.loads(cache_path.read_text())
    by_norm: Dict[str, dict] = {}
    for entry in raw.values():
        norm = _normalise(entry.get("title", ""))
        if norm and norm not in by_norm:
            by_norm[norm] = entry
    return by_norm


def match_ciks(joined: List[dict], by_norm: Dict[str, dict]) -> Dict[str, str]:
    """Exact-match every joined company name to a CIK. Returns {company_id: cik10}."""
    out: Dict[str, str] = {}
    for row in joined:
        hit = by_norm.get(_normalise(row["name"]))
        if hit:
            out[str(row["id"])] = str(hit["cik_str"]).zfill(10)
    return out


def _extract_events_from_text(text: str, today: date, horizon: date) -> List[dict]:
    """Return list of {date: date, title: str} — dates in [today, horizon] with a trigger phrase nearby."""
    out: List[dict] = []
    seen: set = set()
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
                else:  # ymd
                    y = int(m.group(1)); mo = int(m.group(2)); d = int(m.group(3))
                dt = date(y, mo, d)
            except (KeyError, ValueError):
                continue
            if dt < today or dt > horizon or dt in seen:
                continue
            window = text[max(0, m.start()-250):min(len(text), m.end()+250)]
            if not _TRIGGERS.search(window):
                continue
            seen.add(dt)
            # Build a title: {Period} {Year} {Type}
            period = ""
            pm = _PERIOD_HINT.search(window)
            if pm:
                tok = pm.group(1).lower()
                period = tok.upper() if tok.upper() in {"Q1","Q2","Q3","Q4","H1","H2","FY","1H","2H"} \
                         else _WORDY_PERIOD.get(tok.replace(" ", "-"), _WORDY_PERIOD.get(tok, ""))
            if not period:
                mm = dt.month
                period = "Q4" if mm <= 3 else "Q1" if mm <= 6 else "H1" if mm <= 9 else "Q3"
            etype = "Results"
            for label, rx in _TYPE_HINT:
                if rx.search(window):
                    etype = label
                    break
            title = f"{period} {dt.year} {etype}"
            out.append({"date": dt, "title": title})
    return out


class _RateLimiter:
    """Serialises SEC hits to ~9 req/s regardless of concurrency."""
    def __init__(self, min_interval: float):
        self._min = min_interval
        self._lock = asyncio.Lock()
        self._next_ok = 0.0

    async def wait(self):
        loop = asyncio.get_event_loop()
        async with self._lock:
            now = loop.time()
            delay = self._next_ok - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_ok = loop.time() + self._min


async def _fetch(session: aiohttp.ClientSession, url: str, limiter: _RateLimiter,
                 timeout: int = 25) -> Optional[str]:
    await limiter.wait()
    try:
        async with session.get(
            url,
            headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                return await r.text()
            log.debug("SEC GET %s → HTTP %d", url, r.status)
    except Exception as e:
        log.debug("SEC GET %s → %s", url, type(e).__name__)
    return None


async def _events_for_cik(session: aiohttp.ClientSession, cik: str, limiter: _RateLimiter,
                          today: date, horizon: date, lookback_days: int) -> List[dict]:
    text = await _fetch(session, SEC_SUBMISSIONS.format(cik=cik), limiter)
    if not text:
        return []
    try:
        subs = json.loads(text)
    except ValueError:
        return []
    recent = subs.get("filings", {}).get("recent", {})
    if not recent:
        return []
    accessions = recent.get("accessionNumber", [])
    dates_    = recent.get("filingDate", [])
    forms     = recent.get("form", [])
    items     = recent.get("items", [])
    docs      = recent.get("primaryDocument", [])
    cutoff    = (today - timedelta(days=lookback_days)).isoformat()

    events: List[dict] = []
    for i in range(len(accessions)):
        form = forms[i]
        if form not in EARNINGS_FORMS:
            continue
        if dates_[i] < cutoff:
            continue
        if form == "8-K":
            item_str = items[i] if i < len(items) else ""
            item_set = {x.strip() for x in item_str.split(",") if x.strip()}
            if not (item_set & EARNINGS_ITEMS):
                continue
        acc = accessions[i]
        doc = docs[i] if i < len(docs) else ""
        if not doc:
            continue
        doc_url = SEC_DOC.format(
            cik_int=str(int(cik)),
            acc_nodash=acc.replace("-", ""),
            doc=doc,
        )
        body = await _fetch(session, doc_url, limiter)
        if not body:
            continue
        if "<" in body[:200]:
            body = _TAG.sub(" ", body)
        body = _WS.sub(" ", body)
        events.extend(_extract_events_from_text(body, today, horizon))

    # Dedupe by (date, title) — the same event tends to appear in successive filings
    seen: set = set()
    out: List[dict] = []
    for e in events:
        k = (e["date"].isoformat(), e["title"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


async def fetch_sec_events(
    ciks_by_company: Dict[str, str],
    today: date,
    horizon: date,
    lookback_days: int = 90,
    concurrency: int = 8,
) -> Dict[str, List[dict]]:
    """Concurrently fetch SEC events for a batch of companies.

    Returns {company_id: [{date: date, title: str}]}.
    Rate-limited to ~9 req/s regardless of concurrency (SEC's 10 req/s cap).
    """
    if not ciks_by_company:
        return {}
    limiter = _RateLimiter(SEC_MIN_INTERVAL)
    connector = aiohttp.TCPConnector(limit=concurrency)
    sem = asyncio.Semaphore(concurrency)
    processed = {"n": 0}
    total = len(ciks_by_company)

    async with aiohttp.ClientSession(connector=connector) as session:
        async def _one(cid: str, cik: str):
            async with sem:
                events = await _events_for_cik(session, cik, limiter, today, horizon, lookback_days)
                processed["n"] += 1
                if processed["n"] % 50 == 0 or processed["n"] == total:
                    log.info("SEC progress: %d/%d", processed["n"], total)
                return cid, events
        pairs = await asyncio.gather(*[_one(c, k) for c, k in ciks_by_company.items()])
    return dict(pairs)
