"""Auto-discover a working calendar URL for every failed / thin-page company on
the last Core run, then propose Monday updates.

Strategy per company:
  1. Load its current URL (if valid). If the extractor recovers events, keep it.
  2. Otherwise, try candidate URLs derived from:
        a) common IR-calendar path patterns under the same domain
        b) links present on the current page whose text/href suggests a calendar
        c) the domain root's investor landing page
  3. First candidate whose extractor returns ≥1 real event wins.
  4. Write proposals to logs/url_fix_proposals.jsonl for review + batch-apply.

Runs async with the same worker pool as the main scrape.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, TimeoutError as PWTimeout, async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config
from calendar_sync.sources.ir_scraper import _extract_events, _extract_from_html_attrs
from calendar_sync.sources.ir_scraper_async import (
    COOKIE_INIT, DOM_WALK_JS, LINKS_JS,
    _grab_iframe_text, _grab_innertext, _grab_walk_text,
    scrape_one_async,
)
from calendar_sync.sources.monday_client import fetch_nmd_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("fix_urls")

# Common calendar URL path suffixes, ordered by prior success.
COMMON_PATHS = [
    "/investors/financial-calendar",
    "/investors/financial-calendar/",
    "/investors/calendar",
    "/investors/calendar/",
    "/investor-relations/financial-calendar",
    "/investor-relations/financial-calendar/",
    "/investor-relations/events",
    "/investor-relations/events/",
    "/investor-relations/calendar",
    "/investor-relations/calendar/",
    "/investor-relations/financial-agenda",
    "/investors/events-and-presentations",
    "/investors/events-and-presentations/",
    "/investors/events-and-presentations/default.aspx",
    "/investor-relations/events-and-presentations",
    "/en/investors/financial-calendar",
    "/en/investors/financial-calendar/",
    "/en/investor-relations/financial-calendar",
    "/en/investors/calendar",
    "/en/investors/events",
    "/en/investor-relations/events",
    "/investors/",
    "/investor-relations/",
    "/en/investors/",
    "/en/investor-relations/",
]

CAL_LINK_RX = re.compile(r"financial\s+calendar|investor\s+calendar|reporting\s+calendar|events\s+and\s+presentations|financial\s+events|upcoming\s+events|^calendar$|financial\s+dates|calendar\s+of\s+events", re.I)
CAL_HREF_RX = re.compile(r"(?:financial-|investor-|reporting-)?calendar|financial-events|events-presentations|financial-agenda|investor-events", re.I)


@dataclass
class Candidate:
    company: str
    item_id: str
    old_url: str
    new_url: Optional[str] = None
    events: List[dict] = field(default_factory=list)
    notes: str = ""


# Looser page-quality test — a URL wins if the page is a plausible calendar,
# not necessarily one my strict extractor can parse. Criteria:
#   • body innerText ≥ 800 chars (past the thin_page threshold)
#   • text contains a calendar-page heading ("financial calendar", "investor
#     calendar", "events and presentations", "reporting calendar", etc.) OR
#   • text contains at least one FUTURE 20YY date near an event keyword
_CAL_HEADING = re.compile(
    r"financial\s+calendar|investor\s+calendar|reporting\s+calendar|"
    r"events\s+and\s+presentations|financial\s+events|financial\s+dates|"
    r"calendar\s+of\s+events|company\s+calendar",
    re.I,
)
_EVENT_KEYWORD = re.compile(
    r"results|earnings|trading\s+update|interim|quarterly|"
    r"half[- ]year|full[- ]year|annual\s+report|AGM|q[1-4]\s+20",
    re.I,
)
_FUTURE_YEAR = re.compile(r"20(2[6-9]|[3-9]\d)")  # 2026-2099


async def try_url(context: BrowserContext, url: str, timeout_s: int) -> Tuple[str, List[dict], int]:
    """Return (text, events, quality_score) for a candidate URL. Empty on error.

    quality_score:
       0 = didn't load / thin / no calendar signal
       1 = plausible calendar page (has heading OR event keyword + future date)
       2 = our strict extractor recovers events from it (strongest)
    """
    page = None
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try: await page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout: pass
        await page.wait_for_timeout(1200)
        try:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
            await page.evaluate("() => window.scrollTo(0, 0)")
        except: pass
        text = await _grab_innertext(page)
        walk = await _grab_walk_text(page)
        iframe = await _grab_iframe_text(page)
        combined = "\n\n".join(t for t in (text, walk, iframe) if t)
        today = date.today()
        horizon = today + timedelta(days=90)
        evs = _extract_events(combined, today, horizon)
        try:
            html_content = await page.content()
            html_evs = _extract_from_html_attrs(html_content, today, horizon)
        except: html_evs = []
        keys = {(e["period"], e["year"], e["type"]) for e in evs}
        for e in html_evs:
            k = (e["period"], e["year"], e["type"])
            if k not in keys: evs.append(e); keys.add(k)

        if evs:
            return combined, evs, 2

        # Loose test — is this a plausible calendar page?
        if len(combined) >= 800:
            has_heading = bool(_CAL_HEADING.search(combined))
            has_event_kw = bool(_EVENT_KEYWORD.search(combined))
            has_future_year = bool(_FUTURE_YEAR.search(combined))
            if has_heading and (has_event_kw or has_future_year):
                return combined, [], 1
            if has_event_kw and has_future_year:
                return combined, [], 1
        return combined, [], 0
    except Exception:
        return "", [], 0
    finally:
        if page:
            try: await page.close()
            except: pass


async def find_link_candidates(context: BrowserContext, base_url: str, timeout_s: int) -> List[str]:
    """Look at the base URL page and rank calendar-suggesting links."""
    page = None
    out: List[Tuple[int, str]] = []
    try:
        page = await context.new_page()
        await page.goto(base_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try: await page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout: pass
        links = await page.evaluate(LINKS_JS)
        base_host = urlparse(base_url).netloc
        seen = set()
        for l in links:
            href = (l.get("href") or "").split("#")[0]
            text = l.get("text") or ""
            if not href or href.startswith("javascript:") or href in seen:
                continue
            if href.rstrip("/") == base_url.rstrip("/"):
                continue
            host = urlparse(href).netloc
            if host and host != base_host: continue
            seen.add(href)
            score = 0
            if CAL_LINK_RX.search(text): score += 3
            if CAL_HREF_RX.search(href): score += 2
            if score > 0: out.append((score, href))
        out.sort(key=lambda x: -x[0])
        return [h for _, h in out[:5]]
    except Exception:
        return []
    finally:
        if page:
            try: await page.close()
            except: pass


async def fix_one(context: BrowserContext, cand: Candidate, timeout_s: int) -> Candidate:
    """Find a working URL for this company, populating cand.new_url + events."""
    old = cand.old_url.strip()
    if not old.startswith("http"): old = "https://" + old
    tried = set()

    # Keep the best candidate seen so far (highest quality).
    best_url = None; best_events = []; best_score = 0; best_notes = ""

    def maybe_update(url, evs, score, notes):
        nonlocal best_url, best_events, best_score, best_notes
        if score > best_score:
            best_url = url; best_events = evs; best_score = score; best_notes = notes

    # 1) Common path patterns under the domain root.
    parsed = urlparse(old)
    root = f"{parsed.scheme}://{parsed.netloc}"
    for path in COMMON_PATHS:
        url = root + path
        if url in tried: continue
        tried.add(url)
        text, evs, score = await try_url(context, url, timeout_s)
        if score > 0:
            maybe_update(url, evs, score, f"pattern:{path}")
            if score == 2: break  # best possible, stop early

    # 2) Link-follow from the old URL (only if we don't already have a strong hit).
    if best_score < 2:
        try:
            link_cands = await find_link_candidates(context, old, timeout_s)
        except Exception:
            link_cands = []
        for url in link_cands:
            if url in tried: continue
            tried.add(url)
            text, evs, score = await try_url(context, url, timeout_s)
            if score > 0:
                maybe_update(url, evs, score, "link-follow")
                if score == 2: break

    # 3) Root landing + follow.
    if best_score < 2 and root not in tried:
        try:
            link_cands = await find_link_candidates(context, root, timeout_s)
        except Exception:
            link_cands = []
        for url in link_cands:
            if url in tried: continue
            tried.add(url)
            text, evs, score = await try_url(context, url, timeout_s)
            if score > 0:
                maybe_update(url, evs, score, "root-follow")
                if score == 2: break

    if best_url:
        cand.new_url = best_url
        cand.events = best_events
        cand.notes = f"{best_notes} (score={best_score})"
    else:
        cand.notes = f"no-candidate-worked (tried {len(tried)})"
    return cand


async def main():
    cfg = Config.load()

    # Parse failures + thin from last_run.md.
    last = open("/Users/petermegarity/Claude/repos/calendar-sync/logs/last_run.md").read()
    problems: List[Tuple[str, str]] = []  # (company, url)
    # Hard failures
    m = re.search(r"## Scrape failures.*?(?=^##|\Z)", last, flags=re.S|re.M)
    if m:
        for line in m.group(0).splitlines():
            f = re.match(r"^- \*\*(.+?)\*\* — .+? — <(.+?)>", line)
            if f: problems.append((f.group(1), f.group(2)))
    # Thin pages
    m = re.search(r"## Thin pages.*", last, flags=re.S)
    if m:
        for line in m.group(0).splitlines():
            f = re.match(r"^- \*\*(.+?)\*\* — <(.+?)>", line)
            if f: problems.append((f.group(1), f.group(2)))

    log.info("URLs to investigate: %d", len(problems))

    # Fetch Monday rows to get item IDs.
    rows = fetch_nmd_rows(cfg.monday_api_token, cfg.nmd_board_id)
    by_name = {r.street_name.lower().strip(): r.monday_item_id for r in rows}

    candidates: List[Candidate] = []
    for name, url in problems:
        item_id = by_name.get(name.lower().strip(), "")
        candidates.append(Candidate(company=name, item_id=item_id, old_url=url))

    log.info("Testing %d candidates with concurrency=6", len(candidates))

    sem = asyncio.Semaphore(6)
    completed = [0]

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        contexts: List[BrowserContext] = []
        for _ in range(6):
            ctx = await browser.new_context()
            await ctx.add_init_script(COOKIE_INIT)
            contexts.append(ctx)

        async def worker(i: int, cand: Candidate):
            async with sem:
                ctx = contexts[i % len(contexts)]
                r = await fix_one(ctx, cand, timeout_s=15)
                completed[0] += 1
                if completed[0] % 10 == 0 or r.new_url:
                    status = "✓" if r.new_url else "✗"
                    log.info("[%d/%d] %s %s → %s", completed[0], len(candidates), status,
                             r.company[:32], r.new_url or r.notes)
                return r

        tasks = [asyncio.create_task(worker(i, c)) for i, c in enumerate(candidates)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for ctx in contexts:
            try: await ctx.close()
            except: pass
        await browser.close()

    # Write proposals
    out_path = Path("/Users/petermegarity/Claude/repos/calendar-sync/logs/url_fix_proposals.jsonl")
    strong = plausible = 0
    with out_path.open("w") as f:
        for r in results:
            if not isinstance(r, Candidate): continue
            if r.new_url:
                if "score=2" in r.notes: strong += 1
                elif "score=1" in r.notes: plausible += 1
                rec = {"company": r.company, "item_id": r.item_id,
                       "old_url": r.old_url, "new_url": r.new_url,
                       "notes": r.notes, "events": r.events[:5]}
                f.write(json.dumps(rec) + "\n")

    log.info("=== Done ===")
    log.info("Tested %d URLs → %d strong (extractor recovers events), %d plausible (calendar page but 0 extracted)",
             len(candidates), strong, plausible)
    log.info("Proposals written to %s", out_path)


if __name__ == "__main__":
    asyncio.run(main())
