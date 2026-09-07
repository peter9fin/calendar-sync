# GitHub Actions setup — one-time

The daily calendar-sync job now runs on GitHub Actions (not your Mac). This guide walks through wiring it up.

## 1. Merge the v3 files into your local repo

From Terminal:

```bash
STAGE="/Users/petermegarity/Library/Application Support/Claude/local-agent-mode-sessions/a278bf7c-20ad-46f0-b1dd-661eff466505/afa739f3-d8d7-47f9-98a4-1ead341aff56/local_312e540e-5ff5-4db7-bb3b-d0c9dfee488d/outputs/calendar-sync-v3"
REPO="$HOME/Claude/repos/calendar-sync"
mkdir -p "$REPO/.github/workflows" "$REPO/scripts" "$REPO/calendar_sync/render"
cp "$STAGE/.github/workflows/daily.yml" "$REPO/.github/workflows/"
cp "$STAGE/scripts/refresh_state.sh" "$REPO/scripts/"
cp "$STAGE/calendar_sync/render/gaps_json.py" "$REPO/calendar_sync/render/"
chmod +x "$REPO/scripts/refresh_state.sh"
```

## 2. Push the repo to GitHub

The repo must be **public** so the artifact can fetch `gaps.json` from `raw.githubusercontent.com`. All secrets stay encrypted inside GitHub Actions Secrets — no credentials are ever in the code.

Create a new repo at https://github.com/new (name it `calendar-sync`, mark **Public**, don't init README/gitignore/license).

Then push:

```bash
cd ~/Claude/repos/calendar-sync
git init 2>/dev/null || true
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/calendar-sync.git
git push -u origin main
```

## 3. Add secrets to the GitHub repo

Go to `https://github.com/YOUR_USERNAME/calendar-sync/settings/secrets/actions` and add each of these as a **Repository secret**:

| Name                 | Value                                                                    |
|----------------------|--------------------------------------------------------------------------|
| `OMNI_API_KEY`       | your Omni API key                                                        |
| `OMNI_MODEL_ID`      | `ddcb9fa8-453d-44ad-bd12-5e02a366a51b` (Data Warehouse Prod)             |
| `MONDAY_API_TOKEN`   | your Monday.com personal access token                                    |
| `SLACK_BOT_TOKEN`    | Slack bot token starting `xoxb-...` (needs `chat:write` + `users:read.email`) |
| `SLACK_TARGET_EMAIL` | `peter.megarity@9fin.com`                                                |
| `NINEFIN_EMAIL`      | your 9fin login email                                                    |
| `NINEFIN_PASSWORD`   | your 9fin login password                                                 |
| `NINEFIN_STATE_B64`  | (populated automatically by step 4 below — leave empty for now)          |

## 4. One-time: save your 9fin session cookies to a secret

The GitHub Actions runner is a fresh container each time — it can't log in interactively. Instead you log in once locally, capture the cookies, and upload them as a secret. The workflow decodes them at run time.

First install the GitHub CLI (once):

```bash
brew install gh
gh auth login    # follow the prompts
```

Then run the helper (from repo root):

```bash
cd ~/Claude/repos/calendar-sync
source .venv/bin/activate     # your local venv
./scripts/refresh_state.sh
```

That opens a visible Chromium, waits for you to sign in to 9fin, then uploads the resulting `state/9fin_state.json` as base64 to the `NINEFIN_STATE_B64` GitHub secret.

**Repeat this step whenever the daily workflow fails with "9fin session invalid"** (typically every 30–90 days, whenever 9fin's cookies expire).

## 5. Test-run the workflow manually

Go to `https://github.com/YOUR_USERNAME/calendar-sync/actions/workflows/daily.yml` and click **Run workflow** (top-right). Set `dry_run: true` and `limit: 8` for the first test. Wait ~3–5 min.

If it succeeds:
- Green tick appears on the run
- `dashboards/gaps.json` is committed to `main` — check with `git pull`
- No Slack DM (dry_run skipped it)

If it fails, download the "run-log" artifact from the run page to see logs.

## 6. Live run (Slacks Peter + updates the artifact)

Same page, **Run workflow**, leave both inputs blank. Full 1,000+ core companies, ~20 min. Slack DM lands when done. `dashboards/gaps.json` is refreshed.

## 7. Wire the artifact to fetch live data

Once step 6 works and `gaps.json` is in the repo, the artifact fetches from:

```
https://raw.githubusercontent.com/YOUR_USERNAME/calendar-sync/main/dashboards/gaps.json
```

Tell me your GitHub username and I'll update the Cowork artifact to fetch from that URL on every open.

## 8. It's now running daily

Workflow fires at **05:00 UTC weekdays** (06:00 UK during BST / 05:00 UK during GMT). To change the schedule, edit `.github/workflows/daily.yml` and adjust the `cron:` line.

## Rolling out to other associates

Each associate:
1. Opens the Cowork artifact URL (bookmark it)
2. Their `Mark checked` state stays in their own browser (localStorage)

For shared check-state across the team, we'd add step 9 later (writing check-state back to a Monday board via MCP), but v1 is per-viewer.

## Troubleshooting

- **Workflow says "9fin session invalid"** — re-run `./scripts/refresh_state.sh` locally.
- **"Missing required env var"** — one of the GitHub secrets is empty. Recheck step 3.
- **Slack DM not landing** — check the Slack bot token has `chat:write` and the bot has been added to a DM with Peter. Enable log detail: temporarily set `LOG_LEVEL: "DEBUG"` in the workflow.
- **Gaps look wrong / lots of scrape failures** — download the run log artifact, check `logs/last_run.md` for the per-company breakdown.
