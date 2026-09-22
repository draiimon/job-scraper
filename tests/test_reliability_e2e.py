from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import func, select

from src.config import Settings
from src.jobs import NormalizedJob, evaluate
from src.models import Job
from src.services import Pipeline, Repository
from src.sources import Greenhouse


def _matching_job(source_job_id="one") -> NormalizedJob:
    return NormalizedJob(
        source="greenhouse:Acme PH",
        source_job_id=source_job_id,
        title="Junior Software Engineer",
        company="Acme PH",
        location="Remote - Philippines",
        description="Fresh graduates welcome. Python Node.js PostgreSQL Docker REST APIs. 0-2 years experience.",
        requirements="Bachelor of Computer Science",
        url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        application_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        date_posted=datetime.now(timezone.utc),
        raw_metadata={"canonical_url": f"https://boards.greenhouse.io/acme/jobs/{source_job_id}"},
    )


def test_target_profile_scoring_and_canonical_enrichment(tmp_path):
    job = _matching_job()
    score, reasons, warnings, eligible = evaluate(job)
    assert score >= 80
    assert eligible
    assert not warnings
    assert "Entry-level indicator" in reasons

    repo = Repository(f"sqlite:///{tmp_path / 'canonical.db'}")
    repo.create_schema()
    pipeline = Pipeline(repo, Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'canonical.db'}"))
    stored, accepted = _run(pipeline.process(job, notify=False))
    assert accepted and stored is not None
    assert stored.experience_min == 0
    assert stored.experience_max == 2
    assert stored.seniority == "ENTRY_LEVEL"
    assert stored.category == "SOFTWARE_ENGINEERING"
    assert stored.country == "Philippines"
    assert stored.work_setup == "REMOTE"


@pytest.mark.asyncio
async def test_failed_webhook_retries_and_delivery_is_durable(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'retry.db'}")
    repo.create_schema()
    cfg = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'retry.db'}",
        discord_webhook_url="https://discord.example/webhook",
        notification_retry_base_seconds=1,
        notification_retry_max_attempts=3,
    )
    pipeline = Pipeline(repo, cfg)
    attempts = 0

    async def flaky_send(_job):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            request = httpx.Request("POST", cfg.discord_webhook_url)
            response = httpx.Response(429, headers={"Retry-After": "2"}, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return "SENT"

    pipeline.discord.send = flaky_send
    stored, accepted = await pipeline.process(_matching_job(), notify=True)
    assert accepted and stored is not None
    with repo.sessions.begin() as session:
        failed = session.get(Job, stored.id)
        assert failed.notification_state == "FAILED"
        assert failed.notification_attempts == 1
        assert failed.notification_next_attempt_at is not None
        failed.notification_next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await pipeline.retry_notifications() == 1
    with repo.sessions() as session:
        delivered = session.get(Job, stored.id)
        assert delivered.notification_state == "SENT"
        assert delivered.status == "NOTIFIED"
        assert delivered.notification_error is None
    assert attempts == 2


@pytest.mark.asyncio
async def test_bulk_digest_batches_every_due_job_without_top_five_truncation(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'digest.db'}")
    repo.create_schema()
    cfg = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'digest.db'}",
        discord_webhook_url="https://discord.example/webhook",
        notification_batch_size=8,
        min_notify_score=60,
    )
    pipeline = Pipeline(repo, cfg)
    for index in range(12):
        item = _matching_job(str(index))
        item.company = f"Company {index}"
        stored = repo.save(item, 70 + index % 5, ["entry-level", "Philippines"], [])
        assert stored is not None

    payload_sizes = []

    async def send_payload(payload):
        payload_sizes.append(len(payload["embeds"]))
        return "SENT"

    pipeline.discord.send_payload = send_payload
    delivered = await pipeline.send_digest()

    assert delivered == 12
    assert payload_sizes == [8, 4]
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.notification_state == "SENT")) == 12


@pytest.mark.asyncio
async def test_fixture_source_to_normalize_dedupe_score_store_notify_end_to_end(monkeypatch, tmp_path):
    async def fake_get_json(_url, **_kwargs):
        row = {
            "id": 44,
            "title": "Junior Software Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/44",
            "location": {"name": "Remote - Philippines"},
            "content": "Fresh graduates welcome. Python Node.js PostgreSQL Docker REST APIs. 0-2 years. Bachelor of Computer Science.",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"jobs": [row, dict(row)], "meta": {"total": 2}}

    monkeypatch.setattr("src.sources.get_json", fake_get_json)
    db = tmp_path / "e2e.db"
    repo = Repository(f"sqlite:///{db}")
    repo.create_schema()
    repo.upsert_source_target({"kind": "greenhouse", "name": "Acme PH", "token": "acme", "career_url": "https://boards.greenhouse.io/acme"}, "fixture")
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{db}", discord_webhook_url="https://discord.example/webhook")
    pipeline = Pipeline(repo, cfg)
    delivered = []

    async def send(job):
        delivered.append(job.id)
        return "SENT"

    pipeline.discord.send = send
    source = Greenhouse({"kind": "greenhouse", "name": "Acme PH", "token": "acme"})
    first = await pipeline.run_source(source)
    second = await pipeline.run_source(Greenhouse({"kind": "greenhouse", "name": "Acme PH", "token": "acme"}))

    assert first["success"] and second["success"]
    assert first["raw_jobs_discovered"] == 1  # provider ID dedupe happens during retrieval
    assert first["new"] == 1
    assert second["new"] == 0
    assert second["duplicates_removed"] >= 1
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        stored = session.scalar(select(Job))
        assert stored.score >= 80
        assert stored.notification_state == "SENT"
    assert delivered == [stored.id]
    assert repo.source_registry_snapshot()[0]["last_success"] is not None


@pytest.mark.asyncio
async def test_one_malformed_job_does_not_abort_valid_items(tmp_path):
    class Broken:
        @property
        def title(self):
            raise ValueError("malformed")

    class MixedSource:
        name = "fixture:mixed"
        raw_jobs_discovered = 2
        normalized_jobs = 2
        pages_fetched = 1
        max_fetch_attempts = 1

        async def fetch(self):
            return [Broken(), _matching_job("valid")]

    repo = Repository(f"sqlite:///{tmp_path / 'mixed.db'}")
    repo.create_schema()
    pipeline = Pipeline(repo, Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'mixed.db'}"))
    outcome = await pipeline.run_source(MixedSource())

    assert outcome["success"]
    assert outcome["new"] == 1
    assert outcome["malformed_jobs"] >= 1


def test_worker_lease_prevents_duplicate_scheduler_owners(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'lease.db'}")
    repo.create_schema()
    assert repo.acquire_worker_lease("worker-a", ttl_seconds=60)
    assert not repo.acquire_worker_lease("worker-b", ttl_seconds=60)
    assert repo.renew_worker_lease("worker-a", ttl_seconds=60)
    repo.release_worker_lease("worker-a")
    assert repo.acquire_worker_lease("worker-b", ttl_seconds=60)


def test_outbound_ats_job_link_is_automatically_promoted_to_registry(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'promotion.db'}")
    repo.create_schema()
    pipeline = Pipeline(repo, Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'promotion.db'}"))
    job = _matching_job()
    job.source = "jobspy:google"
    job.url = "https://jobs.lever.co/new-company/abc123"
    job.application_url = job.url
    job.raw_metadata = {"canonical_url": job.url}

    stored, accepted = _run(pipeline.process(job, notify=False))

    assert accepted and stored is not None
    sources = repo.source_registry_snapshot()
    assert len(sources) == 1
    assert sources[0]["provider"] == "lever"
    assert sources[0]["board_identifier"] == "new-company"
    assert sources[0]["discovery_method"] == "job_outbound_link"


def test_concurrent_ingestion_keeps_one_canonical_record(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'concurrent.db'}")
    repo.create_schema()

    def save_once(_index):
        return repo.save(_matching_job("concurrent"), 90, ["entry-level"], [])

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(save_once, range(8)))

    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_material_job_change_updates_canonical_record_without_duplicate(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'update.db'}")
    repo.create_schema()
    original = _matching_job("update-me")
    first, outcome = repo.save_with_result(original, 82, ["original"], [])
    assert first is not None and outcome == "new"

    changed = _matching_job("update-me")
    changed.description += " Added Terraform and GitHub Actions."
    result, outcome = repo.save_with_result(changed, 91, ["material update"], [])

    assert result is None and outcome == "updated"
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        stored = session.scalar(select(Job))
        assert "Terraform" in stored.description
        assert stored.score == 91
        assert stored.match_reasons == ["material update"]


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)
