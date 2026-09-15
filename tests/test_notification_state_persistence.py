from datetime import datetime, timezone

import pytest

from src.config import Settings
from src.jobs import NormalizedJob
from src.models import Job, JobStatus
from src.services import Pipeline, Repository


def _job() -> NormalizedJob:
    return NormalizedJob(
        source="fixture",
        source_job_id="notification-state",
        title="Junior DevOps Engineer",
        company="Example Technologies",
        location="Manila, Philippines",
        description="AWS Docker Terraform Linux CI/CD fresh graduate.",
        url="https://example.test/jobs/notification-state",
        date_posted=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_status",
    [
        JobStatus.SAVED.value,
        JobStatus.IGNORED.value,
        JobStatus.APPLIED.value,
        JobStatus.REJECTED.value,
    ],
)
async def test_webhook_delivery_never_overwrites_a_user_or_application_decision(tmp_path, user_status):
    repo = Repository(f"sqlite:///{tmp_path}/notifications.db")
    repo.create_schema()
    stored = repo.save(_job(), 85, ["AWS"], [])
    assert stored is not None

    # Simulate a decision made after this detached object was queued for
    # notification but before the webhook delivery transaction completes.
    with repo.sessions.begin() as session:
        session.get(Job, stored.id).status = user_status

    pipeline = Pipeline(
        repo,
        Settings(
            database_url=f"sqlite:///{tmp_path}/notifications.db",
            polling_enabled=False,
            discord_webhook_url="https://discord.example/webhook",
            discord_bot_token=None,
        ),
    )

    async def sent(_job_record):
        return "SENT"

    pipeline.discord.send = sent
    await pipeline.notify(stored)

    with repo.sessions() as session:
        record = session.get(Job, stored.id)
        assert record.notification_state == "SENT"
        assert record.status == user_status


@pytest.mark.asyncio
async def test_successful_webhook_delivery_marks_an_unmodified_job_notified(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path}/notifications.db")
    repo.create_schema()
    stored = repo.save(_job(), 85, ["AWS"], [])
    assert stored is not None
    pipeline = Pipeline(
        repo,
        Settings(
            database_url=f"sqlite:///{tmp_path}/notifications.db",
            polling_enabled=False,
            discord_webhook_url="https://discord.example/webhook",
            discord_bot_token=None,
        ),
    )

    async def sent(_job_record):
        return "SENT"

    pipeline.discord.send = sent
    await pipeline.notify(stored)

    with repo.sessions() as session:
        record = session.get(Job, stored.id)
        assert record.notification_state == "SENT"
        assert record.status == JobStatus.NOTIFIED.value


def test_failed_bot_alert_can_be_reclaimed_once_but_a_sent_alert_cannot(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path}/notifications.db")
    repo.create_schema()
    stored = repo.save(_job(), 85, ["AWS"], [])
    assert stored is not None

    with repo.sessions.begin() as session:
        session.get(Job, stored.id).notification_state = "BOT_PENDING"

    assert repo.claim_bot_alert(stored.id)
    assert not repo.claim_bot_alert(stored.id)  # another concurrent worker loses

    with repo.sessions.begin() as session:
        session.get(Job, stored.id).notification_state = "FAILED"

    assert repo.claim_bot_alert(stored.id)  # retry a failed pending bot delivery
    assert not repo.claim_bot_alert(stored.id)

    assert repo.record_alert_delivery(stored.id, "SENT")
    assert not repo.claim_bot_alert(stored.id)  # successful alerts never re-ping
