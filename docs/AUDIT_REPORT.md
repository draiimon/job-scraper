# Job Discovery Application Audit

Audit date: 2026-09-21
Repository: `job-scraper`
Auditor: Codex

This report records the repository state and a controlled read-only inspection
before the hardening work. No production data was changed by the audit.

## Executive summary

The application is a FastAPI service that also starts an in-process polling
worker and, when configured, a Discord gateway supervisor. SQLAlchemy persists
jobs, source health, scheduler state, application state, resumes, and private
application events. The existing system is a useful prototype with working
Greenhouse, Lever, Ashby, Bright Data, JobSpy, and authorized JobStreet paths,
but it is not yet a reliable 24/7 discovery engine.

The largest root causes are:

1. Sources are manually configured ATS boards. There is no source registry or
   automatic source discovery.
2. The canonical model is too small for the requested lifecycle and analytics:
   it lacks first/last discovery fields, explicit expiry, structured salary and
   experience fields, duplicate linkage, and notification delivery metadata.
3. Greenhouse, Lever, and Ashby adapters perform one request each and do not
   implement provider-level pagination or schema-tolerant extraction.
4. Matching is a single heuristic score. It does not represent the requested
   role tiers, full technology profile, entry-level signals, structured
   experience parsing, or transparent score dimensions.
5. Notifications are single-job alerts with a per-cycle cap. There is no bulk
   digest, durable failed-delivery queue, attempt counter, backoff state, or
   delivery lease recovery.
6. The worker is coupled to the web process, uses only an in-process lock, has
   no jitter or durable scheduler lease, and an unexpected source result can
   still abort an entire cycle before the worker-level recovery loop.
7. `/health` can report `status: ok` while the scheduler or notifications are
   stale; it does not expose readiness/liveness semantics or meaningful worker
   freshness thresholds.
8. Configuration and documentation drift: several documented environment
   variables are ignored by `Settings`, while active settings such as
   `JOBSTREET_ENABLED` and `JOBSTREET_MAX_PAGES` are missing from `.env.example`.

## Evidence from the current repository

### Controlled tests and static checks

- `python -m pytest -q`: **115 passed**, one third-party `audioop`
  deprecation warning.
- `python -m compileall -q src`: passed.
- `git diff --check`: passed.
- Existing tests are primarily unit/flow tests. They do not prove a live
  provider run, source discovery, durable scheduler locking, notification
  batching, or restart recovery.

### Live configured database snapshot

The configured database was queried without mutating it:

- Jobs: **382** total; **335** non-expired.
- Source-health rows: **30**; latest scheduled cycle loaded **27** sources.
- Source runs: **22,816**.
- Latest persisted cycle: **1,291** raw/normalized rows, **820** within the
  0–90-day window, **83** PH/remote-compatible, **23** technical, **7**
  entry-level-compatible, **8** qualifying, **8** duplicate removals, **0** new
  database records, and **0** alerts sent.
- Latest cycle source result: **20/27 successful**.
- Persisted job notification states included **332 PENDING**, **49 SENT**, and
  **1 BOT_SENDING**. There are no attempt count, last-attempt, or error-message
  columns.
- Job status distribution included **290 NEW**, **44 NOTIFIED**, **1 SAVED**,
  and **47 EXPIRED**.
- The two JobSpy providers were persisted as failed: Google was rate-limited;
  Indeed repeatedly timed out. Several large ATS boards also timed out.

No secrets or credential values are included in this report.

## Subsystem assessment

| Subsystem | Status | Findings |
|---|---|---|
| Application architecture | PARTIAL | FastAPI, an in-process worker, Discord supervisor, SQLAlchemy repository, and source pipeline are connected. Web and worker lifecycle are coupled. |
| Database initialization | PARTIAL | `create_all` plus ad-hoc widening migrations work for known columns. There is no versioned migration system, migration locking, or complete schema migration for the requested fields. |
| Persistence | PARTIAL | Jobs and source health persist, but lifecycle and notification metadata are incomplete. Save/dedupe logic is not safe enough for all cross-source variants. |
| Scheduler | PARTIAL/BROKEN FOR 24/7 | Polling starts from FastAPI lifespan when enabled, but there is no durable worker lease, jitter, or multi-process scheduler lock. Render sleeping prevents a guarantee of 24/7 operation. |
| Worker isolation | PARTIAL | `Pipeline.run_source` isolates many source errors, but `asyncio.gather` in `poll_once` does not request `return_exceptions=True`; an unexpected source-level error can abort aggregation. |
| Greenhouse | PARTIAL | Public adapter exists and works for configured boards, but retrieval is one request and has no complete-pagination contract or registry integration. |
| Lever | PARTIAL | Public postings adapter exists and works for some boards, but no pagination/health registry/discovery. |
| Ashby | PARTIAL | Public job-board adapter exists and filters listed jobs, but no pagination/secondary-location/employment/compensation normalization contract. |
| JobSpy | DEGRADED | Indeed and Google are enabled by default, but live health shows repeated timeout/rate-limit failures. The adapter is a third-party wrapper rather than a durable public-feed integration. |
| Bright Data/LinkedIn | DISABLED | Deliberately off unless paid credentials, datasets, and inputs are configured. It is optional and not a base dependency. |
| JobStreet | CONDITIONAL | Authorized browser/session flow exists. It requires manual authentication and is not a general unattended public source. |
| Automatic source discovery | UNUSED/MISSING | No source/company registry or discovery worker exists. `config/job_sources.json` and `SOURCE_TARGETS_JSON` are manual board lists. |
| Normalization | PARTIAL | `NormalizedJob` is provider-neutral enough for current sources, but lacks the requested canonical fields and provenance lifecycle. |
| Deduplication | PARTIAL | URL/source-ID/description signals exist, but there is no duplicate record/link table, content hash, or durable cross-source duplicate relation. |
| Matching/scoring | PARTIAL | Deterministic and explainable at a basic level, but role families, skills, experience, seniority, recency, and location scoring do not match the requested profile. |
| Recency/active state | PARTIAL | Provider date and `last_seen_at` exist, with a 90-day filter. `first_seen_at`, `updated_at`, `expires_at`, explicit active verification, and requested labels are missing. |
| Instant notifications | PARTIAL | New high-score jobs can be sent, but there is no durable outbound queue and notification state is not enough to recover every failure. |
| Bulk digest | BROKEN/MISSING | No scheduled digest mode; per-cycle notification cap can suppress valid matching jobs. |
| Notification reliability | BROKEN/PARTIAL | Webhook failures can retry, but there is no exponential delivery queue/attempt metadata, bot-pending lease recovery, or batch-level delivery record. |
| Health/readiness | PARTIAL | `/health` checks the database and exposes snapshots, but always returns `status: ok` when DB access works even if the worker is stale or most sources fail. |
| Observability | PARTIAL | Source run and health rows exist, but logs are plain text, source/company/duration/new/duplicate fields are inconsistent, and no aggregate metrics endpoint exists. |
| Admin/status UI | PARTIAL | Discord status and a public read-only jobs table exist. There is no source registry dashboard or filters covering provider/company/technology/remote. |
| Configuration | BROKEN/PARTIAL | `.env.example`, `Settings`, README, Render, and runtime settings have drift. Required/optional/production validation is not explicit. |
| Deployment | PARTIAL/RISKY | Render starts a single web process that also owns polling and Discord. It is not a separate worker, has no process-level scheduler lease, and free web instances can sleep. |
| Security | PARTIAL | Signed private actions and owner checks exist, but public mutation fixtures and unauthenticated data/status endpoints need hardening and externally fetched URLs need stricter validation. |
| Tests | PARTIAL | The current suite is green and valuable, but lacks provider contract fixtures, pagination, discovery, bulk delivery, retry recovery, migration concurrency, and controlled end-to-end coverage. |

## Configuration drift found

The following `.env.example` names are present but not modeled by
`src/config.py`: `GEMINI_API_KEY`, `GEMINI_API_KEYS`, `GEMINI_API_KEY2` through
`GEMINI_API_KEY9`, `GOOGLE_REDIRECT_URI`, `HOST`, `LOG_LEVEL`, and `PORT`.

The following modeled runtime settings are missing from `.env.example`:
`JOBSTREET_ENABLED` and `JOBSTREET_MAX_PAGES`.

`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` are
documented as if Gmail/OAuth were available, but the current application does
not implement an active email-application delivery path. Automatic application
sending remains intentionally disabled and must stay that way.

## Hardening sequence

The repair work will proceed in this order:

1. Add durable source/company registry and safe bounded discovery primitives.
2. Expand the canonical schema and idempotent migration path without losing
   existing jobs or application state.
3. Repair provider adapters, pagination, normalization, timeouts, and source
   isolation.
4. Replace the profile heuristic with transparent tier/technology/
   experience/location/recency scoring and preserve low-priority browsing.
5. Add durable notification queue fields, instant alerts, bulk digest batching,
   retries, leases, and restart-safe delivery.
6. Add durable scheduler locking/jitter and meaningful liveness/readiness and
   source metrics.
7. Harden public routes and configuration validation.
8. Add fixtures, integration tests, migration/restart tests, and a controlled
   live run. Live counts will be reported only from observed results.

## Known constraints

- LinkedIn and JobStreet remain optional/authorized paths; no CAPTCHA bypass,
  proxy evasion, login scraping, or access-control circumvention will be added.
- A truly 24/7 guarantee requires a hosting plan/process that does not sleep.
  The application can be made restart-safe and observable, but a sleeping free
  web instance cannot poll while suspended.
- Automatic discovery can safely discover public ATS/career sources from
  bounded allowlisted seeds and known public ATS patterns; it cannot promise
  every company on the internet without becoming an uncontrolled crawler.

## Post-repair status (2026-09-22)

The findings above intentionally preserve the pre-repair evidence. The
following is the verified state after remediation.

| Subsystem | Status | Post-repair evidence |
|---|---|---|
| Architecture | WORKING | FastAPI and a dedicated worker have separate production entrypoints. The always-on worker owns polling and the Discord gateway. |
| Database/migration | WORKING | The configured PostgreSQL database completed the additive migration with all canonical fields and registry tables present. A second migration completed idempotently. Existing job counts remained intact. |
| Scheduler | WORKING | Durable database lease, heartbeat, 30-second renewal during idle time, jitter, source concurrency bounds, and a standalone worker are implemented. |
| Greenhouse | WORKING | Complete-page loop, provider-ID dedupe, schema-tolerant normalization, retries, timeout, and fixture/live verification. |
| Lever | WORKING | List and cursor shapes, hosted/apply URLs, categories, retries, and fixture/live verification. |
| Ashby | WORKING | Listed jobs, secondary locations, employment, compensation, and fixture/live verification. |
| SmartRecruiters | WORKING | Public offset pagination and canonical normalization are implemented and fixture-tested. |
| JobSpy | WORKING/DEGRADED BY PROVIDER | Indeed PH and Google were isolated and both completed in the controlled live run; their upstream rate limits can still degrade individual cycles. |
| Automatic discovery | WORKING | Durable company/source registry, provenance, bounded public search, HTTPS seed inspection, and promotion of official ATS links from broad job results. |
| Normalization | WORKING | Canonical fields cover company/domain, requirements, country/remote type, structured salary/experience, category, lifecycle timestamps, hashes, active/duplicate state, score, and explanation. |
| Deduplication | WORKING | Source IDs, canonical URLs, company/title/location identity, description similarity, content/source hashes, and cross-source provenance are used. Concurrent ingestion is tested. |
| Matching/scoring | WORKING | Four role tiers, the supplied technology profile, entry signals, 1-2/3-4/5+ experience behavior, PH locations, seniority context, education, and recency are deterministic and tested. |
| Notifications | WORKING | Score-80 instant queue, score-60 digest, 8/10-safe batching, atomic delivery claims, exponential backoff, rate-limit delay, max attempts, stuck-claim recovery, and sent-only-after-success state. |
| Discord View All pagination | WORKING | NEXT/PREVIOUS edit the interaction response directly, preserve filters, clamp page bounds, and have a Page 2 regression test. |
| Health/observability | WORKING | Liveness, readiness, worker freshness, source health, registry, job counters, notification counters, JSON logs, and secret-field redaction are present. |
| Admin/status view | WORKING | Discord system/source status plus a read-only job table with role, technology, location, remote, score/date, provider, company, and status filters. |
| Configuration | WORKING | Every `Settings` field is represented in `.env.example`; typed bounds fail fast. Missing notifications report `DISABLED`. |
| Deployment | WORKING WITH PAID WORKER | Render has API and always-on worker services. The worker uses `0.5c-512mb`; a free-only deployment is intentionally not represented as 24/7. |
| Security | WORKING | Sensitive settings are excluded from repr, JSON logs redact secret-like fields, fixtures/status mutation are token-protected, raw private metadata is absent from public job responses, and application preparation is signed-flow only. |
| Tests | WORKING | Provider pagination, discovery/SSRF guards, canonical scoring, retries, digest batching, dedupe, concurrency, leases, malformed isolation, persistence restart, and complete source-to-notify flow are covered. |

## Controlled live verification

The full run used a temporary SQLite database, real public source responses,
and disabled production notifications. It did not touch the configured
production job rows and did not submit applications.

- Sources discovered in the discovery phase: **15**
- Sources in the registry after configured/discovered merge: **42**
- Sources enabled/attempted (including JobSpy): **43**
- Sources healthy: **37**
- Jobs fetched: **6,071**
- New canonical Philippine-compatible matches: **66**
- Duplicates removed: **3**
- Entry-level-compatible/stretch matches stored: **66**
- Score 80 or higher: **1**
- Records persisted after repository restart: **66**
- Notification failures: **0**

The six isolated failing seeds were Greenhouse GitLab, Cloudflare, and Zapier,
and Lever Netlify, Snyk, and Retool. Their failures did not stop 37 healthy
sources. They remain in the registry with reduced health for later recovery or
operator review.

A second explicit controlled run sent **1** no-ping webhook verification
message successfully with **0** failures. Automated tests separately prove a
12-job digest is delivered in batches of 8 and 4, failed instant delivery is
retried and becomes `SENT`, and a restart does not reclaim a sent alert.

The live run returned 20 real matching jobs in its report because at least 20
were available. The highest observed match was **81/100, Junior QA Tester
(Caloocan Onsite), Teligent Systems**, discovered through Indeed Philippines.
