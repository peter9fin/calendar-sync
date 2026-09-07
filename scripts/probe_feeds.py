"""Phase 4 discovery: for each company's IR page, discover what structured
feeds are available.

Looks for, in this order:
  1. <link rel="alternate" type="application/rss+xml"> / atom+xml — RSS
  2. <link rel="alternate" type="text/calendar"> — iCal
  3. <script type="application/ld+json"> containing Event schema
  4. Common RSS paths under the domain (/rss, /feed, /press-releases/rss …)
  5. Common iCal paths (/calendar.ics, /events.ics)

Output: state/feeds_{slug}.json — per-company summary of what's discoverable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config, configure_logging
from calendar_sync.sources.monday_client import fetch_nmd_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("probe_feeds")

UA = "Mozilla/5.0 9fin credit desk feed-discovery research@9fin.com"

# Common paths worth probing
RSS_PATHS = [
    "/rss", "/rss.xml", "/feed", "/feed.xml", "/atom.xml",
    "/news/rss", "/news/rss.xml", "/press-releases/rss", "/press-releases/rss.xml",
    "/investors/rss", "/investor-relations/rss",
    "/media/rss", "/newsroom/rss",
]
ICAL_PATHS = [
    "/calendar.ics", "/events.ics", "/ir.ics",
    "/investors/calendar.ics", "/investor-relations/calendar.ics",
]

# HTML head autodiscovery patterns
_RE_HEAD_LINKS = re.compile(
    r'<link[^>]*rel=["\']?alternate["\']?[^>]*>',
    re.I,
)
_RE_TYPE = re.compile(r'type=["\']?([^\s"\'>]+)', re.I)
_RE_HREF = re.compile(r'href=["\']?([^\s"\'>]+)', re.I)
_RE_TITLE = re.compile(r'title=["\']?([^"\'>]+)', re.I)
_RE_JSONLD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


@dataclass
class FeedProbe:
    company: str
    monday_id: str
    ir_url: str
    rss: List[dict] = field(default_factory=list)     # discovered RSS/Atom
    ical: List[str] = field(default_factory=list)     # iCal URLs
    jsonld_events: int = 0                             # count of Event schemas
    error: Optional[str] = None


async def _fetch(session: aiohttp.ClientSession, url: str, timeout: int = 12) -> Optional[str]:
    try:
        async with session.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            if resp.status == 200:
                # Read up to 200 KB — feeds and pages are usually well under this
                data = await resp.content.read(200_000)
                try:
                    return data.decode("utf-8", errors="replace")
                except Exception:
                    return None
    except Exception:
        return None
    return None


def _extract_head_feeds(html: str, base_url: str) -> tuple[list[dict], list[str], int]:
    rss: List[dict] = []
    ical: List[str] = []
    for m in _RE_HEAD_LINKS.finditer(html):
        tag = m.group(0)
        t = _RE_TYPE.search(tag)
        h = _RE_HREF.search(tag)
        if not (t and h):
            continue
        mime = t.group(1).lower()
        href = urljoin(base_url, h.group(1))
        ti = _RE_TITLE.search(tag)
        title = ti.group(1) if ti else ""
        if "rss" in mime or "atom" in mime:
            rss.append({"url": href, "type": mime, "title": title})
        elif "calendar" in mime or mime == "text/calendar":
            ical.append(href)
    # JSON-LD Event count
    jsonld_count = 0
    for m in _RE_JSONLD.finditer(html):
        body = m.group(1)
        if '"@type"' in body and ("Event" in body or "EventSeries" in body):
            jsonld_count += 1
    return rss, ical, jsonld_count


async def _probe_common_paths(session: aiohttp.ClientSession, base_url: str) -> tuple[list[str], list[str]]:
    """Try common RSS + iCal paths under the domain root. Return lists of URLs
    that respond 200 with the right content-type or plausible body."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    good_rss: List[str] = []
    good_ical: List[str] = []
    for path in RSS_PATHS:
        url = root + path
        body = await _fetch(session, url, timeout=8)
        if body and ("<rss" in body[:400].lower() or "<feed" in body[:400].lower() or '<?xml' in body[:100]):
            good_rss.append(url)
            break  # one good RSS per company is enough
    for path in ICAL_PATHS:
        url = root + path
        body = await _fetch(session, url, timeout=8)
        if body and body.strip().startswith("BEGIN:VCALENDAR"):
            good_ical.append(url)
            break
    return good_rss, good_ical


async def probe_one(session: aiohttp.ClientSession, company: str, monday_id: str, ir_url: str) -> FeedProbe:
    r = FeedProbe(company=company, monday_id=monday_id, ir_url=ir_url)
    html = await _fetch(session, ir_url)
    if not html:
        r.error = "fetch_failed"
        # Still try common paths — the IR URL may 404 but the root may serve feeds
        rss_paths, ical_paths = await _probe_common_paths(session, ir_url)
        r.rss.extend({"url": u, "type": "guess", "title": ""} for u in rss_paths)
        r.ical.extend(ical_paths)
        return r
    # Head autodiscovery
    head_rss, head_ical, jsonld_count = _extract_head_feeds(html, ir_url)
    r.rss.extend(head_rss)
    r.ical.extend(head_ical)
    r.jsonld_events = jsonld_count
    # Only probe common paths if head didn't find anything
    if not head_rss:
        found_rss, found_ical = await _probe_common_paths(session, ir_url)
        r.rss.extend({"url": u, "type": "guess", "title": ""} for u in found_rss)
        r.ical.extend(found_ical)
    return r


async def main_async(rows: list, out_path: Path, concurrency: int = 10):
    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(concurrency)
        results: List[FeedProbe] = []

        async def _worker(i, row):
            async with sem:
                r = await probe_one(session, row.street_name, row.monday_item_id, row.ir_url)
                if (i + 1) % 10 == 0 or i == 0:
                    log.info("Probed %d/%d — last: %s (rss=%d ical=%d jsonld=%d err=%s)",
                             i + 1, len(rows), row.street_name[:30],
                             len(r.rss), len(r.ical), r.jsonld_events, r.error or "-")
                return r

        tasks = [asyncio.create_task(_worker(i, r)) for i, r in enumerate(rows)]
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, FeedProbe):
                results.append(r)

    out_path.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    # Summary
    n_rss = sum(1 for r in results if r.rss)
    n_ical = sum(1 for r in results if r.ical)
    n_jsonld = sum(1 for r in results if r.jsonld_events)
    n_none = sum(1 for r in results if not r.rss and not r.ical and not r.jsonld_events)
    log.info("=== Feed-discovery summary ===")
    log.info("  Total probed:       %d", len(results))
    log.info("  Have RSS/Atom:      %d", n_rss)
    log.info("  Have iCal:          %d", n_ical)
    log.info("  Have JSON-LD Event: %d", n_jsonld)
    log.info("  No feed found:      %d", n_none)


def main() -> int:
    configure_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--associate", type=str, default="Peter Megarity")
    p.add_argument("--core", action="store_true")
    p.add_argument("--concurrency", type=int, default=10)
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
    log.info("Probing %d IR pages", len(rows))

    out_path = cfg.state_dir / f"feeds_{slug}.json"
    asyncio.run(main_async(rows, out_path, concurrency=args.concurrency))
    log.info("Wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
