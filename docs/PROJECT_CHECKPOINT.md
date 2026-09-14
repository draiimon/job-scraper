# Project Checkpoint

## Repository and purpose

- Repository: `https://github.com/draiimon/job-scraper`
- Branch: `main`
- Owner: `draiimon`

After Hours Job Hunter is a Discord-first monitor for recent entry-level Philippine and Remote PH technology jobs. It discovers, filters, scores, deduplicates, stores, and presents opportunities without making AI, Bright Data, or any single source a dependency for normal ATS polling.

## Current architecture

- FastAPI service with an in-process 900-second scheduler.
- PostgreSQL/Supabase persistence in deployment, SQLite fallback locally.
- Public Greenhouse, Lever, and Ashby adapters, with `config/job_sources.json` as the checked-in fallback.
- Optional Bright Data LinkedIn discovery and authenticated JobStreet browser discovery.
- Discord bot is the primary interface. A webhook is emergency alert fallback only and never competes with a healthy bot.
- Gemini is optional. It only edits a deterministic cover-letter draft after an application review is started.
- Docker/Render compatible: binds to `0.0.0.0`, honours `PORT`, and has no persistent-local-disk dependency for core monitoring.

## Discord-first operation

Commands:

- `v!search <role>` — targeted recent search, with immediate acknowledgement, typing/progress updates, query expansion, cache, cooldown, and no duplicate response.
- `v!latest` — newest qualifying stored matches.
- `v!viewall` — compact paginated Discord job board. It reads five stored rows at a time and never starts a source scan when changing pages.
- `v!status` — scheduler, source, database, Discord, LinkedIn, JobStreet, and AI health.
- `v!scan` — one protected immediate background scan.
- `v!resume` — private resume-management flow.
- `v!help` — command guide.

The persistent bot-authored control panel provides matching buttons for scan, search, latest, view-all, status, help, and resume upload where configured. Discord controls do not redirect to internal web pages. `VIEW JOB` is the only normal external job-card button and opens the original employer/ATS listing.

All user-facing embeds use the shared factory based on the verified reference message in channel `1346038098802249798`, message `1346058363556986963`: warm orange `#FF7F00`, compact hierarchy, mobile-readable fields, and footer `After Hours Job Hunter • Made by masoncalix`. The gallery preview uses these production renderers and safe no-ping/no-write mock data.

## Scheduler and sources

Startup performs one controlled scan. Automatic scans then run every 900 seconds. Manual scans run in the background, do not move the scheduled next-poll deadline, have a five-minute cooldown, and allow only one global scan. Independent ATS sources use bounded concurrency and per-source timeouts; a slow or failed source cannot block the batch.

`SOURCE_TARGETS_JSON`, when valid and non-empty, overrides `config/job_sources.json`. Blank values keep the checked-in fallback active. The service records source state and continues if an optional source fails.

LinkedIn is optional and is used only when Bright Data is enabled, authenticated, has the correct LinkedIn Jobs dataset/input configuration, and stays within the configured free-tier request limit. It is never required for ATS scanning or Discord search.

JobStreet is optional. Run `python -m src.jobstreet_auth` locally, complete Continue-with-Google, 2FA, consent, or CAPTCHA manually, and keep the resulting `data/private/jobstreet_session.json` private. For Render, place only a base64-encoded storage state in secret `JOBSTREET_SESSION_STATE_B64`; runtime materializes it privately. No password, cookies, session JSON, or token belongs in Git. Status is reported as `READY`, `AUTH REQUIRED`, or a source error based on available authenticated state and results.

## Freshness, relevance, and alerts

The pipeline prioritizes DevOps, cloud, infrastructure, systems, Linux, SRE, support, networking, software, QA, operations, analyst, cybersecurity, and other credible junior technology roles. It rejects unrelated accounting, HR, sales, marketing, generic VA, admin, and nontechnical customer-service work.

Freshness uses the actual source post date, not discovery time. Jobs from 0–3 days receive highest priority; 4–7 days normal priority; 8–14 days reduced priority; 15–30 days require a strong match; jobs older than 30 days are normally skipped. A stored job can alert again only with source evidence of a genuine repost/reactivation, such as a new posting timestamp or ID. Cross-source deduplication remains active.

Only a real new qualifying automatic alert can mention role `<@&1346328166100107366>`. The allowlist permits that role only. Search, status, help, previews, errors, scan updates, saves, and duplicate deliveries never ping a role, `@everyone`, or `@here`.

## Cover-letter and application flow

`APPLY NOW` loads the selected job and canonical resume, builds a deterministic truthful draft, optionally lets Gemini improve grammar, validates the final text, then opens an ephemeral Discord review. The same canonical final text supplies Discord preview, copyable TXT, and PDF. The letter has a complete contact header, professional paragraph spacing, simple natural English, and role-specific facts only.

Review actions include cover-letter preview/download, regenerate, use-template, resume preview, send confirmation, and cancel. Gemini rate limits, timeouts, invalid responses, or unavailable quota fall back immediately to the deterministic letter. Generated letters are cached per job. `APPLICATION_DRY_RUN=true` simulates sending and never contacts an employer; live sending remains deliberately unimplemented.

## Important configuration

Copy `.env.example`; never commit values. Important names include `DATABASE_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_CONTROL_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, `DISCORD_ALERT_ROLE_ID`, `POLLING_ENABLED`, `POLL_INTERVAL_SECONDS`, `SOURCE_TARGETS_JSON`, `SOURCE_CONFIG_PATH`, `APP_SECRET_KEY`, `APPLICATION_DRY_RUN`, Gemini variables, Bright Data variables, `JOBSTREET_SESSION_PATH`, and `JOBSTREET_SESSION_STATE_B64`.

## Verification and deployment

Run:

```bash
pytest -q
python -m compileall -q src
git diff --check
python -m src.preview_discord_ui
```

The preview posts safe mock UI states through the bot account and does not change the scheduler, create jobs/applications, contact employers, consume scan cooldown, or ping the job-alert role.

Render needs an external PostgreSQL/Supabase `DATABASE_URL`, bot token, control channel ID, and relevant optional integrations. Free Render instances may sleep, so they cannot guarantee uninterrupted 24/7 polling. Check public `/health` after deployment for scheduler and integration state.

## Known limitations

- The user must complete JobStreet/Google authentication manually and refresh it when it expires.
- LinkedIn and JobStreet availability depend on configured authorized accounts, datasets, and free-tier limits.
- Discord component clicks need live server-side verification after every deployment; unit tests and the gallery cover renderer and safety paths.
- Application sending is dry-run only by design.

## Latest verified state

- Tests: 69 passed (one third-party Python 3.12 `audioop` deprecation warning)
- Final release commit: current `main` HEAD (see `git log -1 --oneline`)
- Included finalization: Discord-first controls, JobStreet Render-session handling, visual gallery, date-sorted job table/export/timeline, role-alert safeguards, and targeted-search regression tests.
