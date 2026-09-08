"""Daily calendar-sync entry point.

Pulls core companies (Omni) + their IR URLs (Monday) + 9fin's forward
calendar (via Playwright session) + each IR page's dates (Playwright),
computes the diff per company, writes dashboards/gaps.json for the
artifact, and Slacks the owner a summary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from .config import Config, configure_logging
from .logic.diff import find_gaps
from .logic.normalise import normalise
from .notify.slack import send_abort, send_report
from .render.gaps_json import write_gaps_json
from .sources.ir_scraper import scrape_many  # noqa: F401 (kept for backwards compat)
from .sources.ir_scraper_async import scrape_many_async
from .sources.monday_client import fetch_nmd_rows
from .sources.ninefin_client import NineFinSessionError, fetch_calendar, load_context
from .sources.omni_client import fetch_core_companies
from .sources.sec_client import fetch_sec_events, load_sec_ticker_index, match_ciks

log = logging.getLogger("calendar_sync")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="Only process the first N companies (post-join).")
    p.add_argument("--only-ids", type=str, default=None, help="Comma-separated company IDs to restrict to.")
    p.add_argument("--dry-run", action="store_true", help="Skip Slack + skip commit-triggering file writes.")
    p.add_argument("--skip-ir-scrape", action="store_true", help="Skip IR page scraping (for testing).")
    return p.parse_args()


def _join_core_and_nmd(core, nmd_rows):
    """Inner-join Omni core companies with the NMD board rows.

    NMD rows don't reliably carry a company_id, so we join by street_name
    (case-insensitive, whitespace-trimmed). Only NMD rows flagged as core
    on Monday AND matching an Omni core company survive.
    """
    core_nmd = {}
    for row in nmd_rows:
        if not row.core:
            continue  # NMD's own core flag must be set
        if not row.ir_url:
            continue
        key = (row.street_name or "").strip().lower()
        if key:
            core_nmd[key] = row

    joined = []
    seen = set()
    for c in core:
        key = (c.street_name or "").strip().lower()
        if key in seen:
            continue
        row = core_nmd.get(key)
        if not row:
            continue
        seen.add(key)
        joined.append({
            "id": str(c.company_id),
            "name": row.street_name or c.street_name,
            "ir_url": row.ir_url,
            "analyst": row.associate or "",
            "region": row.office or "",
            "country": "",
            "hosts_calls": row.hosts_calls or "",
            "monday_item_id": row.monday_item_id or "",
            "monday_core": c.monday_core,
        })
    return joined


def _events_for_company(street_name: str, ninefin_events):
    key = (street_name or "").strip().lower()
    return [
        {"date": e.start_date, "title": e.title}
        for e in ninefin_events
        if (e.company or "").strip().lower() == key
    ]


def run(cfg: Config, args: argparse.Namespace) -> int:
    log.info("Starting daily run (dry_run=%s, limit=%s)", args.dry_run, args.limit)
    today = dt.date.today()

    # --- 1. Omni ------------------------------------------------------------
    try:
        core = fetch_core_companies(cfg.omni_api_key, cfg.omni_base_url, cfg.omni_model_id)
    except Exception as e:
        log.exception("Omni fetch failed")
        if not args.dry_run:
            send_abort(cfg, f"Omni fetch failed: {e}")
        return 2
    log.info("Omni returned %d core companies", len(core))

    # --- 2. Monday NMD board ------------------------------------------------
    try:
        nmd_rows = fetch_nmd_rows(cfg.monday_api_token, cfg.nmd_board_id)
    except Exception as e:
        log.exception("Monday fetch failed")
        if not args.dry_run:
            send_abort(cfg, f"Monday fetch failed: {e}")
        return 3
    log.info("Monday returned %d NMD rows with an IR URL", len(nmd_rows))

    # --- 3. Join ------------------------------------------------------------
    joined = _join_core_and_nmd(core, nmd_rows)
    log.info("Joined: %d core companies with IR URL", len(joined))

    if args.only_ids:
        wanted = {s.strip() for s in args.only_ids.split(",") if s.strip()}
        joined = [c for c in joined if c["id"] in wanted]
        log.info("Filtered to --only-ids: %d companies", len(joined))
    if args.limit:
        joined = joined[: args.limit]
        log.info("Trimmed to --limit=%d", args.limit)

    if not joined:
        log.warning("Nothing to do — joined list is empty. Exiting.")
        if not args.dry_run:
            send_abort(cfg, "Nothing to do — no core companies with IR URL.")
        return 0

    # --- 4. Playwright: 9fin calendar + IR pages ---------------------------
    diffs = []
    with sync_playwright() as p:
        try:
            context = load_context(p, cfg.state_dir / "9fin_state.json", headless=True)
        except Exception as e:
            log.exception("Playwright / 9fin session load failed")
            if not args.dry_run:
                send_abort(cfg, f"9fin session load failed: {e}")
            return 4

        try:
            ninefin_events = fetch_calendar(context, cfg.ninefin_base_url, cfg.lookahead_days)
        except NineFinSessionError as e:
            log.exception("9fin session invalid")
            if not args.dry_run:
                send_abort(cfg, f"9fin session invalid — please refresh cookies: {e}")
            context.close()
            return 5
        except Exception as e:
            log.exception("9fin calendar download failed")
            if not args.dry_run:
                send_abort(cfg, f"9fin calendar download failed: {e}")
            context.close()
            return 6
        log.info("9fin returned %d events across %d companies", len(ninefin_events), len({e.company for e in ninefin_events}))

        context.close()

    import asyncio

    if args.skip_ir_scrape:
        ir_results = {}
    else:
        # Adapt joined-row keys (id/name) to the async scraper's expected keys.
        async_input = [
            {"company_id": r["id"], "street_name": r["name"], "ir_url": r["ir_url"]}
            for r in joined
        ]
        async_out = asyncio.run(scrape_many_async(
            async_input,
            lookahead_days=cfg.lookahead_days,
            timeout_s=cfg.ir_scrape_timeout_s,
            concurrency=max(cfg.ir_scrape_concurrency, 8),
            use_llm=False,
            progress_every=25,
        ))
        ir_results = {r.company_id: r for r in async_out}

    # --- 4b. SEC EDGAR (secondary source) -----------------------------------
    sec_events_by_company: dict = {}
    try:
        sec_index = load_sec_ticker_index(cfg.state_dir / "sec_tickers.json")
        ciks_by_company = match_ciks(joined, sec_index)
        log.info("SEC: matched %d/%d companies to CIKs", len(ciks_by_company), len(joined))
        if ciks_by_company:
            horizon = today + dt.timedelta(days=cfg.lookahead_days)
            sec_events_by_company = asyncio.run(fetch_sec_events(
                ciks_by_company, today, horizon, lookback_days=90, concurrency=8,
            ))
            n_with_events = sum(1 for v in sec_events_by_company.values() if v)
            log.info("SEC: extracted events for %d/%d matched companies",
                     n_with_events, len(ciks_by_company))
    except Exception as e:
        log.warning("SEC layer failed (continuing without): %s", e)

    # --- 5. Diff per company ------------------------------------------------
    ir_only = sec_only = both_sources = 0
    for row in joined:
        ir = ir_results.get(row["id"])
        ninefin = _events_for_company(row["name"], ninefin_events)
        sec_events = sec_events_by_company.get(str(row["id"]), [])
        row["sec_events"] = len(sec_events)
        row["ir_events"] = 0 if (ir is None or ir.error) else len(ir.events)

        # Merge IR + SEC into a combined event list for gap detection.
        if ir is None or ir.error:
            combined = list(sec_events)
            ir_broken = True
        else:
            combined = list(ir.events) + list(sec_events)
            ir_broken = False

        gaps = find_gaps(combined, ninefin)
        if gaps:
            row["status"] = "to-check"
        elif not combined:
            row["status"] = "urlbroken" if ir_broken else "no-dates"
        else:
            row["status"] = "cleared"

        # Source attribution for measuring the SEC lift.
        if row["ir_events"] and row["sec_events"]:
            both_sources += 1
        elif row["sec_events"] and not row["ir_events"]:
            sec_only += 1
        elif row["ir_events"]:
            ir_only += 1

        row["to_file"] = [e["date"].isoformat() if hasattr(e["date"], "isoformat") else e["date"] for e in gaps]
        row["on_9fin"] = [{"d": e["date"].isoformat() if hasattr(e["date"], "isoformat") else e["date"], "t": e["title"]} for e in ninefin]

    gap_rows = [r for r in joined if r["status"] == "to-check"]
    fail_rows = [r for r in joined if r["status"] == "urlbroken"]
    nodate_rows = [r for r in joined if r["status"] == "no-dates"]
    cleared_rows = [r for r in joined if r["status"] == "cleared"]
    log.info(
        "Diff complete: %d gaps, %d URL failures, %d no-dates, %d cleared",
        len(gap_rows), len(fail_rows), len(nodate_rows), len(cleared_rows),
    )
    log.info(
        "Source coverage: %d IR-only, %d SEC-only, %d both (SEC lifted %d companies from silence)",
        ir_only, sec_only, both_sources, sec_only,
    )

    # --- 6. Write gaps.json for the artifact -------------------------------
    write_gaps_json(joined, cfg.repo_root, today)

    # --- 7. Slack (skipped on dry-run) -------------------------------------
    if args.dry_run:
        log.info("Dry-run: skipping Slack.")
    else:
        try:
            send_report(cfg, today, gap_rows, fail_rows, total=len(joined))
        except Exception as e:
            log.exception("Slack send failed")
            return 7

    log.info("Done.")
    return 0


def main() -> int:
    configure_logging()
    cfg = Config.load()
    args = _parse_args()
    try:
        return run(cfg, args)
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
