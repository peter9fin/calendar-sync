"""Emit `dashboards/gaps.json` for consumption by the Cowork artifact."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


def write_gaps_json(
    diffs: Iterable[dict],
    repo_root: Path,
    last_scrape: dt.date,
) -> Path:
    """Write dashboards/gaps.json.

    Each diff dict is expected to contain:
      id, name, ir_url, to_file (list[str] dates), on_9fin (list[{d,t}]),
      status ("to-check" | "cleared" | "changed" | "urlbroken"),
      analyst, region, country, hosts_calls, monday_item_id
    """
    out_dir = repo_root / "dashboards"
    out_dir.mkdir(exist_ok=True)

    normalised = [_normalise(d) for d in diffs]
    normalised.sort(key=lambda r: r["name"].lower())

    analysts = sorted({r["analyst"] for r in normalised if r["analyst"]})

    payload = {
        "last_scrape": last_scrape.isoformat(),
        "company_count": len(normalised),
        "analysts": analysts,
        "counts": _counts(normalised),
        "companies": normalised,
    }
    out = out_dir / "gaps.json"
    out.write_text(json.dumps(payload, indent=2))
    log.info("Wrote %s (%d companies across %d analysts)", out, len(normalised), len(analysts))
    return out


def _normalise(d: dict) -> dict:
    analyst = (d.get("analyst") or "").strip()
    return {
        "id": str(d["id"]),
        "name": d["name"],
        "ir": d.get("ir_url") or d.get("ir") or "",
        "toFile": d.get("to_file") or [],
        "on9fin": d.get("on_9fin") or [],
        "status": d.get("status") or "to-check",
        "analyst": analyst or "Unassigned",
        "region": d.get("region") or "",
        "country": d.get("country") or "",
        "hostsCalls": d.get("hosts_calls") or "",
    }


def _counts(rows: list[dict]) -> dict:
    c = {"to-check": 0, "changed": 0, "cleared": 0, "no-dates": 0, "urlbroken": 0, "candidate": 0, "all": len(rows)}
    for r in rows:
        s = r["status"]
        if s in c:
            c[s] = c.get(s, 0) + 1
        if r["toFile"] and s not in ("cleared", "urlbroken", "no-dates"):
            c["candidate"] += 1
    return c
