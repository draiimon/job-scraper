from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.jobs import NormalizedJob, evaluate, is_ph_location
from src.jobspy_source import (
    JOBSPY_PH_LOCATIONS,
    JobSpyPlan,
    JobSpySource,
    jobspy_manual_sources,
    jobspy_provider_status,
    jobspy_sources,
)
from src.manual_search import ManualJobSearch
from src.models import Job, JobStatus, SourceHealth
from src.services import Pipeline, Repository
from src.sources import SourceError


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def sample_row(**overrides):
    row = {
        "id": "indeed-123",
        "site": "indeed",
        "title": "Junior Cloud Support Engineer",
        "company": "Cloud PH",
        "location": "Makati, P00, PH",
        "description": "Entry-level technical support for AWS and Linux environments.",
        "job_url": "https://ph.indeed.com/viewjob?jk=123",
        "job_url_direct": "https://careers.cloudph.example/jobs/123",
        "date_posted": datetime.now(timezone.utc) - timedelta(hours=3),
        "job_type": "fulltime",
        "is_remote": False,
    }
    row.update(overrides)
    return row


def cfg(tmp_path=None, **overrides):
    values = {
        "jobspy_enabled": True,
        "jobspy_results_per_query": 3,
        "jobspy_queries_per_cycle": 2,
        "jobspy_min_interval_seconds": 21600,
        "jobspy_request_concurrency": 1,
        "jobspy_max_age_days": 90,
        "discord_webhook_url": None,
        "discord_bot_token": None,
    }
    if tmp_path is not None:
        values["database_url"] = f"sqlite:///{tmp_path}/jobspy.db"
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_indeed_normalizes_direct_url_ph_code_and_provider_metadata(tmp_path):
    calls = []

    def scraper(**kwargs):
        calls.append(kwargs)
        return Rows([sample_row()])

    source = JobSpySource("indeed", cfg(tmp_path), plans=[JobSpyPlan("Cloud Engineer", "Makati, Philippines")], scraper=scraper)
    jobs = await source.fetch()

    assert source.name == "jobspy:indeed_ph"
    assert len(jobs) == 1
    job = jobs[0]
    assert job.url == "https://careers.cloudph.example/jobs/123"
    assert job.application_url == job.url
    assert job.raw_metadata["discovery_url"].startswith("https://ph.indeed.com/")
    assert job.raw_metadata["direct_apply_url"] == job.url
    assert job.raw_metadata["source_provider"] == "indeed"
    assert is_ph_location(job)
    assert calls[0]["site_name"] == ["indeed"]
    assert calls[0]["country_indeed"] == "Philippines"
    assert calls[0]["hours_old"] == 90 * 24
    assert "proxies" not in calls[0]


@pytest.mark.asyncio
async def test_google_uses_google_specific_query_and_never_invents_posted_date(tmp_path):
    calls = []

    def scraper(**kwargs):
        calls.append(kwargs)
        return Rows([sample_row(id="google-1", site="google", date_posted=None, job_url_direct=None)])

    source = JobSpySource("google", cfg(tmp_path), plans=[JobSpyPlan("Software Engineer", "Philippines")], scraper=scraper)
    jobs = await source.fetch()

    assert source.name == "jobspy:google_jobs"
    assert len(jobs) == 1 and jobs[0].date_posted is None
    assert calls[0]["site_name"] == ["google"]
    assert "google_search_term" in calls[0]
    assert "Software Engineer" in calls[0]["google_search_term"]
    assert "Philippines" in calls[0]["google_search_term"]
    assert "hours_old" not in calls[0]
    assert "country_indeed" not in calls[0]


@pytest.mark.asyncio
async def test_google_empty_response_is_degraded_not_falsely_ready(tmp_path):
    source = JobSpySource(
        "google", cfg(tmp_path), plans=[JobSpyPlan("Software Engineer", "Philippines")],
        scraper=lambda **_kwargs: Rows([]),
    )
    assert await source.fetch() == []
    assert source.degraded
    assert source.last_error_category == "no_results"


@pytest.mark.asyncio
async def test_jobspy_partial_provider_failure_is_degraded_but_isolated(tmp_path):
    def scraper(**kwargs):
        if "fail" in kwargs["search_term"]:
            raise TimeoutError("provider timed out")
        return Rows([sample_row(id="ok")])

    source = JobSpySource(
        "indeed",
        cfg(tmp_path),
        plans=[JobSpyPlan("ok", "Philippines"), JobSpyPlan("fail", "Metro Manila, Philippines")],
        scraper=scraper,
    )
    jobs = await source.fetch()

    assert len(jobs) == 1
    assert source.degraded
    assert source.last_error_category == "timeout"
    assert source.pages_fetched == 2


@pytest.mark.asyncio
async def test_jobspy_all_provider_failures_raise_safe_category(tmp_path):
    def scraper(**_kwargs):
        raise RuntimeError("HTTP 429 rate limit")

    source = JobSpySource("google", cfg(tmp_path), plans=[JobSpyPlan("qa", "Philippines")], scraper=scraper)
    with pytest.raises(SourceError, match="rate_limited"):
        await source.fetch()


def test_scheduled_sources_are_only_indeed_and_google_and_rotate_safely(tmp_path):
    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()

    first = jobspy_sources(configuration, repo)
    assert [source.name for source in first] == ["jobspy:indeed_ph", "jobspy:google_jobs"]
    assert all(source.provider in {"indeed", "google"} for source in first)
    assert all(plan.location in JOBSPY_PH_LOCATIONS for source in first for plan in source.plans)
    assert "Remote Philippines" in JOBSPY_PH_LOCATIONS
    assert "Remote" in JOBSPY_PH_LOCATIONS
    assert "Work From Home, Philippines" in JOBSPY_PH_LOCATIONS
    assert [plan.location for plan in first[0].plans] == [
        "Philippines", "Metro Manila, Philippines"
    ]

    now = datetime.now(timezone.utc).isoformat()
    repo.set_state("jobspy_schedule:indeed", {"attempted_at": now})
    repo.set_state("jobspy_schedule:google", {"attempted_at": now})
    assert jobspy_sources(configuration, repo) == []
    # A protected manual scan cannot hammer a provider that was just checked.
    assert jobspy_sources(configuration, repo, force=True) == []
    older = (datetime.now(timezone.utc) - timedelta(seconds=configuration.jobspy_manual_min_interval_seconds + 1)).isoformat()
    repo.set_state("jobspy_schedule:indeed", {"attempted_at": older})
    repo.set_state("jobspy_schedule:google", {"attempted_at": older})
    assert [source.name for source in jobspy_sources(configuration, repo, force=True)] == ["jobspy:indeed_ph", "jobspy:google_jobs"]


def test_jobspy_rejects_unsupported_boards(tmp_path):
    with pytest.raises(ValueError, match="indeed or google"):
        JobSpySource("linkedin", cfg(tmp_path))
    with pytest.raises(ValueError, match="indeed or google"):
        JobSpySource("zip_recruiter", cfg(tmp_path))


def test_remote_ph_requires_explicit_evidence_and_salesforce_stays_technical():
    remote = NormalizedJob(
        "jobspy:google_jobs", "Software Engineer", "Worldwide Corp", "Remote", "Python", "https://example.com/remote",
        date_posted=datetime.now(timezone.utc), work_setup="Remote",
    )
    remote_ph = NormalizedJob(
        "jobspy:google_jobs", "Software Engineer", "PH Corp", "Remote", "Philippines applicants welcome", "https://example.com/remote-ph",
        date_posted=datetime.now(timezone.utc), work_setup="Remote", raw_metadata={"remote_ph_evidence": True},
    )
    salesforce = NormalizedJob(
        "jobspy:indeed_ph", "Salesforce Developer", "Cloud PH", "Manila, Philippines", "Entry-level Python and APIs", "https://example.com/sf",
        date_posted=datetime.now(timezone.utc),
    )
    sales = NormalizedJob(
        "jobspy:indeed_ph", "Technology Sales Representative", "Cloud PH", "Manila, Philippines", "Entry-level CRM sales", "https://example.com/sales",
        date_posted=datetime.now(timezone.utc),
    )
    junior_with_senior_teammate = NormalizedJob(
        "jobspy:indeed_ph", "Junior Software Developer", "Cloud PH", "Manila, Philippines", "Work with senior engineers on Python APIs.", "https://example.com/junior",
        date_posted=datetime.now(timezone.utc),
    )

    assert not is_ph_location(remote)
    assert is_ph_location(remote_ph)
    assert evaluate(salesforce)[3]
    assert not evaluate(sales)[3]
    assert evaluate(junior_with_senior_teammate)[3]


@pytest.mark.asyncio
async def test_pipeline_deduplicates_cross_source_and_preserves_provenance(tmp_path):
    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()
    pipeline = Pipeline(repo, configuration)
    first = NormalizedJob(
        "jobspy:indeed_ph", "Junior Cloud Engineer", "Cloud PH", "Taguig, Philippines", "Entry-level AWS Linux", "https://indeed.example/job",
        source_job_id="indeed-1", application_url="https://careers.cloudph.example/job", date_posted=datetime.now(timezone.utc),
        raw_metadata={"discovery_url": "https://indeed.example/job", "direct_apply_url": "https://careers.cloudph.example/job", "canonical_url": "https://careers.cloudph.example/job"},
    )
    official = NormalizedJob(
        "greenhouse:Cloud PH", "Junior Cloud Engineer", "Cloud PH", "Taguig, Philippines", "Entry-level AWS Linux", "https://boards.greenhouse.io/cloudph/jobs/42",
        source_job_id="42", application_url="https://boards.greenhouse.io/cloudph/jobs/42", date_posted=datetime.now(timezone.utc),
    )

    stored, accepted = await pipeline.process(first, notify=False)
    duplicate, duplicate_accepted = await pipeline.process(official, notify=False)
    assert accepted and stored is not None
    assert duplicate_accepted and duplicate is None
    with repo.sessions() as session:
        row = session.get(type(stored), stored.id)
    assert row.last_seen_at is not None
    assert len(row.raw_metadata["source_provenance"]) == 2
    assert row.application_url == "https://boards.greenhouse.io/cloudph/jobs/42"


def test_same_title_company_location_does_not_merge_distinct_source_jobs(tmp_path):
    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()
    first = NormalizedJob(
        "jobspy:indeed_ph", "Software Engineer", "Same Company", "Makati, Philippines",
        "Build Python APIs with PostgreSQL, Docker, Linux, and automated tests.",
        "https://example.com/role-one", source_job_id="role-one", date_posted=datetime.now(timezone.utc),
    )
    second = NormalizedJob(
        "jobspy:indeed_ph", "Software Engineer", "Same Company", "Makati, Philippines",
        "Create React TypeScript web interfaces, design systems, accessibility, and browser tests.",
        "https://example.com/role-two", source_job_id="role-two", date_posted=datetime.now(timezone.utc),
    )

    row_one = repo.save(first, 70, ["Python"], [])
    row_two = repo.save(second, 70, ["React"], [])

    assert row_one and row_two and row_one.id != row_two.id
    assert row_one.fingerprint != row_two.fingerprint
    assert row_one.identity_key == row_two.identity_key == first.fingerprint
    assert repo.by_item(second).id == row_two.id


@pytest.mark.asyncio
async def test_jobspy_funnel_counts_follow_the_canonical_order(tmp_path):
    class StaticJobSpy:
        name = "jobspy:indeed_ph"
        pages_fetched = 1
        raw_jobs_discovered = 5
        normalized_jobs = 5
        degraded = False
        last_error_category = None

        async def fetch(self):
            now = datetime.now(timezone.utc)
            return [
                NormalizedJob(self.name, "Junior Cloud Engineer", "PH One", "Makati, Philippines", "AWS", "https://example.com/one", date_posted=now),
                NormalizedJob(self.name, "Junior Cloud Engineer", "US One", "New York, United States", "AWS", "https://example.com/us", date_posted=now),
                NormalizedJob(self.name, "Junior Cloud Engineer", "PH Two", "Makati, Philippines", "AWS", "https://example.com/stale", date_posted=now - timedelta(days=91)),
                NormalizedJob(self.name, "Accounting Assistant", "PH Three", "Makati, Philippines", "AWS", "https://example.com/accounting", date_posted=now),
                NormalizedJob(self.name, "Senior Software Engineer", "PH Four", "Makati, Philippines", "Python", "https://example.com/senior", date_posted=now),
            ]

    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()
    outcome = await Pipeline(repo, configuration).run_source(StaticJobSpy())

    assert outcome["raw_jobs_discovered"] == 5
    assert outcome["normalized_jobs"] == 5
    assert outcome["jobs_0_90"] == 4
    assert outcome["ph_remote_ph"] == 3
    assert outcome["computer_related"] == 2
    assert outcome["entry_level_compatible"] == 1
    assert outcome["qualifying"] == 1
    health = repo.health("jobspy:indeed_ph")
    assert (health.last_raw_jobs, health.last_normalized_jobs, health.last_accepted_jobs) == (5, 5, 1)


def test_expiry_keeps_31_to_90_day_jobs_but_expires_older_records(tmp_path):
    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()
    now = datetime.now(timezone.utc)
    within_window = NormalizedJob(
        "jobspy:indeed_ph", "Cloud Support Associate", "Cloud PH", "Makati, Philippines",
        "AWS Linux", "https://example.com/within", source_job_id="within", date_posted=now - timedelta(days=31),
    )
    too_old = NormalizedJob(
        "jobspy:indeed_ph", "Cloud Support Associate", "Cloud PH Two", "Makati, Philippines",
        "AWS Linux", "https://example.com/old", source_job_id="old", date_posted=now - timedelta(days=91),
    )
    row_within = repo.save(within_window, 60, ["AWS"], [])
    row_old = repo.save(too_old, 60, ["AWS"], [])
    assert row_within and row_old

    assert repo.expire_stale_jobs() == 1
    assert repo.by_fingerprint(within_window.fingerprint).status == JobStatus.NEW.value
    assert repo.by_fingerprint(too_old.fingerprint).status == JobStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_manual_search_uses_jobspy_through_existing_query_relevance_path(tmp_path, monkeypatch):
    class FakeJobSpy:
        name = "jobspy:indeed_ph"

        async def fetch(self):
            now = datetime.now(timezone.utc)
            return [
                NormalizedJob(self.name, "Junior Software Developer", "Cloud PH", "Manila, Philippines", "Python APIs", "https://example.com/software", "software", date_posted=now),
                NormalizedJob(self.name, "Infrastructure Engineer", "Cloud PH", "Manila, Philippines", "AWS Terraform", "https://example.com/infra", "infra", date_posted=now),
            ]

    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [])
    monkeypatch.setattr("src.manual_search.jobstreet_sources", lambda *_args: [])
    monkeypatch.setattr("src.manual_search.jobspy_manual_sources", lambda *_args: [FakeJobSpy()])
    configuration = cfg(tmp_path)
    repo = Repository(configuration.database_url)
    repo.create_schema()

    outcome = await ManualJobSearch(configuration, repo).find_with_progress("software engineer")
    assert [job.title for job in outcome.jobs] == ["Junior Software Developer"]
    assert outcome.progress.sources_total == 1
    assert outcome.progress.sources_checked == 1


def test_query_relevance_handles_punctuation_in_real_job_titles():
    cloud_role = NormalizedJob(
        "jobspy:indeed_ph", "O&M Engineer (Cloud Platform Software)", "Cloud PH",
        "Taguig, Philippines", "Cloud platform work", "https://example.com/cloud",
        date_posted=datetime.now(timezone.utc),
    )
    assert ManualJobSearch._matches_query(cloud_role, "Cloud Engineer")
    assert not ManualJobSearch._matches_query(cloud_role, "IT Support")


def test_jobspy_statuses_are_safe_and_source_specific(tmp_path):
    configuration = cfg(tmp_path)
    healthy = SourceHealth(source="jobspy:indeed_ph", status="healthy")
    degraded = SourceHealth(source="jobspy:google_jobs", status="degraded")
    assert jobspy_provider_status(configuration, {healthy.source: healthy}, "indeed") == "READY"
    assert jobspy_provider_status(configuration, {degraded.source: degraded}, "google") == "DEGRADED"
    assert jobspy_provider_status(cfg(tmp_path, jobspy_enabled=False), {}, "indeed") == "DISABLED"
