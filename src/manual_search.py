from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select

from .brightdata import BrightDataClient, BrightDataJobs
from .jobspy_source import jobspy_manual_sources
from .config import Settings
from .jobs import NormalizedJob, evaluate, freshness, is_ph_location
from .sources import SourceError, configured_sources
from .jobstreet import jobstreet_sources
from .jobstreet_link import connection_status, has_managed_connection


ProgressCallback = Callable[["SearchProgress"], Awaitable[None]]


# A zero-result targeted search may offer useful alternatives, but those
# alternatives must never be represented as exact search results.  Keeping the
# pool identity here gives every caller one honest, reusable label.
STORED_ALTERNATIVE_POOL_DAYS = 30
STORED_ALTERNATIVE_POOL = "stored_30_day_profile_qualified_alternatives"
STORED_ALTERNATIVE_POOL_LABEL = "Stored profile-qualified alternatives from the last 30 days"


@dataclass
class SearchProgress:
    sources_total: int = 0
    sources_checked: int = 0
    jobs_reviewed: int = 0
    recent_ph_jobs: int = 0
    query_relevant_jobs: int = 0
    entry_level_compatible: int = 0
    potential_matches: int = 0
    live_attempted: bool = False
    live_available: bool = False
    # This is intentionally separate from ``live_available``.  A cached
    # response can contain results previously obtained from live sources, but
    # this invocation did not make a new external request.
    cached_only: bool = False


@dataclass(frozen=True)
class StoredAlternatives:
    """Transparent metadata for non-exact, stored fallback suggestions."""

    jobs: list
    pool: str = STORED_ALTERNATIVE_POOL
    window_days: int = STORED_ALTERNATIVE_POOL_DAYS
    label: str = STORED_ALTERNATIVE_POOL_LABEL


@dataclass
class SearchOutcome:
    jobs: list
    progress: SearchProgress
    duration_seconds: float
    cached_only: bool = False
    errors: list[str] = field(default_factory=list)
    # These are deliberately distinct from ``jobs``.  ``jobs`` matched the
    # user's requested role; alternatives came from a stored 30-day pool.
    alternatives: StoredAlternatives | None = None


class ManualJobSearch:
    """Targeted, bounded discovery. LinkedIn enriches search; ATS is the base."""

    def __init__(self, cfg: Settings, repo):
        self.cfg = cfg
        self.repo = repo
        self.client = BrightDataClient(cfg.brightdata_api_token or "")
        self.cache: dict[tuple, tuple[float, SearchOutcome]] = {}

    @staticmethod
    def _max_age(freshness_text: str) -> int:
        text = (freshness_text or "Past 24 hours").lower()
        if "24" in text:
            return 1
        if "90" in text:
            return 90
        if "60" in text:
            return 60
        if "30" in text:
            return 30
        if "14" in text:
            return 14
        if "7" in text or "week" in text:
            return 7
        return 3

    @staticmethod
    def _family_rank(item: NormalizedJob, role: str) -> int:
        """Prefer the requested computer-role family over broad keyword hits."""
        title = item.title.lower()
        query = role.lower()
        software_family = (
            "software", "developer", "development", "backend", "frontend",
            "full stack", "fullstack", "web developer", "python developer",
            "java developer", "php developer", "node.js developer",
        )
        infrastructure_family = (
            "infrastructure", "devops", "cloud", "platform", "sre",
            "systems", "network", "linux", "noc",
        )
        support_family = ("support", "help desk", "service desk", "application support")
        if any(term in query for term in ("software", "developer", "backend", "frontend", "full stack")):
            if any(term in title for term in software_family) and not any(term in title for term in infrastructure_family):
                return 0
            if any(term in title for term in software_family):
                return 1
            return 2
        if "support" in query or "help desk" in query or "service desk" in query:
            return 0 if any(term in title for term in support_family) else 1
        if any(term in query for term in infrastructure_family):
            return 0 if any(term in title for term in infrastructure_family) else 1
        return 0

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", (value or "").lower()))

    @staticmethod
    def _query_terms(role: str) -> set[str]:
        terms = {
            token for token in ManualJobSearch._tokens(role)
            if len(token) > 2 and token not in {"engineer", "junior", "entry", "level"}
        }
        groups = {
            "software": {"software", "developer", "development", "backend", "frontend", "fullstack", "full", "application"},
            "developer": {"developer", "software", "backend", "frontend", "fullstack", "full", "application"},
            "devops": {"devops", "cloud", "platform", "infrastructure", "sre", "reliability"},
            "cloud": {"cloud", "devops", "platform", "infrastructure", "sre"},
            "support": {"support", "helpdesk", "service", "desktop", "technical", "noc", "operations"},
            "network": {"network", "noc", "infrastructure", "systems", "support"},
        }
        expanded = set(terms)
        for trigger, values in groups.items():
            if trigger in terms:
                expanded.update(values)
        return expanded

    @classmethod
    def _matches_query(cls, item: NormalizedJob, role: str) -> bool:
        title_terms = cls._tokens(item.title)
        requested = {token for token in cls._tokens(role) if len(token) > 2}
        expanded = cls._query_terms(role)
        if requested and requested.issubset(title_terms):
            return True
        # A title is preferred. Description matches alone are too noisy.
        return bool(title_terms & expanded) and (len(requested) == 1 or len(title_terms & expanded) >= 2)

    async def _emit(self, callback: ProgressCallback | None, progress: SearchProgress) -> None:
        if callback:
            await callback(progress)

    @staticmethod
    def _is_recent_active_ph(item: NormalizedJob, cutoff: datetime) -> bool:
        """Shared first stage for both reporting and actual acceptance.

        ``freshness`` normalizes naive source timestamps itself, while this
        helper also normalizes before comparing against the requested cutoff.
        That avoids a naive/aware datetime error from one malformed source
        changing the reporting funnel.
        """
        posted = item.date_posted
        if posted and posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return bool(
            is_ph_location(item)
            and posted
            and posted >= cutoff
            and freshness(item)[2]
        )

    @staticmethod
    def _is_technology_relevant(warnings: list[str]) -> bool:
        """Use the central evaluator's classification without seniority.

        A senior DevOps listing is still query-relevant, but should fall out at
        the subsequent entry-level stage.  This is what makes the visible
        counts a meaningful funnel rather than four unrelated totals.
        """
        return not any(
            warning.startswith("Role is outside")
            for warning in warnings
        )

    @staticmethod
    def _is_entry_level_compatible(warnings: list[str]) -> bool:
        return not any(
            warning.startswith("Senior-level") or warning.startswith("Requires")
            for warning in warnings
        )

    def _accept(self, item: NormalizedJob, role: str, cutoff: datetime, min_score: int, entry_level_only: bool, work_setup: str):
        score, reasons, warnings, relevant = evaluate(item)
        text = f"{item.title} {item.description}".lower()
        if not (self._is_recent_active_ph(item, cutoff) and relevant and score >= min_score):
            return None
        if not self._matches_query(item, role):
            return None
        # Use the central seniority classifier rather than a raw substring
        # check.  A junior listing can legitimately mention a hiring manager,
        # project manager, or manager-facing workflow in its description.
        if entry_level_only and not self._is_entry_level_compatible(warnings):
            return None
        if work_setup and work_setup.lower() not in text and work_setup.lower() not in item.location.lower():
            return None
        return score, reasons, warnings

    def suggested_stored_alternatives(self, role: str, limit: int = 3) -> StoredAlternatives:
        """Return a clearly identified 30-day *stored* alternative pool.

        A zero-result search should still be useful, but these are explicitly
        profile-qualified alternatives rather than pretending they matched the
        requested role.  Related title terms rank first, followed by freshness
        and the existing deterministic profile score.
        """
        from .models import Job, JobStatus

        cutoff = datetime.now(timezone.utc) - timedelta(days=STORED_ALTERNATIVE_POOL_DAYS)
        blocked = {
            JobStatus.EXPIRED.value,
            JobStatus.IGNORED.value,
            JobStatus.APPLIED.value,
            JobStatus.REJECTED.value,
        }
        with self.repo.sessions() as session:
            candidates = session.scalars(
                select(Job).where(Job.score >= self.cfg.min_notify_score, Job.date_posted.is_not(None))
            ).all()

        terms = self._query_terms(role)

        def rank(job):
            posted = job.date_posted
            if posted and posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if not posted or posted < cutoff or job.status in blocked:
                return None
            title_terms = set(job.title.lower().replace("/", " ").replace("-", " ").split())
            related = len(title_terms & terms)
            if not related:
                return None
            return related, posted, job.score

        ranked = [(value, job) for job in candidates if (value := rank(job)) is not None]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return StoredAlternatives(
            jobs=[job for _value, job in ranked[: max(1, min(int(limit), 3))]]
        )

    def suggested_recent_jobs(self, role: str, limit: int = 3) -> list:
        """Compatibility wrapper for callers that only need the jobs list.

        New callers should use :meth:`suggested_stored_alternatives` and show
        its label, so there is no ambiguity between exact live results and the
        separate stored fallback pool.
        """
        return self.suggested_stored_alternatives(role, limit).jobs

    async def find_with_progress(self, role: str, location="Philippines", freshness_text="Past 24 hours", remote="", limit=10, work_setup="", min_score=0, entry_level_only=True, source_filter="all", progress_callback: ProgressCallback | None = None) -> SearchOutcome:
        started = time.monotonic()
        role = role.strip()
        if not role:
            raise ValueError("A job role is required")
        limit = max(1, min(int(limit), 10)); age = self._max_age(freshness_text)
        key = (role.lower(), location.strip().lower(), freshness_text.strip().lower(), remote.strip().lower(), limit, work_setup.lower(), min_score, entry_level_only, source_filter.lower())
        cached = self.cache.get(key)
        if cached and cached[0] > time.time():
            outcome = cached[1]
            # Do not mutate the canonical cached progress object: a later
            # non-cached consumer must not be told its live scan was cached.
            cached_progress = replace(outcome.progress, cached_only=True)
            await self._emit(progress_callback, cached_progress)
            return SearchOutcome(
                outcome.jobs[:limit],
                cached_progress,
                time.monotonic() - started,
                cached_only=True,
                errors=list(outcome.errors),
                alternatives=outcome.alternatives,
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=age)
        progress = SearchProgress()
        accepted: dict[str, object] = {}
        errors: list[str] = []

        async def consume(items: list[NormalizedJob]):
            for item in items:
                progress.jobs_reviewed += 1
                # The visible counters intentionally form a real funnel:
                # recent/active PH -> requested technical role -> entry level
                # -> every remaining user-selected constraint.
                if not self._is_recent_active_ph(item, cutoff):
                    continue
                progress.recent_ph_jobs += 1

                score, _reasons, warnings, _accepted_by_profile = evaluate(item)
                if not (self._is_technology_relevant(warnings) and self._matches_query(item, role)):
                    continue
                progress.query_relevant_jobs += 1

                if not self._is_entry_level_compatible(warnings):
                    continue
                progress.entry_level_compatible += 1

                result = self._accept(item, role, cutoff, min_score, entry_level_only, work_setup)
                if not result:
                    continue
                score, reasons, warnings = result
                progress.potential_matches += 1
                stored = await asyncio.to_thread(self.repo.save, item, score, reasons, warnings)
                stored = stored or await asyncio.to_thread(self.repo.by_item, item)
                if stored:
                    accepted[stored.fingerprint] = stored

        # Search starts with the accumulated database pool, then adds a
        # targeted live pass. This makes a search useful even when one live
        # source is slow while still allowing newly posted jobs to appear.
        def load_stored() -> list[NormalizedJob]:
            from .models import Job, JobStatus
            with self.repo.sessions() as session:
                rows = session.scalars(
                    select(Job).where(
                        Job.status != JobStatus.EXPIRED.value,
                        Job.date_posted.is_not(None),
                        Job.date_posted >= cutoff,
                    ).order_by(Job.date_posted.desc()).limit(500)
                ).all()
            return [
                NormalizedJob(
                    source=row.source, source_job_id=row.source_job_id, title=row.title,
                    company=row.company, location=row.location, work_setup=row.work_setup,
                    description=row.description, url=row.url,
                    application_url=row.application_url, application_email=row.application_email,
                    salary=row.salary, date_posted=row.date_posted,
                    employment_type=row.employment_type, seniority=row.seniority,
                    skills=row.skills or [], raw_metadata=row.raw_metadata or {},
                )
                for row in rows
            ]

        await consume(await asyncio.to_thread(load_stored))

        # ATS sources are the reliable base and remain available without Bright Data.
        source_filter = source_filter.lower()
        # Manual searches are targeted and bounded. The 15-minute scheduler is
        # responsible for the complete source set; a Discord query must not
        # make the user wait through every configured board.
        ats = [] if source_filter in ("linkedin", "brightdata_linkedin", "jobstreet", "indeed", "google", "jobspy", "jobspy_indeed", "jobspy_google") else configured_sources(self.cfg.source_targets)
        jobstreet = []
        if source_filter in ("all", "jobstreet"):
            # A managed JobStreet session is opt-in for targeted searches.
            # jobstreet_sources also permits the documented legacy B64
            # fallback when no managed connection exists.
            if connection_status(self.cfg, self.repo) == "READY" or not hasattr(self.repo, "sessions"):
                jobstreet = jobstreet_sources(self.cfg, self.repo)
            elif not has_managed_connection(self.repo):
                jobstreet = jobstreet_sources(self.cfg, self.repo)
        jobspy = []
        if source_filter in ("all", "indeed", "google", "jobspy", "jobspy_indeed", "jobspy_google"):
            jobspy = jobspy_manual_sources(self.cfg, role, location)
            if source_filter in ("indeed", "jobspy_indeed"):
                jobspy = [source for source in jobspy if source.provider == "indeed"]
            elif source_filter in ("google", "jobspy_google"):
                jobspy = [source for source in jobspy if source.provider == "google"]
        # Keep Discord searches bounded: public ATS + JobStreet + both JobSpy
        # providers share the same eight-source live-search budget.
        ats = ats[:max(0, 8-len(jobstreet)-len(jobspy))] + jobstreet + jobspy
        progress.sources_total = len(ats) + (1 if self.cfg.brightdata_enabled and self.cfg.brightdata_api_token and source_filter in ("all", "linkedin", "brightdata_linkedin") else 0)
        progress.live_attempted = bool(progress.sources_total)
        await self._emit(progress_callback, progress)
        semaphore = asyncio.Semaphore(max(1, min(self.cfg.scan_source_concurrency, 8)))

        async def fetch_ats(source):
            async with semaphore:
                try:
                    timeout = self.cfg.jobspy_scan_timeout_seconds if source.name.startswith("jobspy:") else self.cfg.scan_source_timeout_seconds
                    items = await asyncio.wait_for(source.fetch(), timeout=timeout)
                    progress.live_available = True
                    await consume(items)
                except Exception as exc:
                    errors.append(f"{source.name}: unavailable")
                finally:
                    progress.sources_checked += 1
                    await self._emit(progress_callback, progress)

        ats_task = asyncio.gather(*(fetch_ats(source) for source in ats))

        async def fetch_linkedin():
            if not (self.cfg.brightdata_enabled and self.cfg.brightdata_api_token and source_filter in ("all", "linkedin", "brightdata_linkedin")):
                return
            try:
                if not await asyncio.to_thread(self.repo.reserve_brightdata_page_loads, "linkedin_jobs", 1, self.cfg.brightdata_linkedin_monthly_request_limit):
                    raise SourceError("Bright Data LinkedIn monthly safety limit reached")
                # The Discover API applies its own recent-results default;
                # its schema rejects the UI labels ("Past 90 days", etc.).
                # The stored ATS pool remains the authoritative 90-day
                # search source, while this call adds targeted live results.
                payload = {"location": location, "keyword": role, "country": "PH" if "philipp" in location.lower() else "", "job_type": "", "experience_level": "Entry level" if entry_level_only else "", "remote": remote or work_setup, "company": "", "selective_search": False, "jobs_to_not_include": [], "location_radius": ""}
                rows = await self.client.linkedin_discovery([payload], limit)
                source = BrightDataJobs("linkedin_jobs", self.cfg.brightdata_linkedin_jobs_dataset_id or "gd_lpfll7v5hcqtkxl6l", [payload], self.client)
                items = [source._normalize(row) for row in rows if isinstance(row, dict) and source._usable(row)]
                progress.live_available = True
                await consume(items)
            except Exception:
                errors.append("linkedin: unavailable")
            finally:
                progress.sources_checked += 1
                await self._emit(progress_callback, progress)

        await asyncio.gather(ats_task, fetch_linkedin())
        jobs = sorted(
            accepted.values(),
            key=lambda job: (
                self._family_rank(job, role),
                -(job.score or 0),
                -(job.date_posted.timestamp() if job.date_posted else 0),
            ),
        )[:limit]
        alternatives = None
        if not jobs:
            # This is a distinct pool, not a second hidden live scan and not
            # an exact role match.  Callers can label it truthfully.
            alternatives = await asyncio.to_thread(self.suggested_stored_alternatives, role, 3)
        outcome = SearchOutcome(
            jobs,
            progress,
            time.monotonic() - started,
            errors=errors,
            alternatives=alternatives,
        )
        self.cache[key] = (time.time() + 300, outcome)
        return outcome

    async def find(self, *args, **kwargs):
        return (await self.find_with_progress(*args, **kwargs)).jobs
