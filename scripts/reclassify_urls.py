"""Re-classify audit's `needs_fix` URLs using Playwright (JS-rendered).

The initial audit (scripts/audit_urls.py) uses plain aiohttp — JS-heavy or
bot-blocked IR pages returned fetch-errors or thin bodies and were flagged
as `replace-url` / `find-calendar-subpage` / `js-render-required` /
`handle-cookies` even though a real browser handles them fine.

This pass re-fetches each of those URLs with Chromium, re-classifies the
page, and rewrites dashboards/url_audit.csv in-place. Anything that now
classifies as `calendar-page` gets promoted back to `keep-current` — no
URL replacement needed.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import configure_logging

log = logging.getLogger("reclassify_urls")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

# Same classifiers as audit_urls, replicated here so we don't depend on
# aiohttp-specific plumbing.
CALENDAR_KW = re.compile(
    r"financial\s+calendar|investor\s+calendar|upcoming\s+events|"
    r"events\s+and\s+presentations|reporting\s+calendar|financial\s+diary|"
    r"next\s+events|q[1-4]\s+20\d\d(?:\s+results|\s+trading|\s+report)|"
    r"full[-\s]?year\s+results|half[-\s]?year\s+results|interim\s+results|"
    r"trading\s+update|preliminary\s+results|earnings\s+call|earnings\s+release",
    re.I,
)
IR_KW = re.compile(r"investor\s+relations|shareholder|inversores", re.I)
COOKIE_MARK = re.compile(
    r"cookie\s+consent|accept\s+all\s+cookies|cookie\s+policy|reject\s+all|"
    r"onetrust|cookiebot|didomi|trustarc",
    re.I,
)
PDF_RE = re.compile(r"\.pdf($|\?)", re.I)
STRIP_TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def classify_rendered(orig_url: str, final_url: str, status: int, body: str) -> tuple[str, str]:
    if not body:
        return "empty-body", f"status={status}"
    if PDF_RE.search(final_url):
        return "pdf-url", "points to PDF"
    orig_path = urlparse(orig_url).path.rstrip("/")
    final_path = urlparse(final_url).path.rstrip("/")
    if orig_path and orig_path not in ("", "/") and final_path in ("", "/"):
        return "redirected-to-home", "redirected to homepage"
    text = WS.sub(" ", STRIP_TAGS.sub(" ", body)).strip()
    text_lower = text.lower()
    if COOKIE_MARK.search(body) and len(text) < 1500:
        return "cookie-wall", f"cookie markers + short body ({len(text)} chars)"
    if CALENDAR_KW.search(text_lower):
        return "calendar-page", "calendar/results keywords present"
    if IR_KW.search(text_lower) or "/investor" in final_url.lower() or "/ir/" in final_url.lower():
        return "ir-page-no-calendar", "IR content but no calendar keywords"
    if len(text) < 500:
        return "thin-body", f"only {len(text)} chars of visible text"
    return "non-ir-page", "no IR/calendar signals"


def suggest_action(verdict: str, rss: int, ical: int, jsonld: int, sec: str) -> str:
    if sec:
        return "use-sec"
    if ical:
        return "use-ical"
    if jsonld:
        return "use-jsonld"
    if rss:
        return "use-rss"
    if verdict in ("http-404", "http-5xx", "http-4xx", "pdf-url", "empty-body",
                   "redirected-to-home", "non-ir-page", "fetch-error"):
        return "replace-url"
    if verdict == "cookie-wall":
        return "handle-cookies"
    if verdict == "ir-page-no-calendar":
        return "find-calendar-subpage"
    if verdict == "thin-body":
        return "js-render-required"
    if verdict == "calendar-page":
        return "keep-current"
    return "manual-review"


async def fetch_rendered(browser, url: str, timeout: int = 20) -> tuple[int, str, str]:
    context = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    try:
        resp = await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        # Give JS-heavy calendars a moment to hydrate.
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        status = resp.status if resp else 0
        final_url = page.url
        body = await page.content()
        return status, final_url, body
    except Exception as e:
        return 0, url, f"ERROR: {type(e).__name__}: {str(e)[:80]}"
    finally:
        await context.close()


async def main_async():
    configure_logging()

    audit_path = Path("dashboards/url_audit.csv")
    with audit_path.open() as f:
        rows = list(csv.DictReader(f))

    RECHECK_ACTIONS = {
        "replace-url", "find-calendar-subpage",
        "js-render-required", "handle-cookies",
    }
    to_recheck = [r for r in rows if r["suggested_action"] in RECHECK_ACTIONS]
    log.info("Re-classifying %d URLs with Playwright (chromium, JS on)", len(to_recheck))

    id_to_new: dict[str, dict] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        sem = asyncio.Semaphore(6)
        done = {"n": 0}
        total = len(to_recheck)

        async def _worker(row):
            async with sem:
                url = row["current_url"]
                status, final, body = await fetch_rendered(browser, url)
                if isinstance(body, str) and body.startswith("ERROR:"):
                    verdict, why = "fetch-error", body[7:]
                    body = ""
                else:
                    verdict, why = classify_rendered(url, final, status, body)
                rss    = int(row.get("rss_feeds", 0) or 0)
                ical   = int(row.get("ical_feeds", 0) or 0)
                jsonld = int(row.get("jsonld_events", 0) or 0)
                sec    = (row.get("sec_cik", "") or "").strip()
                new_action = suggest_action(verdict, rss, ical, jsonld, sec)
                id_to_new[row["id"]] = {
                    "verdict": verdict,
                    "why": why,
                    "status_code": str(status),
                    "final_url": final,
                    "suggested_action": new_action,
                }
                done["n"] += 1
                if done["n"] % 25 == 0 or done["n"] == total:
                    log.info("Progress %d/%d — %s → %s (%s)",
                             done["n"], total, row["name"][:32], verdict, new_action)

        await asyncio.gather(*[_worker(r) for r in to_recheck])
        await browser.close()

    # Rewrite audit CSV in place.
    for row in rows:
        if row["id"] in id_to_new:
            for k, v in id_to_new[row["id"]].items():
                row[k] = v
    with audit_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            w.writerow(r)

    before = Counter(r["suggested_action"] for r in to_recheck)  # from stale copies
    # After: look up in id_to_new
    after_actions = [id_to_new[r["id"]]["suggested_action"] for r in to_recheck]
    after = Counter(after_actions)
    log.info("Before (recheck subset): %s", dict(before))
    log.info("After  (recheck subset): %s", dict(after))
    rescued = after.get("keep-current", 0)
    log.info("Rescued: %d URLs moved to keep-current (no replacement needed)", rescued)


if __name__ == "__main__":
    asyncio.run(main_async())
