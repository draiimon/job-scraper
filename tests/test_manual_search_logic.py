from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.jobs import NormalizedJob
from src.manual_search import (
    STORED_ALTERNATIVE_POOL,
    STORED_ALTERNATIVE_POOL_DAYS,
    ManualJobSearch,
)
from src.services import Repository


def job(title: str, company: str, description: str, *, posted: datetime) -> NormalizedJob:
    return NormalizedJob(
        "fixture",
        title,
        company,
        "Manila, Philippines",
        description,
        f"https://example.com/{company.lower().replace(' ', '-')}",
        company.lower().replace(" ", "-"),
        date_posted=posted,
    )


@pytest.mark.asyncio
async def test_progress_counts_are_a_real_recent_query_entry_match_funnel(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)

    class FunnelSource:
        name = "fixture:funnel"

        async def fetch(self):
            return [
                # Outside the selected 24-hour window: not in any stage.
                job("Software Engineer", "Stale Co", "Python software role", posted=now - timedelta(days=2)),
                # Recent PH, but unrelated despite the AWS keyword.
                job("Accounting Assistant", "Accounts Co", "Uses AWS accounting software", posted=now),
                # Query-relevant, but must drop before the entry-level stage.
                job("Senior Software Engineer", "Senior Co", "AWS Docker Python", posted=now),
                # Entry-level compatible but deliberately below the chosen score.
                job("Junior Backend Developer", "Starter Co", "Fresh graduate Python API work", posted=now),
                # Survives every selected constraint.
                job("Junior Software Developer", "Match Co", "Fresh graduate AWS Docker Linux", posted=now),
            ]

    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [FunnelSource()])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/funnel.db", brightdata_enabled=False, jobspy_enabled=False)
    repo = Repository(cfg.database_url)
    repo.create_schema()

    outcome = await ManualJobSearch(cfg, repo).find_with_progress("Software Engineer", min_score=90)

    progress = outcome.progress
    assert progress.jobs_reviewed == 5
    assert progress.recent_ph_jobs == 4
    assert progress.query_relevant_jobs == 3
    assert progress.entry_level_compatible == 2
    assert progress.potential_matches == 1
    assert progress.recent_ph_jobs >= progress.query_relevant_jobs >= progress.entry_level_compatible >= progress.potential_matches
    assert [stored.title for stored in outcome.jobs] == ["Junior Software Developer"]


@pytest.mark.asyncio
async def test_cached_outcome_is_explicit_and_emits_cached_progress(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    calls = 0
    callbacks: list[bool] = []

    class OneSource:
        name = "fixture:cache"

        async def fetch(self):
            nonlocal calls
            calls += 1
            return [job("Junior Software Developer", "Cache Co", "Fresh graduate AWS Docker Linux", posted=now)]

    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [OneSource()])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/cache.db", brightdata_enabled=False, jobspy_enabled=False)
    repo = Repository(cfg.database_url)
    repo.create_schema()
    search = ManualJobSearch(cfg, repo)

    first = await search.find_with_progress("Software Engineer")

    async def capture(progress):
        callbacks.append(progress.cached_only)

    second = await search.find_with_progress("Software Engineer", progress_callback=capture)

    assert not first.cached_only
    assert not first.progress.cached_only
    assert second.cached_only
    assert second.progress.cached_only
    assert callbacks == [True]
    assert calls == 1
    # The cached flag belongs to the returned copy, not the saved result.
    assert not first.progress.cached_only


@pytest.mark.asyncio
async def test_zero_result_alternatives_are_explicitly_separate_stored_30_day_pool(tmp_path, monkeypatch):
    monkeypatch.setattr("src.manual_search.configured_sources", lambda _targets: [])
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/alternatives.db", brightdata_enabled=False, jobspy_enabled=False)
    repo = Repository(cfg.database_url)
    repo.create_schema()
    now = datetime.now(timezone.utc)
    stored = job("Junior Backend Developer", "Alternative Co", "Fresh graduate Python API development", posted=now)
    repo.save(stored, 80, ["Python"], [])

    outcome = await ManualJobSearch(cfg, repo).find_with_progress("Software Engineer", min_score=100)

    assert outcome.jobs == []
    assert outcome.alternatives is not None
    assert outcome.alternatives.pool == STORED_ALTERNATIVE_POOL
    assert outcome.alternatives.window_days == STORED_ALTERNATIVE_POOL_DAYS == 30
    assert [candidate.title for candidate in outcome.alternatives.jobs] == ["Junior Backend Developer"]
    assert "stored" in outcome.alternatives.label.lower()
    assert "30 days" in outcome.alternatives.label
