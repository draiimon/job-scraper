# After Hours Job Hunter

A Discord-first, 24/7 job discovery and notification service for Philippine entry-level technology roles. It discovers public sources, normalizes and scores jobs, persists canonical records, suppresses duplicates, retries notifications, and never submits applications automatically.

## Architecture

- FastAPI web/API service with `/live`, `/ready`, `/health`, and `/metrics`.
- Dedicated production worker for polling, source discovery, cleanup, Discord delivery, and the Discord gateway.
- PostgreSQL/Supabase in production; SQLite for local development and tests.
- Public Greenhouse, Lever, Ashby, and SmartRecruiters adapters with pagination and normalized output.
- Public Indeed Philippines and Google Jobs discovery through JobSpy. JobStreet links returned by Google Jobs are labeled `jobstreet:google-index`; the authenticated JobStreet session is never replayed for automated scraping.
- Bounded automatic source discovery through public search, allowlisted HTTPS career-page seeds, and official ATS links found in job results.
- Deterministic scoring. Gemini is optional and never required to ingest, score, store, or notify.
- Durable worker/gateway leases, provider isolation, source health, scheduler heartbeats, structured JSON logs, and notification retry state.

The normal polling interval is 15 minutes. Source discovery runs every six hours by default. One failing provider, malformed posting, AI outage, or Discord outage does not stop collection from other sources.

## Job flow

```text
discover source -> fetch -> normalize -> active/recency/location checks
-> deterministic score -> cross-source dedupe -> store/update
-> instant alert (80+) and scheduled digest (60+) -> durable delivery state
```

All valid matches are stored. Discord messages are batched to platform limits; no top-five truncation is applied. Jobs below the notification threshold remain browsable.

## Discord

Commands:

```text
v!search <role>
v!latest
v!viewall
v!view all
v!status
v!scan
v!jobstreet
v!resume
v!help
```

The control panel includes search, latest jobs, all jobs, status, JobStreet, scan, and help. `VIEW ALL JOBS` is paginated from stored records. `NEXT` and `PREVIOUS` edit the original interaction response and preserve filters.

Only real alerts may mention the allowlisted role `1345727357662658603`. Preview, test, status, search, digest, and error messages do not ping roles. The obsolete role ID is not present in production code or documentation.

`APPLY NOW` prepares a review; it never submits an application. Resume upload and application actions use short-lived signed links and owner-only Discord controls. CAPTCHA, login, 2FA, and consent are always completed by the user.

## Local setup

PowerShell:

```powershell
Copy-Item .env.example .env
python -m pip install -r requirements.txt
python -m pytest -q
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Bash:

```bash
cp .env.example .env
python -m pip install -r requirements.txt
python -m pytest -q
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

With `POLLING_ENABLED=true`, the local web process also runs the scheduler and Discord gateway. Open:

- `GET /live`: process liveness only.
- `GET /ready`: database plus worker readiness; returns 503 for a stale required worker.
- `GET /health`: database, worker heartbeat, scheduler, notifications, source health, registry, and AI state.
- `GET /metrics`: jobs found in 1h/24h, matches, delivery failures, and source counts.
- `GET /jobs/table`: read-only, paginated dashboard with role, technology, location, remote, provider, company, score, date, and status filters.
- `GET /jobs/export.csv`: read-only job export.

Raw metadata, generated cover letters, resume data, private application email addresses, and secrets are not returned from public job JSON endpoints. Resume management and application generation are available only through signed Discord flows.

Docker runs the combined local process:

```bash
docker compose up --build
```

## Production startup

The checked-in Render Blueprint defines two services:

1. `ph-job-agent` web service:

   ```text
   uvicorn src.main:app --host 0.0.0.0 --port $PORT
   POLLING_ENABLED=false
   ```

2. `ph-job-agent-worker` always-on background worker:

   ```text
   python -m src.worker
   POLLING_ENABLED=true
   ```

The worker owns polling and the Discord gateway. The database lease prevents duplicate schedulers/gateway consumers. Both services must receive the same `DATABASE_URL`, Discord values, `APP_SECRET_KEY`, `PUBLIC_BASE_URL`, source configuration, and optional integration values.

Render does not offer free background workers; the Blueprint explicitly uses `0.5c-512mb`. This is intentional because a sleeping free web service cannot guarantee 24/7 discovery. See Render's current [compute-plan documentation](https://render.com/docs/compute-plans) and [background-worker documentation](https://render.com/docs/background-workers).

For a non-Render host, run these as separate supervised processes:

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
python -m src.worker
```

Set `POLLING_ENABLED=false` on the API and `true` on the worker.

## Sources and automatic discovery

If `SOURCE_TARGETS_JSON` is blank, `config/job_sources.json` supplies bootstrap sources. These are only seeds, not the full registry. Every discovery cycle also:

- runs a bounded set of public search queries for supported ATS links;
- inspects configured HTTPS career pages once for allowlisted ATS links;
- promotes official Greenhouse, Lever, Ashby, and SmartRecruiters URLs found in broader job results;
- stores the company, board identifier, discovery method, timestamps, errors, and health score;
- keeps failing sources with reduced health instead of deleting them immediately.

The crawler is bounded and allowlisted. It does not crawl recursively, bypass CAPTCHA, rotate proxies, scrape authenticated pages, or directly scrape LinkedIn/JobStreet.

Run controlled live verification without touching the production database or sending notifications:

```bash
python -m scripts.live_verify
```

To explicitly send one no-ping webhook test message:

```bash
python -m scripts.live_verify --max-sources 1 --skip-jobspy --send-test-notification
```

## Scoring

The deterministic 100-point model covers:

- entry-level signals: up to 30;
- profile technology overlap: up to 25;
- role/title tier: up to 20;
- Philippines/Remote-PH location: up to 10;
- recency: up to 10;
- compatible education language: up to 5.

One to two years is not rejected. Three to four years receives a penalty. Explicit 5+ years and senior/lead/principal/staff/manager/director roles are normally excluded. “Architect” is interpreted in title context rather than penalized wherever the word appears.

Scores are categorized as excellent (90+), strong (80-89), good (70-79), stretch (60-69), and low priority (below 60). Instant alerts use `INSTANT_ALERT_SCORE=80`; digests use `MIN_NOTIFY_SCORE=60`.

## Required configuration

Production requires:

- `DATABASE_URL`: persistent PostgreSQL/Supabase URL, identical on API and worker.
- At least one of `DISCORD_BOT_TOKEN` or `DISCORD_WEBHOOK_URL` for notifications. Without either, health reports notifications as `DISABLED`; collection still runs.
- `DISCORD_CONTROL_CHANNEL_ID` when the bot cannot infer the desired channel.

Required for signed private resume/application links:

- `APP_SECRET_KEY`
- `PUBLIC_BASE_URL`

Recommended:

- `DISCORD_OWNER_ID`
- `DISCORD_ALERT_ROLE_ID=1345727357662658603`
- `ADMIN_API_TOKEN` if the protected status mutation API is used.

Optional integrations are explicitly disabled when their credentials are missing. `BRIGHTDATA_ENABLED=false` by default. JobStreet requires a user-authorized encrypted browser session. Gemini remains optional.

Every supported variable, grouped by subsystem with safe defaults, is documented in `.env.example`. Startup uses typed validation for score ranges, concurrency, polling intervals, retry counts, and timeouts. Never commit `.env`, database credentials, Discord credentials, resumes, browser storage state, or generated application packages.

## Database and migrations

Startup creates new tables and applies idempotent widening/additive migrations for existing databases. Canonical jobs track provider IDs, company/domain, requirements, location/country/remote type, salary, experience, category, posting/first-seen/last-seen/update/expiry timestamps, hashes, duplicate state, match details, and durable notification attempts.

Runtime indexes cover source IDs, identity, posting/activity/score, notification due time, source health, and source runs. A controlled verification recreates the repository against the same SQLite file to prove persistence after restart.

## Verification

```bash
python -m pytest -q
python -m compileall -q src scripts
python -m pip check
git diff --check
python -m scripts.live_verify
```

The audit and repair evidence is in `docs/AUDIT_REPORT.md`.

## Security notes

- Externally fetched titles, descriptions, and URLs are treated as data and never executed.
- Discovery accepts HTTPS only, rejects literal private/loopback/link-local IPs, and follows only supported ATS links.
- Logs are JSON and redact fields whose names indicate tokens, secrets, passwords, authorization, API keys, database URLs, or webhooks.
- Test/fixture routes are disabled unless both `ENABLE_FIXTURE_ROUTES=true` and a matching `ADMIN_API_TOKEN` are supplied.
- Notification state is claimed atomically, retried with bounded exponential backoff, recovered after a crash, and marked sent only after delivery succeeds.
- Automatic application submission is not implemented.
