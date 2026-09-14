"""Human-in-the-loop JobStreet connection state.

This module deliberately never handles a Google password, OTP, CAPTCHA answer,
or Browserless Live URL outside the requesting user's private interaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import base64
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .models import SourceConnection, SourceConnectionRequest

SOURCE = "jobstreet"
log = logging.getLogger(__name__)


class ConnectionError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fernet(cfg: Settings) -> Fernet:
    # The application secret is the only operator-managed secret for this
    # feature.  Domain separation keeps this ciphertext key independent from
    # any other use of APP_SECRET_KEY, while avoiding a second secret that can
    # drift out of sync across instances.
    if not cfg.app_secret_key:
        raise ConnectionError("Set APP_SECRET_KEY before connecting JobStreet.")
    material = hashlib.sha256(
        b"after-hours-jobstreet-session-v1\x00" + cfg.app_secret_key.encode("utf-8")
    ).digest()
    value = base64.urlsafe_b64encode(material)
    try:
        return Fernet(value)
    except (ValueError, TypeError) as exc:
        raise ConnectionError("JobStreet session encryption configuration is invalid.") from exc


def browserless_ready(cfg: Settings) -> bool:
    return bool(cfg.browserless_api_token and cfg.browserless_endpoint and cfg.app_secret_key)


def create_request(repo, discord_user_id: int | str, ttl_seconds: int = 600) -> str:
    """Create one opaque, short-lived private connection link token."""
    token = secrets.token_urlsafe(32)
    with repo.sessions.begin() as session:
        session.add(SourceConnectionRequest(
            discord_user_id=str(discord_user_id), source=SOURCE, token_digest=_digest(token),
            nonce=secrets.token_hex(24), expires_at=_now() + timedelta(seconds=max(60, min(ttl_seconds, 600))),
        ))
    return token


def request_for_token(
    repo, token: str, consume: bool = False, include_used: bool = False
) -> SourceConnectionRequest | None:
    with repo.sessions.begin() as session:
        request = session.scalar(select(SourceConnectionRequest).where(
            SourceConnectionRequest.source == SOURCE,
            SourceConnectionRequest.token_digest == _digest(token),
        ))
        expires = request.expires_at.replace(tzinfo=timezone.utc) if request and request.expires_at.tzinfo is None else (request.expires_at if request else None)
        if not request or (request.used_at and not include_used) or expires <= _now():
            return None
        if consume:
            request.used_at = _now(); request.status = "USED"
        return request


def connection_status(cfg: Settings, repo, discord_user_id: int | str | None = None) -> str:
    with repo.sessions() as session:
        query = select(SourceConnection).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        record = session.scalars(query.order_by(SourceConnection.updated_at.desc())).first()
    if record:
        return record.status
    return "AUTH REQUIRED"


def save_verified_session(cfg: Settings, repo, discord_user_id: int | str, storage_state: dict) -> None:
    """Encrypt valid Playwright state before it ever reaches persistence."""
    if not isinstance(storage_state, dict) or not isinstance(storage_state.get("cookies"), list):
        raise ConnectionError("JobStreet did not provide valid browser session state.")
    ciphertext = _fernet(cfg).encrypt(json.dumps(storage_state, separators=(",", ":")).encode("utf-8")).decode("ascii")
    now = _now()
    with repo.sessions.begin() as session:
        record = session.scalar(select(SourceConnection).where(
            SourceConnection.source == SOURCE, SourceConnection.discord_user_id == str(discord_user_id)
        ))
        if not record:
            record = SourceConnection(discord_user_id=str(discord_user_id), source=SOURCE); session.add(record)
        record.status = "READY"; record.encrypted_session = ciphertext
        record.connected_at = record.connected_at or now; record.last_verified_at = now
        record.updated_at = now; record.last_error = None
    if hasattr(repo, "set_setting"):
        repo.set_setting("jobstreet_last_verified", now.isoformat())
        repo.set_setting("jobstreet_status", "READY")


def restore_latest_session(cfg: Settings, repo, destination: Path) -> bool:
    """Decrypt a READY session into private ephemeral storage for one browser run."""
    with repo.sessions() as session:
        record = session.scalars(select(SourceConnection).where(
            SourceConnection.source == SOURCE, SourceConnection.status == "READY"
        ).order_by(SourceConnection.last_verified_at.desc())).first()
        ciphertext = record.encrypted_session if record else None
    if not ciphertext:
        return False
    try:
        state = json.loads(_fernet(cfg).decrypt(ciphertext.encode("ascii"), ttl=None).decode("utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError
    except (InvalidToken, UnicodeError, ValueError, json.JSONDecodeError):
        mark_status(repo, None, "SESSION EXPIRED", "Stored session could not be restored.")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try: os.chmod(destination.parent, 0o700)
    except OSError: pass
    destination.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    try: os.chmod(destination, 0o600)
    except OSError: pass
    return True


def mark_status(repo, discord_user_id: int | str | None, status: str, error: str | None = None) -> None:
    with repo.sessions.begin() as session:
        query = select(SourceConnection).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        records = session.scalars(query).all()
        if discord_user_id is not None and not records:
            records = [SourceConnection(discord_user_id=str(discord_user_id), source=SOURCE)]
            session.add(records[0])
        for record in records:
            record.status = status; record.updated_at = _now(); record.last_error = error
    if hasattr(repo, "set_setting"):
        repo.set_setting("jobstreet_status", status)


def disconnect(repo, discord_user_id: int | str) -> bool:
    with repo.sessions.begin() as session:
        record = session.scalar(select(SourceConnection).where(
            SourceConnection.source == SOURCE, SourceConnection.discord_user_id == str(discord_user_id)
        ))
        if not record:
            return False
        record.encrypted_session = None; record.status = "AUTH REQUIRED"; record.updated_at = _now(); record.last_error = None
    return True


def browserless_cdp_endpoint(cfg: Settings) -> str:
    """Construct a connection URL without logging the bearer token."""
    if not browserless_ready(cfg):
        raise ConnectionError("Browserless and session encryption must be configured first.")
    base = str(cfg.browserless_endpoint).rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote_plus(str(cfg.browserless_api_token))}"


@dataclass
class InteractiveSession:
    """Process-local state for one private Browserless live session.

    The live URL is intentionally not persisted. Browserless returns a
    one-time URL without the API token, and the signed setup link is the only
    way the application exposes it to the requesting Discord user.
    """

    nonce: str
    discord_user_id: str
    status: str = "STARTING"
    live_url: str | None = None
    error: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


_interactive_sessions: dict[str, InteractiveSession] = {}


def _request_status(repo, nonce: str, status: str) -> None:
    with repo.sessions.begin() as session:
        request = session.scalar(select(SourceConnectionRequest).where(
            SourceConnectionRequest.nonce == nonce,
            SourceConnectionRequest.source == SOURCE,
        ))
        if request:
            request.status = status


def _safe_request_status(repo, nonce: str, status: str) -> None:
    try:
        _request_status(repo, nonce, status)
    except Exception as exc:
        log.warning("jobstreet_request_status_update_failed", extra={"error_type": type(exc).__name__})


def _safe_mark_status(repo, discord_user_id: str, status: str, error: str | None = None) -> None:
    try:
        mark_status(repo, discord_user_id, status, error)
    except Exception as exc:
        log.warning("jobstreet_connection_status_update_failed", extra={"error_type": type(exc).__name__})


def _safe_session_error(exc: Exception) -> str:
    """Return a user-safe error without copying Browserless URLs or secrets."""
    if isinstance(exc, TimeoutError):
        return "The private browser session timed out before authentication finished."
    if isinstance(exc, SQLAlchemyError):
        return "The service database is temporarily unavailable. Try again in a moment."
    if isinstance(exc, ConnectionError):
        return str(exc)
    return "The private browser session could not be completed."


async def _run_interactive_session(cfg: Settings, repo, session: InteractiveSession) -> None:
    browser = None
    stage = "starting"
    try:
        from playwright.async_api import async_playwright
        from .jobstreet import _is_authenticated

        async with async_playwright() as playwright:
            stage = "connect_over_cdp"
            browser = await playwright.chromium.connect_over_cdp(browserless_cdp_endpoint(cfg))
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            stage = "open_jobstreet_login"
            page = await context.new_page()
            login_url = cfg.jobstreet_login_url or f"{cfg.jobstreet_base_url.rstrip('/')}/oauth/login"
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)

            stage = "create_live_url"
            cdp = await context.new_cdp_session(page)
            response = await cdp.send(
                "Browserless.liveURL",
                {"quality": 70, "showBrowserInterface": True},
            )
            session.live_url = str(response.get("liveURL") or "")
            if not session.live_url:
                raise RuntimeError("Browserless did not return a live URL.")
            session.status = "WAITING_FOR_USER"
            session.ready.set()
            _safe_request_status(repo, session.nonce, "RUNNING")

            stage = "wait_for_user_login"
            deadline = asyncio.get_running_loop().time() + cfg.jobstreet_auth_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if await _is_authenticated(page):
                    state = await context.storage_state()
                    save_verified_session(cfg, repo, session.discord_user_id, state)
                    _safe_request_status(repo, session.nonce, "COMPLETE")
                    session.status = "READY"
                    session.ready.set()
                    return
                await page.wait_for_timeout(1_500)
            raise TimeoutError
    except Exception as exc:
        session.status = "ERROR"
        session.error = _safe_session_error(exc)
        session.ready.set()
        _safe_request_status(repo, session.nonce, "ERROR")
        _safe_mark_status(repo, session.discord_user_id, "AUTH REQUIRED", session.error)
        # Do not log exception text: Playwright/Browserless errors can echo a
        # connection URL or a provider response containing sensitive data.
        log.warning(
            "jobstreet_browser_session_failed stage=%s error_type=%s",
            stage,
            type(exc).__name__,
            exc_info=True,
        )
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


async def start_interactive_session(cfg: Settings, repo, request: SourceConnectionRequest) -> InteractiveSession:
    """Start Browserless and return after a private live URL is available."""
    if not browserless_ready(cfg):
        raise ConnectionError("JobStreet connection is not configured on this service.")
    existing = _interactive_sessions.get(request.nonce)
    if existing:
        await existing.ready.wait()
        return existing
    session = InteractiveSession(request.nonce, request.discord_user_id)
    _interactive_sessions[request.nonce] = session
    mark_status(repo, request.discord_user_id, "CONNECTING")
    session.task = asyncio.create_task(_run_interactive_session(cfg, repo, session))
    await session.ready.wait()
    return session


def interactive_session_for_request(request: SourceConnectionRequest) -> InteractiveSession | None:
    return _interactive_sessions.get(request.nonce)


def has_managed_connection(repo, discord_user_id: int | str | None = None) -> bool:
    with repo.sessions() as session:
        query = select(SourceConnection.id).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        return session.scalar(query.limit(1)) is not None


def cancel_interactive_sessions(discord_user_id: int | str) -> None:
    for session in list(_interactive_sessions.values()):
        if session.discord_user_id != str(discord_user_id):
            continue
        if session.task and not session.task.done():
            session.task.cancel()
        _interactive_sessions.pop(session.nonce, None)
