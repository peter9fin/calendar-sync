#!/usr/bin/env bash
# Refresh the 9fin browser session and push the cookies to GitHub as a secret.
#
# Prereqs (local machine):
#   - venv active in repo root
#   - `gh` CLI installed (`brew install gh`) and authenticated (`gh auth login`)
#
# Run:  ./scripts/refresh_state.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Missing .env — create it from .env.example first."
  exit 1
fi

echo "Opening a visible Chromium — sign in to 9fin, then close the browser window when the dashboard is fully loaded."
python -m calendar_sync.setup_9fin_session

if [ ! -s state/9fin_state.json ]; then
  echo "state/9fin_state.json is empty — session save failed. Aborting."
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found. Install with 'brew install gh' and re-run, or manually paste the below into your GitHub repo → Settings → Secrets → NINEFIN_STATE_B64:"
  base64 -i state/9fin_state.json
  exit 0
fi

echo "Uploading NINEFIN_STATE_B64 to the GitHub repo…"
base64 -i state/9fin_state.json | gh secret set NINEFIN_STATE_B64 --body -

echo "Done. Session cookies pushed. The next scheduled run should have a fresh session."
