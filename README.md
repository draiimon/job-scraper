# Philippine Job Agent

Self-hosted, deterministic job monitor for entry-level Philippine DevOps, cloud, infrastructure, platform and systems roles. It persists every listing before notification, so webhook outages never lose jobs.

## Cost model

The core is free-first: SQLite or any standard PostgreSQL (including Supabase/Neon free tiers), public Greenhouse/Lever/Ashby endpoints, normal HTTP requests, local deterministic scoring, and Discord webhooks. It has no paid API, proxy, scraping service, or LLM dependency. Standard Supabase `postgresql://...` URLs are automatically routed through the bundled `psycopg` v3 driver; no Supabase-specific feature is used.

AI is disabled by default and is not implemented in the monitoring path. The `AI_*` configuration fields reserve an optional provider-neutral extension point, with a default request cap. Any future provider must use deterministic prefiltering, fingerprint caching, daily limits, and a no-AI fallback.

## Start

```powershell
Copy-Item .env.example .env
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn src.main:app --reload
```

Open `http://localhost:8000/docs`; `GET /health` confirms database, source and integration state. Use `docker compose up --build` for a persistent container. Set `DISCORD_WEBHOOK_URL` to enable delivery.

## Render

`render.yaml` deploys one web service: its in-process scheduler runs alongside the API and binds to Render's `PORT`. Use the external Supabase/PostgreSQL `DATABASE_URL`; no persistent local disk is required. Render free instances can sleep when inactive, so they are not a true 24/7 monitoring option.

## Sources

Set `SOURCE_TARGETS_JSON` to a JSON array of public ATS targets, for example:

```json
[{"kind":"greenhouse","name":"Acme","token":"acme"},{"kind":"lever","name":"Example","site":"example"},{"kind":"ashby","name":"Company","board":"Company"}]
```

When `SOURCE_TARGETS_JSON` is blank, the app loads the verified public boards in [config/job_sources.json](config/job_sources.json). Environment JSON overrides that file for deployment. Validate them with `python -m src.tools.validate_sources`; safely test one configured Discord webhook with `python -m src.tools.test_discord`.

The service intentionally does not bypass bot protections. Unsupported commercial boards are represented as disabled adapters and reported in source health; add official feeds/API endpoints through an adapter. Polling defaults to 15 minutes and each source failure is isolated with exponential backoff.

## Operations

`POST /run` starts a one-off source pass. `GET /jobs`, `PATCH /jobs/{id}/status`, and `GET /source-runs` form the minimal dashboard API. `POST /fixtures/smoke` runs a safe fixture through the real persistence/notification pipeline (no webhook request unless configured). Run `pytest` before deployment.

`data/master-profile.json` is the private canonical profile used for truthful tailoring. It is gitignored and excluded from the image; mount it as a volume in production. Do not add contact details, references, secrets, or credentials to source control. Gmail and automatic email applications are deliberately feature-gated until OAuth credentials and a verified recipient are available; neither can affect the notification worker.
