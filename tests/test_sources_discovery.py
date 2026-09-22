from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.config import Settings
from src.discovery import _public_search_targets, _safe_url, discover_sources, target_from_job_url
from src.services import Repository
from src.sources import Ashby, Greenhouse, Lever, SmartRecruiters


@pytest.mark.asyncio
async def test_greenhouse_fetches_every_page_and_normalizes(monkeypatch):
    calls = []

    async def fake_get_json(_url, *, params=None, **_kwargs):
        calls.append(dict(params or {}))
        if params and params.get("page") == 2:
            return {"jobs": [{"id": 2, "title": "QA Automation Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "location": {"name": "Makati, Philippines"}, "content": "Playwright, fresh graduates welcome", "updated_at": "2026-09-21T00:00:00Z"}], "meta": {"total": 2}}
        return {"jobs": [None, {"id": 1, "title": "Junior Software Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote - Philippines"}, "content": "Python Docker entry level", "updated_at": "2026-09-22T00:00:00Z"}], "meta": {"total": 2}}

    monkeypatch.setattr("src.sources.get_json", fake_get_json)
    source = Greenhouse({"kind": "greenhouse", "name": "Acme", "token": "acme"})
    jobs = await source.fetch()

    assert len(jobs) == 2
    assert source.pages_fetched == 2
    assert calls == [{"content": "true"}, {"content": "true", "page": 2}]
    assert jobs[0].company == "Acme"
    assert jobs[0].country == "Philippines"
    assert jobs[0].remote_type == "REMOTE"
    assert jobs[0].date_posted == datetime(2026, 9, 22, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_lever_cursor_pagination_and_hosted_apply_url(monkeypatch):
    async def fake_get_json(_url, *, params=None, **_kwargs):
        if params and params.get("offset") == "cursor-2":
            return {"data": [{"id": "b", "text": "Cloud Support Engineer", "hostedUrl": "https://jobs.lever.co/acme/b", "applyUrl": "https://jobs.lever.co/acme/b/apply", "categories": {"location": "Manila, Philippines", "commitment": "Full-time"}, "descriptionPlain": "AWS Linux 0-2 years", "createdAt": 1789948800000}]}
        return {"data": [{"id": "a", "text": "Associate Software Engineer", "hostedUrl": "https://jobs.lever.co/acme/a", "categories": {"location": "Remote Philippines"}, "descriptionPlain": "Java SQL"}], "next": "cursor-2"}

    monkeypatch.setattr("src.sources.get_json", fake_get_json)
    source = Lever({"kind": "lever", "name": "Acme", "site": "acme"})
    jobs = await source.fetch()

    assert [job.source_job_id for job in jobs] == ["a", "b"]
    assert source.pages_fetched == 2
    assert jobs[1].application_url.endswith("/apply")
    assert jobs[1].employment_type == "Full-time"


@pytest.mark.asyncio
async def test_ashby_secondary_locations_compensation_and_unlisted_filter(monkeypatch):
    async def fake_get_json(_url, **_kwargs):
        return {"jobs": [
            {"id": "one", "title": "Junior AI Engineer", "location": "Manila", "secondaryLocations": [{"location": "Remote - Philippines"}], "descriptionPlain": "RAG LLM Python", "department": "Engineering", "team": "AI", "employmentType": "FullTime", "publishedAt": "2026-09-22T01:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/acme/one", "applyUrl": "https://jobs.ashbyhq.com/acme/one/application", "compensation": {"min": 40000, "max": 60000, "currency": "PHP"}},
            {"id": "hidden", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/hidden"},
        ]}

    monkeypatch.setattr("src.sources.get_json", fake_get_json)
    jobs = await Ashby({"kind": "ashby", "name": "Acme", "board": "acme"}).fetch()

    assert len(jobs) == 1
    assert "Remote - Philippines" in jobs[0].location
    assert jobs[0].salary_min == 40000
    assert jobs[0].salary_max == 60000
    assert jobs[0].salary_currency == "PHP"


@pytest.mark.asyncio
async def test_smartrecruiters_offset_pagination(monkeypatch):
    async def fake_get_json(_url, *, params=None, **_kwargs):
        offset = params["offset"]
        if offset == 0:
            return {"totalFound": 2, "content": [{"id": "1", "name": "Junior Developer", "location": {"city": "Taguig", "country": "ph", "remote": True}}]}
        return {"totalFound": 2, "content": [{"id": "2", "name": "QA Engineer", "location": {"city": "Makati", "country": "ph"}}]}

    monkeypatch.setattr("src.sources.get_json", fake_get_json)
    source = SmartRecruiters({"kind": "smartrecruiters", "name": "Acme", "company": "Acme"})
    jobs = await source.fetch()

    assert len(jobs) == 2
    assert source.pages_fetched == 2
    assert jobs[0].work_setup == "REMOTE"


def test_discovery_rejects_private_and_non_https_urls():
    assert _safe_url("http://jobs.lever.co/acme") is None
    assert _safe_url("https://127.0.0.1/jobs") is None
    assert _safe_url("https://10.0.0.4/jobs") is None
    assert _safe_url("https://jobs.lever.co/acme") == "https://jobs.lever.co/acme"
    assert target_from_job_url("https://jobs.lever.co/acme/123", "Acme PH") == {
        "kind": "lever",
        "name": "Acme PH",
        "site": "acme",
        "career_url": "https://jobs.lever.co/acme/123",
        "domain": "jobs.lever.co",
        "discovery_method": "job_outbound_link",
    }
    assert target_from_job_url("https://example.com/jobs/1", "Ignored") is None


@pytest.mark.asyncio
async def test_public_search_is_bounded_and_accepts_only_supported_ats(monkeypatch):
    rss = """<?xml version='1.0'?><rss><channel>
      <item><link>https://jobs.lever.co/acme/123</link></item>
      <item><link>https://evil.example/jobs/1</link></item>
      <item><link>https://jobs.ashbyhq.com/cloud-ph/abc</link></item>
    </channel></rss>"""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(200, text=rss, request=request)

    monkeypatch.setattr("src.discovery.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    cfg = Settings(_env_file=None, source_discovery_search_queries_json=json.dumps(["one query"]), source_discovery_max_search_results=10)
    targets = await _public_search_targets(cfg)

    assert {(row["kind"], row.get("site") or row.get("board")) for row in targets} == {("lever", "acme"), ("ashby", "cloud-ph")}
    assert all(row["discovery_method"] == "public_web_search" for row in targets)


@pytest.mark.asyncio
async def test_discovery_persists_provenance_and_deduplicates_search_and_seed(monkeypatch, tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'discovery.db'}")
    repo.create_schema()
    cfg = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'discovery.db'}",
        source_discovery_seeds_json=json.dumps([
            {"company": "Acme Philippines", "url": "https://jobs.lever.co/acme"},
            "http://jobs.lever.co/rejected",
        ]),
    )
    monkeypatch.setattr("src.discovery._public_search_targets", lambda _cfg: _async_value([
        {"kind": "lever", "name": "Acme", "site": "acme", "career_url": "https://jobs.lever.co/acme/1", "discovery_method": "public_web_search"}
    ]))

    targets = await discover_sources(cfg, repo)
    snapshot = repo.source_registry_snapshot()

    assert len(targets) == 1
    assert len(snapshot) == 1
    assert snapshot[0]["company"] == "Acme Philippines"
    assert snapshot[0]["discovery_method"] == "seed_ats_url"


async def _async_value(value):
    return value
