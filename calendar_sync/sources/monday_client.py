"""Monday.com client — pulls IR URLs from the NMD News Team Calendar Tracking board.

The NMD board is the source of truth for which companies have an IR calendar page and
where that page lives. Company ID / Core / Region are mirror columns fed from a linked
master board and aren't reliably populated on the NMD items themselves, so we key by
the item name (Company Street Name) and use the Calendar Link text column directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

COL_IR_URL = "text_mm01p3e4"           # text: Calendar Link
COL_HOSTS_CALLS = "boolean_mm6s659s"   # checkbox: Host Conference Calls
COL_OFFICE = "text_mm5nwtvy"           # text: Office Coverage (APAC/EMEA/etc.)
COL_PRIVATE = "text_mm5n6zmv"          # text: Private ("Yes"/"No")
COL_ASSOCIATE = "text_mm5n479t"        # text: Associate (analyst name)
COL_CORE = "lookup_mm67jjec"           # mirror: Core (Core / Core US / Core Europe / "")

MONDAY_API = "https://api.monday.com/v2"


@dataclass(frozen=True)
class NMDRow:
    street_name: str
    ir_url: str
    monday_item_id: str
    office: str
    private: str
    hosts_calls: str
    associate: str
    core: str  # "" | "Core" | "Core US" | "Core Europe"


def fetch_nmd_rows(api_token: str, board_id: int) -> List[NMDRow]:
    """Return every NMD row that has a Calendar Link (IR URL) set."""
    query = """
    query ($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 500, cursor: $cursor) {
          cursor
          items {
            id
            name
            column_values(ids: [
              "text_mm01p3e4",
              "boolean_mm6s659s",
              "text_mm5nwtvy",
              "text_mm5n6zmv",
              "text_mm5n479t",
              "lookup_mm67jjec"
            ]) {
              id
              text
              ... on MirrorValue { display_value }
            }
          }
        }
      }
    }
    """

    out: List[NMDRow] = []
    cursor: Optional[str] = None
    pages = 0

    while True:
        log.info("Fetching Monday NMD page %d (cursor=%s)...", pages + 1, "yes" if cursor else "none")
        resp = requests.post(
            MONDAY_API,
            headers={
                "Authorization": api_token,
                "Content-Type": "application/json",
                "API-Version": "2024-01",
            },
            json={"query": query, "variables": {"board": str(board_id), "cursor": cursor}},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Monday query failed: {resp.status_code} {resp.text[:500]}")

        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"Monday query errors: {payload['errors']}")

        boards = payload.get("data", {}).get("boards") or []
        if not boards:
            raise RuntimeError("Monday returned no boards — check API token / board access")

        page = boards[0].get("items_page") or {}
        items = page.get("items") or []

        for item in items:
            cols = {}
            core_val = ""
            for c in item.get("column_values", []):
                cid = c["id"]
                if cid == COL_CORE:
                    core_val = (c.get("display_value") or c.get("text") or "").strip()
                else:
                    cols[cid] = (c.get("text") or "")
            ir = cols.get(COL_IR_URL, "").strip()
            if not ir:
                continue
            out.append(NMDRow(
                street_name=(item.get("name") or "").strip(),
                ir_url=ir,
                monday_item_id=str(item.get("id") or ""),
                office=cols.get(COL_OFFICE, "").strip(),
                private=cols.get(COL_PRIVATE, "").strip(),
                hosts_calls=cols.get(COL_HOSTS_CALLS, "").strip(),
                associate=cols.get(COL_ASSOCIATE, "").strip(),
                core=core_val,
            ))

        pages += 1
        cursor = page.get("cursor")
        if not cursor:
            break

    log.info("Monday NMD: %d rows with IR URL across %d pages", len(out), pages)
    return out
