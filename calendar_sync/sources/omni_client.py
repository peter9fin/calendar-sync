"""Omni Analytics client — pulls core company list from the `companies` topic."""
from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass
from typing import List

import pyarrow as pa
import requests

log = logging.getLogger(__name__)

VIEW = "omni_dbt_marts__companies"
TOPIC = "companies"


@dataclass(frozen=True)
class CoreCompany:
    company_id: str
    street_name: str
    monday_core: str  # "Core" | "Core US" | "Core Europe"


def _decode_arrow(b64: str) -> List[dict]:
    buf = base64.b64decode(b64)
    reader = pa.ipc.open_stream(io.BytesIO(buf))
    table = reader.read_all()
    return table.to_pylist()


def fetch_core_companies(api_key: str, base_url: str, model_id: str) -> List[CoreCompany]:
    """Query Omni's `companies` topic for rows where Monday Core is set.

    Response is NDJSON: line 0 is job registration, line 1 is the completion event
    whose `result` field is base64-encoded Arrow.
    """
    url = f"{base_url}/api/v1/query/run"

    query = {
        "modelId": model_id,
        "table": VIEW,
        "join_paths_from_topic_name": TOPIC,
        "fields": [
            f"{VIEW}.company_id",
            f"{VIEW}.street_name",
            f"{VIEW}.monday_core",
        ],
        "filters": {
            f"{VIEW}.monday_core": {"type": "null", "is_negative": True},
        },
        "limit": 2000,
        "sorts": [{"column_name": f"{VIEW}.street_name", "sort_descending": False}],
    }

    log.info("Querying Omni companies topic for core companies...")
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Omni query failed: {resp.status_code} {resp.text[:500]}")

    completion = None
    for line in resp.text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("status") == "COMPLETE" and "result" in obj:
            completion = obj
            break
        if obj.get("status") == "FAILED":
            raise RuntimeError(
                f"Omni job failed: {obj.get('error_type')} {obj.get('error_message')}"
            )

    if completion is None:
        raise RuntimeError(f"Omni: no COMPLETE event in response: {resp.text[:500]}")

    rows = _decode_arrow(completion["result"])
    if not rows:
        raise RuntimeError("Omni returned zero core companies — check API key + model access")

    out: List[CoreCompany] = []
    for r in rows:
        cid = str(r.get(f"{VIEW}.company_id") or "").strip()
        name = str(r.get(f"{VIEW}.street_name") or "").strip()
        core = str(r.get(f"{VIEW}.monday_core") or "").strip()
        if not cid or not name or not core:
            continue
        out.append(CoreCompany(company_id=cid, street_name=name, monday_core=core))

    log.info(
        "Omni returned %d core companies (Core=%d, Core US=%d, Core Europe=%d)",
        len(out),
        sum(1 for c in out if c.monday_core == "Core"),
        sum(1 for c in out if c.monday_core == "Core US"),
        sum(1 for c in out if c.monday_core == "Core Europe"),
    )
    return out
