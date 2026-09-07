"""One-time interactive login to save the 9fin session state.

Launches a visible Chromium window. User signs in (including any 2FA). Script waits
until the user lands on /calendar (indicates a successful auth), then saves cookies
+ localStorage to state/9fin_state.json. Subsequent headless runs reuse this file.
"""
from __future__ import annotations

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from calendar_sync.config import Config, configure_logging

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    cfg = Config.load()
    state_path = cfg.state_dir / "9fin_state.json"

    log.info("Launching Chromium (visible). Please sign in.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{cfg.ninefin_base_url}/dashboard")

        log.info("Waiting for you to reach the calendar page (URL contains /calendar or /dashboard after login)...")
        # Wait up to 5 min for the user to complete login and reach a signed-in page.
        page.wait_for_url("**/dashboard**", timeout=300_000)

        # Give any post-login redirects a moment to settle. The dashboard polls
        # continuously so networkidle may never fire — treat the timeout as a
        # soft signal and save cookies anyway.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            log.info("networkidle didn't settle within 10s; saving session anyway.")

        context.storage_state(path=str(state_path))
        browser.close()

    log.info("Session saved to %s", state_path)


if __name__ == "__main__":
    main()
