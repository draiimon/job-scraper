from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.jobs import NormalizedJob
from src.manual_search import ManualJobSearch
from src.services import Repository
from src.models import JobEvent


class FixtureSource:
    name = "greenhouse:fixture"

    async def fetch(self):
        return [
            NormalizedJob(self.name, "Junior Software Developer", "Cloud PH", "Manila, Philippines", "Fresh graduate Python developer", "https://example.com/software", "software", date_posted=datetime.now(timezone.utc)),
            NormalizedJob(self.name, "Accounting Assistant", "Cloud PH", "Manila, Philippines", "Uses AWS accounting software", "https://example.com/accounting", "accounting", date_posted=datetime.now(timezone.utc)),
        ]


@pytest.mark.asyncio
async def test_targeted_ats_search_works_without_linkedin_or_jobstreet(tmp_path, monkeypatch):
    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [FixtureSource()])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/search.db", brightdata_enabled=False, brightdata_api_token=None, jobspy_enabled=False)
    repo = Repository(cfg.database_url); repo.create_schema()
    snapshots=[]

    async def progress(value):
        snapshots.append((value.sources_checked, value.jobs_reviewed, value.potential_matches))

    outcome = await ManualJobSearch(cfg, repo).find_with_progress("software engineer", progress_callback=progress)
    assert [job.title for job in outcome.jobs] == ["Junior Software Developer"]
    assert outcome.progress.live_available and outcome.progress.sources_checked == 1
    assert any(checked == 1 and reviewed == 2 and matches == 1 for checked, reviewed, matches in snapshots)


@pytest.mark.asyncio
async def test_search_does_not_reject_entry_level_job_for_manager_word_in_description(tmp_path, monkeypatch):
    class ManagerMentionSource:
        name = "fixture:manager-word"

        async def fetch(self):
            return [NormalizedJob(
                self.name, "DevOps Engineer", "Cloud PH", "Manila, Philippines",
                "Entry-level AWS Docker Linux role. Work with the project manager on deployment updates.",
                "https://example.com/devops", "devops-manager-word", date_posted=datetime.now(timezone.utc),
            )]

    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [ManagerMentionSource()])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/manager-word.db", brightdata_enabled=False, jobspy_enabled=False)
    repo = Repository(cfg.database_url); repo.create_schema()

    outcome = await ManualJobSearch(cfg, repo).find_with_progress("DevOps Engineer")

    assert [job.title for job in outcome.jobs] == ["DevOps Engineer"]


def test_zero_match_suggestions_use_recent_stored_tech_jobs_without_a_live_scan(tmp_path):
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/suggestions.db", brightdata_enabled=False)
    repo = Repository(cfg.database_url); repo.create_schema()
    now = datetime.now(timezone.utc)
    backend = NormalizedJob(
        "fixture", "Junior Backend Developer", "Cloud PH", "Manila, Philippines",
        "Python API development", "https://example.com/backend", "backend", date_posted=now,
    )
    cloud = NormalizedJob(
        "fixture", "Junior Cloud Engineer", "Cloud PH", "Makati, Philippines",
        "AWS Linux", "https://example.com/cloud", "cloud", date_posted=now,
    )
    stale = NormalizedJob(
        "fixture", "Software Engineer", "Cloud PH", "Manila, Philippines",
        "Python", "https://example.com/stale", "stale", date_posted=now - timedelta(days=31),
    )
    repo.save(backend, 80, ["Python"], [])
    repo.save(cloud, 85, ["AWS"], [])
    repo.save(stale, 95, ["Python"], [])

    suggested = ManualJobSearch(cfg, repo).suggested_recent_jobs("Software Engineer")

    assert [job.title for job in suggested] == ["Junior Backend Developer"]


@pytest.mark.asyncio
async def test_manual_search_limits_live_ats_sources_to_keep_discord_fast(tmp_path, monkeypatch):
    class EmptySource:
        def __init__(self, number): self.name = f"fixture:{number}"
        async def fetch(self): return []

    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [EmptySource(i) for i in range(12)])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/bounded.db", brightdata_enabled=False, jobspy_enabled=False)
    repo = Repository(cfg.database_url); repo.create_schema()
    outcome = await ManualJobSearch(cfg, repo).find_with_progress("software engineer")
    assert outcome.progress.sources_total == 8
    assert outcome.progress.sources_checked == 8


def test_discord_gateway_lease_allows_only_one_owner_and_expires(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path}/lease.db"); repo.create_schema()
    assert repo.acquire_discord_bot_lease("render-a", ttl_seconds=15)
    assert not repo.acquire_discord_bot_lease("local-b", ttl_seconds=15)
    assert repo.renew_discord_bot_lease("render-a", ttl_seconds=15)
    repo.release_discord_bot_lease("render-a")
    assert repo.acquire_discord_bot_lease("local-b", ttl_seconds=15)

def test_application_status_changes_create_a_private_activity_timeline(tmp_path):
    repo=Repository(f"sqlite:///{tmp_path}/timeline.db"); repo.create_schema()
    item=NormalizedJob('fixture','IT Support Specialist','Cloud PH','Manila, Philippines','Fresh graduate Linux support','https://example.com/job','fixture-1',date_posted=datetime.now(timezone.utc))
    stored=repo.save(item,70,['Linux'],[])
    assert repo.set_job_status(stored.id,'SAVED','Saved from Discord.')
    with repo.sessions() as s:
        event=s.query(JobEvent).filter_by(job_id=stored.id).one()
    assert event.event_type == 'SAVED' and event.detail == 'Saved from Discord.'
