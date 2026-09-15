from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.services import Repository
import src.main as main_module


@pytest.mark.asyncio
async def test_automatic_cycle_skips_a_just_completed_manual_overlap(tmp_path, monkeypatch):
    """A manual batch that crosses the deadline must not trigger a duplicate full scan."""
    repo = Repository(f"sqlite:///{tmp_path}/scheduler.db")
    repo.create_schema()
    now = datetime.now(timezone.utc)
    repo.set_state(
        "scheduler",
        {
            "status": "running",
            "phase": "complete",
            "last_poll_at": (now - timedelta(seconds=10)).isoformat(),
            "next_poll_at": (now - timedelta(seconds=20)).isoformat(),
            "jobs_checked": 123,
        },
    )
    cfg = Settings(
        database_url=f"sqlite:///{tmp_path}/scheduler.db",
        polling_enabled=False,
        poll_interval_seconds=900,
    )
    monkeypatch.setattr(main_module, "repo", repo)
    monkeypatch.setattr(main_module, "cfg", cfg)

    outcomes = await main_module.poll_once(manual=False)

    state = repo.state("scheduler")
    assert outcomes == []
    assert state["jobs_checked"] == 123
    assert datetime.fromisoformat(state["next_poll_at"]) > now
