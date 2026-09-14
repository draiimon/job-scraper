# After Hours Job Hunter

After Hours Job Hunter is a Discord-first Philippine technology job monitor and application assistant. It finds recent PH or Remote PH roles, filters and scores them against the active profile, stores results persistently, and keeps search, review, saving, skipping, and dry-run application preparation inside Discord.

## Current architecture

- FastAPI API and in-process scheduler
- PostgreSQL-compatible persistence through SQLAlchemy and `psycopg`
- SQLite fallback for local development
- Greenhouse, Lever, and Ashby public-board adapters; the active deployment currently loads 26 targets through `SOURCE_TARGETS_JSON`
- Optional Bright Data LinkedIn discovery and JobStreet adapters
- Discord bot as the primary UI
- Discord webhook as emergency notification fallback only; the bot owns the control panel when healthy
- Deterministic local scoring and filtering; Gemini is optional and never required for monitoring
- Docker and Render deployment support

The default polling interval is 15 minutes. Startup performs one controlled scan, then the scheduler runs every 900 seconds. A source failure or Discord outage is isolated from the rest of the scan, and stored jobs can be retried for delivery.

## Discord

Configure `DISCORD_BOT_TOKEN` for the primary bot experience. The bot supports:

```text
v!search <role>
v!latest
v!viewall
v!status
v!scan
v!resume
v!help
```

The persistent control panel provides `SCAN NOW`, `SEARCH JOBS`, `VIEW LATEST JOBS`, `VIEW ALL JOBS`, `VIEW STATUS`, `HELP`, and `UPLOAD RESUME` when signed links are configured. `v!viewall` provides a compact, paginated Discord job board. Job cards provide `VIEW JOB`, `APPLY NOW`, `SAVE`, and `SKIP`.

`APPLY NOW` opens an internal review flow. It can prepare a truthful cover letter and show the configured resume before an explicit send confirmation. Live sending is not implemented; dry-run mode is enabled by default. Only `VIEW JOB` and the employer's application URL open external pages.

The bot uses a dark Discord embed style with an orange accent, selective Unicode headings, compact fields, and the footer `After Hours Job Hunter • Made by masoncalix`. The persistent view is restored on bot startup and uses stable component IDs. Slow button and modal actions acknowledge first, then perform database, search, resume, or cover-letter work asynchronously.

If the bot is not configured or temporarily unavailable, set `DISCORD_WEBHOOK_URL` for fallback notifications. Do not configure the webhook as a competing primary UI when the bot is healthy.

## Resume-backed applications

The active resume is stored in the database as one private record containing the PDF bytes, filename, upload time, and extracted text. Cover-letter drafts use that extracted text as their factual source, so the application flow can reference real experience and projects instead of an untracked local file. Gemini, when enabled, receives redacted resume context and is instructed not to add claims.

Open `GET /resume` for the current resume status and a short-lived signed upload link. The link accepts PDF files up to 10 MB. Uploading a replacement updates the active record, clears cached cover letters, and includes the new PDF in newly generated application packages. Discord users can type `v!resume` or use `UPLOAD RESUME`.

## Local setup

```bash
cp .env.example .env
python -m pip install -r requirements.txt
python -m pytest -q
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`. `GET /health` reports database, scheduler, source, Discord, Bright Data, and AI state. Replit's configured workflow uses port 5000; Render and Docker use the runtime `PORT` value.

Useful endpoints:

- `GET /` — service landing page
- `GET /health` — health and integration status
- `GET /resume` — active resume status and signed upload link
- `POST /resume/{token}` — replace the active PDF resume using a signed link
- `GET /jobs` and `GET /latest` — internal/debugging data endpoints
- `GET /jobs/table` — date-sorted browser table for stored jobs
- `GET /jobs/export.csv` — current stored-job export
- `GET /jobs/{id}/timeline` — stored status-event timeline
- `POST /find` — intentionally disabled; search is available in Discord
- `POST /control/scan/{token}` — intentionally disabled; scans are available in Discord
- `PATCH /jobs/{id}/status` — update job status
- `POST /fixtures/smoke` — exercise persistence and notification logic safely

The browser job UI routes `/search`, `/status`, `/latest-page`, `/help`, and the browser scan controls return `410` by design. They are not part of the user flow. `/health`, `/resume`, and internal data endpoints remain for hosting, administration, and debugging.

For a persistent local container:

```bash
docker compose up --build
```

## Sources

When `SOURCE_TARGETS_JSON` is blank or empty, the service loads [config/job_sources.json](config/job_sources.json). A valid, non-empty `SOURCE_TARGETS_JSON` overrides that file. The checked-in fallback contains six public boards; the current deployment override supplies approximately 26 active targets. Empty environment values do not replace typed defaults.

```json
[
  {"kind":"greenhouse","name":"Acme","token":"acme"},
  {"kind":"lever","name":"Example","site":"example"},
  {"kind":"ashby","name":"Company","board":"Company"}
]
```

Validate all configured public boards without writing jobs:

```bash
python -m src.tools.validate_sources
```

The service does not bypass bot protections or scrape unsupported commercial boards. Add an official feed or API through a source adapter instead.

## Filtering and freshness

The scanner keeps recent active Philippine or Remote PH technology roles, rejects unrelated Accounting, Finance, HR, Recruiting, Sales, Marketing, Admin, generic VA, and nontechnical customer-service listings, and strongly penalizes senior, lead, staff, principal, architect, manager, and multi-year roles for this profile. A job discovered today is not automatically a job posted today. Reposts require source evidence such as a new timestamp or job ID.

## Optional Bright Data and AI features

Bright Data is disabled unless both `BRIGHTDATA_ENABLED=true` and `BRIGHTDATA_API_TOKEN` are configured. LinkedIn manual searches are user-initiated and capped by `BRIGHTDATA_LINKEDIN_MONTHLY_REQUEST_LIMIT`; JobStreet page loads have their own monthly safety cap.

### JobStreet Google session

JobStreet discovery can use a private Playwright browser session authenticated with **Continue with Google**:

```bash
python -m src.jobstreet_auth
```

The command opens a visible browser, navigates to JobStreet, starts Google sign-in when the button is available, and waits for the user to complete Google authentication, 2FA, security prompts, consent, or CAPTCHA manually. It never receives or stores a Google password, automates 2FA, bypasses CAPTCHA, or prints cookies/tokens. The resulting Playwright storage state is saved at `data/private/jobstreet_session.json`, which is gitignored.

The saved session is reused for JobStreet listing discovery only. For an ephemeral Render instance, encode the resulting Playwright storage-state JSON as `JOBSTREET_SESSION_STATE_B64` in Render's secret environment; it is materialized only at runtime with private file permissions. Do not put the value in Git or logs. Listings enter the normal freshness, technical-role, seniority, scoring, deduplication, and Discord pipeline. JobStreet applications are never submitted automatically. If the session expires, the source reports `AUTH REQUIRED`; run the setup command and authenticate with Google again.

AI is disabled by default. When enabled for application review, Gemini only revises a deterministic draft, validates the response for common fabricated claims, and falls back to the deterministic letter on any failure or rate limit. It is not part of normal monitoring.

`data/master-profile.json` is private, gitignored, and remains an optional fallback for tailoring when no database resume is present. The database resume is the primary application context. Cover letters use deterministic truthful drafting, optional Gemini revision, factual validation, and a deterministic fallback when Gemini fails. Do not store contact details, credentials, OAuth material, or provider keys in source control.

## Render

`render.yaml` deploys one web service with the in-process scheduler and binds to Render's `PORT`. Use an external Supabase or PostgreSQL `DATABASE_URL`; the application automatically uses the bundled `psycopg` v3 driver for standard `postgresql://` URLs. Render free instances may sleep, so they are not a guaranteed 24/7 monitoring host.

## Configuration

Copy `.env.example` and provide only the integrations you intend to use. Important settings include:

- `DATABASE_URL`
- `DISCORD_BOT_TOKEN` and optionally `DISCORD_CONTROL_CHANNEL_ID`
- `DISCORD_WEBHOOK_URL` as fallback
- `APP_SECRET_KEY` and `PUBLIC_BASE_URL` for signed internal action links
- `POLLING_ENABLED` and `POLL_INTERVAL_SECONDS`
- `PROFILE_PATH` for the optional JSON fallback profile; the active PDF resume is managed through `/resume` or `v!resume` (`RESUME_PATH` remains available for legacy Discord viewing)
- Bright Data and Gemini variables when those optional features are enabled
- JobStreet Google-session variables when browser discovery is enabled; no Google password belongs in `.env`

Never print or commit secrets. Run the full test suite before deploying:

```bash
python -m pytest -q
```

## Environment variable names

Values belong in the deployment secret/environment manager, not in Git. Supported names are:

`DATABASE_URL`, `DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_BOT_GUILD_ID`, `DISCORD_CONTROL_CHANNEL_ID`, `DISCORD_MOTIVATION`, `DISCORD_MOTIVATIONS_JSON`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `AUTO_SEND_EMAIL_APPLICATIONS`, `MIN_NOTIFY_SCORE`, `MIN_AUTO_APPLICATION_SCORE`, `MAX_NOTIFICATIONS_PER_CYCLE`, `MANUAL_SCAN_COOLDOWN_SECONDS`, `SCAN_SOURCE_CONCURRENCY`, `SCAN_SOURCE_TIMEOUT_SECONDS`, `POLLING_ENABLED`, `APP_TIMEZONE`, `APP_SECRET_KEY`, `PUBLIC_BASE_URL`, `COVER_LETTER_MODE`, `SOURCE_TARGETS_JSON`, `SOURCE_CONFIG_PATH`, `POLL_INTERVAL_SECONDS`, `LOG_LEVEL`, `HOST`, `PORT`, `PROFILE_PATH`, `RESUME_PATH`, `APPLICATION_DRY_RUN`, `AI_ENABLED`, `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, `AI_DAILY_REQUEST_LIMIT`, `AI_MIN_JOB_SCORE`, `GEMINI_API_KEY`, `GEMINI_API_KEYS`, `GEMINI_API_KEY2`, `GEMINI_API_KEY3`, `GEMINI_API_KEY4`, `GEMINI_API_KEY5`, `GEMINI_API_KEY6`, `GEMINI_API_KEY7`, `GEMINI_API_KEY8`, `GEMINI_API_KEY9`, `GEMINI_KEY_PROJECTS_JSON`, `GEMINI_MODEL`, `GEMINI_MAX_RETRIES`, `GEMINI_CONCURRENCY_LIMIT`, `BRIGHTDATA_ENABLED`, `BRIGHTDATA_API_TOKEN`, `BRIGHTDATA_LINKEDIN_JOBS_DATASET_ID`, `BRIGHTDATA_JOBSTREET_DATASET_ID`, `BRIGHTDATA_LINKEDIN_JOBS_INPUTS_JSON`, `BRIGHTDATA_JOBSTREET_INPUTS_JSON`, `BRIGHTDATA_JOBSTREET_MONTHLY_PAGE_LIMIT`, `BRIGHTDATA_LINKEDIN_MONTHLY_REQUEST_LIMIT`, `JOBSTREET_SESSION_PATH`, `JOBSTREET_BASE_URL`, `JOBSTREET_LOGIN_URL`, `JOBSTREET_LOCATION`, `JOBSTREET_SEARCH_TERMS_JSON`, `JOBSTREET_MAX_RESULTS`, `JOBSTREET_AUTH_TIMEOUT_SECONDS`, and `JOBSTREET_SCAN_TIMEOUT_SECONDS`.

## Troubleshooting

- If Discord is not configured after adding or reconnecting secrets, restart the running workflow so the process receives the updated environment.
- Check `/health` for `discord_bot`, scheduler, database, source, LinkedIn, JobStreet, and AI state.
- `discord_bot_ready` in the application log confirms gateway readiness and persistent-view restoration.
- A full scan runs in the background and offloads synchronous persistence work so it does not block Discord heartbeats.
- If a Discord task exits unexpectedly, the application logs `discord_bot_task_failed` while FastAPI and the scheduler remain alive.
- `DISCORD_WEBHOOK_URL` is fallback delivery only. It must not create a competing control panel when the bot is healthy.
- If JobStreet shows `AUTH REQUIRED`, run `python -m src.jobstreet_auth` and complete Google sign-in manually. Never add Google credentials to `.env`.

## Replit and Render

Replit is used for development and verification. The existing workflow runs `uvicorn src.main:app --host 0.0.0.0 --port 5000`. Production remains compatible with the included Dockerfile and `render.yaml`: bind to `0.0.0.0`, use the runtime `PORT`, and provide an external PostgreSQL/Supabase `DATABASE_URL`. Render instances may sleep, so continuous monitoring requires an always-on host.

## Security and privacy

Do not commit `.env` files, bot or webhook credentials, database credentials, Bright Data tokens, Gemini keys, Google OAuth secrets, `APP_SECRET_KEY`, private resumes, or generated private application documents. The active resume is database-backed and should be replaced only through the signed resume flow. Keep dry-run enabled until live sending is intentionally implemented and reviewed.

## Repository and verification

Repository: https://github.com/draiimon/job-scraper
Branch: `main`

The standard verification commands are:

```bash
python -m pytest -q
python -m compileall -q src
git diff --check
```
