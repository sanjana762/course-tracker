# Competitor Course Tracker

Runs automatically every Monday at 10:00 AM IST via GitHub Actions, updates the Google Sheet and emails a summary.

## One-time setup
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | full contents of the service-account .json file |
| `EMAIL_USER` | sender Gmail address |
| `EMAIL_APP_PASSWORD` | Gmail App Password (myaccount.google.com/apppasswords) |
| `EMAIL_TO` | recipient(s), comma-separated |
| `FIRECRAWL_API_KEY` | optional |

Share the Google Sheet (Editor) with the service account's email.

## Test
Actions tab -> "Weekly Course Tracker" -> Run workflow.
