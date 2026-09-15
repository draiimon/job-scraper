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

- `v!search <role>` — targeted recent search, with immediate acknowledgement, typing/progress updates, query expansion, cache, cooldown, and no duplicate response. Its counts are a real funnel: recent PH jobs in the selected window → query-relevant tech jobs → entry-level-compatible jobs → qualifying matches. A cache hit says so explicitly. If no exact match is found, it offers up to three clearly labeled, separate 30-day stored alternatives without starting a second scan.
- `v!latest` — newest qualifying stored matches.
- `v!viewall` and `v!view all` — the same compact paginated Discord job board. It reads five stored rows at a time and never starts a source scan when changing pages.
- `v!status` — scheduler, source, database, Discord, LinkedIn, JobStreet, and AI health.
- `v!scan` — one protected immediate background scan.
- `v!resume` — owner-only resume-management flow. The short-lived upload link is sent by DM rather than posted in a server channel.
- `v!help` — command guide.

The persistent bot-authored control panel provides matching buttons for scan, search, latest, view-all, status, help, and JobStreet. Discord controls do not redirect to internal web pages. `VIEW JOB` is the only normal external job-card button and opens the original employer/ATS listing. Private application/resume controls are restricted to `DISCORD_OWNER_ID` when configured, otherwise to the Discord server owner.

All user-facing embeds use the shared factory based on the verified reference message in channel `1346038098802249798`, message `1346058363556986963`: warm orange `#FF7F00`, compact hierarchy, mobile-readable fields, and footer `After Hours Job Hunter • Made by masoncalix`. The gallery preview uses these production renderers and safe no-ping/no-write mock data.

## Scheduler and sources

Startup performs one controlled scan. Automatic scans then run every 900 seconds. Manual scans run in the background, do not move the scheduled next-poll deadline, have a five-minute cooldown, and allow only one global scan. Independent ATS sources use bounded concurrency and per-source timeouts; a slow or failed source cannot block the batch.

`SOURCE_TARGETS_JSON`, when valid and non-empty, overrides `config/job_sources.json`. Blank values keep the checked-in fallback active. The service records source state and continues if an optional source fails.

LinkedIn is optional and is used only when Bright Data is enabled, authenticated, has the correct LinkedIn Jobs dataset/input configuration, and stays within the configured free-tier request limit. It is never required for ATS scanning or Discord search.

JobStreet is optional and is linked from Discord with `v!jobstreet`. The
`CONNECT JOBSTREET` action creates a signed, short-lived, one-time private URL.
The setup page downloads a temporary Windows connector. Running it opens
installed Google Chrome with a separate temporary profile and keeps it open
while the user completes Google/JobStreet sign-in, 2FA, consent, or CAPTCHA
manually. Playwright attaches to that Chrome process over CDP and uploads only
the JobStreet storage state; the server encrypts it before storing it in
`source_connections`. `CHECK CONNECTION`, `REAUTHENTICATE`, and `DISCONNECT`
are available from the same control panel. Automatic scans and `v!search`
include JobStreet only while the stored connection is `READY`; public ATS
sources continue if it fails.

JobStreet runtime settings are initialized idempotently in `app_settings`,
including the enabled flag, search terms, and timeouts. There is no Browserless
dependency or Browserless secret. JobStreet session encryption is
deterministically derived with domain separation from `APP_SECRET_KEY`, which
must remain a deployment secret. There is no separate encryption-key
environment variable and no random key on restart. `JOBSTREET_SESSION_STATE_B64`
and `python -m src.jobstreet_auth` remain legacy fallbacks only.

## Freshness, relevance, and alerts

The pipeline prioritizes DevOps, cloud, infrastructure, systems, Linux, SRE, support, networking, software, QA, operations, analyst, cybersecurity, and other credible junior technology roles. It rejects unrelated accounting, HR, sales, marketing, generic VA, admin, and nontechnical customer-service work.

Freshness uses the actual source post date, not discovery time. Jobs from 0–1, 2–7, 8–14, 15–30, 31–60, and 61–90 days receive progressively lower priority; relevant active jobs remain eligible through day 90. Jobs older than 90 days are normally skipped. A stored job can alert again only with source evidence of a genuine repost/reactivation, such as a new posting timestamp or ID. Cross-source deduplication remains active.

Only a real new qualifying automatic alert can mention role `<@&1346328166100107366>`. The allowlist permits that role only. Search, status, help, previews, errors, scan updates, saves, and duplicate deliveries never ping a role, `@everyone`, or `@here`. The default automatic anti-spam limit is 10 alerts per cycle; valid overflow stays persisted instead of being discarded. A late successful alert can mark only `NEW`/`NOTIFIED` jobs as notified; it never overwrites a saved, skipped, applied, rejected, or other application decision.

## Cover-letter and application flow

`APPLY NOW` loads the selected job and canonical resume, builds a deterministic truthful draft, optionally lets Gemini improve grammar, validates the final text, then opens an ephemeral Discord review. The same canonical final text supplies Discord preview, copyable TXT, and PDF. The letter has a complete contact header, professional paragraph spacing, simple natural English, and role-specific facts only. Public GitHub/portfolio links live in `config/candidate_public_profile.json`; private resume facts remain database-backed or ignored locally.

Review actions include cover-letter preview/download, regenerate, use-template, resume preview, send confirmation, and cancel. Every generated result records `AI_REVISED`, `DETERMINISTIC_FALLBACK`, or `DETERMINISTIC_ONLY` plus a safe failure reason when applicable. Regenerate has a per-job lock, generation version, prior-body hashes, a fresh prompt version, and a similarity guard; a date-only change is rejected. If Gemini is unavailable, the Discord review says that a template fallback was used and immediately previews the active saved version. Gemini rate limits, timeouts, invalid responses, or unavailable quota fall back immediately to the deterministic letter. Generated letters are cached per job. `APPLICATION_DRY_RUN=true` simulates sending once per job and never contacts an employer; live sending remains deliberately unimplemented.

## Important configuration

Copy `.env.example`; never commit values. Important bootstrap names include
`DATABASE_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_CONTROL_CHANNEL_ID`, `DISCORD_OWNER_ID`,
`DISCORD_WEBHOOK_URL`, `DISCORD_ALERT_ROLE_ID`, `POLLING_ENABLED`,
`POLL_INTERVAL_SECONDS`, `SOURCE_TARGETS_JSON`, `SOURCE_CONFIG_PATH`,
`APP_SECRET_KEY`, `PUBLIC_BASE_URL`, `APPLICATION_DRY_RUN`, Gemini variables,
Bright Data variables, and the
legacy `JOBSTREET_SESSION_PATH` / `JOBSTREET_SESSION_STATE_B64` fallback.
Managed JobStreet settings live in `app_settings`.

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
- Application-review and paginated-job buttons are intentionally short-lived Discord views. If one expires after a restart or timeout, use `v!viewall` or the current job card to open a fresh review; expired controls never reuse a different job ID.
- Discord component clicks need live server-side verification after every deployment; unit tests and the gallery cover renderer and safety paths.
- Application sending is dry-run only by design.

## Latest verified state

- Tests: 99 passed (one third-party Python 3.12 `audioop` deprecation warning)
- Live startup scan: 27 sources loaded, 27 attempted, 27 successful, 5,101 raw/normalized jobs, 4,318 within 0–90 days, 242 computer-related, 165 entry-compatible, and 865 duplicates removed.
- Live database compatibility: `app_state.value` is `TEXT` in the deployment database; scheduler counters persisted and read back after restart without data loss. The SQLite legacy-schema migration is idempotent and preserves payloads over 500 characters.
- Direct Playwright Chromium is installed in the Render image with its Linux
  dependencies. JobStreet scans restore the encrypted session into private
  ephemeral storage, run headless Chromium with container-safe flags, refresh
  the stored state, and remove the temporary file after the scan.
- Live targeted searches: software engineer returned 2 qualifying results, developer 2, DevOps 1, and IT Support 5. Query relevance was counted before profile scoring; software-family titles were returned before infrastructure-only suggestions. A LinkedIn failure was isolated and reported without blocking ATS results.
- Live stored job board: `v!viewall` and `v!view all` both use the stored paginated board; the deployment database contained 26 active 0–90 day stored matches during verification.
- Final release commit: current `main` HEAD (see `git log -1 --oneline`)
- Included finalization: Discord-first controls, database/Vault-backed
  JobStreet local connector linking, direct Render Chromium scanning, visual gallery, date-sorted job table/export/timeline,
  role-alert safeguards, and targeted-search regression tests.
