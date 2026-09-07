"""A/B/C benchmark: given a list of (company, url, target_iso_date) triples,
run three scrape strategies and see which one recovers the target date.

Strategies:
  A) link-follow    — if the given URL yields no events, look for a link on the
                      page whose text/href contains calendar-related keywords
                      and try it instead.
  B) aggressive-js  — longer JS wait, wait for calendar selectors, click any
                      "Upcoming events" / "Show more" / year buttons.
  C) llm-extract    — pass the page's innerText to Claude and ask it to list
                      every future earnings/results/trading-update date.

Usage:
  python scripts/benchmark_strategies.py            # all misses
  python scripts/benchmark_strategies.py --strategies A,B     # subset
  python scripts/benchmark_strategies.py --company Arkema     # single case
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config
from calendar_sync.sources.ir_scraper import _extract_events, _RE_BOILERPLATE
from calendar_sync.sources.ninefin_client import load_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("benchmark")

# The 18 misses from the diagnostic (extractor got nothing; date not on page).
DEFAULT_MISSES: List[Tuple[str, str, str]] = [
    ("Arkema", "https://www.arkema.com/global/en/investor-relations/financials/financial-results/", "2026-11-05"),
    ("BW Energy", "https://www.bwenergy.no/investors/press-releases/?year=2025", "2026-10-29"),
    ("BlueNord", "https://www.bluenord.com/investors/reports-presentations/", "2026-10-28"),
    ("Branicks", "https://branicks.com/en/ir/financial-calendar/", "2026-11-05"),
    ("Carmila", "https://www.carmila.com/en/financial-calendar", "2026-10-22"),
    ("ConvaTec", "https://www.convatecgroup.com/investors/financial-calendar/", "2026-11-18"),
    ("Daimler Truck", "https://www.daimlertruck.com/en/investors/financial-calendar", "2026-11-06"),
    ("Fiskars", "https://fiskarsgroup.com/investors/ir-calendar/", "2026-10-22"),
    ("Forvia", "https://www.forvia.com/en/investors", "2026-11-02"),
    ("IAG", "https://www.iairgroup.com/investors-and-shareholders/financial-calendar/", "2026-11-06"),
    ("Loomis", "https://www.loomis.com/en/investors/calendar", "2026-10-30"),
    ("MTU Aero Engines", "https://www.mtu.de/investors/publications-events/financial-calendar/", "2026-10-29"),
    ("Metsa Board", "https://www.metsagroup.com/metsaboard/investors/ir-contacts/investor-calendar/", "2026-10-29"),
    ("Nestle", "https://www.nestle.com/investors/events", "2026-10-22"),
    ("Novo Nordisk", "https://www.novonordisk.com/investors/financial-results.html", "2026-11-04"),
    ("Sandvik", "https://www.home.sandvik/en/investors/calendar/", "2026-10-22"),
    ("Tokmanni", "https://tokmannigroup.com/en/investors/investor-calendar/", "2026-11-06"),
    ("ams Osram", "https://ams-osram.com/about-us/investor-relations/investor-calendar", "2026-11-13"),
]

COOKIE_INIT_SCRIPT = """
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
new MutationObserver(sweep).observe(document.documentElement, {childList: true, subtree: true});
setTimeout(sweep, 400); setTimeout(sweep, 1500); setTimeout(sweep, 3500);
"""


@dataclass
class StrategyResult:
    strategy: str
    company: str
    target: str
    url_used: str
    text_len: int
    events: List[dict] = field(default_factory=list)
    recovered: bool = False
    error: Optional[str] = None
    took_s: float = 0.0
    notes: str = ""


def _new_page(context: BrowserContext) -> Page:
    page = context.new_page()
    page.add_init_script(COOKIE_INIT_SCRIPT)
    return page


def _load_and_get_text(page: Page, url: str, timeout_s: int, wait_extra_ms: int = 0) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
    try:
        page.wait_for_load_state("networkidle", timeout=6_000)
    except PWTimeout:
        pass
    if wait_extra_ms:
        page.wait_for_timeout(wait_extra_ms)
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
    except Exception:
        pass
    return page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 60000)")


def _recovered(events: List[dict], target_iso: str, tol_days: int = 5) -> bool:
    t = date.fromisoformat(target_iso)
    return any(abs((date.fromisoformat(ev["date"]) - t).days) <= tol_days for ev in events)


# ---------- STRATEGY A: link-follow ----------
_CAL_LINK_TEXT = re.compile(
    r"(?:financial|investor|corporate)\s+calendar|"
    r"upcoming\s+events|upcoming\s+dates|"
    r"^events?$|^calendar$|events and presentations|"
    r"schedule|reporting\s+calendar",
    re.I,
)
_CAL_HREF_TOKEN = re.compile(r"calendar|events|schedule|upcoming", re.I)


def _find_calendar_links(page: Page, base_url: str) -> List[str]:
    """Extract candidate calendar-page links from the current page, ranked."""
    try:
        links = page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').trim().slice(0, 120)
            }))
        """)
    except Exception:
        return []
    base_host = urlparse(base_url).netloc
    candidates: List[Tuple[int, str]] = []
    seen = set()
    for l in links:
        href = l.get("href") or ""
        text = l.get("text") or ""
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        host = urlparse(href).netloc
        if host and host != base_host:
            continue  # stay on the same site
        if href in seen:
            continue
        seen.add(href)
        score = 0
        if _CAL_LINK_TEXT.search(text): score += 3
        if _CAL_HREF_TOKEN.search(href): score += 2
        if href == base_url or href.rstrip('/') == base_url.rstrip('/'):
            score = 0  # skip self-link
        if score > 0:
            candidates.append((score, href))
    candidates.sort(key=lambda x: -x[0])
    return [href for _, href in candidates[:5]]


def strategy_link_follow(context: BrowserContext, company: str, url: str, target: str) -> StrategyResult:
    t0 = time.time()
    r = StrategyResult(strategy="A-link-follow", company=company, target=target, url_used=url, text_len=0)
    page = _new_page(context)
    try:
        text = _load_and_get_text(page, url, timeout_s=20)
        r.text_len = len(text)
        events = _extract_events(text, date.today(), date.today() + timedelta(days=90))
        if events and _recovered(events, target):
            r.events = events
            r.recovered = True
            r.notes = "found on initial URL"
            return r
        # Try candidate calendar links.
        links = _find_calendar_links(page, url)
        r.notes = f"tried_links={len(links)}"
        for link in links:
            try:
                text2 = _load_and_get_text(page, link, timeout_s=15)
            except Exception as e:
                continue
            events2 = _extract_events(text2, date.today(), date.today() + timedelta(days=90))
            if events2 and _recovered(events2, target):
                r.url_used = link
                r.text_len = len(text2)
                r.events = events2
                r.recovered = True
                r.notes = f"found via link: {link}"
                return r
            # Even if not recovered, prefer richer follow-up
            if len(text2) > r.text_len and events2:
                r.url_used = link
                r.text_len = len(text2)
                r.events = events2
        return r
    except Exception as e:
        r.error = f"{type(e).__name__}: {str(e)[:120]}"
        return r
    finally:
        try: page.close()
        except: pass
        r.took_s = time.time() - t0


# ---------- STRATEGY B: aggressive JS ----------
_CAL_SELECTORS = [
    ".financial-calendar", ".calendar", "[data-calendar]", ".events-list",
    ".event-list", "table.calendar", ".ir-calendar", ".investor-calendar",
    ".financial-events", ".upcoming-events", ".financialcalendar",
]

_EXPAND_BUTTON_RX = re.compile(
    r"upcoming|show more|load more|view all|see all|expand|next events|"
    r"forthcoming|future events|2026|2027",
    re.I,
)


def strategy_aggressive_js(context: BrowserContext, company: str, url: str, target: str) -> StrategyResult:
    t0 = time.time()
    r = StrategyResult(strategy="B-aggressive-js", company=company, target=target, url_used=url, text_len=0)
    page = _new_page(context)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        try: page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout: pass
        # Wait for any of the common calendar selectors.
        selector_expr = ", ".join(_CAL_SELECTORS)
        try:
            page.wait_for_selector(selector_expr, timeout=6_000, state="attached")
        except PWTimeout:
            pass
        # Click any expand/upcoming/year buttons.
        clicked = page.evaluate("""
            (rxSource) => {
                const rx = new RegExp(rxSource, 'i');
                const els = document.querySelectorAll('button, a, [role="button"], .btn, .tab, li');
                let n = 0;
                for (const el of els) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.length < 60 && rx.test(t)) {
                        try { el.click(); n++; if (n > 5) break; } catch(e) {}
                    }
                }
                return n;
            }
        """, _EXPAND_BUTTON_RX.pattern)
        r.notes = f"clicked={clicked}"
        page.wait_for_timeout(2500)
        # Scroll a couple of times to trigger lazy-load.
        for _ in range(3):
            try:
                page.evaluate("() => window.scrollBy(0, window.innerHeight * 2)")
                page.wait_for_timeout(600)
            except Exception:
                pass
        page.evaluate("() => window.scrollTo(0, 0)")
        text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 60000)")
        r.text_len = len(text)
        events = _extract_events(text, date.today(), date.today() + timedelta(days=90))
        r.events = events
        r.recovered = _recovered(events, target)
        return r
    except Exception as e:
        r.error = f"{type(e).__name__}: {str(e)[:120]}"
        return r
    finally:
        try: page.close()
        except: pass
        r.took_s = time.time() - t0


# ---------- STRATEGY C: LLM extraction ----------
_LLM_PROMPT = """You are extracting upcoming financial events from an issuer's IR page.

From the page text below, list every future earnings release, results announcement,
trading update, quarterly report, or conference call — anything a financial analyst
would put on a calendar. Ignore capital-markets days, investor days, AGMs, silent
periods, and past events.

Return STRICT JSON only, no prose:
{"events": [{"date": "YYYY-MM-DD", "title": "...", "type": "results|call|trading update"}]}

Today's date is TODAY. Use the calendar year from context if the page omits it.

PAGE TEXT:
---
TEXT_HERE
---
"""


def strategy_llm(context: BrowserContext, company: str, url: str, target: str) -> StrategyResult:
    t0 = time.time()
    r = StrategyResult(strategy="C-llm-extract", company=company, target=target, url_used=url, text_len=0)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        r.error = "no ANTHROPIC_API_KEY in env — skipped"
        return r
    page = _new_page(context)
    try:
        text = _load_and_get_text(page, url, timeout_s=20)
        r.text_len = len(text)
        if not text:
            r.error = "empty page"
            return r
        # Trim to 20k chars — plenty for a calendar page.
        text = text[:20000]
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        prompt = _LLM_PROMPT.replace("TODAY", date.today().isoformat()).replace("TEXT_HERE", text)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text if resp.content else ""
        # Extract JSON from possible fenced code block.
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0)) if m else {"events": []}
        events = []
        for e in obj.get("events", []):
            try:
                d = date.fromisoformat(e["date"])
                if d < date.today() or d > date.today() + timedelta(days=90):
                    continue
                events.append({"date": e["date"], "title": e.get("title", ""), "type": e.get("type", "")})
            except Exception:
                continue
        r.events = events
        r.recovered = _recovered(events, target)
        r.notes = f"llm={len(events)} events"
        return r
    except Exception as e:
        r.error = f"{type(e).__name__}: {str(e)[:160]}"
        return r
    finally:
        try: page.close()
        except: pass
        r.took_s = time.time() - t0


# ---------- runner ----------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategies", type=str, default="A,B,C",
                   help="Comma-separated: A,B,C (default all)")
    p.add_argument("--company", type=str, default="", help="Run just this company (substring match)")
    p.add_argument("--out", type=str, default="", help="Write JSON results to this file")
    args = p.parse_args()

    strategies = [s.strip().upper() for s in args.strategies.split(",") if s.strip()]
    fns = {"A": strategy_link_follow, "B": strategy_aggressive_js, "C": strategy_llm}
    to_run = [(s, fns[s]) for s in strategies if s in fns]

    misses = DEFAULT_MISSES
    if args.company:
        misses = [m for m in misses if args.company.lower() in m[0].lower()]

    cfg = Config.load()
    all_results: List[StrategyResult] = []

    with sync_playwright() as p:
        ctx = load_context(p, cfg.state_dir / "9fin_state.json", headless=True)
        for i, (company, url, target) in enumerate(misses, 1):
            print(f"\n===== [{i}/{len(misses)}] {company}  target={target}")
            print(f"  url: {url}")
            for label, fn in to_run:
                r = fn(ctx, company, url, target)
                all_results.append(r)
                mark = "✓" if r.recovered else "✗"
                events_str = ", ".join(f"{e['date']} {e.get('title','')[:30]}" for e in r.events[:3])
                print(f"  [{mark}] {r.strategy:<20} took={r.took_s:>5.1f}s  len={r.text_len:>5}  {r.notes}  err={r.error or '-'}")
                if events_str:
                    print(f"      events: {events_str}")
        ctx.close()

    # Summary
    print("\n===== SUMMARY =====")
    by_strategy: dict = {}
    for r in all_results:
        by_strategy.setdefault(r.strategy, []).append(r)
    for s, results in by_strategy.items():
        rec = sum(1 for r in results if r.recovered)
        avg_t = sum(r.took_s for r in results) / max(len(results), 1)
        print(f"  {s}: {rec}/{len(results)} recovered  (avg {avg_t:.1f}s/page)")

    if args.out:
        with open(args.out, "w") as f:
            json.dump([r.__dict__ for r in all_results], f, indent=2, default=str)
        print(f"\nWrote details to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
