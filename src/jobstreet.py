from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from .config import Settings
from .jobs import NormalizedJob
from .jobstreet_link import connection_status, has_managed_connection, restore_latest_session
from .sources import Source, SourceError

log = logging.getLogger(__name__)

JOBSTREET_SOURCE_NAME = "jobstreet:google-session"
DEFAULT_JOBSTREET_BASE_URL = "https://ph.jobstreet.com"


class JobStreetAuthRequired(SourceError):
    """The saved browser session no longer authenticates to JobStreet."""


def session_path(cfg: Settings) -> Path:
    return Path(cfg.jobstreet_session_path).expanduser()


def has_storage_state(cfg: Settings) -> bool:
    path = session_path(cfg)
    if not path.is_file():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and isinstance(state.get("cookies"), list) and isinstance(
        state.get("origins", []), list
    )


def ensure_storage_state(cfg: Settings) -> bool:
    """Materialize an operator-provided Render secret into private temp storage.

    The secret is never logged and the file remains outside source control. On
    Render's ephemeral disk it is recreated from the secret after each restart.
    """
    if has_storage_state(cfg):
        return True
    encoded=(cfg.jobstreet_session_state_b64 or '').strip()
    if not encoded:
        return False
    try:
        raw=base64.b64decode(encoded,validate=True)
        state=json.loads(raw.decode('utf-8'))
        if not isinstance(state,dict) or not isinstance(state.get('cookies'),list) or not isinstance(state.get('origins',[]),list):
            return False
    except (ValueError,UnicodeDecodeError):
        return False
    target=session_path(cfg); target.parent.mkdir(parents=True,exist_ok=True)
    try: os.chmod(target.parent,0o700)
    except OSError: pass
    try:
        target.write_text(json.dumps(state,separators=(',',':')),encoding='utf-8')
        os.chmod(target,0o600)
    except OSError:
        return False
    return has_storage_state(cfg)


def _auth_state(cfg: Settings, repo=None) -> str:
    if repo is not None and hasattr(repo, "sessions"):
        linked = connection_status(cfg, repo)
        if linked != "AUTH REQUIRED":
            return linked
    if not ensure_storage_state(cfg):
        return "AUTH REQUIRED"
    if repo is None:
        return "READY"
    state = repo.state("jobstreet_auth_status")
    if not isinstance(state, dict) or state.get("status") != "AUTH REQUIRED":
        return "READY"
    try:
        if session_path(cfg).stat().st_mtime > float(state.get("session_mtime", 0)):
            return "READY"
    except (OSError, TypeError, ValueError):
        pass
    return "AUTH REQUIRED"


def jobstreet_status(cfg: Settings, repo=None) -> str:
    return _auth_state(cfg, repo)


def brightdata_jobstreet_status(cfg: Settings) -> str:
    if (
        cfg.brightdata_enabled
        and cfg.brightdata_api_token
        and cfg.brightdata_jobstreet_dataset_id
        and cfg.brightdata_inputs("jobstreet")
    ):
        return "READY"
    return "DISABLED"


def _mark_auth_state(cfg: Settings, repo, status: str) -> None:
    if repo is None:
        return
    try:
        modified = session_path(cfg).stat().st_mtime
    except OSError:
        modified = 0
    repo.set_state("jobstreet_auth_status", {"status": status, "session_mtime": modified})


def _looks_like_login_url(url: str) -> bool:
    lowered = url.lower()
    return (
        "accounts.google." in lowered
        or any(marker in lowered for marker in ("/login", "/signin", "/sign-in", "auth/"))
    )


def _body_indicates_auth_required(body: str) -> bool:
    lowered = body.lower()
    if any(marker in lowered for marker in ("sign out", "log out", "my profile", "my account")):
        return False
    return any(marker in lowered for marker in ("continue with google", "sign in", "log in"))


async def _is_authenticated(page) -> bool:
    if _looks_like_login_url(page.url):
        return False
    try:
        body = await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return False
    return not _body_indicates_auth_required(body)


async def _click_google_sign_in(page) -> bool:
    import re as _re

    patterns = (
        _re.compile(r"continue\s+with\s+google", _re.IGNORECASE),
        _re.compile(r"sign\s+in\s+with\s+google", _re.IGNORECASE),
    )
    for pattern in patterns:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=pattern).first
                if await locator.count():
                    await locator.click()
                    return True
            except Exception:
                continue
    try:
        locator = page.locator("button, a").filter(has_text=re.compile(r"google", re.IGNORECASE)).first
        if await locator.count():
            await locator.click()
            return True
    except Exception:
        pass
    return False


async def authenticate_jobstreet(cfg: Settings | None = None) -> Path:
    """Open JobStreet and wait for the user to finish Google authentication."""

    cfg = cfg or Settings()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required. Install requirements.txt first.") from exc

    target = session_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    login_url = cfg.jobstreet_login_url or f"{cfg.jobstreet_base_url.rstrip('/')}/login"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            clicked = await _click_google_sign_in(page)
            if clicked:
                print(
                    "Google sign-in opened. Complete Google authentication, 2FA, "
                    "security prompts, and consent manually in the browser."
                )
            else:
                print(
                    "JobStreet login opened. Choose Continue with Google and finish "
                    "all authentication steps manually in the browser."
                )
            print("Waiting for JobStreet to confirm the authenticated session...")

            deadline = asyncio.get_running_loop().time() + cfg.jobstreet_auth_timeout_seconds
            authenticated = False
            while asyncio.get_running_loop().time() < deadline:
                if await _is_authenticated(page):
                    authenticated = True
                    break
                await page.wait_for_timeout(1_500)
            if not authenticated:
                raise RuntimeError("JobStreet authentication was not completed before the timeout.")

            await context.storage_state(path=str(target))
            os.chmod(target, 0o600)
            if not has_storage_state(cfg):
                raise RuntimeError("Playwright did not save a valid JobStreet session state.")
            print("Authenticated JobStreet session saved privately for discovery only.")
            return target
        finally:
            await context.close()
            await browser.close()


def _parse_posted(value: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    text = value.strip().lower()
    if not text:
        return None
    if "today" in text or "just posted" in text:
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)
    match = re.search(r"(\d+)\s*(minute|hour|day|week)s?\s*ago", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        divisor = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}[unit]
        return now - timedelta(seconds=amount * divisor)
    for fmt in ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _work_setup(text: str) -> str | None:
    lowered = text.lower()
    if "hybrid" in lowered:
        return "Hybrid"
    if "remote" in lowered:
        return "Remote"
    if "on-site" in lowered or "onsite" in lowered:
        return "On-site"
    return None


def _normalize_listing(row: dict) -> NormalizedJob | None:
    url = str(row.get("url") or "").strip()
    title = " ".join(str(row.get("title") or "").split())
    if not title or not url.startswith(("https://", "http://")):
        return None
    lines = [line.strip() for line in str(row.get("text") or "").splitlines() if line.strip()]
    company = " ".join(str(row.get("company") or "").split()) or (
        lines[1] if len(lines) > 1 and lines[1] != title else "Unknown company"
    )
    location = " ".join(str(row.get("location") or "").split())
    if not location:
        location = next(
            (
                line
                for line in lines
                if any(place in line.lower() for place in ("philippines", "manila", "makati", "remote", "hybrid"))
            ),
            "",
        )
    text = " ".join(lines)
    source_job_id = urlparse(url).path.rstrip("/").split("/")[-1] or url
    return NormalizedJob(
        source=JOBSTREET_SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        work_setup=_work_setup(text),
        description=text,
        url=url,
        application_url=url,
        date_posted=_parse_posted(str(row.get("posted") or "")),
        raw_metadata={"provider": "jobstreet", "auth": "google_oauth", "discovery_only": True},
    )


async def _visible_listing_rows(page) -> list[dict]:
    return await page.locator("a[href]").evaluate_all(
        """anchors => {
            const rows = [];
            const seen = new Set();
            for (const anchor of anchors) {
                const url = anchor.href || "";
                if (!/\\/jobs?\\//i.test(url) || seen.has(url)) continue;
                const rect = anchor.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                const card = anchor.closest('article,[data-automation*="job-card"],li') || anchor.parentElement;
                const text = (card?.innerText || anchor.innerText || "").trim();
                if (!text) continue;
                seen.add(url);
                const time = card?.querySelector("time");
                rows.push({
                    url,
                    title: (anchor.innerText || "").trim(),
                    text,
                    posted: time?.dateTime || time?.innerText || ""
                });
            }
            return rows;
        }"""
    )


class JobStreetBrowserSource(Source):
    def __init__(self, cfg: Settings, repo=None):
        self.cfg = cfg
        self.repo = repo
        self.name = JOBSTREET_SOURCE_NAME

    async def fetch(self) -> list[NormalizedJob]:
        runtime_state = session_path(self.cfg).with_name("jobstreet_runtime_session.json")
        linked_state = False
        if self.repo is not None and hasattr(self.repo, "sessions"):
            linked_state = await asyncio.to_thread(restore_latest_session, self.cfg, self.repo, runtime_state)
        if not linked_state and not ensure_storage_state(self.cfg):
            raise JobStreetAuthRequired("AUTH REQUIRED")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceError("Playwright is required for JobStreet discovery") from exc

        jobs: list[NormalizedJob] = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=str(runtime_state if linked_state else session_path(self.cfg)))
                page = await context.new_page()
                try:
                    for term in self.cfg.jobstreet_search_terms:
                        url = (
                            f"{self.cfg.jobstreet_base_url.rstrip('/')}/jobs"
                            f"?keywords={quote_plus(term)}&location={quote_plus(self.cfg.jobstreet_location)}"
                        )
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        if not await _is_authenticated(page):
                            raise JobStreetAuthRequired("AUTH REQUIRED")
                        for row in await _visible_listing_rows(page):
                            job = _normalize_listing(row)
                            if job and all(existing.url != job.url for existing in jobs):
                                jobs.append(job)
                                if len(jobs) >= self.cfg.jobstreet_max_results:
                                    break
                        if len(jobs) >= self.cfg.jobstreet_max_results:
                            break
                finally:
                    await context.close()
                    await browser.close()
        except JobStreetAuthRequired:
            await asyncio.to_thread(_mark_auth_state, self.cfg, self.repo, "AUTH REQUIRED")
            raise
        finally:
            if linked_state:
                try:
                    runtime_state.unlink(missing_ok=True)
                except OSError:
                    pass
        await asyncio.to_thread(_mark_auth_state, self.cfg, self.repo, "READY")
        return jobs


def jobstreet_sources(cfg: Settings, repo=None) -> list[Source]:
    if not getattr(cfg, 'jobstreet_enabled', True):
        return []
    if repo is not None and connection_status(cfg, repo) == "READY":
        return [JobStreetBrowserSource(cfg, repo)]
    # Once the managed flow has been used, do not silently fall back to an
    # older local/Render session after the user disconnects or needs reauth.
    # The base64 state remains supported for deployments that have never used
    # the managed flow.
    if repo is not None and has_managed_connection(repo):
        return []
    if not ensure_storage_state(cfg):
        return []
    return [JobStreetBrowserSource(cfg, repo)]
