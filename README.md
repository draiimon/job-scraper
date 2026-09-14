# After Hours Job Hunter

Automated Philippine technology job monitoring and application assistance for entry-level and junior roles.

The service polls public ATS boards, filters for Philippine or Remote PH opportunities, scores roles against a technical profile, removes duplicates across sources, stores accepted listings in PostgreSQL or SQLite, and delivers interactive alerts through Discord.

## Current architecture

- FastAPI API and in-process scheduler
- PostgreSQL-compatible persistence through SQLAlchemy and `psycopg`
- SQLite fallback for local development
- 26 configured Greenhouse, Lever, and Ashby public boards
- Optional Bright Data LinkedIn discovery and JobStreet adapters
- Discord bot as the primary UI
- Discord webhook as an emergency delivery and control-panel fallback
- Deterministic local scoring and filtering; Gemini is optional and never required for monitoring
- Docker and Render deployment support

The default polling interval is 15 minutes. A source failure or Discord outage is isolated from the rest of the scan, and stored jobs can be retried for delivery.

## Discord

Configure `DISCORD_BOT_TOKEN` for the primary bot experience. The bot supports:

```text
v!search <role>
v!latest
v!status
v!scan
v!help
```

The persistent control panel provides `SCAN NOW`, `SEARCH JOBS`, `VIEW LATEST JOBS`, `VIEW STATUS`, and `HELP`. Job cards provide `VIEW JOB`, `APPLY NOW`, `SAVE`, and `SKIP`.

`APPLY NOW` opens an internal review flow. It can prepare a truthful cover letter and show the configured resume before an explicit send confirmation. Live sending is not implemented; dry-run mode is enabled by default. Only `VIEW JOB` and the employer's application URL open external pages.

If the bot is not configured or temporarily unavailable, set `DISCORD_WEBHOOK_URL` for fallback notifications. Do not configure the webhook as a competing primary UI when the bot is healthy.

## Local setup

```bash
cp .env.example .env
python -m pip install -r requirements.txt
python -m pytest -q
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`. `GET /health` reports database, scheduler, source, Discord, Bright Data, and AI state.

Useful endpoints:

- `GET /` — service landing page
- `GET /health` — health and integration status
- `GET /jobs` and `GET /latest` — stored qualifying jobs
- `POST /find` — targeted Bright Data search when configured
- `POST /control/scan/{token}` — signed manual scan link
- `PATCH /jobs/{id}/status` — update job status
- `POST /fixtures/smoke` — exercise persistence and notification logic safely

For a persistent local container:

```bash
docker compose up --build
```

## Sources

When `SOURCE_TARGETS_JSON` is blank, the service loads [config/job_sources.json](config/job_sources.json). Deployment configuration can override it with a JSON array:

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

## Optional Bright Data and AI features

Bright Data is disabled unless both `BRIGHTDATA_ENABLED=true` and `BRIGHTDATA_API_TOKEN` are configured. LinkedIn manual searches are user-initiated and capped by `BRIGHTDATA_LINKEDIN_MONTHLY_REQUEST_LIMIT`; JobStreet page loads have their own monthly safety cap.

AI is disabled by default. When enabled for application review, Gemini only revises a deterministic draft, validates the response for common fabricated claims, and falls back to the deterministic letter on any failure or rate limit. It is not part of normal monitoring.

`data/master-profile.json` is private, gitignored, and used for truthful profile/project tailoring. Mount it in production rather than committing it. Do not store contact details, credentials, OAuth material, or provider keys in source control.

## Render

`render.yaml` deploys one web service with the in-process scheduler and binds to Render's `PORT`. Use an external Supabase or PostgreSQL `DATABASE_URL`; the application automatically uses the bundled `psycopg` v3 driver for standard `postgresql://` URLs. Render free instances may sleep, so they are not a guaranteed 24/7 monitoring host.

## Configuration

Copy `.env.example` and provide only the integrations you intend to use. Important settings include:

- `DATABASE_URL`
- `DISCORD_BOT_TOKEN` and optionally `DISCORD_CONTROL_CHANNEL_ID`
- `DISCORD_WEBHOOK_URL` as fallback
- `APP_SECRET_KEY` and `PUBLIC_BASE_URL` for signed internal action links
- `POLLING_ENABLED` and `POLL_INTERVAL_SECONDS`
- `PROFILE_PATH` and `RESUME_PATH` for private application materials
- Bright Data and Gemini variables when those optional features are enabled

Never print or commit secrets. Run the full test suite before deploying:

```bash
python -m pytest -q
```