from __future__ import annotations

"""Bounded, canonical JobSpy discovery for Indeed Philippines and Google Jobs.

This adapter intentionally does not use JobSpy's LinkedIn or ZipRecruiter
providers, proxies, stealth options, or CAPTCHA-bypass behaviour.  It exposes
only dated public-listing records to the application's existing pipeline.
"""

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .config import Settings
from .jobs import NormalizedJob
from .sources import Source, SourceError

log = logging.getLogger(__name__)


# These are role families, not skill-keyword searches.  They cover the broad
# technical scope while keeping the outbound request matrix deliberately
# bounded.  The scheduler rotates query/location pairs across cycles.
JOBSPY_QUERY_FAMILIES: tuple[str, ...] = (
    '"DevOps Engineer" OR "Junior DevOps" OR "Cloud Engineer" OR "Infrastructure Engineer" OR "Platform Engineer" OR "Site Reliability Engineer"',
    '"Cloud Support" OR "Cloud Operations" OR "Cloud Administrator" OR "IT Operations" OR "Infrastructure Support"',
    '"Systems Engineer" OR "Systems Administrator" OR "Linux Administrator" OR "Server Administrator"',
    '"Network Engineer" OR "NOC Engineer" OR "Network Operations" OR "Technical Operations"',
    '"IT Support" OR "Technical Support" OR "Technical Support Engineer" OR "Help Desk" OR "Service Desk" OR "Desktop Support"',
    '"Application Support" OR "Production Support" OR "Support Engineer" OR "Implementation Engineer"',
    '"Software Engineer" OR "Software Developer" OR "Junior Developer" OR "Backend Developer" OR "Frontend Developer" OR "Full Stack Developer" OR "Web Developer"',
    '"Python Developer" OR "Java Developer" OR "PHP Developer" OR "Node.js Developer" OR "React Developer"',
    '"QA Engineer" OR "QA Tester" OR "Software Tester" OR "Automation QA" OR "Test Engineer"',
    '"Cybersecurity" OR "Security Analyst" OR "SOC Analyst" OR "Database Administrator" OR "Database Support"',
    '"Systems Analyst" OR "Technical Analyst" OR "Cloud Support Associate" OR "IT Associate" OR "Technology Associate" OR "Graduate Technology" OR "Entry Level IT"',
)


# The broad national/Metro/remote terms capture the main market while city
# terms improve local coverage over time.  A persistent cursor prevents the
# source from repeatedly hammering the same query/location pair.
JOBSPY_PH_LOCATIONS: tuple[str, ...] = (
    "Philippines",
    "Metro Manila, Philippines",
    "Remote Philippines",
    "Remote",
    "Work From Home, Philippines",
    "Makati, Philippines",
    "Taguig, Philippines",
    "BGC, Taguig, Philippines",
    "Pasig, Philippines",
    "Mandaluyong, Philippines",
    "Quezon City, Philippines",
    "Manila, Philippines",
    "Muntinlupa, Philippines",
    "Alabang, Muntinlupa, Philippines",
    "Pasay, Philippines",
    "Parañaque, Philippines",
    "Cavite, Philippines",
    "Laguna, Philippines",
    "Clark, Pampanga, Philippines",
    "Cebu, Philippines",
    "Davao, Philippines",
)


@dataclass(frozen=True)
class JobSpyPlan:
    query: str
    location: str


def _clean_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "null"} else text


def _http_url(value: Any) -> str:
    url = _clean_value(value)
    parsed = urlsplit(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _parse_posted_at(value: Any) -> datetime | None:
    """Keep only a provider date; never substitute the discovery timestamp."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    text = _clean_value(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_value(value).lower() in {"1", "true", "yes", "y", "remote"}


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "to_dict"):
        rows = result.to_dict("records")
    elif isinstance(result, list):
        rows = result
    else:
        raise SourceError("JobSpy returned an unsupported response shape")
    return [row for row in rows if isinstance(row, dict)]


def _error_category(error: Exception) -> str:
    text = str(error).lower()
    if "429" in text or "rate limit" in text or "too many" in text:
        return "rate_limited"
    if "captcha" in text or "403" in text or "forbidden" in text or "blocked" in text:
        return "blocked"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text or "network" in text or "dns" in text:
        return "network"
    if "import" in text or "module" in text:
        return "dependency"
    return "provider_error"


def _plan_key(provider: str) -> str:
    return f"jobspy_schedule:{provider}"


def _cursor_key(provider: str) -> str:
    return f"jobspy_cursor:{provider}"


def _parse_state_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, dict) or not value.get("attempted_at"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value["attempted_at"]))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _scheduled_due(cfg: Settings, repo: Any, provider: str, force: bool) -> bool:
    if repo is None:
        return True
    previous = _parse_state_timestamp(repo.state(_plan_key(provider), {}))
    if previous is None:
        return True
    # ``force`` means a protected manual v!scan, not an unlimited bypass of
    # public-board limits. A first manual scan can participate immediately;
    # repeated clicks remain bounded independently of the 15-minute scheduler.
    minimum = (
        cfg.jobspy_manual_min_interval_seconds
        if force
        else cfg.jobspy_min_interval_seconds
    )
    return (datetime.now(timezone.utc) - previous).total_seconds() >= minimum


def _scheduled_plans(cfg: Settings, repo: Any, provider: str) -> list[JobSpyPlan]:
    # Interleave role families over locations. The first bounded cycles
    # therefore cover Philippines, Metro Manila, Remote PH, and nearby cities
    # instead of spending days on one national role family. The complete
    # deterministic matrix still rotates through every pairing.
    plans = [
        JobSpyPlan(
            JOBSPY_QUERY_FAMILIES[(location_index + family_offset) % len(JOBSPY_QUERY_FAMILIES)],
            location,
        )
        for family_offset in range(len(JOBSPY_QUERY_FAMILIES))
        for location_index, location in enumerate(JOBSPY_PH_LOCATIONS)
    ]
    limit = max(1, min(cfg.jobspy_queries_per_cycle, len(plans)))
    if repo is None:
        return plans[:limit]
    cursor_state = repo.state(_cursor_key(provider), {})
    try:
        cursor = int(cursor_state.get("next", 0)) % len(plans)
    except (AttributeError, TypeError, ValueError):
        cursor = 0
    selected = [plans[(cursor + offset) % len(plans)] for offset in range(limit)]
    repo.set_state(_cursor_key(provider), {"next": (cursor + limit) % len(plans)})
    return selected


class JobSpySource(Source):
    """One isolated public-board collector for exactly one JobSpy provider."""

    max_fetch_attempts = 1

    def __init__(
        self,
        provider: str,
        cfg: Settings,
        repo: Any = None,
        plans: Iterable[JobSpyPlan] = (),
        *,
        scheduled: bool = False,
        scraper: Callable[..., Any] | None = None,
    ):
        if provider not in {"indeed", "google"}:
            raise ValueError("JobSpy provider must be indeed or google")
        self.provider = provider
        self.cfg = cfg
        self.repo = repo
        self.name = "jobspy:indeed_ph" if provider == "indeed" else "jobspy:google_jobs"
        self.plans = list(plans)
        self.scheduled = scheduled
        self._scraper = scraper
        self.pages_fetched = 0
        self.raw_jobs_discovered = 0
        self.normalized_jobs = 0
        self.degraded = False
        self.last_error_category: str | None = None

    def _scrape_sync(self, plan: JobSpyPlan) -> Any:
        scraper = self._scraper
        if scraper is None:
            try:
                from jobspy import scrape_jobs
            except ImportError as error:  # pragma: no cover - requirements pins it
                raise SourceError("JobSpy dependency unavailable") from error
            scraper = scrape_jobs
        common = {
            "site_name": [self.provider],
            "search_term": plan.query,
            "location": plan.location,
            "results_wanted": self.cfg.jobspy_results_per_query,
            "description_format": "markdown",
            "verbose": 0,
        }
        if self.provider == "indeed":
            # The package documents Philippines as the exact country value.
            # Do not combine Indeed's hours filter with remote-only filters.
            common.update(
                country_indeed="Philippines",
                hours_old=self.cfg.jobspy_max_age_days * 24,
            )
        else:
            # Google Jobs uses its own query string; it is intentionally not
            # sent JobSpy's hours_old parameter.  Freshness is verified after
            # normalization against the returned provider timestamp.
            common["google_search_term"] = f"{plan.query} jobs in {plan.location}"
        return scraper(**common)

    def _normalize(self, row: dict[str, Any]) -> NormalizedJob | None:
        title = _clean_value(row.get("title"))
        discovery_url = _http_url(row.get("job_url"))
        direct_url = _http_url(row.get("job_url_direct"))
        canonical_url = direct_url or discovery_url
        company = _clean_value(row.get("company"))
        if not company and canonical_url:
            company = urlsplit(canonical_url).netloc.lower()
        if not title or not company or not canonical_url:
            return None

        location = _clean_value(row.get("location"))
        description = _clean_value(row.get("description"))
        is_remote = _truthy(row.get("is_remote"))
        remote_ph_evidence = bool(
            is_remote
            and re.search(r"\b(?:philippines|philippine|ph)\b", f"{location} {description}", flags=re.I)
        )
        source_job_id = _clean_value(row.get("id")) or canonical_url
        if not source_job_id:
            seed = "|".join((self.provider, company, title, location, _clean_value(row.get("date_posted"))))
            source_job_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()

        metadata = {
            "provider": "jobspy",
            "site": self.provider,
            "source_provider": _clean_value(row.get("site")) or self.provider,
            "discovery_url": discovery_url or None,
            "direct_apply_url": direct_url or None,
            "canonical_url": canonical_url,
            "remote_ph_evidence": remote_ph_evidence,
        }
        return NormalizedJob(
            source=self.name,
            source_job_id=source_job_id,
            title=title,
            company=company,
            location=location,
            work_setup="Remote" if is_remote else None,
            description=description,
            url=canonical_url,
            application_url=canonical_url,
            date_posted=_parse_posted_at(row.get("date_posted")),
            employment_type=_clean_value(row.get("job_type")) or None,
            raw_metadata=metadata,
        )

    async def _fetch_plan(self, plan: JobSpyPlan, semaphore: asyncio.Semaphore) -> tuple[list[dict[str, Any]], str | None]:
        async with semaphore:
            self.pages_fetched += 1
            try:
                return _rows_from_result(await asyncio.to_thread(self._scrape_sync, plan)), None
            except Exception as error:
                return [], _error_category(error)

    async def fetch(self) -> list[NormalizedJob]:
        self.pages_fetched = self.raw_jobs_discovered = self.normalized_jobs = 0
        self.degraded = False
        self.last_error_category = None
        if self.scheduled and self.repo is not None:
            await asyncio.to_thread(
                self.repo.set_state,
                _plan_key(self.provider),
                {"attempted_at": datetime.now(timezone.utc).isoformat()},
            )

        semaphore = asyncio.Semaphore(max(1, min(self.cfg.jobspy_request_concurrency, 2)))
        outcomes = await asyncio.gather(*(self._fetch_plan(plan, semaphore) for plan in self.plans))
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for rows, error in outcomes:
            records.extend(rows)
            if error:
                errors.append(error)
        self.raw_jobs_discovered = len(records)
        jobs = [job for row in records if (job := self._normalize(row)) is not None]
        self.normalized_jobs = len(jobs)
        if errors:
            self.last_error_category = errors[0]
            self.degraded = bool(jobs or len(errors) < len(outcomes))
            if not jobs and len(errors) == len(outcomes):
                raise SourceError(f"JobSpy {self.provider} unavailable ({self.last_error_category})")
        elif self.provider == "google" and not jobs:
            # The Google endpoint can return a syntactically valid empty page.
            # Treat that as degraded discovery—not a fake READY state—until a
            # later bounded run yields usable records.
            self.degraded = True
            self.last_error_category = "no_results"
        return jobs


def jobspy_sources(cfg: Settings, repo: Any = None, *, force: bool = False) -> list[JobSpySource]:
    """Return only due scheduled Indeed PH and Google Jobs sources.

    The global scheduler remains unchanged.  Each JobSpy provider has its own
    persisted interval so a 15-minute application poll never hammers boards.
    A protected manual scan passes ``force=True`` and uses the same collectors.
    """
    if not cfg.jobspy_enabled:
        return []
    sources: list[JobSpySource] = []
    for provider, enabled in (("indeed", cfg.jobspy_indeed_enabled), ("google", cfg.jobspy_google_enabled)):
        if not enabled or not _scheduled_due(cfg, repo, provider, force):
            continue
        sources.append(
            JobSpySource(
                provider,
                cfg,
                repo,
                _scheduled_plans(cfg, repo, provider),
                scheduled=True,
            )
        )
    return sources


def jobspy_manual_sources(cfg: Settings, role: str, location: str) -> list[JobSpySource]:
    """Targeted JobSpy requests for the existing v!search path only."""
    if not cfg.jobspy_enabled:
        return []
    plan = JobSpyPlan(role.strip()[:160], location.strip()[:160] or "Philippines")
    sources: list[JobSpySource] = []
    if cfg.jobspy_indeed_enabled:
        sources.append(JobSpySource("indeed", cfg, plans=[plan]))
    if cfg.jobspy_google_enabled:
        sources.append(JobSpySource("google", cfg, plans=[plan]))
    return sources


def jobspy_provider_status(cfg: Settings, health_rows: dict[str, Any], provider: str) -> str:
    if not cfg.jobspy_enabled:
        return "DISABLED"
    if provider == "indeed" and not cfg.jobspy_indeed_enabled:
        return "DISABLED"
    if provider == "google" and not cfg.jobspy_google_enabled:
        return "DISABLED"
    source_name = "jobspy:indeed_ph" if provider == "indeed" else "jobspy:google_jobs"
    row = health_rows.get(source_name)
    if row is None:
        return "STARTING"
    if row.status == "healthy":
        return "READY"
    if row.status == "degraded":
        return "DEGRADED"
    if row.status == "unhealthy":
        return "FAILED"
    return "STARTING"
