"""Async IR scraper — parallel worker-pool version with optional LLM rescue tier.

Design:
  - Uses playwright.async_api so many pages can be in-flight at once with a
    single browser process. Concurrency capped by an asyncio.Semaphore.
  - Reuses the pure-Python extractors from ir_scraper.py (regex-based text,
    HTML-attr, and structured-feed parsing).
  - Adds one optional extractor tier: when no events are found by any local
    tier, and ANTHROPIC_API_KEY is set in the environment, sends the rendered
    text to Claude Haiku 4.5 for structured extraction.

Entry point: `scrape_many_async(companies, ...)`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import replace
from datetime import date, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

from calendar_sync.sources.ir_scraper import (
    IRPageResult,
    _CAL_HREF_TOKEN_RX,
    _CAL_LINK_TEXT_RX,
    _detect_period,
    _detect_type,
    _extract_events,
    _extract_from_html_attrs,
    _infer_period_from_month,
)

log = logging.getLogger(__name__)

COOKIE_INIT = """
const rejectRx = /reject all|decline all|only necessary|reject cookies|deny|refuse/i;
const acceptRx = /accept all|allow all|i agree|got it|ok|continue|stay on current|proceed/i;
const clickIfMatch = (el, rx) => {
    const t = (el.innerText || el.textContent || "").trim();
    if (rx.test(t)) { try { el.click(); return true; } catch(e) {} }
    return false;
};
const sweep = () => {
    const cs = document.querySelectorAll(
        'button, a, [role="button"], .ot-pc-refuse-all-handler, #onetrust-reject-all-handler,' +
        ' #onetrust-accept-btn-handler, .cookie-consent__button, .cc-btn'
    );
    for (const el of cs) if (clickIfMatch(el, rejectRx)) return;
    for (const el of cs) if (clickIfMatch(el, acceptRx)) return;
};
new MutationObserver(sweep).observe(document.documentElement, {childList:true, subtree:true});
setTimeout(sweep, 500); setTimeout(sweep, 2000); setTimeout(sweep, 3500);
"""

DOM_WALK_JS = """
() => {
    function walk(node, out) {
        if (node.nodeType === 3) { out.push(node.textContent); return; }
        if (node.nodeType !== 1) return;
        const tag = node.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return;
        const blk = /^(DIV|P|LI|TR|SECTION|ARTICLE|H[1-6]|UL|OL|TABLE|TBODY|THEAD|TFOOT|BR)$/.test(tag);
        if (blk) out.push('\\n\\n');
        for (const c of node.childNodes) walk(c, out);
        if (blk) out.push('\\n');
    }
    const out = [];
    if (document.body) walk(document.body, out);
    return out.join('').replace(/[ \\t\\xa0]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n').slice(0, 80000);
}
"""

LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.href,
    text: (a.innerText || a.textContent || '').trim().slice(0, 120)
}))
"""

# ---------- Local extraction pipeline (mirrors sync scrape_one) ----------

async def _grab_innertext(page: Page) -> str:
    try:
        return await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 60000)")
    except Exception:
        return ""


async def _grab_walk_text(page: Page) -> str:
    try:
        return await page.evaluate(DOM_WALK_JS)
    except Exception:
        return ""


async def _grab_iframe_text(page: Page) -> str:
    parts: List[str] = []
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        try:
            t = await fr.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            t = ""
        if t:
            parts.append(t[:20000])
    return "\n\n".join(parts)


def _merge_events(base: list, extra: list) -> list:
    keys = {(e["period"], e["year"], e["type"]) for e in base}
    for ev in extra:
        k = (ev["period"], ev["year"], ev["type"])
        if k not in keys:
            base.append(ev)
            keys.add(k)
    base.sort(key=lambda h: h["date"])
    return base


async def _scrape_calendar_link(page: Page, ir_url: str, today: date, horizon: date, timeout_s: int) -> tuple[str, list]:
    """Look for a "financial calendar" link on the current page and try scraping it."""
    try:
        links = await page.evaluate(LINKS_JS)
    except Exception:
        return "", []
    base_host = urlparse(ir_url).netloc
    candidates: list = []
    seen = set()
    for l in links:
        href = (l.get("href") or "").split("#")[0]
        text = l.get("text") or ""
        if not href or href.startswith("javascript:"):
            continue
        if href.rstrip("/") == ir_url.rstrip("/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        host = urlparse(href).netloc
        if host and host != base_host:
            continue
        score = 0
        if _CAL_LINK_TEXT_RX.search(text):
            score += 3
        if _CAL_HREF_TOKEN_RX.search(href):
            score += 2
        if score > 0:
            candidates.append((score, href))
    candidates.sort(key=lambda x: -x[0])
    for _, link in candidates[:2]:
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PWTimeout:
                pass
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
            await page.evaluate("() => window.scrollTo(0, 0)")
            txt = await _grab_innertext(page)
            if txt and len(txt) >= 500:
                events = _extract_events(txt, today, horizon)
                if events:
                    return link, events
        except Exception:
            continue
    return "", []


# ---------- LLM rescue tier ----------

_LLM_PROMPT = """You are extracting future financial-calendar events from an issuer's IR page.

From the page text below, list every future earnings release, results announcement,
trading update, quarterly report, or conference call — anything a financial analyst
would put on a calendar. Ignore capital-markets days, investor days, AGMs, silent
periods, and past events.

Today is TODAY_ISO. Only include events dated TODAY_ISO or later, within 90 days.

Return STRICT JSON only, no prose:
{"events": [{"date": "YYYY-MM-DD", "period": "Q1|Q2|Q3|Q4|H1|H2|FY", "type": "Results|Earnings Call|Trading Update"}]}

PAGE TEXT:
---
TEXT_HERE
---"""


async def _llm_rescue(text: str, today: date, horizon: date) -> list:
    """If ANTHROPIC_API_KEY is set, ask Claude Haiku 4.5 to extract events."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not text:
        return []
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return []
    client = AsyncAnthropic(api_key=key)
    prompt = (
        _LLM_PROMPT
        .replace("TODAY_ISO", today.isoformat())
        .replace("TEXT_HERE", text[:18000])
    )
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning("LLM rescue failed: %s", e)
        return []
    raw = resp.content[0].text if resp.content else ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for e in obj.get("events", []):
        try:
            d = date.fromisoformat(e["date"])
        except Exception:
            continue
        if d < today or d > horizon:
            continue
        period = (e.get("period") or "").upper()
        if period not in {"Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY"}:
            period = _infer_period_from_month(d.month) or "FY"
        etype = e.get("type") or "Results"
        title = f"{period} {d.year} {etype}"
        out.append({
            "date": d.isoformat(),
            "title": title,
            "period": period,
            "year": str(d.year),
            "type": etype,
            "entry": "[llm]",
        })
    return out


# ---------- Per-company scrape ----------

async def scrape_one_async(
    context: BrowserContext,
    company_id: str,
    street_name: str,
    ir_url: str,
    lookahead_days: int,
    timeout_s: int,
    use_llm: bool,
) -> IRPageResult:
    page = await context.new_page()
    result = IRPageResult(company_id=company_id, street_name=street_name, ir_url=ir_url, text_len=0)
    today = date.today()
    horizon = today + timedelta(days=lookahead_days)
    try:
        try:
            await page.goto(ir_url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
        except PWTimeout as exc:
            result.error = f"scrape_error: TimeoutError: {str(exc)[:120]}"
            return result
        except Exception as exc:
            result.error = f"scrape_error: {type(exc).__name__}: {str(exc)[:120]}"
            return result

        try:
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except PWTimeout:
            pass

        # Scroll to trigger lazy-loaded widgets.
        try:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        # Tier 1: innerText
        text = await _grab_innertext(page)
        result.text_len = len(text or "")
        result.events = _extract_events(text or "", today, horizon)

        # Tier 2: walk + iframe (additive)
        walk_text = await _grab_walk_text(page)
        iframe_text = await _grab_iframe_text(page)
        combined = (walk_text or "") + ("\n\n" + iframe_text if iframe_text else "")
        result.text_len = max(result.text_len, len(combined))
        if len(combined) >= 500:
            _merge_events(result.events, _extract_events(combined, today, horizon))

        # Tier 3: link-follow (only if we still have nothing)
        if not result.events:
            new_url, events2 = await _scrape_calendar_link(page, ir_url, today, horizon, timeout_s)
            if events2:
                result.events = events2
                result.ir_url = new_url

        # Tier 4: HTML-attr (additive)
        try:
            html = await page.content()
        except Exception:
            html = ""
        html_events = _extract_from_html_attrs(html, today, horizon)
        if html_events:
            _merge_events(result.events, html_events)

        # Tier 5: LLM rescue (paid API, off by default in manual-rescue mode)
        if use_llm and not result.events:
            best_text = (walk_text or text or "")[:18000]
            if best_text:
                llm_events = await _llm_rescue(best_text, today, horizon)
                if llm_events:
                    _merge_events(result.events, llm_events)

        # Stash rendered text for manual rescue when local tiers found nothing.
        # This is consumed later by the "process rescue queue" flow — the user
        # asks Claude Code inside a session to extract dates from these pages,
        # avoiding a paid API call.
        if not result.events and (text or walk_text):
            result.rescue_text = (walk_text or text or "")[:18000]

        if result.text_len < 500 and not result.events:
            result.error = "thin_page"
        return result
    except Exception as exc:
        result.error = f"scrape_error: {type(exc).__name__}: {str(exc)[:120]}"
        return result
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ---------- Worker pool ----------

async def _worker(
    idx: int,
    total: int,
    company: dict,
    sem: asyncio.Semaphore,
    context_pool: List[BrowserContext],
    pool_idx: int,
    lookahead_days: int,
    timeout_s: int,
    use_llm: bool,
    on_done,
) -> IRPageResult:
    async with sem:
        ctx = context_pool[pool_idx % len(context_pool)]
        r = await scrape_one_async(
            ctx,
            company["company_id"],
            company["street_name"],
            company["ir_url"],
            lookahead_days,
            timeout_s,
            use_llm,
        )
        if on_done:
            on_done(idx, total, r)
        return r


async def scrape_many_async(
    companies: List[dict],
    lookahead_days: int,
    timeout_s: int = 20,
    concurrency: int = 8,
    use_llm: bool = True,
    progress_every: int = 10,
) -> List[IRPageResult]:
    """Scrape a batch of companies concurrently with a browser worker pool.

    Each worker uses one of `concurrency` browser contexts (round-robin). LLM
    rescue is enabled when `use_llm=True` and ANTHROPIC_API_KEY is set.
    """
    if not companies:
        return []

    total = len(companies)
    log.info("IR scrape (async): %d companies, concurrency=%d, llm=%s",
             total, concurrency, "yes" if (use_llm and os.environ.get("ANTHROPIC_API_KEY")) else "no")

    completed = [0]

    def on_done(idx, total, r):
        completed[0] += 1
        if completed[0] % progress_every == 0 or completed[0] == total or r.error:
            log.info(
                "IR scrape progress: %d/%d (%s: %s, events=%d, err=%s)",
                completed[0], total, r.company_id[:8] if r.company_id else "-",
                r.street_name[:30], len(r.events), r.error or "-",
            )

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        contexts: List[BrowserContext] = []
        for _ in range(concurrency):
            ctx = await browser.new_context()
            await ctx.add_init_script(COOKIE_INIT)
            contexts.append(ctx)

        sem = asyncio.Semaphore(concurrency)
        tasks = [
            asyncio.create_task(_worker(
                i, total, c, sem, contexts, i,
                lookahead_days, timeout_s, use_llm, on_done,
            ))
            for i, c in enumerate(companies)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for ctx in contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        await browser.close()

    # Filter exceptions into error results.
    out: List[IRPageResult] = []
    for i, r in enumerate(results):
        if isinstance(r, IRPageResult):
            out.append(r)
        else:
            c = companies[i]
            out.append(IRPageResult(
                company_id=c["company_id"],
                street_name=c["street_name"],
                ir_url=c["ir_url"],
                text_len=0,
                error=f"async_error: {type(r).__name__}: {str(r)[:120]}",
            ))
    return out
