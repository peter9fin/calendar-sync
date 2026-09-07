"""Render the daily review as a traffic-light dashboard artifact.

Reads state/company_state.json (updated by daily_review.py + mark_checked.py)
and produces a compact, filterable dashboard.

Status model per company:
    GREEN   — analyst marked checked; page unchanged since mark
    YELLOW  — candidate dates on IR page not on 9fin; needs review
    RED     — was marked green, page has since changed; needs re-review
    ERROR   — fetch failed
    BLANK   — no candidates, never marked (rare; usually just landing pages)

Design: cool cream ground, considered traffic-light palette, IBM Plex Serif/
Sans/Mono, filter chips, dense-but-scannable card grid.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config


def esc(s):
    return html.escape(str(s) if s is not None else "")


def domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def relative(iso: str) -> str:
    """Human 'checked 2 days ago' from an ISO timestamp."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return ""
    delta = datetime.now() - dt
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return "just now"
        if hours == 1:
            return "1 hour ago"
        return f"{hours} hours ago"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return dt.date().isoformat()


CSS = """
:root {
  --ground: #F5F6F4;
  --surface: #FFFFFF;
  --surface-2: #FBFBF9;
  --ink: #111418;
  --ink-2: #2A2E36;
  --muted: #5C6470;
  --rail: #E4E5E0;
  --rail-2: #EFF0EC;

  --green: #0F5C42;
  --green-soft: #E1EEE7;
  --amber: #8A6408;
  --amber-soft: #F4EBD3;
  --red: #943127;
  --red-soft: #F1DAD6;
  --grey: #7F858F;
  --grey-soft: #ECEDE9;

  --shadow-1: 0 1px 0 rgba(17,20,24,0.04);
  --shadow-2: 0 8px 24px -14px rgba(17,20,24,0.10);
}

:root[data-theme="dark"], :root:not([data-theme="light"]) {
  @media (prefers-color-scheme: dark) {
    --ground: #0E1013;
    --surface: #191C21;
    --surface-2: #14171B;
    --ink: #EAECEF;
    --ink-2: #C7CBD0;
    --muted: #8F949C;
    --rail: #252932;
    --rail-2: #1F232A;

    --green: #4AB48C;
    --green-soft: #133329;
    --amber: #D89845;
    --amber-soft: #3A2D14;
    --red: #D8695D;
    --red-soft: #3A1D19;
    --grey: #8B9199;
    --grey-soft: #1F2229;

    --shadow-1: 0 1px 0 rgba(0,0,0,0.4);
    --shadow-2: 0 10px 26px -14px rgba(0,0,0,0.5);
  }
}
:root[data-theme="dark"] {
  --ground: #0E1013;
  --surface: #191C21;
  --surface-2: #14171B;
  --ink: #EAECEF;
  --ink-2: #C7CBD0;
  --muted: #8F949C;
  --rail: #252932;
  --rail-2: #1F232A;

  --green: #4AB48C;
  --green-soft: #133329;
  --amber: #D89845;
  --amber-soft: #3A2D14;
  --red: #D8695D;
  --red-soft: #3A1D19;
  --grey: #8B9199;
  --grey-soft: #1F2229;

  --shadow-1: 0 1px 0 rgba(0,0,0,0.4);
  --shadow-2: 0 10px 26px -14px rgba(0,0,0,0.5);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "ss01" 1, "ss02" 1;
}

.wrap {
  max-width: 1180px;
  margin: 0 auto;
  padding: 48px 32px 80px;
}

.mono { font-family: 'IBM Plex Mono', ui-monospace, Menlo, monospace; font-variant-numeric: tabular-nums; }
.serif { font-family: 'IBM Plex Serif', Georgia, serif; }

/* ── HEADER ── */
header.crest {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  padding-bottom: 22px;
  margin-bottom: 36px;
  border-bottom: 1px solid var(--rail);
}
header .brand {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-weight: 500;
  font-size: 26px;
  letter-spacing: -0.015em;
  color: var(--ink);
  line-height: 1.1;
}
header .brand span {
  color: var(--muted);
  font-weight: 400;
}
header .brand small {
  display: block;
  margin-top: 6px;
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 13px;
  color: var(--muted);
  font-weight: 400;
  letter-spacing: 0;
}
header .stamp {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  text-align: right;
}
header .stamp b { color: var(--ink-2); font-weight: 500; }

/* ── KPI HERO ── */
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1px;
  background: var(--rail);
  border: 1px solid var(--rail);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 28px;
  box-shadow: var(--shadow-1), var(--shadow-2);
}
.kpi {
  background: var(--surface);
  padding: 20px 22px 22px;
  position: relative;
}
.kpi::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
}
.kpi.green::before { background: var(--green); }
.kpi.amber::before { background: var(--amber); }
.kpi.red::before   { background: var(--red); }
.kpi.grey::before  { background: var(--grey); }

.kpi .k {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 10px;
  font-family: 'IBM Plex Mono', monospace;
}
.kpi .v {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-size: 40px;
  font-weight: 500;
  line-height: 1;
  letter-spacing: -0.025em;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}
.kpi .sub {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
  font-family: 'IBM Plex Mono', monospace;
}
.kpi.green .v { color: var(--green); }
.kpi.amber .v { color: var(--amber); }
.kpi.red .v   { color: var(--red); }

/* ── FILTERS ── */
.filters {
  display: flex;
  gap: 6px;
  margin-bottom: 20px;
  flex-wrap: wrap;
  align-items: center;
}
.filters .lbl {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  margin-right: 10px;
}
.chip {
  background: var(--surface);
  border: 1px solid var(--rail);
  border-radius: 24px;
  padding: 6px 14px;
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 13px;
  color: var(--ink-2);
  cursor: pointer;
  transition: all 0.12s;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.chip:hover { border-color: var(--ink-2); }
.chip.on {
  background: var(--ink);
  color: var(--surface);
  border-color: var(--ink);
}
.chip .c {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--muted);
}
.chip.on .c { color: var(--surface); opacity: 0.7; }

.searchbox {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.searchbox input {
  padding: 7px 12px;
  border: 1px solid var(--rail);
  border-radius: 5px;
  background: var(--surface);
  color: var(--ink);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 13px;
  min-width: 220px;
  outline: none;
  transition: border-color 0.12s;
}
.searchbox input:focus { border-color: var(--ink-2); }

/* ── CARD GRID ── */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
}

.card {
  background: var(--surface);
  border: 1px solid var(--rail);
  border-left-width: 3px;
  border-radius: 6px;
  padding: 16px 18px 14px;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-1);
  transition: transform 0.12s, box-shadow 0.12s;
}
.card:hover { box-shadow: var(--shadow-2); transform: translateY(-1px); }
.card.green  { border-left-color: var(--green); }
.card.amber  { border-left-color: var(--amber); }
.card.red    { border-left-color: var(--red); }
.card.grey   { border-left-color: var(--grey); }
.card.error  { border-left-color: var(--red); background: var(--red-soft); }

.card-h {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}
.card-h .co {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-size: 16px;
  font-weight: 500;
  letter-spacing: -0.005em;
  color: var(--ink);
  text-wrap: balance;
  flex: 1;
  min-width: 0;
  word-break: break-word;
}
.pill {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 3px 9px;
  border-radius: 3px;
  font-weight: 500;
  white-space: nowrap;
}
.pill.green { background: var(--green-soft); color: var(--green); }
.pill.amber { background: var(--amber-soft); color: var(--amber); }
.pill.red   { background: var(--red-soft); color: var(--red); }
.pill.grey  { background: var(--grey-soft); color: var(--muted); }

.data {
  display: grid;
  grid-template-columns: auto 1fr;
  column-gap: 12px;
  row-gap: 6px;
  margin-bottom: 12px;
  font-size: 13px;
}
.data .k {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
  align-self: center;
  white-space: nowrap;
}
.data .v {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--ink-2);
  overflow-wrap: anywhere;
  min-width: 0;
}
.data .v.muted { color: var(--muted); }
.data .v.err   { color: var(--red); }

.footer {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--rail-2);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.footer a.ir {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--muted);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.footer a.ir:hover { color: var(--ink-2); text-decoration: underline; }

.btn {
  display: inline-block;
  padding: 6px 14px;
  border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  font-weight: 500;
  text-decoration: none;
  cursor: pointer;
  border: none;
  white-space: nowrap;
}
.btn.primary { background: var(--ink); color: var(--surface); }
.btn.primary:hover { opacity: 0.88; }
.btn.ghost { background: transparent; color: var(--muted); border: 1px solid var(--rail); }
.btn.ghost:hover { color: var(--ink); border-color: var(--ink-2); }

/* Empty state */
.empty {
  padding: 64px 24px;
  text-align: center;
  background: var(--surface);
  border: 1px solid var(--rail);
  border-radius: 8px;
  color: var(--muted);
}
.empty .big {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-size: 22px;
  color: var(--ink);
  margin-bottom: 8px;
  font-weight: 500;
}
.empty .check {
  font-size: 48px;
  color: var(--green);
  margin-bottom: 12px;
  line-height: 1;
}

footer.foot {
  margin-top: 64px;
  padding-top: 20px;
  border-top: 1px solid var(--rail);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.04em;
}
footer.foot .pipe { color: var(--rail); margin: 0 8px; }

@media (max-width: 700px) {
  .wrap { padding: 32px 20px 60px; }
  header.crest { flex-direction: column; align-items: flex-start; gap: 12px; }
  header .stamp { text-align: left; }
  .grid { grid-template-columns: 1fr; }
  .searchbox { margin-left: 0; width: 100%; }
  .searchbox input { min-width: 0; width: 100%; }
}
"""


def _card(entry: dict) -> str:
    status = entry.get("status", "BLANK")
    css_status = {"GREEN": "green", "YELLOW": "amber", "RED": "red",
                  "ERROR": "error"}.get(status, "amber")
    pill_cls = {"GREEN": "green", "YELLOW": "amber", "RED": "red",
                "ERROR": "red"}.get(status, "amber")
    pill_label = {"GREEN": "cleared", "YELLOW": "to check",
                  "RED": "changed", "ERROR": "url broken"}.get(status, "to check")

    gaps = entry.get("audit_gaps") or []
    nine = entry.get("ninefin_events") or []
    nine_str = " · ".join(f"{e['date']} {e['title'][:32]}" for e in sorted(nine, key=lambda x: x['date'])) if nine else "no upcoming events on 9fin"

    data_rows = []
    if status == "ERROR":
        data_rows.append(('<div class="k">Error</div><div class="v err">{}</div>'
                          .format(esc(entry.get("error") or ""))))
    else:
        if gaps:
            data_rows.append('<div class="k">To file</div><div class="v">{}</div>'.format(esc(", ".join(gaps))))
        elif status == "GREEN":
            data_rows.append('<div class="k">Locked</div><div class="v muted">{}</div>'.format(esc(", ".join(entry.get("dates_at_green") or []) or "—")))
        else:
            data_rows.append('<div class="k">On page</div><div class="v muted">{}</div>'.format(esc(", ".join(entry.get("all_page_dates") or []) or "no future dates")))
        data_rows.append('<div class="k">On 9fin</div><div class="v muted">{}</div>'.format(esc(nine_str)))

    # Footer
    if status == "GREEN":
        marked = relative(entry.get("marked_green_at") or "")
        footer_html = f'<a href="{esc(entry["url"])}" class="ir mono" target="_blank" rel="noopener">{esc(domain(entry["url"]))}</a><span class="mono" style="font-size:11px; color:var(--muted);">Cleared {esc(marked)}</span>'
    elif status in ("YELLOW", "RED"):
        footer_html = (f'<a href="{esc(entry["url"])}" class="ir mono" target="_blank" rel="noopener">{esc(domain(entry["url"]))} ↗</a>'
                       f'<button class="btn primary" data-mark="{esc(entry["company"])}">Mark checked</button>')
    else:
        footer_html = f'<a href="{esc(entry["url"])}" class="ir mono" target="_blank" rel="noopener">{esc(domain(entry["url"]))}</a>'

    hasgaps = "1" if entry.get("audit_gaps") else "0"
    return f"""
<div class="card {css_status}" data-status="{status}" data-hasgaps="{hasgaps}" data-company="{esc(entry.get('company',''))}">
  <div class="card-h">
    <span class="co">{esc(entry.get('company',''))}</span>
    <span class="pill {pill_cls}">{pill_label}</span>
  </div>
  <div class="data">
    {''.join(data_rows)}
  </div>
  <div class="footer">
    {footer_html}
  </div>
</div>"""


def render(state_path: Path, out_path: Path, filter_label: str, run_stamp: str) -> None:
    state = json.loads(state_path.read_text())
    entries = list(state.values())

    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0, "ERROR": 0}
    for e in entries:
        s = e.get("status", "YELLOW")
        counts[s] = counts.get(s, 0) + 1

    total = len(entries)
    # Order: RED → YELLOW → ERROR → GREEN. Within RED/YELLOW, put ones with
    # candidate dates first — they're actionable "add these to 9fin" items,
    # not just "eyeball the page" ones.
    def _sort_key(e):
        status = e.get("status", "YELLOW")
        rank = {"RED": 0, "YELLOW": 1, "ERROR": 2, "GREEN": 3}.get(status, 9)
        has_gaps = 0 if e.get("audit_gaps") else 1
        return (rank, has_gaps, e.get("company", "").lower())
    entries.sort(key=_sort_key)

    cards = "\n".join(_card(e) for e in entries)

    body = f"""<title>Calendar review — {filter_label}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{CSS}</style>

<div class="wrap">

  <header class="crest">
    <div class="brand">
      Calendar review <span>· 9fin credit desk</span>
      <small>{esc(filter_label)} · {total} companies</small>
    </div>
    <div class="stamp">
      <span>Last scrape</span> <b>{esc(run_stamp)}</b>
    </div>
  </header>

  <div class="kpis">
    <div class="kpi amber">
      <div class="k">To check</div>
      <div class="v">{counts['YELLOW']}</div>
      <div class="sub">verify against 9fin</div>
    </div>
    <div class="kpi red">
      <div class="k">Changed since cleared</div>
      <div class="v">{counts['RED']}</div>
      <div class="sub">re-verify these</div>
    </div>
    <div class="kpi green">
      <div class="k">Cleared</div>
      <div class="v">{counts['GREEN']}</div>
      <div class="sub">calendar matches 9fin</div>
    </div>
    <div class="kpi grey">
      <div class="k">URL broken</div>
      <div class="v">{counts['ERROR']}</div>
      <div class="sub">fix URL on Monday</div>
    </div>
  </div>

  <div class="filters">
    <span class="lbl">Filter</span>
    <button class="chip on" data-filter="TODO">To do <span class="c">{counts['YELLOW'] + counts['RED']}</span></button>
    <button class="chip"    data-filter="GAPS">With candidate dates <span class="c">{sum(1 for e in entries if e.get('audit_gaps') and e.get('status') in ('YELLOW','RED'))}</span></button>
    <button class="chip"    data-filter="GREEN">Cleared <span class="c">{counts['GREEN']}</span></button>
    <button class="chip"    data-filter="ERROR">URL broken <span class="c">{counts['ERROR']}</span></button>
    <button class="chip"    data-filter="ALL">All <span class="c">{total}</span></button>
    <div class="searchbox">
      <input id="q" type="text" placeholder="Filter by company…" autocomplete="off">
    </div>
  </div>

  <div class="grid" id="grid">
    {cards}
  </div>

  <div id="empty" class="empty" style="display:none;">
    <div class="check">✓</div>
    <div class="big">All caught up.</div>
    <div>No companies match this filter.</div>
  </div>

  <footer class="foot">
    <div>Calendar review <span class="pipe">/</span> 9fin credit desk</div>
    <div>{esc(run_stamp)}</div>
  </footer>
</div>

<script>
(function() {{
  var chips = document.querySelectorAll('.chip[data-filter]');
  var q = document.getElementById('q');
  var cards = document.querySelectorAll('#grid .card');
  var empty = document.getElementById('empty');
  var current = 'TODO';

  function apply() {{
    var needle = (q.value || '').toLowerCase().trim();
    var visible = 0;
    cards.forEach(function(c) {{
      var s = c.getAttribute('data-status');
      var hasGaps = c.getAttribute('data-hasgaps') === '1';
      var passStatus = (current === 'ALL')
        || (current === 'TODO' && (s === 'YELLOW' || s === 'RED'))
        || (current === 'GAPS' && hasGaps && (s === 'YELLOW' || s === 'RED'))
        || (current === s);
      var passSearch = !needle || c.getAttribute('data-company').toLowerCase().indexOf(needle) !== -1;
      var show = passStatus && passSearch;
      c.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    empty.style.display = visible === 0 ? '' : 'none';
    document.getElementById('grid').style.display = visible === 0 ? 'none' : '';
  }}

  chips.forEach(function(c) {{
    c.addEventListener('click', function() {{
      chips.forEach(function(x) {{ x.classList.remove('on'); }});
      c.classList.add('on');
      current = c.getAttribute('data-filter');
      apply();
    }});
  }});
  q.addEventListener('input', apply);
  apply();

  // "Mark checked" placeholder — this static build shows a hint; the
  // interactive persistent version is a follow-up with db capability.
  document.querySelectorAll('[data-mark]').forEach(function(b) {{
    b.addEventListener('click', function() {{
      var name = b.getAttribute('data-mark');
      alert(
        "To mark this company checked, run:\\n\\n" +
        "  python scripts/mark_checked.py " + JSON.stringify(name) + "\\n\\n" +
        "then re-generate the dashboard."
      );
    }});
  }});
}})();
</script>
"""
    out_path.write_text(body)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, default=None)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--filter-label", type=str, default="Peter Megarity")
    args = p.parse_args()

    cfg = Config.load()
    state_path = args.state or (cfg.state_dir / "company_state.json")
    if not state_path.exists():
        print(f"error: {state_path} not found — run daily_review.py first")
        return 2

    run_stamp = date.today().isoformat() + " " + datetime.now().strftime("%H:%M")
    render(state_path, args.out, args.filter_label, run_stamp)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
