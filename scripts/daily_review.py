"""Daily morning-review pipeline.

For a given associate (default Peter Megarity):
  1. Load their Monday NMD rows (URLs).
  2. Fetch each URL concurrently via async Playwright — body innerText only.
  3. Compare today's text against yesterday's snapshot per URL.
  4. Classify each source: HIGH / MEDIUM / LOW / UNCHANGED / NEW_BASELINE / ERROR.
  5. Cross-reference any *new* future date on a HIGH page against 9fin's own
     calendar: if a new date isn't on 9fin yet, flag it explicitly.
  6. Write markdown + HTML review report.

Usage:
    python scripts/daily_review.py                        # Peter Megarity
    python scripts/daily_review.py --associate "Ross Murray"
    python scripts/daily_review.py --core                 # entire Core universe
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from playwright.async_api import (
    Browser,
    BrowserContext,
    TimeoutError as PWTimeout,
    async_playwright,
)
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config, configure_logging
from calendar_sync.logic.change_detect import classify, extract_date_set, sha256, strip_cookie_prefix
from calendar_sync.sources.ir_scraper_async import COOKIE_INIT
from calendar_sync.sources.monday_client import fetch_nmd_rows
from calendar_sync.sources.ninefin_client import fetch_calendar, load_context

log = logging.getLogger("daily_review")


@dataclass
class Review:
    company: str
    url: str
    priority: str = "ERROR"       # NEW_BASELINE / HIGH / MEDIUM / LOW / UNCHANGED / ERROR
    text_len: int = 0
    new_dates: Set[str] = field(default_factory=set)          # new since yesterday
    all_page_dates: Set[str] = field(default_factory=set)     # every future date on page today
    dates_missing_from_9fin: Set[str] = field(default_factory=set)  # (new_dates diff)
    audit_gaps: Set[str] = field(default_factory=set)         # (all_page_dates − 9fin) — state audit
    ninefin_events: List[dict] = field(default_factory=list)
    error: Optional[str] = None
    content_hash: str = ""
    # Per-company traffic-light status: GREEN, YELLOW, RED, ERROR, BLANK
    status: str = "BLANK"
    marked_green_at: Optional[str] = None
    hash_at_green: Optional[str] = None
    dates_at_green: List[str] = field(default_factory=list)


def _url_slug(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


async def _fetch_text(ctx: BrowserContext, url: str, timeout_s: int) -> tuple[str, str]:
    """Return (text, error_or_empty)."""
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass
        try:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        text = await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 80000)"
        )
        return text or "", ""
    except Exception as exc:
        return "", f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _process_one(
    ctx: BrowserContext,
    company: str,
    url: str,
    snapshot_dir: Path,
    today: date,
    timeout_s: int,
) -> Review:
    r = Review(company=company, url=url)
    text, err = await _fetch_text(ctx, url, timeout_s)
    if err:
        r.error = err
        return r

    r.text_len = len(text)
    slug = _url_slug(url)
    snap_path = snapshot_dir / f"{slug}.txt"
    hash_path = snapshot_dir / f"{slug}.sha"

    yesterday_text = ""
    if snap_path.exists():
        yesterday_text = snap_path.read_text(errors="replace")

    text_for_extract = strip_cookie_prefix(text)
    today_dates = extract_date_set(text_for_extract, today)
    yesterday_dates = extract_date_set(strip_cookie_prefix(yesterday_text), today)

    priority, new_dates = classify(text, yesterday_text, today_dates, yesterday_dates)
    r.priority = priority
    r.new_dates = new_dates
    r.all_page_dates = today_dates
    r.content_hash = sha256(text)

    # Persist today's snapshot for tomorrow's comparison.
    snap_path.write_text(text)
    hash_path.write_text(sha256(text))
    return r


async def _fetch_all(
    companies: List[tuple[str, str]],
    snapshot_dir: Path,
    today: date,
    concurrency: int,
    timeout_s: int,
) -> List[Review]:
    if not companies:
        return []
    sem = asyncio.Semaphore(concurrency)
    completed = [0]
    total = len(companies)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        contexts = []
        for _ in range(concurrency):
            c = await browser.new_context()
            await c.add_init_script(COOKIE_INIT)
            contexts.append(c)

        async def _worker(i: int, company: str, url: str) -> Review:
            async with sem:
                ctx = contexts[i % len(contexts)]
                r = await _process_one(ctx, company, url, snapshot_dir, today, timeout_s)
                completed[0] += 1
                if completed[0] % 10 == 0 or completed[0] == total:
                    log.info("Progress %d/%d — last: %s [%s]",
                             completed[0], total, company[:30], r.priority)
                return r

        tasks = [
            asyncio.create_task(_worker(i, c, u))
            for i, (c, u) in enumerate(companies)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for c in contexts:
            try: await c.close()
            except Exception: pass
        await browser.close()

    out: List[Review] = []
    for i, r in enumerate(results):
        if isinstance(r, Review):
            out.append(r)
        else:
            company, url = companies[i]
            out.append(Review(company=company, url=url, priority="ERROR",
                              error=f"{type(r).__name__}: {str(r)[:120]}"))
    return out


# ─────────────────────────────── output ────────────────────────────────

MD_TEMPLATE_HEADER = """# Morning review — {date}

Filter: **{filter_label}**  ·  {n_scanned} companies scanned  ·  wall clock **{wall:.1f}s**

- HIGH — content changed AND has a new future date: **{n_high}**
- MEDIUM — content changed, some dates disappeared: **{n_medium}**
- LOW — content changed but no date-level change: **{n_low}**
- UNCHANGED: **{n_unchanged}**
- NEW BASELINE (first fetch): **{n_baseline}**
- ERRORS: **{n_error}**
"""

def _fmt_dates(dates: Set[str]) -> str:
    return ", ".join(sorted(dates)) if dates else "—"

def _fmt_9fin(events: List[dict]) -> str:
    if not events:
        return "_(no upcoming events on 9fin)_"
    return " · ".join(f"{e['date']} {e['title'][:40]}" for e in sorted(events, key=lambda x: x['date']))


def write_markdown(reviews: List[Review], filter_label: str, wall_s: float, out_path: Path) -> None:
    buckets: Dict[str, List[Review]] = {k: [] for k in ("HIGH","MEDIUM","LOW","UNCHANGED","NEW_BASELINE","ERROR")}
    for r in reviews:
        buckets.setdefault(r.priority, []).append(r)
    header = MD_TEMPLATE_HEADER.format(
        date=date.today().isoformat(),
        filter_label=filter_label,
        n_scanned=len(reviews),
        wall=wall_s,
        n_high=len(buckets["HIGH"]),
        n_medium=len(buckets["MEDIUM"]),
        n_low=len(buckets["LOW"]),
        n_unchanged=len(buckets["UNCHANGED"]),
        n_baseline=len(buckets["NEW_BASELINE"]),
        n_error=len(buckets["ERROR"]),
    )
    parts = [header]

    if buckets["HIGH"]:
        parts.append("\n## HIGH — check these first\n")
        for r in sorted(buckets["HIGH"], key=lambda x: x.company.lower()):
            parts.append(f"### {r.company}")
            parts.append(f"IR page: <{r.url}>")
            parts.append(f"New dates on page: **{_fmt_dates(r.new_dates)}**")
            not_on_9fin = r.new_dates - {e["date"] for e in r.ninefin_events}
            if r.dates_missing_from_9fin:
                parts.append(f"Dates NOT on 9fin: **{_fmt_dates(r.dates_missing_from_9fin)}**")
            parts.append(f"Currently on 9fin: {_fmt_9fin(r.ninefin_events)}\n")

    if buckets["MEDIUM"]:
        parts.append("\n## MEDIUM — dates removed (possible reschedule / cancellation)\n")
        for r in sorted(buckets["MEDIUM"], key=lambda x: x.company.lower()):
            parts.append(f"- **{r.company}** — <{r.url}>")

    if buckets["LOW"]:
        parts.append("\n## LOW — page changed, no date-level shift\n")
        for r in sorted(buckets["LOW"], key=lambda x: x.company.lower()):
            parts.append(f"- **{r.company}** — <{r.url}>")

    if buckets["NEW_BASELINE"]:
        parts.append(f"\n## New baseline captures ({len(buckets['NEW_BASELINE'])})\n")
        parts.append("_First fetch — snapshot saved, no comparison possible yet. These will be classified normally from tomorrow._\n")
        for r in sorted(buckets["NEW_BASELINE"], key=lambda x: x.company.lower())[:30]:
            parts.append(f"- **{r.company}** — <{r.url}> · dates on page: {_fmt_dates(r.new_dates)}")
        if len(buckets["NEW_BASELINE"]) > 30:
            parts.append(f"- _(+{len(buckets['NEW_BASELINE']) - 30} more)_")

    if buckets["ERROR"]:
        parts.append(f"\n## Fetch errors ({len(buckets['ERROR'])}) — worth checking URL on Monday\n")
        for r in sorted(buckets["ERROR"], key=lambda x: x.company.lower()):
            parts.append(f"- **{r.company}** — {r.error} — <{r.url}>")

    out_path.write_text("\n".join(parts))


def _sync_fetch_9fin(cfg: Config) -> Dict[str, List[dict]]:
    """Grab 9fin events, keyed by lowercased company name."""
    state_path = cfg.state_dir / "9fin_state.json"
    events_by = {}
    with sync_playwright() as p:
        ctx = load_context(p, state_path, headless=True)
        try:
            events = fetch_calendar(ctx, cfg.ninefin_base_url, cfg.lookahead_days)
        finally:
            ctx.close()
    for e in events:
        key = e.company.lower().strip()
        events_by.setdefault(key, []).append({"date": e.start_date.isoformat(), "title": e.title})
    return events_by


def main() -> int:
    configure_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--associate", type=str, default="Peter Megarity")
    p.add_argument("--core", action="store_true", help="Scan the whole Core universe.")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--timeout", type=int, default=15)
    args = p.parse_args()

    cfg = Config.load()

    log.info("Loading Monday NMD rows…")
    rows = fetch_nmd_rows(cfg.monday_api_token, cfg.nmd_board_id)
    if args.core:
        rows = [r for r in rows if r.core.lower().startswith("core")]
        filter_label = "Core universe"
    else:
        needle = args.associate.lower()
        rows = [r for r in rows if needle in r.associate.lower()]
        filter_label = f"Associate: {args.associate}"
    log.info("Filter %s → %d companies with IR URLs", filter_label, len(rows))

    log.info("Fetching 9fin calendar for cross-reference…")
    ninefin_by = _sync_fetch_9fin(cfg)

    snapshot_dir = cfg.state_dir / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)

    today = date.today()
    started = datetime.now()

    companies = [(r.street_name, r.ir_url) for r in rows]
    reviews = asyncio.run(_fetch_all(companies, snapshot_dir, today,
                                     concurrency=args.concurrency,
                                     timeout_s=args.timeout))

    # Enrich every review with 9fin context.
    for r in reviews:
        key = r.company.lower().strip()
        r.ninefin_events = ninefin_by.get(key, [])
        nf_dates = {e["date"] for e in r.ninefin_events}
        r.audit_gaps = r.all_page_dates - nf_dates
        if r.priority == "HIGH":
            r.dates_missing_from_9fin = r.new_dates - nf_dates

    # ─────── Per-company state (traffic light) ───────
    # Load prior state — an analyst may have "marked" companies GREEN yesterday.
    # We compute today's status by combining the fresh fetch with those marks.
    state_path = cfg.state_dir / "company_state.json"
    prior_state: Dict[str, dict] = {}
    if state_path.exists():
        try:
            prior_state = json.loads(state_path.read_text())
        except Exception:
            prior_state = {}

    for r in reviews:
        key = r.company.lower().strip()
        p = prior_state.get(key, {})
        r.marked_green_at = p.get("marked_green_at")
        r.hash_at_green = p.get("hash_at_green")
        r.dates_at_green = p.get("dates_at_green", [])

        if r.error:
            r.status = "ERROR"
        elif r.marked_green_at and r.hash_at_green:
            if r.content_hash == r.hash_at_green:
                r.status = "GREEN"
            else:
                r.status = "RED"
        elif r.all_page_dates and not r.audit_gaps:
            # Auto-GREEN: we parsed dates from the IR page AND every one of
            # them is already on 9fin. Positive evidence of alignment, no
            # analyst review needed. Snapshot the hash so future page changes
            # correctly flip this back to RED.
            r.status = "GREEN"
            r.marked_green_at = datetime.now().isoformat(timespec="seconds") + " (auto)"
            r.hash_at_green = r.content_hash
            r.dates_at_green = sorted(r.all_page_dates)
        else:
            # Never marked, and either no candidates parsed or candidates that
            # 9fin doesn't have → needs analyst review.
            r.status = "YELLOW"

    # Persist state — for each review, refresh its record. Analyst marks stay
    # untouched (they're re-read from prior_state above and only mutated by the
    # mark-checked CLI or the interactive dashboard).
    new_state = dict(prior_state)
    for r in reviews:
        key = r.company.lower().strip()
        entry = new_state.get(key, {})
        entry.update({
            "company": r.company,
            "url": r.url,
            "status": r.status,
            "content_hash": r.content_hash,
            "text_len": r.text_len,
            "all_page_dates": sorted(r.all_page_dates),
            "ninefin_events": r.ninefin_events,
            "audit_gaps": sorted(r.audit_gaps),
            "error": r.error,
            "last_scrape_at": datetime.now().isoformat(timespec="seconds"),
            # Preserve any prior analyst marks — do NOT auto-mark green.
            "marked_green_at": r.marked_green_at,
            "hash_at_green": r.hash_at_green,
            "dates_at_green": r.dates_at_green,
        })
        new_state[key] = entry
    state_path.write_text(json.dumps(new_state, indent=2))
    log.info("State written: %s (%d companies)", state_path, len(new_state))

    wall = (datetime.now() - started).total_seconds()
    log.info("Wall clock: %.1fs", wall)

    date_stamp = today.isoformat()
    out_dir = cfg.logs_dir
    md_path = out_dir / f"review_{date_stamp}.md"
    write_markdown(reviews, filter_label, wall, md_path)
    log.info("Wrote %s", md_path)

    # Also stamp a "latest" copy for easy re-publish
    (out_dir / "review_latest.md").write_text(md_path.read_text())

    # Dump structured JSON for the artifact renderer.
    json_path = out_dir / f"review_{date_stamp}.json"
    with json_path.open("w") as f:
        json.dump([{
            "company": r.company, "url": r.url, "priority": r.priority,
            "text_len": r.text_len,
            "new_dates": sorted(r.new_dates),
            "all_page_dates": sorted(r.all_page_dates),
            "dates_missing_from_9fin": sorted(r.dates_missing_from_9fin),
            "audit_gaps": sorted(r.audit_gaps),
            "ninefin_events": r.ninefin_events,
            "error": r.error,
        } for r in reviews], f, indent=2)
    log.info("Wrote %s", json_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
