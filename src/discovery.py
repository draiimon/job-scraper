from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

ATS_PATTERNS = (
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"https?://careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
)
ALLOWED_ATS_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "careers.smartrecruiters.com"}
DEFAULT_SEARCH_QUERIES = (
    'site:boards.greenhouse.io Philippines (software OR cloud OR devops OR QA)',
    'site:job-boards.greenhouse.io Philippines (developer OR engineer)',
    'site:jobs.lever.co Philippines (software OR cloud OR support)',
    'site:jobs.ashbyhq.com Philippines (developer OR engineer)',
    'site:careers.smartrecruiters.com Philippines (software OR technology)',
)


def _safe_url(value: str) -> str | None:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return None
    except ValueError:
        pass
    return value.split("#", 1)[0]


def _target_from_url(url: str, method: str) -> dict | None:
    for provider, pattern in ATS_PATTERNS:
        match = pattern.search(url)
        if not match:
            continue
        identifier = match.group(1)
        name = identifier.replace("-", " ").replace("_", " ").title()
        if provider == "smartrecruiters":
            return {"kind": provider, "name": name, "company": identifier, "career_url": url, "domain": urlsplit(url).hostname, "discovery_method": method}
        key = "site" if provider == "lever" else "board" if provider == "ashby" else "token"
        return {"kind": provider, "name": name, key: identifier, "career_url": url, "domain": urlsplit(url).hostname, "discovery_method": method}
    return None


def target_from_job_url(url: str, company: str | None = None) -> dict | None:
    """Promote an ATS link emitted by another legitimate job source."""
    safe = _safe_url(str(url or ""))
    if not safe or urlsplit(safe).hostname not in ALLOWED_ATS_HOSTS:
        return None
    target = _target_from_url(safe, "job_outbound_link")
    if target and company:
        target["name"] = str(company).strip() or target["name"]
    return target


def _seed_urls(cfg) -> list[dict]:
    raw = cfg.source_discovery_seeds_json.strip()
    if not raw:
        try:
            raw = Path(cfg.source_discovery_seed_path).read_text(encoding="utf-8")
        except OSError:
            return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SOURCE_DISCOVERY_SEEDS_JSON must contain a JSON array") from exc
    if not isinstance(values, list):
        raise ValueError("SOURCE_DISCOVERY_SEEDS_JSON must contain a JSON array")
    result = []
    for value in values[: max(1, int(cfg.source_discovery_max_seeds))]:
        if isinstance(value, str):
            url, company = value, None
        elif isinstance(value, dict):
            url, company = value.get("url"), value.get("company")
        else:
            continue
        safe = _safe_url(str(url or ""))
        if safe:
            result.append({"url": safe, "company": company})
    return result


def _search_queries(cfg) -> list[str]:
    raw = str(getattr(cfg, "source_discovery_search_queries_json", "") or "").strip()
    if not raw:
        return list(DEFAULT_SEARCH_QUERIES)
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SOURCE_DISCOVERY_SEARCH_QUERIES_JSON must contain a JSON array") from exc
    if not isinstance(values, list):
        raise ValueError("SOURCE_DISCOVERY_SEARCH_QUERIES_JSON must contain a JSON array")
    return [str(value).strip() for value in values[:10] if str(value).strip()]


async def _public_search_targets(cfg) -> list[dict]:
    """Use Bing's public RSS search output as a bounded discovery input.

    This performs at most ten ordinary public searches per discovery interval,
    follows no result pages, and accepts only allowlisted public ATS URLs.  It
    is therefore discovery—not an uncontrolled crawler—and a search outage is
    isolated from configured and seed-based sources.
    """
    if not getattr(cfg, "source_discovery_search_enabled", True):
        return []
    limit = max(1, min(100, int(getattr(cfg, "source_discovery_max_search_results", 50))))
    targets: dict[tuple[str, str], dict] = {}
    timeout = max(3, float(cfg.source_discovery_timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for query in _search_queries(cfg):
            try:
                response = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query, "format": "rss", "count": min(50, limit)},
                    headers={"User-Agent": "PH-Job-Agent/1.0 (+bounded-source-discovery)"},
                )
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("search response exceeded discovery size limit")
                root = ET.fromstring(response.text)
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                log.warning("source_discovery_search_failed error=%s", type(exc).__name__)
                continue
            for node in root.findall(".//item/link"):
                safe = _safe_url(node.text or "")
                if not safe or urlsplit(safe).hostname not in ALLOWED_ATS_HOSTS:
                    continue
                target = _target_from_url(safe, "public_web_search")
                if not target:
                    continue
                identifier = target.get("token") or target.get("site") or target.get("board") or target.get("company")
                targets[(target["kind"], identifier)] = target
                if len(targets) >= limit:
                    return list(targets.values())
    return list(targets.values())


async def discover_sources(cfg, repo) -> list[dict]:
    """Discover only public ATS links from bounded HTTPS seeds.

    Direct ATS board URLs are verified by their host pattern. Other seeds are
    fetched once and only links to the explicitly supported ATS hosts are
    extracted. No search-engine scraping, login, CAPTCHA, proxy, or recursive
    uncontrolled crawling is used.
    """
    if not cfg.source_discovery_enabled:
        return []
    discovered: dict[tuple[str, str], dict] = {}
    for target in await _public_search_targets(cfg):
        identifier = target.get("token") or target.get("site") or target.get("board") or target.get("company")
        discovered[(target["kind"], identifier)] = target
    for seed in _seed_urls(cfg):
        url = seed["url"]
        direct = _target_from_url(url, "seed_ats_url")
        if direct:
            if seed.get("company"):
                direct["name"] = str(seed["company"])
            discovered[(direct["kind"], direct.get("token") or direct.get("site") or direct.get("board") or direct.get("company"))] = direct
            continue
        try:
            async with httpx.AsyncClient(timeout=max(3, cfg.source_discovery_timeout_seconds), follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": "PH-Job-Agent/1.0 (+bounded-source-discovery)"})
                response.raise_for_status()
                final_url = _safe_url(str(response.url))
                if not final_url:
                    continue
                body = response.text[:2_000_000]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("source_discovery_seed_failed seed=%s error=%s", urlsplit(url).hostname, type(exc).__name__)
            continue
        links = {urljoin(final_url, match) for match in re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", body, re.I)}
        for provider, pattern in ATS_PATTERNS:
            for identifier in pattern.findall(body):
                host = {'greenhouse': 'boards.greenhouse.io', 'lever': 'jobs.lever.co', 'ashby': 'jobs.ashbyhq.com', 'smartrecruiters': 'careers.smartrecruiters.com'}[provider]
                links.add(f'https://{host}/{identifier}')
        for link in links:
            safe = _safe_url(link)
            if not safe or urlsplit(safe).hostname not in ALLOWED_ATS_HOSTS:
                continue
            target = _target_from_url(safe, "career_page_link")
            if target:
                target["name"] = str(seed.get("company") or target["name"])
                discovered[(target["kind"], target.get("token") or target.get("site") or target.get("board") or target.get("company"))] = target
    persisted = []
    for target in discovered.values():
        row = await asyncio.to_thread(repo.upsert_source_target, target, target.get("discovery_method", "source_discovery"))
        persisted.append({**target, "registry_id": row.id})
    return persisted
