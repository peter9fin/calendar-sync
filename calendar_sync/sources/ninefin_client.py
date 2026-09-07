"""9fin client — headless Playwright session against app.9fin.com.

Two responsibilities:
  1. Load a saved session (from setup_9fin_session.py) into a headless browser.
  2. Hit POST /api/v1.0/calendar/download for a date range, parse XLSX rows.

The saved session is reused across all IR page scrapes too (same Playwright context).
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List

from openpyxl import load_workbook
from playwright.sync_api import BrowserContext, TimeoutError as PlaywrightTimeoutError, sync_playwright

log = logging.getLogger(__name__)


class NineFinSessionError(RuntimeError):
    """Raised when the saved 9fin session is invalid / expired."""


@dataclass(frozen=True)
class NineFinEvent:
    company: str
    start_date: date
    title: str
    event_type: str  # e.g. "Confirmed Event"
    link: str


def load_context(playwright, state_path: Path, headless: bool = True) -> BrowserContext:
    if not state_path.exists():
        raise NineFinSessionError(
            f"No saved 9fin session at {state_path}. Run: python -m calendar_sync.setup_9fin_session"
        )
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=str(state_path))
    return context


def fetch_calendar(
    context: BrowserContext,
    base_url: str,
    lookahead_days: int,
) -> List[NineFinEvent]:
    """Call the 9fin calendar download endpoint and parse the XLSX response."""
    page = context.new_page()
    log.info("Warming 9fin session by loading /calendar ...")
    # The calendar page polls continuously, so networkidle rarely fires. Fall back
    # to domcontentloaded and treat a networkidle timeout as informational.
    try:
        page.goto(f"{base_url}/calendar", wait_until="domcontentloaded", timeout=45_000)
    except PlaywrightTimeoutError as exc:
        raise NineFinSessionError(f"9fin /calendar didn't load: {exc}") from exc

    # Sanity check: are we still logged in?
    if "/login" in page.url or "/signin" in page.url:
        raise NineFinSessionError("9fin session expired — visible login page after redirect")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now + timedelta(days=lookahead_days)
    payload = {
        "start_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    log.info("Downloading 9fin calendar: %s → %s", payload["start_date"], payload["end_date"])

    # Use APIRequestContext so cookies from the storage_state are attached.
    api = context.request
    resp = api.post(
        f"{base_url}/api/v1.0/calendar/download",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
    )
    if resp.status != 200:
        body = (resp.text() or "")[:400]
        if "login" in body.lower() or resp.status in (401, 403):
            raise NineFinSessionError(
                f"9fin session invalid (status {resp.status}). Re-run setup_9fin_session."
            )
        raise RuntimeError(f"9fin calendar download failed: {resp.status} {body}")

    xlsx_bytes = resp.body()
    events = _parse_xlsx(xlsx_bytes)
    log.info(
        "9fin calendar: %d events, %d distinct companies",
        len(events),
        len({e.company for e in events}),
    )
    return events


def _parse_xlsx(data: bytes) -> List[NineFinEvent]:
    wb = load_workbook(io.BytesIO(data), read_only=True)
    ws = wb[wb.sheetnames[0]]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(h or "").strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(headers)}

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    out: List[NineFinEvent] = []
    for row in rows[1:]:
        if row is None or all(v is None or v == "" for v in row):
            continue
        raw_start = get(row, "Start Date")
        if isinstance(raw_start, datetime):
            start = raw_start.date()
        elif isinstance(raw_start, date):
            start = raw_start
        elif isinstance(raw_start, (int, float)):
            # Excel serial (days since 1899-12-30 in openpyxl parlance).
            start = (datetime(1899, 12, 30) + timedelta(days=int(raw_start))).date()
        else:
            continue

        company = str(get(row, "Company") or "").strip()
        title = str(get(row, "Title") or "").strip()
        if not company or not title:
            continue

        out.append(
            NineFinEvent(
                company=company,
                start_date=start,
                title=title,
                event_type=str(get(row, "Event Type") or "").strip(),
                link=str(get(row, "Link") or "").strip(),
            )
        )
    return out
