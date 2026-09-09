"""Source dispatcher.

Reads the URL audit CSV (dashboards/url_audit.csv) and, for each company,
assigns exactly ONE source of truth for calendar events:

  - sec:               SEC EDGAR 8-K / 6-K poller (source_id = CIK)
  - rss:               RSS/Atom feed poll (source_id = feed URL)
  - ical:              iCal parse (source_id = ics URL)
  - jsonld:            JSON-LD Event parse from the IR page (source_id = ir_url)
  - ir_calendar_page:  Playwright scrape of the IR page (source_id = ir_url)
  - needs_fix:         wrong URL, needs manual replacement (source_id = "")
  - orphan:            no automated source known (source_id = "")

The registry is derived deterministically from the audit's suggested_action
field, so re-running scripts/audit_urls.py refreshes assignments.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

# audit suggested_action → dispatcher source_type
_ACTION_MAP = {
    "use-sec":              "sec",
    "use-ical":             "ical",
    "use-jsonld":           "jsonld",
    "use-rss":              "rss",
    "keep-current":         "ir_calendar_page",
    "find-calendar-subpage":"needs_fix",
    "replace-url":          "needs_fix",
    "js-render-required":   "needs_fix",
    "handle-cookies":       "ir_calendar_page",  # scraper handles cookie banners
    "manual-review":        "orphan",
}


def load_source_registry(audit_csv: Path) -> Dict[str, dict]:
    """Return {company_id: {source_type, source_id, current_url, verdict}}.

    Missing audit rows → default to ir_calendar_page (unknown, worth trying).
    """
    if not audit_csv.exists():
        log.warning("No audit CSV at %s — every company defaults to ir_calendar_page", audit_csv)
        return {}
    out: Dict[str, dict] = {}
    with audit_csv.open() as f:
        for row in csv.DictReader(f):
            action = (row.get("suggested_action") or "").strip()
            source_type = _ACTION_MAP.get(action, "ir_calendar_page")
            source_id = ""
            if source_type == "sec":
                source_id = row.get("sec_cik", "").strip()
            elif source_type == "ir_calendar_page":
                source_id = row.get("current_url", "").strip()
            # rss/ical/jsonld source_ids need a separate feed-discovery output;
            # for now those routes are marked but their fetchers are stubbed.
            out[str(row["id"])] = {
                "source_type": source_type,
                "source_id": source_id,
                "current_url": row.get("current_url", "").strip(),
                "final_url": row.get("final_url", "").strip(),
                "verdict": row.get("verdict", ""),
                "audit_action": action,
            }
    return out


def assign(registry: Dict[str, dict], company_id: str, fallback_url: str) -> dict:
    """Look up a company; fall back to ir_calendar_page if unknown."""
    entry = registry.get(str(company_id))
    if entry:
        return entry
    return {
        "source_type": "ir_calendar_page",
        "source_id": fallback_url,
        "current_url": fallback_url,
        "final_url": "",
        "verdict": "unaudited",
        "audit_action": "",
    }
