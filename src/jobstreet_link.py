"""Secure JobStreet session handoff.

The production scanner runs Playwright directly on Render.  Authentication is
the one human-in-the-loop step: Discord creates a short-lived signed link,
which downloads a temporary Windows connector.  That connector opens local
Playwright Chromium, waits for the user to finish authentication, and uploads
only the resulting storage state.  The server encrypts it before persistence.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

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
    if not cfg.app_secret_key:
        raise ConnectionError("Set APP_SECRET_KEY before connecting JobStreet.")
    material = hashlib.sha256(
        b"after-hours-jobstreet-session-v2\x00" + cfg.app_secret_key.encode("utf-8")
    ).digest()
    try:
        return Fernet(base64.urlsafe_b64encode(material))
    except (ValueError, TypeError) as exc:
        raise ConnectionError("JobStreet session encryption configuration is invalid.") from exc


def _issue_token(secret: str | None, user_id: int | str, nonce: str, expires_at: datetime) -> str:
    """Issue a signed token while retaining a database digest for replay control."""
    if not secret:
        # Test fixtures can exercise the database replay behavior without an
        # application secret. Production requests always use the signed form.
        return secrets.token_urlsafe(32)
    payload = json.dumps(
        {"u": str(user_id), "n": nonce, "e": int(expires_at.timestamp()), "r": secrets.token_urlsafe(18)},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload + signature)
        .decode("ascii")
        .rstrip("=")
    )


def _verify_token(secret: str | None, token: str, request: SourceConnectionRequest) -> bool:
    if not secret:
        return True
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        values = json.loads(payload.decode("utf-8"))
        return (
            str(values["u"]) == str(request.discord_user_id)
            and values["n"] == request.nonce
            and int(values["e"]) >= int(time.time())
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def create_request(
    repo,
    discord_user_id: int | str,
    ttl_seconds: int = 600,
    app_secret_key: str | None = None,
) -> str:
    """Create one opaque, signed, short-lived private connection link token."""
    nonce = secrets.token_hex(24)
    expires_at = _now() + timedelta(seconds=max(60, min(ttl_seconds, 600)))
    token = _issue_token(app_secret_key, discord_user_id, nonce, expires_at)
    with repo.sessions.begin() as session:
        session.add(
            SourceConnectionRequest(
                discord_user_id=str(discord_user_id),
                source=SOURCE,
                token_digest=_digest(token),
                nonce=nonce,
                expires_at=expires_at,
            )
        )
    return token


def request_for_token(
    repo,
    token: str,
    consume: bool = False,
    include_used: bool = False,
    app_secret_key: str | None = None,
) -> SourceConnectionRequest | None:
    with repo.sessions.begin() as session:
        request = session.scalar(
            select(SourceConnectionRequest).where(
                SourceConnectionRequest.source == SOURCE,
                SourceConnectionRequest.token_digest == _digest(token),
            )
        )
        if not request:
            return None
        expires = (
            request.expires_at.replace(tzinfo=timezone.utc)
            if request.expires_at.tzinfo is None
            else request.expires_at
        )
        if (
            (request.used_at and not include_used)
            or expires <= _now()
            or not _verify_token(app_secret_key, token, request)
        ):
            return None
        if consume:
            request.used_at = _now()
            request.status = "USED"
        return request


def connection_status(cfg: Settings, repo, discord_user_id: int | str | None = None) -> str:
    with repo.sessions() as session:
        query = select(SourceConnection).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        record = session.scalars(query.order_by(SourceConnection.updated_at.desc())).first()
    return record.status if record else "AUTH REQUIRED"


def _validate_storage_state(storage_state: dict) -> None:
    if (
        not isinstance(storage_state, dict)
        or not isinstance(storage_state.get("cookies"), list)
        or not isinstance(storage_state.get("origins", []), list)
    ):
        raise ConnectionError("JobStreet did not provide valid browser session state.")


def save_verified_session(cfg: Settings, repo, discord_user_id: int | str, storage_state: dict) -> None:
    """Encrypt valid Playwright state before it ever reaches persistence."""
    _validate_storage_state(storage_state)
    ciphertext = _fernet(cfg).encrypt(
        json.dumps(storage_state, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    now = _now()
    with repo.sessions.begin() as session:
        record = session.scalar(
            select(SourceConnection).where(
                SourceConnection.source == SOURCE,
                SourceConnection.discord_user_id == str(discord_user_id),
            )
        )
        if not record:
            record = SourceConnection(
                discord_user_id=str(discord_user_id), source=SOURCE
            )
            session.add(record)
        record.status = "READY"
        record.encrypted_session = ciphertext
        record.connected_at = record.connected_at or now
        record.last_verified_at = now
        record.updated_at = now
        record.last_error = None
    if hasattr(repo, "set_setting"):
        repo.set_setting("jobstreet_last_verified", now.isoformat())
        repo.set_setting("jobstreet_status", "READY")


def complete_request(
    cfg: Settings, repo, token: str, storage_state: dict
) -> str:
    """Atomically consume a valid connector token and save its encrypted state."""
    _validate_storage_state(storage_state)
    ciphertext = _fernet(cfg).encrypt(
        json.dumps(storage_state, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    now = _now()
    with repo.sessions.begin() as session:
        request = session.scalar(
            select(SourceConnectionRequest)
            .where(
                SourceConnectionRequest.source == SOURCE,
                SourceConnectionRequest.token_digest == _digest(token),
            )
            .with_for_update()
        )
        if not request:
            raise ConnectionError("This JobStreet connector link is invalid.")
        expires = (
            request.expires_at.replace(tzinfo=timezone.utc)
            if request.expires_at.tzinfo is None
            else request.expires_at
        )
        if request.used_at or expires <= now or not _verify_token(cfg.app_secret_key, token, request):
            raise ConnectionError("This JobStreet connector link is expired or already used.")
        record = session.scalar(
            select(SourceConnection).where(
                SourceConnection.source == SOURCE,
                SourceConnection.discord_user_id == request.discord_user_id,
            )
        )
        if not record:
            record = SourceConnection(
                discord_user_id=request.discord_user_id, source=SOURCE
            )
            session.add(record)
        record.status = "READY"
        record.encrypted_session = ciphertext
        record.connected_at = record.connected_at or now
        record.last_verified_at = now
        record.updated_at = now
        record.last_error = None
        request.used_at = now
        request.status = "COMPLETE"
        discord_user_id = request.discord_user_id
    if hasattr(repo, "set_setting"):
        repo.set_setting("jobstreet_last_verified", now.isoformat())
        repo.set_setting("jobstreet_status", "READY")
    log.info("jobstreet_session_connected user=%s", discord_user_id)
    return "READY"


def restore_latest_session(cfg: Settings, repo, destination: Path) -> bool:
    """Decrypt a READY session into private ephemeral storage for one browser run."""
    with repo.sessions() as session:
        record = session.scalars(
            select(SourceConnection)
            .where(
                SourceConnection.source == SOURCE,
                SourceConnection.status == "READY",
            )
            .order_by(SourceConnection.last_verified_at.desc())
        ).first()
        ciphertext = record.encrypted_session if record else None
    if not ciphertext:
        return False
    try:
        state = json.loads(
            _fernet(cfg).decrypt(ciphertext.encode("ascii"), ttl=None).decode("utf-8")
        )
        _validate_storage_state(state)
    except (InvalidToken, UnicodeError, ValueError, json.JSONDecodeError, ConnectionError):
        mark_status(repo, None, "SESSION EXPIRED", "Stored session could not be restored.")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    destination.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return True


def refresh_latest_session(cfg: Settings, repo, storage_state: dict) -> None:
    """Persist any cookies refreshed during a successful Render scan."""
    _validate_storage_state(storage_state)
    with repo.sessions() as session:
        record = session.scalars(
            select(SourceConnection)
            .where(
                SourceConnection.source == SOURCE,
                SourceConnection.status == "READY",
            )
            .order_by(SourceConnection.last_verified_at.desc())
        ).first()
        user_id = record.discord_user_id if record else None
    if user_id is not None:
        save_verified_session(cfg, repo, user_id, storage_state)


def mark_status(
    repo, discord_user_id: int | str | None, status: str, error: str | None = None
) -> None:
    with repo.sessions.begin() as session:
        query = select(SourceConnection).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        records = session.scalars(query).all()
        if discord_user_id is not None and not records:
            records = [
                SourceConnection(discord_user_id=str(discord_user_id), source=SOURCE)
            ]
            session.add(records[0])
        for record in records:
            record.status = status
            record.updated_at = _now()
            record.last_error = error
    if hasattr(repo, "set_setting"):
        repo.set_setting("jobstreet_status", status)


def disconnect(repo, discord_user_id: int | str) -> bool:
    with repo.sessions.begin() as session:
        record = session.scalar(
            select(SourceConnection).where(
                SourceConnection.source == SOURCE,
                SourceConnection.discord_user_id == str(discord_user_id),
            )
        )
        if not record:
            return False
        record.encrypted_session = None
        record.status = "AUTH REQUIRED"
        record.updated_at = _now()
        record.last_error = None
    return True


def has_managed_connection(repo, discord_user_id: int | str | None = None) -> bool:
    with repo.sessions() as session:
        query = select(SourceConnection.id).where(SourceConnection.source == SOURCE)
        if discord_user_id is not None:
            query = query.where(SourceConnection.discord_user_id == str(discord_user_id))
        return session.scalar(query.limit(1)) is not None


def windows_connector_python() -> str:
    """Python helper downloaded by the temporary Windows connector."""
    return r'''from __future__ import annotations
import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

def looks_like_login(url):
    lowered = (url or "").lower()
    return ("accounts.google." in lowered or
            any(marker in lowered for marker in ("/login", "/signin", "/sign-in", "auth/")))

async def authenticated(page):
    if looks_like_login(page.url):
        return False
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in ("sign out", "log out", "my profile", "my account")):
        return True
    return not any(marker in lowered for marker in ("continue with google", "sign in", "log in"))

async def run(args):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed. Run: py -m pip install playwright", file=sys.stderr)
        raise
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(args.login_url, wait_until="domcontentloaded", timeout=60000)
            print("Chromium is open. Complete JobStreet/Google login, 2FA, and CAPTCHA manually.")
            print("Waiting for JobStreet authentication...")
            deadline = asyncio.get_running_loop().time() + args.timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if await authenticated(page):
                    state = await context.storage_state()
                    body = json.dumps(state, separators=(",", ":")).encode("utf-8")
                    request = urllib.request.Request(
                        args.upload_url,
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-JobStreet-Connection-Token": args.token,
                        },
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=45) as response:
                            result = json.loads(response.read().decode("utf-8"))
                    except urllib.error.HTTPError as exc:
                        detail = exc.read().decode("utf-8", "replace")
                        raise RuntimeError("Session upload was rejected: " + detail[:240]) from exc
                    if result.get("status") != "READY":
                        raise RuntimeError("Session upload did not return READY.")
                    print("JobStreet is READY. Session uploaded successfully.")
                    return
                await page.wait_for_timeout(1500)
            raise TimeoutError("Authentication was not completed before the safety timeout.")
        finally:
            await context.close()
            await browser.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--upload-url", required=True)
    parser.add_argument("--login-url", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    asyncio.run(run(parser.parse_args()))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("JobStreet connector cancelled.")
'''


def windows_connector_ps1(base_url: str, token: str, cfg: Settings) -> str:
    setup_url = f"{base_url.rstrip('/')}/connect/jobstreet/{quote(token, safe='')}"
    helper_url = f"{setup_url}/connector.py"
    upload_url = f"{setup_url}/session"
    login_url = cfg.jobstreet_login_url or f"{cfg.jobstreet_base_url.rstrip('/')}/oauth/login"
    return f'''$ErrorActionPreference = "Stop"
$token = "{token}"
$helperUrl = "{helper_url}"
$uploadUrl = "{upload_url}"
$loginUrl = "{login_url}"
$temporaryHelper = Join-Path $env:TEMP ("jobstreet-connector-" + [guid]::NewGuid().ToString() + ".py")
try {{
  Invoke-WebRequest -UseBasicParsing -Uri $helperUrl -OutFile $temporaryHelper
  $python = Get-Command py -ErrorAction SilentlyContinue
  if ($python) {{
    & $python.Source -3 $temporaryHelper --token $token --upload-url $uploadUrl --login-url $loginUrl
  }} else {{
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {{ throw "Python 3 was not found. Install Python 3 and Playwright, then run this connector again." }}
    & $python.Source $temporaryHelper --token $token --upload-url $uploadUrl --login-url $loginUrl
  }}
  if ($LASTEXITCODE -ne 0) {{ throw "The local JobStreet connector exited with code $LASTEXITCODE." }}
}} finally {{
  Remove-Item -Force -ErrorAction SilentlyContinue $temporaryHelper
}}
'''


def windows_connector_cmd(base_url: str, token: str) -> str:
    setup_url = f"{base_url.rstrip('/')}/connect/jobstreet/{quote(token, safe='')}"
    return f'''@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing -Uri '{setup_url}/connector.ps1').Content))"
if errorlevel 1 pause
'''