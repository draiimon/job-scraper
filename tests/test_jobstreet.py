from datetime import datetime, timezone

from src.config import Settings
from src.jobstreet import (
    JOBSTREET_SOURCE_NAME,
    _normalize_listing,
    _parse_posted,
    has_storage_state,
    jobstreet_sources,
    jobstreet_status,
)


def test_jobstreet_session_defaults_to_private_and_auth_required(tmp_path):
    cfg = Settings(jobstreet_session_path=str(tmp_path / "private" / "jobstreet_session.json"))
    assert not has_storage_state(cfg)
    assert jobstreet_status(cfg) == "AUTH REQUIRED"
    assert jobstreet_sources(cfg) == []


def test_jobstreet_storage_state_is_accepted_without_exposing_contents(tmp_path):
    path = tmp_path / "jobstreet_session.json"
    path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    cfg = Settings(jobstreet_session_path=str(path))
    assert has_storage_state(cfg)
    assert jobstreet_status(cfg) == "READY"
    assert len(jobstreet_sources(cfg)) == 1


def test_jobstreet_normalizes_visible_discovery_listing():
    item = _normalize_listing(
        {
            "url": "https://ph.jobstreet.com/job/123",
            "title": "Junior Cloud Engineer",
            "text": "Junior Cloud Engineer\nCloud PH\nMakati, Philippines\nPosted 2 hours ago\nAWS Docker Linux",
            "posted": "Posted 2 hours ago",
        }
    )
    assert item is not None
    assert item.source == JOBSTREET_SOURCE_NAME
    assert item.application_url == item.url
    assert item.raw_metadata["discovery_only"] is True
    assert item.raw_metadata["auth"] == "google_oauth"
    assert item.date_posted is not None


def test_jobstreet_relative_date_is_timezone_aware():
    posted = _parse_posted("1 day ago", datetime(2026, 9, 14, tzinfo=timezone.utc))
    assert posted == datetime(2026, 9, 13, tzinfo=timezone.utc)