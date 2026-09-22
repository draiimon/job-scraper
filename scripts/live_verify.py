"""Controlled live verification for public sources and the full ingest pipeline.

The run uses a temporary SQLite database and disables real notifications unless
``--send-test-notification`` is explicitly passed. It never submits a job
application and never touches the configured production job database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from src.config import Settings
from src.discovery import discover_sources
from src.jobspy_source import jobspy_sources
from src.models import Job
from src.services import Discord, Pipeline, Repository
from src.sources import configured_sources


def _target_key(target: dict) -> tuple[str, str]:
    return str(target.get("kind")), str(target.get("token") or target.get("site") or target.get("board") or target.get("company"))


async def run(args) -> dict:
    live = Settings()
    with tempfile.TemporaryDirectory(prefix="ph-job-agent-live-") as directory:
        database_path = Path(directory, "verification.sqlite3").resolve().as_posix()
        controlled = live.model_copy(update={
            "database_url": f"sqlite:///{database_path}",
            "discord_webhook_url": None,
            "discord_bot_token": None,
            "ai_enabled": False,
            "application_dry_run": True,
            "max_notifications_per_cycle": 1000,
        })
        repo = Repository(controlled.database_url)
        repo.create_schema()

        discovered = await discover_sources(controlled, repo)
        configured = live.source_targets
        for target in configured:
            repo.upsert_source_target(target, "configured")
        targets = []
        seen = set()
        for target in configured + repo.registry_targets():
            key = _target_key(target)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
        if args.max_sources:
            targets = targets[: args.max_sources]

        sources = configured_sources(targets)
        if not args.skip_jobspy:
            sources.extend(jobspy_sources(controlled, repo, force=True))
        pipeline = Pipeline(repo, controlled)
        pipeline.begin_cycle()
        semaphore = asyncio.Semaphore(max(1, controlled.scan_source_concurrency))

        async def execute(source):
            async with semaphore:
                timeout = getattr(source, "timeout_seconds", controlled.scan_source_timeout_seconds)
                try:
                    return await asyncio.wait_for(pipeline.run_source(source), timeout=timeout)
                except asyncio.TimeoutError:
                    return {"source": source.name, "success": False, "timeout": True, "raw_jobs_discovered": 0, "new": 0, "duplicates_removed": 0, "entry_level_compatible": 0}

        outcomes = await asyncio.gather(*(execute(source) for source in sources))

        with repo.sessions() as session:
            total_jobs = session.scalar(select(func.count()).select_from(Job)) or 0
            score_80 = session.scalar(select(func.count()).select_from(Job).where(Job.score >= 80)) or 0
            notification_failures = session.scalar(select(func.count()).select_from(Job).where(Job.notification_state == "FAILED")) or 0
            top = session.scalars(select(Job).order_by(Job.score.desc(), Job.date_posted.desc().nullslast()).limit(20)).all()
            rendered = [{
                "score": job.score,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "posted_at": job.date_posted.isoformat() if job.date_posted else None,
                "source": job.source,
                "url": job.application_url or job.url,
                "reason": ", ".join(job.match_reasons[:5]),
            } for job in top]
        registry_after_run = len(repo.source_registry_snapshot())

        # Prove persistence with a fresh engine/session against the same file.
        repo.engine.dispose()
        restarted = Repository(controlled.database_url)
        restarted.create_schema()
        with restarted.sessions() as session:
            persisted_after_restart = session.scalar(select(func.count()).select_from(Job)) or 0
        restarted.engine.dispose()

        notification_successes = 0
        notification_mode = "disabled"
        if args.send_test_notification and live.discord_webhook_url:
            notifier = Discord(live.discord_webhook_url, [], live)
            payload = {
                "content": "PH Job Agent controlled verification passed. This test does not submit applications or mention any role.",
                "allowed_mentions": {"parse": []},
            }
            if await notifier.send_payload(payload) == "SENT":
                notification_successes = 1
                notification_mode = "live-test-no-ping"
        elif args.send_test_notification:
            notification_mode = "disabled-missing-webhook"

        provider_counts = Counter(item["source"].split(":", 1)[0] for item in outcomes if item.get("success"))
        return {
            "sources_discovered_this_run": len(discovered),
            "sources_registry_after_run": registry_after_run,
            "sources_enabled": len(sources),
            "sources_healthy": sum(bool(item.get("success")) for item in outcomes),
            "healthy_providers": dict(sorted(provider_counts.items())),
            "jobs_fetched": sum(int(item.get("raw_jobs_discovered", 0) or 0) for item in outcomes),
            "new_jobs": total_jobs,
            "duplicates_removed": sum(int(item.get("duplicates_removed", 0) or 0) for item in outcomes),
            "entry_level_matches": total_jobs,
            "score_gte_80": score_80,
            "notifications_successfully_sent": notification_successes,
            "notification_failures": notification_failures,
            "notification_mode": notification_mode,
            "persisted_after_restart": persisted_after_restart,
            "source_failures": [item["source"] for item in outcomes if not item.get("success")],
            "jobs": rendered,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-sources", type=int, default=0, help="0 checks all configured/discovered sources")
    parser.add_argument("--skip-jobspy", action="store_true")
    parser.add_argument("--send-test-notification", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
