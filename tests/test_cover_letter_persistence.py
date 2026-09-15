from datetime import datetime, timezone

import pytest

from src.applications import (
    cover_letter_metadata,
    generate_cover_letter,
    letter_body_hash,
)
from src.models import Job
from src.services import Repository


RESUME = """Mark Andrei R. Castillo
Bacoor City, Cavite, Philippines
+63 953 852 1829
andreicastillofficial@gmail.com
https://github.com/draiimon
https://www.draiimon.gt.tc/

Cloud DevOps Intern — Oaktree Innovations
Worked with AWS, Docker, Terraform, GitHub Actions, and Linux.
Kubernetes Deployment Lab
Docker, Deployments, Services, Nginx Ingress, Linux
"""


def _job() -> Job:
    return Job(
        fingerprint="cover-letter-persistence",
        source="fixture",
        source_job_id="persistence-1",
        title="Infrastructure Developer",
        company="Morgan McKinley",
        location="Taguig, Philippines",
        description="AWS Terraform Docker GitHub Actions Linux Kubernetes CI/CD Python",
        url="https://example.test/jobs/1",
        score=90,
        warnings=[],
        match_reasons=["AWS"],
        raw_metadata={},
        date_posted=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_new_generation_becomes_the_one_persisted_canonical_version(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path}/cover-letter.db")
    repo.create_schema()
    with repo.sessions.begin() as session:
        session.add(_job())

    with repo.sessions() as session:
        stored = session.query(Job).one()
        version_a = await generate_cover_letter(stored, use_ai=False, resume_text=RESUME)
        stored.raw_metadata = cover_letter_metadata(stored.raw_metadata, version_a)
        session.commit()

    with repo.sessions() as session:
        stored = session.query(Job).one()
        version_b = await generate_cover_letter(stored, use_ai=False, regenerate=True, resume_text=RESUME)
        stored.raw_metadata = cover_letter_metadata(stored.raw_metadata, version_b)
        session.commit()

    with repo.sessions() as session:
        active = session.query(Job).one().raw_metadata
    assert active["cover_letter"] == version_b.text
    assert active["cover_letter_version"] == 2
    assert active["cover_letter_body_hash"] == letter_body_hash(version_b.text)
    assert active["cover_letter_versions"][-1]["body_hash"] == letter_body_hash(version_a.text)
