# Calendar Sync — 9fin gap check

Daily QA layer that catches results releases and results conference calls appearing on a core company's IR page but missing from 9fin's platform. Runs headlessly on your Mac via `launchd`, Slacks Peter with any gaps found.

## Architecture

```
Omni  ─────►  core company list (Company ID + Street Name)
Monday ────►  IR URL per company (NMD News Team Calendar Tracking board)
9fin  ─────►  what's already on 9fin's calendar (POST /api/v1.0/calendar/download)
IR page ──►   what's actually announced (Playwright + regex extraction)
              │
              ▼
           diff logic
              │
              ▼
          Slack DM
```

No Cowork involvement at runtime. Fully headless once configured.

## Prereqs

- macOS
- Python 3.10+ (`python3 --version` to verify)
- ~500MB free disk (for Playwright's Chromium)

## Move this repo into place

Currently staged in Cowork's outputs folder. Move it:

```bash
mv "/Users/petermegarity/Library/Application Support/Claude/local-agent-mode-sessions/*/local_*/outputs/calendar-sync" ~/Claude/repos/calendar-sync
cd ~/Claude/repos/calendar-sync
git init
```

## Setup

```bash
cd ~/Claude/repos/calendar-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
# Fill in .env with the tokens listed below
```

## `.env` — required values

| Variable | Where to get it |
|---|---|
| `OMNI_API_KEY` | Omni Analytics → your profile → API keys → Create new key |
| `OMNI_MODEL_ID` | Already known: `ddcb9fa8-453d-44ad-bd12-5e02a366a51b` (Data Warehouse Prod) |
| `MONDAY_API_TOKEN` | Monday.com → your avatar → Developers → My Access Tokens |
| `NINEFIN_EMAIL` | Your 9fin login email |
| `NINEFIN_PASSWORD` | Your 9fin login password |
| `SLACK_BOT_TOKEN` | Slack app → OAuth & Permissions → Bot User OAuth Token (`xoxb-...`). Bot needs `chat:write` + `users:read.email` scopes. |
| `SLACK_TARGET_EMAIL` | `peter.megarity@9fin.com` — the account to DM |

Optional (defaults shown in `.env.example`):
- `NMD_BOARD_ID=5099036324` — Monday board with IR URLs
- `LOOKAHEAD_DAYS=90` — forward calendar window
- `IR_SCRAPE_CONCURRENCY=4` — parallel IR page fetches
- `IR_SCRAPE_TIMEOUT_S=15` — per-page load timeout
- `LOG_LEVEL=INFO`

## First run — validation

**Step 1.** One-time interactive login to save the 9fin session:

```bash
python3 -m calendar_sync.setup_9fin_session
```

Launches a visible Chromium window; you sign in (including any 2FA); the script saves cookies + storage to `state/9fin_state.json`. Subsequent runs reuse this state and run headlessly. Re-run this if the session expires (Slack will alert you).

**Step 2.** Dry-run the full pipeline on a small subset:

```bash
python3 -m calendar_sync.main --limit 8 --dry-run
```

`--limit 8` runs against 8 core companies (matches the mini trial size).
`--dry-run` skips Slack and prints the report to stdout.

**Step 3.** Full run once you're happy:

```bash
python3 -m calendar_sync.main
```

## Scheduled daily run

Copy the launchd plist into place and load it:

```bash
cp launchd/com.petermegarity.calendar-sync.plist ~/Library/LaunchAgents/
# Edit the plist to fix the ProgramArguments path if your repo isn't at ~/Claude/repos/calendar-sync
launchctl load ~/Library/LaunchAgents/com.petermegarity.calendar-sync.plist
```

That fires the pipeline at 06:00 local (UK) every weekday. Logs go to `logs/last_run.log`.

To disable: `launchctl unload ~/Library/LaunchAgents/com.petermegarity.calendar-sync.plist`.

## Layout

```
calendar-sync/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── calendar_sync/
│   ├── __init__.py
│   ├── config.py                 # env loading
│   ├── main.py                   # entry point
│   ├── setup_9fin_session.py    # one-time interactive login
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── omni_client.py        # core company list
│   │   ├── monday_client.py      # IR URLs from NMD board
│   │   ├── ninefin_client.py     # 9fin calendar via Playwright
│   │   └── ir_scraper.py         # IR page scraping via Playwright
│   ├── logic/
│   │   ├── __init__.py
│   │   ├── normalise.py          # fiscal period / event kind normalisation
│   │   └── diff.py               # gap detection
│   └── notify/
│       ├── __init__.py
│       └── slack.py              # Slack DM
├── launchd/
│   └── com.petermegarity.calendar-sync.plist
├── state/                        # session state (gitignored)
├── data/                         # dedup state (gitignored)
└── logs/                         # run logs (gitignored)
```

## Troubleshooting

**"9fin session expired" in the Slack digest** — re-run `python3 -m calendar_sync.setup_9fin_session`.

**Chromium not found** — run `python -m playwright install chromium` from the venv.

**Slack DM says "no gaps found" but you expected some** — check `logs/last_run.log`. Some IR pages don't parse cleanly; those show up as "Needs manual check" in the digest, not as gaps.

**Many companies failing to scrape** — often means the IR page URL changed. Update the Monday NMD board and re-run.
