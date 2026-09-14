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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from .config import Settings
from .models import SourceConnection, SourceConnectionRequest

SOURCE = "jobstreet"


class ConnectionError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fernet(cfg: Settings) -> Fernet:
    configured = (cfg.jobstreet_session_encryption_key or "").encode("ascii")
    # A dedicated key is supported, but requiring another manually managed
    # secret is unnecessary when the existing application secret is present.
    # Never store this derived key in the database with the ciphertext.
    value = configured
    if not value and cfg.app_secret_key:
        material = hashlib.sha256(
            b"after-hours-jobstreet-session-v1\x00" + cfg.app_secret_key.encode("utf-8")
        ).digest()
        value = base64.urlsafe_b64encode(material)
    if not value:
        raise ConnectionError("Set APP_SECRET_KEY or JOBSTREET_SESSION_ENCRYPTION_KEY before connecting JobStreet.")
    try:
        return Fernet(value)
    except (ValueError, TypeError) as exc:
        raise ConnectionError("JobStreet session encryption configuration is invalid.") from exc


def browserless_ready(cfg: Settings) -> bool:
    return bool(cfg.browserless_api_token and cfg.browserless_endpoint and (cfg.jobstreet_session_encryption_key or cfg.app_secret_key))


def create_request(repo, discord_user_id: int | str, ttl_seconds: int = 600) -> str:
    """Create one opaque, short-lived private connection link token."""
    token = secrets.token_urlsafe(32)
    with repo.sessions.begin() as session:
        session.add(SourceConnectionRequest(
            discord_user_id=str(discord_user_id), source=SOURCE, token_digest=_digest(token),
            nonce=secrets.token_hex(24), expires_at=_now() + timedelta(seconds=max(60, min(ttl_seconds, 600))),
        ))
    return token


def request_for_token(repo, token: str, consume: bool = False) -> SourceConnectionRequest | None:
    with repo.sessions.begin() as session:
        request = session.scalar(select(SourceConnectionRequest).where(
            SourceConnectionRequest.source == SOURCE,
            SourceConnectionRequest.token_digest == _digest(token),
        ))
        expires = request.expires_at.replace(tzinfo=timezone.utc) if request and request.expires_at.tzinfo is None else (request.expires_at if request else None)
        if not request or request.used_at or expires <= _now():
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
        for record in records:
            record.status = status; record.updated_at = _now(); record.last_error = error


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
