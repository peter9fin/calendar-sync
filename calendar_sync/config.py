"""Environment / config loading."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _url(name: str, default: str) -> str:
    """Read a URL-typed env var. Strips whitespace, falls back to default when
    empty, and defensively prepends https:// if the caller supplied a bare host
    (a common GitHub-secrets footgun that manifests as
    ``Invalid URL '.../api': No scheme supplied``).
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raw = default
    if not raw:
        raise RuntimeError(f"Missing required URL env var: {name}")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    return raw.rstrip("/")


@dataclass(frozen=True)
class Config:
    # Omni
    omni_api_key: str
    omni_base_url: str
    omni_model_id: str

    # Monday
    monday_api_token: str
    nmd_board_id: int

    # 9fin
    ninefin_email: str
    ninefin_password: str
    ninefin_base_url: str

    # Slack
    slack_bot_token: str
    slack_target_email: str

    # Runtime
    lookahead_days: int
    ir_scrape_concurrency: int
    ir_scrape_timeout_s: int

    # Paths
    repo_root: Path
    state_dir: Path
    data_dir: Path
    logs_dir: Path

    @classmethod
    def load(cls) -> "Config":
        state_dir = REPO_ROOT / "state"
        data_dir = REPO_ROOT / "data"
        logs_dir = REPO_ROOT / "logs"
        state_dir.mkdir(exist_ok=True)
        data_dir.mkdir(exist_ok=True)
        logs_dir.mkdir(exist_ok=True)

        return cls(
            omni_api_key=_required("OMNI_API_KEY"),
            omni_base_url=_url("OMNI_BASE_URL", "https://9fin.omniapp.co"),
            omni_model_id=_required("OMNI_MODEL_ID"),
            monday_api_token=_required("MONDAY_API_TOKEN"),
            nmd_board_id=_int("NMD_BOARD_ID", 5099036324),
            ninefin_email=_required("NINEFIN_EMAIL"),
            ninefin_password=_required("NINEFIN_PASSWORD"),
            ninefin_base_url=_url("NINEFIN_BASE_URL", "https://app.9fin.com"),
            slack_bot_token=_required("SLACK_BOT_TOKEN"),
            slack_target_email=_required("SLACK_TARGET_EMAIL"),
            lookahead_days=_int("LOOKAHEAD_DAYS", 90),
            ir_scrape_concurrency=_int("IR_SCRAPE_CONCURRENCY", 8),
            ir_scrape_timeout_s=_int("IR_SCRAPE_TIMEOUT_S", 15),
            repo_root=REPO_ROOT,
            state_dir=state_dir,
            data_dir=data_dir,
            logs_dir=logs_dir,
        )


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
