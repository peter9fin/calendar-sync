"""URL diagnostic — for each joined core company, classify what the current
IR URL actually is, whether a structured alternative exists, and what action
to take.

Emits: dashboards/url_audit.csv
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
from urllib.parse import urljoin, urlparse

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config, configure_logging
from calendar_sync.main import _join_core_and_nmd
from calendar_sync.sources.monday_client import fetch_nmd_rows
from calendar_sync.sources.omni_client import fetch_core_companies

from scripts.classify_sources import _fetch_sec_tickers, _match_sec, _normalise
from scripts.probe_feeds import _extract_head_feeds, _probe_common_paths

log = logging.getLogger("audit_urls")

UA = "Mozilla/5.0 9fin credit desk research@9fin.com"

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


async def _fetch_page(session, url, timeout=15):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": UA},
            allow_redirects=True,
            ssl=False,
        ) as resp:
            body_bytes = await resp.content.read(500_000)
            try:
                body = body_bytes.decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return {
                "status": resp.status,
                "final_url": str(resp.url),
                "ctype": resp.headers.get("content-type", "").lower(),
                "body": body,
            }
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:80]}


def _classify(orig_url: str, resp: dict) -> tuple[str, str]:
    if "error" in resp:
        return "fetch-error", resp["error"]
    status = resp["status"]
    final = resp["final_url"]
    ctype = resp.get("ctype", "")
    body = resp.get("body", "")

    if status == 404:
        return "http-404", "not found"
    if status >= 500:
        return "http-5xx", f"status {status}"
    if status >= 400:
        return "http-4xx", f"status {status}"
    if "pdf" in ctype or PDF_RE.search(final):
        return "pdf-url", "points to PDF"
    if not body:
        return "empty-body", f"status {status}"

    # Redirected to root?
    orig_path = urlparse(orig_url).path.rstrip("/")
    final_path = urlparse(final).path.rstrip("/")
    if orig_path and orig_path not in ("", "/") and final_path in ("", "/"):
        return "redirected-to-home", "redirected to homepage"

    # Visible text length (rough)
    text = WS.sub(" ", STRIP_TAGS.sub(" ", body)).strip()
    text_lower = text.lower()

    # Cookie wall: cookie markers + very short visible text
    if COOKIE_MARK.search(body) and len(text) < 1500:
        return "cookie-wall", f"cookie markers + short body ({len(text)} chars)"

    # Real calendar signals
    if CALENDAR_KW.search(text_lower):
        return "calendar-page", "calendar/results keywords present"

    # IR-shaped page but no calendar signals (probably wrong subpage)
    if IR_KW.search(text_lower) or "/investor" in final.lower() or "/ir/" in final.lower():
        return "ir-page-no-calendar", "IR content but no calendar keywords"

    # Very thin page (JS widget)
    if len(text) < 500:
        return "thin-body", f"only {len(text)} chars of visible text"

    return "non-ir-page", "no IR/calendar signals"


def _suggest_action(verdict: str, rss: int, ical: int, jsonld: int, sec: str) -> str:
    """Ordered: prefer the highest-quality structured source we can find."""
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


async def _audit_one(session, sem, row, sec_by_norm, i, total):
    async with sem:
        url = row["ir_url"]
        name = row["name"]
        resp = await _fetch_page(session, url)
        verdict, why = _classify(url, resp)

        rss_head: list = []
        ical_head: list = []
        jsonld_n = 0
        if "body" in resp and resp["body"]:
            rss_head, ical_head, jsonld_n = _extract_head_feeds(
                resp["body"], resp.get("final_url", url)
            )

        # Common-path probe only if head yielded nothing AND page is problematic
        if not rss_head and verdict not in ("calendar-page",):
            try:
                probe_url = resp.get("final_url", url) if "error" not in resp else url
                found_rss, found_ical = await _probe_common_paths(session, probe_url)
                rss_head = [{"url": u} for u in found_rss]
                ical_head = ical_head + found_ical
            except Exception:
                pass

        sec_hit = _match_sec(_normalise(name), sec_by_norm)
        sec_cik = str(sec_hit["cik_str"]).zfill(10) if sec_hit else ""

        action = _suggest_action(verdict, len(rss_head), len(ical_head), jsonld_n, sec_cik)

        if i % 50 == 0 or i == total:
            log.info("Progress %d/%d — %s → %s (%s)", i, total, name[:32], verdict, action)

        return {
            "id": row["id"],
            "name": name,
            "analyst": row.get("analyst", ""),
            "current_url": url,
            "final_url": resp.get("final_url", "") if "error" not in resp else "",
            "status_code": resp.get("status", 0) if "error" not in resp else 0,
            "verdict": verdict,
            "why": why,
            "rss_feeds": len(rss_head),
            "ical_feeds": len(ical_head),
            "jsonld_events": jsonld_n,
            "sec_cik": sec_cik,
            "suggested_action": action,
        }


async def main_async():
    configure_logging()
    cfg = Config.load()
    log.info("Fetching Omni core companies…")
    core = fetch_core_companies(cfg.omni_api_key, cfg.omni_base_url, cfg.omni_model_id)
    log.info("Fetching Monday NMD rows…")
    nmd = fetch_nmd_rows(cfg.monday_api_token, cfg.nmd_board_id)
    joined = _join_core_and_nmd(core, nmd)
    log.info("Joined: %d companies", len(joined))

    cache = cfg.state_dir / "sec_tickers.json"
    sec_by_norm = _fetch_sec_tickers(cache)
    log.info("SEC tickers loaded: %d entries", len(sec_by_norm))

    connector = aiohttp.TCPConnector(limit=25, ssl=False)
    sem = asyncio.Semaphore(20)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            _audit_one(session, sem, r, sec_by_norm, i, len(joined))
            for i, r in enumerate(joined, 1)
        ]
        results = await asyncio.gather(*tasks)

    out = cfg.repo_root / "dashboards" / "url_audit.csv"
    out.parent.mkdir(exist_ok=True)
    fields = [
        "id", "name", "analyst", "current_url", "final_url", "status_code",
        "verdict", "why", "rss_feeds", "ical_feeds", "jsonld_events",
        "sec_cik", "suggested_action",
    ]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(r)
    log.info("Wrote %s (%d rows)", out, len(results))

    log.info("Verdict distribution: %s", Counter(r["verdict"] for r in results).most_common())
    log.info("Action distribution:  %s", Counter(r["suggested_action"] for r in results).most_common())


if __name__ == "__main__":
    asyncio.run(main_async())
