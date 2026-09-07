"""Mark a company as 'checked / calendar matches 9fin' — sets status GREEN.

Usage:
    python scripts/mark_checked.py "HelloFresh"
    python scripts/mark_checked.py --unmark "HelloFresh"
    python scripts/mark_checked.py --all-yellow    # sweep all currently-yellow

The mark records the current content hash + candidate-date set. On the next run
the daily_review script auto-flips this company back to RED (or YELLOW) if the
IR page's content changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calendar_sync.config import Config


def _load(state_path: Path) -> dict:
    if not state_path.exists():
        print(f"error: {state_path} not found — run daily_review.py first")
        sys.exit(1)
    return json.loads(state_path.read_text())


def _save(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2))


def mark_one(state: dict, company: str, unmark: bool) -> bool:
    key = company.lower().strip()
    if key not in state:
        # Try partial match
        matches = [k for k in state if company.lower() in k]
        if not matches:
            return False
        if len(matches) > 1:
            print(f"Ambiguous — matches: {matches}")
            return False
        key = matches[0]
    entry = state[key]
    if unmark:
        entry["status"] = "ERROR" if entry.get("error") else "YELLOW"
        entry["marked_green_at"] = None
        entry["hash_at_green"] = None
        entry["dates_at_green"] = []
        print(f"UNMARKED {entry['company']}")
    else:
        entry["status"] = "GREEN"
        entry["marked_green_at"] = datetime.now().isoformat(timespec="seconds")
        entry["hash_at_green"] = entry.get("content_hash", "")
        entry["dates_at_green"] = entry.get("all_page_dates", [])
        print(f"GREEN {entry['company']} — {len(entry['dates_at_green'])} dates locked in")
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("company", nargs="?", type=str, help="Company name (or substring)")
    p.add_argument("--unmark", action="store_true", help="Clear the green mark")
    p.add_argument("--all-yellow", action="store_true",
                   help="Sweep every currently-YELLOW company to GREEN")
    args = p.parse_args()

    cfg = Config.load()
    state_path = cfg.state_dir / "company_state.json"
    state = _load(state_path)

    if args.all_yellow:
        n = 0
        for entry in state.values():
            if entry.get("status") == "YELLOW":
                entry["status"] = "GREEN"
                entry["marked_green_at"] = datetime.now().isoformat(timespec="seconds")
                entry["hash_at_green"] = entry.get("content_hash", "")
                entry["dates_at_green"] = entry.get("all_page_dates", [])
                n += 1
        _save(state_path, state)
        print(f"marked {n} companies GREEN")
        return 0

    if not args.company:
        print("Provide a company name, or use --all-yellow")
        return 2

    ok = mark_one(state, args.company, args.unmark)
    if not ok:
        print(f"No company matching {args.company!r}")
        return 3
    _save(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
