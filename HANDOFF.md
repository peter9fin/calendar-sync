# calendar-sync — Session Handoff

**Repo**: https://github.com/peter9fin/calendar-sync
**Owner**: Peter Megarity (peter.megarity@9fin.com), 9fin credit desk
**Handoff date**: 2026-09-09
**Purpose**: catch a fresh assistant up to the exact state this project is in so work can continue without repeating discovery.

---

## 1. What this tool does

Automates the daily calendar-sync check the 9fin news team currently does by hand.
For every "core" company in the 9fin universe (~1,135 today), it:

1. Fetches upcoming earnings/results dates from the best available public source.
2. Compares them to what's already on 9fin's forward calendar.
3. Reports gaps (dates the source has that 9fin doesn't) so the news team can file them.

Runs unattended on **GitHub Actions** every weekday at 05:00 UTC (workflow `.github/workflows/daily.yml`). Writes `dashboards/gaps.json` back to the repo for a Cowork Artifact to consume, and DMs Peter a Slack summary.

---

## 2. Architecture — source dispatch (added this session, most important change)

Before this session: the pipeline ran the Playwright scraper against **every** company's IR page, then bolted SEC on top. Result: ~34% URL failures + ~56% "no-dates" and lots of false-positive gaps from scraping the wrong page.

Now: **one source per company**, assigned via `dashboards/url_audit.csv`.

| Source type | Fetcher | Companies |
|---|---|---|
| `sec` | `calendar_sync/sources/sec_client.py` (SEC EDGAR 8-K/6-K) | 364 |
| `ir_calendar_page` | `calendar_sync/sources/ir_scraper_async.py` (Playwright) | 207 |
| `rss` | ⏳ **not wired yet** | 113 |
| `jsonld` | ⏳ **not wired yet** | 53 |
| `ical` | ⏳ **not wired yet** | 6 |
| `needs_fix` | queue for URL replacement | 392 → 175 (see §4) |
| `orphan` | fully unfound | small |

Dispatcher lives at `calendar_sync/sources/dispatcher.py`. It reads
`dashboards/url_audit.csv` and returns `{source_type, source_id}` for each
company. `main.py` buckets companies by source_type, calls each fetcher
exactly once, then diffs against 9fin's calendar.

**Key insight**: Peter's earlier frustration ("we don't have a valuable
source for each of the 1,200 core companies") drove this refactor. The
answer isn't better scraping — it's picking the right source per company.

---

## 3. What was built this session

### New files
- `calendar_sync/sources/sec_client.py` — SEC EDGAR poller (CIK match + 8-K/6-K extraction), rate-limited to ~9 req/s.
- `calendar_sync/sources/dispatcher.py` — reads `dashboards/url_audit.csv`, assigns source per company.
- `scripts/audit_urls.py` — one-shot pass that classifies every URL (calendar-page / IR-no-calendar / broken / etc.) and probes for structured feeds (RSS/iCal/JSON-LD) and SEC CIKs. Writes `dashboards/url_audit.csv`.
- `scripts/reclassify_urls.py` — re-runs the audit's `needs_fix` subset through Playwright (JS-rendered) instead of aiohttp. Rescued 61 URLs that only looked broken to plain HTTP.
- `HANDOFF.md` — this file.

### Modified files
- `calendar_sync/main.py` — refactored around the dispatcher. Now logs `Automated coverage: N/T` and per-source signal counts.
- `calendar_sync/render/gaps_json.py` — added `needs_fix` and `orphan` buckets.
- `.github/workflows/daily.yml` — dropped Playwright `--with-deps` (was hitting flaky Google Chrome apt repo), added retry loop, uploads `dashboards/gaps.json` as an artifact even on dry runs.
- `requirements.txt` — added `aiohttp` (SEC client + audit).

### Session commits (in order)
```
6bf3aba chore: upload gaps.json artifact on dry runs
a4e59d2 fix(sec): reject filing-date and period-boundary FPs
aef3745 feat: source-dispatch pipeline — one source per company
621e961 ci: avoid flaky google-chrome apt repo in Playwright install
6e6763e reclassify: rescue 61 URLs to keep-current via Playwright re-fetch
```
Plus earlier `52f0f8a` (SEC as secondary), `ba57702` (aiohttp req), `e40a650` (metric split).

---

## 4. URL discovery pass — 337 companies, master CSV built

Peter asked me to "go find" the correct calendar URLs for companies whose current URL is broken or wrong. I spawned 13 parallel discovery agents (one per batch of ~28 companies) using WebSearch + WebFetch. Output: `dashboards/url_replacements_master.csv`.

**Verdict counts (337 unique):**

| Verdict | Count | Action |
|---|---|---|
| `found` | 59 | ✅ Verified real calendar page |
| `found-tentative` | 103 | IR page + calendar section, but thin or JS-loaded |
| `no-calendar-page` | 163 | Mostly PE-owned bond issuers, sovereign SOEs, private companies — need email-subscription channel or manual monitoring |
| `dead` | 6 | Defunct / rebranded IR sites |
| `fetch-error` / `ir-page-no-calendar` | 6 | Retry candidates |

**Three actionable buckets:**

- **102 real URL replacements** (`proposed_url` ≠ `current_url`, verdict is `found`/`found-tentative`) → **Peter needs to review then apply to Monday.com**
- **60 verified same-URL** (audit was wrong to flag; belongs in `keep-current`) → auto-move in registry once Peter OKs
- **175 no automated calendar source** → email-subscription queue (Phase 3, not built)

**Projected coverage if all applied**:
- Currently: **571 / 1135 (50%)** actively fetching
- After URL updates + audit rescue: **~733 / 1135 (65%)**
- After RSS + iCal + JSON-LD fetchers ship: **~905 / 1135 (80%)**

---

## 5. Current state — where we're leaving things

### Last successful full-book dry run: [34385375929](https://github.com/peter9fin/calendar-sync/actions/runs/34385375929)
Status breakdown:
- `to-check` (gaps): **18** (down from 48 pre-dispatch — 62% FP reduction from not scraping known-wrong URLs)
- `cleared`: 29
- `no-dates`: 659 (mostly RSS/iCal/JSON-LD companies waiting for fetcher; also SEC-only companies with quiet 8-K windows)
- `urlbroken`: 37 (down from 386)
- `needs_fix`: 392 (URL discovery queue)

### Cron
Weekday 05:00 UTC. Green after we dropped `--with-deps` from Playwright install. **Real production runs still fail Slack** because the bot token lacks the `users:read.email` scope. This is unresolved — Peter deferred it. Fix options documented earlier in the session: either add the OAuth scope to the Slack app OR add `SLACK_TARGET_USER_ID` as a secret and skip the email lookup.

### Known bugs / rough edges
1. `scripts/reclassify_urls.py` has a display bug: the "before" Counter is polluted by in-place mutation. The **rescued count (61) is correct**; the "before" bucket display is wrong. Cosmetic only.
2. Batch 01 of URL discovery overlapped with batch 02+ (I forgot to exclude batch 01's companies when generating the reclassified queue). Dedup in the consolidation script handled it — no data loss, just wasted compute.
3. Batch 05's first attempt tried to delegate to sub-agents (illegal); had to resume with a corrective message. It then produced its CSV correctly.

---

## 6. Open work in priority order

1. **Peter reviews `dashboards/url_replacements_master.csv`** (attached this session) — approves batch of URL replacements.
2. **Apply the 102 URL updates on Monday.com** using MCP `change_item_column_values` (Peter must confirm per batch — his standing rule is no un-approved Monday writes).
3. **Auto-rescue the 60 verified-same-URL companies** — script to promote them from `needs_fix` back to `keep-current` in `dashboards/url_audit.csv`.
4. **Build the RSS + iCal + JSON-LD fetchers.** Framework in `calendar_sync/sources/dispatcher.py` already marks these as stubs. `scripts/probe_feeds.py` has the discovery code — feeds themselves aren't fetched yet in the daily pipeline. Building 3 async fetchers in `calendar_sync/sources/{rss,ical,jsonld}_client.py` and wiring them into `main.py` would unlock 172 companies.
5. **Email subscription channel (Phase 3, not started).** For the 175 `no-calendar-page` companies (mostly PE issuers): provision an inbox, subscribe manually or via a signup form probe, parse incoming earnings-announcement emails.
6. **Team sharing + per-analyst filter on the dashboard.** Peter mentioned early in the session. `dashboards/gaps.json` already carries the `analyst` field (from Monday NMD board), so the Artifact just needs filter chips.
7. **Skip companies already covered.** Also Peter's ask: if 9fin already has an event within the expected quarter's window, don't scrape until after that period passes. Would cut runtime 40-60% at steady state.

---

## 7. Key files map

```
.github/workflows/daily.yml         # weekday 05:00 UTC cron
calendar_sync/
  main.py                           # ⭐ orchestrator, refactored for dispatch
  config.py                         # env-var loading, .env
  logic/
    diff.py                         # find_gaps — 5-day tolerance
    normalise.py                    # event kind detection (RESULTS/CALL/CMD/...)
    change_detect.py                # hash + snapshot per company (legacy)
  render/
    gaps_json.py                    # writes dashboards/gaps.json
  sources/
    dispatcher.py                   # ⭐ NEW — one source per company
    sec_client.py                   # ⭐ NEW — SEC EDGAR poller (async)
    ir_scraper.py                   # sync Playwright (kept for compat)
    ir_scraper_async.py             # async Playwright — the active scraper
    monday_client.py                # NMD board GraphQL
    ninefin_client.py               # 9fin login + calendar fetch
    omni_client.py                  # Omni "core companies" API
  notify/
    slack.py                        # DM Peter the summary
scripts/
  audit_urls.py                     # ⭐ NEW — one-shot URL classifier
  reclassify_urls.py                # ⭐ NEW — Playwright re-fetch of needs_fix
  classify_sources.py               # older SEC-only classifier
  poll_sec.py                       # older SEC poll script (superseded by sec_client)
  probe_feeds.py                    # RSS/iCal/JSON-LD discovery (used by audit)
  benchmark_strategies.py           # extractor tuning benchmark
  fix_urls.py                       # earlier Monday URL fixer (unused now)
  daily_review.py                   # pre-cron review script (legacy)
  mark_checked.py                   # mark company GREEN in state (legacy)
  render_review_artifact.py         # traffic-light HTML artifact (legacy)
  refresh_state.sh                  # regenerate 9fin session cookies
  upload_secrets.sh                 # push env-vars to GH Actions secrets
dashboards/
  url_audit.csv                     # ⭐ registry — one source per company
  url_replacements_master.csv       # ⭐ Peter's review queue for URL fixes
  url_replacements_batch{01..13}.csv  # per-agent output (redundant, kept for audit)
state/
  9fin_state.json                   # Playwright storage state (session cookies)
  sec_tickers.json                  # SEC CIK/ticker index (~8k entries)
  # + earlier per-associate JSONs (feeds/sources/announcements/company_state) — legacy
```

---

## 8. How to reproduce a full-book measurement

Locally:
```bash
cd /Users/petermegarity/Claude/repos/calendar-sync
.venv/bin/python -m calendar_sync.main --dry-run
```
Takes ~20 min. Writes `dashboards/gaps.json` and logs `Automated coverage: X/Y`.

Via CI:
```bash
gh workflow run daily.yml -f dry_run=true
```
Watch: `gh run watch <RUN_ID>`. Artifact download: `gh run download <RUN_ID>`.

To re-generate the source registry after Monday changes:
```bash
.venv/bin/python -m scripts.audit_urls               # ~30 min (aiohttp)
.venv/bin/python -m scripts.reclassify_urls          # ~5 min (Playwright)
git add dashboards/url_audit.csv && git commit -m "audit: refresh"
```

---

## 9. Peter's operational rules to preserve

- **No un-approved writes to Monday.com.** Per-batch approval, not blanket. Batches he approved in previous sessions do not carry forward.
- **No un-approved Slack messages / commits to third-party services.**
- Slack DM target: `peter.megarity@9fin.com`.
- Slack app currently lacks `users:read.email` scope — real Slack sends fail. Deferred.
- Peter uses JumpCloud SSO for 9fin — session cookies live in `state/9fin_state.json`, base64 in GH secret `NINEFIN_STATE_B64`. Refresh with `scripts/refresh_state.sh` when they expire.
- Preferred output style: terse, structured, numbers-first. Peter dislikes prose that doesn't lead with the answer.

---

## 10. One thing to know before you start

Peter tried this project before this session with a loose extractor and got ~68% recall but ~90% false-positive rate on gaps. The session's throughline is **precision over recall**: better to say nothing than to Slack fake gaps. The dispatch pipeline (§2) is a direct consequence — assigning one source per company and only running the fetcher that source needs means fewer, cleaner signals. Any future changes should preserve that: don't add a broad fallback scraper "just in case."
