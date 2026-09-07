"""Slack notifier — DMs Peter with the gap report."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 3500


@dataclass
class GapRow:
    company: str
    date: str
    title: str
    ir_url: str
    ninefin_link: Optional[str] = None
    context: str = ""  # raw text snippet from the IR page (for debug / verification)


@dataclass
class FailureRow:
    company: str
    ir_url: str
    reason: str


def _lookup_user_id(client: WebClient, email: str) -> str:
    try:
        resp = client.users_lookupByEmail(email=email)
        return resp["user"]["id"]
    except SlackApiError as exc:
        raise RuntimeError(f"Slack lookup failed for {email}: {exc.response.get('error')}")


def _row_company(r) -> str:
    if isinstance(r, dict):
        return str(r.get("name") or r.get("company") or "").strip()
    return getattr(r, "company", "") or getattr(r, "name", "")


def _row_ir_url(r) -> str:
    if isinstance(r, dict):
        return str(r.get("ir_url") or r.get("ir") or "").strip()
    return getattr(r, "ir_url", "") or ""


def _row_dates(r) -> List[str]:
    """List of ISO date strings this row wants filed on 9fin."""
    if isinstance(r, dict):
        return [str(d) for d in (r.get("to_file") or [])]
    dates = getattr(r, "to_file", None) or []
    return [str(d) for d in dates]


def _row_reason(r) -> str:
    if isinstance(r, dict):
        return str(r.get("reason") or r.get("status") or "urlbroken")
    return getattr(r, "reason", None) or getattr(r, "status", "urlbroken")


def format_message(
    run_date: str,
    total_scanned: int,
    gaps: List,
    failures: List,
) -> str:
    lines = [
        "*Daily calendar-sync gap check*",
        f"Run date: {run_date}",
        f"Companies scanned: {total_scanned}",
        f"Gaps found: {len(gaps)}",
        f"Scrape failures: {len(failures)}",
        "",
    ]

    if gaps:
        lines.append("*Gaps* (IR page has, 9fin doesn't):")
        for g in gaps[:20]:
            company = _row_company(g)
            ir_url = _row_ir_url(g)
            dates = _row_dates(g)
            if dates:
                lines.append(f"• {company} — {', '.join(dates)}")
            else:
                lines.append(f"• {company}")
            if ir_url:
                lines.append(f"  {ir_url}")
        if len(gaps) > 20:
            lines.append(f"...and {len(gaps) - 20} more (see logs/last_run.log)")
    else:
        lines.append("_No gaps found today._")

    if failures:
        lines.append("")
        lines.append("*Needs manual check* (page didn't parse):")
        for f in failures[:15]:
            company = _row_company(f)
            ir_url = _row_ir_url(f)
            reason = _row_reason(f)
            lines.append(f"• {company} — {reason}")
            if ir_url:
                lines.append(f"  {ir_url}")
        if len(failures) > 15:
            lines.append(f"...and {len(failures) - 15} more failures (see logs/last_run.log)")

    body = "\n".join(lines)
    if len(body) > MAX_MESSAGE_CHARS:
        body = body[: MAX_MESSAGE_CHARS - 30] + "\n...(truncated, see logs)"
    return body


def send_report(cfg, run_date, gaps: List, failures: List, *, total: int) -> None:
    """DM the run summary to ``cfg.slack_target_email``.

    ``gaps`` and ``failures`` may be dicts (main.py row shape) or GapRow /
    FailureRow objects — both are supported by the formatter.
    """
    bot_token = cfg.slack_bot_token
    target_email = cfg.slack_target_email
    client = WebClient(token=bot_token)
    user_id = _lookup_user_id(client, target_email)
    text = format_message(str(run_date), total, list(gaps), list(failures))
    log.info(
        "Slacking %s (%d chars, %d gaps, %d failures)",
        target_email, len(text), len(gaps), len(failures),
    )
    try:
        client.chat_postMessage(channel=user_id, text=text, mrkdwn=True)
    except SlackApiError as exc:
        raise RuntimeError(f"Slack send failed: {exc.response.get('error')}")


def send_abort(cfg, reason: str) -> None:
    """Short-circuit notifier when the pipeline can't even start."""
    bot_token = cfg.slack_bot_token
    target_email = cfg.slack_target_email
    client = WebClient(token=bot_token)
    try:
        user_id = _lookup_user_id(client, target_email)
        client.chat_postMessage(
            channel=user_id,
            text=f":warning: *Calendar sync aborted* — {reason}",
            mrkdwn=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send abort Slack: %s", exc)
