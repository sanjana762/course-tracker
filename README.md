# Competitor Course Tracker

Updates the Google Sheet and emails a summary when triggered. Use cron-job.org to schedule the GitHub Actions workflow every Monday at 10:00 AM IST.

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

## Schedule with cron-job.org
Create a fine-grained GitHub personal access token for this repository with **Actions: Read and write** permission. Add it to a cron-job.org job as an authorization header; do not put the token in the URL or request body.

Configure the job to run every Monday at 10:00 AM IST (or use the cron expression `30 4 * * 1` with the timezone set to UTC), using:

| Setting | Value |
|---|---|
| Request method | `POST` |
| URL | `https://api.github.com/repos/sanjana762/course-tracker/actions/workflows/course-tracker.yml/dispatches` |
| `Accept` header | `application/vnd.github+json` |
| `Authorization` header | Set this to the generated GitHub token using bearer authentication. |
| `X-GitHub-Api-Version` header | `2022-11-28` |
| Request body | `{"ref":"main"}` |

Keep the workflow enabled in the repository's Actions tab. The cron job triggers it through GitHub's workflow-dispatch API; you can still run it manually from Actions -> "Weekly Course Tracker" -> Run workflow.
