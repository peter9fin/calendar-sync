"""Phase 1: source classifier.

For each company on an associate's Monday book, decide the best announcement
source. Currently distinguishes:
    - sec_edgar    : company files 8-Ks with the SEC (highest confidence)
    - scrape_fallback : anything else (uses the current Monday URL)

Output: state/sources_{slug}.json — one JSON row per company, ready for the
downstream pollers.

Design notes:
    - We use SEC's `company_tickers.json` (public, no auth) for name→CIK matching
    - Names are normalised (strip Inc/PLC/SA/etc.) before matching
    - When two candidates match at the same normalised length, we pick the one
      whose recent submissions include an 8-K in the last 12 months.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config, configure_logging
from calendar_sync.sources.monday_client import fetch_nmd_rows

log = logging.getLogger("classify")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# SEC requires a descriptive User-Agent identifying the caller.
UA = "9fin credit desk calendar-sync research@9fin.com"

# Words to strip from a company name before matching. Order matters: longer
# first so "S.p.A." is stripped before "SA".
_STRIP_TOKENS = [
    r"\bincorporated\b", r"\bcorporation\b", r"\bcorp\.?\b",
    r"\bholdings?\b", r"\bplc\b", r"\bltd\.?\b", r"\blimited\b",
    r"\bs\.p\.a\.?\b", r"\bs\.?a\.?\b", r"\bn\.?v\.?\b", r"\bab\.?\b",
    r"\bg\.?m\.?b\.?\b", r"\bag\.?\b", r"\binc\.?\b",
    r"\bgroup\b", r"\bcompany\b", r"\bco\.?\b",
    r"\bthe\b",
    # Nordic / continental legal-entity abbreviations
    r"\ba/?s\b", r"\baktieselskab\b", r"\basa\b", r"\boyj\b",
    r"\bspa\b", r"\baps\b", r"\bab publ\b", r"\bpubl\b",
]


def _normalise(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\s*\([^)]*\)\s*", " ", n)   # drop parenthetical qualifiers
    n = n.replace("&", "and")
    for tok in _STRIP_TOKENS:
        n = re.sub(tok, " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # Trailing-single-letter residue (Nordic "A S", Dutch "N V", French "S A"):
    # after stripping legal-entity words, forms like "novo nordisk a s" become
    # "novo nordisk" by removing dangling single letters at the end.
    for _ in range(3):
        stripped = re.sub(r"\s+[a-z]\s*$", "", n).strip()
        if stripped == n:
            break
        n = stripped
    return n


@dataclass
class Classified:
    company: str
    monday_id: str
    monday_url: str
    source_type: str = "scrape_fallback"   # sec_edgar | scrape_fallback (later: lse_rns, rss, ...)
    source_id: str = ""                     # CIK / RSS URL / …
    confidence: float = 0.3
    match_title: str = ""
    match_ticker: str = ""
    notes: str = ""


def _fetch_sec_tickers(cache_path: Path) -> Dict[str, dict]:
    """Return {'normalised title': {cik_str, title, ticker}}. Cached ~7 days."""
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 7 * 24 * 3600:
            data = json.loads(cache_path.read_text())
        else:
            data = None
    else:
        data = None
    if data is None:
        log.info("Downloading SEC tickers file…")
        r = requests.get(SEC_TICKERS_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        data = r.json()
        cache_path.write_text(json.dumps(data))
    # tickers.json is a dict of {"0": {"cik_str": ..., "ticker": ..., "title": ...}, ...}
    by_norm: Dict[str, dict] = {}
    for entry in data.values():
        title = entry.get("title") or ""
        norm = _normalise(title)
        if not norm:
            continue
        by_norm.setdefault(norm, entry)  # first wins on exact-normalised collisions
    return by_norm


def _match_sec(company_norm: str, by_norm: Dict[str, dict]) -> Optional[dict]:
    """Return the SEC tickers entry that best matches `company_norm`.

    Precision-first: we accept ONLY exact matches on the normalised name.
    The normaliser strips legal-entity suffixes and dangling single-letter
    residue (A/S, N.V., S.A.) so "Novo Nordisk A/S" and "Novo Nordisk"
    normalise identically.

    Substring/prefix matching was tried and reliably produced false positives
    on short single-word inputs (K+S→MITEK, Next→NextTech, Sky→SkyHarbour) —
    the exact-only rule is the cost of trusting the SEC branch.
    """
    if not company_norm:
        return None
    return by_norm.get(company_norm)


def _sec_has_recent_8k(cik: str) -> bool:
    """Quick check that this CIK actually files 8-Ks — filters out ETFs, funds,
    or entities that share a name with our target but don't report earnings."""
    cik10 = str(cik).zfill(10)
    try:
        r = requests.get(SEC_SUBMISSIONS_URL.format(cik=cik10),
                         headers={"User-Agent": UA}, timeout=20)
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    try:
        obj = r.json()
    except ValueError:
        return False
    forms = obj.get("filings", {}).get("recent", {}).get("form", [])
    for f in forms:
        if f in ("8-K", "6-K"):  # 6-K = foreign private issuers
            return True
    return False


def classify_one(company: str, monday_id: str, url: str, sec_by_norm: Dict[str, dict]) -> Classified:
    r = Classified(company=company, monday_id=monday_id, monday_url=url)
    norm = _normalise(company)
    sec_match = _match_sec(norm, sec_by_norm)
    if sec_match:
        cik = str(sec_match["cik_str"]).zfill(10)
        title = sec_match.get("title", "")
        ticker = sec_match.get("ticker", "")
        # Verify by fetching submissions and confirming an earnings-adjacent form
        if _sec_has_recent_8k(cik):
            r.source_type = "sec_edgar"
            r.source_id = cik
            r.confidence = 0.95 if _normalise(title) == norm else 0.80
            r.match_title = title
            r.match_ticker = ticker
            r.notes = f"CIK {cik} · title={title!r} ticker={ticker}"
            return r
        else:
            r.notes = f"SEC match found ({title!r} CIK {cik}) but no 8-K/6-K on file — likely different entity."

    # No SEC match. Fall through to scraper.
    r.source_type = "scrape_fallback"
    r.source_id = url
    r.confidence = 0.3
    if not r.notes:
        r.notes = "No SEC match by normalised name."
    return r


def main() -> int:
    configure_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--associate", type=str, default="Peter Megarity")
    p.add_argument("--core", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    cfg = Config.load()
    rows = fetch_nmd_rows(cfg.monday_api_token, cfg.nmd_board_id)
    if args.core:
        rows = [r for r in rows if r.core.lower().startswith("core")]
        slug = "core"
    else:
        needle = args.associate.lower()
        rows = [r for r in rows if needle in r.associate.lower()]
        slug = args.associate.lower().replace(" ", "_")
    log.info("Classifying %d companies for %s", len(rows),
             "Core universe" if args.core else args.associate)

    cache = cfg.state_dir / "sec_tickers.json"
    by_norm = _fetch_sec_tickers(cache)
    log.info("Loaded SEC tickers: %d entries", len(by_norm))

    results: List[Classified] = []
    for i, r in enumerate(rows, 1):
        c = classify_one(r.street_name, r.monday_item_id, r.ir_url, by_norm)
        results.append(c)
        # Small delay to be polite to SEC
        if c.source_type == "sec_edgar":
            time.sleep(0.1)
        if i % 20 == 0 or i == len(rows):
            log.info("Progress %d/%d — last: %s → %s", i, len(rows),
                     r.street_name[:32], c.source_type)

    out_path = args.out or (cfg.state_dir / f"sources_{slug}.json")
    with out_path.open("w") as f:
        json.dump([c.__dict__ for c in results], f, indent=2)
    log.info("Wrote %s", out_path)

    # Summary
    from collections import Counter
    by_type = Counter(c.source_type for c in results)
    log.info("=== Classification summary ===")
    for t, n in by_type.most_common():
        log.info("  %s : %d", t, n)
    log.info("SEC-classified companies:")
    for c in results:
        if c.source_type == "sec_edgar":
            log.info("  %s → CIK %s (%s, ticker %s, conf %.2f)",
                     c.company, c.source_id, c.match_title, c.match_ticker, c.confidence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
