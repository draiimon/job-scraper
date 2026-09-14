from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .brightdata import BrightDataClient, BrightDataJobs
from .config import Settings
from .jobs import NormalizedJob, evaluate, freshness, is_ph_location
from .sources import SourceError, configured_sources


ProgressCallback = Callable[["SearchProgress"], Awaitable[None]]


@dataclass
class SearchProgress:
    sources_total: int = 0
    sources_checked: int = 0
    jobs_reviewed: int = 0
    recent_ph_jobs: int = 0
    potential_matches: int = 0
    live_attempted: bool = False
    live_available: bool = False


@dataclass
class SearchOutcome:
    jobs: list
    progress: SearchProgress
    duration_seconds: float
    cached_only: bool = False
    errors: list[str] = field(default_factory=list)


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
        if "14" in text:
            return 14
        if "7" in text or "week" in text:
            return 7
        return 3

    @staticmethod
    def _query_terms(role: str) -> set[str]:
        raw = role.lower().replace("/", " ").replace("-", " ")
        terms = {token for token in raw.split() if len(token) > 2}
        groups = {
            "software": {"software", "developer", "development", "backend", "frontend", "fullstack", "full", "application", "engineer"},
            "developer": {"developer", "software", "backend", "frontend", "fullstack", "full", "application", "engineer"},
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
        title = item.title.lower().replace("/", " ").replace("-", " ")
        title_terms = set(title.split())
        requested = {x for x in role.lower().replace("/", " ").replace("-", " ").split() if len(x) > 2}
        expanded = cls._query_terms(role)
        if requested and requested.issubset(title_terms):
            return True
        # A title is preferred. Description matches alone are too noisy.
        return bool(title_terms & expanded) and (len(requested) == 1 or len(title_terms & expanded) >= 2)

    async def _emit(self, callback: ProgressCallback | None, progress: SearchProgress) -> None:
        if callback:
            await callback(progress)

    def _accept(self, item: NormalizedJob, role: str, cutoff: datetime, min_score: int, entry_level_only: bool, work_setup: str):
        score, reasons, warnings, relevant = evaluate(item)
        text = f"{item.title} {item.description}".lower()
        posted = item.date_posted
        if posted and posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if not (is_ph_location(item) and posted and posted >= cutoff and freshness(item)[2] and relevant and score >= min_score):
            return None
        if not self._matches_query(item, role):
            return None
        if entry_level_only and any(word in text for word in ("senior", "lead", "principal", "manager", "architect", "5+ years", "4+ years")):
            return None
        if work_setup and work_setup.lower() not in text and work_setup.lower() not in item.location.lower():
            return None
        return score, reasons, warnings

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
            return SearchOutcome(outcome.jobs[:limit], outcome.progress, time.monotonic() - started, cached_only=True, errors=outcome.errors)

        cutoff = datetime.now(timezone.utc) - timedelta(days=age)
        progress = SearchProgress()
        accepted: dict[str, object] = {}
        errors: list[str] = []

        async def consume(items: list[NormalizedJob]):
            for item in items:
                progress.jobs_reviewed += 1
                if is_ph_location(item) and item.date_posted and item.date_posted >= cutoff:
                    progress.recent_ph_jobs += 1
                result = self._accept(item, role, cutoff, min_score, entry_level_only, work_setup)
                if not result:
                    continue
                score, reasons, warnings = result
                progress.potential_matches += 1
                stored = await asyncio.to_thread(self.repo.save, item, score, reasons, warnings)
                stored = stored or await asyncio.to_thread(self.repo.by_fingerprint, item.fingerprint)
                if stored:
                    accepted[stored.fingerprint] = stored

        # ATS sources are the reliable base and remain available without Bright Data.
        source_filter = source_filter.lower()
        ats = [] if source_filter in ("linkedin", "brightdata_linkedin", "jobstreet") else configured_sources(self.cfg.source_targets)
        progress.sources_total = len(ats) + (1 if self.cfg.brightdata_enabled and self.cfg.brightdata_api_token and source_filter in ("all", "linkedin", "brightdata_linkedin") else 0)
        progress.live_attempted = bool(progress.sources_total)
        await self._emit(progress_callback, progress)
        semaphore = asyncio.Semaphore(max(1, min(self.cfg.scan_source_concurrency, 8)))

        async def fetch_ats(source):
            async with semaphore:
                try:
                    items = await asyncio.wait_for(source.fetch(), timeout=self.cfg.scan_source_timeout_seconds)
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
                payload = {"location": location, "keyword": role, "country": "PH" if "philipp" in location.lower() else "", "time_range": freshness_text or "Past 24 hours", "job_type": "", "experience_level": "Entry level" if entry_level_only else "", "remote": remote or work_setup, "company": "", "selective_search": False, "jobs_to_not_include": [], "location_radius": ""}
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
        jobs = sorted(accepted.values(), key=lambda job: job.date_posted or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:limit]
        outcome = SearchOutcome(jobs, progress, time.monotonic() - started, errors=errors)
        self.cache[key] = (time.time() + 300, outcome)
        return outcome

    async def find(self, *args, **kwargs):
        return (await self.find_with_progress(*args, **kwargs)).jobs
