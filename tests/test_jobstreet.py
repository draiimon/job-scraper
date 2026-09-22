from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.jobstreet import (
    JOBSTREET_SOURCE_NAME,
    JobStreetAuthRequired,
    JobStreetBrowserSource,
    _normalize_listing,
    _parse_posted,
    authenticate_jobstreet,
    has_storage_state,
    ensure_storage_state,
    jobstreet_sources,
    jobstreet_status,
)


def test_jobstreet_session_defaults_to_private_and_auth_required(tmp_path):
    cfg = Settings(jobstreet_session_path=str(tmp_path / "private" / "jobstreet_session.json"))
    assert not has_storage_state(cfg)
    assert jobstreet_status(cfg) == "INDEXED VIA GOOGLE"
    assert jobstreet_sources(cfg) == []


def test_jobstreet_storage_state_is_accepted_without_exposing_contents(tmp_path):
    path = tmp_path / "jobstreet_session.json"
    path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    cfg = Settings(jobstreet_session_path=str(path))
    assert has_storage_state(cfg)
    assert jobstreet_status(cfg) == "INDEXED VIA GOOGLE"
    assert jobstreet_sources(cfg) == []

def test_render_secret_restores_private_jobstreet_session(tmp_path):
    import base64
    encoded=base64.b64encode(b'{"cookies": [], "origins": []}').decode()
    path=tmp_path / 'private' / 'jobstreet_session.json'
    cfg=Settings(jobstreet_session_path=str(path),jobstreet_session_state_b64=encoded)
    assert ensure_storage_state(cfg) and has_storage_state(cfg)
    assert path.read_text(encoding='utf-8') == '{"cookies":[],"origins":[]}'


def test_jobstreet_normalizes_visible_discovery_listing():
    item = _normalize_listing(
        {
            "url": "https://ph.jobstreet.com/job/123",
            "title": "Junior Cloud Engineer",
            "text": "Junior Cloud Engineer\nCloud PH\nMakati, Philippines\nPosted 2 hours ago\nAWS Docker Linux",
            "posted": "Posted 2 hours ago",
        }
    )
    assert item is not None
    assert item.source == JOBSTREET_SOURCE_NAME
    assert item.application_url == item.url
    assert item.raw_metadata["discovery_only"] is True
    assert item.raw_metadata["auth"] == "google_oauth"
    assert item.date_posted is not None


def test_jobstreet_relative_date_is_timezone_aware():
    posted = _parse_posted("1 day ago", datetime(2026, 9, 14, tzinfo=timezone.utc))
    assert posted == datetime(2026, 9, 13, tzinfo=timezone.utc)


class _FakeLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.first = self

    async def count(self):
        return 1

    async def click(self):
        self.page.actions.append(("click", self.kind))
        if "google" in self.kind:
            self.page.url = "https://ph.jobstreet.com/"

    async def inner_text(self, timeout=None):
        return "My Account"

    async def evaluate_all(self, script):
        return self.page.rows


class _FakePage:
    def __init__(self, rows=None, url="https://ph.jobstreet.com/", expired=False):
        self.rows = rows or []
        self.url = url
        self.expired = expired
        self.actions = []

    async def goto(self, url, **kwargs):
        self.actions.append(("goto", url))
        self.url = "https://ph.jobstreet.com/login" if self.expired else url

    def get_by_role(self, role, name):
        return _FakeLocator(self, f"{role}:google")

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def wait_for_timeout(self, milliseconds):
        return None


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]

    async def new_page(self):
        return self.page

    async def storage_state(self, path):
        with open(path, "w", encoding="utf-8") as saved:
            saved.write('{"cookies": [], "origins": []}')

    async def close(self):
        return None


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.launch_options = None

    async def new_context(self, **kwargs):
        return _FakeContext(self.page)

    async def close(self):
        return None


class _FakePlaywrightContext:
    def __init__(self, page):
        self.page = page
        self.browser = _FakeBrowser(page)
        self.chromium = SimpleNamespace(
            launch=self.launch,
            connect_over_cdp=self.connect_over_cdp,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def launch(self, **kwargs):
        self.browser.launch_options = kwargs
        return self.browser

    async def connect_over_cdp(self, endpoint):
        self.browser.cdp_endpoint = endpoint
        self.browser.contexts = [_FakeContext(self.page)]
        return self.browser


@pytest.mark.asyncio
async def test_manual_auth_uses_installed_chrome_cdp_without_credential_automation(monkeypatch, tmp_path):
    import playwright.async_api
    import src.jobstreet as jobstreet_module

    page = _FakePage()
    playwright_context = _FakePlaywrightContext(page)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: playwright_context)
    chrome_command = []
    class _FakeProcess:
        def terminate(self):
            chrome_command.append("terminate")
        def wait(self, timeout=None):
            chrome_command.append(("wait", timeout))
        def kill(self):
            chrome_command.append("kill")
    monkeypatch.setattr(jobstreet_module, "_find_google_chrome", lambda: "C:\\Chrome\\chrome.exe")
    monkeypatch.setattr(jobstreet_module, "_wait_for_cdp", lambda port: None)
    monkeypatch.setattr(
        jobstreet_module.subprocess,
        "Popen",
        lambda command, **kwargs: (chrome_command.append(command) or _FakeProcess()),
    )
    cfg = Settings(
        jobstreet_session_path=str(tmp_path / "private" / "jobstreet_session.json"),
        jobstreet_auth_timeout_seconds=1,
    )

    saved = await authenticate_jobstreet(cfg)

    assert saved.is_file()
    assert playwright_context.browser.cdp_endpoint.startswith("http://127.0.0.1:")
    command = chrome_command[0]
    assert command[0] == "C:\\Chrome\\chrome.exe"
    assert any(argument.startswith("--user-data-dir=") for argument in command)
    assert any(argument.startswith("--remote-debugging-port=") for argument in command)
    assert not any(action[0] == "click" for action in page.actions)
    assert not any(action[0] in {"fill", "press", "type"} for action in page.actions)


@pytest.mark.asyncio
async def test_authenticated_source_discovers_normalized_listings(monkeypatch, tmp_path):
    import playwright.async_api

    state_path = tmp_path / "jobstreet_session.json"
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    page = _FakePage(
        rows=[
            {
                "url": "https://ph.jobstreet.com/job/456",
                "title": "Cloud Support Engineer",
                "text": "Cloud Support Engineer\nCloud PH\nManila, Philippines\nToday\nAWS Linux",
                "posted": "Today",
            }
        ]
    )
    playwright_context = _FakePlaywrightContext(page)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: playwright_context)
    cfg = Settings(jobstreet_session_path=str(state_path), jobstreet_search_terms_json='["Cloud"]')

    jobs = await JobStreetBrowserSource(cfg).fetch()

    assert len(jobs) == 1
    assert jobs[0].title == "Cloud Support Engineer"
    assert "keywords=Cloud" in page.actions[0][1]
    assert playwright_context.browser.launch_options == {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }


@pytest.mark.asyncio
async def test_expired_session_reports_auth_required_and_records_status(monkeypatch, tmp_path):
    import playwright.async_api

    state_path = tmp_path / "jobstreet_session.json"
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    page = _FakePage(url="https://ph.jobstreet.com/login", expired=True)
    playwright_context = _FakePlaywrightContext(page)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: playwright_context)

    class Repo:
        def __init__(self):
            self.values = {}

        def set_state(self, key, value):
            self.values[key] = value

    repo = Repo()
    cfg = Settings(jobstreet_session_path=str(state_path))
    with pytest.raises(JobStreetAuthRequired, match="AUTH REQUIRED"):
        await JobStreetBrowserSource(cfg, repo).fetch()
    assert repo.values["jobstreet_auth_status"]["status"] == "AUTH REQUIRED"
