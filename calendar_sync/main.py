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
from .sources.dispatcher import assign, load_source_registry
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
    from collections import Counter

    # --- 4a. Source assignment (one source per company) ---------------------
    registry = load_source_registry(cfg.repo_root / "dashboards" / "url_audit.csv")
    for row in joined:
        row["_source"] = assign(registry, row["id"], row["ir_url"])
    source_hist = Counter(r["_source"]["source_type"] for r in joined)
    log.info("Source assignment: %s", dict(source_hist))
    log.info("Automated coverage: %d / %d (orphan + needs_fix = %d)",
             sum(v for k, v in source_hist.items() if k not in ("orphan", "needs_fix")),
             len(joined),
             source_hist.get("orphan", 0) + source_hist.get("needs_fix", 0))

    # --- 4b. IR scrape — only for companies assigned ir_calendar_page --------
    if args.skip_ir_scrape:
        ir_results = {}
    else:
        ir_targets = [r for r in joined if r["_source"]["source_type"] == "ir_calendar_page"]
        log.info("IR scrape: %d companies (skipping %d assigned to structured sources / orphan / needs_fix)",
                 len(ir_targets), len(joined) - len(ir_targets))
        async_input = [
            {"company_id": r["id"], "street_name": r["name"],
             "ir_url": r["_source"]["source_id"] or r["ir_url"]}
            for r in ir_targets
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

    # --- 4c. SEC EDGAR — only for companies assigned sec ---------------------
    sec_events_by_company: dict = {}
    sec_targets = [r for r in joined if r["_source"]["source_type"] == "sec"
                   and r["_source"]["source_id"]]
    if sec_targets:
        try:
            ciks_by_company = {str(r["id"]): r["_source"]["source_id"] for r in sec_targets}
            log.info("SEC: polling %d companies", len(ciks_by_company))
            horizon = today + dt.timedelta(days=cfg.lookahead_days)
            sec_events_by_company = asyncio.run(fetch_sec_events(
                ciks_by_company, today, horizon, lookback_days=90, concurrency=8,
            ))
            n_with_events = sum(1 for v in sec_events_by_company.values() if v)
            log.info("SEC: extracted events for %d/%d matched companies",
                     n_with_events, len(ciks_by_company))
        except Exception as e:
            log.warning("SEC layer failed (continuing without): %s", e)

    # --- 5. Diff per company (dispatched from _source) ----------------------
    stype_hits = Counter()
    for row in joined:
        stype = row["_source"]["source_type"]
        ninefin = _events_for_company(row["name"], ninefin_events)

        # Collect events from the ONE assigned source for this company.
        events: list = []
        ir_broken = False
        if stype == "sec":
            events = list(sec_events_by_company.get(str(row["id"]), []))
        elif stype == "ir_calendar_page":
            ir = ir_results.get(row["id"])
            if ir is None or ir.error:
                ir_broken = True
            else:
                events = list(ir.events)
        # rss / ical / jsonld / needs_fix / orphan → no fetcher yet → events = []

        row["source_type"] = stype
        row["source_events"] = len(events)
        if events:
            stype_hits[stype] += 1

        gaps = find_gaps(events, ninefin)
        if gaps:
            row["status"] = "to-check"
        elif events:
            row["status"] = "cleared"
        elif stype == "ir_calendar_page" and ir_broken:
            row["status"] = "urlbroken"
        elif stype in ("needs_fix", "orphan"):
            row["status"] = stype  # dashboard treats these as their own bucket
        else:
            row["status"] = "no-dates"

        row["to_file"] = [e["date"].isoformat() if hasattr(e["date"], "isoformat") else e["date"] for e in gaps]
        row["on_9fin"] = [{"d": e["date"].isoformat() if hasattr(e["date"], "isoformat") else e["date"], "t": e["title"]} for e in ninefin]

    log.info("Signal per source: %s", dict(stype_hits))

    status_hist = Counter(r["status"] for r in joined)
    log.info("Status breakdown: %s", dict(status_hist))
    gap_rows = [r for r in joined if r["status"] == "to-check"]
    fail_rows = [r for r in joined if r["status"] in ("urlbroken", "needs_fix", "orphan")]

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
