# Project Checkpoint

## Repository

GitHub: https://github.com/draiimon/job-scraper  
Branch: `main`  
Owner: `draiimon`

## Project goal

After Hours Job Hunter monitors recent Philippine and Remote PH technology roles, filters them against the active profile, stores results persistently, and provides job search and application preparation through Discord.

## Current architecture

- FastAPI application and in-process scheduler
- Discord bot as the primary UI
- Discord webhook as emergency notification fallback
- SQLAlchemy persistence using PostgreSQL/Supabase in deployment or SQLite locally
- Greenhouse, Lever, and Ashby ATS adapters
- Optional Bright Data LinkedIn adapter and manual Google-authenticated JobStreet browser discovery
- Optional Gemini cover-letter polishing with deterministic fallback
- Docker and Render-compatible process configuration

## Discord UX

Commands:

- `v!search <role>`
- `v!latest`
- `v!status`
- `v!scan`
- `v!resume`
- `v!help`

The persistent bot-authored control panel provides `SCAN NOW`, `SEARCH JOBS`, `VIEW LATEST JOBS`, `VIEW STATUS`, `HELP`, and optional `UPLOAD RESUME`. Job cards provide `VIEW JOB`, `APPLY NOW`, `SAVE`, and `SKIP`. Application review provides `VIEW COVER LETTER`, `REGENERATE`, `USE TEMPLATE`, `VIEW RESUME`, `SEND APPLICATION`, and `CANCEL`, followed by a second confirmation.

The embed style is dark Discord UI with an orange accent, compact fields, selective Unicode headings, and the footer `After Hours Job Hunter • Made by masoncalix`. Rotating motivational headlines appear only on real job alerts.

Slow button and modal callbacks acknowledge immediately and move synchronous database/file work to `asyncio.to_thread`. The persistent view uses `timeout=None`, stable custom IDs, and is restored in `on_ready`.

## Scheduler

- One controlled startup scan
- Automatic scans every 900 seconds
- `v!scan`/`SCAN NOW` runs one extra background scan
- Manual scans preserve the existing automatic `next_poll_at`
- One global scan at a time
- Five-minute manual cooldown
- Source concurrency and per-source timeouts
- Synchronous scan persistence is offloaded so Discord heartbeats remain responsive

## Job sources

If `SOURCE_TARGETS_JSON` is valid and non-empty, it overrides `config/job_sources.json`. Empty values fall back to the checked-in JSON file. The checked-in fallback currently contains six public boards; the active verified deployment loaded 26 targets through its valid override. Invalid or empty source configuration fails explicitly instead of silently producing zero sources.

LinkedIn is disabled unless Bright Data is enabled, authenticated, supplied with valid inputs, and configured with the LinkedIn dataset. JobStreet discovery uses a manually authenticated Playwright Google session at `data/private/jobstreet_session.json`; it reports `AUTH REQUIRED` when no valid saved session is available. It never stores Google passwords, automates 2FA, bypasses CAPTCHA, or submits applications.

## Filtering rules

The scanner prioritizes DevOps, Cloud, Infrastructure, Platform, Systems, Linux, SRE, Cloud Operations, IT Support, Technical Support, Service Desk, NOC, Networking, Software Engineering, QA, Application Support, IT Operations, Systems Analyst, and Cybersecurity roles. It rejects unrelated Accounting, Finance, HR, Recruiting, Sales, Marketing, Admin, generic VA, and nontechnical customer-service listings.

Freshness is based on the source posting timestamp, not discovery time. Older listings are not presented as newly posted. Strong seniority signals such as Senior, Lead, Staff, Principal, Architect, Manager, and four-plus years are penalized or suppressed for this profile.

## Application workflow

`APPLY NOW` loads the stored job and canonical resume context, creates a deterministic truthful draft, optionally asks Gemini to revise it, validates the result, and presents the application review inside Discord. Gemini failures fall back to the deterministic draft. No resume facts, skills, employers, dates, achievements, or metrics are invented.

`APPLICATION_DRY_RUN=true` prevents employer contact. Live sending is not implemented, employer email addresses are never guessed, and duplicate applications are blocked.

## Environment variables

Use `.env.example` as the name reference. Store values only in the deployment environment or secret manager. Important names include `DATABASE_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_CONTROL_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, `POLLING_ENABLED`, `POLL_INTERVAL_SECONDS`, `SOURCE_TARGETS_JSON`, `SOURCE_CONFIG_PATH`, `APPLICATION_DRY_RUN`, `APP_SECRET_KEY`, `PUBLIC_BASE_URL`, Bright Data variables, Gemini variables, and Google OAuth variables. No values belong in this checkpoint.

## Important files

- `src/main.py` — FastAPI app lifespan, scheduler, scan lifecycle, health endpoint, and Discord task supervision
- `src/discord_bot.py` — Discord bot, persistent control panel, commands, views, modals, job cards, and application review
- `src/services.py` — repository, persistence, source processing, notifications, deduplication, and fallback delivery
- `src/config.py` — settings, empty-environment handling, source fallback, motivations, and optional integrations
- `src/manual_search.py` — targeted Discord search flow
- `src/jobs.py` — normalization, freshness, location, scoring, and filtering
- `src/applications.py` — deterministic cover letters, Gemini revision, validation, and application packages
- `src/brightdata.py` — optional Bright Data LinkedIn/JobStreet adapters
- `src/jobstreet.py` — manual Google-session setup and authenticated JobStreet discovery source
- `src/jobstreet_auth.py` — `python -m src.jobstreet_auth` interactive setup command
- `config/job_sources.json` — checked-in public ATS fallback targets
- `tests/test_pipeline.py` — regression and behavior tests
- `Dockerfile` and `render.yaml` — production-compatible process configuration

## Current verified state

- Current commit: `f0085c07e87ca592f39cb8a88497c3e134618ac5`
- Tests: 35 passed
- Scheduler: running at 900 seconds
- Discord: gateway connected and `discord_bot_ready` observed
- Database: connected
- Active source count: 26
- Source health after full scan: 26 working
- Jobs checked in the verified full scan: 5,103
- LinkedIn: disabled in the current runtime
- JobStreet: Google-session discovery is ready only after manual authentication; otherwise `AUTH REQUIRED`
- Browser job UI: disabled for normal user flow; Discord is primary
- Render compatibility: preserved through `Dockerfile`, `render.yaml`, `0.0.0.0`, and runtime `PORT`

## Known limitations / remaining work

Real user-side Discord button and modal clicks were not available from this environment, so Task #1 remains the live end-to-end interaction verification step. The bot gateway, heartbeat responsiveness during a full scan, callback acknowledgment paths, and application health were verified locally.

## Security notes

No secrets are documented in this file. Do not commit `.env`, Discord credentials, database credentials, Bright Data tokens, Gemini keys, Google OAuth secrets, `APP_SECRET_KEY`, private resumes, or generated private application documents. A webhook URL must remain in the secret manager and should be rotated separately if it was ever exposed.

## Rules for future agents

- Discord bot stays primary; webhook is fallback notification only.
- Do not recreate a user-facing web job dashboard.
- Preserve the automatic 900-second scheduler.
- Manual scans must not reset the automatic deadline.
- Keep source fallback behavior and explicit configuration failures.
- Do not fabricate resume or job facts.
- Keep freshness and seniority filters intact.
- Do not add paid scraping fallbacks automatically.
- Preserve the `draiimon/job-scraper` repository and `main` branch.
- Preserve Render compatibility.