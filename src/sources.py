from __future__ import annotations

import asyncio
import html
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .jobs import NormalizedJob, detect_remote_type

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source-specific failure that must not stop other providers."""


class Source(ABC):
    name: str
    # HTTP requests already retry transient failures. Retrying a complete,
    # paginated board duplicates work and can exceed the scheduler timeout.
    max_fetch_attempts = 1

    @abstractmethod
    async def fetch(self) -> list[NormalizedJob]:
        raise NotImplementedError


class DisabledBoard(Source):
    def __init__(self, name: str):
        self.name = name

    async def fetch(self):
        raise SourceError("No official public endpoint configured; adapter disabled")


def _text(value) -> str:
    if value is None:
        return ""
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"\s+", " ", value).strip()


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _domain(url: str | None) -> str | None:
    try:
        return urlsplit(url or "").netloc.lower() or None
    except ValueError:
        return None


def _join_locations(values) -> str:
    if isinstance(values, str):
        return values.strip()
    if isinstance(values, dict):
        return _text(values.get("name") or values.get("location") or values.get("city"))
    result = []
    for value in values or []:
        if isinstance(value, dict):
            result.append(_text(value.get("name") or value.get("location") or value.get("city")))
        else:
            result.append(_text(value))
    return ", ".join(value for value in result if value)


def _salary(value) -> tuple[str | None, float | None, float | None, str | None]:
    if not value:
        return None, None, None, None
    if isinstance(value, dict):
        minimum = value.get("min") or value.get("minimum") or value.get("minValue")
        maximum = value.get("max") or value.get("maximum") or value.get("maxValue")
        currency = value.get("currency") or value.get("currencyCode")
        display = value.get("description") or value.get("text")
        if not display:
            display = " - ".join(str(x) for x in (minimum, maximum, currency) if x is not None)
        return _text(display) or None, _number(minimum), _number(maximum), _text(currency).upper() or None
    return _text(value) or None, None, None, None


def _number(value) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _normalize_safely(source: Source, rows: list[dict], converter) -> list[NormalizedJob]:
    """Skip one malformed provider record without discarding the whole board."""
    result: list[NormalizedJob] = []
    errors = 0
    for row in rows:
        try:
            item = converter(row)
            if item and item.title and item.url:
                result.append(item)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            errors += 1
            log.warning("source_record_normalization_failed source=%s error=%s", source.name, type(exc).__name__)
    source.normalization_errors = getattr(source, "normalization_errors", 0) + errors
    source.normalized_jobs = len(result)
    return result


async def get_json(url: str, *, params: dict | None = None, timeout: float = 20, retries: int = 2) -> dict | list:
    """Bounded public GET with retry/backoff and no credential forwarding."""
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers={"User-Agent": "PH-Job-Agent/1.0 (+public-ats-reader)"})
            if response.status_code in {408, 429, 500, 502, 503, 504} and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(8, 2**attempt)
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, (dict, list)):
                raise SourceError("public ATS endpoint returned an unsupported JSON shape")
            return data
        except (httpx.HTTPError, ValueError, SourceError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            await asyncio.sleep(min(8, 2**attempt))
    raise SourceError(f"public ATS request failed: {urlsplit(url).netloc}") from last_error


class Greenhouse(Source):
    def __init__(self, target):
        self.target = target
        self.name = f"greenhouse:{target['name']}"
        self.token = target["token"]
        self.company = target["name"]
        self.pages_fetched = 0
        self.raw_jobs_discovered = 0
        self.normalized_jobs = 0

    async def _jobs(self) -> list[dict]:
        all_rows: list[dict] = []
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, 51):
            params = {"content": "true"}
            if page > 1:
                params["page"] = page
            data = await get_json(f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs", params=params)
            self.pages_fetched += 1
            rows = data.get("jobs", []) if isinstance(data, dict) else []
            if total is None and isinstance(data, dict):
                total = int((data.get("meta") or {}).get("total") or 0) or None
            added = 0
            for row in rows:
                if not isinstance(row, dict):
                    self.normalization_errors = getattr(self, "normalization_errors", 0) + 1
                    continue
                key = str(row.get("id") or row.get("absolute_url") or "")
                if key and key not in seen:
                    seen.add(key)
                    all_rows.append(row)
                    added += 1
            if not rows or not added or (total is not None and len(all_rows) >= total):
                break
        return all_rows

    async def fetch(self) -> list[NormalizedJob]:
        rows = await self._jobs()
        self.raw_jobs_discovered = len(rows)
        def convert(row):
            offices = row.get("offices") or []
            location = _join_locations(row.get("location") or "") or _join_locations(offices)
            content = _text(row.get("content"))
            url = row.get("absolute_url") or ""
            return NormalizedJob(
                source=self.name, source_job_id=str(row.get("id") or ""), title=_text(row.get("title")),
                company=self.company, company_domain=_domain(url), location=location, country="Philippines" if "philipp" in location.lower() else None,
                description=content, requirements=content, url=url, application_url=url,
                work_setup=detect_remote_type(content, location), remote_type=detect_remote_type(content, location),
                date_posted=_parse_date(row.get("first_published") or row.get("updated_at")),
                employment_type=_text(row.get("employment_type")) or None,
                raw_metadata={"ats": "greenhouse", "department": _join_locations(row.get("departments")), "offices": offices, "canonical_url": url, "discovery_url": url},
            )
        return _normalize_safely(self, rows, convert)


class Lever(Source):
    def __init__(self, target):
        self.target = target
        self.name = f"lever:{target['name']}"
        self.site = target.get("token") or target["site"]
        self.company = target["name"]
        self.pages_fetched = 0
        self.raw_jobs_discovered = 0
        self.normalized_jobs = 0

    async def _jobs(self) -> list[dict]:
        all_rows: list[dict] = []
        cursor = None
        seen: set[str] = set()
        for _ in range(100):
            params = {"mode": "json", "limit": "100"}
            if cursor:
                params["offset"] = cursor
            data = await get_json(f"https://api.lever.co/v0/postings/{self.site}", params=params)
            self.pages_fetched += 1
            if isinstance(data, list):
                rows, next_cursor = data, None
            else:
                rows, next_cursor = data.get("data", []), data.get("next")
            added = 0
            for row in rows:
                if not isinstance(row, dict):
                    self.normalization_errors = getattr(self, "normalization_errors", 0) + 1
                    continue
                key = str(row.get("id") or row.get("hostedUrl") or "")
                if key and key not in seen:
                    seen.add(key)
                    all_rows.append(row)
                    added += 1
            if not next_cursor or not added:
                break
            cursor = next_cursor
        return all_rows

    async def fetch(self) -> list[NormalizedJob]:
        rows = await self._jobs()
        self.raw_jobs_discovered = len(rows)
        def convert(row):
            categories = row.get("categories") or {}
            location = _join_locations(categories.get("location") or row.get("allLocations") or "")
            description = _text(row.get("descriptionPlain") or row.get("description"))
            url = row.get("hostedUrl") or row.get("applyUrl") or ""
            salary, salary_min, salary_max, currency = _salary(row.get("salaryDescription"))
            return NormalizedJob(
                source=self.name, source_job_id=str(row.get("id") or ""), title=_text(row.get("text")), company=self.company,
                company_domain=_domain(url), location=location, country="Philippines" if "philipp" in location.lower() else None,
                description=description, requirements=description, url=url, application_url=row.get("applyUrl") or url,
                work_setup=detect_remote_type(description, location), remote_type=detect_remote_type(description, location), salary=salary,
                salary_min=salary_min, salary_max=salary_max, salary_currency=currency,
                date_posted=_parse_date(row.get("createdAt")), employment_type=_text(categories.get("commitment")) or None,
                raw_metadata={"ats": "lever", "team": _text(categories.get("team")), "department": _text(categories.get("department")), "canonical_url": url, "discovery_url": url},
            )
        return _normalize_safely(self, rows, convert)


class Ashby(Source):
    def __init__(self, target):
        self.target = target
        self.name = f"ashby:{target['name']}"
        self.board = target.get("token") or target["board"]
        self.company = target["name"]
        self.pages_fetched = 0
        self.raw_jobs_discovered = 0
        self.normalized_jobs = 0

    async def fetch(self) -> list[NormalizedJob]:
        data = await get_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.board}", params={"includeCompensation": "true"})
        self.pages_fetched = 1
        rows = data.get("jobs", []) if isinstance(data, dict) else []
        self.raw_jobs_discovered = len(rows)
        listed = [row for row in rows if isinstance(row, dict) and row.get("isListed", True)]
        def convert(row):
            if not row.get("isListed", True):
                return None
            locations = [row.get("location", "")] + [item.get("location", "") for item in row.get("secondaryLocations", []) if isinstance(item, dict)]
            location = _join_locations(locations)
            description = _text(row.get("descriptionPlain") or row.get("descriptionHtml") or row.get("description"))
            url = row.get("jobUrl") or row.get("applyUrl") or ""
            salary, salary_min, salary_max, currency = _salary(row.get("compensation"))
            return NormalizedJob(
                source=self.name, source_job_id=str(row.get("id") or row.get("jobUrl") or ""), title=_text(row.get("title")), company=self.company,
                company_domain=_domain(url), location=location, country="Philippines" if "philipp" in location.lower() else None,
                description=description, requirements=description, url=url, application_url=row.get("applyUrl") or url,
                work_setup=detect_remote_type(description, location), remote_type=detect_remote_type(description, location), salary=salary,
                salary_min=salary_min, salary_max=salary_max, salary_currency=currency,
                date_posted=_parse_date(row.get("publishedAt") or row.get("updatedAt")), employment_type=_text(row.get("employmentType")) or None,
                raw_metadata={"ats": "ashby", "team": _text(row.get("team")), "department": _text(row.get("department")), "canonical_url": url, "discovery_url": url, "secondary_locations": row.get("secondaryLocations", [])},
            )
        return _normalize_safely(self, listed, convert)


class SmartRecruiters(Source):
    """Public SmartRecruiters posting list; details are optional and bounded."""

    def __init__(self, target):
        self.target = target
        self.company = target["name"]
        self.company_id = target.get("token") or target["company"]
        self.name = f"smartrecruiters:{self.company}"
        self.pages_fetched = 0
        self.raw_jobs_discovered = 0
        self.normalized_jobs = 0

    async def fetch(self) -> list[NormalizedJob]:
        rows: list[dict] = []
        offset = 0
        for _ in range(100):
            data = await get_json(f"https://api.smartrecruiters.com/v1/companies/{self.company_id}/postings", params={"limit": 100, "offset": offset})
            self.pages_fetched += 1
            page = data.get("content", []) if isinstance(data, dict) else []
            rows.extend(page)
            total = int(data.get("totalFound") or len(rows)) if isinstance(data, dict) else len(rows)
            if not page or len(rows) >= total:
                break
            offset += len(page)
        self.raw_jobs_discovered = len(rows)
        def convert(row):
            location_data = row.get("location") or {}
            location = _join_locations({"name": ", ".join(str(x) for x in (location_data.get("city"), location_data.get("region"), location_data.get("country")) if x)})
            url = f"https://jobs.smartrecruiters.com/{self.company_id}/{row.get('id')}"
            return NormalizedJob(
                source=self.name, source_job_id=str(row.get("id") or row.get("uuid") or ""), title=_text(row.get("name")), company=self.company,
                location=location, country=_text(location_data.get("country")).upper() or None,
                description=_text(row.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text")) if isinstance(row.get("jobAd"), dict) else "",
                url=url, application_url=url, work_setup="REMOTE" if location_data.get("remote") else None,
                employment_type=_text((row.get("typeOfEmployment") or {}).get("label")) or None,
                raw_metadata={"ats": "smartrecruiters", "department": _text((row.get("department") or {}).get("label")), "canonical_url": url},
            )
        return _normalize_safely(self, rows, convert)


def configured_sources(targets):
    factories = {"greenhouse": Greenhouse, "lever": Lever, "ashby": Ashby, "smartrecruiters": SmartRecruiters}
    result = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("source target must be a JSON object")
        kind = str(target.get("kind") or "").lower()
        if kind not in factories:
            raise ValueError(f"unsupported source kind: {kind!r}")
        if not target.get("name"):
            raise ValueError(f"{kind} source is missing name")
        identifier = target.get("token") or target.get("site") or target.get("board") or target.get("company")
        if not identifier:
            raise ValueError(f"{kind} source {target['name']!r} is missing an identifier")
        result.append(factories[kind](target))
    if not result:
        raise ValueError("no public ATS sources configured")
    return result


def parse_date(value):
    return _parse_date(value)
